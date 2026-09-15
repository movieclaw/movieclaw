import assert from "node:assert/strict";
import test from "node:test";

import {
  THINKING_LEVEL_ORDER,
  nearestStopIndex,
  steppedStopIndex,
  stopIndexAtPointer,
  stopPercent,
  thinkingControlShape,
  thinkingListItems,
  thinkingStops,
  thinkingValueLabel,
} from "../lib/thinking-level-control.ts";

// 以下菜单取自预设目录（服务端 ModelInfo.thinking_levels 的真实输出）：
const KIMI_K2_6 = ["off"]; // kimi 官方 k2.6 / glm-5.2、5.1、5：toggle 方言，只有「关」
const KIMI_K3 = ["low", "high", "max"]; // effort 三档，不可关
const QWEN = ["off", "low", "medium", "high"]; // budget 分段 + 可关
const GPT = ["off", "low", "medium", "high", "xhigh", "max"]; // effort 全档 + 可关

test("菜单按统一词汇表归一排序，词汇表外的值丢弃", () => {
  assert.deepEqual(thinkingStops(["max", "low", "high"]), ["low", "high", "max"]);
  assert.deepEqual(thinkingStops(["high", "ultra", "off"]), ["off", "high"]);
  assert.deepEqual(thinkingStops([]), []);
  // 顺序表本身就是"浅 → 深"，off 在最左
  assert.equal(THINKING_LEVEL_ORDER[0], "off");
});

test("只有开关的模型（kimi-k2.6 / glm-5.x）不画滑杆，用两格分段", () => {
  // 曾经的 bug：单刻度滑杆没有可拖的距离，点了「关」再点还是「关」，回不到默认
  assert.equal(thinkingControlShape(thinkingStops(KIMI_K2_6)), "toggle");
  assert.equal(thinkingControlShape(thinkingStops(KIMI_K3)), "slider");
  assert.equal(thinkingControlShape(thinkingStops(QWEN)), "slider");
  assert.equal(thinkingControlShape(thinkingStops(GPT)), "slider");
  assert.equal(thinkingControlShape([]), "hidden");
});

test("刻度均布：两端 0/100，中间等分；单刻度居中", () => {
  assert.equal(stopPercent(0, 3), 0);
  assert.equal(stopPercent(1, 3), 50);
  assert.equal(stopPercent(2, 3), 100);
  assert.equal(stopPercent(0, 1), 50);
  assert.deepEqual(
    GPT.map((_, i) => stopPercent(i, GPT.length)),
    [0, 20, 40, 60, 80, 100],
  );
});

test("轨道位置吸附到最近刻度，越界钳到两端", () => {
  // kimi-k3 三档：刻度在 0 / 0.5 / 1，四分位是分界
  assert.equal(nearestStopIndex(0, 3), 0);
  assert.equal(nearestStopIndex(0.2, 3), 0);
  assert.equal(nearestStopIndex(0.3, 3), 1);
  assert.equal(nearestStopIndex(0.74, 3), 1);
  assert.equal(nearestStopIndex(0.76, 3), 2);
  assert.equal(nearestStopIndex(1, 3), 2);
  // 拖出轨道外不会得到不存在的下标
  assert.equal(nearestStopIndex(-0.4, 3), 0);
  assert.equal(nearestStopIndex(1.7, 3), 2);
  assert.equal(nearestStopIndex(0.99, 1), 0);
});

test("指针横坐标换算刻度：扣掉两端留白，落在留白上按端点算", () => {
  // 轨道 left=100 宽 288（18rem 弹层减内边距），两端各留 16px 给刻度圆心：
  // 可用长度 256，GPT 六档刻度圆心在 116 / 167.2 / 218.4 / 269.6 / 320.8 / 372
  const at = (x) => stopIndexAtPointer(x, 100, 288, GPT.length, 16);
  assert.equal(at(116), 0);
  assert.equal(at(372), 5);
  assert.equal(at(218), 2);
  assert.equal(at(244), 3); // 218.4 与 269.6 的中点右侧
  // 留白与轨道外：按最近端点
  assert.equal(at(100), 0);
  assert.equal(at(388), 5);
  assert.equal(at(-50), 0);
  assert.equal(at(9999), 5);
  // 拖动是同一函数连续调用：从左到右单调不减，不会跳档回退
  let prev = -1;
  for (let x = 100; x <= 388; x += 1) {
    const i = at(x);
    assert.ok(i >= prev, `x=${x} 得到 ${i}，小于前一像素的 ${prev}`);
    prev = i;
  }
  // 轨道退化到没有可用长度时不除零
  assert.equal(stopIndexAtPointer(50, 0, 20, 3, 16), 0);
});

test("键盘步进：默认态先落到最浅档，两端不越界，Home/End 直达", () => {
  const n = KIMI_K3.length;
  assert.equal(steppedStopIndex(-1, n, "ArrowRight"), 0);
  assert.equal(steppedStopIndex(-1, n, "ArrowLeft"), 0);
  assert.equal(steppedStopIndex(0, n, "ArrowRight"), 1);
  assert.equal(steppedStopIndex(2, n, "ArrowRight"), 2);
  assert.equal(steppedStopIndex(0, n, "ArrowLeft"), 0);
  assert.equal(steppedStopIndex(1, n, "ArrowDown"), 0);
  assert.equal(steppedStopIndex(0, n, "End"), 2);
  assert.equal(steppedStopIndex(2, n, "Home"), 0);
  assert.equal(steppedStopIndex(1, n, "Enter"), null);
  assert.equal(steppedStopIndex(-1, 0, "ArrowRight"), null);
});

test("只能关的模型：两格写成「开启（模型默认）/ 关闭」，各带一句说明", () => {
  const items = thinkingListItems(thinkingStops(KIMI_K2_6));
  assert.deepEqual(
    items.map((i) => [i.level, i.label]),
    [
      [null, "开启（模型默认）"],
      ["off", "关闭"],
    ],
  );
  // 两项都要说清到底发不发参数、后果是什么，不能只有一个词
  for (const item of items) assert.ok(item.description.length >= 8, item.label);
  assert.match(items[0].description, /不发送/);
  assert.match(items[1].description, /关闭/);
});

test("单档但不是「关」的罕见声明退回通用「默认 / 该档」；滑杆标题显示档位本身", () => {
  const items = thinkingListItems(["max"]);
  assert.deepEqual(
    items.map((i) => [i.level, i.label]),
    [
      [null, "默认"],
      ["max", "最高"],
    ],
  );
  // 「默认」不是强度轴上的一点，滑杆无滑块时标题就写「默认」
  assert.equal(thinkingValueLabel(null), "默认");
  assert.equal(thinkingValueLabel("high"), "高");
  assert.equal(thinkingValueLabel("off"), "关");
});
