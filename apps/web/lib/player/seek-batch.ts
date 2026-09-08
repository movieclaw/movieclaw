/**
 * 连按快进/快退的合并（docs/design/player-feel.md §2.B4）。
 *
 * **一次 seek 在转码会话下不是免费的**：落点越出已转区间就要杀掉 ffmpeg
 * 换会话直奔目标。连按三次「快进 10 秒」如果发三次 seek，用户看到的是黑三下
 * 才走到 +30 秒——这是「不丝滑」里最难受的一种。
 *
 * 做法照 jellyfin-web 的 emby-slider（`KeyboardDraggingTimeout`）：按键只
 * 累积落点、立刻更新读数，**静默一小段时间后才真正提交一次** seek。
 *
 * 纯函数在这里，计时器的接线在 video-player 组件里。
 */

import { clampSeekTarget } from "./timeline.ts";

/**
 * 合并窗口（毫秒）。
 *
 * jellyfin-web 用 1000ms——那是给电视遥控器的节奏，鼠标/键盘用户按一下要等
 * 一秒才动，钝得像卡了。400ms 足够接住连按（人连按的间隔通常 150~300ms），
 * 单次按键的延迟又还在「按了就动」的感知范围内。
 */
export const SEEK_BATCH_WINDOW_MS = 400;

/**
 * 这次跳转要不要合并。
 *
 * 判据是**这一跳贵不贵**，而不是「会不会换会话」：
 *
 * - 落点已经在缓冲里（或档 0 直出，整个文件随便跳）→ 零成本，立刻执行。
 *   那里合并只是凭空加 400ms 延迟。
 * - 落点在缓冲之外、且这是一路转码会话 → 服务端要按分片请求把 ffmpeg 杀掉
 *   重启直奔目标（VOD 预生成列表同样如此，它只是把这件事从换会话变成了
 *   换分片）。连按三次就是连着捅三刀，值得等 400 毫秒并成一次。
 */
export function seekBatchWindowMs(input: {
  /** 有没有一路转码会话（档 0 直出没有） */
  hasSession: boolean;
  /** 落点是否落在已缓冲区间内 */
  buffered: boolean;
}): number {
  return input.hasSession && !input.buffered ? SEEK_BATCH_WINDOW_MS : 0;
}

/**
 * 累积落点：有在途的累积就在它上面加，否则从当前播放位置加。
 *
 * 「在它上面加」是连按累积的全部要义——每次都从 positionMs 起算的话，画面
 * 还没跳走，连按三次的结果仍然只有 +10 秒。
 */
export function nextSeekTarget(input: {
  /** 在途的累积落点；null = 这是本轮第一次按 */
  pendingMs: number | null;
  /** 当前真实播放位置（文件毫秒） */
  positionMs: number;
  /** 本次按键的增量（毫秒，可负） */
  deltaMs: number;
  durationMs: number | null;
}): number {
  const base = input.pendingMs ?? input.positionMs;
  return clampSeekTarget(base + input.deltaMs, input.durationMs);
}
