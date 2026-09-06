/**
 * 影片分享（docs/design/media-share.md）的纯逻辑：有效期档位、访问码生成、
 * 链接补全、可直接发给别人的复制文本。无 React / Next 依赖，`node --test`
 * 直接跑（test/share.test.mjs）。
 */

/** 有效期档位（天）。没有「永久」——一条链接不该在聊天记录里躺一年（设计文档 §2.2）。 */
export const SHARE_EXPIRY_OPTIONS: readonly { days: number; label: string }[] = [
  { days: 1, label: "1 天" },
  { days: 3, label: "3 天" },
  { days: 7, label: "7 天" },
  { days: 30, label: "30 天" },
];

export const DEFAULT_SHARE_EXPIRY_DAYS = 7;

/** 访问码字符集：小写字母数字，去掉 0/o/1/l 这种口头转述会混淆的字符。 */
const PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789";
export const SHARE_PASSWORD_LENGTH = 6;
export const SHARE_PASSWORD_MIN = 4;
export const SHARE_PASSWORD_MAX = 32;

/** 生成一个 6 位访问码。`random` 可注入（测试用），默认 crypto 随机。 */
export function generateSharePassword(random: () => number = secureRandom): string {
  let out = "";
  for (let i = 0; i < SHARE_PASSWORD_LENGTH; i += 1) {
    const index = Math.min(
      PASSWORD_ALPHABET.length - 1,
      Math.floor(random() * PASSWORD_ALPHABET.length),
    );
    out += PASSWORD_ALPHABET[index];
  }
  return out;
}

function secureRandom(): number {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.getRandomValues) {
    const buf = new Uint32Array(1);
    cryptoApi.getRandomValues(buf);
    return buf[0] / 2 ** 32;
  }
  return Math.random();
}

/** 密码校验：空 = 不设密码；非空须在长度边界内。返回错误文案，合法为 null。 */
export function validateSharePassword(password: string): string | null {
  const cleaned = password.trim();
  if (!cleaned) return null;
  if (cleaned.length < SHARE_PASSWORD_MIN || cleaned.length > SHARE_PASSWORD_MAX) {
    return `密码长度须在 ${SHARE_PASSWORD_MIN}–${SHARE_PASSWORD_MAX} 位之间`;
  }
  return null;
}

/**
 * 分享链接补全：后端在未配置外部访问地址时给的是相对路径 `/s/{slug}`，
 * 用当前页面的 origin 补成绝对地址；已经是绝对地址的原样返回。
 */
export function absoluteShareUrl(url: string, origin: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  const base = origin.replace(/\/+$/, "");
  return url.startsWith("/") ? `${base}${url}` : `${base}/${url}`;
}

/** 后端给的是不是相对路径（= 未配置外部访问地址，对话框据此给提示）。 */
export function isRelativeShareUrl(url: string): boolean {
  return !/^https?:\/\//i.test(url);
}

/** 「复制链接和密码」的文本：一段可以直接粘进聊天框的话。 */
export function shareCopyText(title: string, url: string, password: string | null): string {
  const parts = [`《${title}》`, `链接：${url}`];
  if (password) parts.push(`密码：${password}`);
  return parts.join(" ");
}

/**
 * 到期提示：「N 天后失效」/「N 小时后失效」/「即将失效」。绝对时间由调用方
 * 另配（lib/time.ts 的 formatDateTime）。
 */
export function expiryHint(expiresAtIso: string, nowMs: number = Date.now()): string {
  const remainingMs = Date.parse(expiresAtIso) - nowMs;
  if (!Number.isFinite(remainingMs) || remainingMs <= 0) return "已失效";
  const hours = remainingMs / 3_600_000;
  if (hours >= 47) return `${Math.round(hours / 24)} 天后失效`;
  if (hours >= 1) return `${Math.floor(hours)} 小时后失效`;
  const minutes = Math.max(1, Math.floor(remainingMs / 60_000));
  return `${minutes} 分钟后失效`;
}

/** 分享页地址（站内相对路径）。 */
export function sharePath(slug: string): string {
  return `/s/${encodeURIComponent(slug)}`;
}

/** 分享页里的播放地址：与 /play 同一套 sXXeYY + ?t= 约定。 */
export function sharePlayPath(
  slug: string,
  options?: { season?: number; episode?: number; tSeconds?: number },
): string {
  const { season, episode, tSeconds } = options ?? {};
  let href = `${sharePath(slug)}/play`;
  if (season !== undefined && episode !== undefined) {
    href += `/s${String(season).padStart(2, "0")}e${String(episode).padStart(2, "0")}`;
  }
  if (tSeconds !== undefined && tSeconds > 0) href += `?t=${Math.floor(tSeconds)}`;
  return href;
}
