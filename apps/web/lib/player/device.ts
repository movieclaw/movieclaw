/**
 * 网页播放器的设备标识（对齐 Jellyfin 客户端的 DeviceId 语义）。
 *
 * 活动页「正在播放」按设备区分会话：Jellyfin 客户端天然带 DeviceId，浏览器
 * 没有——登录 Cookie 里只有用户名和过期时间。这里给每个浏览器生成一个稳定
 * 标识存在 localStorage，随进度上报与开会话请求一起带给服务端，同一成员在
 * 两台浏览器上的播放才能各自成一张卡片，而不是互相覆盖。
 *
 * 它只是展示锚点，不参与任何授权判定：身份永远来自登录会话。
 */

import { nanoid } from "nanoid";

const STORAGE_KEY = "movieclaw.player.device-id";

let memoized: string | null = null;

/** 本浏览器的设备标识；首次调用生成并持久化，localStorage 不可用时本次会话内保持稳定。 */
export function getPlayerDeviceId(): string {
  if (memoized) return memoized;
  let stored: string | null = null;
  try {
    stored = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    stored = null;
  }
  const id = stored && /^[A-Za-z0-9_-]{8,64}$/.test(stored) ? stored : nanoid(21);
  if (id !== stored) {
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // 隐私模式下写不进：本次会话内仍稳定，刷新后换一个而已
    }
  }
  memoized = id;
  return id;
}

/** 仅测试使用：清掉进程内缓存，让下一次调用重新读存储。 */
export function resetPlayerDeviceIdForTests(): void {
  memoized = null;
}
