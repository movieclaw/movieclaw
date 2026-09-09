import assert from "node:assert/strict";
import test from "node:test";

import { createSessionSnapshots } from "../lib/session-snapshot.ts";

test("存进去的快照按同一个 key 取得回来，没存过的是 undefined", () => {
  const snapshots = createSessionSnapshots(3);
  snapshots.set("a", { items: [1, 2] });
  assert.deepEqual(snapshots.get("a"), { items: [1, 2] });
  assert.equal(snapshots.get("b"), undefined);
});

test("同一个 key 是覆盖，不是新增一条", () => {
  const snapshots = createSessionSnapshots(3);
  snapshots.set("a", 1);
  snapshots.set("a", 2);
  assert.equal(snapshots.get("a"), 2);
  assert.equal(snapshots.size, 1);
});

test("超限时淘汰最久没写过的那一条", () => {
  const snapshots = createSessionSnapshots(2);
  snapshots.set("a", 1);
  snapshots.set("b", 2);
  snapshots.set("c", 3);
  assert.equal(snapshots.size, 2);
  assert.equal(snapshots.get("a"), undefined);
  assert.equal(snapshots.get("b"), 2);
  assert.equal(snapshots.get("c"), 3);
});

test("重新写入算「刚用过」，下一次淘汰轮不到它", () => {
  const snapshots = createSessionSnapshots(2);
  snapshots.set("a", 1);
  snapshots.set("b", 2);
  snapshots.set("a", 11); // a 回到队尾，队首变成 b
  snapshots.set("c", 3);
  assert.equal(snapshots.get("a"), 11);
  assert.equal(snapshots.get("b"), undefined);
});

test("读取不改变淘汰顺序：只有写入算「用过」", () => {
  // 墙每次滚动都会写快照，读只发生在返回那一刻——按写入排序足够，也更好预测
  const snapshots = createSessionSnapshots(2);
  snapshots.set("a", 1);
  snapshots.set("b", 2);
  snapshots.get("a");
  snapshots.set("c", 3);
  assert.equal(snapshots.get("a"), undefined);
});

test("上限为 1 时永远只留最后写入的那一条", () => {
  const snapshots = createSessionSnapshots(1);
  snapshots.set("a", 1);
  snapshots.set("b", 2);
  assert.equal(snapshots.size, 1);
  assert.equal(snapshots.get("b"), 2);
});
