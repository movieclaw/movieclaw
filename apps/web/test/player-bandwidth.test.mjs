import assert from "node:assert/strict";
import test from "node:test";

import {
  BANDWIDTH_MIN_SAMPLES,
  BANDWIDTH_WINDOW_MS,
  BITRATE_SAMPLE_COUNT,
  LOADING_WINDOW_MS,
  PROGRESS_MAX_GAP_MS,
  bandwidthBps,
  bitrateBps,
  continuedGrowthSeconds,
  createBandwidthWindow,
  createLoadingMeter,
  formatBandwidth,
  formatLoadingSpeed,
  pushBandwidthSample,
  peakBandwidthBps,
  peakBitrateBps,
  pushBitrateSample,
  readBufferedProbe,
  sampleFromProgress,
  sampleFromResourceTiming,
  sampleLoadingMeter,
} from "../lib/player/bandwidth.ts";

/** 1MB，够跨过最小样本量的门槛 */
const MB = 1024 * 1024;

test("速率按传输时间算，不按墙钟：缓冲喂饱后停下来不取，读数照旧", () => {
  let w = createBandwidthWindow();
  // 一个 1MB 的分片花了 1 秒传完，然后 10 秒什么都没下（缓冲满了）
  w = pushBandwidthSample(w, { at: 0, bytes: MB, transferMs: 1000 });
  const bps = bandwidthBps(w);
  // 1MB / 1s = 8 Mbps。墙钟口径会算成 1/11 —— 线路明明很好，读数却掉到接近 0
  assert.equal(Math.round(bps), MB * 8);
});

test("窗口外的旧样本被淘汰", () => {
  let w = createBandwidthWindow();
  // 一个很慢的老样本，加一个刚到的快样本；老的落在窗口外就不该再拖后腿
  w = pushBandwidthSample(w, { at: 0, bytes: MB, transferMs: 10_000 });
  const late = BANDWIDTH_WINDOW_MS + 1_000;
  w = pushBandwidthSample(w, { at: late, bytes: MB, transferMs: 1_000 });
  assert.equal(w.samples.length, 1);
  assert.equal(Math.round(bandwidthBps(w)), MB * 8);
});

test("样本不够就不给读数，宁可不显示也不显示错的", () => {
  const empty = createBandwidthWindow();
  assert.equal(bandwidthBps(empty), null);
  // init 分片才几 KB，单靠它算出来的速度是噪声
  const tiny = pushBandwidthSample(empty, { at: 0, bytes: 4 * 1024, transferMs: 300 });
  assert.equal(bandwidthBps(tiny), null);
  // 传输时间短到时钟本身都量不准就不作数（下限只为时钟精度留余量）
  const brief = pushBandwidthSample(empty, { at: 0, bytes: 2 * MB, transferMs: 5 });
  assert.equal(bandwidthBps(brief), null);
});

test("传输时间为 0 或负的样本直接丢掉，不能让速度变成无穷大", () => {
  const w = createBandwidthWindow();
  assert.equal(pushBandwidthSample(w, { at: 0, bytes: MB, transferMs: 0 }).samples.length, 0);
  assert.equal(pushBandwidthSample(w, { at: 0, bytes: 0, transferMs: 500 }).samples.length, 0);
});

test("多个分片按总字节 ÷ 总传输时间合并", () => {
  let w = createBandwidthWindow();
  w = pushBandwidthSample(w, { at: 0, bytes: MB, transferMs: 500 });
  w = pushBandwidthSample(w, { at: 1000, bytes: MB, transferMs: 1500 });
  // 2MB / 2s = 1MB/s
  assert.equal(Math.round(bandwidthBps(w)), MB * 8);
});

test("格式化：MB/s 与 KB/s 分档，没有读数就没有那一格", () => {
  assert.equal(formatBandwidth(null), null);
  assert.equal(formatBandwidth(0), null);
  assert.equal(formatBandwidth(MB * 8), "1.0 MB/s");
  assert.equal(formatBandwidth(MB * 8 * 3.25), "3.3 MB/s");
  assert.equal(formatBandwidth(512 * 1024 * 8), "512 KB/s");
  assert.equal(formatBandwidth(8), "0 KB/s");
});

/** 一条正常的 Resource Timing 条目：5MB 走了 4 秒 */
const entry = (overrides = {}) => ({
  responseStart: 1_000,
  responseEnd: 5_000,
  transferSize: 5 * MB + 400,
  encodedBodySize: 5 * MB,
  ...overrides,
});

test("Resource Timing：字节 ÷（末字节 − 首字节），服务端等待落在首字节之前不算", () => {
  // 请求 t=0 发出、服务端挂到 t=1000 才给首字节：分母只有 4 秒，不是 5 秒
  const sample = sampleFromResourceTiming(entry(), 9_000);
  assert.deepEqual(sample, { at: 9_000, bytes: 5 * MB + 400, transferMs: 4_000 });
});

test("缓存命中的条目丢掉：一个字节都没走网络，算进去就是拿内存速度冒充带宽", () => {
  // 回跳到已下过的分片：encodedBodySize 照旧是完整大小，transferSize 却是 0
  assert.equal(
    sampleFromResourceTiming(
      entry({ transferSize: 0, responseStart: 1_000, responseEnd: 1_002 }),
      0,
    ),
    null,
  );
});

test("跨源没有 Timing-Allow-Origin 时字段被抹成 0，宁可不给读数", () => {
  assert.equal(sampleFromResourceTiming(entry({ responseStart: 0, transferSize: 0 }), 0), null);
});

test("条目缺失或首末字节同刻都不作数，不能让速度变成无穷大", () => {
  assert.equal(sampleFromResourceTiming(null, 0), null);
  assert.equal(sampleFromResourceTiming(undefined, 0), null);
  assert.equal(sampleFromResourceTiming(entry({ responseEnd: 1_000 }), 0), null);
  assert.equal(sampleFromResourceTiming(entry({ encodedBodySize: 0 }), 0), null);
});

test("快线路上照样出读数：千兆局域网不该把这一格变成空白", () => {
  // 4Mbps 的源、6 秒一片 ≈ 2.9MB，千兆线路上一片只传 24 毫秒。窗口里两片
  // 合计 48 毫秒——原先 120ms 的下限会把它整个丢掉，而局域网放片恰恰是
  // 自建媒体库最常见的场景。
  const segment = (4e6 * 6) / 8;
  let w = createBandwidthWindow();
  w = pushBandwidthSample(w, { at: 0, bytes: segment, transferMs: 24 });
  w = pushBandwidthSample(w, { at: 6_000, bytes: segment, transferMs: 24 });
  const bps = bandwidthBps(w);
  assert.ok(bps !== null, "千兆线路上读数不该是空白");
  assert.ok(Math.abs(bps / 1e9 - 1) < 0.05, `读数 ${bps} 应当贴着 1Gbps`);
});

test("304 重校验的条目丢掉：体是从缓存取的，只有响应头走了网络", () => {
  // 刷新页面时浏览器对分片发条件请求，服务端回 304：transferSize 只有响应头
  // 那几百字节，encodedBodySize 却是完整分片。拿完整体积除一个 RTT，读数
  // 当场几百 MB/s——正是改用 Resource Timing 要修掉的那种虚高。
  const revalidated = entry({
    responseStart: 1_000,
    responseEnd: 1_020,
    transferSize: 280,
    encodedBodySize: 4 * MB,
  });
  assert.equal(sampleFromResourceTiming(revalidated, 0), null);
});

test("字节数取上网字节（含响应头），那才是压在链路上的量", () => {
  const sample = sampleFromResourceTiming(entry(), 0);
  assert.equal(sample.bytes, 5 * MB + 400);
});

// ---------------------------------------------------------------------------
// 档 0 直出：从 progress 反推
// ---------------------------------------------------------------------------

const NETWORK_LOADING = 2;
const NETWORK_IDLE = 1;

/** 造一个假 <video>：ranges 是 [起点, 末端] 的秒数对 */
const media = (currentTime, ranges, networkState = NETWORK_LOADING) => ({
  currentTime,
  networkState,
  buffered: {
    length: ranges.length,
    start: (i) => ranges[i][0],
    end: (i) => ranges[i][1],
  },
});

const BITRATE = 8e6; // 8Mbps 源
/** 8Mbps 下 1 秒内容 = 1MB */
const secondsToBytes = (s) => (s * BITRATE) / 8;

test("量的是播放头所在那段缓冲，不是最后一段", () => {
  // 往前跳过一次之后，buffered 里会留着两段：播放头在前面那段，远处还挂着
  // 一段。取 buffered.end(length - 1) 量到的是远处那段，两次相减得到的是
  // 「两段隔了多远」而不是「下了多少」。
  const probe = readBufferedProbe(media(20, [[0, 60], [1800, 1860]]), 0);
  assert.deepEqual(probe.active, { start: 0, end: 60 });
  assert.equal(probe.nextStart, 1800);
});

test("播放头不在任何缓冲区间里时没有现场可量", () => {
  const probe = readBufferedProbe(media(900, [[0, 60], [1800, 1860]]), 0);
  assert.equal(probe.active, null);
});

test("正常增长：缓冲涨了几秒 × 码率 = 这段时间下了多少字节", () => {
  const previous = readBufferedProbe(media(10, [[0, 30]]), 0);
  const current = readBufferedProbe(media(10.35, [[0, 31]]), 350);
  const sample = sampleFromProgress({ previous, current, sourceBitrateBps: BITRATE });
  assert.deepEqual(sample, { at: 350, bytes: secondsToBytes(1), transferMs: 350 });
  // 1MB / 350ms ≈ 22.9 Mbps
  assert.ok(Math.abs((sample.bytes * 8 * 1000) / sample.transferMs / 22.86e6 - 1) < 0.01);
});

test("回归：往前跳之后那一次差值必须丢掉（现场是几百 MB/s 虚高的来源）", () => {
  // 播放头在 20 秒、缓冲到 60 秒；拖到 30 分钟处，新开一段 [1800, 1805]。
  // 旧实现取「最后一段的末端」相减 = 1805 − 60 = 1745 秒内容，按 8Mbps 折
  // 1.7GB「在 350 毫秒内下完」——读数上百 MB/s。
  const previous = readBufferedProbe(media(20, [[0, 60]]), 0);
  const current = readBufferedProbe(media(1800, [[0, 60], [1800, 1805]]), 350);
  assert.equal(sampleFromProgress({ previous, current, sourceBitrateBps: BITRATE }), null);
  // 旧口径会算出多离谱：留个数字盯着，别让它悄悄回来
  const naiveBytes = ((1805 - 60) * BITRATE) / 8;
  const naiveBps = (naiveBytes * 8 * 1000) / 350;
  assert.ok(naiveBps > 10e9, `旧口径会算出 ${(naiveBps / 1e9).toFixed(0)}Gbps，够离谱`);
});

test("回归：往回跳之后那一次差值同样丢掉", () => {
  const previous = readBufferedProbe(media(1850, [[1800, 1900]]), 0);
  const current = readBufferedProbe(media(10, [[0, 20], [1800, 1900]]), 350);
  assert.equal(sampleFromProgress({ previous, current, sourceBitrateBps: BITRATE }), null);
});

test("回归：播放头这段把后面那段吃掉时不算数——那些内容是之前下好的", () => {
  // 往回跳过之后一路播到与远处那段接上：末端从 95 一下子跳到 1900
  const previous = readBufferedProbe(media(50, [[0, 95], [100, 1900]]), 0);
  const current = readBufferedProbe(media(50.35, [[0, 1900]]), 350);
  assert.equal(sampleFromProgress({ previous, current, sourceBitrateBps: BITRATE }), null);
});

test("头部被回收让起点前移不影响：那仍是同一段连续缓冲", () => {
  const previous = readBufferedProbe(media(100, [[0, 130]]), 0);
  const current = readBufferedProbe(media(100.35, [[40, 131]]), 350);
  const sample = sampleFromProgress({ previous, current, sourceBitrateBps: BITRATE });
  assert.deepEqual(sample, { at: 350, bytes: secondsToBytes(1), transferMs: 350 });
});

test("浏览器停手（networkState 不是 LOADING）的那一段不算传输时间", () => {
  // 注释承诺了「只在 NETWORK_LOADING 时采样」，原先代码里根本没有这一条
  const loading = readBufferedProbe(media(10, [[0, 30]]), 0);
  const idle = readBufferedProbe(media(10.35, [[0, 31]], NETWORK_IDLE), 350);
  assert.equal(sampleFromProgress({ previous: loading, current: idle, sourceBitrateBps: BITRATE }), null);
  assert.equal(sampleFromProgress({ previous: idle, current: loading, sourceBitrateBps: BITRATE }), null);
});

test("间隔过长的一段作废：标签页切后台回来、响应挂住都会留下长间隔", () => {
  const previous = readBufferedProbe(media(10, [[0, 30]]), 0);
  const current = readBufferedProbe(media(11, [[0, 40]]), PROGRESS_MAX_GAP_MS + 1);
  assert.equal(sampleFromProgress({ previous, current, sourceBitrateBps: BITRATE }), null);
});

test("没有上一次现场、没有码率、缓冲没涨，都不出样本", () => {
  const previous = readBufferedProbe(media(10, [[0, 30]]), 0);
  const current = readBufferedProbe(media(10.35, [[0, 30]]), 350);
  assert.equal(sampleFromProgress({ previous: null, current, sourceBitrateBps: BITRATE }), null);
  assert.equal(sampleFromProgress({ previous, current, sourceBitrateBps: null }), null);
  assert.equal(sampleFromProgress({ previous, current, sourceBitrateBps: BITRATE }), null);
});

// ---------------------------------------------------------------------------
// 实时码率：取流速度的另一半
// ---------------------------------------------------------------------------

/** 一个 6 秒分片，码率 mbps */
const frag = (mbps) => ({ bytes: (mbps * 1e6 * 6) / 8, seconds: 6 });

test("实时码率取最近几片的平均，不是开播以来的最大值", () => {
  // 一部均值 4Mbps 的片子，中间一片动作戏冲到 12.1Mbps
  let samples = [];
  for (const mbps of [3.5, 4.2, 3.8, 12.1, 4.0, 3.6, 4.4, 3.9]) {
    samples = pushBitrateSample(samples, frag(mbps));
  }
  const bps = bitrateBps(samples);
  // 最近四片是 12.1 之后的那些，读数应当回落到 4Mbps 一带
  assert.ok(bps / 1e6 < 5, `读数 ${(bps / 1e6).toFixed(1)}Mbps 应当回到 4Mbps 一带`);
});

test("回归：降档之后码率读数必须跟着下来，否则「降档有没有用」就看不出来", () => {
  let samples = [];
  for (const mbps of [4.0, 12.1, 4.0, 3.6]) samples = pushBitrateSample(samples, frag(mbps));
  const peak = bitrateBps(samples);
  // 降档到 2Mbps，再过四片
  for (const mbps of [2.0, 2.1, 1.9, 2.0]) samples = pushBitrateSample(samples, frag(mbps));
  const after = bitrateBps(samples);
  assert.ok(after < peak, "降档后码率读数没有下来");
  assert.ok(Math.abs(after / 1e6 - 2) < 0.2, `降档后应当显示 2Mbps 上下，实际 ${(after / 1e6).toFixed(1)}`);
});

test("码率窗口只留最近几片，坏样本不入窗", () => {
  let samples = [];
  for (let i = 0; i < BITRATE_SAMPLE_COUNT + 3; i += 1) samples = pushBitrateSample(samples, frag(4));
  assert.equal(samples.length, BITRATE_SAMPLE_COUNT);
  assert.equal(pushBitrateSample([], { bytes: 0, seconds: 6 }).length, 0);
  assert.equal(pushBitrateSample([], { bytes: 100, seconds: 0 }).length, 0);
  assert.equal(bitrateBps([]), null);
});

test("回看缓冲的预算用窗口内峰值，不用平均——平均会让字节预算被突破", () => {
  let samples = [];
  for (const mbps of [4.0, 12.1, 3.8, 4.2]) samples = pushBitrateSample(samples, frag(mbps));
  // 读数要的是「现在大概多少码率」
  assert.ok(Math.abs(bitrateBps(samples) / 1e6 - 6.0) < 0.5);
  // 预算要的是最坏的那一片
  assert.ok(Math.abs(peakBitrateBps(samples) / 1e6 - 12.1) < 0.1);
  assert.equal(peakBitrateBps([]), null);
});

test("峰值同样只看窗口：降档之后几片就跟着下来，不像原先的累计最大值", () => {
  let samples = [];
  for (const mbps of [4.0, 12.1, 3.8, 4.2]) samples = pushBitrateSample(samples, frag(mbps));
  for (const mbps of [2.0, 2.1, 1.9, 2.0]) samples = pushBitrateSample(samples, frag(mbps));
  assert.ok(peakBitrateBps(samples) / 1e6 < 2.5, "降档后峰值没有跟着下来");
});

// ---------------------------------------------------------------------------
// 读数参与决策（docs/design/player-pipeline-optimization.md §C）
// ---------------------------------------------------------------------------

import {
  DIRECT_SHORTFALL_RATIO,
  bandwidthDegradeWanted,
  bandwidthRestartWanted,
  directDownlinkShort,
  downlinkHintBps,
} from "../lib/player/bandwidth.ts";

test("缺粮 + 视频直通 + 线路装不下 → 越过逐级降档直接转码；其余情况不动", () => {
  // 直出 / remux / 音频单转的视频都是 copy，码率改不了；逐级降到 remux 照样
  // 缺粮，用户要转圈一分多钟外加三次黑屏重开才落到能压码率的转码档
  const base = {
    cause: "starved",
    videoAction: "copy",
    downlinkBps: 3_000_000,
    bitrateBps: 8_000_000,
  };
  assert.equal(bandwidthDegradeWanted(base), true);
  // 解码卡死不是带宽问题，照旧逐级降
  assert.equal(bandwidthDegradeWanted({ ...base, cause: "decode-stalled" }), false);
  // 已经在转码：那是 bandwidthRestartWanted 的地盘（同档重开），不是这条
  assert.equal(bandwidthDegradeWanted({ ...base, videoAction: "transcode" }), false);
  // 线路装得下源码率：慢的不是线路，别把直通白白换成转码
  assert.equal(bandwidthDegradeWanted({ ...base, downlinkBps: 9_000_000 }), false);
  // 读数缺一样都不猜
  assert.equal(bandwidthDegradeWanted({ ...base, downlinkBps: null }), false);
  assert.equal(bandwidthDegradeWanted({ ...base, bitrateBps: null }), false);
  assert.equal(bandwidthDegradeWanted({ ...base, bitrateBps: 0 }), false);
});

test("带宽提示：没有读数就不带，有读数取整", () => {
  assert.equal(downlinkHintBps(null), undefined);
  assert.equal(downlinkHintBps(0), undefined);
  assert.equal(downlinkHintBps(Number.NaN), undefined);
  assert.equal(downlinkHintBps(1234567.8), 1234568);
});

test("缺粮 + 转码 + 线路装不下 → 按带宽重开；其余情况走原来的降档", () => {
  const base = {
    cause: "starved",
    videoAction: "transcode",
    downlinkBps: 2_000_000,
    bitrateBps: 4_000_000,
    alreadyRestarted: false,
  };
  assert.equal(bandwidthRestartWanted(base), true);
  // 解码卡死不是带宽问题
  assert.equal(bandwidthRestartWanted({ ...base, cause: "decode-stalled" }), false);
  // 直通视频码率改不了，重开没用
  assert.equal(bandwidthRestartWanted({ ...base, videoAction: "copy" }), false);
  // 线路装得下：慢的是服务端转码，重开救不了
  assert.equal(bandwidthRestartWanted({ ...base, downlinkBps: 8_000_000 }), false);
  // 没有读数不能猜
  assert.equal(bandwidthRestartWanted({ ...base, downlinkBps: null }), false);
  assert.equal(bandwidthRestartWanted({ ...base, bitrateBps: null }), false);
  // 每会话只试一次
  assert.equal(bandwidthRestartWanted({ ...base, alreadyRestarted: true }), false);
});

test("直通档线路够不够：低于源码率的 1.2 倍算不够，缺读数一律算够", () => {
  assert.equal(directDownlinkShort({ downlinkBps: 5e6, sourceBitrateBps: 8e6 }), true);
  assert.equal(
    directDownlinkShort({ downlinkBps: 8e6 * DIRECT_SHORTFALL_RATIO, sourceBitrateBps: 8e6 }),
    false,
  );
  assert.equal(directDownlinkShort({ downlinkBps: null, sourceBitrateBps: 8e6 }), false);
  assert.equal(directDownlinkShort({ downlinkBps: 5e6, sourceBitrateBps: null }), false);
  assert.equal(directDownlinkShort({ downlinkBps: 5e6, sourceBitrateBps: 0 }), false);
});

// ---------------------------------------------------------------------------
// 实时加载速度（顶栏「↓」）：在下载报实际速度、没在下载报 0、量不出字节不显示
// ---------------------------------------------------------------------------

/** 按顺序喂一串采样点，返回每一步的读数 */
function feedLoading(points) {
  let meter = createLoadingMeter();
  return points.map(([at, bytes]) => {
    meter = sampleLoadingMeter(meter, { at, bytes });
    return meter.bps;
  });
}

test("加载速度：量不出字节时没有读数，不报 0", () => {
  assert.deepEqual(feedLoading([[0, null], [1000, null]]), [null, null]);
});

test("加载速度：第一个点只当起点，满 900ms 才给第一个读数", () => {
  const [first, early, ready] = feedLoading([[0, 200_000], [500, 700_000], [1000, 200_000 + MB]]);
  assert.equal(first, null);
  assert.equal(early, null);
  assert.equal(Math.round(ready), MB * 8);
});

test("加载速度：稳定下载报实际速度，缓冲喂饱停下来后归零", () => {
  const points = [[0, 0]];
  for (let second = 1; second <= 5; second += 1) points.push([second * 1000, second * MB]);
  // 停下：第一秒窗口里还有一半在下，满 2 秒后归零，之后一直是 0
  points.push([6000, 5 * MB], [7000, 5 * MB], [30_000, 5 * MB]);
  const readings = feedLoading(points);
  for (const bps of readings.slice(1, 6)) assert.equal(Math.round(bps), MB * 8);
  assert.equal(Math.round(readings[6]), (MB * 8) / 2);
  assert.equal(readings[7], 0);
  assert.equal(readings[8], 0);
});

test(`加载速度：计数攒一坨再到时按 ${LOADING_WINDOW_MS / 1000} 秒窗口摊平`, () => {
  // 实际一直 1MB/s，进度回调这一秒只到了 0.2MB、下一秒补到 1.8MB：两次都按 2 秒窗口读
  const readings = feedLoading([
    [0, 0],
    [1000, MB],
    [2000, 2 * MB],
    [3000, 2.2 * MB],
    [4000, 4 * MB],
  ]);
  assert.equal(Math.round(readings[3]), Math.round(MB * 8 * 0.6));
  assert.equal(Math.round(readings[4]), MB * 8);
});

test("加载速度：一秒问两次时窗口按时间算，读数不因调用次数变化", () => {
  const points = [[0, 0]];
  for (let step = 1; step <= 10; step += 1) points.push([step * 500, step * 0.5 * MB]);
  const readings = feedLoading(points);
  assert.equal(Math.round(readings.at(-1)), MB * 8);
});

test("加载速度：计数倒退（换了取流对象）从头量，不出负数", () => {
  const readings = feedLoading([[0, 0], [1000, 10 * MB], [2000, 100_000], [3000, 100_000 + MB]]);
  assert.equal(readings[2], null);
  assert.equal(Math.round(readings[3]), MB * 8);
});

test("加载速度的文案：没在下载写 0 KB/s，没有读数不显示", () => {
  assert.equal(formatLoadingSpeed(null), null);
  assert.equal(formatLoadingSpeed(0), "0 KB/s");
  assert.equal(formatLoadingSpeed(MB * 8 * 3.2), "3.2 MB/s");
  assert.equal(formatLoadingSpeed(512 * 1024 * 8), "512 KB/s");
});

test("直出的缓冲增长：不看 networkState，只认同一段连续缓冲", () => {
  const probe = (at, loading, start, end, nextStart = null) => ({ at, loading, active: { start, end }, nextStart });
  // Chrome 直出早早报空闲，之后照样一阵一阵取数据：报空闲期间缓冲从 20.7 涨到 28.6，要算进加载速度
  assert.equal(
    Math.round(continuedGrowthSeconds(probe(0, false, 0, 20.7), probe(1000, false, 0, 28.6)) * 10) / 10,
    7.9,
  );
  // 往前跳到远处新开一段：不是同一段，量不了
  assert.equal(continuedGrowthSeconds(probe(0, true, 0, 20), probe(1000, true, 600, 605)), null);
  // 这一段吃掉了后面那段：末端跳过去的那些是之前下好的
  assert.equal(continuedGrowthSeconds(probe(0, true, 0, 20, 30), probe(1000, true, 0, 40)), null);
  // 没涨就是 0
  assert.equal(continuedGrowthSeconds(probe(0, false, 0, 20), probe(1000, false, 0, 20)), 0);
});

test("HLS 带宽取最快一片：接收端读慢的那几片不把带宽拖低", () => {
  let w = createBandwidthWindow();
  // 线路 1MB/s；个别片被读慢到三成、五成（实测过）——平均会掉到 0.6 左右，最快一片才是线路
  for (const [at, transferMs] of [[0, 1000], [2000, 3400], [4000, 2000], [6000, 1030]]) {
    w = pushBandwidthSample(w, { at, bytes: MB, transferMs });
  }
  assert.equal(Math.round(peakBandwidthBps(w)), MB * 8);
  assert.ok(bandwidthBps(w) < MB * 8 * 0.7);
});

test("HLS 带宽：几 KB 的 init 分片算出来的速度是噪声，不算", () => {
  let w = createBandwidthWindow();
  w = pushBandwidthSample(w, { at: 0, bytes: 8 * 1024, transferMs: 1 });
  assert.equal(peakBandwidthBps(w), null);
  w = pushBandwidthSample(w, { at: 100, bytes: MB, transferMs: 1000 });
  assert.equal(Math.round(peakBandwidthBps(w)), MB * 8);
});

test(`HLS 带宽：分片稀疏时窗口外也至少留最近 ${BANDWIDTH_MIN_SAMPLES} 片`, () => {
  let w = createBandwidthWindow();
  const push = (at, transferMs) => {
    w = pushBandwidthSample(w, { at, bytes: MB, transferMs }, { minSamples: BANDWIDTH_MIN_SAMPLES });
  };
  // 转码会话隔十几秒才到一片，最新那片又被读慢了：仍按最近三片里最快的算
  push(0, 1000);
  push(17_000, 14_000);
  assert.equal(Math.round(peakBandwidthBps(w)), MB * 8);
  push(31_000, 8_300);
  assert.equal(Math.round(peakBandwidthBps(w)), MB * 8);
  // 第四片到了，最老那片（已在窗口外）才被挤掉
  push(45_000, 4_000);
  assert.equal(w.samples.length, BANDWIDTH_MIN_SAMPLES);
});
