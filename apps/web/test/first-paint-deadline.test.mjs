import assert from "node:assert/strict";
import test from "node:test";

import { withDeadline } from "../lib/first-paint-deadline.ts";

const after = (ms, value) => new Promise((resolve) => setTimeout(() => resolve(value), ms));
const failAfter = (ms) =>
  new Promise((_, reject) => setTimeout(() => reject(new Error("boom")), ms));

test("预算内回来：直接用，没有晚到的那份", async () => {
  const out = await withDeadline(after(5, ["a"]), 200, null);
  assert.deepEqual(out, { value: ["a"], late: null });
});

test("超时：先给兜底值，真正的结果晚到时从 late 拿", async () => {
  const out = await withDeadline(after(60, ["a"]), 10, null);
  assert.equal(out.value, null);
  assert.ok(out.late);
  assert.deepEqual(await out.late, ["a"]);
});

test("预算内失败：退回兜底值，不 reject", async () => {
  const out = await withDeadline(failAfter(5), 200, []);
  assert.deepEqual(out, { value: [], late: null });
});

test("超时后失败：late 也归成兜底值，永不 reject", async () => {
  const out = await withDeadline(failAfter(60), 10, []);
  assert.equal(out.late !== null, true);
  assert.deepEqual(await out.late, []);
});

test("不限预算：等到结果为止，失败归兜底", async () => {
  assert.deepEqual(await withDeadline(after(30, 7), null, 0), { value: 7, late: null });
  assert.deepEqual(await withDeadline(failAfter(5), null, 0), { value: 0, late: null });
});
