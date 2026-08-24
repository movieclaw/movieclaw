/**
 * 横屏（全屏 + 锁定横向）。
 *
 * **顺序不能反**：`screen.orientation.lock()` 在浏览器里只对已经全屏的文档
 * 生效，先锁再全屏必然抛 `NotSupportedError`。所以一律「先全屏、再锁方向」，
 * 而且锁失败**不能**回滚全屏——桌面浏览器根本没有方向可锁，那里全屏本身
 * 就是用户要的结果。
 *
 * 三种结局要分开，因为它们对应三种完全不同的设备，而按钮的可用性判断只能
 * 靠它们：
 *
 * - `locked`：手机 / 平板，全屏了也锁了横向，这是这个按钮的本职；
 * - `fullscreen`：桌面，方向锁不可用，但全屏拿到了，画面就是横的，等于达成；
 * - `unsupported`：连全屏都进不去（典型是 iPhone Safari，元素级全屏 API
 *   不存在），此时退回让 `<video>` 走系统原生全屏——iOS 上本来就不硬撑
 *   自定义 UI（见 docs/design/web-player.md §6.4）。
 */

export type LandscapeOutcome = "locked" | "fullscreen" | "unsupported";

/** 只取用到的那两个方法，测试因此不必造真的 DOM 元素。 */
export interface FullscreenTarget {
  requestFullscreen?: () => Promise<void>;
  /** 老 WebKit（含部分 iPadOS Safari）只有带前缀的那个 */
  webkitRequestFullscreen?: () => Promise<void> | void;
}

export interface OrientationLock {
  lock?: (orientation: string) => Promise<void>;
  unlock?: () => void;
}

/** iPhone Safari 的兜底：只有 `<video>` 自己能进系统全屏。 */
export interface NativeVideoFullscreen {
  webkitEnterFullscreen?: () => void;
}

/**
 * 把浏览器里这两个 TS DOM 类型至今没收录的东西收口在这一处。
 *
 * `ScreenOrientation.lock/unlock` 与 `HTMLVideoElement.webkitEnterFullscreen`
 * 都是浏览器实装多年、类型定义里却没有的 API。断言散落在调用处会变成一堆
 * 无从解释的 `as unknown as`，所以只在这里断言一次，并写清为什么。
 */
export function screenOrientation(): OrientationLock | undefined {
  if (typeof screen === "undefined") return undefined;
  return screen.orientation as unknown as OrientationLock | undefined;
}

export function asNativeVideo(video: HTMLVideoElement | null): NativeVideoFullscreen | null {
  return video as unknown as NativeVideoFullscreen | null;
}

/** 进入全屏。带前缀的那个可能返回 undefined，统一成 Promise。 */
/** 元素级全屏。返回是否进成了——iPhone Safari 没有这个能力，返回 false。 */
export async function requestFullscreen(target: FullscreenTarget): Promise<boolean> {
  const request = target.requestFullscreen ?? target.webkitRequestFullscreen;
  if (!request) return false;
  try {
    await request.call(target);
    return true;
  } catch {
    // 用户拒绝、或不在手势上下文里。不是错误，只是没进成
    return false;
  }
}

/**
 * 全屏并锁横向。
 *
 * 返回 `unsupported` 时调用方应当退回 `video.webkitEnterFullscreen()`
 * （见 `enterNativeVideoFullscreen`）。
 */
export async function enterLandscape(
  target: FullscreenTarget,
  orientation: OrientationLock | undefined,
): Promise<LandscapeOutcome> {
  if (!(await requestFullscreen(target))) return "unsupported";
  if (!orientation?.lock) return "fullscreen";
  try {
    await orientation.lock("landscape");
    return "locked";
  } catch {
    // 桌面浏览器普遍有这个方法但一调就抛。全屏已经拿到了，不回滚。
    return "fullscreen";
  }
}

/** iPhone Safari 兜底：把 `<video>` 交给系统播放器。成功返回 true。 */
export function enterNativeVideoFullscreen(video: NativeVideoFullscreen | null): boolean {
  if (!video?.webkitEnterFullscreen) return false;
  video.webkitEnterFullscreen();
  return true;
}

/**
 * 退出横屏。
 *
 * 解锁必须先于退全屏：退了全屏之后 `unlock()` 在部分浏览器上会抛，
 * 结果是方向锁留在那儿，用户退出播放器后整个页面还被钉在横屏。
 */
export async function exitLandscape(
  orientation: OrientationLock | undefined,
  exitFullscreen: (() => Promise<void>) | null,
): Promise<void> {
  try {
    orientation?.unlock?.();
  } catch {
    // 没锁过、或浏览器不支持解锁：都不影响接下来退全屏
  }
  if (!exitFullscreen) return;
  try {
    await exitFullscreen();
  } catch {
    // 已经不在全屏里了
  }
}
