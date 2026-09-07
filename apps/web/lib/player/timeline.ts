/**
 * 会话时间轴 ↔ 文件时间轴的换算（docs/design/web-player.md §4.3）。
 *
 * **转码会话的时间轴恒从 0 起**：ffmpeg 用 `-ss` 从文件中间起转，产出的
 * HLS 里第一帧就是 0 秒。所以播放器里到处都要做一次
 * `文件时间 = start_ms + currentTime`——**全前端只有本模块做这个换算**，
 * 散到各处必然有一处忘了加偏移，表现是进度条跳、续播点越存越靠前。
 *
 * 档 0（原文件直出）没有会话，`startMs` 恒为 0，同一套函数直接通用。
 */

/** 播放器的 currentTime（秒）→ 文件里的绝对位置（毫秒）。 */
export function toFileMs(currentTimeSeconds: number, startMs: number): number {
  return Math.max(0, Math.round(startMs + currentTimeSeconds * 1000));
}

/**
 * 文件里的绝对位置（毫秒）→ 当前会话里该 seek 到的 currentTime（秒）。
 *
 * **结果可能为负**，且这里刻意不夹到 0：负数正是「这个位置不在本次会话里」
 * 的信号，`planSeek` 靠它判断要不要换会话。夹成 0 会让往回拖变成静默地跳回
 * 会话开头——用户拖到 20 分钟却回到 10 分钟，且没有任何提示。
 */
export function toSessionSeconds(fileMs: number, startMs: number): number {
  return (fileMs - startMs) / 1000;
}

/**
 * 跳转落点离片尾至少留出的余量（毫秒）。
 *
 * 一秒足够：留着的这一秒会照常播完并发 `ended`，与「跳到结尾」在观感上没有
 * 区别；留得更多反而是把片尾生生截掉一段。
 */
export const SEEK_TAIL_GUARD_MS = 1000;

/**
 * 把跳转目标夹进「真的落得下去」的区间。
 *
 * 片尾连按快进键、或者把进度条一路拖到最右端，都会给出一个正好落在文件末尾
 * （甚至之外）的位置。`native` 那条浏览器自己会收住，**换会话那条不会**：它
 * 会拿这个位置去开一个 `-ss` 落在末尾的会话，ffmpeg 一帧都转不出来，用户对着
 * 转圈一直等到分片超时。片长未知（服务端算不出、也没有元数据）时不夹——那时
 * 进度条本来就是禁用的，只剩快捷键，越界交给浏览器收住。
 */
export function clampSeekTarget(targetFileMs: number, durationMs: number | null): number {
  const floored = Math.max(0, targetFileMs);
  if (!durationMs || durationMs <= 0) return floored;
  return Math.min(floored, Math.max(0, durationMs - SEEK_TAIL_GUARD_MS));
}

export type SeekPlan =
  | { kind: "native"; seconds: number }
  | { kind: "restart"; startMs: number };

/**
 * 一次 seek 该怎么走。
 *
 * 转码会话是「从 start_ms 起、边转边给」的单向流：往回拖、或往前拖到已转
 * 区间之内，浏览器自己就能跳（分片已经在手）；拖出已转区间则必须换一个新
 * 会话——干等 ffmpeg 追上来，用户看到的是永远转不完的圈。
 *
 * 服务端在开新会话时会先杀掉同文件的旧会话（§4.4），所以这里不用担心
 * 连拖五下留下五个 ffmpeg。
 *
 * `seekableEndSeconds` 传 `video.seekable.end(...)`：档 0 是整个文件（随便
 * 拖），转码会话是当前 playlist 已经产出的时长。
 */
export function planSeek(
  targetFileMs: number,
  options: { startMs: number; seekableEndSeconds: number; hasSession: boolean },
): SeekPlan {
  const { startMs, seekableEndSeconds, hasSession } = options;
  const seconds = toSessionSeconds(targetFileMs, startMs);
  if (!hasSession) return { kind: "native", seconds };
  // 落在 [0, 已转时长] 之内就地跳；越界（含往回拖到本次会话起点之前）换会话
  if (seconds >= 0 && seconds <= seekableEndSeconds) return { kind: "native", seconds };
  return { kind: "restart", startMs: Math.max(0, targetFileMs) };
}

/**
 * 毫秒 → 钟表格式（H:MM:SS / M:SS）。
 *
 * 播放器里的时间必须是钟表格式，不能复用站内那套「1.2 小时」——进度条旁边
 * 写「0.4 小时」没人读得出自己看到哪了。不足一小时不补出 `0:`，与所有主流
 * 播放器一致。
 */
export function formatClock(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "--:--";
  const total = Math.floor(ms / 1000);
  const seconds = total % 60;
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  const mm = String(minutes).padStart(hours > 0 ? 2 : 1, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

/**
 * 片尾倒计时该不该出现：距结尾不足 `windowSeconds` 且片长已知。
 *
 * 门槛用「剩余秒数」而不是百分比：45 分钟的剧集和 3 小时的电影，片尾长度
 * 是同一个量级，按百分比算会让长片的倒计时早出现十几分钟。
 */
export function isInEndCredits(
  positionMs: number,
  durationMs: number | null,
  windowSeconds = 40,
): boolean {
  if (!durationMs || durationMs <= 0) return false;
  const remaining = (durationMs - positionMs) / 1000;
  return remaining > 0 && remaining <= windowSeconds;
}
