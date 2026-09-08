/**
 * 长按倍速（docs/design/player-feel.md §2.B2）。
 *
 * 按住画面一小会儿 → 二倍速播放，松手还原。手机播放器的通行手势
 * （B 站、抖音、ArtPlayer 的 fastForward 插件都是这一套）。
 *
 * **只做 2×，不做 3×**：转码会话是边转边给的单向流，3× 必然追上编码器，
 * 用户得到的是「快进两秒然后转圈」——比没有这个功能更糟。2× 也仍要护栏：
 * 缓冲一旦跟不上（video 发 `waiting`）就退出，见 `starve` 事件。
 *
 * 状态机是纯函数，计时器与 `playbackRate` 的接线在 video-player 组件里。
 */

/** 按住多久算长按（毫秒）。ArtPlayer 用 1000ms，按下去要等一秒才有反应，
 * 钝；B 站量级是 400~500ms，取 500 兼顾「不误触」与「按了就有」。 */
export const HOLD_SPEED_DELAY_MS = 500;

/** 长按期间的播放速率。 */
export const HOLD_SPEED_RATE = 2;

export type HoldSpeedState = "idle" | "pending" | "active";

export type HoldSpeedEvent =
  /** 手指按下（且当前允许长按，见 canHoldSpeed） */
  | "press"
  /** 按住够久了 */
  | "elapsed"
  /** 手指移动过门槛——这是横滑/竖滑手势，不是长按 */
  | "move"
  /** 抬手或手势被系统打断 */
  | "release"
  /** 缓冲跟不上：倍速吃光了前向缓冲 */
  | "starve";

/** 长按倍速的状态迁移。 */
export function holdSpeedReducer(state: HoldSpeedState, event: HoldSpeedEvent): HoldSpeedState {
  if (event === "press") return state === "idle" ? "pending" : state;
  if (event === "elapsed") return state === "pending" ? "active" : state;
  // 缓冲告急只掐**已经在吃缓冲**的那一档。`waiting` 在正常播放里也会发
  // （seek 之后、转码会话追编码器时），拿它一并否掉刚按下去的那 500 毫秒，
  // 表现就是「按住了却没反应」——而此刻还没有任何倍速在消耗缓冲。
  if (event === "starve") return state === "active" ? "idle" : state;
  // 手势改判与抬手一律回到起点
  return "idle";
}

/**
 * 现在能不能起长按。
 *
 * 暂停时不行——「按住画面让它快点播」在停着的画面上没有意义，而误触代价是
 * 用户以为自己点到了什么开关；锁屏时不行；多指不行（那是缩放/系统手势）。
 */
export function canHoldSpeed(input: {
  paused: boolean;
  locked: boolean;
  touchCount: number;
}): boolean {
  return !input.paused && !input.locked && input.touchCount === 1;
}
