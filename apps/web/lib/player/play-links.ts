/**
 * 播放页地址与「退出回到哪」的约定（docs/design/web-player.md §6.10）。
 *
 * 地址就是分享凭证：只带 media_item_id（以 (kind, tmdb_id) 为锚，比库自增
 * id 稳定）+ sXXeYY 季集段 + 可选 `?t=` 秒。**导航状态（returnTo）永不进
 * 地址**——那是点进来的人的上下文，不是内容标识：分享出去的链接带着它，
 * 接收者退出播放会被送进分享者当时的页面（可能无权访问），地址还又长又丑。
 * 它走 sessionStorage：同一浏览器标签内有效，链接接收者自然回条目页兜底。
 */

/** sXXeYY（大小写不敏感）。解析不动就当电影播——地址是人手打的，别苛刻。 */
const UNIT_PATTERN = /^s(\d{1,3})e(\d{1,4})$/i;

export function parseUnitSegment(segment: string | undefined): {
  season: number;
  episode: number;
} | null {
  const match = segment?.match(UNIT_PATTERN) ?? null;
  if (!match) return null;
  return { season: Number(match[1]), episode: Number(match[2]) };
}

/** 生成播放页地址。季集号补零是媒体库的通用写法（s01e03），别自创。 */
export function playHref(
  mediaItemId: number,
  options?: { season?: number; episode?: number; tSeconds?: number },
): string {
  const { season, episode, tSeconds } = options ?? {};
  let href = `/play/${mediaItemId}`;
  if (season !== undefined && episode !== undefined) {
    href += `/s${String(season).padStart(2, "0")}e${String(episode).padStart(2, "0")}`;
  }
  if (tSeconds !== undefined && tSeconds > 0) href += `?t=${Math.floor(tSeconds)}`;
  return href;
}

const RETURN_KEY = "movieclaw.player.return-to";

/** 进播放页前记下「从哪来」。只收站内路径——防开放跳转。 */
export function rememberPlayerReturnPath(path: string): void {
  if (!path.startsWith("/")) return;
  try {
    window.sessionStorage.setItem(RETURN_KEY, path);
  } catch {
    // 隐私模式写不进：退出走条目页兜底，不算错误
  }
}

/** 退出播放时读「回哪去」；没有（直开链接 / 隐私模式）返回 null 走兜底。 */
export function playerReturnPath(): string | null {
  try {
    const value = window.sessionStorage.getItem(RETURN_KEY);
    return value && value.startsWith("/") ? value : null;
  } catch {
    return null;
  }
}
