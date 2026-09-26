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

/**
 * 窗口外的样本也至少留这么多个（HLS 取最快一片时用，见 `peakBandwidthBps`）：转码会话要等 ffmpeg 追上来，
 * 分片隔十几秒才到一片，12 秒窗口里常常只剩最新那一片，偏偏它又可能被读慢了。
 */
export const BANDWIDTH_MIN_SAMPLES = 3;

/** 攒够这么多字节才给读数：init 分片才几 KB，单靠它算出来的速度是噪声。 */
const MIN_SAMPLED_BYTES = 64 * 1024;

/**
 * 窗口内传输时间的合计不足这么久就不给读数。
 *
 * 这个下限是给**时钟精度**留的余量，不是给「样本够不够有代表性」留的。原先
 * 是 120ms——那是 `stats.loading` 时代的数，主线程时间戳的抖动本来就有几十
 * 毫秒。换成 Resource Timing 之后时刻由网络栈记录，同源下分辨率到微秒级，
 * 20ms 的分母相对误差已经在 1% 以内。
 *
 * 留着 120ms 的代价是**快线路上这一格永远空着**：千兆局域网上 4Mbps 的源、
 * 6 秒一片，两片合起来才传了 48 毫秒——而自建媒体库在局域网里放片恰恰是最
 * 常见的场景，那一格却偏偏在这时候消失。
 */
const MIN_TRANSFER_MS = 20;

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
  options: { minSamples?: number } = {},
): BandwidthWindow {
  // 传输时间为 0 或负（缓存命中、时钟回拨）的样本直接丢：它会让速度变成无穷大
  if (!(sample.bytes > 0) || !(sample.transferMs > 0)) return window;
  const cutoff = sample.at - BANDWIDTH_WINDOW_MS;
  const samples = [...window.samples, sample];
  const minSamples = options.minSamples ?? 0;
  while (samples.length > minSamples && samples[0].at <= cutoff) samples.shift();
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
 * 窗口内**最快一片**的传输速率（bps）；没有像样的样本时为 null。HLS 的带宽用它（2026-09-27 起）。
 *
 * 为什么不取平均：接收端忙的时候会读慢——浏览器主线程一忙，网络层就把整条连接压慢，个别分片的
 * 「首字节 → 末字节」被拉长。本机限速 1MB/s 实测，同一次播放里按平均算掉到过 0.53MB/s，线路一点没变。
 * 那几片慢在接收端、不在线路；而每一片的时刻都取自网络栈，快的那几片不会超过真实线路。
 * iOS 同理且更明显（AVPlayer 会自己放慢读取），见 apps/apple 的 PlayerEngine.swift `BandwidthMeter`。
 * 直出档仍取平均（`bandwidthBps`）：它的每个样本是「缓冲涨了几秒 × 全片平均码率」的估算，单个样本
 * 本身就带着码率起伏的误差，取最快只会把误差挑出来。
 */
export function peakBandwidthBps(window: BandwidthWindow): number | null {
  let peak = 0;
  for (const sample of window.samples) {
    // 太小太短的一片（init 分片几 KB）算出来的速度是噪声
    if (sample.bytes < MIN_SAMPLED_BYTES || sample.transferMs < MIN_TRANSFER_MS) continue;
    peak = Math.max(peak, (sample.bytes * 8 * 1000) / sample.transferMs);
  }
  return peak > 0 ? peak : null;
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

// ---------------------------------------------------------------------------
// 实时加载速度：顶栏那行「↓」（2026-09-27 改口径）
// ---------------------------------------------------------------------------

/**
 * 顶栏与起播/缓冲转圈下方那行「↓ 3.2 MB/s」报的是**此刻的加载速度**：最近 2 秒实际收到的字节 ÷ 时长。
 * 在下载就是实际下载速度，没在下载（缓冲喂饱、暂停后缓冲够了）就是「0 KB/s」。这是下载器与常见视频
 * App「网速」的口径（2026-09-27 用户定；iOS 同口径，见 apps/apple 的 PlayerEngine.swift `LoadingSpeedMeter`）。
 *
 * 它和上面的取流速度（`bandwidthBps`，诊断面板里叫「带宽」）是两个量：带宽回答「线路能跑多快」，缓冲满了
 * 也保持实测值，还喂着按带宽重开 / 降档 / 直通提示三条决策，口径一点没动；加载速度回答「此刻在下多快」。
 * 原先顶栏显示的是带宽，缓冲满了读数照旧，用户看不出现在到底在不在下。
 *
 * 字节来源：hls.js 在途分片的 `stats.loaded`（XHR 下载途中持续更新）加已下完分片的字节；直出档量不到字节，
 * 沿用「缓冲涨了几秒 × 源码率」。窗口 2 秒：进度回调在主线程上跑，忙的时候会攒一会儿再一起到，1 秒窗口
 * 会把这一坨算进同一秒，读数忽高忽低（iOS 实测同理：1 秒窗口误差是 2 秒窗口的两倍）。
 */
export const LOADING_WINDOW_MS = 2_000;

export interface LoadingMeter {
  /** 最近的采样点：累计收到的字节与时刻（`performance.now()` 口径） */
  points: { at: number; bytes: number }[];
  /** 最新读数（bps）；还没攒够一个窗口时为 null */
  bps: number | null;
}

export function createLoadingMeter(): LoadingMeter {
  return { points: [], bps: null };
}

/**
 * 记一个采样点，返回新的计量状态（纯函数，单测可以一路串着算）。
 *
 * 调用方每秒至少一次（诊断面板开着时会更密）：窗口按时间算，不按调用次数。
 * `bytes` 为 null = 这条路量不出字节（直出且不知道源码率）：没有读数，那一格不显示。
 */
export function sampleLoadingMeter(
  meter: LoadingMeter,
  input: { bytes: number | null; at: number },
): LoadingMeter {
  const { bytes, at } = input;
  if (bytes === null || !Number.isFinite(bytes)) return createLoadingMeter();
  const last = meter.points[meter.points.length - 1];
  // 计数倒退只会是换了取流对象（走缓存的分片被扣回去）：从头量，不许出现负速度
  const points = last && bytes < last.bytes ? [] : meter.points.slice();
  if (points.length === 0) return { points: [{ at, bytes }], bps: null };
  points.push({ at, bytes });
  // 窗口起点：至少早 2 秒的最后一个点（留 250ms 给计时器抖动）；起播不满 2 秒时用最早的点，但至少隔 900ms
  const cutoff = at - LOADING_WINDOW_MS + 250;
  let ref = -1;
  for (let i = points.length - 1; i >= 0; i -= 1) {
    if (points[i].at <= cutoff) {
      ref = i;
      break;
    }
  }
  if (ref < 0 && at - points[0].at >= 900) ref = 0;
  if (ref < 0) return { points, bps: meter.bps };
  const base = points[ref];
  return { points: points.slice(ref), bps: ((bytes - base.bytes) * 8 * 1000) / (at - base.at) };
}

/** 加载速度的文案：没在加载时明确写「0 KB/s」（要让人一眼看出现在没在下），没有读数时不显示 */
export function formatLoadingSpeed(bps: number | null): string | null {
  if (bps === null || !Number.isFinite(bps) || bps < 0) return null;
  return formatBandwidth(bps) ?? "0 KB/s";
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
 * - `encodedBodySize > transferSize`：**响应体比上网字节还大**，说明体是从缓存
 *   取的、只有响应头走了网络——304 重校验就长这样。分片带的是
 *   `max-age=3600, immutable`，正常不会重校验，但用户刷新一次（浏览器对导航
 *   请求发 `max-age=0`）就会。拿完整体积去除一个 RTT，读数当场飙到几百 MB/s
 *   ——正是改用 Resource Timing 要修掉的那种虚高，从另一个口子又漏回来。
 * - `responseStart === 0`：跨源且服务端没给 `Timing-Allow-Origin`，浏览器把
 *   这些字段一律抹成 0，什么都算不出来——宁可不显示也不显示错的。
 *
 * 字节数取 `transferSize` 而不是 `encodedBodySize`：前者是**真正压在链路上**
 * 的八位组（含响应头），那才是「带宽」这个词的意思。两者在正常响应上只差几
 * 百字节的响应头，但只有前者能和上面那条守卫自洽。
 */
export function sampleFromResourceTiming(
  entry: ResourceTimingLike | null | undefined,
  at: number,
): BandwidthSample | null {
  if (!entry) return null;
  if (!(entry.responseStart > 0) || !(entry.transferSize > 0)) return null;
  if (entry.encodedBodySize > entry.transferSize) return null;
  // 只有响应头、没有响应体（重定向、空响应、出错页）：不是一次分片到货
  if (!(entry.encodedBodySize > 0)) return null;
  const bytes = entry.transferSize;
  const transferMs = entry.responseEnd - entry.responseStart;
  if (!(bytes > 0) || !(transferMs > 0)) return null;
  return { at, bytes, transferMs };
}

// ---------------------------------------------------------------------------
// 档 0 直出：从 `progress` 反推取流速度
// ---------------------------------------------------------------------------

/**
 * `<video src>` 这条路一个字节计数都拿不到（`webkitVideoDecodedByteCount` 是
 * 解码字节，不是网络字节），Resource Timing 也没有——媒体元素的 range 请求
 * 不进 resource 条目。只能反推：**两次 `progress` 之间缓冲涨了多少秒 × 源码率**。
 *
 * 误差来自「码率是全片均值而这一段可能是动作戏」，但用途是「够不够」这个量级
 * 的判断，够用。真正会把读数毁掉的是下面三件事，每一件都要显式挡掉：
 *
 * 1. **量错了缓冲段**。`buffered.end(length - 1)` 取的是**最后**一段，不是播放头
 *    所在那段。往前跳一次就会新开一段落在远处，两次相减得到的是「两段之间隔了
 *    多远」——半小时的跳转 × 8Mbps 就是 3.5GB「在 350 毫秒内下完」，读数当场
 *    上百 MB/s。这条教训 video-player 的缓冲条早就吃过一次（「只认播放头所在
 *    那段连续缓冲」），这里漏了。
 * 2. **段被合并**。往回跳之后一路播过去，播放头这段会把后面那段吃掉，末端一下
 *    子跳到被吃掉那段的末端——而那些内容是**之前**下好的。
 * 3. **浏览器停手了**。缓冲喂饱后 `<video>` 会 suspend，那段静默不是传输时间，
 *    算进分母就把速度压成十分之一。
 */
export interface BufferedProbe {
  /** 采样时刻（`performance.now()` 口径） */
  at: number;
  /** 浏览器此刻在不在取数据（`networkState === NETWORK_LOADING`） */
  loading: boolean;
  /** 播放头所在那段连续缓冲；播放头不在任何区间里时为 null */
  active: { start: number; end: number } | null;
  /** 播放头这一段**之后**那段缓冲的起点（秒）；没有下一段时为 null */
  nextStart: number | null;
}

/** `HTMLMediaElement.NETWORK_LOADING` */
const NETWORK_LOADING = 2;

/**
 * 两次采样间隔超过它就作废。
 *
 * suspend 会由 `suspend` 事件显式清掉上一次采样，这条是兜底：响应挂住、进程被
 * 挂起、标签页切到后台回来，都会留下一个很长的间隔，而那段里并没有在传输。
 */
export const PROGRESS_MAX_GAP_MS = 2_000;

/** 从 `<video>` 上量一次缓冲现场。结构类型便于单测直接构造。 */
export function readBufferedProbe(
  video: {
    currentTime: number;
    networkState: number;
    buffered: { length: number; start(index: number): number; end(index: number): number };
  },
  at: number,
): BufferedProbe {
  const ranges = video.buffered;
  let active: { start: number; end: number } | null = null;
  let nextStart: number | null = null;
  // buffered 按起点升序，所以找到播放头那段之后，下一段就是紧邻的那一段
  for (let i = 0; i < ranges.length; i += 1) {
    const start = ranges.start(i);
    const end = ranges.end(i);
    if (active === null) {
      if (start <= video.currentTime && end >= video.currentTime) active = { start, end };
      continue;
    }
    nextStart = start;
    break;
  }
  return { at, loading: video.networkState === NETWORK_LOADING, active, nextStart };
}

/**
 * 两次缓冲现场 → 一条取流样本；这一段差值不可信时返回 null。
 *
 * 判据全在这里，engine 那边只负责把 `progress` 接过来并在 `suspend` 时把上一次
 * 采样清掉。三条守卫对应上面 `BufferedProbe` 文档里的三件事。
 */
export function sampleFromProgress(input: {
  /** 上一次采样；null = 这是本轮第一次（或刚被 suspend 清过） */
  previous: BufferedProbe | null;
  current: BufferedProbe;
  /** 源文件总码率（bps）。算不出时没有这条路可走 */
  sourceBitrateBps: number | null | undefined;
}): BandwidthSample | null {
  const { previous, current, sourceBitrateBps } = input;
  if (!sourceBitrateBps || sourceBitrateBps <= 0) return null;
  if (!previous) return null;
  // 两头都得在取数据
  if (!previous.loading || !current.loading) return null;
  const transferMs = current.at - previous.at;
  if (transferMs <= 0 || transferMs > PROGRESS_MAX_GAP_MS) return null;
  const grownSeconds = continuedGrowthSeconds(previous, current);
  if (grownSeconds === null || grownSeconds <= 0) return null;
  return { at: current.at, bytes: (grownSeconds * sourceBitrateBps) / 8, transferMs };
}

/**
 * 两次缓冲现场之间，播放头所在那段连续缓冲**又长了几秒**；两次量的不是同一段（跳转、段被合并）时返回 null。
 *
 * 带宽（上面的 `sampleFromProgress`）与实时加载速度都靠它：带宽另外要求两头都在取数据、间隔不超过 2 秒，
 * 加载速度不看 networkState——Chrome 直出时 `networkState` 早早就报空闲，之后照样一阵一阵地取数据
 * （实测报空闲期间缓冲从 20.7 秒涨到 28.6 秒），缓冲涨了就是有数据到了。
 */
export function continuedGrowthSeconds(previous: BufferedProbe, current: BufferedProbe): number | null {
  const before = previous.active;
  const after = current.active;
  if (!before || !after) return null;
  // **上一次量到的末端必须仍落在这一段连续缓冲之内**，才谈得上「这一段又长了
  // 多少」。往前跳会新开一段落在远处（起点已经越过上次的末端），往回跳会落回
  // 更靠前的一段（末端够不到上次的末端），两种都在这里被挡下。头部被回收让
  // 起点前移不受影响——那仍是同一段连续缓冲。
  if (!(after.start <= before.end && after.end >= before.end)) return null;
  // 这一段把后面那段吃掉了：末端跳到被吃掉那段的末端，而那些内容是之前下好的
  if (previous.nextStart !== null && after.end >= previous.nextStart) return null;
  return after.end - before.end;
}

// ---------------------------------------------------------------------------
// 实时码率：取流速度的另一半
// ---------------------------------------------------------------------------

/**
 * 实时码率取最近几个分片的「字节 ÷ 时长」。
 *
 * 它和取流速度是**一对**才有意义（见 `EngineStats.downlinkBps`），所以口径也
 * 得对得上：速度是最近 12 秒的实测值，码率就不能是「开播以来见过的最大值」。
 * 原先用 `Math.max` 累计，一个动作戏分片就能把读数钉在峰值上再也下不来——
 * 实测一部均值 4Mbps 的片子，中间一片冲到 12.1Mbps，之后即便降档到 2Mbps，
 * 面板仍然显示 12.1Mbps（6 倍虚高），而这行读数恰恰是用来判断「降档有没有
 * 用」的。它还喂着 `backBufferSeconds()`，虚高会让回看缓冲按峰值码率算预算。
 *
 * 4 片 ≈ 24 秒内容：够抹平 VBR 的起伏，又能在降档后一个来回内跟上。
 */
export const BITRATE_SAMPLE_COUNT = 4;

export interface BitrateSample {
  bytes: number;
  seconds: number;
}

export function pushBitrateSample(
  samples: readonly BitrateSample[],
  sample: BitrateSample,
): BitrateSample[] {
  if (!(sample.bytes > 0) || !(sample.seconds > 0)) return samples.slice();
  return [...samples, sample].slice(-BITRATE_SAMPLE_COUNT);
}

/** 最近几片的实测码率（bps）；一片都没有时为 null。 */
export function bitrateBps(samples: readonly BitrateSample[]): number | null {
  let bytes = 0;
  let seconds = 0;
  for (const sample of samples) {
    bytes += sample.bytes;
    seconds += sample.seconds;
  }
  if (!(bytes > 0) || !(seconds > 0)) return null;
  return (bytes * 8) / seconds;
}

/**
 * 最近几片里**最高**的那一片的码率（bps）；一片都没有时为 null。
 *
 * 这一份是给 `backBufferSeconds()` 的，不是给读数的——两个用途要的统计量
 * 正好相反：
 *
 * - 读数要「现在大概是多少码率」，好和实测速度对着看 → 平均值（`bitrateBps`）；
 * - 回看缓冲要的是**字节预算别被突破**。按平均码率算秒数，遇上动作戏那一段
 *   实际占用就会超出 96MB 的预算（均值 4Mbps 算出 180 秒，而那段真码率
 *   12Mbps 时是 270MB）→ 取窗口内峰值才是安全的一侧。
 *
 * 原先那个 `Math.max` 累计值在**这个**用途上是对的，错在它同时被当成读数用，
 * 而且永不衰减。改成窗口内峰值：预算照旧按最坏的一片算，但降档之后几片就能
 * 跟着下来。
 */
export function peakBitrateBps(samples: readonly BitrateSample[]): number | null {
  let peak = 0;
  for (const sample of samples) {
    if (!(sample.bytes > 0) || !(sample.seconds > 0)) continue;
    peak = Math.max(peak, (sample.bytes * 8) / sample.seconds);
  }
  return peak > 0 ? peak : null;
}

// ---------------------------------------------------------------------------
// 带宽读数参与决策（docs/design/player-pipeline-optimization.md §C）
//
// 上面的读数原本只是「给用户看」。单变体 HLS 没有 ABR，线路不够时播放器只会
// 停下来等，45 秒后被判「供流中断」走降档——而降档降的是编码档，对带宽无能
// 为力。所以把读数接进两条决策：转码档缺粮且线路确实不够 → 带着实测带宽同档
// 重开会话（服务端按它压码率）；直通档线路不够 → 提示用户手动换画质（码率
// 改不了，只能换档）。
// ---------------------------------------------------------------------------

/** 传给服务端之前先过这道闸：样本太少或读数为空就不带，服务端按阶梯值。 */
export function downlinkHintBps(bps: number | null): number | undefined {
  if (bps === null || !Number.isFinite(bps) || bps <= 0) return undefined;
  return Math.round(bps);
}

/**
 * 缺粮（starved）要不要按带宽重开而不是降档。
 *
 * 三个条件缺一不可：判定确实是缺粮（不是解码卡死）；视频在转码（直通码率
 * 改不了，重开没用）；实测线路装不下当前码率（否则慢的是服务端转码，重开
 * 也救不了，该走原来的降档回路）。`alreadyRestarted` 是每会话一次的护栏：
 * 重开后仍缺粮说明估错了，再来一次就是无限循环。
 */
export function bandwidthRestartWanted(input: {
  cause: "starved" | "decode-stalled" | null | undefined;
  videoAction: string | null | undefined;
  downlinkBps: number | null;
  bitrateBps: number | null;
  alreadyRestarted: boolean;
}): boolean {
  if (input.cause !== "starved" || input.alreadyRestarted) return false;
  if (input.videoAction !== "transcode") return false;
  if (input.downlinkBps === null || input.bitrateBps === null) return false;
  if (input.downlinkBps <= 0 || input.bitrateBps <= 0) return false;
  return input.downlinkBps < input.bitrateBps;
}

/**
 * 直通视频缺粮（starved）要不要**越过逐级降档，直接改用转码**。
 *
 * 直出 / remux / 音频单转三档的视频都是 `-c:v copy`，码率改不了。线路装不下
 * 源码率时它们的表现完全一样：缓冲耗尽、看门狗判缺粮。原先这条走普通的
 * `failed`，逐级降到 remux、再到音频单转——码率一分没少，照样卡，连败两次才
 * 一步到软转把码率压下来。用户看到的是一分多钟转圈外加三次黑屏重开
 * （docs/design/web-player.md §6.3 的 2026-09-13 补记）。
 *
 * 所以判据与 `bandwidthRestartWanted` 对称：那条是「已在转码、线路不够 → 同档
 * 带带宽重开」，这条是「视频直通、线路不够 → 把三个直通档一并标掉，带着实测
 * 带宽去开转码会话」，服务端 `adapt_to_downlink` 会按线路压码率、必要时降高度。
 * 线路读数或源码率缺失时不作判定，退回原来的逐级降档——那时慢的可能不是线路。
 */
export function bandwidthDegradeWanted(input: {
  cause: "starved" | "decode-stalled" | null | undefined;
  videoAction: string | null | undefined;
  downlinkBps: number | null;
  bitrateBps: number | null;
}): boolean {
  if (input.cause !== "starved") return false;
  if (input.videoAction !== "copy") return false;
  if (input.downlinkBps === null || input.bitrateBps === null) return false;
  if (input.downlinkBps <= 0 || input.bitrateBps <= 0) return false;
  return input.downlinkBps < input.bitrateBps;
}

/** 直通档提示的门槛：线路低于源码率的这个倍数就算「不够」。 */
export const DIRECT_SHORTFALL_RATIO = 1.2;
/** 连续多少次 1Hz 采样都不够才提示——一次抖动不该弹提示。 */
export const DIRECT_SHORTFALL_SAMPLES = 10;

/**
 * 直通档的线路够不够（一次采样）。码率未知或读数为空一律算够——宁可不提示
 * 也不误报。
 */
export function directDownlinkShort(input: {
  downlinkBps: number | null;
  sourceBitrateBps: number | null | undefined;
}): boolean {
  if (input.downlinkBps === null || input.downlinkBps <= 0) return false;
  if (!input.sourceBitrateBps || input.sourceBitrateBps <= 0) return false;
  return input.downlinkBps < input.sourceBitrateBps * DIRECT_SHORTFALL_RATIO;
}
