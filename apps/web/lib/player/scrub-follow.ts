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
 * 值**——合帧上一次刷出去的读数（`flushedMs`），压在最后一帧里没刷出去的
 * 抬手漂移进不来。
 *
 * **不能拿 React 渲染出来的 `dragging` 当这个「屏幕值」**（2026-09-14 反馈：
 * 快速甩到目标立刻松手，落点回到按下的位置）。它是上一次渲染时的闭包值，而
 * 渲染是异步排队的：手指甩得快时，合帧的 rAF 一次都还没跑、或者跑了但 React
 * 还没来得及渲染，`pointerup` 就到了，闭包里的 `dragging` 仍是按下那一刻的值
 * ——两小时的片子上差出几十分钟。Chromium 连发触摸事件复现：提交 45 秒、目标
 * 24 分钟。合帧刷出去的值走 ref，与渲染节奏无关；一次都没刷出去（整个甩动压在
 * 一帧里）就退回指针最后所在处——那时屏幕上什么都还没动，剥离漂移无从谈起，
 * 指针最后所在处就是用户要的位置。`draggingMs` 只剩量不到指针位置时的兜底。
 *
 * 笔跟鼠标走：笔尖抬起没有指腹那种剥离位移。
 */
export function scrubCommitTarget(input: {
  pointerType: string;
  /** 指针最后所在处（含压在最后一帧里没刷出去的那次移动） */
  lastPointerMs: number | null;
  /** 合帧最近一次刷到屏幕上的落点；这次拖动一次都没刷出去时为 null */
  flushedMs: number | null;
  /** React 渲染出来的拖动值，只做最后的兜底 */
  draggingMs: number;
}): number {
  if (input.pointerType === "touch") {
    return input.flushedMs ?? input.lastPointerMs ?? input.draggingMs;
  }
  return input.lastPointerMs ?? input.draggingMs;
}

/**
 * 进度条那个 `<input type="range">` 的步长（毫秒）。
 *
 * 只有键盘（Home / End / PageUp / PageDown）还在用它——方向键被全局快捷键接管
 * 了（shortcuts.ts）。拖动走的是指针事件自己算的落点，与这个步长无关。
 */
export const SCRUB_INPUT_STEP_MS = 1000;

/**
 * 浏览器会把写进 range 的 value **整理**成什么数（HTML 规范的「step 不匹配」
 * 处理）：先夹进 `[0, max]`，再对齐到最近的步长倍数（两边一样近取大的），对齐
 * 之后越过 max 就退一格。
 *
 * 为什么要在这里把这条规则写一遍：受控 input 的 `value` 由我们写，浏览器读回
 * 来的却是整理过的数。写 `1489320` 读回 `1489000`——两边不一致，React 就会把
 * 下一次原生 `input`/`change` 事件当成「用户改了值」派发 onChange（它靠比对
 * 「上次写入的」与「现在读到的」来判断）。抬手之后原生滑块补发的那次 `change`
 * 正是踩在这条缝里（见 acceptsNativeScrubValue）。写进去的值先按同一条规则
 * 整理好，两边从此一致，这条缝在结构上就不存在了。
 */
export function scrubInputValue(
  shownMs: number,
  durationMs: number | null,
  stepMs = SCRUB_INPUT_STEP_MS,
): number {
  const max = durationMs && durationMs > 0 ? durationMs : 0;
  if (!Number.isFinite(shownMs) || max <= 0) return 0;
  const clamped = Math.min(max, Math.max(0, shownMs));
  let aligned = Math.round(clamped / stepMs) * stepMs;
  if (aligned > max) aligned -= stepMs;
  return Math.max(0, aligned);
}

/**
 * 原生 range 事件（`input` / `change`）送来的值要不要当成一次键盘拖动。
 *
 * 进度条的拖动是指针事件自己算的（iOS 按不中 1px 的原生把手，见组件里的注释），
 * 但**原生滑块并没有因此关掉**：桌面上鼠标按下轨道、Chromium 触屏按下轨道、
 * iOS 上手指恰好压在那 1px 把手所在的一列，浏览器都会同时跑自己的那套拖动——
 * 拖动中连发 `input`，抬手时再补一次 `change`。抬手那次是致命的：它在
 * `pointerup` **之后**到，而 `pointerup` 已经提交跳转、把 `dragging` 清成了
 * null；这一发 `change` 一进 onChange 就把 `dragging` 重新钉在落点上，从此圆点与
 * 时间读数都不再跟画面走，直到下一次按下（2026-09-14 反馈：松手后圆点不动。
 * 鼠标次次中招，触屏只在落点没对齐步长时中招，iOS 上还要先按中那一列——所以
 * 表现是「偶尔」）。
 *
 * 两条规则：
 * 1. 指针正按着时一律不收——落点由指针路径算，原生滑块那份是重复且更糙的；
 * 2. 与屏幕上正显示的值整理后相同的不收——那不是用户改了值，只是浏览器把我们
 *    写进去的数读回来了（抬手补发的 `change` 恒是这一种）。键盘真按了
 *    Home / End / PageUp 时值一定不同，照常放行。
 */
export function acceptsNativeScrubValue(input: {
  nativeValue: number;
  shownMs: number;
  durationMs: number | null;
  pointerDragging: boolean;
}): boolean {
  if (input.pointerDragging) return false;
  if (!Number.isFinite(input.nativeValue)) return false;
  return input.nativeValue !== scrubInputValue(input.shownMs, input.durationMs);
}
