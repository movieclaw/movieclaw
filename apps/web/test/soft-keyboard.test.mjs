import assert from "node:assert/strict";
import test from "node:test";

import { softKeyboardPossible } from "../lib/soft-keyboard.ts";

/**
 * document.activeElement 的最小替身：只实现 matches，按「选择器里有没有点名自己」
 * 作答。够验证「哪些元素算可输入」这条契约——名单少一类，就会在那类元素聚焦时
 * 把真键盘误判成"没有键盘"，视口占高被清零，贴底的输入行重新沉到键盘底下。
 */
function focusOn(token) {
  globalThis.document = {
    activeElement:
      token === null
        ? null
        : { matches: (selector) => selector.split(",").some((part) => part.trim().startsWith(token)) },
  };
}

test("焦点不在可输入元素上时，一律按没有键盘处理", () => {
  focusOn(null);
  assert.equal(softKeyboardPossible(), false);

  focusOn("div");
  assert.equal(softKeyboardPossible(), false);
});

test("可输入元素聚焦时认键盘：input / textarea / select / contenteditable", () => {
  for (const token of ["input", "textarea", "select", "[contenteditable]"]) {
    focusOn(token);
    assert.equal(softKeyboardPossible(), true, `${token} 聚焦时应认为键盘可能立着`);
  }
});
