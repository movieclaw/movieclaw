import assert from "node:assert/strict";
import test from "node:test";

// isEditableTarget 用 `instanceof HTMLElement` 判类型，node 里没有 DOM。
// 补一个同名的最小类即可——instanceof 在调用时才查全局，动态 import 放在
// 赋值之后就能拿到它。
class HTMLElement {
  constructor(tagName, extra = {}) {
    this.tagName = tagName;
    this.isContentEditable = false;
    Object.assign(this, extra);
  }
}
globalThis.HTMLElement = HTMLElement;

const { resolveShortcut, isEditableTarget } = await import("../lib/player/shortcuts.ts");

test("输入框里按键一律不接管——用户在搜字幕、填备注", () => {
  assert.equal(isEditableTarget(new HTMLElement("INPUT", { type: "text" })), true);
  assert.equal(isEditableTarget(new HTMLElement("TEXTAREA")), true);
  assert.equal(isEditableTarget(new HTMLElement("SELECT")), true);
  assert.equal(isEditableTarget(new HTMLElement("DIV", { isContentEditable: true })), true);
});

test("进度条（input[type=range]）不算输入控件——点过它之后快捷键必须照常", () => {
  // 播放器里唯一的滑块就是进度条，点一下就拿走焦点。把它当输入控件放行的
  // 后果是空格不再播放暂停、F 不全屏、M 不静音，且焦点一直留在那儿收不回。
  assert.equal(isEditableTarget(new HTMLElement("INPUT", { type: "range" })), false);
});

test("非元素目标（window / document）不算输入控件", () => {
  assert.equal(isEditableTarget(null), false);
  assert.equal(isEditableTarget({}), false);
});

test("焦点在进度条上时，方向键仍走全局的 ±5 秒", () => {
  // 不这样的话方向键会落到 range 原生的 ±1 秒（step=1000），同一个键在
  // 点条前后跳的秒数不一样。
  const inEditable = isEditableTarget(new HTMLElement("INPUT", { type: "range" }));
  assert.deepEqual(resolveShortcut({ key: "ArrowRight", inEditable }), {
    type: "seek-by",
    seconds: 5,
  });
  assert.deepEqual(resolveShortcut({ key: " ", inEditable }), { type: "toggle-play" });
});

test("带 Ctrl/Cmd/Alt 的组合放行给浏览器", () => {
  assert.equal(resolveShortcut({ key: "f", inEditable: false, metaKey: true }), null);
  assert.equal(resolveShortcut({ key: "ArrowLeft", inEditable: false, ctrlKey: true }), null);
});

test("暂停时逐帧：, / . 各走一帧（与 YouTube 同键位）", () => {
  assert.deepEqual(resolveShortcut({ key: ",", inEditable: false }), {
    type: "step-frame",
    direction: -1,
  });
  assert.deepEqual(resolveShortcut({ key: ".", inEditable: false }), {
    type: "step-frame",
    direction: 1,
  });
});
