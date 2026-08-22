import assert from "node:assert/strict";
import test from "node:test";

import {
  enterLandscape,
  enterNativeVideoFullscreen,
  exitLandscape,
} from "../lib/player/orientation.ts";

function target({ ok = true, prefixed = false, none = false } = {}) {
  const calls = [];
  const impl = () => {
    calls.push("fullscreen");
    return ok ? Promise.resolve() : Promise.reject(new Error("denied"));
  };
  const t = { calls };
  if (none) return t;
  if (prefixed) t.webkitRequestFullscreen = impl;
  else t.requestFullscreen = impl;
  return t;
}

function orientation({ ok = true, none = false, unlockThrows = false } = {}) {
  const calls = [];
  const o = { calls };
  if (none) return o;
  o.lock = (value) => {
    calls.push(`lock:${value}`);
    return ok ? Promise.resolve() : Promise.reject(new Error("not supported"));
  };
  o.unlock = () => {
    calls.push("unlock");
    if (unlockThrows) throw new Error("boom");
  };
  return o;
}

test("手机上：先全屏再锁横向", async () => {
  const t = target();
  const o = orientation();
  assert.equal(await enterLandscape(t, o), "locked");
  assert.deepEqual(t.calls, ["fullscreen"]);
  // 顺序反了必然失败，所以要断言锁发生在全屏之后
  assert.deepEqual(o.calls, ["lock:landscape"]);
});

test("桌面上：方向锁一调就抛，但全屏已经拿到，不回滚", async () => {
  const t = target();
  assert.equal(await enterLandscape(t, orientation({ ok: false })), "fullscreen");
  assert.deepEqual(t.calls, ["fullscreen"]);
});

test("完全没有方向锁 API 时也算达成", async () => {
  assert.equal(await enterLandscape(target(), orientation({ none: true })), "fullscreen");
  assert.equal(await enterLandscape(target(), undefined), "fullscreen");
});

test("只有带前缀的全屏方法也能用", async () => {
  const t = target({ prefixed: true });
  assert.equal(await enterLandscape(t, orientation()), "locked");
  assert.deepEqual(t.calls, ["fullscreen"]);
});

test("全屏被拒（不在手势里 / 用户拒绝）不再去锁方向", async () => {
  const o = orientation();
  assert.equal(await enterLandscape(target({ ok: false }), o), "unsupported");
  assert.deepEqual(o.calls, []);
});

test("元素级全屏 API 不存在（iPhone Safari）报 unsupported，交给原生兜底", async () => {
  const o = orientation();
  assert.equal(await enterLandscape(target({ none: true }), o), "unsupported");
  assert.deepEqual(o.calls, []);
});

test("原生视频全屏兜底：有方法才算兜住", () => {
  let called = false;
  assert.equal(enterNativeVideoFullscreen({ webkitEnterFullscreen: () => (called = true) }), true);
  assert.equal(called, true);
  assert.equal(enterNativeVideoFullscreen({}), false);
  assert.equal(enterNativeVideoFullscreen(null), false);
});

test("退出横屏：先解锁再退全屏", async () => {
  const o = orientation();
  const order = [];
  o.unlock = () => order.push("unlock");
  await exitLandscape(o, async () => {
    order.push("exitFullscreen");
  });
  // 反过来的话解锁会在部分浏览器上抛，方向锁留着，退出播放器后整页还是横的
  assert.deepEqual(order, ["unlock", "exitFullscreen"]);
});

test("解锁抛异常不能挡住退全屏", async () => {
  let exited = false;
  await exitLandscape(orientation({ unlockThrows: true }), async () => {
    exited = true;
  });
  assert.equal(exited, true);
});

test("退全屏抛异常（已经不在全屏里）不冒泡", async () => {
  await exitLandscape(orientation(), async () => {
    throw new Error("not in fullscreen");
  });
});

test("没有可退的全屏时只解锁", async () => {
  const o = orientation();
  await exitLandscape(o, null);
  assert.deepEqual(o.calls, ["unlock"]);
});

test("完全没有方向锁时退出也不炸", async () => {
  await exitLandscape(undefined, null);
  await exitLandscape(orientation({ none: true }), null);
});
