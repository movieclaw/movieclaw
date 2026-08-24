import assert from "node:assert/strict";
import test from "node:test";

import {
  FRAMEDROP_MIN_FRAMES,
  FRAMEDROP_RATIO,
  FRAMEDROP_WINDOW_SAMPLES,
  createFrameDropTracker,
} from "../lib/player/framedrop.ts";

/** 按每秒帧数与掉帧数连喂 n 秒，返回最后一次判定。 */
function feed(tracker, seconds, { fps = 24, dropPerSecond = 0, from = { dropped: 0, total: 0 } }) {
  let { dropped, total } = from;
  let verdict = { degrade: false, ratio: null };
  for (let i = 0; i < seconds; i += 1) {
    total += fps;
    dropped += dropPerSecond;
    verdict = tracker.sample({ dropped, total });
  }
  return { verdict, state: { dropped, total } };
}

test("流畅播放永不降档", () => {
  const tracker = createFrameDropTracker();
  const { verdict } = feed(tracker, 30, { fps: 24, dropPerSecond: 0 });
  assert.equal(verdict.degrade, false);
  assert.equal(verdict.ratio, 0);
});

test("持续掉帧超过阈值触发降档", () => {
  const tracker = createFrameDropTracker();
  // 24fps 里每秒掉 4 帧 ≈ 16.7%，高于 10% 阈值
  const { verdict } = feed(tracker, FRAMEDROP_WINDOW_SAMPLES + 1, { fps: 24, dropPerSecond: 4 });
  assert.equal(verdict.degrade, true);
  assert.ok(verdict.ratio >= FRAMEDROP_RATIO);
});

test("难看但可忍的轻微掉帧不降档——降档的代价是整路转码", () => {
  const tracker = createFrameDropTracker();
  // 每秒掉 1 帧 ≈ 4%：超 QoE 及格线但远不到「不可看」
  const { verdict } = feed(tracker, 30, { fps: 24, dropPerSecond: 1 });
  assert.equal(verdict.degrade, false);
  assert.ok(verdict.ratio < FRAMEDROP_RATIO);
});

test("窗口没满之前不判定——起播瞬时掉帧全是误报", () => {
  const tracker = createFrameDropTracker();
  // 前几秒掉帧很凶（解码器起步），但样本不足窗口，不能判
  const { verdict } = feed(tracker, FRAMEDROP_WINDOW_SAMPLES, { fps: 24, dropPerSecond: 10 });
  assert.equal(verdict.degrade, false);
  assert.equal(verdict.ratio, null);
});

test("窗口内帧数不足不判定——挡住除小数陷阱", () => {
  const tracker = createFrameDropTracker();
  // 每秒只有 5 帧（远低于 100 帧下限），3 掉 1 也不能算 33%
  const { verdict } = feed(tracker, 20, { fps: 5, dropPerSecond: 1 });
  assert.equal(verdict.degrade, false);
  assert.equal(verdict.ratio, null);
  assert.ok(FRAMEDROP_WINDOW_SAMPLES * 5 < FRAMEDROP_MIN_FRAMES);
});

test("判定看的是窗口增量，不是历史累计——起播的旧账不追", () => {
  const tracker = createFrameDropTracker();
  // 先狠掉 10 秒，然后完全恢复：窗口滑过去之后必须回到不降档
  const { state } = feed(tracker, 12, { fps: 24, dropPerSecond: 8 });
  const { verdict } = feed(tracker, FRAMEDROP_WINDOW_SAMPLES + 1, {
    fps: 24,
    dropPerSecond: 0,
    from: state,
  });
  assert.equal(verdict.degrade, false);
  assert.equal(verdict.ratio, 0);
});

test("累计计数变小（换了 src）自动重置窗口，不算负增量", () => {
  const tracker = createFrameDropTracker();
  feed(tracker, 15, { fps: 24, dropPerSecond: 4 });
  // 新会话：计数从零重来。若不重置，负增量会把比率算成任意鬼值
  const { verdict } = feed(tracker, 5, { fps: 24, dropPerSecond: 0 });
  assert.equal(verdict.degrade, false);
  assert.equal(verdict.ratio, null); // 重置后窗口未满
});

test("reset 之后从头攒窗口", () => {
  const tracker = createFrameDropTracker();
  feed(tracker, 15, { fps: 24, dropPerSecond: 4 });
  tracker.reset();
  const { verdict } = feed(tracker, 3, {
    fps: 24,
    dropPerSecond: 4,
    from: { dropped: 60, total: 360 },
  });
  assert.equal(verdict.degrade, false);
  assert.equal(verdict.ratio, null);
});
