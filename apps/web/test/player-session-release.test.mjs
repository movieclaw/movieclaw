import assert from "node:assert/strict";
import test from "node:test";

import { createSessionReleaser } from "../lib/player/session-release.ts";

/** 手动调度器：让「下一拍」在测试里可控。 */
function manual() {
  const queue = [];
  return {
    schedule: (task) => queue.push(task),
    flush: () => {
      const tasks = queue.splice(0, queue.length);
      for (const task of tasks) task();
    },
  };
}

test("离开播放器会停掉会话", () => {
  const stopped = [];
  const clock = manual();
  const releaser = createSessionReleaser((id) => stopped.push(id), clock.schedule);

  releaser.acquire("S1");
  releaser.release("S1");
  assert.deepEqual(stopped, [], "不该同步停止——要留出 StrictMode 重挂的窗口");
  clock.flush();
  assert.deepEqual(stopped, ["S1"]);
});

test("StrictMode 的 mount→cleanup→mount 不该停掉会话", () => {
  const stopped = [];
  const clock = manual();
  const releaser = createSessionReleaser((id) => stopped.push(id), clock.schedule);

  // React StrictMode：effect 跑一遍、cleanup 一遍、再跑一遍，全在同一提交内
  releaser.acquire("S1");
  releaser.release("S1");
  releaser.acquire("S1");
  clock.flush();

  assert.deepEqual(stopped, [], "会话被重新挂上了，停它就等于把刚起的会话掐了");
});

test("切下一集：旧会话停、新会话留", () => {
  const stopped = [];
  const clock = manual();
  const releaser = createSessionReleaser((id) => stopped.push(id), clock.schedule);

  releaser.acquire("S1");
  releaser.release("S1");
  releaser.acquire("S2");
  clock.flush();

  assert.deepEqual(stopped, ["S1"], "只该停旧的那个");
});

test("同一会话重复 release 只停一次是调用方的事，这里如实各排一次", () => {
  const stopped = [];
  const clock = manual();
  const releaser = createSessionReleaser((id) => stopped.push(id), clock.schedule);

  releaser.acquire("S1");
  releaser.release("S1");
  releaser.release("S1");
  clock.flush();

  // 服务端对已结束会话返回 404，调用方一律吞掉，因此这里不做去重，
  // 保持行为可预测：预约几次就执行几次。
  assert.deepEqual(stopped, ["S1", "S1"]);
});

test("从未 acquire 过也能安全 release", () => {
  const stopped = [];
  const clock = manual();
  const releaser = createSessionReleaser((id) => stopped.push(id), clock.schedule);

  releaser.release("ghost");
  clock.flush();
  assert.deepEqual(stopped, ["ghost"]);
});

test("默认调度器是异步的", async () => {
  const stopped = [];
  const releaser = createSessionReleaser((id) => stopped.push(id));
  releaser.acquire("S1");
  releaser.release("S1");
  assert.deepEqual(stopped, []);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(stopped, ["S1"]);
});
