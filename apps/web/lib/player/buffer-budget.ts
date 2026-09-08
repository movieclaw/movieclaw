/**
 * 已播缓冲保留多久（hls.js `backBufferLength`）——**按码率算，不是一个常数**。
 *
 * 不回收是不行的：一部三小时的片子会把已播分片一路堆在 SourceBuffer 里，吃掉
 * 几个 G 内存然后整个标签页崩掉——长片播放最典型的一种「放到一半就没了」。
 * jellyfin-web 设 Infinity（配合服务端默认不删分片），我们不跟。
 *
 * 但只留 30 秒又太紧：**回拖是最常见的操作**（没听清、走神），回拖 31 秒就要
 * 重新请求分片，转码会话下还可能把服务端的 ffmpeg 拽回去重启，代价是几秒黑屏。
 *
 * 「秒」这个单位在这里有个陷阱：同样 180 秒，4Mbps 的片子占 90MB，而档 1
 * 原样封装的 4K 蓝光原盘是 80Mbps——那就是 1.8GB，标签页必崩。所以按**字节
 * 预算**反推秒数：预算之内尽量多留，高码率片子自动收回下限
 * （docs/design/player-feel.md §2.C3）。
 */

/** 已播缓冲的字节预算。96MB 与 hls.js 前向缓冲的默认字节上限（60MB）同量级，
 * 两头加起来仍在移动端标签页扛得住的范围。 */
const BACK_BUFFER_BUDGET_BYTES = 96 * 1024 * 1024;
/** 再高的码率也至少留这么久：低于这个数，回拖就没有一次是免费的。 */
const BACK_BUFFER_MIN_S = 30;
/** 再低的码率也不多留：留三分钟已经覆盖「没听清往回拖」的全部场景。 */
const BACK_BUFFER_MAX_S = 180;

/**
 * 码率（bps）→ 该保留多少秒已播缓冲。
 *
 * 码率未知时按上限给：随后 `LEVEL_LOADED` 会带着真实码率把它收紧，而那时
 * 连 30 秒都还没播到，收得及。
 */
export function backBufferSeconds(bitrateBps: number | null | undefined): number {
  if (!bitrateBps || bitrateBps <= 0) return BACK_BUFFER_MAX_S;
  const seconds = (BACK_BUFFER_BUDGET_BYTES * 8) / bitrateBps;
  return Math.min(BACK_BUFFER_MAX_S, Math.max(BACK_BUFFER_MIN_S, Math.round(seconds)));
}
