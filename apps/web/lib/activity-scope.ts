/**
 * 活动页「观看」视角的可见范围口径（docs/design/activity.md「范围切换」）。
 *
 * - `visible`：按超管自己的浏览范围折叠范围外记录（默认）。`admin_visible`
 *   是超管给自己设的隐藏意图——典型场景是把不刮削的内容放进「其他」库并对
 *   自己隐藏，开活动页时旁边有人也不该看到那些片名；
 * - `all`：管控视角的全量口径，跨成员、跨库都看。它不是安全边界（超管随时
 *   能把库翻回可见），所以只是一个显式切换，选过的档记在本浏览器里。
 */

import type { MediaActivityScope } from "@/lib/api/playback";

const STORAGE_KEY = "movieclaw.activity.scope";

/** 读持久化的范围口径。没存过、存的值非法、或没有 localStorage 都回默认。 */
export function loadActivityScope(): MediaActivityScope {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw === "all" ? "all" : "visible";
  } catch {
    return "visible";
  }
}

export function saveActivityScope(scope: MediaActivityScope): void {
  try {
    if (scope === "visible") window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, scope);
  } catch {
    // 隐私模式下写不进：本次会话内仍生效，只是不记住
  }
}
