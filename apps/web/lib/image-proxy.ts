import { resolveRequestUrl } from "@/lib/http";

/** 后端允许的固定图片派生预设；固定枚举避免调用方制造任意尺寸缓存。 */
export type ImageVariant = "landscape-card" | "poster-card" | "photo-tile";

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
 */
export function imageUrl(url: string | null, variant?: ImageVariant): string {
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) return cachedImageUrl(url, variant);
  // 当前只有 metadata 资产路由支持本地派生；文件缩略图等其它相对接口保持原样。
  const resolved =
    variant && /^\/?images\/assets\//.test(url) ? appendVariant(url, variant) : url;
  return resolveRequestUrl(resolved);
}
