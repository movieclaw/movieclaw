import assert from "node:assert/strict";
import test from "node:test";

import { applyNavOrder, mergeNavOrder, sameNavOrder } from "../lib/sidebar-nav.ts";

const DEFAULT_ITEMS = [
  { id: "new" },
  { id: "library" },
  { id: "explore-movies" },
  { id: "explore-tv" },
  { id: "subscriptions" },
];

const ids = (items) => items.map((item) => item.id);

test("没排过顺序时保持内置默认顺序", () => {
  assert.deepEqual(ids(applyNavOrder(DEFAULT_ITEMS, [])), [
    "new",
    "library",
    "explore-movies",
    "explore-tv",
    "subscriptions",
  ]);
});

test("排过的项按保存顺序在前", () => {
  const order = ["subscriptions", "library", "new", "explore-movies", "explore-tv"];
  assert.deepEqual(ids(applyNavOrder(DEFAULT_ITEMS, order)), order);
});

test("版本升级新增的导航入口追加在后，不会消失", () => {
  // 老用户存的顺序里没有 explore-tv（假设它是新版本才加的）
  const order = ["subscriptions", "new", "library", "explore-movies"];
  assert.deepEqual(ids(applyNavOrder(DEFAULT_ITEMS, order)), [
    "subscriptions",
    "new",
    "library",
    "explore-movies",
    "explore-tv",
  ]);
});

test("多个新增入口之间保持内置默认的相对顺序", () => {
  assert.deepEqual(ids(applyNavOrder(DEFAULT_ITEMS, ["subscriptions"])), [
    "subscriptions",
    "new",
    "library",
    "explore-movies",
    "explore-tv",
  ]);
});

test("已删除或当前不可见的 id 直接忽略", () => {
  // 成员看不到「新会话」：可见项里没有它，顺序里有也不该排出来
  const memberItems = DEFAULT_ITEMS.filter((item) => item.id !== "new");
  const order = ["new", "subscriptions", "library", "gone-in-v2", "explore-movies", "explore-tv"];
  assert.deepEqual(ids(applyNavOrder(memberItems, order)), [
    "subscriptions",
    "library",
    "explore-movies",
    "explore-tv",
  ]);
});

test("重复 id 只生效一次", () => {
  const order = ["library", "library", "new"];
  assert.deepEqual(ids(applyNavOrder(DEFAULT_ITEMS, order)), [
    "library",
    "new",
    "explore-movies",
    "explore-tv",
    "subscriptions",
  ]);
});

test("保存时保留当前不可见项的 id", () => {
  const visible = ["subscriptions", "library", "explore-movies", "explore-tv"];
  assert.deepEqual(mergeNavOrder(visible, ["new", "library", "explore-movies"]), [
    ...visible,
    "new",
  ]);
});

test("保存不会把可见项写成两条", () => {
  const visible = ["library", "new"];
  assert.deepEqual(mergeNavOrder(visible, ["new", "library"]), ["library", "new"]);
});

test("顺序比较逐位相等才算没改动", () => {
  assert.equal(sameNavOrder(["a", "b"], ["a", "b"]), true);
  assert.equal(sameNavOrder(["a", "b"], ["b", "a"]), false);
  assert.equal(sameNavOrder(["a"], ["a", "b"]), false);
});
