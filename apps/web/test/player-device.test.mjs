import assert from "node:assert/strict";
import test from "node:test";

import { getPlayerDeviceId, resetPlayerDeviceIdForTests } from "../lib/player/device.ts";
import { loadActivityScope, saveActivityScope } from "../lib/activity-scope.ts";

/** node 环境没有 window：模拟一个内存 localStorage。 */
function withStorage(store, { broken = false } = {}) {
  globalThis.window = {
    localStorage: {
      getItem: (k) => {
        if (broken) throw new Error("SecurityError");
        return k in store ? store[k] : null;
      },
      setItem: (k, v) => {
        if (broken) throw new Error("SecurityError");
        store[k] = String(v);
      },
      removeItem: (k) => {
        delete store[k];
      },
    },
  };
  return () => {
    delete globalThis.window;
    resetPlayerDeviceIdForTests();
  };
}

test("设备标识首次生成后持久化，再次读取拿到同一个", () => {
  const store = {};
  const restore = withStorage(store);
  try {
    const first = getPlayerDeviceId();
    assert.match(first, /^[A-Za-z0-9_-]{8,64}$/);
    assert.equal(store["movieclaw.player.device-id"], first);
    resetPlayerDeviceIdForTests();
    assert.equal(getPlayerDeviceId(), first);
  } finally {
    restore();
  }
});

test("存储里被写坏的值不采用，重新生成", () => {
  const store = { "movieclaw.player.device-id": "bad value with spaces" };
  const restore = withStorage(store);
  try {
    const id = getPlayerDeviceId();
    assert.notEqual(id, "bad value with spaces");
    assert.equal(store["movieclaw.player.device-id"], id);
  } finally {
    restore();
  }
});

test("localStorage 不可用时本次会话内仍稳定", () => {
  const restore = withStorage({}, { broken: true });
  try {
    const id = getPlayerDeviceId();
    assert.equal(getPlayerDeviceId(), id);
  } finally {
    restore();
  }
});

test("活动页范围口径：默认按我的浏览范围，只记住「全部」", () => {
  const store = {};
  const restore = withStorage(store);
  try {
    assert.equal(loadActivityScope(), "visible");
    saveActivityScope("all");
    assert.equal(store["movieclaw.activity.scope"], "all");
    assert.equal(loadActivityScope(), "all");
    saveActivityScope("visible");
    assert.equal("movieclaw.activity.scope" in store, false);
    store["movieclaw.activity.scope"] = "garbage";
    assert.equal(loadActivityScope(), "visible");
  } finally {
    restore();
  }
});
