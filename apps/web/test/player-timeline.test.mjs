import assert from "node:assert/strict";
import test from "node:test";

import {
  SEEK_TAIL_GUARD_MS,
  clampSeekTarget,
  planSeek,
  toFileMs,
  toSessionSeconds,
} from "../lib/player/timeline.ts";

// ---------------------------------------------------------------------------
// 会话时间轴 ↔ 文件时间轴
// ---------------------------------------------------------------------------

test("会话相对制：文件时间 = start_ms + currentTime", () => {
  assert.equal(toFileMs(30, 600_000), 630_000);
  assert.equal(toSessionSeconds(630_000, 600_000), 30);
});

test("往回拖到会话起点之前，换算结果为负且不夹到 0", () => {
  // 夹成 0 会让「拖到 20 分钟」变成静默跳回会话开头，且没有任何提示；
  // planSeek 正是靠这个负数判断要不要换会话。
  assert.equal(toSessionSeconds(60_000, 600_000), -540);
});

// ---------------------------------------------------------------------------
// 跳转落点的夹紧
// ---------------------------------------------------------------------------

test("越过片尾的落点夹回片长之内，留出片尾余量", () => {
  // 片尾连按快进、或把进度条拖到最右端都会给出这种目标。不夹的话换会话
  // 那条路会开出一个 -ss 落在文件末尾的会话，ffmpeg 一帧都转不出来。
  assert.equal(clampSeekTarget(7_200_000, 7_200_000), 7_200_000 - SEEK_TAIL_GUARD_MS);
  assert.equal(clampSeekTarget(9_999_999, 7_200_000), 7_200_000 - SEEK_TAIL_GUARD_MS);
});

test("片长之内的落点原样通过", () => {
  assert.equal(clampSeekTarget(1_800_000, 7_200_000), 1_800_000);
});

test("负数落点夹到 0——快退键在开头会一路减到负值", () => {
  assert.equal(clampSeekTarget(-5_000, 7_200_000), 0);
  assert.equal(clampSeekTarget(-5_000, null), 0);
});

test("片长未知时不夹：那时进度条本来就禁用，越界交给浏览器收住", () => {
  assert.equal(clampSeekTarget(9_999_999, null), 9_999_999);
  assert.equal(clampSeekTarget(9_999_999, 0), 9_999_999);
});

test("极短片子夹到 0 而不是负数", () => {
  assert.equal(clampSeekTarget(900, 800), 0);
});

// ---------------------------------------------------------------------------
// 一次 seek 该走哪条路
// ---------------------------------------------------------------------------

const session = { startMs: 600_000, seekableEndSeconds: 120, hasSession: true };

test("没有会话（档 0 / VOD 全片列表）永远就地跳", () => {
  assert.deepEqual(planSeek(7_000_000, { ...session, hasSession: false }), {
    kind: "native",
    seconds: 6400,
  });
});

test("落在已转区间之内就地跳", () => {
  assert.deepEqual(planSeek(660_000, session), { kind: "native", seconds: 60 });
});

test("拖出已转区间要换会话——干等 ffmpeg 追上来是转不完的圈", () => {
  assert.deepEqual(planSeek(1_800_000, session), { kind: "restart", startMs: 1_800_000 });
});

test("往回拖到本次会话起点之前也要换会话", () => {
  assert.deepEqual(planSeek(60_000, session), { kind: "restart", startMs: 60_000 });
});

test("夹紧之后的片尾落点仍落在已转区间内，不会白换一次会话", () => {
  // 会话从 0 起、整片都已转出时，拖到最右端应当就地跳。
  const whole = { startMs: 0, seekableEndSeconds: 7200, hasSession: true };
  const target = clampSeekTarget(7_200_000, 7_200_000);
  assert.deepEqual(planSeek(target, whole), { kind: "native", seconds: 7199 });
});
