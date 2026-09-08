/**
 * 触屏轻点的单/双击判定（docs/design/player-feel.md §2.B1）。
 *
 * **不用 `dblclick` 事件**：触屏上它的触发条件各家浏览器不一，两指、轻扫、
 * 点得稍慢都可能不发；ArtPlayer / jellyfin-web 也都是自己按时间窗数 tap。
 *
 * 分区照 YouTube 手机端：左右各三分之一双击 = 后退/前进十秒，中间那条留给
 * 控制层开关——整块画面都能双击跳转的话，想点开控制条的人会莫名其妙地跳走。
 */

/** 两次轻点算「双击」的时间窗（毫秒）。300ms 是各家的通行取值。 */
export const DOUBLE_TAP_WINDOW_MS = 300;

/** 双击跳转的步长（秒），与中央簇那两颗按钮、键盘 J/L 一致。 */
export const TAP_SEEK_SECONDS = 10;

/** 左右手势区各占画面宽度的比例。 */
export const TAP_SIDE_RATIO = 1 / 3;

export type TapAction =
  /** 控制层开关（单击，或落在中间区的双击） */
  | { type: "chrome" }
  /** 跳转：负=后退 */
  | { type: "seek"; seconds: number };

/**
 * 这一下轻点该做什么。
 *
 * `xRatio` 是触点在画面里的横向比例（0=最左，1=最右）；`lastTapMs` 传上一次
 * 轻点的时刻，null = 这是本轮第一下。
 *
 * **第一下永远是控制层开关**，不为了等双击而延迟 300 毫秒——「点一下唤出
 * 控制条」是这个播放器最高频的交互，让它慢 300ms 换双击跳转不划算。第二下
 * 命中左右区时由调用方把控制层恢复原状，净效果就只剩跳转。
 */
export function resolveTap(input: {
  nowMs: number;
  lastTapMs: number | null;
  xRatio: number;
}): TapAction {
  const { nowMs, lastTapMs, xRatio } = input;
  const isDouble = lastTapMs !== null && nowMs - lastTapMs <= DOUBLE_TAP_WINDOW_MS;
  if (!isDouble) return { type: "chrome" };
  if (xRatio < TAP_SIDE_RATIO) return { type: "seek", seconds: -TAP_SEEK_SECONDS };
  if (xRatio > 1 - TAP_SIDE_RATIO) return { type: "seek", seconds: TAP_SEEK_SECONDS };
  return { type: "chrome" };
}
