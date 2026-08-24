/**
 * 掉帧 watchdog（docs/design/web-player.md §6.3 / §12.15）。
 *
 * 决策层已经不再预测流畅度（smooth 只作参考，§12.15 Jellyfin 对照），
 * 「这台设备到底放不放得动」由这里的**真实证据**回答：直通期间持续掉帧
 * 超阈值，就把当前档报废（dispatch `failed`），走既有的 failed_tiers
 * 降档回路换转码重来。这是我们比 Jellyfin 强的半边——它只在**硬错误**时
 * 回退转码（playbackmanager.js 的 onPlaybackError），画面卡成幻灯片但
 * 没抛错的话它什么也不做。
 *
 * 输入是 `getVideoPlaybackQuality()` 的**累计**计数，每秒采一次；本模块
 * 维护滑动窗口算增量比率。判定为什么长这样：
 *
 * - **窗口 10 秒**：瞬时掉帧（seek 落点、解码器起步）几秒内就摊平了，
 *   1–2 秒的窗口全是误报；
 * - **窗口内至少 100 帧**：低帧率片（24fps）10 秒约 240 帧，100 帧下限
 *   挡住「刚起播样本不足，3 掉 1 就算 33%」这类除小数陷阱；
 * - **比率 ≥ 10%**：QoE 及格线是 <1%（§8），但降档的代价是整路转码 +
 *   一次约一秒的换流，阈值要留出「难看但可忍」与「不可看」之间的余量。
 *   10% 已是肉眼明显的卡顿。
 *
 * 两个必须由调用方遵守的契约（都会产生假掉帧）：
 * - **页面不可见时不要喂样本**：后台标签页浏览器主动丢帧，那不是设备
 *   放不动；回到前台时先 `reset()`。
 * - **换会话 / 换 src 时 `reset()`**：video 元素的累计计数会归零，直接
 *   续喂会算出负增量（本模块遇到负增量也会自动重置，作为兜底）。
 */

/** 滑动窗口长度（样本数，每秒一个）。 */
export const FRAMEDROP_WINDOW_SAMPLES = 10;
/** 窗口内至少解码这么多帧才有资格判定。 */
export const FRAMEDROP_MIN_FRAMES = 100;
/** 窗口内掉帧率达到这个值即判「放不动」。 */
export const FRAMEDROP_RATIO = 0.1;

export interface FrameSample {
  /** getVideoPlaybackQuality().droppedVideoFrames（累计） */
  dropped: number;
  /** getVideoPlaybackQuality().totalVideoFrames（累计） */
  total: number;
}

export interface FrameDropVerdict {
  /** true = 掉帧率超阈值，当前档应报废降档 */
  degrade: boolean;
  /** 窗口内掉帧率（0–1），样本不足时为 null。诊断面板可直接显示 */
  ratio: number | null;
}

export interface FrameDropTracker {
  sample(sample: FrameSample): FrameDropVerdict;
  reset(): void;
}

export function createFrameDropTracker(): FrameDropTracker {
  let history: FrameSample[] = [];

  return {
    sample(sample: FrameSample): FrameDropVerdict {
      const last = history[history.length - 1];
      // 累计计数变小 = video 元素换了 src，旧窗口作废（调用方漏 reset 的兜底）
      if (last && (sample.total < last.total || sample.dropped < last.dropped)) {
        history = [];
      }
      history.push(sample);
      if (history.length > FRAMEDROP_WINDOW_SAMPLES + 1) history.shift();

      // 窗口增量要用「首尾差」：首样本是窗口起点的基线，所以要 N+1 个样本
      // 才构成 N 秒的窗口
      if (history.length < FRAMEDROP_WINDOW_SAMPLES + 1) {
        return { degrade: false, ratio: null };
      }
      const first = history[0];
      const totalDelta = sample.total - first.total;
      const droppedDelta = sample.dropped - first.dropped;
      if (totalDelta < FRAMEDROP_MIN_FRAMES) {
        return { degrade: false, ratio: null };
      }
      const ratio = droppedDelta / totalDelta;
      return { degrade: ratio >= FRAMEDROP_RATIO, ratio };
    },
    reset() {
      history = [];
    },
  };
}
