import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_AUTOPLAY_ATTEMPTS,
  attemptAutoplay,
  classifyPlayFailure,
  shouldAttemptAutoplay,
} from "../lib/player/autoplay.ts";

/** 造一个 DOMException 那样的错误：只有 name 是判定依据。 */
function named(name) {
  const error = new Error(name);
  error.name = name;
  return error;
}

/**
 * 假 video：按脚本依次抛错 / 成功，并记录每次调用时的静音状态——
 * 「静音重试」这件事只有看调用当时的 muted 才能验。
 */
function fakeVideo(script) {
  const calls = [];
  return {
    muted: false,
    calls,
    async play() {
      const step = script[calls.length];
      calls.push({ muted: this.muted });
      if (step) throw step;
    },
  };
}

test("一次成功就是正常出声播放", async () => {
  const video = fakeVideo([]);
  assert.equal(await attemptAutoplay(video), "playing");
  assert.equal(video.calls.length, 1);
  assert.equal(video.muted, false);
});

test("被策略拦下时静音重试成功", async () => {
  const video = fakeVideo([named("NotAllowedError")]);
  assert.equal(await attemptAutoplay(video), "muted");
  assert.equal(video.calls.length, 2);
  // 第一次带声音、第二次静音，且静音状态要留着（UI 靠它显示恢复声音入口）
  assert.deepEqual(
    video.calls.map((call) => call.muted),
    [false, true],
  );
  assert.equal(video.muted, true);
});

test("静音也放不起来就还原静音状态并报 blocked", async () => {
  const video = fakeVideo([named("NotAllowedError"), named("NotSupportedError")]);
  assert.equal(await attemptAutoplay(video), "blocked");
  // 留一个静音的播放器给用户，他点播放会以为片子没音轨
  assert.equal(video.muted, false);
});

test("静音重试被打断算 interrupted，交给下一次重试而不是判死", async () => {
  const video = fakeVideo([named("NotAllowedError"), named("AbortError")]);
  assert.equal(await attemptAutoplay(video), "interrupted");
  assert.equal(video.muted, false);
});

test("AbortError 是时序打断，不能因此静音", async () => {
  const video = fakeVideo([named("AbortError")]);
  assert.equal(await attemptAutoplay(video), "interrupted");
  // 只调用一次：没有静音重试
  assert.equal(video.calls.length, 1);
  assert.equal(video.muted, false);
});

test("认不出的错误一律 blocked，不做静音降级", async () => {
  const video = fakeVideo([named("NotSupportedError")]);
  assert.equal(await attemptAutoplay(video), "blocked");
  assert.equal(video.calls.length, 1);
});

test("非 Error 的抛出物也不会让分类崩掉", () => {
  assert.equal(classifyPlayFailure(undefined), "blocked");
  assert.equal(classifyPlayFailure("NotAllowedError"), "blocked");
  assert.equal(classifyPlayFailure({ name: "NotAllowedError" }), "muted");
});

const gate = {
  wanted: true,
  paused: true,
  attempts: 0,
  last: null,
};

test("想播、还停着、没试过：该试", () => {
  assert.equal(shouldAttemptAutoplay(gate), true);
});

test("用户自己按了暂停就不再抢遥控器", () => {
  assert.equal(shouldAttemptAutoplay({ ...gate, wanted: false }), false);
});

test("已经在放了就没什么可试的", () => {
  assert.equal(shouldAttemptAutoplay({ ...gate, paused: false }), false);
});

test("interrupted 之后要继续试——这正是首播卡住的那一类", () => {
  assert.equal(shouldAttemptAutoplay({ ...gate, attempts: 1, last: "interrupted" }), true);
});

test("blocked 与 muted 都是终态，不再重试", () => {
  assert.equal(shouldAttemptAutoplay({ ...gate, attempts: 1, last: "blocked" }), false);
  // muted 时 paused 本应为 false，这里强行构造也要挡住
  assert.equal(shouldAttemptAutoplay({ ...gate, attempts: 1, last: "muted" }), false);
});

test("重试次数有上限，不会无限打", () => {
  assert.equal(
    shouldAttemptAutoplay({ ...gate, attempts: MAX_AUTOPLAY_ATTEMPTS - 1, last: "interrupted" }),
    true,
  );
  assert.equal(
    shouldAttemptAutoplay({ ...gate, attempts: MAX_AUTOPLAY_ATTEMPTS, last: "interrupted" }),
    false,
  );
});
