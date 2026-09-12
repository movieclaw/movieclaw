import assert from "node:assert/strict";
import test from "node:test";

import {
  SEEK_TAIL_GUARD_MS,
  livePositionMs,
  clampSeekTarget,
  planSeek,
  progressRatio,
  shownPositionMs,
  toFileMs,
  toSessionSeconds,
} from "../lib/player/timeline.ts";

// ---------------------------------------------------------------------------
// 会话时间轴 ↔ 文件时间轴
// ---------------------------------------------------------------------------

test("会话相对制：文件时间 = start_ms + currentTime", () => {
  assert.equal(toFileMs(30, 600_000), 630_000);
  assert.equal(toSessionSeconds(630_000, 600_000), 30);
});

test("往回拖到会话起点之前，换算结果为负且不夹到 0", () => {
  // 夹成 0 会让「拖到 20 分钟」变成静默跳回会话开头，且没有任何提示；
  // planSeek 正是靠这个负数判断要不要换会话。
  assert.equal(toSessionSeconds(60_000, 600_000), -540);
});

// ---------------------------------------------------------------------------
// 跳转落点的夹紧
// ---------------------------------------------------------------------------

test("越过片尾的落点夹回片长之内，留出片尾余量", () => {
  // 片尾连按快进、或把进度条拖到最右端都会给出这种目标。不夹的话换会话
  // 那条路会开出一个 -ss 落在文件末尾的会话，ffmpeg 一帧都转不出来。
  assert.equal(clampSeekTarget(7_200_000, 7_200_000), 7_200_000 - SEEK_TAIL_GUARD_MS);
  assert.equal(clampSeekTarget(9_999_999, 7_200_000), 7_200_000 - SEEK_TAIL_GUARD_MS);
});

test("片长之内的落点原样通过", () => {
  assert.equal(clampSeekTarget(1_800_000, 7_200_000), 1_800_000);
});

test("负数落点夹到 0——快退键在开头会一路减到负值", () => {
  assert.equal(clampSeekTarget(-5_000, 7_200_000), 0);
  assert.equal(clampSeekTarget(-5_000, null), 0);
});

test("片长未知时不夹：那时进度条本来就禁用，越界交给浏览器收住", () => {
  assert.equal(clampSeekTarget(9_999_999, null), 9_999_999);
  assert.equal(clampSeekTarget(9_999_999, 0), 9_999_999);
});

test("极短片子夹到 0 而不是负数", () => {
  assert.equal(clampSeekTarget(900, 800), 0);
});

// ---------------------------------------------------------------------------
// 一次 seek 该走哪条路
// ---------------------------------------------------------------------------

const session = { startMs: 600_000, seekableEndSeconds: 120, hasSession: true };

test("没有会话（档 0 / VOD 全片列表）永远就地跳", () => {
  assert.deepEqual(planSeek(7_000_000, { ...session, hasSession: false }), {
    kind: "native",
    seconds: 6400,
  });
});

test("落在已转区间之内就地跳", () => {
  assert.deepEqual(planSeek(660_000, session), { kind: "native", seconds: 60 });
});

test("拖出已转区间要换会话——干等 ffmpeg 追上来是转不完的圈", () => {
  assert.deepEqual(planSeek(1_800_000, session), { kind: "restart", startMs: 1_800_000 });
});

test("往回拖到本次会话起点之前也要换会话", () => {
  assert.deepEqual(planSeek(60_000, session), { kind: "restart", startMs: 60_000 });
});

test("夹紧之后的片尾落点仍落在已转区间内，不会白换一次会话", () => {
  // 会话从 0 起、整片都已转出时，拖到最右端应当就地跳。
  const whole = { startMs: 0, seekableEndSeconds: 7200, hasSession: true };
  const target = clampSeekTarget(7_200_000, 7_200_000);
  assert.deepEqual(planSeek(target, whole), { kind: "native", seconds: 7199 });
});

// ---------------------------------------------------------------------------
// 进度条比例（player-feel.md §2.A1：进度条自绘用的唯一换算）
// ---------------------------------------------------------------------------

test("进度比例：正常值按比例，两端夹住", () => {
  assert.equal(progressRatio(30_000, 120_000), 0.25);
  assert.equal(progressRatio(-5_000, 120_000), 0);
  // 换会话的空档里位置可能短暂越过片长，不夹住就是把圆点画到轨道外面
  assert.equal(progressRatio(130_000, 120_000), 1);
});

test("片长未知/非法时比例为 0，不画一条随机长度的已播段", () => {
  assert.equal(progressRatio(30_000, null), 0);
  assert.equal(progressRatio(30_000, 0), 0);
  assert.equal(progressRatio(Number.NaN, 120_000), 0);
});

// ---------------------------------------------------------------------------
// 位置读数的唯一取值规则（player-feel.md §13：写两遍就会各说各话）
// ---------------------------------------------------------------------------

test("拖动中一切听手指的：拖动值压过其余三个来源", () => {
  assert.equal(
    shownPositionMs({ draggingMs: 5_000, overrideMs: 9_000, livePositionMs: 1_000, positionMs: 2_000 }),
    5_000,
  );
});

test("落点（横滑/连按）压过真实播放位置：用户已经表达了意图，画面还没跳过去", () => {
  assert.equal(
    shownPositionMs({ draggingMs: null, overrideMs: 9_000, livePositionMs: 1_000, positionMs: 2_000 }),
    9_000,
  );
});

test("没有落点时用正在播的真实位置——它才是每帧都在变的那个", () => {
  assert.equal(
    shownPositionMs({ draggingMs: null, overrideMs: null, livePositionMs: 1_234, positionMs: 2_000 }),
    1_234,
  );
});

test("真实位置不可用（暂停 / seek 途中 / 换会话空档）时退回状态值", () => {
  // 换会话时 video 还挂着旧流，读它会让进度条先弹回原处再跳过去
  assert.equal(
    shownPositionMs({ draggingMs: null, overrideMs: null, livePositionMs: null, positionMs: 2_000 }),
    2_000,
  );
});

test("0 是合法位置，不能被当成「没有值」", () => {
  assert.equal(
    shownPositionMs({ draggingMs: 0, overrideMs: 9_000, livePositionMs: 1_000, positionMs: 2_000 }),
    0,
  );
  assert.equal(
    shownPositionMs({ draggingMs: null, overrideMs: null, livePositionMs: 0, positionMs: 2_000 }),
    0,
  );
});

// ---------------------------------------------------------------------------
// 进度条自绘的 live 取值：换会话的空档没有参照点
// ---------------------------------------------------------------------------

/** 一路正在播的 video：会话起点 40 分钟，已经放到会话内第 30 秒 */
const playing = (overrides = {}) => ({
  originMs: 2_400_000,
  currentTimeSeconds: 30,
  paused: false,
  seeking: false,
  readyState: 4,
  ...overrides,
});

test("正在播：live = 参照点 + currentTime", () => {
  assert.equal(livePositionMs(playing()), 2_430_000);
});

test("回归：换会话的空档没有参照点，live 必须是 null", () => {
  // 反馈 2026-09-12：拖进度条拖很远会换会话（横滑封顶 ±90 秒，落点几乎总在
  // 已转区间内，走的是 native，所以横滑看不到这个问题）。换会话时
  // `state.session` 被摘成 null，`mode` 跟着变 null，而进度条的参照点写的是
  // `mode?.originMs ?? 0`——**静默掉回 0**。此刻文字读数走 positionMs（文件
  // 时间），进度条算的是 0 + currentTime，两个读数落在两条不同的时间轴上：
  // 起点在 40 分钟处就差 40 分钟。
  assert.equal(livePositionMs(playing({ originMs: null })), null);
  // 掉回 0 的话会算出这个数——与文字读数的 2_430_000 差了整整一个会话起点
  assert.equal(livePositionMs(playing({ originMs: 0 })), 30_000);
});

test("暂停 / seek 途中 / 数据不够时 live 不可用，退回状态值", () => {
  assert.equal(livePositionMs(playing({ paused: true })), null);
  assert.equal(livePositionMs(playing({ seeking: true })), null);
  assert.equal(livePositionMs(playing({ readyState: 1 })), null);
  // readyState 2 = HAVE_CURRENT_DATA，够了
  assert.equal(livePositionMs(playing({ readyState: 2 })), 2_430_000);
});

test("currentTime 不是有限数（刚挂流的一瞬）同样不作数", () => {
  assert.equal(livePositionMs(playing({ currentTimeSeconds: Number.NaN })), null);
});

test("参照点为 0 是合法的（档 0 直出 / VOD 全片列表），不要与 null 混为一谈", () => {
  // 0 是「文件绝对制」的真实参照点，null 才是「没有参照点」
  assert.equal(livePositionMs(playing({ originMs: 0, currentTimeSeconds: 12 })), 12_000);
});

test("换会话空档里两个读数必须同源：live 为 null 时 shownPositionMs 退回 positionMs", () => {
  const positionMs = 2_430_000; // 文字读数：落点（文件时间）
  const live = livePositionMs(playing({ originMs: null }));
  const bar = shownPositionMs({ draggingMs: null, overrideMs: null, livePositionMs: live, positionMs });
  const text = shownPositionMs({ draggingMs: null, overrideMs: null, livePositionMs: null, positionMs });
  assert.equal(bar, text, "换会话空档里进度条与时间文字仍然打架");
});

