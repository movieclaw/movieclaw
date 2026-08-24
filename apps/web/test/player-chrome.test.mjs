import assert from "node:assert/strict";
import test from "node:test";

import { chromeMustStayVisible, shouldHideOnPointerLeave } from "../lib/player/chrome.ts";

const idle = { paused: false, menuOpen: false, awaitingUser: false };

test("正常播放中，控制条可以自动淡出", () => {
  assert.equal(chromeMustStayVisible(idle), false);
});

test("暂停时常显——用户正在找播放键", () => {
  assert.equal(chromeMustStayVisible({ ...idle, paused: true }), true);
});

test("菜单展开时常显——控制条一淡出会把菜单一起带走", () => {
  assert.equal(chromeMustStayVisible({ ...idle, menuOpen: true }), true);
});

test("诊断面板开着不影响控制条——它是独立常驻的一块读数，不该把控制条钉死", () => {
  assert.equal(chromeMustStayVisible({ ...idle, diagnosticsOpen: true }), false);
});

test("等用户拍板时常显——报错和同意弹窗不设超时", () => {
  // 播放失败后界面淡成一块黑屏，用户既不知道出了什么事，也不知道还能点什么。
  // 注意此时 paused 未必为真：解码失败的媒体元素可能既没在放也没触发 pause。
  assert.equal(chromeMustStayVisible({ ...idle, awaitingUser: true }), true);
});

// ---------------------------------------------------------------------------
// pointerleave：这一组就是那个线上 bug 的回归测试
// ---------------------------------------------------------------------------

test("鼠标移出播放器时收起控制条", () => {
  assert.equal(shouldHideOnPointerLeave({ pointerType: "mouse", paused: false }), true);
});

test("鼠标移出但已暂停：不收，暂停时本来就要常显", () => {
  assert.equal(shouldHideOnPointerLeave({ pointerType: "mouse", paused: true }), false);
});

test("触屏抬手绝不能收起控制条", () => {
  // 实测触屏点击的事件序列是 pointerdown → pointerup → pointerleave → click，
  // 也就是**每点一下都会触发 pointerleave**。当初直接接到「收起」上，表现就是
  // 手机上控制条闪一下全没了、而且永远停不住。
  assert.equal(shouldHideOnPointerLeave({ pointerType: "touch", paused: false }), false);
  assert.equal(shouldHideOnPointerLeave({ pointerType: "touch", paused: true }), false);
});

test("触控笔同理：抬笔不等于离开", () => {
  assert.equal(shouldHideOnPointerLeave({ pointerType: "pen", paused: false }), false);
});

test("认不出的 pointerType 一律不收——宁可多显示，也不能让控件消失", () => {
  assert.equal(shouldHideOnPointerLeave({ pointerType: "", paused: false }), false);
  assert.equal(shouldHideOnPointerLeave({ pointerType: "unknown", paused: false }), false);
});
