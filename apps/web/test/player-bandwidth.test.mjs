import assert from "node:assert/strict";
import test from "node:test";

import {
  BANDWIDTH_WINDOW_MS,
  bandwidthBps,
  createBandwidthWindow,
  formatBandwidth,
  pushBandwidthSample,
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
  // 传输时间太短同样不作数：几十毫秒的分母会把误差放大成好几倍的虚高
  const brief = pushBandwidthSample(empty, { at: 0, bytes: 2 * MB, transferMs: 10 });
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
