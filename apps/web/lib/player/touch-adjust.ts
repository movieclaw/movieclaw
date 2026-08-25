/**
 * 触屏滑动调节：左半屏上下滑调亮度、右半屏上下滑调音量（移动端播放器的
 * 通行手势，iOS/Android 的本地播放器与 Jellyfin/Emby 手机端皆如此）。
 *
 * 平台事实决定了两件事怎么做：
 * - **亮度没有系统 API**。网页能做的是「画面亮度」，且实现必须是黑色遮罩
 *   压暗而不是 video 上的 CSS filter——iOS 的视频走独立合成层，filter 在
 *   真机上时常被绕过。只往暗调（0.1~1.0），往上提是拉爆高光。
 * - **音量在 iOS 上网页调不了**（WebKit 只认硬件侧键）。且不可调的表现随
 *   版本漂：老版本赋值被同步忽略，新版本先反映、稍后异步弹回 1——探测
 *   必须延时读回（见 video-player 的探测 effect）。不可调时胶囊改为提示
 *   「音量由系统侧键控制」，比手势毫无反应强。
 *
 * 本模块只放纯函数（分区判定、伪横屏坐标映射、增量换算），可表驱动单测；
 * 触摸事件的接线在 video-player 组件里。
 */

export type AdjustKind = "brightness" | "volume";

/** 亮度下限。调到全黑等于「屏幕坏了」，留一点画面让用户能滑回来。 */
export const MIN_BRIGHTNESS = 0.1;

/** 竖直方向滑过「可用区高度 × 这个比例」= 从 0 拉满。0.6 是各家手机播放器
 * 的常见手感：全程不用换手指，又不至于一碰就跳几十个百分点。 */
const FULL_SWEEP_RATIO = 0.6;

/** 判定为「有意的竖直滑动」前的最小位移（px）：小于它可能只是想点一下。 */
export const ACTIVATE_PX = 12;

/** 手势区的排除带（相对布局视口的比例）。
 * 顶部让开状态栏/顶栏与 iOS 通知中心下拉的起手区；底部让开进度条与控制区
 * ——在那里起手的竖直滑动多半是想去摸进度条，不该被劫持成调亮度。 */
const TOP_EXCLUDE = 0.12;
const BOTTOM_EXCLUDE = 0.24;
/** 屏幕左右缘的手势带宽度（px）。两处共用同一个值：边缘返回手势守卫
 * （video-player 里 preventDefault 的范围）和本模块的调节手势排除带——
 * 定义在一起才不会出现「守卫拦了、调节又接手」的缝隙。 */
export const EDGE_GUARD_PX = 32;

export interface ViewportInfo {
  /** 物理视口宽高（window.innerWidth/Height——触摸事件坐标所在的坐标系） */
  width: number;
  height: number;
  /** iOS 伪横屏：容器转了 90°，布局方向 ≠ 物理方向 */
  fakeLandscape: boolean;
}

/** 物理触点 → 布局坐标。
 *
 * 伪横屏是把容器顺时针转 90° 再平移（globals.css .player-fake-landscape）：
 * 布局 x 轴沿物理 y 轴向下，布局 y 轴沿物理 x 轴向左——即
 * `layoutX = physicalY`、`layoutY = width - physicalX`，布局视口尺寸对调。
 * 真横屏（方向锁）视口自己转了，直接用物理坐标。 */
export function toLayoutPoint(
  x: number,
  y: number,
  viewport: ViewportInfo,
): { x: number; y: number; width: number; height: number } {
  if (!viewport.fakeLandscape) {
    return { x, y, width: viewport.width, height: viewport.height };
  }
  return { x: y, y: viewport.width - x, width: viewport.height, height: viewport.width };
}

/** 起手点落在哪个手势区：左半 = 亮度，右半 = 音量，排除带 = null。 */
export function classifyTouchZone(
  physicalX: number,
  physicalY: number,
  viewport: ViewportInfo,
): AdjustKind | null {
  const p = toLayoutPoint(physicalX, physicalY, viewport);
  if (p.y < p.height * TOP_EXCLUDE || p.y > p.height * (1 - BOTTOM_EXCLUDE)) return null;
  if (p.x < EDGE_GUARD_PX || p.x > p.width - EDGE_GUARD_PX) return null;
  return p.x < p.width / 2 ? "brightness" : "volume";
}

/** 手势起点值 + 竖直位移（布局坐标，向上为负）→ 新值。
 * 向上滑增大——与所有手机播放器一致；clamp 到该量的合法区间。 */
export function applySwipe(
  kind: AdjustKind,
  startValue: number,
  layoutDeltaY: number,
  layoutHeight: number,
): number {
  const sweep = Math.max(1, layoutHeight * FULL_SWEEP_RATIO);
  const next = startValue + -layoutDeltaY / sweep;
  const min = kind === "brightness" ? MIN_BRIGHTNESS : 0;
  return Math.min(1, Math.max(min, next));
}

/** 这次移动算不算「有意的竖直滑动」：位移够大且明显竖直（布局坐标）。 */
export function isVerticalIntent(layoutDeltaX: number, layoutDeltaY: number): boolean {
  return Math.abs(layoutDeltaY) >= ACTIVATE_PX && Math.abs(layoutDeltaY) > Math.abs(layoutDeltaX);
}
