import assert from "node:assert/strict";
import test from "node:test";

import {
  STALL_TIMEOUT_S,
  STARVE_TIMEOUT_S,
  bufferedAhead,
  classifyStall,
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
