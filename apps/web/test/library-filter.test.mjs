import assert from "node:assert/strict";
import test from "node:test";

import { filterQuery, isFilterEmpty } from "../lib/library-filter.ts";
import { filterFingerprint, wallRecallScope } from "../lib/library-wall-recall.ts";

test("筛选条件序列化成三个接口共用的查询串", () => {
  const query = new URLSearchParams();
  filterQuery({ genres: [16, 878], countries: ["JP", "KR"], decades: ["2010s"], watch: "unwatched" }, query);
  assert.equal(query.get("g"), "16,878");
  assert.equal(query.get("c"), "JP,KR");
  assert.equal(query.get("d"), "2010s");
  assert.equal(query.get("w"), "unwatched");
});

test("空维度不写进查询串——未筛选的请求与改造前逐字相同", () => {
  const query = new URLSearchParams();
  filterQuery({ genres: [], countries: [], decades: [], watch: null }, query);
  assert.equal(query.size, 0);
  assert.equal(isFilterEmpty({ genres: [], watch: null }), true);
  assert.equal(isFilterEmpty(undefined), true);
  assert.equal(isFilterEmpty({ genres: [16] }), false);
  assert.equal(isFilterEmpty({ watch: "played" }), false);
});

test("筛选指纹与勾选顺序无关：同一组条件永远得到同一个键", () => {
  const a = filterFingerprint({ g: ["16", "878"], c: ["JP"], w: null });
  const b = filterFingerprint({ c: ["JP"], w: null, g: ["878", "16"] });
  assert.equal(a, b);
  assert.notEqual(a, filterFingerprint({ g: ["16"], c: ["JP"] }));
});

test("空筛选不加指纹——老的位置记录不会因为这次改动失效", () => {
  assert.equal(filterFingerprint({ g: [], c: [], d: [], w: null }), "");
  assert.equal(wallRecallScope(12, ""), "library:12");
  assert.equal(wallRecallScope(12), "library:12");
  assert.equal(wallRecallScope("favorites"), "library:favorites");
});

test("不同筛选态各记各的位置，互不串台", () => {
  const anime = wallRecallScope(12, filterFingerprint({ g: ["16"] }));
  const docs = wallRecallScope(12, filterFingerprint({ g: ["99"] }));
  assert.notEqual(anime, docs);
  assert.notEqual(anime, wallRecallScope(12));
});
