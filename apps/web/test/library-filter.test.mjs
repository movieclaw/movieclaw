import assert from "node:assert/strict";
import test from "node:test";

import { filterCount, filterQuery, isFilterEmpty } from "../lib/library-filter.ts";
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

test("二级维度也进查询串，且 isFilterEmpty 认得它们", () => {
  const query = new URLSearchParams();
  filterQuery(
    { ratingGte: 8, runtimes: ["gt120"], resolutions: ["2160p"], hdr: true, stock: ["missing"] },
    query,
  );
  assert.equal(query.get("rating_gte"), "8");
  assert.equal(query.get("rt"), "gt120");
  assert.equal(query.get("res"), "2160p");
  assert.equal(query.get("hdr"), "true");
  assert.equal(query.get("stock"), "missing");

  // 只选了二级维度也不算「空筛选」——曾经写错成恒真的判断，条件行会不显示
  assert.equal(isFilterEmpty({ ratingGte: 8 }), false);
  assert.equal(isFilterEmpty({ hdr: false }), false, "只看 SDR 也是一个条件");
  assert.equal(isFilterEmpty({ ratingGte: null, hdr: null }), true);
});

test("筛选条数统计覆盖全部十个维度（按取值数，不是维度数）", () => {
  assert.equal(filterCount(undefined), 0);
  assert.equal(
    filterCount({
      genres: [16, 18],
      countries: ["JP"],
      decades: ["2010s"],
      watch: "played",
      ratingGte: 8,
      runtimes: ["gt120"],
      languages: ["ja"],
      resolutions: ["2160p", "1080p"],
      hdr: true,
      stock: ["missing"],
    }),
    // 10 个维度、12 个取值：类型与分辨率各选了两个
    12,
  );
});
