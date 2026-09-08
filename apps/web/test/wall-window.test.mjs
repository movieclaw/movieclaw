import assert from "node:assert/strict";
import test from "node:test";

import { lowerBoundY, tileWindowRange } from "../lib/wall-window.ts";

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
