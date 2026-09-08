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
 * 把速度压到十分之一，然后用户以为自己宽带坏了。调用方取 hls.js 的
 * `stats.loading.first`（首字节到达时刻）而不是 `start` 作为传输起点，这条
 * 语义由本模块的文档钉住。
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
