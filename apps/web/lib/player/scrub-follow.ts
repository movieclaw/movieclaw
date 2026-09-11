/**
 * 拖动进度条时「画面什么时候跟过去」的节流裁决（docs/design/player-feel.md §2.C2）。
 *
 * 跟随本身是 C2 定下来的：跳转不要钱（档 0 直出 / 落点已在缓冲里）时，拖动中
 * 就该让画面跟着手指走。难点在**多久跟一次**——一次跟随是一次真的 seek，
 * 一秒六十次会把加载管线掐死；而跟得太稀，画面就永远停在手指早已离开的地方。
 *
 * 原先这里是一条「**前沿**节流」：`if (now - lastAt < 100) return`。它有两个
 * 在快速高频拖动下会当场暴露的毛病：
 *
 * 1. **落地的是窗口开头那个落点，窗口里后来的全被丢掉**。手指在 100 毫秒里
 *    能扫过 2 小时片子的一大截，于是画面拿到的是「100 毫秒之前手指在哪儿」。
 *    仿真里量到的中位差：0.4 秒扫过半部片时画面落后读数 **10 分钟**，来回
 *    抖动时 **16 分钟**——这正是「播放时间和展示进度对不上」的来源。
 * 2. **手指停下来不会触发任何跟随**。停住时不再有 pointermove，最后那个落点
 *    如果正好被窗口吃掉，画面就一直停在错的地方，直到松手才跳过去。
 *
 * 换成**后沿**：每次移动只记下最新落点并排一次延时落地，手指一停（60ms 没有
 * 新的移动）立刻跟过去；手指一直在扫时由 `SCRUB_FOLLOW_MAX_WAIT_MS` 兜底，
 * 保证连续拖动中画面仍以 5Hz 刷新，且每一次刷新用的都是**最新**落点。
 *
 * 纯函数在这里，计时器的接线在 video-player 组件里（与 seek-batch.ts 同样的
 * 分工）。
 */

/**
 * 手指停稳多久算「停下来了」。
 *
 * 60ms 约等于 4 帧：比人手抖的间隔长，比「停住了」的感知阈值短，所以停住时
 * 画面几乎是立刻跟过去的，而扫动途中的每一次移动都会把它重排掉。
 */
export const SCRUB_FOLLOW_SETTLE_MS = 60;

/**
 * 连续扫动时两次跟随之间的上限。
 *
 * 只有后沿的话，手指不停地动就永远等不到落地——画面在整个拖动过程中一动不动。
 * 100ms 沿用原先前沿节流的节奏：连续拖动时跟随频率与改动前**一模一样**
 * （10Hz，实测写 currentTime 的次数也一样），改变的只是每一次落地用的是
 * 当下最新的落点，而不是窗口开头那个已经过期的。
 */
export const SCRUB_FOLLOW_MAX_WAIT_MS = 100;

/**
 * 同一次拖动的判定窗口。
 *
 * 超过它没有新的跟随，下一次就算作用户**新的一次**跳转（QoE 的 seek_count
 * 只记每次拖动的第一次，见 player-feel.md §11「拖动跟随把 QoE 指标搅了」）。
 */
export const SCRUB_DRAG_GAP_MS = 500;

export interface ScrubFollowState {
  /** 上一次真的写进 video 的时刻。0 = 还没跟随过 */
  at: number;
  /** 这一次拖动里已经跟随了几次（0 = 下一次是本轮第一次，要计入 QoE） */
  count: number;
  /** 已排队、还没落地的最新落点。null = 没有在途的跟随 */
  pendingMs: number | null;
}

export function initialScrubFollowState(): ScrubFollowState {
  return { at: 0, count: 0, pendingMs: null };
}

export type ScrubFollowPlan =
  /** 什么都不做（这一跳不便宜，或落点不在本次会话里） */
  | { kind: "skip" }
  /** 立刻写 currentTime */
  | { kind: "follow" }
  /** 记下落点，`delayMs` 之后再落地；期间来新的移动就重排 */
  | { kind: "defer"; delayMs: number };

/**
 * 这一次 pointermove 该怎么处理。
 *
 * `cheap` 是 `isCheapSeek` 的结果（落点已缓冲 / 档 0 直出），`reachable` 是
 * 「落点落在本次会话的时间轴之内」（`toSessionSeconds >= 0`）。两者都成立才
 * 谈得上跟随。
 */
export function planScrubFollow(input: {
  now: number;
  state: ScrubFollowState;
  cheap: boolean;
  reachable: boolean;
}): ScrubFollowPlan {
  if (!input.cheap || !input.reachable) return { kind: "skip" };
  const waited = input.now - input.state.at;
  // 连续扫动的兜底：到点了就立刻用最新落点刷一次
  if (waited >= SCRUB_FOLLOW_MAX_WAIT_MS) return { kind: "follow" };
  // 否则排后沿。延时不能越过兜底时刻，否则连续扫动会被一路往后推
  const delayMs = Math.min(SCRUB_FOLLOW_SETTLE_MS, SCRUB_FOLLOW_MAX_WAIT_MS - waited);
  return { kind: "defer", delayMs: Math.max(0, delayMs) };
}

/**
 * 一次跟随真的落地之后的新状态。
 *
 * `count` 的语义与原先一致：同一次拖动（两次跟随间隔 < 500ms）里第二次及以后
 * 递增，间隔超过窗口则归零——`onSeeking` 靠它把「我们自己写的 currentTime」
 * 与「用户又跳了一次」区分开。
 */
export function afterScrubFollow(state: ScrubFollowState, now: number): ScrubFollowState {
  const continuing = now - state.at < SCRUB_DRAG_GAP_MS;
  return { at: now, count: continuing ? state.count + 1 : 0, pendingMs: null };
}
