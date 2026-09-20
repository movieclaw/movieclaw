import { resolveRequestUrl } from "@/lib/http";

/** 后端允许的固定图片派生预设；固定枚举避免调用方制造任意尺寸缓存。 */
export type ImageVariant =
  | "landscape-card"
  | "poster-card"
  | "photo-tile"
  | "gallery-tile"
  | "photo-screen";

/**
 * 海报墙格子按主图比例挑派生预设：预设是等比缩放的外接框，横版封面（其他库
 * 的 16:9 抓帧 / 横版 -poster）套进竖框只能缩到 328×184，在 220px 起步的宽列上
 * 会糊；横图取横卡预设（480×270）才够 2x 屏。竖版海报仍走 poster-card。
 */
export function cardVariantFor(aspect: number | undefined): ImageVariant {
  return aspect !== undefined && aspect >= 1 ? "landscape-card" : "poster-card";
}

function appendVariant(url: string, variant: ImageVariant): string {
  return `${url}${url.includes("?") ? "&" : "?"}variant=${variant}`;
}

/**
 * 远程静态图片的统一收口入口。
 *
 * 所有 http(s) 绝对地址（TMDB 海报、豆瓣剧照、PT 站图床截图等）一律改走
 * 后端 /images/proxy：后端首次回源抓取后缓存到 data/cache/images，之后同一
 * URL 直接读本地磁盘，不再依赖外网图床的可达性与速度。
 * 非 http(s) 的相对路径（本地上传的背景图等）原样返回，不经代理。
 *
 * 新增图片展示位时请一律经过本函数，不要直接引用远程 URL。
 */
export function cachedImageUrl(url: string, variant?: ImageVariant): string {
  if (!/^https?:\/\//i.test(url)) return url;
  const path = `images/proxy?url=${encodeURIComponent(url)}`;
  return resolveRequestUrl(variant ? appendVariant(path, variant) : path);
}

/**
 * 后端给出的图片地址的通用解析：http(s) 绝对地址走缓存代理；
 * API 相对路径（本地刮削资产 /images/assets/...、条目美术图 /libraries/...）
 * 补上 API base 直连后端。图片可能来自两种形态的展示位统一用它。
 * （本注释存在仅作构建指纹：确认 dev server 重编译后反斜杠归一化已生效。）
 */
export function imageUrl(url: string | null, variant?: ImageVariant): string {
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) return cachedImageUrl(url, variant);
  // Windows 刮削器写库的资产路径带反斜杠（/images/assets/5\backdrop.jpg）。
  // img src 里浏览器会把 \ 归一成 /，但同一字符串进 CSS url("...") 时 \b 是
  // 十六进制转义（\bac → U+0BAC）、\p 等未知转义会吞掉反斜杠——沉浸背景层
  // 因此 404 变纯黑（视觉验收实测）。统一在入口归一成 /，所有消费位都安全。
  const normalized = url.replace(/\\/g, "/");
  // 当前只有 metadata 资产路由支持本地派生；文件缩略图等其它相对接口保持原样。
  const resolved =
    variant && /^\/?images\/assets\//.test(normalized)
      ? appendVariant(normalized, variant)
      : normalized;
  return resolveRequestUrl(resolved);
}

/**
 * 把经代理的 TMDB 图 URL 升级到 original 尺寸档（同一张图换分辨率）。
 * 用于大屏全幅场景（发现页 Hero、详情页沉浸背景）：w1280 拉伸到整屏会发虚。
 * 非 TMDB 图（豆瓣等没有 /t/p/ 尺寸段的）原样返回——没有更高清的档位可升。
 */
export function upgradedTmdbOriginalUrl(proxiedUrl: string): string {
  if (typeof window === "undefined") return proxiedUrl;
  try {
    const u = new URL(proxiedUrl, window.location.origin);
    const remote = u.searchParams.get("url");
    if (!remote || !/\/t\/p\/w\d+\//.test(remote)) return proxiedUrl;
    return cachedImageUrl(remote.replace(/\/t\/p\/w\d+\//, "/t/p/original/"));
  } catch {
    return proxiedUrl;
  }
}
