/**
 * 解码类致命错误的就地自救阶梯（docs/design/player-feel.md §2.D1）。
 *
 * 直接判失败的代价是走降档回路：画质掉一级 + 几秒黑屏重开会话。而这类错误
 * （buffer append error、解码器抽风）hls.js 自己往往一秒内就能救回来——
 * 最贵的手段不该是第一反应。阶梯照 jellyfin-web 的 `handleHlsJsMediaError`：
 *
 * 1. `recover`：重建 SourceBuffer，不重新拉流；
 * 2. `swap`：换音频编解码再重建（mp4a 的 AAC profile 猜错是常见成因）；
 * 3. `give-up`：交给降档。
 *
 * 判定是纯函数，接线在 engine.ts。
 */

/**
 * 两级自救之间的冷却（毫秒）。
 *
 * 语义是「上一次自救有没有把播放救活」：隔了这么久还没再犯，就当那次成功、
 * 这次是全新的一起事故，从第一级重来；冷却内又炸说明上一级没救回来，升级。
 * 3 秒是 jellyfin-web 的取值，沿用不自创。
 */
export const MEDIA_RECOVER_COOLDOWN_MS = 3000;

export interface MediaRecoverState {
  /** 上一次「重建解码管线」的时刻；null = 还没自救过 */
  lastRecoverAt: number | null;
  /** 上一次「换音频编解码再重建」的时刻 */
  lastSwapAt: number | null;
}

export type MediaRecoverStep = "recover" | "swap" | "give-up";

/** 这次解码错误该走哪一级。`nowMs` 传 `performance.now()`。 */
export function nextMediaRecovery(state: MediaRecoverState, nowMs: number): MediaRecoverStep {
  const { lastRecoverAt, lastSwapAt } = state;
  if (lastRecoverAt === null || nowMs - lastRecoverAt > MEDIA_RECOVER_COOLDOWN_MS) return "recover";
  if (lastSwapAt === null || nowMs - lastSwapAt > MEDIA_RECOVER_COOLDOWN_MS) return "swap";
  return "give-up";
}
