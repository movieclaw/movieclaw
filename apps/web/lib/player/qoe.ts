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
 *   记成一次卡顿，数据全废。
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
    case "seeking":
      return {
        ...state,
        inSeek: true,
        seekCount: state.seekCount + 1,
        waitingSince: null,
        // 连续拖拽（seeking 里再 seeking）以第一次为起点：用户感知的等待
        // 从第一下拖动就开始了
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
