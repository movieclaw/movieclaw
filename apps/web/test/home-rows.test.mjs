import assert from "node:assert/strict";
import test from "node:test";

import {
  buildHomeRows,
  moveRowTo,
  newCollectionRow,
  newLibraryRow,
  newRowId,
  rowMeta,
  rowTitle,
  rowsToPrefs,
  sortPresetsFor,
} from "../lib/home-rows.ts";

const lib = (id, name, kind = "movie", extra = {}) => ({
  id,
  name,
  kind,
  viewer_access: true,
  exclude_from_home: false,
  ...extra,
});
const LIBS = [lib(1, "电影"), lib(2, "剧集", "tv"), lib(3, "动漫", "tv")];
const COLS = [
  { id: 7, name: "宫崎骏", library_id: 3, sort: "release_date_asc" },
  { id: 8, name: "诺兰", library_id: 1, sort: "title" },
];
const ids = (rows) => rows.map((row) => row.id);

test("空清单 = 出厂布局：三个内置行 + 每库一行最近添加", () => {
  const rows = buildHomeRows({ rows: [] }, LIBS, COLS);
  assert.deepEqual(ids(rows), ["up-next", "favorites", "libraries", "lib:1", "lib:2", "lib:3"]);
  assert.equal(rowTitle(rows[3]), "最近添加的电影");
  assert.equal(rows[3].sort, "added_at");
  assert.equal(rows[3].builtin, true);
});

test("存过的按存的顺序在前，没存过的内置行与默认库行补在后面", () => {
  const rows = buildHomeRows(
    { rows: [{ id: "lib:2" }, { id: "up-next" }] },
    LIBS,
    COLS,
  );
  // lib:1 / lib:3 是没存过的库行：插在最后一条库行（lib:2）之后；内置行追加在末尾
  assert.deepEqual(ids(rows), ["lib:2", "lib:1", "lib:3", "up-next", "favorites", "libraries"]);
});

test("新库的默认行插在最后一条库行之后，不落到合集行后面", () => {
  const rows = buildHomeRows(
    {
      rows: [
        { id: "up-next" },
        { id: "lib:1", sort: "release_date" },
        { id: "row:c", collection_id: 7 },
      ],
    },
    LIBS,
    COLS,
  );
  assert.deepEqual(ids(rows), ["up-next", "lib:1", "lib:2", "lib:3", "row:c", "favorites", "libraries"]);
});

test("指向已删库 / 不可见合集 / 仅管理的库的行静默消失", () => {
  const rows = buildHomeRows(
    {
      rows: [
        { id: "lib:99" },
        { id: "row:a", library_id: 99 },
        { id: "row:b", collection_id: 404 },
        { id: "row:c", collection_id: 7 },
        { id: "lib:4" },
      ],
    },
    [...LIBS, lib(4, "仅管理", "movie", { viewer_access: false })],
    COLS,
  );
  assert.deepEqual(ids(rows), ["row:c", "up-next", "favorites", "libraries", "lib:1", "lib:2", "lib:3"]);
});

test("从首页排除的库不给默认行，用户自己加的行仍尊重", () => {
  const libs = [lib(1, "电影"), lib(2, "家庭录像", "video", { exclude_from_home: true })];
  assert.deepEqual(ids(buildHomeRows({ rows: [] }, libs, [])), [
    "up-next",
    "favorites",
    "libraries",
    "lib:1",
  ]);
  const rows = buildHomeRows({ rows: [{ id: "row:v", library_id: 2 }] }, libs, []);
  assert.equal(rows[0].id, "row:v");
  assert.equal(rowTitle(rows[0]), "最近添加的家庭录像");
});

test("隐藏保留位置；名字为空跟随排序推荐，手输过就不动", () => {
  const rows = buildHomeRows(
    {
      rows: [
        { id: "up-next", hidden: true },
        { id: "lib:1", sort: "rating", unwatched: true },
        { id: "row:x", library_id: 3, sort: "rating", name: "周末补番" },
      ],
    },
    LIBS,
    COLS,
  );
  assert.equal(rows[0].hidden, true);
  assert.equal(rowTitle(rows[1]), "评分最高的电影");
  assert.equal(rowMeta(rows[1]), "电影库 · 评分最高 · 只看没看过的");
  assert.equal(rowTitle(rows[2]), "周末补番");
});

test("排序档不在库 kind 的预设里时回落到最近添加", () => {
  const libs = [lib(5, "照片", "photo")];
  const rows = buildHomeRows({ rows: [{ id: "lib:5", sort: "rating" }] }, libs, []);
  assert.equal(rows[0].sort, "added_at");
  assert.deepEqual(sortPresetsFor("photo"), ["added_at", "title", "random"]);
});

test("合集行的排序：存了用存的，没存用合集自己的，都不认识回落到最近添加", () => {
  const rows = buildHomeRows(
    {
      rows: [
        { id: "row:a", collection_id: 7 },
        { id: "row:b", collection_id: 8, sort: "random" },
      ],
    },
    LIBS,
    [...COLS, { id: 9, name: "怪", library_id: null, sort: "probing" }],
  );
  assert.equal(rows[0].sort, "release_date_asc");
  assert.equal(rows[1].sort, "random");
  assert.equal(rowTitle(rows[0]), "宫崎骏");
  assert.equal(newCollectionRow({ id: 9, name: "怪", library_id: null, sort: "probing" }, "row:z").sort, "added_at");
});

test("收藏行只认自己的四档，其余回落到未看优先", () => {
  const rows = buildHomeRows({ rows: [{ id: "favorites", sort: "random" }] }, LIBS, COLS);
  assert.equal(rows[0].sort, "unwatched_first");
});

test("写回时只存与默认不同的字段，默认库行不带来源", () => {
  const rows = buildHomeRows(
    {
      rows: [
        { id: "favorites", sort: "rating" },
        { id: "lib:1", sort: "release_date", hidden: true },
        { id: "row:x", library_id: 3, sort: "rating", unwatched: true, name: "周末补番" },
        { id: "row:c", collection_id: 7 },
      ],
    },
    LIBS,
    COLS,
  );
  const picked = ["favorites", "lib:1", "row:x", "row:c"].map((id) => rows.find((row) => row.id === id));
  assert.deepEqual(rowsToPrefs(picked), [
    { id: "favorites", sort: "rating" },
    { id: "lib:1", hidden: true, sort: "release_date" },
    { id: "row:x", library_id: 3, sort: "rating", unwatched: true, name: "周末补番" },
    { id: "row:c", collection_id: 7, sort: "release_date_asc" },
  ]);
});

test("新行 id 是 row: 加 6 位 base36；拖到某一位越界原样返回", () => {
  assert.match(newRowId(), /^row:[0-9a-z]{6}$/);
  assert.equal(newRowId(() => 0), "row:000000");
  const rows = buildHomeRows({ rows: [] }, LIBS, COLS);
  assert.deepEqual(ids(moveRowTo(rows, 0, -1)), ids(rows));
  assert.deepEqual(ids(moveRowTo(rows, 5, 0)).slice(0, 3), ["lib:3", "up-next", "favorites"]);
  assert.deepEqual(ids(moveRowTo(rows, 3, 2)).slice(0, 4), ["up-next", "favorites", "lib:1", "libraries"]);
  assert.equal(newLibraryRow(LIBS[0], "row:abc").id, "row:abc");
});

test("管理员「从首页排除」的库：存过的默认行也不出现，用户自加的行仍尊重", () => {
  const libs = [lib(1, "电影"), lib(2, "家庭录像", "video", { exclude_from_home: true })];
  const rows = buildHomeRows(
    { rows: [{ id: "lib:2", sort: "title" }, { id: "row:v", library_id: 2 }, { id: "lib:1" }] },
    libs,
    [],
  );
  assert.deepEqual(ids(rows), ["row:v", "lib:1", "up-next", "favorites", "libraries"]);
});

test("「最近观看」与「只看没看过的」互斥：以排序为准，开关作废", () => {
  const rows = buildHomeRows(
    { rows: [{ id: "lib:1", sort: "last_played", unwatched: true }] },
    LIBS,
    COLS,
  );
  assert.equal(rows[0].unwatched, false);
  assert.deepEqual(rowsToPrefs([rows[0]]), [{ id: "lib:1", sort: "last_played" }]);
});

test("一条库行都没有时，新库的默认行插在「我的媒体库」之后，不落到合集行后面", () => {
  const rows = buildHomeRows(
    { rows: [{ id: "up-next" }, { id: "libraries" }, { id: "row:c", collection_id: 7 }] },
    LIBS,
    COLS,
  );
  assert.deepEqual(ids(rows), ["up-next", "libraries", "lib:1", "lib:2", "lib:3", "row:c", "favorites"]);
});
