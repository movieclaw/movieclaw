/**
 * 播放质量采集（docs/design/web-player.md §8）。
 *
 * 按 CTA-2066 的口径算，不自创——这样「卡顿率」在这里和在别处是同一个东西。
 *
 * 两条最容易出错的地方，也是这个模块单独存在的理由：
 *
 * - **首帧只能用 `requestVideoFrameCallback` 量**。`canplay` / `playing` /
 *   `loadeddata` 全都早于真实出画（有时早几百毫秒），用它们量会系统性偏乐观，
 *   然后困惑「数据好看但用户说慢」。
 * - **卡顿必须排除 seek 引起的 `waiting`**。不排除的话用户拖一下进度条就被
 *   记成一次卡顿，数据全废。**排除的闸必须在「用户要求跳转」那一刻就打开**，
 *   不能等 video 的 `seeking` 事件——换会话那条路上它一次都不会来（见
 *   `seek-requested`），于是拖一下就多记一次卡顿，正是这条要防的事。
 *
 * 遥测**只落本地**（硬边界 3）：写进自己的数据库、设置页可看，绝不外发。
 */

export interface QoeSummary {
  /** 点击播放 → 第一帧真正渲染（毫秒）。拿不到为 null */
  ttff_ms: number | null;
  /** 卡顿总时长（毫秒），不含 seek 造成的等待 */
  rebuffer_ms: number;
  /** 卡顿次数 */
  rebuffer_count: number;
  /** 拖动次数 */
  seek_count: number;
  dropped_frames: number | null;
  total_frames: number | null;
  /** 实际观看时长（毫秒），用于算卡顿率 */
  watched_ms: number;
}

export type QoeEvent =
  | { type: "play-requested"; at: number }
  | { type: "first-frame"; at: number }
  | { type: "waiting"; at: number }
  | { type: "playing"; at: number }
  /**
   * **用户要求跳转**（进度条松手、横滑松手、连按合并落地、系统媒体键）。
   *
   * 「跳了几次」和「这一跳花了多久」都以它为准，**不以 video 的 `seeking`
   * 事件为准**。两者差别在换会话那条路上是决定性的：拖出已转区间要杀掉
   * ffmpeg 换一个新会话，新流的起播点恒在自己时间轴的 0 秒，于是 hls.js
   * 根本不会 seek——`seeking` 一次都不会来。结果是**最贵的那种跳转在质量
   * 数据里完全不存在**：`seek_count` 不记，`lastSeekMs`（诊断面板那行「跳转
   * 耗时」，用户报「拖拽不丝滑」时唯一能量化它的数字）也不更新，而会话拆除
   * + 重开 + ffmpeg 起转这几秒正是用户等的那几秒。
   */
  | { type: "seek-requested"; at: number }
  /**
   * video 元素开始 seek。**只用来开「这段等待不算卡顿」的闸，不计数**。
   *
   * 计数交给 `seek-requested`：元素这个事件会为拖动跟随写的每一次
   * currentTime、以及换会话后新流的起播 seek 都触发一次，拿它计数就是把
   * 「用户跳了几次」算成「写了几次 currentTime」。
   */
  | { type: "seeking"; at: number }
  | { type: "seeked"; at: number }
  | { type: "frames"; dropped: number; total: number }
  | { type: "tick"; at: number; playing: boolean };

interface State {
  requestedAt: number | null;
  firstFrameAt: number | null;
  waitingSince: number | null;
  /** seek 引起的等待不算卡顿；这个标记在 seeked 之后的第一次 playing 才清 */
  inSeek: boolean;
  /** 本次 seek 的起点时刻；恢复播放时结算成 lastSeekMs */
  seekStartedAt: number | null;
  /** 上一次 seek 从发起到画面恢复的耗时（毫秒）。诊断面板的「跳转耗时」——
   * 用户报「快进/拖拽不丝滑」时，这个数字直接量化「不丝滑」有多少毫秒 */
  lastSeekMs: number | null;
  rebufferMs: number;
  rebufferCount: number;
  seekCount: number;
  dropped: number | null;
  total: number | null;
  watchedMs: number;
  lastTickAt: number | null;
}

export function initialQoe(): State {
  return {
    requestedAt: null,
    firstFrameAt: null,
    waitingSince: null,
    inSeek: false,
    seekStartedAt: null,
    lastSeekMs: null,
    rebufferMs: 0,
    rebufferCount: 0,
    seekCount: 0,
    dropped: null,
    total: null,
    watchedMs: 0,
    lastTickAt: null,
  };
}

/** 诊断面板的实时读数（与上报的 QoeSummary 分开：面板要的是「刚才那一下
 * 多久」，上报要的是整场累计）。 */
export interface QoeLiveStats {
  lastSeekMs: number | null;
  rebufferMs: number;
  rebufferCount: number;
}

export function liveStats(state: State): QoeLiveStats {
  return {
    lastSeekMs: state.lastSeekMs,
    rebufferMs: Math.round(state.rebufferMs),
    rebufferCount: state.rebufferCount,
  };
}


/** 纯归约：事件进、状态出。没有副作用，因此可以直接表驱动单测。 */
export function reduceQoe(state: State, event: QoeEvent): State {
  switch (event.type) {
    case "play-requested":
      return { ...state, requestedAt: event.at };
    case "first-frame":
      // 只记第一次——降档重来时的第二次出画不是"首帧"
      return state.firstFrameAt === null ? { ...state, firstFrameAt: event.at } : state;
    case "seek-requested":
      return {
        ...state,
        inSeek: true,
        seekCount: state.seekCount + 1,
        waitingSince: null,
        // 连续拖拽（一跳还没落地又来一跳）以第一次为起点：用户感知的等待
        // 从第一下拖动就开始了
        seekStartedAt: state.seekStartedAt ?? event.at,
      };
    case "seeking":
      // 开闸、必要时起表，但不计数（理由见事件类型上的注释）
      return {
        ...state,
        inSeek: true,
        waitingSince: null,
        seekStartedAt: state.seekStartedAt ?? event.at,
      };
    case "seeked":
      return state;
    case "waiting":
      // seek 期间的等待不是卡顿，是用户自己要求的跳转
      if (state.inSeek || state.waitingSince !== null) return state;
      return { ...state, waitingSince: event.at };
    case "playing": {
      const seekSettled =
        state.inSeek && state.seekStartedAt !== null
          ? { seekStartedAt: null, lastSeekMs: Math.max(0, event.at - state.seekStartedAt) }
          : {};
      if (state.waitingSince === null) return { ...state, inSeek: false, ...seekSettled };
      return {
        ...state,
        inSeek: false,
        waitingSince: null,
        rebufferMs: state.rebufferMs + Math.max(0, event.at - state.waitingSince),
        rebufferCount: state.rebufferCount + 1,
        ...seekSettled,
      };
    }
    case "frames":
      return { ...state, dropped: event.dropped, total: event.total };
    case "tick": {
      const delta =
        state.lastTickAt !== null && event.playing
          ? Math.max(0, event.at - state.lastTickAt)
          : 0;
      return { ...state, watchedMs: state.watchedMs + delta, lastTickAt: event.at };
    }
    default:
      return state;
  }
}

export function summarize(state: State): QoeSummary {
  return {
    ttff_ms:
      state.requestedAt !== null && state.firstFrameAt !== null
        ? Math.max(0, Math.round(state.firstFrameAt - state.requestedAt))
        : null,
    rebuffer_ms: Math.round(state.rebufferMs),
    rebuffer_count: state.rebufferCount,
    seek_count: state.seekCount,
    dropped_frames: state.dropped,
    total_frames: state.total,
    watched_ms: Math.round(state.watchedMs),
  };
}

/** 一次会话是否值得上报：没真的播起来就没有意义，只会污染统计。 */
export function isReportable(summary: QoeSummary): boolean {
  return summary.watched_ms >= 3000 || summary.ttff_ms !== null;
}
