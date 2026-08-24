import assert from "node:assert/strict";
import test from "node:test";

import {
  QUALITY_OPTIONS,
  loadQualityPreference,
  qualityLabel,
  saveQualityPreference,
} from "../lib/player/quality.ts";

/** node 环境没有 window：模拟一个内存 localStorage。 */
function withStorage(store) {
  globalThis.window = {
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => {
        store[k] = String(v);
      },
      removeItem: (k) => {
        delete store[k];
      },
    },
  };
  return () => {
    delete globalThis.window;
  };
}

test("默认自动：没存过时读回 null", () => {
  const restore = withStorage({});
  assert.equal(loadQualityPreference(), null);
  restore();
});

test("存取往返一致；自动（null）等于清掉存储", () => {
  const store = {};
  const restore = withStorage(store);
  saveQualityPreference(720);
  assert.equal(loadQualityPreference(), 720);
  saveQualityPreference(null);
  assert.equal(Object.keys(store).length, 0);
  assert.equal(loadQualityPreference(), null);
  restore();
});

test("存了阶梯之外的脏值回退自动", () => {
  const restore = withStorage({ "movieclaw.player.quality": "999" });
  assert.equal(loadQualityPreference(), null);
  restore();
});

test("没有 window（SSR）不抛错，回自动", () => {
  assert.equal(loadQualityPreference(), null);
});

test("选项含自动且高度全部在码率阶梯语义内", () => {
  assert.equal(QUALITY_OPTIONS[0].maxHeight, null);
  for (const option of QUALITY_OPTIONS.slice(1)) {
    assert.equal(typeof option.maxHeight, "number");
  }
  assert.equal(qualityLabel(null), "自动");
  assert.equal(qualityLabel(720), "720p");
});
