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

/** 假 video：按脚本依次抛错 / 成功，并记录调用次数。 */
function fakeVideo(script) {
  const calls = [];
  return {
    calls,
    async play() {
      const step = script[calls.length];
      calls.push({});
      if (step) throw step;
    },
  };
}

test("一次成功就是正常出声播放", async () => {
  const video = fakeVideo([]);
  assert.equal(await attemptAutoplay(video), "playing");
  assert.equal(video.calls.length, 1);
});

test("被策略拦下判 blocked：不做静音降级，等用户自己点播放", async () => {
  const video = fakeVideo([named("NotAllowedError")]);
  assert.equal(await attemptAutoplay(video), "blocked");
  // 只调用一次：没有静音重试
  assert.equal(video.calls.length, 1);
});

test("AbortError 是时序打断，交给下一次重试而不是判死", async () => {
  const video = fakeVideo([named("AbortError")]);
  assert.equal(await attemptAutoplay(video), "interrupted");
  assert.equal(video.calls.length, 1);
});

test("认不出的错误一律 blocked", async () => {
  const video = fakeVideo([named("NotSupportedError")]);
  assert.equal(await attemptAutoplay(video), "blocked");
  assert.equal(video.calls.length, 1);
});

test("非 Error 的抛出物也不会让分类崩掉", () => {
  assert.equal(classifyPlayFailure(undefined), "blocked");
  assert.equal(classifyPlayFailure("NotAllowedError"), "blocked");
  assert.equal(classifyPlayFailure({ name: "AbortError" }), "interrupted");
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

test("blocked 是终态，不再重试", () => {
  assert.equal(shouldAttemptAutoplay({ ...gate, attempts: 1, last: "blocked" }), false);
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

test("放完的元素不自动重播：拖到片尾落地后 canplay 照样来，play() 会 seek 回 0 从头放", () => {
  // 2026-09-14 真浏览器复现：拖到片尾，seek 落在片长处元素当场 ended，随之而来的
  // canplay 触发自动播放闸门——ended 的元素按规范是暂停的，闸门原本只看 paused
  const gate = { wanted: true, paused: true, attempts: 0, last: null };
  assert.equal(shouldAttemptAutoplay({ ...gate, ended: true }), false);
  assert.equal(shouldAttemptAutoplay({ ...gate, ended: false }), true);
});
