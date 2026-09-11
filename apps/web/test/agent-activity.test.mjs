import assert from "node:assert/strict";
import test from "node:test";

import { applyTurns } from "../lib/agent-activity.ts";

/** 一个最小可用的会话壳（字段取 AgentConversation 的必填部分）。 */
function conversation(overrides = {}) {
  return {
    id: "s1",
    title: "会话",
    updatedAt: 1000,
    running: true,
    turns: [],
    loaded: true,
    ...overrides,
  };
}

const turn = (status) => ({ id: "t1", input: "问", status, segments: [], startedAt: 0 });

test("流式产出不刷新活跃时间：多个会话同时生成时列表不会互相换位", () => {
  const before = conversation({ running: true, turns: [turn("running")] });
  // 模拟 80ms 一次的事件合并：轮次内容在变，但运行状态没翻转
  let current = before;
  for (const now of [2000, 2080, 2160]) {
    current = applyTurns(current, [turn("running")], now);
    assert.equal(current.updatedAt, 1000, "流式期间排序键必须保持不变");
  }
  assert.equal(current.running, true);
});

test("轮次落终态刷新一次活跃时间（与服务端 finish_run 同口径）", () => {
  const before = conversation({ running: true, turns: [turn("running")] });
  const after = applyTurns(before, [turn("done")], 9999);
  assert.equal(after.updatedAt, 9999);
  assert.equal(after.running, false);
});

test("出错同样是终态，一样刷新", () => {
  const before = conversation({ running: true, turns: [turn("running")] });
  const after = applyTurns(before, [turn("error")], 9999);
  assert.equal(after.updatedAt, 9999);
  assert.equal(after.running, false);
});

test("已结束的会话被重新跑起来时刷新活跃时间", () => {
  const before = conversation({ running: false, turns: [turn("done")] });
  const after = applyTurns(before, [turn("running")], 9999);
  assert.equal(after.updatedAt, 9999);
  assert.equal(after.running, true);
});

test("轮次内容更新但运行状态没变（如回填 messageId）不动排序", () => {
  const before = conversation({ running: false, turns: [turn("done")] });
  const after = applyTurns(before, [{ ...turn("done"), messageId: "m1" }], 9999);
  assert.equal(after.updatedAt, 1000);
  assert.equal(after.turns[0].messageId, "m1");
});
