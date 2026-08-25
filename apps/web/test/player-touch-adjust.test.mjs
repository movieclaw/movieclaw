import assert from "node:assert/strict";
import test from "node:test";

import {
  MIN_BRIGHTNESS,
  applySwipe,
  classifyTouchZone,
  isVerticalIntent,
  toLayoutPoint,
} from "../lib/player/touch-adjust.ts";

const PORTRAIT = { width: 390, height: 844, fakeLandscape: false };
const FAKE_LANDSCAPE = { width: 390, height: 844, fakeLandscape: true };

test("竖屏：左半屏是亮度、右半屏是音量", () => {
  assert.equal(classifyTouchZone(100, 400, PORTRAIT), "brightness");
  assert.equal(classifyTouchZone(300, 400, PORTRAIT), "volume");
});

test("顶部与底部排除带不劫持手势——那里是通知中心起手区和进度条", () => {
  assert.equal(classifyTouchZone(100, 40, PORTRAIT), null); // 顶部 12% 内
  assert.equal(classifyTouchZone(100, 800, PORTRAIT), null); // 底部 24% 内
});

test("左右缘排除带与返回手势守卫同宽，不当成调节起手点", () => {
  assert.equal(classifyTouchZone(10, 400, PORTRAIT), null);
  assert.equal(classifyTouchZone(385, 400, PORTRAIT), null);
});

test("伪横屏坐标映射：布局 x 沿物理 y、布局 y 沿物理 x 反向", () => {
  // 容器顺时针转 90°：物理左上角 (0,0) → 布局 (0, width)
  const p = toLayoutPoint(0, 0, FAKE_LANDSCAPE);
  assert.deepEqual(p, { x: 0, y: 390, width: 844, height: 390 });
  // 物理右上角 (390,0) → 布局左上角 (0,0)
  assert.deepEqual(toLayoutPoint(390, 0, FAKE_LANDSCAPE), { x: 0, y: 0, width: 844, height: 390 });
});

test("伪横屏：用户视角的左半屏（物理上半）是亮度、右半（物理下半）是音量", () => {
  // 横过来拿的手机：布局 x = 物理 y。物理 y=200 → 布局 x=200 < 844/2 → 左半
  assert.equal(classifyTouchZone(195, 200, FAKE_LANDSCAPE), "brightness");
  assert.equal(classifyTouchZone(195, 700, FAKE_LANDSCAPE), "volume");
});

test("向上滑增大、向下滑减小，滑过 60% 屏高拉满全程", () => {
  const height = 844;
  const sweep = height * 0.6;
  assert.equal(applySwipe("volume", 0.5, -sweep / 2, height), 1); // 上滑半程 +0.5
  assert.equal(applySwipe("volume", 0.5, sweep / 2, height), 0); // 下滑半程 -0.5
});

test("clamp：音量下限 0，亮度下限保留一点画面", () => {
  assert.equal(applySwipe("volume", 0.1, 10_000, 844), 0);
  assert.equal(applySwipe("brightness", 0.5, 10_000, 844), MIN_BRIGHTNESS);
  assert.equal(applySwipe("volume", 0.9, -10_000, 844), 1);
});

test("竖直意图判定：位移要够大且明显竖直，斜划和轻点都不算", () => {
  assert.equal(isVerticalIntent(0, -20), true);
  assert.equal(isVerticalIntent(0, 8), false); // 不到激活门槛
  assert.equal(isVerticalIntent(30, -20), false); // 更像横划
});
