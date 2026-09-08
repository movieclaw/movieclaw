import assert from "node:assert/strict";
import test from "node:test";

import {
  MEDIA_RECOVER_COOLDOWN_MS,
  nextMediaRecovery,
} from "../lib/player/media-recover.ts";

test("第一次解码错误先重建解码管线，不许直接降档", () => {
  // 降档 = 画质掉一级 + 几秒黑屏重开会话，是最贵的手段，不该是第一反应
  assert.equal(nextMediaRecovery({ lastRecoverAt: null, lastSwapAt: null }, 1_000), "recover");
});

test("冷却内又炸说明上一级没救回来，升到换音频编解码", () => {
  const state = { lastRecoverAt: 1_000, lastSwapAt: null };
  assert.equal(nextMediaRecovery(state, 1_000 + MEDIA_RECOVER_COOLDOWN_MS - 1), "swap");
});

test("两级都在冷却内失效才放弃，交给降档", () => {
  const state = { lastRecoverAt: 1_000, lastSwapAt: 2_000 };
  assert.equal(nextMediaRecovery(state, 2_500), "give-up");
});

test("隔了冷却还没再犯，当作上次救活了，下一起事故从第一级重来", () => {
  const state = { lastRecoverAt: 1_000, lastSwapAt: 2_000 };
  assert.equal(nextMediaRecovery(state, 2_000 + MEDIA_RECOVER_COOLDOWN_MS + 1), "recover");
});

test("页面刚加载（performance.now 还很小）时也从第一级开始", () => {
  // 用 0 当「没自救过」的哨兵会让开局 3 秒内的第一次错误直接跳到第二级，
  // 所以状态用 null 而不是 0——这条守的就是那个坑
  assert.equal(nextMediaRecovery({ lastRecoverAt: null, lastSwapAt: null }, 12), "recover");
});
