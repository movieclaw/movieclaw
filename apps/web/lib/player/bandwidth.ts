/**
 * 取流速度计：播放器上那行「↓ 3.2 MB/s」的算法（docs/design/player-feel.md §2.G3）。
 *
 * 外网播放最难回答的一个问题是「现在到底卡在谁身上」——是我这条线拉不动，
 * 还是服务端 ffmpeg 转不过来？没有读数时用户只能看着转圈猜，我们也只能猜。
 * 一行实测速度就能把这件事一刀切开：速度贴着码率跑说明线路吃得下，卡的是
 * 服务端；速度远低于码率说明就是带宽不够，降一档画质才是解法。
 *
 * ## 口径：**传输期速率**，不是墙钟平均速率
 *
 * 两种算法差别很大，选错的话读数会骗人：
 *
 * - 墙钟平均（窗口内字节 ÷ 窗口时长）：缓冲喂饱之后 hls.js 会停下来不取，
 *   读数掉到接近 0——线路明明很好，用户看到的却是「0 KB/s」。
 * - 传输期速率（字节 ÷ **真正花在传输上的时间**）：只统计请求在途的那段，
 *   缓冲满了照样保持上一次实测值。这才是「带宽」这个词的意思，也是 hls.js
 *   自己的 ABR 用的口径。
 *
 * 所以本模块要的样本是 `{ 字节, 传输毫秒 }` 两件一起给，缺一不可。
 *
 * ## 服务端等待不算进传输时间
 *
 * 转码会话是边转边给的，一个分片请求可能先在服务端挂几秒等 ffmpeg 追上来
 * （`ensure_segment` 最长挂 30 秒）。那几秒里一个字节都没在传，算进分母会
 * 把速度压到十分之一，然后用户以为自己宽带坏了。所以传输起点取**首字节
 * 到达**而不是请求发出。
 *
 * ## 时刻只认网络栈，不认 JS 回调
 *
 * 这两个时刻必须由浏览器的 Resource Timing 提供（见 `sampleFromResourceTiming`），
 * **不能用 hls.js 的 `stats.loading`**——那是 XHR 事件在主线程上被处理到的时刻，
 * 主线程一卡两个回调就挤在一起，几 MB 的分片会被算成传了十几毫秒。
 */

/** 统计窗口。短了随分片到货剧烈跳动，长了跟不上外网的抖动。 */
export const BANDWIDTH_WINDOW_MS = 12_000;

/** 攒够这么多字节才给读数：init 分片才几 KB，单靠它算出来的速度是噪声。 */
const MIN_SAMPLED_BYTES = 64 * 1024;

/** 传输时间不足这么久也不给：几十毫秒的分母会把误差放大成好几倍的虚高。 */
const MIN_TRANSFER_MS = 120;

export interface BandwidthSample {
  /** 样本落地时刻（`performance.now()` 口径），只用于按窗口淘汰 */
  at: number;
  /** 这次实际收到的字节数 */
  bytes: number;
  /** 这些字节**在路上**花的毫秒（不含服务端等待，见模块文档） */
  transferMs: number;
}

export interface BandwidthWindow {
  samples: BandwidthSample[];
}

export function createBandwidthWindow(): BandwidthWindow {
  return { samples: [] };
}

/**
 * 记一次到货，并淘汰掉窗口外的旧样本。
 *
 * 返回新对象而不是原地改：这层是纯函数，调用方（引擎）自己持有当前窗口，
 * 单测里可以一路串起来对着算。
 */
export function pushBandwidthSample(
  window: BandwidthWindow,
  sample: BandwidthSample,
): BandwidthWindow {
  // 传输时间为 0 或负（缓存命中、时钟回拨）的样本直接丢：它会让速度变成无穷大
  if (!(sample.bytes > 0) || !(sample.transferMs > 0)) return window;
  const cutoff = sample.at - BANDWIDTH_WINDOW_MS;
  const samples = window.samples.filter((item) => item.at > cutoff);
  samples.push(sample);
  return { samples };
}

/**
 * 窗口内的实测速率（bps）；样本不够时返回 null——**宁可不显示也不显示错的**。
 *
 * 起播头一两秒、或者一部片全程只有 init 分片到货时都会落到 null，界面上那
 * 一格就先空着，等有真数据了再出现。
 */
export function bandwidthBps(window: BandwidthWindow): number | null {
  let bytes = 0;
  let transferMs = 0;
  for (const sample of window.samples) {
    bytes += sample.bytes;
    transferMs += sample.transferMs;
  }
  if (bytes < MIN_SAMPLED_BYTES || transferMs < MIN_TRANSFER_MS) return null;
  return (bytes * 8 * 1000) / transferMs;
}

/**
 * bps → 「3.2 MB/s」这样的人话。
 *
 * 用 MB/s 而不是 Mbps：用户对下载速度的直觉全部来自下载器和浏览器的下载
 * 条，那里一律是 MB/s，换成 Mbps 会让人以为快了八倍。进位按 1024，同上。
 */
export function formatBandwidth(bps: number | null): string | null {
  if (bps === null || !Number.isFinite(bps) || bps <= 0) return null;
  const bytesPerSecond = bps / 8;
  const mbPerSecond = bytesPerSecond / (1024 * 1024);
  if (mbPerSecond >= 1) return `${mbPerSecond.toFixed(1)} MB/s`;
  const kbPerSecond = bytesPerSecond / 1024;
  // 不足 1 KB/s 就别装精确了，那已经是「基本没在动」
  if (kbPerSecond < 1) return "0 KB/s";
  return `${Math.round(kbPerSecond)} KB/s`;
}

/**
 * 一条 Resource Timing 条目里我们要的四个字段（便于单测构造假条目）。
 */
export interface ResourceTimingLike {
  /** 首字节到达时刻；跨源且没有 `Timing-Allow-Origin` 时为 0 */
  responseStart: number;
  /** 末字节到达时刻 */
  responseEnd: number;
  /** 含响应头的**上网**字节数；命中浏览器缓存时为 0 */
  transferSize: number;
  /** 响应体压缩后的字节数（缓存命中时照样是完整大小） */
  encodedBodySize: number;
}

/**
 * 把一条 Resource Timing 折成取流样本；读不出可信数据时返回 null。
 *
 * ## 为什么不用 hls.js 的 `stats.loading`
 *
 * `stats.loading.first/end` 是 **XHR 事件在主线程上被处理到的时刻**，不是字节
 * 真正到达的时刻。主线程一忙（解码、`appendBuffer`、界面重绘），`headers` 与
 * `done` 两个回调就会挤在同一帧里连着跑——几 MB 的分片被算成传了十几毫秒，
 * 读数飙到几百 MB/s，比用户的实际带宽高两个数量级。这不是偶发抖动：越是卡顿
 * 的时候主线程越忙，读数反而越离谱，正好把这行读数该回答的问题答反。
 *
 * Resource Timing 的 `responseStart`/`responseEnd` 由浏览器网络栈记录，与主线程
 * 忙闲无关，且语义与我们要的口径完全一致：`responseStart` 就是首字节到达
 * （服务端等 ffmpeg 的那几秒落在它之前，天然不算进分母）。
 *
 * ## 两种要丢掉的条目
 *
 * - `transferSize === 0`：整份响应来自浏览器缓存，一个字节都没走网络。回跳到
 *   已下过的分片时就是这样，算进去等于拿内存速度冒充带宽。
 * - `responseStart === 0`：跨源且服务端没给 `Timing-Allow-Origin`，浏览器把
 *   这些字段一律抹成 0，什么都算不出来——宁可不显示也不显示错的。
 */
export function sampleFromResourceTiming(
  entry: ResourceTimingLike | null | undefined,
  at: number,
): BandwidthSample | null {
  if (!entry) return null;
  if (!(entry.responseStart > 0) || !(entry.transferSize > 0)) return null;
  const bytes = entry.encodedBodySize;
  const transferMs = entry.responseEnd - entry.responseStart;
  if (!(bytes > 0) || !(transferMs > 0)) return null;
  return { at, bytes, transferMs };
}
