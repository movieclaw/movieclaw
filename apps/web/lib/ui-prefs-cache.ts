import type { UiPreferences } from "@/lib/api/ui";

/**
 * 界面偏好的首帧缓存（localStorage）。
 *
 * 为什么需要：`ui.preferences` 是启动后异步拉回来的（见 lib/ui-prefs.tsx），
 * 在它返回之前全站只能按内置默认渲染——自定义过导航顺序的用户每次刷新都会
 * 先看到默认排序、接口回来后再重排一次，肉眼可见地"跳"一下（玻璃透明度、
 * 蒙版同理）。这里把上一次拿到的偏好整份存下来，下次启动时首帧就直接按它
 * 渲染，接口返回后再以服务端值为准纠正。
 *
 * 与 lib/backdrop-cache.ts 是同一套思路，同样刻意不 import 任何运行时模块
 * （只用 `import type`）：http.ts 在 401 时要清它，带运行时依赖会形成循环。
 *
 * 三条约定：
 *   - **缓存只是首帧优化**，真实状态永远以 GET /ui/preferences 为准；缓存脏了
 *     （换了设备上改的顺序）也只是首帧短暂显示旧值，接口回来即纠正；
 *   - 读出来的内容不可信（用户可改、也可能是旧版本写的），必须过一遍
 *     `normalizeUiPreferences` 补齐缺项，因此这里只负责 JSON 解析；
 *   - **账号切换时必须清除**，否则共享浏览器上换个账号登录会先闪出上一个人的
 *     导航顺序（清除点与背景缓存一致：登录成功、退出登录、401 跳登录页）。
 */
export const UI_PREFS_CACHE_KEY = "movieclaw.ui-prefs";

/** 读取上次缓存的偏好；无缓存 / 已损坏 / localStorage 不可用时返回 null。
 *  返回值是"生的"，调用方须自行补齐默认值。 */
export function readUiPrefsCache(): Partial<UiPreferences> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(UI_PREFS_CACHE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    // 只接受对象字面量：数组、字符串、null 都是被写坏的缓存，直接当没有
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed as Partial<UiPreferences>;
  } catch {
    return null;
  }
}

/** 写入首帧缓存。失败（隐私模式、配额满）只是失去首帧优化，不影响功能。 */
export function writeUiPrefsCache(prefs: UiPreferences): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(UI_PREFS_CACHE_KEY, JSON.stringify(prefs));
  } catch {
    // 忽略写入失败
  }
}

/** 清除首帧缓存。账号切换时必须调用，避免共享浏览器短暂显示上一账号的界面偏好。 */
export function clearUiPrefsCache(): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(UI_PREFS_CACHE_KEY);
  } catch {
    // localStorage 不可用只会失去首帧优化，不影响服务端的账号隔离。
  }
}
