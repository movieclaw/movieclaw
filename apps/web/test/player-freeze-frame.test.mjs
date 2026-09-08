import assert from "node:assert/strict";
import test from "node:test";

import { canReleaseFreeze } from "../lib/player/freeze-frame.ts";

test("新位置那一帧解出来了才撤：readyState >= HAVE_CURRENT_DATA", () => {
  assert.equal(canReleaseFreeze({ seeking: false, readyState: 2 }), true);
  assert.equal(canReleaseFreeze({ seeking: false, readyState: 4 }), true);
  // HAVE_METADATA：只知道时长和尺寸，还没有可画的一帧，撤了就露出黑屏
  assert.equal(canReleaseFreeze({ seeking: false, readyState: 1 }), false);
  assert.equal(canReleaseFreeze({ seeking: false, readyState: 0 }), false);
});

test("跳转途中不撤：readyState 可能还留着旧值，撤了会闪一下黑", () => {
  assert.equal(canReleaseFreeze({ seeking: true, readyState: 4 }), false);
});
