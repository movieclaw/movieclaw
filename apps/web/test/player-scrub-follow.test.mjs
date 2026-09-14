import assert from "node:assert/strict";
import test from "node:test";

import {
  SCRUB_FOLLOW_MAX_WAIT_MS,
  SCRUB_FOLLOW_SETTLE_MS,
  SCRUB_INPUT_STEP_MS,
  SEEK_DUPLICATE_TOLERANCE_S,
  acceptsNativeScrubValue,
  afterScrubFollow,
  initialScrubFollowState,
  planScrubFollow,
  scrubCommitTarget,
  scrubInputValue,
  seekAlreadyInFlight,
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
// 直出档：缓冲外只在手指停住时跟一次（2026-09-13 真机反馈）
//
// 整个文件都能 seek 不等于不要钱：远程的进度 MP4 在手机上每一次 seek 都是一条
// 新的 Range 请求外加播放器重新起解码。原先直出档被 isCheapSeek 无条件判成
// 便宜，扫动途中一秒十次、每次都被下一次打断，画面一路抽却追不上手指。
// ---------------------------------------------------------------------------

test("直出档拖出缓冲：不按 10Hz 跟，只排一个停稳窗口的后沿", () => {
  const plan = planScrubFollow({
    now: 5_000,
    state: initialScrubFollowState(),
    cheap: false,
    reachable: true,
    settleOnly: true,
  });
  assert.deepEqual(plan, { kind: "defer", delayMs: SCRUB_FOLLOW_SETTLE_MS });
});

test("直出档扫动途中永远不落地：兜底窗口到了也只是重排后沿", () => {
  // 上次跟随已经过去很久，便宜的跳转此刻会「follow」；不便宜的直出档不行
  const plan = planScrubFollow({
    now: 1_000 + SCRUB_FOLLOW_MAX_WAIT_MS * 5,
    state: { at: 1_000, pendingMs: null },
    cheap: false,
    reachable: true,
    settleOnly: true,
  });
  assert.deepEqual(plan, { kind: "defer", delayMs: SCRUB_FOLLOW_SETTLE_MS });
});

test("直出档落在缓冲里照旧 10Hz 跟随：数据在手上没理由等停住", () => {
  const plan = planScrubFollow({
    now: 5_000,
    state: initialScrubFollowState(),
    cheap: true,
    reachable: true,
    settleOnly: true,
  });
  assert.deepEqual(plan, { kind: "follow" });
});

test("settleOnly 不放宽可达性：往回拖过会话起点仍然不跟", () => {
  const plan = planScrubFollow({
    now: 5_000,
    state: initialScrubFollowState(),
    cheap: false,
    reachable: false,
    settleOnly: true,
  });
  assert.deepEqual(plan, { kind: "skip" });
});

test("直出档一段扫动只在停住后落地一次，落的是最后那个落点", () => {
  // 复用下面的 drive：把每一次移动都按 settleOnly 排后沿，手指停住 2 秒
  const moves = sweep(1_000);
  const landed = drive(moves, { settleOnly: true });
  assert.equal(landed.length, 1, `扫动 1 秒落地了 ${landed.length} 次，应当只有停住那一次`);
  assert.equal(landed[0].targetMs, moves[moves.length - 1].targetMs);
  assert.equal(landed[0].at - moves[moves.length - 1].at, SCRUB_FOLLOW_SETTLE_MS);
});

// ---------------------------------------------------------------------------
// 抬手提交到哪儿（2026-09-13 真机反馈：触屏松手后进度往回跳一下）
// ---------------------------------------------------------------------------

test("鼠标抬手提交指针最后所在处：合帧压着的最后一段位移不能丢", () => {
  assert.equal(
    scrubCommitTarget({
      pointerType: "mouse",
      lastPointerMs: 305_000,
      flushedMs: 300_000,
      draggingMs: 290_000,
    }),
    305_000,
  );
  // 量不到指针位置（片长刚变 null 之类）退回渲染出来的值
  assert.equal(
    scrubCommitTarget({ pointerType: "mouse", lastPointerMs: null, flushedMs: null, draggingMs: 300_000 }),
    300_000,
  );
});

test("触屏抬手提交屏幕上正显示的值：指腹剥离时的漂移进不了落点", () => {
  // 手指抬起瞬间接触点往回挪了一截，指针最后所在处已经不是用户看到的位置；
  // 合帧最近刷出去的 300_000 才是屏幕上的读数
  assert.equal(
    scrubCommitTarget({
      pointerType: "touch",
      lastPointerMs: 262_000,
      flushedMs: 300_000,
      draggingMs: 300_000,
    }),
    300_000,
  );
});

test("触屏快速甩动一帧都没刷出去：提交指针最后所在处，而不是按下那一刻的渲染值", () => {
  // 2026-09-14 反馈：快速拖到目标立刻松手，落点回到起点。整个甩动压在一帧里，
  // rAF 一次都没跑，React 渲染出来的 dragging 仍是按下时的 45_926
  assert.equal(
    scrubCommitTarget({
      pointerType: "touch",
      lastPointerMs: 1_489_320,
      flushedMs: null,
      draggingMs: 45_926,
    }),
    1_489_320,
  );
});

test("触屏刷出去过但 React 还没渲染：提交刷出去的值，不是过期的渲染值", () => {
  assert.equal(
    scrubCommitTarget({
      pointerType: "touch",
      lastPointerMs: 1_489_320,
      flushedMs: 1_450_000,
      draggingMs: 45_926,
    }),
    1_450_000,
  );
});

test("笔跟鼠标走：笔尖抬起没有指腹那种剥离位移", () => {
  assert.equal(
    scrubCommitTarget({
      pointerType: "pen",
      lastPointerMs: 301_000,
      flushedMs: 300_000,
      draggingMs: 300_000,
    }),
    301_000,
  );
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
function drive(moves, { leadingEdge = false, settleOnly = false } = {}) {
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
    // settleOnly 模拟的是直出档拖出缓冲：不便宜、但允许停住时跟一次
    const plan = planScrubFollow({
      now: clock,
      state,
      cheap: !settleOnly,
      reachable: true,
      settleOnly,
    });
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

// ---------------------------------------------------------------------------
// 回归：抬手之后原生滑块补发的 change 不能把 dragging 重新钉住
//
// 2026-09-14 反馈「拖到 15 分钟松手，画面到了 15 分钟，圆点却不动了」。进度条的
// 拖动是指针事件自己算的，但原生 range 滑块并没有关掉：鼠标按下轨道、Chromium
// 触屏按下轨道、iOS 上手指恰好压中那 1px 把手所在的一列，浏览器都会同时跑自己
// 的拖动——抬手时补一次 `change`，而它在 pointerup 之后才到。pointerup 已经把
// dragging 清成 null，这一发 change 一进 onChange 又把 dragging 钉回落点。
// Chromium 真机复现（Playwright，鼠标与 CDP 触摸各一遍）：修前松手 1.6 秒后
// input.value 纹丝不动，修后跟着位置走。
// ---------------------------------------------------------------------------

test("写进 range 的值先按浏览器规则整理：夹进片长、对齐步长、两边一样近取大的", () => {
  assert.equal(scrubInputValue(1_489_320, 3_600_000), 1_489_000);
  assert.equal(scrubInputValue(1_489_500, 3_600_000), 1_490_000);
  assert.equal(scrubInputValue(-5, 3_600_000), 0);
  assert.equal(scrubInputValue(9_999_999, 3_600_000), 3_600_000);
  // 片长未知时进度条是禁用的，值统一为 0
  assert.equal(scrubInputValue(1_000, null), 0);
  assert.equal(scrubInputValue(1_000, 0), 0);
});

test("对齐后越过 max 要退一格：浏览器不会把 value 整理到 max 之外", () => {
  // max 本身不在步长上：3_599_700 最近的步长倍数是 3_600_000，越过 max
  assert.equal(scrubInputValue(3_599_700, 3_599_999), 3_599_000);
  assert.equal(scrubInputValue(3_599_999, 3_599_999), 3_599_000);
  assert.equal(SCRUB_INPUT_STEP_MS, 1_000);
});

test("抬手补发的 change 与屏幕值整理后相同：不是用户改了值，不收", () => {
  // pointerup 已把 dragging 清掉、跳转提交到 1_489_320，React 写进 range 的是
  // 整理后的 1_489_000，原生 change 读回来的也是它
  assert.equal(
    acceptsNativeScrubValue({
      nativeValue: 1_489_000,
      shownMs: 1_489_320,
      durationMs: 3_600_000,
      pointerDragging: false,
    }),
    false,
  );
});

test("指针按着的时候原生滑块连发的 input 一律不收：落点由指针路径算", () => {
  assert.equal(
    acceptsNativeScrubValue({
      nativeValue: 1_500_000,
      shownMs: 1_489_320,
      durationMs: 3_600_000,
      pointerDragging: true,
    }),
    false,
  );
});

test("键盘真改了值（Home / End / PageUp）照常放行", () => {
  assert.equal(
    acceptsNativeScrubValue({
      nativeValue: 1_490_000,
      shownMs: 1_489_320,
      durationMs: 3_600_000,
      pointerDragging: false,
    }),
    true,
  );
  assert.equal(
    acceptsNativeScrubValue({
      nativeValue: 0,
      shownMs: 1_489_320,
      durationMs: 3_600_000,
      pointerDragging: false,
    }),
    true,
  );
});

test("片尾那一格同样认得出来：max 不在步长上时补发的 change 带的是退一格的值", () => {
  assert.equal(
    acceptsNativeScrubValue({
      nativeValue: 3_599_000,
      shownMs: 3_599_700,
      durationMs: 3_599_999,
      pointerDragging: false,
    }),
    false,
  );
});

// ---------------------------------------------------------------------------
// 回归：跟随已经为这个落点发了 seek，松手不再叠一次
//
// 2026-09-14 请求日志实证：第二次 seek 会把第一次正在取的索引请求掐掉
// （ERR_ABORTED），Chromium 退回从当前缓冲末尾顺序扫描找目标，跳 6 分钟要顺序
// 读 47MB——画面停在原地、圆点不动。seek 途中 currentTime 读到的是这次 seek 的
// 目标，所以「正在往同一落点去」直接用它判。
// ---------------------------------------------------------------------------

test("元素正往同一落点 seek（或已停在那儿）：不再叠一次 seek", () => {
  assert.equal(seekAlreadyInFlight({ currentTimeSeconds: 720.0, targetSeconds: 720.0 }), true);
  // 时间刻度换算带来的抖动吃得掉
  assert.equal(seekAlreadyInFlight({ currentTimeSeconds: 720.12, targetSeconds: 720.0 }), true);
});

test("落点差得超过容差就要真的 seek：快速跟随落在关键帧上时松手仍要精确落地", () => {
  assert.equal(seekAlreadyInFlight({ currentTimeSeconds: 716.0, targetSeconds: 720.3 }), false);
  assert.equal(
    seekAlreadyInFlight({ currentTimeSeconds: 720.0, targetSeconds: 720.0 + SEEK_DUPLICATE_TOLERANCE_S }),
    false,
  );
  assert.equal(seekAlreadyInFlight({ currentTimeSeconds: Number.NaN, targetSeconds: 720 }), false);
});
