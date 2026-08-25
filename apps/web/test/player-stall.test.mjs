import assert from "node:assert/strict";
import test from "node:test";

import {
  DECODE_STALL_MIN_BUFFER_S,
  MAX_NUDGES,
  NUDGE_AT_S,
  STALL_TIMEOUT_S,
  STARVE_TIMEOUT_S,
  bufferedAhead,
  classifyStall,
  shouldNudge,
  stallReason,
} from "../lib/player/stall.ts";

const base = {
  paused: false,
  ended: false,
  seeking: false,
  advanced: false,
  bufferedAhead: 10,
  stalledFor: 0,
};

test("正在前进就不是停顿", () => {
  assert.equal(classifyStall({ ...base, advanced: true, stalledFor: 99 }), "ok");
});

for (const flag of ["paused", "ended", "seeking"]) {
  test(`${flag} 期间不算停顿——用户自己按的暂停不该被当成故障`, () => {
    assert.equal(classifyStall({ ...base, [flag]: true, stalledFor: 99 }), "ok");
  });
}

test("缓冲里有数据却放不动 = 解码器卡死，尽快降档", () => {
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 10, stalledFor: STALL_TIMEOUT_S }),
    "decode-stalled",
  );
});

test("解码卡死的判定不能太急，免得把网络抖动当故障", () => {
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 10, stalledFor: STALL_TIMEOUT_S - 1 }),
    "ok",
  );
});

test("缓冲耗尽 = 在等上游供流，短时间内绝不判失败", () => {
  // 这正是「软件转码 4K，编码慢于实时播放」的现场：判成失败会触发降档，
  // 而档 4 已是最低，用户看到的是「所有播放方式都失败了」——真实原因只是
  // 服务器转得慢。
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 0, stalledFor: STALL_TIMEOUT_S }),
    "ok",
  );
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 0, stalledFor: STARVE_TIMEOUT_S - 1 }),
    "ok",
  );
});

test("供流也不能无限等：会话半路死掉同样是缓冲耗尽后再无新数据", () => {
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 0, stalledFor: STARVE_TIMEOUT_S }),
    "starved",
  );
});

test("供流的容忍度必须远大于解码卡死", () => {
  assert.ok(STARVE_TIMEOUT_S > STALL_TIMEOUT_S * 3);
});

test("零点几秒的前方缓冲视同没有", () => {
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 0.2, stalledFor: STALL_TIMEOUT_S }),
    "ok",
  );
});

test("两种原因给的是不同的中文说法", () => {
  const decode = stallReason("decode-stalled");
  const starve = stallReason("starved");
  assert.notEqual(decode, starve);
  assert.match(decode, /吃不下/);
  assert.match(starve, /转码速度跟不上|转码已中断/);
});

function fakeVideo(currentTime, ranges) {
  return {
    currentTime,
    buffered: {
      length: ranges.length,
      start: (i) => ranges[i][0],
      end: (i) => ranges[i][1],
    },
  };
}

test("前方缓冲取的是当前所在的那段", () => {
  assert.equal(bufferedAhead(fakeVideo(5, [[0, 12]])), 7);
});

test("落在缓冲空洞里记 0", () => {
  // seek 到还没下载的区间：前方没有数据，属于"等供流"而不是"解码卡死"
  assert.equal(bufferedAhead(fakeVideo(20, [[0, 12], [30, 40]])), 0);
});

test("没有任何缓冲记 0", () => {
  assert.equal(bufferedAhead(fakeVideo(0, [])), 0);
});

test("前方剩一两秒卡住是「追上了转码器」，不是解码卡死——按缺粮长限等", () => {
  // 转码会话里 buffered 尾 = 已转出的全部，播放头贴着尾巴跑时前方常剩
  // 0.5~2 秒。烧录/软转会话起步慢，按 8 秒解码卡死判会误杀降档（真机踩中：
  // 「选个 PGS 字幕先给我降了一档」）。
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 1.5, stalledFor: STALL_TIMEOUT_S }),
    "ok",
  );
  assert.equal(
    classifyStall({ ...base, bufferedAhead: 1.5, stalledFor: STARVE_TIMEOUT_S }),
    "starved",
  );
});

test("解码卡死要求前方缓冲至少 DECODE_STALL_MIN_BUFFER_S", () => {
  assert.equal(
    classifyStall({ ...base, bufferedAhead: DECODE_STALL_MIN_BUFFER_S, stalledFor: STALL_TIMEOUT_S }),
    "decode-stalled",
  );
});

// ---------------------------------------------------------------------------
// 推一把（nudge）：iOS AVPlayer「有数据却楞住」的 wedge 先踢再判死
// ---------------------------------------------------------------------------

const nudgeBase = {
  stalledFor: NUDGE_AT_S,
  nudges: 0,
  bufferedAhead: 8,
  readyState: 3,
  everAdvanced: true,
};

test("播起来过之后有数据卡满 3 秒且还有推动额度 → 推一把", () => {
  assert.equal(shouldNudge({ ...nudgeBase }), true);
});

test("卡的时间不足推动起点 → 不推", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: NUDGE_AT_S - 1 }), false);
});

test("推满次数后不再推——让 decode-stalled 判定接手降档", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: 99, nudges: MAX_NUDGES }), false);
});

test("前方缓冲不足（追上编码器）不推——那是缺粮不是 wedge", () => {
  assert.equal(
    shouldNudge({ ...nudgeBase, stalledFor: 99, bufferedAhead: DECODE_STALL_MIN_BUFFER_S - 1 }),
    false,
  );
});

test("从未真正播起来过绝不推——起播预滚被推动打断是真机踩过的回归", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: 99, everAdvanced: false }), false);
});

test("readyState 不足 HAVE_FUTURE_DATA（还在预滚）绝不推", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: 99, readyState: 2 }), false);
});
