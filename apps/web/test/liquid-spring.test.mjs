import assert from "node:assert/strict";
import test from "node:test";

import {
  liquidKeyframes,
  liquidTransform,
  sampleAt,
  simulateSpring,
  stretchForVelocity,
} from "../lib/liquid-spring.ts";

test("弹簧最终精确落在目标上，并在 1 秒内静止", () => {
  const frames = simulateSpring(0, 180);
  const last = frames[frames.length - 1];
  assert.equal(last.x, 180);
  assert.equal(last.v, 0);
  assert.ok(last.t < 1000, `静止用时 ${last.t}ms`);
});

test("欠阻尼：行程中越过目标一点再回弹（液滴的惯性），但过冲不超过 8%", () => {
  const frames = simulateSpring(0, 200);
  const peak = Math.max(...frames.map((f) => f.x));
  assert.ok(peak > 200, "应当有过冲");
  assert.ok(peak < 216, `过冲过大：${peak}`);
});

test("反向滑动同样成立（从右往左）", () => {
  const frames = simulateSpring(300, 0);
  assert.equal(frames[frames.length - 1].x, 0);
  assert.ok(Math.min(...frames.map((f) => f.x)) < 0);
});

test("拉伸随速度增长且有上限", () => {
  assert.equal(stretchForVelocity(0), 1);
  const s = stretchForVelocity(1300);
  assert.ok(s > 1 && s < 1.3, `中速应有可见但未封顶的拉伸：${s}`);
  assert.ok(stretchForVelocity(-1300) === stretchForVelocity(1300), "方向不影响拉伸量");
  assert.equal(stretchForVelocity(1e6), 1.3);
});

test("静止时 transform 不带形变", () => {
  assert.equal(liquidTransform(42, 0, 0), "translateX(42.00px) scale(1.0000, 1.0000)");
});

test("关键帧 offset 单调递增、首尾为 0 和 1、首尾不抬起", () => {
  const kf = liquidKeyframes(simulateSpring(0, 160));
  assert.equal(kf[0].offset, 0);
  assert.equal(kf[kf.length - 1].offset, 1);
  for (let i = 1; i < kf.length; i++) assert.ok(kf[i].offset >= kf[i - 1].offset);
  assert.match(kf[kf.length - 1].transform, /scale\(1\.0000, 1\.0000\)/);
});

test("原地重播（无位移）不抬起", () => {
  const kf = liquidKeyframes(simulateSpring(80, 80));
  for (const k of kf) assert.match(k.transform, /scale\(1\.0000, 1\.0000\)/);
});

test("打断续算：按已播放时长插值出位置与速度，超时取末帧", () => {
  const frames = simulateSpring(0, 100);
  const mid = sampleAt(frames, frames[5].t);
  assert.equal(mid.x, frames[5].x);
  assert.ok(sampleAt(frames, 99999).x === 100);
  assert.equal(sampleAt(frames, -5).x, 0);
});
