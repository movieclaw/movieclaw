import assert from "node:assert/strict";
import test from "node:test";

import {
  loadComposerPrefs,
  reconcileComposerPrefs,
  saveComposerPrefs,
} from "../lib/composer-prefs.ts";

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

const OPTIONS = [
  { ref: "kimi-k3", is_default: true, thinking_levels: ["low", "high", "max"] },
  { ref: "kimi-k2.6", is_default: false, thinking_levels: ["off"] },
  { ref: "deepseek-v4-pro", is_default: false, thinking_levels: ["low", "high", "max"] },
];

test("没存过、存坏了、没有 localStorage 都读回「默认 / 默认」", () => {
  let restore = withStorage({});
  assert.deepEqual(loadComposerPrefs(), { model: null, thinking: null });
  restore();
  restore = withStorage({ "movieclaw.composer.choice": "not json" });
  assert.deepEqual(loadComposerPrefs(), { model: null, thinking: null });
  restore();
  restore = withStorage({ "movieclaw.composer.choice": '["kimi-k3"]' });
  assert.deepEqual(loadComposerPrefs(), { model: null, thinking: null });
  restore();
  // 没有 window 也不能抛
  assert.deepEqual(loadComposerPrefs(), { model: null, thinking: null });
});

test("选择一改就写入，读回原样；两项都回默认时清掉键", () => {
  const store = {};
  const restore = withStorage(store);
  saveComposerPrefs({ model: "kimi-k2.6", thinking: "off" });
  assert.deepEqual(loadComposerPrefs(), { model: "kimi-k2.6", thinking: "off" });
  saveComposerPrefs({ model: null, thinking: "high" });
  assert.deepEqual(loadComposerPrefs(), { model: null, thinking: "high" });
  saveComposerPrefs({ model: null, thinking: null });
  assert.equal("movieclaw.composer.choice" in store, false);
  restore();
});

test("清单未加载时原样返回，不能把记忆误判为失效", () => {
  const prefs = { model: "kimi-k2.6", thinking: "off" };
  assert.equal(reconcileComposerPrefs(prefs, []), prefs);
});

test("记的模型已不在清单：模型与档位一起丢弃", () => {
  assert.deepEqual(
    reconcileComposerPrefs({ model: "gone-model", thinking: "high" }, OPTIONS),
    { model: null, thinking: null },
  );
});

test("档位不在生效模型的菜单里：只丢档位", () => {
  // 记的是 kimi-k2.6（只能关），却存着「high」
  assert.deepEqual(
    reconcileComposerPrefs({ model: "kimi-k2.6", thinking: "high" }, OPTIONS),
    { model: "kimi-k2.6", thinking: null },
  );
  // 没记模型 = 默认模型 kimi-k3，「off」不在它菜单里
  assert.deepEqual(reconcileComposerPrefs({ model: null, thinking: "off" }, OPTIONS), {
    model: null,
    thinking: null,
  });
});

test("记忆仍然有效时原对象返回（不触发多余的状态更新）", () => {
  const a = { model: "kimi-k2.6", thinking: "off" };
  assert.equal(reconcileComposerPrefs(a, OPTIONS), a);
  const b = { model: null, thinking: "high" };
  assert.equal(reconcileComposerPrefs(b, OPTIONS), b);
});
