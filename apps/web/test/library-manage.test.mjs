import assert from "node:assert/strict";
import test from "node:test";

import {
  accessLabel,
  filterIsActive,
  filterLibraries,
  inventoryLabel,
  libraryIsBusy,
  libraryStatus,
  moveInList,
} from "../lib/library-manage.ts";

const PHASES = {
  walking: "正在盘点文件",
  ingesting: "正在扫描",
  probing: "正在补探画质与音轨",
  assets: "正在补齐海报与剧照",
  reidentifying: "正在重新识别条目",
  organizing: "正在整理文件名",
};
const ctx = { phaseLabels: PHASES, relativeTime: () => "2 小时前" };

function lib(overrides = {}) {
  return {
    id: 1,
    name: "电影",
    kind: "movie",
    viewer_access: true,
    access_mode: "everyone",
    admin_visible: true,
    member_ids: [],
    root_paths: ["/remote/media/电影"],
    realtime_watch: true,
    stats: { item_count: 10, file_count: 12, unidentified_count: 0, missing_count: 0 },
    scanning: false,
    scan_progress: null,
    organizing: false,
    organize_progress: null,
    metadata_refresh: null,
    last_scan: { finished_at: "2026-09-05T00:00:00+00:00", deferred: 0 },
    ...overrides,
  };
}

test("扫描中：阶段词 + 百分比，分子分母进第二行", () => {
  const s = libraryStatus(
    lib({ scanning: true, scan_progress: { phase: "ingesting", processed: 42, total: 100 } }),
    ctx,
  );
  assert.equal(s.tone, "busy");
  assert.equal(s.kind, "scan");
  assert.equal(s.title, "正在扫描 42%");
  assert.equal(s.detail, "42 / 100");
  assert.equal(s.percent, 42);
});

test("扫描盘点阶段分母未知：不给百分比", () => {
  const s = libraryStatus(
    lib({ scanning: true, scan_progress: { phase: "walking", processed: 0, total: 0 } }),
    ctx,
  );
  assert.equal(s.title, "正在盘点文件");
  assert.equal(s.percent, null);
});

test("整理中优先于待识别", () => {
  const s = libraryStatus(
    lib({
      organizing: true,
      organize_progress: { phase: "organizing", processed: 5, total: 10 },
      stats: { item_count: 1, file_count: 1, unidentified_count: 3, missing_count: 0 },
    }),
    ctx,
  );
  assert.equal(s.kind, "organize");
  assert.equal(s.percent, 50);
});

test("刷新元数据：带当前条目与阶段", () => {
  const s = libraryStatus(
    lib({
      metadata_refresh: {
        refreshing: true,
        processed: 12,
        total: 100,
        failed: 0,
        stopping: false,
        active: [{ media_item_id: 1, title: "银翼杀手 2049", phase: "下载海报" }],
      },
    }),
    ctx,
  );
  assert.equal(s.kind, "refresh");
  assert.equal(s.title, "刷新元数据 12%");
  assert.equal(s.detail, "正在处理「银翼杀手 2049」· 下载海报");
});

test("写入中暂缓入账：入库中", () => {
  const s = libraryStatus(lib({ last_scan: { finished_at: "x", deferred: 3 } }), ctx);
  assert.equal(s.kind, "importing");
  assert.equal(s.tone, "busy");
  assert.equal(s.title, "3 个新文件入库中");
});

test("有缺失压过待识别，两者并列写出", () => {
  const s = libraryStatus(
    lib({ stats: { item_count: 1, file_count: 1, unidentified_count: 12, missing_count: 2 } }),
    ctx,
  );
  assert.equal(s.tone, "missing");
  assert.equal(s.title, "12 个待识别 · 2 个缺失");
  assert.equal(s.detail, "最近扫描 2 小时前");
});

test("只有待识别：黄色", () => {
  const s = libraryStatus(
    lib({ stats: { item_count: 1, file_count: 1, unidentified_count: 4, missing_count: 0 } }),
    ctx,
  );
  assert.equal(s.tone, "pending");
  assert.equal(s.title, "4 个待识别");
});

test("空闲：最近扫描 + 实时监控开关", () => {
  const s = libraryStatus(lib({ realtime_watch: false }), ctx);
  assert.equal(s.tone, "idle");
  assert.equal(s.title, "空闲");
  assert.equal(s.detail, "最近扫描 2 小时前 · 实时监控关");
  assert.equal(libraryStatus(lib({ last_scan: null }), ctx).detail, "尚未扫描 · 实时监控开");
});

test("筛选：类型、搜索词（库名或根目录）、在跑任务", () => {
  const libs = [
    lib({ id: 1, name: "电影", kind: "movie" }),
    lib({ id: 2, name: "剧集", kind: "tv", scanning: true, root_paths: ["/mnt/nas2/剧集"] }),
    lib({ id: 3, name: "演唱会", kind: "video", root_paths: ["/remote/media/演唱会"] }),
  ];
  const ids = (r) => r.map((l) => l.id);
  assert.deepEqual(ids(filterLibraries(libs, { query: "", kind: null, busyOnly: false })), [1, 2, 3]);
  assert.deepEqual(ids(filterLibraries(libs, { query: "", kind: "tv", busyOnly: false })), [2]);
  assert.deepEqual(ids(filterLibraries(libs, { query: "NAS2", kind: null, busyOnly: false })), [2]);
  assert.deepEqual(ids(filterLibraries(libs, { query: "演唱", kind: null, busyOnly: false })), [3]);
  assert.deepEqual(ids(filterLibraries(libs, { query: "", kind: null, busyOnly: true })), [2]);
  assert.deepEqual(ids(filterLibraries(libs, { query: "电影", kind: "tv", busyOnly: false })), []);
  assert.equal(filterIsActive({ query: "  ", kind: null, busyOnly: false }), false);
  assert.equal(filterIsActive({ query: "", kind: "movie", busyOnly: false }), true);
});

test("在跑任务判定覆盖三种长任务", () => {
  assert.equal(libraryIsBusy(lib()), false);
  assert.equal(libraryIsBusy(lib({ scanning: true })), true);
  assert.equal(libraryIsBusy(lib({ organizing: true })), true);
  assert.equal(
    libraryIsBusy(lib({ metadata_refresh: { refreshing: true, processed: 0, total: 0, failed: 0, stopping: false, active: [] } })),
    true,
  );
});

test("换位：向后、向前、越界与原地", () => {
  const list = ["a", "b", "c", "d"];
  assert.deepEqual(moveInList(list, 0, 2), ["b", "c", "a", "d"]);
  assert.deepEqual(moveInList(list, 3, 1), ["a", "d", "b", "c"]);
  assert.equal(moveInList(list, 1, 1), list);
  assert.equal(moveInList(list, 0, 4), list);
  assert.equal(moveInList(list, -1, 0), list);
});

test("可见范围与库存文案", () => {
  assert.equal(accessLabel(lib()), "全员");
  assert.equal(accessLabel(lib({ access_mode: "selected", member_ids: [1, 2], admin_visible: false })), "指定成员 2");
  assert.equal(accessLabel(lib({ access_mode: "selected", member_ids: [1], admin_visible: true })), "指定成员 2");
  assert.equal(accessLabel(lib({ viewer_access: false })), "仅管理");
  assert.deepEqual(inventoryLabel(lib()), { primary: "10 部", secondary: "12 个文件" });
  assert.deepEqual(inventoryLabel(lib({ kind: "video" })), { primary: "10 个条目", secondary: "12 个文件" });
});
