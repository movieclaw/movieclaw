import assert from "node:assert/strict";
import test from "node:test";

import {
  planSystemTrackModes,
  resolvePlaybackMode,
  shouldApplyPostAttachSeek,
} from "../lib/player/playback-mode.ts";

const CAP_DESKTOP = { mse: "full", native_hls: false, is_mobile: false };
const CAP_IPHONE = { mse: "managed", native_hls: true, is_mobile: true };
const CAP_LEGACY = { mse: "none", native_hls: true, is_mobile: true };

function session(overrides = {}) {
  return {
    stream_url: "/s/index.m3u8?token=t",
    master_url: "/s/master.m3u8?token=t",
    timeline: "file",
    start_ms: 120_000,
    decision: { container: "hls-fmp4" },
    ...overrides,
  };
}

test("档 0 直出：direct 引擎、自绘字幕、时间轴零点", () => {
  const mode = resolvePlaybackMode(
    session({ decision: { container: "mp4" }, master_url: null }),
    CAP_DESKTOP,
  );
  assert.equal(mode.engine, "direct");
  assert.equal(mode.subtitleRenderer, "overlay");
  assert.equal(mode.originMs, 0);
});

test("桌面 MSE：hls.js 吃媒体列表，自绘 + PiP 补丁轨", () => {
  const mode = resolvePlaybackMode(session(), CAP_DESKTOP);
  assert.equal(mode.engine, "mse");
  assert.equal(mode.streamUrl, "/s/index.m3u8?token=t");
  assert.equal(mode.subtitleRenderer, "overlay");
  assert.equal(mode.pipPatchTrack, true);
});

test("现代 iPhone + VOD：ManagedMediaSource 走 hls.js，避免原生 VOD 解码边界", () => {
  const mode = resolvePlaybackMode(session(), CAP_IPHONE);
  assert.equal(mode.engine, "mse");
  assert.equal(mode.streamUrl, "/s/index.m3u8?token=t");
  assert.equal(mode.subtitleRenderer, "overlay");
  assert.equal(mode.pipPatchTrack, true);
});

test("没有 MSE 的老 iPhone + VOD：保留原生 HLS 字幕轨兜底", () => {
  const mode = resolvePlaybackMode(session(), CAP_LEGACY);
  assert.equal(mode.engine, "native-hls");
  assert.equal(mode.streamUrl, "/s/master.m3u8?token=t");
  assert.equal(mode.subtitleRenderer, "system-track");
  assert.equal(mode.pipPatchTrack, false);
});

test("iPhone 但会话是旧相对时间轴：不走原生（EVENT 列表会被当直播）", () => {
  const mode = resolvePlaybackMode(
    session({ timeline: "session", master_url: null }),
    CAP_IPHONE,
  );
  assert.equal(mode.engine, "mse");
  assert.equal(mode.originMs, 120_000); // 会话相对制参照 start_ms
  assert.equal(mode.seekBeyondBufferedRestarts, true); // EVENT 列表越界要换会话
});

test("VOD 会话 seek 不换会话（列表覆盖全片）", () => {
  const mode = resolvePlaybackMode(session(), CAP_DESKTOP);
  assert.equal(mode.seekBeyondBufferedRestarts, false);
});

test("文件绝对制的参照点为 0", () => {
  const mode = resolvePlaybackMode(session(), CAP_DESKTOP);
  assert.equal(mode.originMs, 0);
});

test("无 MSE 老设备兜底：原生硬吃媒体列表，字幕自绘降级", () => {
  const mode = resolvePlaybackMode(session({ master_url: null }), CAP_LEGACY);
  assert.equal(mode.engine, "native-hls");
  assert.equal(mode.streamUrl, "/s/index.m3u8?token=t");
  assert.equal(mode.subtitleRenderer, "overlay");
});

test("原生 HLS 的高位首次 seek 只由引擎执行，组件不得重复跳转", () => {
  assert.equal(shouldApplyPostAttachSeek("native-hls", 2885.5), false);
});

test("其他引擎的高位首次 seek 仍由组件补齐，低位起播不需要 seek", () => {
  assert.equal(shouldApplyPostAttachSeek("mse", 2885.5), true);
  assert.equal(shouldApplyPostAttachSeek("direct", 2885.5), true);
  assert.equal(shouldApplyPostAttachSeek("mse", 1), false);
});

test("consent/rejected（无 stream_url）返回 null", () => {
  assert.equal(resolvePlaybackMode(session({ stream_url: null }), CAP_DESKTOP), null);
});

test("系统轨 mode 规划：选中恒 showing，其余 disabled", () => {
  assert.deepEqual(planSystemTrackModes(3, 1), ["disabled", "showing", "disabled"]);
  assert.deepEqual(planSystemTrackModes(2, -1), ["disabled", "disabled"]);
  assert.deepEqual(planSystemTrackModes(0, 0), []);
});
