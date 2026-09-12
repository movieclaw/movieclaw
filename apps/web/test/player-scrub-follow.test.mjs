import assert from "node:assert/strict";
import test from "node:test";

import {
  SCRUB_FOLLOW_MAX_WAIT_MS,
  SCRUB_FOLLOW_SETTLE_MS,
  afterScrubFollow,
  initialScrubFollowState,
  planScrubFollow,
} from "../lib/player/scrub-follow.ts";

// ---------------------------------------------------------------------------
// 判定表
// ---------------------------------------------------------------------------

test("这一跳不便宜就不跟随——转码会话拖出缓冲要杀 ffmpeg 重启", () => {
  const plan = planScrubFollow({
    now: 1000,
    state: initialScrubFollowState(),
    cheap: false,
    reachable: true,
  });
  assert.deepEqual(plan, { kind: "skip" });
});

test("落点不在本次会话的时间轴上（往回拖过会话起点）同样不跟随", () => {
  const plan = planScrubFollow({
    now: 1000,
    state: initialScrubFollowState(),
    cheap: true,
    reachable: false,
  });
  assert.deepEqual(plan, { kind: "skip" });
});

test("本轮第一次移动就立刻跟随：按下就动，不凭空加一帧延迟", () => {
  // at = 0 意味着「这次拖动还没跟随过」，waited 必然越过兜底窗口
  const plan = planScrubFollow({
    now: 5_000,
    state: initialScrubFollowState(),
    cheap: true,
    reachable: true,
  });
  assert.deepEqual(plan, { kind: "follow" });
});

test("刚跟随过就排后沿，而不是像原先那样直接丢掉", () => {
  const plan = planScrubFollow({
    now: 1_010,
    state: { at: 1_000, pendingMs: null },
    cheap: true,
    reachable: true,
  });
  assert.deepEqual(plan, { kind: "defer", delayMs: SCRUB_FOLLOW_SETTLE_MS });
});

test("后沿的延时不许越过兜底时刻，否则连续扫动会被一路往后推到永远", () => {
  // 距上次跟随已 70ms，只剩 30ms 就到兜底窗口——只能等 30ms
  const plan = planScrubFollow({
    now: 1_070,
    state: { at: 1_000, pendingMs: null },
    cheap: true,
    reachable: true,
  });
  assert.deepEqual(plan, { kind: "defer", delayMs: 30 });
});

test("到了兜底窗口就立刻跟随：连续拖动中画面仍以 10Hz 刷新", () => {
  const plan = planScrubFollow({
    now: 1_000 + SCRUB_FOLLOW_MAX_WAIT_MS,
    state: { at: 1_000, pendingMs: null },
    cheap: true,
    reachable: true,
  });
  assert.deepEqual(plan, { kind: "follow" });
});

// ---------------------------------------------------------------------------
// 跟随落地之后的状态
// ---------------------------------------------------------------------------

test("跟随落地记下时刻并清掉在途落点", () => {
  assert.deepEqual(afterScrubFollow(1_060), { at: 1_060, pendingMs: null });
});

// ---------------------------------------------------------------------------
// 回归：手指停住之后，画面必须追上读数
//
// 这是 2026-09-11 反馈「快速高频率拖动时播放时间和展示进度不一致」的根因。
// 原先是**前沿**节流（`if (now - at < 100) return`）：窗口里后来的移动全被
// 丢掉，而手指停住之后浏览器不再发 pointermove——最后那个落点于是永远没有
// 机会落地，画面就钉在半路，读数却停在手指所在处，直到松手才对上。
//
// 下面用一段真实节奏的拖动（120Hz 移动 → 突然停住）把两种节流各跑一遍，
// 断言新实现在手指停住后一定有一次落地，且落的是**最后**那个落点。
// ---------------------------------------------------------------------------

/** 跑一段拖动，返回每一次真正写进 video 的落点与时刻 */
function drive(moves, { leadingEdge = false } = {}) {
  const landed = [];
  let state = initialScrubFollowState();
  let timer = null; // { at, targetMs }
  let clock = 0;

  const runDueTimer = (until) => {
    while (timer !== null && timer.at <= until) {
      const { at, targetMs } = timer;
      timer = null;
      state = afterScrubFollow(at);
      landed.push({ at, targetMs });
    }
  };

  for (const move of moves) {
    runDueTimer(move.at);
    clock = move.at;
    if (leadingEdge) {
      // 改动前的实现
      if (clock - state.at < 100) continue;
      state = afterScrubFollow(clock);
      landed.push({ at: clock, targetMs: move.targetMs });
      continue;
    }
    const plan = planScrubFollow({ now: clock, state, cheap: true, reachable: true });
    if (plan.kind === "skip") continue;
    if (plan.kind === "follow") {
      timer = null;
      state = afterScrubFollow(clock);
      landed.push({ at: clock, targetMs: move.targetMs });
      continue;
    }
    state = { ...state, pendingMs: move.targetMs };
    timer = { at: clock + plan.delayMs, targetMs: move.targetMs };
  }
  // 手指停住：不再有 pointermove，只剩在途的计时器
  runDueTimer(clock + 2_000);
  return landed;
}

/** 120Hz 的一段扫动：`stopAt` 毫秒处停住，落点从 600 秒扫到 3600 秒 */
function sweep(stopAtMs, startMs = 0) {
  const moves = [];
  for (let at = startMs; at <= startMs + stopAtMs; at += 1000 / 120) {
    const progress = (at - startMs) / stopAtMs;
    moves.push({ at, targetMs: Math.round(600_000 + 3_000_000 * progress) });
  }
  return moves;
}

test("回归：手指停住后画面必须追上最后那个落点（原前沿节流做不到）", () => {
  let leadingMisses = 0;
  let trailingMisses = 0;
  let worstLeadingGapMs = 0;
  // 扫 20 种「停住时刻落在节流窗口的不同相位」
  for (let phase = 0; phase < 20; phase += 1) {
    const moves = sweep(800 + phase * 5);
    const finalMs = moves[moves.length - 1].targetMs;
    const last = (list) => list[list.length - 1];

    const leading = last(drive(moves, { leadingEdge: true }));
    if (!leading || leading.targetMs !== finalMs) {
      leadingMisses += 1;
      worstLeadingGapMs = Math.max(worstLeadingGapMs, finalMs - (leading?.targetMs ?? 0));
    }

    const trailing = last(drive(moves));
    if (!trailing || trailing.targetMs !== finalMs) trailingMisses += 1;
  }
  assert.ok(
    leadingMisses >= 18,
    `前沿节流应当在几乎所有相位上丢掉最后一个落点（实测 ${leadingMisses}/20）——` +
      "丢不掉说明这条回归测试没测到东西",
  );
  // 画面与读数差出几分钟，正是反馈里「播放时间和展示进度不一致」的量级
  assert.ok(worstLeadingGapMs > 60_000, `前沿节流最坏只差了 ${worstLeadingGapMs}ms，没复现出问题`);
  assert.equal(trailingMisses, 0, "后沿节流必须在任何相位下都把最后一个落点送到");
});

test("回归：手指停住后，最后一次落地不迟于停手 + 停稳窗口", () => {
  const moves = sweep(833);
  const stoppedAt = moves[moves.length - 1].at;
  const landed = drive(moves);
  const last = landed[landed.length - 1];
  assert.ok(
    last.at - stoppedAt <= SCRUB_FOLLOW_SETTLE_MS,
    `停手到画面落地用了 ${last.at - stoppedAt}ms，超过了 ${SCRUB_FOLLOW_SETTLE_MS}ms`,
  );
});

test("连续扫动时跟随频率仍是 10Hz，没有因为改后沿而多捅服务端", () => {
  const moves = sweep(2_000);
  const trailing = drive(moves);
  const leading = drive(moves, { leadingEdge: true });
  // 前沿节流的实际周期略大于 100ms（要等到第一个越过窗口的移动事件），后沿
  // 恰好 100ms，2 秒里因此多出一两次。多出两次以上就说明节奏被改快了。
  assert.ok(
    trailing.length <= leading.length + 2,
    `后沿跟随 ${trailing.length} 次、前沿 ${leading.length} 次，节奏变快了`,
  );
  for (let i = 1; i < trailing.length; i += 1) {
    const gap = trailing[i].at - trailing[i - 1].at;
    assert.ok(gap >= SCRUB_FOLLOW_SETTLE_MS, `两次跟随只隔了 ${gap}ms`);
  }
});

test("每一次落地用的都是新鲜落点，不是窗口开头那个过期的", () => {
  const moves = sweep(1_000);
  const landed = drive(moves);
  for (const hit of landed) {
    // 落地的这个值必须是「停稳窗口之内」某一次移动时手指的位置
    const fresh = moves.some(
      (m) => m.targetMs === hit.targetMs && hit.at - m.at >= 0 && hit.at - m.at <= SCRUB_FOLLOW_SETTLE_MS,
    );
    assert.ok(fresh, `${hit.at}ms 落地的 ${hit.targetMs} 已经过期超过停稳窗口`);
  }
});
