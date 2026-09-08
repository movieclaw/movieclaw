import assert from "node:assert/strict";
import test from "node:test";

import { DOUBLE_TAP_WINDOW_MS, TAP_SEEK_SECONDS, resolveTap } from "../lib/player/tap.ts";

test("第一下永远是控制层开关，不为了等双击而延迟", () => {
  assert.deepEqual(resolveTap({ nowMs: 1_000, lastTapMs: null, xRatio: 0.1 }), { type: "chrome" });
});

test("窗口内的第二下：左三分之一后退、右三分之一前进", () => {
  const now = 1_200;
  assert.deepEqual(resolveTap({ nowMs: now, lastTapMs: 1_000, xRatio: 0.1 }), {
    type: "seek",
    seconds: -TAP_SEEK_SECONDS,
  });
  assert.deepEqual(resolveTap({ nowMs: now, lastTapMs: 1_000, xRatio: 0.9 }), {
    type: "seek",
    seconds: TAP_SEEK_SECONDS,
  });
});

test("中间那条留给控制层：整块画面都能双击跳转会让想开控制条的人莫名跳走", () => {
  assert.deepEqual(resolveTap({ nowMs: 1_200, lastTapMs: 1_000, xRatio: 0.5 }), {
    type: "chrome",
  });
});

test("超出时间窗就是两次独立的单击", () => {
  assert.deepEqual(
    resolveTap({ nowMs: 1_000 + DOUBLE_TAP_WINDOW_MS + 1, lastTapMs: 1_000, xRatio: 0.1 }),
    { type: "chrome" },
  );
  // 边界上仍算双击
  assert.deepEqual(
    resolveTap({ nowMs: 1_000 + DOUBLE_TAP_WINDOW_MS, lastTapMs: 1_000, xRatio: 0.1 }),
    { type: "seek", seconds: -TAP_SEEK_SECONDS },
  );
});
