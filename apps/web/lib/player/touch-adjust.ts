/**
 * 触屏滑动手势：竖滑调亮度/音量（左半屏亮度、右半屏音量），横滑拖进度——
 * 移动端播放器的通行三件套，iOS/Android 的本地播放器与 Jellyfin/Emby 手机端
 * 皆如此。
 *
 * 三种手势共用同一根手指，所以**方向只在位移首次过门槛时裁决一次**
 * （`classifyIntent`），之后到松手为止都不再改判：中途改判会让一次手势前半段
 * 调音量、后半段拖进度，用户完全无法预期自己在动哪个量。
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

/**
 * 指针落在某个元素上的**横向位置**，用元素自己的布局坐标表示。
 *
 * 直接写 `(clientX - rect.left) / rect.width` 在伪横屏下是错的，而且错得
 * 很隐蔽：容器整体 `rotate(90deg)` 之后，元素的布局 x 轴沿物理 y 轴，
 * `getBoundingClientRect()` 给的是**旋转后的外接矩形**——进度条量出来是
 * 「44 × 788」，`rect.width` 成了它的**厚度**（真机 390×844 视口实测）。
 * 于是整部片被映射到 44 个物理像素上：手指沿用户眼里的横向拖，clientX
 * 几乎不变、进度条不动；而横跨条厚度的一点点抖动却是几分钟的跳变。
 *
 * 返回 px 偏移与该轴的总长，调用方既能算比例，也能拿它直接定位气泡
 * （气泡的 `left` 也是布局坐标）。
 */
export function pointerOffsetX(
  point: { clientX: number; clientY: number },
  rect: { left: number; top: number; width: number; height: number },
  fakeLandscape: boolean,
): { offset: number; length: number } {
  // 布局 +x 沿物理 +y（rotate(90deg) 顺时针，与 toLayoutPoint 同一套换算）
  if (fakeLandscape) return { offset: point.clientY - rect.top, length: rect.height };
  return { offset: point.clientX - rect.left, length: rect.width };
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

export type SwipeIntent = "vertical" | "horizontal";

/**
 * 这次移动算不算「有意的滑动」，是的话是哪个方向（布局坐标）。
 *
 * 位移不够大就返回 null——那可能只是想点一下（轻点是控制层开关）。够大之后
 * 按主轴分：竖 = 亮度/音量，横 = 拖进度。正好 45° 归横向，与从前
 * 「竖直要严格大于水平」的判据一致。
 */
export function classifyIntent(
  layoutDeltaX: number,
  layoutDeltaY: number,
): SwipeIntent | null {
  const dx = Math.abs(layoutDeltaX);
  const dy = Math.abs(layoutDeltaY);
  if (Math.max(dx, dy) < ACTIVATE_PX) return null;
  return dy > dx ? "vertical" : "horizontal";
}

/**
 * 横滑跳转的灵敏度：划过**一整屏宽** = 这么多秒。
 *
 * 固定秒数而不是按片长取百分比：百分比在三小时的片子上一划就是十几分钟，
 * 停不准；90 秒是各家手机播放器的常见量级，一屏之内既够跨过片头，又能靠
 * 短距离微调。
 */
export const FULL_SWEEP_SEEK_S = 90;

/** 横向位移（布局坐标）→ 相对手势起点的毫秒增量。 */
export function seekDeltaMs(layoutDeltaX: number, layoutWidth: number): number {
  return Math.round((layoutDeltaX / Math.max(1, layoutWidth)) * FULL_SWEEP_SEEK_S * 1000);
}
