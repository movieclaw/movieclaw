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

export interface ScrubFollowState {
  /** 上一次真的写进 video 的时刻。0 = 还没跟随过 */
  at: number;
  /** 已排队、还没落地的最新落点。null = 没有在途的跟随 */
  pendingMs: number | null;
}

export function initialScrubFollowState(): ScrubFollowState {
  return { at: 0, pendingMs: null };
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
 * `cheap` 是 `isCheapSeek` 的结果（落点已在缓冲里），`reachable` 是「落点落在
 * 本次会话的时间轴之内」（`toSessionSeconds >= 0`）。便宜且可达才按 10Hz
 * 跟着手指走。
 *
 * `settleOnly` 是档 0 直出的口径：整个文件都能 seek，但**不等于不要钱**——
 * 远程的进度 MP4 在手机上每一次 seek 都是一条新的 Range 请求外加播放器重新
 * 起解码，扫动途中一秒十次、每次都被下一次打断，画面追不上手指还一路抽
 * （2026-09-13 真机反馈）。所以直出档拖出缓冲之后只在**手指停住**时补一次
 * 跟随：停下来看一眼落点是用户真的想要的，扫动途中的十几次不是。局域网上
 * 这一跳本来就快，停住 60ms 之内照样能看到画面，牺牲的只有「扫动途中画面
 * 跟着抽」这件本来就不该有的事。
 */
export function planScrubFollow(input: {
  now: number;
  state: ScrubFollowState;
  cheap: boolean;
  reachable: boolean;
  settleOnly?: boolean;
}): ScrubFollowPlan {
  if (!input.reachable) return { kind: "skip" };
  if (!input.cheap) {
    // 不便宜但允许停住时跟一次：只排后沿、不设兜底，手指不停就永远不落地。
    // 每次移动都重排，所以真正落地的只有手指停稳之后那一次
    return input.settleOnly
      ? { kind: "defer", delayMs: SCRUB_FOLLOW_SETTLE_MS }
      : { kind: "skip" };
  }
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
 * 这里曾经还带一个 `count`（同一次拖动里跟随了几次），供 `onSeeking` 把「我们
 * 自己写的 currentTime」与「用户又跳了一次」区分开，好让 QoE 的 `seek_count`
 * 不被灌脏。现在计数改由跳转入口直接发 `seek-requested`（见 qoe.ts），元素的
 * `seeking` 事件只负责开「这段等待不算卡顿」的闸、不再计数——这一类从结构上
 * 就不存在了，`count` 随之没有用处。
 */
export function afterScrubFollow(now: number): ScrubFollowState {
  return { at: now, pendingMs: null };
}

/**
 * 抬手时该提交到哪儿。
 *
 * 鼠标用**指针最后所在处**（`lastPointerMs`）：指针输入是合帧的（player-feel.md
 * §2.C4），最后一次移动可能还压在这一帧里没落到 `dragging` 上，快速拖动时一帧
 * 的位移在两小时的片子上就是好几分钟，提交上一帧那个值等于丢掉最后一段。
 *
 * 触屏反过来：指腹是从屏幕上**剥离**的，抬起瞬间接触点会挪几个像素，手指
 * 滑出进度条再抬更是如此，而指针捕获把这段位移一并收了进来。用户眼里进度条
 * 停在哪儿他就是要跳到哪儿，落点比屏幕上的值差出几十秒，看起来就是「松手
 * 后进度往回跳了一下」（2026-09-13 真机反馈）。所以触屏提交**屏幕上正显示的
 * 值**（`draggingMs`）——它是上一帧落地的读数，抬手漂移进不来。
 *
 * 笔跟鼠标走：笔尖抬起没有指腹那种剥离位移。
 */
export function scrubCommitTarget(input: {
  pointerType: string;
  lastPointerMs: number | null;
  draggingMs: number;
}): number {
  if (input.pointerType === "touch") return input.draggingMs;
  return input.lastPointerMs ?? input.draggingMs;
}
