import { getAppearance } from "@/lib/api/appearance";
import { fetchUiPreferences } from "@/lib/api/ui";
import { clearBackdropCache, writeBackdropCache } from "@/lib/backdrop-cache";
import { clearUiPrefsCache, writeUiPrefsCache } from "@/lib/ui-prefs-cache";

/**
 * 账号变了之后的整页跳转：登录成功、切换账号、退出 / 移除当前账号（自动切到下一个）、
 * 添加账号、全部退出（docs/design/account-switching.md）。
 *
 * 为什么要整页跳：工作台各 Store、AuthGate 的会话缓存都是模块级内存，整页刷新才能
 * 保证一点不串到上一个账号。
 *
 * 为什么不能「清了缓存就跳」：背景图与界面偏好各有一份首帧缓存（lib/backdrop-cache、
 * lib/ui-prefs-cache），layout.tsx 的内联脚本在首帧绘制前按它恢复壁纸、主题与外壳几何。
 * 它们按账号区分，换人时旧的必须作废；但只清不补，新页面首帧就只能按内置默认壁纸、
 * 默认偏好画，等 /appearance、/ui/preferences 回来再整屏换壁纸、重排媒体库首页——
 * 手机上就是「启动页 → 默认壁纸 → 换成自己的壁纸 → 内容重排」连着闪好几下
 * （2026-09-26 用户反馈「切换账号来回闪动」，隔离环境逐帧录屏复现）。
 *
 * 所以跳转前还登录着（切到了另一个账号）时，趁 Cookie 已换成新身份，先把新身份的两份
 * 缓存取回来写好，并把壁纸图预载进浏览器缓存（该接口带一年期缓存头），新页面首帧即按
 * 新账号渲染。最多等 PRIME_TIMEOUT_MS：取不到就按清空后的默认首帧走，绝不让跳转卡住。
 *
 * @param href 跳转目标
 * @param loggedIn 跳过去时是否处于登录态；false（去登录页）时只清缓存
 */
export async function reloadAfterAccountChange(href: string, loggedIn: boolean): Promise<void> {
  clearBackdropCache();
  clearUiPrefsCache();
  if (loggedIn) {
    await Promise.race([
      primeFirstFrameCaches(),
      new Promise<void>((resolve) => window.setTimeout(resolve, PRIME_TIMEOUT_MS)),
    ]);
  }
  window.location.href = href;
}

/** 备缓存的等待上限：切换请求已经成功，再慢也只多等这一小会儿 */
const PRIME_TIMEOUT_MS = 1500;

/** 取当前（新）身份的界面偏好与外观写进首帧缓存；任一失败都不影响另一项 */
async function primeFirstFrameCaches(): Promise<void> {
  await Promise.allSettled([
    fetchUiPreferences().then(writeUiPrefsCache),
    getAppearance().then((view) => {
      // 没设壁纸的账号保持清空：首帧就是内置默认图，本来就对
      if (!view.active_url) return;
      writeBackdropCache(view.active_url);
      return preloadImage(view.active_url);
    }),
  ]);
}

/** 让壁纸先进浏览器缓存：新页面首帧设上 --backdrop-image 时直接出图，不先空一拍 */
function preloadImage(url: string): Promise<void> {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve();
    img.onerror = () => resolve();
    img.src = url;
  });
}
