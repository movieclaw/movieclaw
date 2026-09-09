import assert from "node:assert/strict";
import test from "node:test";

import {
  filterCount,
  filterKey,
  filterQuery,
  filterToRules,
  isFilterEmpty,
  rulesToFilter,
} from "../lib/library-filter.ts";
import { wallRecallScope } from "../lib/library-wall-recall.ts";

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

test("条件的规范化键与勾选顺序无关：同一组条件永远得到同一个键", () => {
  const a = filterKey({ genres: [16, 878], countries: ["JP"], watch: null });
  const b = filterKey({ countries: ["JP"], watch: null, genres: [878, 16] });
  assert.equal(a, b);
  assert.notEqual(a, filterKey({ genres: [16], countries: ["JP"] }));
});

test("空筛选不加指纹——老的位置记录不会因为这次改动失效", () => {
  assert.equal(filterKey({ genres: [], countries: [], decades: [], watch: null }), "");
  assert.equal(filterKey(undefined), "");
  assert.equal(wallRecallScope(12, ""), "library:12");
  assert.equal(wallRecallScope(12), "library:12");
  assert.equal(wallRecallScope("favorites"), "library:favorites");
});

test("不同筛选态各记各的位置，互不串台", () => {
  const anime = wallRecallScope(12, filterKey({ genres: [16] }));
  const docs = wallRecallScope(12, filterKey({ genres: [99] }));
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

test("筛选 ↔ 合集规则是一次可逆的形状转换", () => {
  const filter = {
    genres: [16],
    countries: ["JP"],
    decades: ["2010s"],
    watch: "unwatched",
    ratingGte: 8,
    resolutions: ["2160p"],
    hdr: true,
  };
  const rules = filterToRules(filter);
  // 字段名必须与后端 rules_to_filter 那份一一对上，否则规则会被静默忽略
  assert.deepEqual(
    rules.map((r) => r.field).sort(),
    ["decades", "genres", "hdr", "origin_countries", "rating_gte", "resolutions", "watch"],
  );
  assert.equal(filterKey(rulesToFilter(rules)), filterKey(filter));
});

test("未知规则字段保守忽略——新版本写的合集被旧代码读到时不误收窄", () => {
  assert.equal(isFilterEmpty(rulesToFilter([{ field: "从未见过的字段", op: "any_of", values: ["x"] }])), true);
});

test("同一组条件的合集与当前墙对得上，改一条就对不上了", () => {
  // 合集 chip 的选中态是这么推导出来的：不存状态，也就没有谁需要记得清掉它
  const collectionRules = filterToRules({ genres: [16], countries: ["JP"] });
  const wall = { countries: ["JP"], genres: [16] };
  assert.equal(filterKey(rulesToFilter(collectionRules)), filterKey(wall));
  assert.notEqual(filterKey(rulesToFilter(collectionRules)), filterKey({ ...wall, decades: ["2010s"] }));
});
