import assert from "node:assert/strict";
import test from "node:test";

import { backBufferSeconds } from "../lib/player/buffer-budget.ts";

test("常见码率下尽量多留：回拖三分钟之内都该是瞬间的", () => {
  // 4Mbps 的 1080p：预算之内放得下 180 秒
  assert.equal(backBufferSeconds(4_000_000), 180);
  assert.equal(backBufferSeconds(1_500_000), 180);
});

test("高码率片子自动收回下限，不然「秒」这个单位会把内存吃穿", () => {
  // 档 1 原样封装的 4K 蓝光原盘 80Mbps：180 秒就是 1.8GB，标签页必崩
  assert.equal(backBufferSeconds(80_000_000), 30);
  // 20Mbps 落在中间：96MB 的预算按码率反推大约 40 秒
  assert.equal(backBufferSeconds(20_000_000), 40);
});

test("码率未知先按上限给，等真实码率上报再收紧", () => {
  assert.equal(backBufferSeconds(null), 180);
  assert.equal(backBufferSeconds(0), 180);
  assert.equal(backBufferSeconds(undefined), 180);
});
