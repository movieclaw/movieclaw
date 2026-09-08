import assert from "node:assert/strict";
import test from "node:test";

import { canHoldSpeed, holdSpeedReducer } from "../lib/player/hold-speed.ts";

test("按住够久才生效：中途松手不算长按", () => {
  assert.equal(holdSpeedReducer("idle", "press"), "pending");
  assert.equal(holdSpeedReducer("pending", "release"), "idle");
  assert.equal(holdSpeedReducer("pending", "elapsed"), "active");
});

test("手指滑起来就撤：长按与横滑共用一根手指，不能两个都生效", () => {
  assert.equal(holdSpeedReducer("pending", "move"), "idle");
  assert.equal(holdSpeedReducer("active", "move"), "idle");
});

test("松手还原；缓冲跟不上也强制还原", () => {
  assert.equal(holdSpeedReducer("active", "release"), "idle");
  // 倍速把前向缓冲吃光 = 追上了编码器，按着不放只会对着转圈
  assert.equal(holdSpeedReducer("active", "starve"), "idle");
});

test("重复的 press 不会把已经生效的倍速打回 pending", () => {
  assert.equal(holdSpeedReducer("active", "press"), "active");
  assert.equal(holdSpeedReducer("pending", "press"), "pending");
});

test("暂停 / 锁屏 / 多指都不许起长按", () => {
  assert.equal(canHoldSpeed({ paused: false, locked: false, touchCount: 1 }), true);
  // 停着的画面上「按住让它快点播」没有意义，误触代价却实打实
  assert.equal(canHoldSpeed({ paused: true, locked: false, touchCount: 1 }), false);
  assert.equal(canHoldSpeed({ paused: false, locked: true, touchCount: 1 }), false);
  assert.equal(canHoldSpeed({ paused: false, locked: false, touchCount: 2 }), false);
});
