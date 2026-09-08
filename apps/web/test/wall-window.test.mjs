import assert from "node:assert/strict";
import test from "node:test";

import {
  activeInitialAt,
  layoutPosterGrid,
  lowerBoundY,
  posterGridColumns,
  tileWindowRange,
} from "../lib/wall-window.ts";

/** 两列、每块高 100、间距 0 的一面墙：y 序列 0,0,100,100,200,200,… */
function evenWall(rows) {
  const placements = [];
  for (let r = 0; r < rows; r += 1) {
    placements.push({ y: r * 100, height: 100 }, { y: r * 100, height: 100 });
  }
  return placements;
}

test("二分给出第一个 y 不小于目标值的下标", () => {
  const wall = evenWall(3); // y: 0,0,100,100,200,200
  assert.equal(lowerBoundY(wall, -50), 0);
  assert.equal(lowerBoundY(wall, 0), 0);
  assert.equal(lowerBoundY(wall, 1), 2);
  assert.equal(lowerBoundY(wall, 100), 2);
  assert.equal(lowerBoundY(wall, 201), 6);
  assert.equal(lowerBoundY([], 0), 0);
});

test("段顶就在视口顶边时，只挂视口 + 提前量之内的行", () => {
  const wall = evenWall(50); // 5000px 高
  // 视口 800、提前量 0、最高块 100：期望覆盖 y ∈ [-100, 800)
  const [from, to] = tileWindowRange(wall, 0, 800, 0, 100);
  assert.equal(from, 0);
  assert.equal(to, 16); // 前 8 行（y 0..700）× 2 列
});

test("滑到中段时窗口跟着走，且上沿多退一块最高瓦片", () => {
  const wall = evenWall(50);
  // 段顶已滑到视口上方 2000px
  const [from, to] = tileWindowRange(wall, -2000, 800, 400, 100);
  // 下沿：y < 2000 + 800 + 400 = 3200 → 前 32 行
  assert.equal(to, 64);
  // 上沿：带子上沿在 1600，退一块最高瓦片到 1500 → 第 15 行起
  assert.equal(from, 30);
  // 窗口的第一块必须已经够到带子上沿，否则视口顶部会缺一格
  assert.ok(wall[from].y + wall[from].height >= 1600);
});

test("跨在上沿的高瓦片不会被漏掉", () => {
  // 一块 300 高的长图，顶在 y=1000；带子上沿在 1200——它仍然可见
  const wall = [
    { y: 0, height: 100 },
    { y: 1000, height: 300 },
    { y: 1300, height: 100 },
  ];
  const [from] = tileWindowRange(wall, -1200, 800, 0, 300);
  assert.equal(from, 1);
});

test("整段都在视口下方时窗口为空区间", () => {
  const wall = evenWall(10);
  const [from, to] = tileWindowRange(wall, 5000, 800, 400, 100);
  assert.equal(from, 0);
  assert.equal(to, 0);
});

test("整段都在视口上方时窗口落在末尾", () => {
  const wall = evenWall(10); // 1000px 高
  const [from, to] = tileWindowRange(wall, -5000, 800, 400, 100);
  assert.equal(from, 20);
  assert.equal(to, 20);
});

test("稀疏行（所有 y 都是 0）整段一起挂", () => {
  const row = [
    { y: 0, height: 200 },
    { y: 0, height: 200 },
  ];
  assert.deepEqual(Array.from(tileWindowRange(row, 0, 800, 400, 200)), [0, 2]);
});

test("空段不返回任何瓦片", () => {
  assert.deepEqual(Array.from(tileWindowRange([], 0, 800, 400, 0)), [0, 0]);
});

/* —— 海报墙的网格几何 —— */

/** 列宽 148、间距 16/28、基准格高 300：与库页海报墙同一口径 */
const SPEC = { minColumn: 148, gapX: 16, gapY: 28, cellHeight: 300 };

test("列数与 CSS auto-fill minmax 同一口径", () => {
  // (宽 + gapX) / (列宽 + gapX) 向下取整
  assert.equal(posterGridColumns(414, SPEC), 2); // 手机竖屏
  assert.equal(posterGridColumns(1200, SPEC), 7);
  assert.equal(posterGridColumns(164, SPEC), 1); // 正好一列
  assert.equal(posterGridColumns(100, SPEC), 1); // 再窄也至少一列
});

test("格子按行铺开，列宽把整行填满", () => {
  const { placements, columns, height } = layoutPosterGrid(5, 414, SPEC);
  assert.equal(columns, 2);
  const width = (414 - 16) / 2;
  assert.equal(placements[0].x, 0);
  assert.equal(placements[1].x, width + 16);
  assert.equal(placements[0].width, width);
  // 第二行换行回到 x=0，y 下移一个行高 + 行距
  assert.equal(placements[2].x, 0);
  assert.equal(placements[2].y, 328);
  assert.equal(placements[4].y, 656);
  // 三行：末行不带行距
  assert.equal(height, 300 * 3 + 28 * 2);
});

test("y 非降序——二分取窗口的前提", () => {
  const { placements } = layoutPosterGrid(50, 700, SPEC);
  for (let i = 1; i < placements.length; i += 1) {
    assert.ok(placements[i].y >= placements[i - 1].y, `第 ${i} 格的 y 变小了`);
  }
});

test("一行里有格子要加高，整行跟着高（与 CSS auto 行高同语义）", () => {
  // 第 3 格（第二行）带「文件缺失」提示，多 20px
  const extraOf = (i) => (i === 3 ? 20 : 0);
  const { placements, height } = layoutPosterGrid(6, 414, SPEC, extraOf);
  // 第一行不受影响
  assert.equal(placements[0].height, 300);
  assert.equal(placements[1].y, 0);
  // 第二行两格都高 320（行内等高，片名仍在一条线上）
  assert.equal(placements[2].height, 320);
  assert.equal(placements[3].height, 320);
  // 第三行的起点被推下去 20px
  assert.equal(placements[4].y, 328 + 320 + 28);
  assert.equal(height, 300 + 28 + 320 + 28 + 300);
});

test("空墙不占高度", () => {
  const { placements, height } = layoutPosterGrid(0, 414, SPEC);
  assert.equal(placements.length, 0);
  assert.equal(height, 0);
});

test("当前字母取最后一个已滑过的锚点", () => {
  const { rowTops, columns } = layoutPosterGrid(20, 414, SPEC); // 2 列，行高 328
  const anchors = [
    { initial: "A", index: 0 }, // 第 0 行，y=0
    { initial: "B", index: 4 }, // 第 2 行，y=656
    { initial: "C", index: 10 }, // 第 5 行，y=1640
  ];
  assert.equal(activeInitialAt(anchors, rowTops, columns, 0, null), "A");
  assert.equal(activeInitialAt(anchors, rowTops, columns, 700, null), "B");
  assert.equal(activeInitialAt(anchors, rowTops, columns, 1700, null), "C");
  // 还没滑到首个锚点时保留兜底档
  assert.equal(activeInitialAt(anchors.slice(1), rowTops, columns, 0, "M"), "M");
});
