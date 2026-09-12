import assert from "node:assert/strict";
import test from "node:test";

import {
  BANDWIDTH_WINDOW_MS,
  BITRATE_SAMPLE_COUNT,
  PROGRESS_MAX_GAP_MS,
  bandwidthBps,
  bitrateBps,
  createBandwidthWindow,
  formatBandwidth,
  pushBandwidthSample,
  peakBitrateBps,
  pushBitrateSample,
  readBufferedProbe,
  sampleFromProgress,
  sampleFromResourceTiming,
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
