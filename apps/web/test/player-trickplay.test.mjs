import assert from "node:assert/strict";
import test from "node:test";

import { tileAt } from "../lib/player/trickplay.ts";

const index = {
  ready: true,
  interval_ms: 10_000,
  tile_width: 320,
  tile_height: 180,
  columns: 10,
  rows: 10,
  count: 150,
  sheets: ["/s1.jpg?token=t", "/s2.jpg?token=t"],
};

test("第一格在雪碧图左上角", () => {
  const tile = tileAt(index, 0);
  assert.deepEqual(tile, {
    url: "/s1.jpg?token=t",
    width: 320,
    height: 180,
    offsetX: -0,
    offsetY: -0,
  });
});

test("同一行往右走", () => {
  assert.equal(tileAt(index, 25_000).offsetX, -640); // 第 2 格（0-based）
  assert.equal(tileAt(index, 25_000).offsetY, -0);
});

test("满一行换到下一行", () => {
  const tile = tileAt(index, 100_000); // 第 10 格 → 第二行第一列
  assert.equal(tile.offsetX, -0);
  assert.equal(tile.offsetY, -180);
});

test("满一张图换到下一张", () => {
  // 第 100 格（1000 秒）落在第二张雪碧图的左上角
  const tile = tileAt(index, 1_000_000);
  assert.equal(tile.url, "/s2.jpg?token=t");
  assert.equal(tile.offsetX, -0);
  assert.equal(tile.offsetY, -0);
});

test("越界夹到最后一格，而不是忽然没有预览", () => {
  const tile = tileAt(index, 99_999_999);
  assert.equal(tile.url, "/s2.jpg?token=t");
  // count=150 → 最后一格序号 149 → 第二张图里的第 49 格 → 第 5 行第 10 列
  assert.equal(tile.offsetX, -9 * 320);
  assert.equal(tile.offsetY, -4 * 180);
});

test("负数时间当作 0", () => {
  assert.deepEqual(tileAt(index, -5000), tileAt(index, 0));
});

test("索引没就绪就没有预览", () => {
  assert.equal(tileAt({ ...index, ready: false }, 0), null);
  assert.equal(tileAt(null, 0), null);
});

for (const [label, broken] of [
  ["没有雪碧图", { sheets: [] }],
  ["格数为零", { count: 0 }],
  ["间隔为零", { interval_ms: 0 }],
  ["列数为零", { columns: 0 }],
  ["行数为零", { rows: 0 }],
]) {
  test(`索引残缺（${label}）不算预览，也不该炸`, () => {
    assert.equal(tileAt({ ...index, ...broken }, 30_000), null);
  });
}
