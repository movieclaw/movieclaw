import assert from "node:assert/strict";
import test from "node:test";

import { canReleaseFreeze, drawTile } from "../lib/player/freeze-frame.ts";

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

/** 极简 canvas 替身：只记下 drawImage 的入参，用来钉住取源矩形的符号。 */
function fakeCanvas() {
  const calls = [];
  return {
    width: 0,
    height: 0,
    calls,
    getContext: () => ({ drawImage: (...args) => calls.push(args) }),
  };
}

test("缩略图取的是雪碧图里的那一格：offset 是 CSS 负值，取源矩形要取反", () => {
  const canvas = fakeCanvas();
  const image = { naturalWidth: 1600, naturalHeight: 900 };
  // 第 2 列第 1 行（tileAt 给出的是 background-position，故为负）
  const tile = { width: 320, height: 180, offsetX: -640, offsetY: -180 };
  assert.equal(drawTile(canvas, image, tile), true);
  assert.equal(canvas.width, 320);
  assert.equal(canvas.height, 180);
  // 源矩形 (640,180,320,180) → 画到 (0,0,320,180)
  assert.deepEqual(canvas.calls[0], [image, 640, 180, 320, 180, 0, 0, 320, 180]);
});

test("图还没解码好就不画：画上去是一格空白，比留着上一帧更糟", () => {
  const canvas = fakeCanvas();
  const tile = { width: 320, height: 180, offsetX: 0, offsetY: 0 };
  assert.equal(drawTile(canvas, { naturalWidth: 0, naturalHeight: 0 }, tile), false);
  assert.equal(canvas.calls.length, 0);
});
