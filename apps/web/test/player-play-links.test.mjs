import assert from "node:assert/strict";
import test from "node:test";

import { parseUnitSegment, playHref } from "../lib/player/play-links.ts";

test("电影地址只有条目 id", () => {
  assert.equal(playHref(127), "/play/127");
});

test("剧集地址带补零的 sXXeYY 段", () => {
  assert.equal(playHref(127, { season: 1, episode: 3 }), "/play/127/s01e03");
  assert.equal(playHref(127, { season: 12, episode: 345 }), "/play/127/s12e345");
});

test("季集必须成对：缺一个就按电影出——半个单元没有意义", () => {
  assert.equal(playHref(127, { season: 1 }), "/play/127");
  assert.equal(playHref(127, { episode: 3 }), "/play/127");
});

test("t 参数（秒）拼进查询串，0 与负数不拼——从头播不需要说明", () => {
  assert.equal(playHref(127, { season: 1, episode: 3, tSeconds: 1520 }), "/play/127/s01e03?t=1520");
  assert.equal(playHref(127, { tSeconds: 95.7 }), "/play/127?t=95");
  assert.equal(playHref(127, { tSeconds: 0 }), "/play/127");
});

test("解析 sXXeYY：大小写不敏感，前导零可省", () => {
  assert.deepEqual(parseUnitSegment("s01e03"), { season: 1, episode: 3 });
  assert.deepEqual(parseUnitSegment("S1E3"), { season: 1, episode: 3 });
  assert.deepEqual(parseUnitSegment("s12e345"), { season: 12, episode: 345 });
});

test("解析不动就当电影播——地址是人手打的，别苛刻", () => {
  assert.equal(parseUnitSegment("season1"), null);
  assert.equal(parseUnitSegment("s1"), null);
  assert.equal(parseUnitSegment("e3"), null);
  assert.equal(parseUnitSegment(""), null);
  assert.equal(parseUnitSegment(undefined), null);
});
