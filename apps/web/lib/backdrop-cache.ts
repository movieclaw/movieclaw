/** 首帧背景缓存。账号切换时必须清除，避免共享浏览器短暂显示上一账号的背景。 */
export const BACKDROP_CACHE_KEY = "movieclaw.backdrop";

/** 写入首帧背景缓存（后端给的相对地址，layout.tsx 的内联脚本只认 / 开头）。
 *  失败（隐私模式、配额满）只是失去首帧优化。 */
export function writeBackdropCache(url: string): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(BACKDROP_CACHE_KEY, url);
  } catch {
    // 忽略写入失败
  }
}

export function clearBackdropCache(): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(BACKDROP_CACHE_KEY);
  } catch {
    // localStorage 不可用只会失去首帧优化，不影响服务端的账号隔离。
  }
}
