import assert from "node:assert/strict";
import test from "node:test";

import {
  FULL_SWEEP_SEEK_S,
  MIN_BRIGHTNESS,
  applySwipe,
  classifyIntent,
  classifyTouchZone,
  pointerOffsetX,
  seekDeltaMs,
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

test("方向裁决：位移不够大先不算数——轻点是控制层开关，不该被当成手势", () => {
  assert.equal(classifyIntent(0, 8), null);
  assert.equal(classifyIntent(8, 0), null);
  assert.equal(classifyIntent(8, 8), null);
});

test("方向裁决：竖滑调亮度/音量，横滑拖进度，按主轴分", () => {
  assert.equal(classifyIntent(0, -20), "vertical");
  assert.equal(classifyIntent(30, -20), "horizontal");
  assert.equal(classifyIntent(-40, 5), "horizontal");
  // 正好 45° 归横向，与从前「竖直要严格大于水平」的判据一致
  assert.equal(classifyIntent(20, 20), "horizontal");
});

test("横滑换算：划过一整屏宽正好是约定的秒数，方向跟着位移符号", () => {
  assert.equal(seekDeltaMs(390, 390), FULL_SWEEP_SEEK_S * 1000);
  assert.equal(seekDeltaMs(-390, 390), -FULL_SWEEP_SEEK_S * 1000);
  assert.equal(seekDeltaMs(195, 390), (FULL_SWEEP_SEEK_S / 2) * 1000);
  assert.equal(seekDeltaMs(0, 390), 0);
});

test("横滑换算：宽度为 0 不产生 Infinity/NaN", () => {
  // 真实落点最终由 clampSeekTarget 夹进片长，这里只保证不把 NaN 传下去
  assert.equal(Number.isFinite(seekDeltaMs(100, 0)), true);
});

test("伪横屏下横滑的位移取自布局坐标——物理上是竖着划的", () => {
  // 用户横过来拿手机、手指从左向右划，物理上是 y 从小到大。
  const start = toLayoutPoint(200, 100, FAKE_LANDSCAPE);
  const now = toLayoutPoint(200, 300, FAKE_LANDSCAPE);
  assert.equal(classifyIntent(now.x - start.x, now.y - start.y), "horizontal");
  assert.equal(seekDeltaMs(now.x - start.x, now.width) > 0, true);
});

// ---------------------------------------------------------------------------
// 元素内的横向位置：伪横屏下不能直接用 clientX（2026-09-08 实测发现）
// ---------------------------------------------------------------------------

test("常规方向：沿元素的 clientX 量", () => {
  const rect = { left: 28, top: 736, width: 334, height: 44 };
  const r = pointerOffsetX({ clientX: 195, clientY: 750 }, rect, false);
  assert.equal(r.offset, 167);
  assert.equal(r.length, 334);
});

test("伪横屏：布局 x 沿物理 y，量的是 clientY 与外接矩形的高", () => {
  // 真机 390×844 视口实测：进度条转 90° 后外接矩形是 44×788，
  // 用 clientX/rect.width 等于把整部片映射到 44 个物理像素上
  const rect = { left: 64, top: 28, width: 44, height: 788 };
  const r = pointerOffsetX({ clientX: 80, clientY: 422 }, rect, true);
  assert.equal(r.offset, 394);
  assert.equal(r.length, 788);
  // 半程就是半程：换算完的比例必须是 0.5
  assert.equal(r.offset / r.length, 0.5);
});
