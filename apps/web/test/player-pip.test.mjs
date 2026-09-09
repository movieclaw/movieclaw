import assert from "node:assert/strict";
import test from "node:test";

import { pipSupported } from "../lib/player/pip.ts";

test("iOS 主屏 Web App：标准标志谎报 true，以前缀 API 的 false 为准", () => {
  // 回归用例。原来两个信号取或，这里会得到 true——按钮渲染出来、点了没反应。
  assert.equal(
    pipSupported({ disabled: false, webkitSupports: false, standardEnabled: true }),
    false,
  );
});

test("iOS / macOS Safari：前缀 API 报 true 就是能用", () => {
  assert.equal(
    pipSupported({ disabled: false, webkitSupports: true, standardEnabled: true }),
    true,
  );
});

test("没有前缀 API 的浏览器（Chrome/Edge）退回标准标志", () => {
  assert.equal(
    pipSupported({ disabled: false, webkitSupports: null, standardEnabled: true }),
    true,
  );
  assert.equal(
    pipSupported({ disabled: false, webkitSupports: null, standardEnabled: false }),
    false,
  );
});

test("webkitSupports 的 null 是「没有这个 API」，不是「探测为假」", () => {
  // 两者都落到 standardEnabled 上才算区分开：null 不能被当成 false 短路掉
  assert.equal(
    pipSupported({ disabled: false, webkitSupports: null, standardEnabled: true }),
    true,
  );
  assert.equal(
    pipSupported({ disabled: false, webkitSupports: false, standardEnabled: true }),
    false,
  );
});

test("disablePictureInPicture 一票否决，压过两个信号", () => {
  assert.equal(
    pipSupported({ disabled: true, webkitSupports: true, standardEnabled: true }),
    false,
  );
});
