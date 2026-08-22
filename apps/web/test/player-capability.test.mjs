import assert from "node:assert/strict";
import test from "node:test";

import {
  buildCapabilitySnapshot,
  pickAudioChannels,
  pickVideoSupport,
} from "../lib/player/capability.ts";

const yes = { supported: true, smooth: true, powerEfficient: true };
const no = { supported: false, smooth: false, powerEfficient: false };
const choppy = { supported: true, smooth: false, powerEfficient: false };

test("取能流畅解码的最大高度，而不是能解码的最大高度", () => {
  // 4K 能解但掉帧、1080p 流畅：应上报 1080p 而不是 2160p——否则服务端会把
  // 一部 4K 直通给一台放不动的机器，用户看到的是卡成幻灯片。
  const picked = pickVideoSupport([
    { height: 2160, probe: choppy },
    { height: 1080, probe: yes },
    { height: 720, probe: yes },
  ]);
  assert.deepEqual(picked, { max_height: 1080, smooth: true, power_efficient: true });
});

test("支持但没有一档流畅时，上报 smooth=false 让服务端降分辨率", () => {
  const picked = pickVideoSupport([
    { height: 2160, probe: choppy },
    { height: 1080, probe: choppy },
  ]);
  assert.deepEqual(picked, { max_height: 2160, smooth: false, power_efficient: false });
});

test("一档都不支持的编码整个不出现在快照里", () => {
  assert.equal(pickVideoSupport([{ height: 1080, probe: no }]), null);
});

test("音轨取能直通的最大声道数", () => {
  assert.equal(
    pickAudioChannels([
      { channels: 8, probe: no },
      { channels: 6, probe: yes },
      { channels: 2, probe: yes },
    ]),
    6,
  );
  assert.equal(pickAudioChannels([{ channels: 2, probe: no }]), null);
});

test("codec 上报的是家族名而不是探测用的 RFC 6381 串", async () => {
  // 服务端要拿它和 ffprobe 落库的 codec_name 直接比对，带 profile 的全串对不上。
  const snapshot = await buildCapabilitySnapshot(
    async ({ kind, contentType }) =>
      (kind === "video" && contentType.includes("avc1")) ||
      (kind === "audio" && contentType.includes("mp4a.40.2"))
        ? yes
        : no,
    { mse: "full", hdr_passthrough: false, is_mobile: false, native_hls: false },
  );
  assert.deepEqual(
    snapshot.video.map((v) => v.codec),
    ["h264"],
  );
  assert.deepEqual(
    snapshot.audio.map((a) => a.codec),
    ["aac"],
  );
});

test("没有 MSE 也没有原生 HLS 的客户端只报 mp4 容器", async () => {
  const base = {
    hdr_passthrough: false,
    is_mobile: false,
  };
  const allYes = async () => yes;

  const noMse = await buildCapabilitySnapshot(allYes, {
    ...base,
    mse: "none",
    native_hls: false,
  });
  assert.deepEqual(noMse.containers, ["mp4"]);

  // iOS：只有 ManagedMediaSource，但原生 HLS 在——分片照样喂得进去
  const ios = await buildCapabilitySnapshot(allYes, {
    ...base,
    mse: "managed",
    native_hls: true,
  });
  assert.deepEqual(ios.containers, ["mp4", "hls-fmp4"]);
});
