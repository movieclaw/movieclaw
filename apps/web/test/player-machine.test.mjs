import assert from "node:assert/strict";
import test from "node:test";

import { initialPlayerState, isBusy, playerReducer } from "../lib/player/machine.ts";
import {
  formatClock,
  isInEndCredits,
  planSeek,
  toFileMs,
  toSessionSeconds,
} from "../lib/player/timeline.ts";

/** 造一个 outcome=plan 的会话，tier 可指定。 */
function planSession(tier, startMs = 0) {
  return {
    decision: {
      outcome: "plan",
      tier,
      file_id: 1,
      container: tier === 0 ? "mp4" : "hls-fmp4",
      video: { action: "copy", codec: null, height: null, tone_map: false },
      audio: { action: "copy", track_ref: null, codec: null, channels: null, downmix: false },
      subtitles: [],
      degraded_from: null,
      cost_hint: null,
      can_self_enable: null,
      setting_namespace: null,
      setting_key: null,
      reason: "可直通",
      suggestion: null,
    },
    session_id: tier === 0 ? null : "sess-1",
    stream_url: "/stream",
    start_ms: startMs,
    subtitle_urls: [],
  };
}

function run(events, state = initialPlayerState) {
  return events.reduce(playerReducer, state);
}

test("正常起播：决策 → 缓冲 → 出画", () => {
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(0) },
    { type: "playing" },
  ]);
  assert.equal(state.phase, "playing");
  assert.equal(state.session.session_id, null); // 档 0 无会话
});

test("降档是逐级的：档 1 失败后只把 1 标失败，仍会试档 2", () => {
  // 档 2 同样 -c:v copy——失败原因若在音轨，档 2 正好修好；跳过它可能让
  // 本可直通的视频每次播放都多转一路。
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(1) },
    { type: "playing" },
    { type: "failed", reason: "解码错误" },
  ]);
  assert.equal(state.phase, "degrading");
  assert.deepEqual(state.failedTiers, [1]);
});

test("连败两次直接跳兜底档：兜底档以下全部标失败", () => {
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(1) },
    { type: "playing" },
    { type: "failed", reason: "第一次" },
    { type: "session", session: planSession(2) },
    { type: "failed", reason: "第二次" },
  ]);
  assert.deepEqual(state.failedTiers, [0, 1, 2, 3]);
});

test("出画会重置连败计数：后面再失败重新从逐级降开始", () => {
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(1) },
    { type: "playing" },
    { type: "failed", reason: "第一次" },
    { type: "session", session: planSession(2) },
    { type: "playing" }, // 这一档放出来了
    { type: "failed", reason: "半小时后又坏了" },
  ]);
  // 只加了档 2，没有一步跳到兜底
  assert.deepEqual(state.failedTiers, [1, 2]);
  assert.equal(state.phase, "degrading");
});

test("兜底档也失败：进错误态并给出换播放器的建议", () => {
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(4) },
    { type: "failed", reason: "软转也放不出来" },
  ]);
  assert.equal(state.phase, "error");
  assert.match(state.error.suggestion, /原生播放器/);
});

test("兜底档失败进错误态时会话必须清空——错误页背后不能还挂着转码", () => {
  // 不清的话心跳继续给这个会话续命、引擎还在拉流：用户对着错误页，
  // 服务端的软转 ffmpeg 却一直烧 CPU（「播放停了 ffmpeg 还在跑」的来路之一）。
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(4) },
    { type: "failed", reason: "软转也放不出来" },
  ]);
  assert.equal(state.session, null);
});

test("需要同意软件转码时进 consent 态，同意后回到决策", () => {
  const consent = planSession(4);
  consent.decision = {
    ...consent.decision,
    outcome: "consent",
    cost_hint: "会占用大量 CPU",
    can_self_enable: true,
  };
  const waiting = run([{ type: "request", startMs: 0 }, { type: "session", session: consent }]);
  assert.equal(waiting.phase, "consent");
  assert.equal(playerReducer(waiting, { type: "consent-granted" }).phase, "deciding");
});

test("播放中途的缓冲不会把状态打回起播阶段", () => {
  // 否则进度条一卡，界面就闪一下"正在启动播放"
  const starting = run([{ type: "request", startMs: 0 }]);
  assert.equal(playerReducer(starting, { type: "buffering" }).phase, "deciding");

  const playing = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(0) },
    { type: "playing" },
  ]);
  assert.equal(playerReducer(playing, { type: "buffering" }).phase, "buffering");
});

test("起播路上的续播点 seek 不把 phase 顶成 seeking——加载转圈要一直亮到出画", () => {
  // seeking 不算 busy（正常拖拽不弹全屏转圈）。挂流后跳续播点会让 video
  // 发 seeking 事件，若被它顶掉 buffering，转圈提前消失、播放键在放不动
  // 的时候就亮出来：用户点了没反应，以为播放器坏了（真机复现）。
  const loading = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(0) },
  ]);
  assert.equal(loading.phase, "buffering");
  const afterSeek = playerReducer(loading, { type: "seeking" });
  assert.equal(afterSeek.phase, "buffering");
  assert.equal(isBusy(afterSeek.phase), true);

  // 出画之后的 seek 才是真 seeking；ended 之后往回拖同理
  const playing = playerReducer(afterSeek, { type: "playing" });
  assert.equal(playerReducer(playing, { type: "seeking" }).phase, "seeking");
  const ended = playerReducer(playing, { type: "ended" });
  assert.equal(playerReducer(ended, { type: "seeking" }).phase, "seeking");
});

test("重新起播清空上一轮的失败记录", () => {
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(1) },
    { type: "failed", reason: "坏了" },
    { type: "request", startMs: 90_000 }, // 换了一集
  ]);
  assert.deepEqual(state.failedTiers, []);
  assert.equal(state.startMs, 90_000);
});

test("手动切档开始新一轮请求：旧会话与自动降档记录都清空", () => {
  const playing = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(3) },
    { type: "playing" },
  ]);
  const switched = playerReducer(playing, { type: "request", startMs: 12_000 });
  assert.equal(switched.phase, "deciding");
  assert.equal(switched.session, null);
  assert.equal(switched.decision, null);
  assert.deepEqual(switched.failedTiers, []);
  assert.equal(switched.startMs, 12_000);
});

test("新会话建立前，旧媒体事件不能污染切档请求", () => {
  const playing = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(3) },
    { type: "playing" },
  ]);
  const switching = playerReducer(playing, { type: "request", startMs: 12_000 });
  for (const stale of [
    { type: "playing" },
    { type: "ended" },
    { type: "buffering" },
    { type: "seeking" },
    { type: "failed", reason: "旧会话的解码错误" },
  ]) {
    const after = playerReducer(switching, stale);
    assert.equal(after.phase, "deciding", `${stale.type} 不该改写新请求`);
    assert.equal(after.session, null);
    assert.deepEqual(after.failedTiers, []);
  }
});

test("restart 先摘掉旧会话，但保留自动重试的失败历史", () => {
  const state = run([
    { type: "request", startMs: 0 },
    { type: "session", session: planSession(3) },
    { type: "playing" },
    { type: "failed", reason: "网络中断" },
  ]);
  const restarted = playerReducer(state, { type: "restart", startMs: 8_000 });
  assert.equal(restarted.phase, "session-starting");
  assert.equal(restarted.session, null);
  assert.equal(restarted.decision, null);
  assert.deepEqual(restarted.failedTiers, [3]);
});

test("起播失败后原样重试，状态必须与上一次可区分", () => {
  // 编排层靠状态指纹给 effect 去重，指纹撞了请求就发不出去，界面会永远停在
  // 「正在判断播放方式」。重试恰恰是参数一模一样再来一次，全靠 attempt 区分。
  const failed = run([
    { type: "request", startMs: 0 },
    { type: "fatal", message: "转码进程启动失败" },
  ]);
  const retried = playerReducer(failed, { type: "request", startMs: 0 });
  assert.equal(retried.phase, "deciding");
  assert.notEqual(retried.attempt, failed.attempt);
});

test("同意继续播也算一次新的起播，不能与之前的决策撞纹", () => {
  const consent = planSession(4);
  consent.decision = { ...consent.decision, outcome: "consent" };
  const waiting = run([{ type: "request", startMs: 0 }, { type: "session", session: consent }]);
  const granted = playerReducer(waiting, { type: "consent-granted" });
  assert.equal(granted.phase, "deciding");
  assert.notEqual(granted.attempt, waiting.attempt);
});

test("连续重试每次都递增，第二次重试同样发得出去", () => {
  let state = run([{ type: "request", startMs: 0 }]);
  const seen = new Set([state.attempt]);
  for (let i = 0; i < 3; i += 1) {
    state = playerReducer(playerReducer(state, { type: "fatal", message: "又坏了" }), {
      type: "request",
      startMs: 0,
    });
    assert.equal(seen.has(state.attempt), false);
    seen.add(state.attempt);
  }
});

test("报错态是终态：video 元素的迟到事件顶不掉它", () => {
  // 解码失败后媒体元素并不安静，playing/ended/seeking 仍可能各自再飞一发。
  // 这些事件当时无条件改写 phase，报错浮层被顶掉——用户看到提示闪几秒，
  // 然后只剩一个黑播放器，不知道发生了什么、也没得选。
  const failed = run([
    { type: "request", startMs: 0 },
    { type: "fatal", message: "这一档的码流浏览器解不了" },
  ]);
  assert.equal(failed.phase, "error");
  for (const stray of [{ type: "playing" }, { type: "ended" }, { type: "seeking" }, { type: "buffering" }]) {
    const after = playerReducer(failed, stray);
    assert.equal(after.phase, "error", `${stray.type} 不该改写报错态`);
    assert.equal(after.error?.message, "这一档的码流浏览器解不了");
  }
});

test("同意弹窗同理：等用户拍板期间不被播放事件挤掉", () => {
  const consent = planSession(4);
  consent.decision = { ...consent.decision, outcome: "consent" };
  const waiting = run([{ type: "request", startMs: 0 }, { type: "session", session: consent }]);
  assert.equal(waiting.phase, "consent");
  for (const stray of [{ type: "playing" }, { type: "ended" }, { type: "seeking" }]) {
    assert.equal(playerReducer(waiting, stray).phase, "consent", `${stray.type} 不该关掉弹窗`);
  }
});

test("终态只有用户自己能离开", () => {
  const failed = run([
    { type: "request", startMs: 0 },
    { type: "fatal", message: "坏了" },
  ]);
  // 重试、换一集是用户动作，必须放行
  assert.equal(playerReducer(failed, { type: "request", startMs: 0 }).phase, "deciding");
  assert.equal(playerReducer(failed, { type: "reset" }).phase, "idle");
});

test("忙碌态覆盖起播四段与降档重来", () => {
  assert.equal(isBusy("deciding"), true);
  assert.equal(isBusy("degrading"), true);
  assert.equal(isBusy("playing"), false);
  assert.equal(isBusy("consent"), false); // 等用户拍板，不是转圈
});

// ---------------------------------------------------------------------------
// 时间轴
// ---------------------------------------------------------------------------

test("文件时间 = start_ms + currentTime", () => {
  assert.equal(toFileMs(30, 600_000), 630_000);
  assert.equal(toSessionSeconds(630_000, 600_000), 30);
  // 档 0 没有偏移，同一套函数通用
  assert.equal(toFileMs(30, 0), 30_000);
});

test("seek 落在已转区间内就地跳，越界才换会话", () => {
  const opts = { startMs: 600_000, seekableEndSeconds: 60, hasSession: true };
  assert.deepEqual(planSeek(630_000, opts), { kind: "native", seconds: 30 });
  // 往前拖出已转区间：干等 ffmpeg 追上来就是永远转不完的圈
  assert.deepEqual(planSeek(900_000, opts), { kind: "restart", startMs: 900_000 });
  // 往回拖到本次会话起点之前：那段根本不在这个会话里
  assert.deepEqual(planSeek(120_000, opts), { kind: "restart", startMs: 120_000 });
});

test("档 0 没有会话，怎么拖都是就地跳", () => {
  assert.deepEqual(
    planSeek(3_600_000, { startMs: 0, seekableEndSeconds: 60, hasSession: false }),
    { kind: "native", seconds: 3600 },
  );
});

test("片尾倒计时按剩余秒数而不是百分比", () => {
  // 3 小时的电影按 5% 算会提前 9 分钟弹倒计时
  assert.equal(isInEndCredits(10_780_000, 10_800_000), true);
  assert.equal(isInEndCredits(10_000_000, 10_800_000), false);
  assert.equal(isInEndCredits(1000, null), false);
});

test("时间显示是钟表格式，不足一小时不补出零点位", () => {
  assert.equal(formatClock(0), "0:00");
  assert.equal(formatClock(9_000), "0:09");
  assert.equal(formatClock(605_000), "10:05");
});

test("超过一小时补出小时位，分钟补零", () => {
  assert.equal(formatClock(3_600_000), "1:00:00");
  assert.equal(formatClock(3_965_000), "1:06:05");
});

test("片长未知时给占位而不是 NaN", () => {
  assert.equal(formatClock(Number.NaN), "--:--");
  assert.equal(formatClock(-1), "--:--");
});

test("startMs 为 null（服务端定起点）时，会话响应的 start_ms 落回状态", () => {
  const state = run([
    { type: "request", startMs: null },
    { type: "session", session: planSession(1, 120_000) },
  ]);
  assert.equal(state.startMs, 120_000);
  assert.equal(state.phase, "buffering");
});

test("null 起点只存在于首次请求在途期间：降档重来带的是解析后的位置", () => {
  const state = run([
    { type: "request", startMs: null },
    { type: "session", session: planSession(1, 120_000) },
    { type: "failed", reason: "测试失败" },
  ]);
  assert.equal(state.phase, "degrading");
  assert.equal(state.startMs, 120_000); // 不能又变回 null 从头放
});
