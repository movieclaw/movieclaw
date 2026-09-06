import assert from "node:assert/strict";
import test from "node:test";

import {
  accessLabel,
  accessRestricted,
  chapterJobLabel,
  configNotes,
  filterIsActive,
  filterLibraries,
  inventoryLabel,
  libraryIsBusy,
  libraryNeedsAttention,
  libraryStatus,
  moveInList,
  summarizeLibraries,
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
    exclude_from_home: false,
    capabilities: { scraped: true, naming: true },
    match_rules: [{ field: "genres", op: "any_of", values: [28] }],
    stats: {
      item_count: 10,
      file_count: 12,
      total_size_bytes: 1024 ** 3,
      unidentified_count: 0,
      missing_count: 0,
    },
    scanning: false,
    scan_progress: null,
    organizing: false,
    organize_progress: null,
    metadata_refresh: null,
    chapter_job: null,
    last_scan: {
      finished_at: "2026-09-05T00:00:00+00:00",
      scanned: 0,
      marked_missing: 0,
      cancelled: false,
      deferred: 0,
    },
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
  assert.equal(s.detail, "最近扫描 2 小时前 · 无新文件");
});

test("只有待识别：黄色", () => {
  const s = libraryStatus(
    lib({ stats: { item_count: 1, file_count: 1, unidentified_count: 4, missing_count: 0 } }),
    ctx,
  );
  assert.equal(s.tone, "pending");
  assert.equal(s.title, "4 个待识别");
});

test("空闲：只留最近扫描这行事实，实时监控开关不写进状态", () => {
  const s = libraryStatus(lib({ realtime_watch: false }), ctx);
  assert.equal(s.tone, "idle");
  assert.equal(s.title, "空闲");
  assert.equal(s.detail, "最近扫描 2 小时前 · 无新文件");
  assert.equal(libraryStatus(lib({ last_scan: null }), ctx).detail, "尚未扫描");
});

test("最近扫描的结论：新增 / 标记缺失 / 手动停止，都没有才写无新文件", () => {
  const scan = (over) => ({ finished_at: "x", scanned: 0, marked_missing: 0, cancelled: false, deferred: 0, ...over });
  const detail = (over) => libraryStatus(lib({ last_scan: scan(over) }), ctx).detail;
  assert.equal(detail({ scanned: 3 }), "最近扫描 2 小时前 · 新增 3 个文件");
  assert.equal(detail({ scanned: 3, marked_missing: 2 }), "最近扫描 2 小时前 · 新增 3 个文件 · 标记缺失 2");
  assert.equal(detail({ marked_missing: 1 }), "最近扫描 2 小时前 · 标记缺失 1");
  assert.equal(detail({ cancelled: true }), "最近扫描 2 小时前 · 手动停止");
  assert.equal(detail({ cancelled: true, scanned: 5 }), "最近扫描 2 小时前 · 手动停止 · 新增 5 个文件");
});

test("待处理判定：有待识别或缺失，且没有任务在跑、没有文件在入库", () => {
  const pending = { item_count: 1, file_count: 1, total_size_bytes: 0, unidentified_count: 2, missing_count: 0 };
  assert.equal(libraryNeedsAttention(lib()), false);
  assert.equal(libraryNeedsAttention(lib({ stats: pending })), true);
  assert.equal(libraryNeedsAttention(lib({ stats: { ...pending, unidentified_count: 0, missing_count: 1 } })), true);
  // 状态列此时归进度，摘要也不把它算作待处理，两边口径一致
  assert.equal(libraryNeedsAttention(lib({ stats: pending, scanning: true })), false);
  assert.equal(libraryNeedsAttention(lib({ stats: pending, last_scan: { finished_at: "x", deferred: 2 } })), false);
});

test("页头摘要：规模事实 + 在跑 / 待处理的库数，有缺失时标红", () => {
  const libs = [
    lib({ id: 1 }),
    lib({ id: 2, scanning: true, stats: { item_count: 5, file_count: 5, total_size_bytes: 1024 ** 3, unidentified_count: 3, missing_count: 0 } }),
    lib({ id: 3, stats: { item_count: 5, file_count: 5, total_size_bytes: 0, unidentified_count: 3, missing_count: 0 } }),
  ];
  const s = summarizeLibraries(libs);
  assert.equal(s.facts, "3 个媒体库 · 20 个条目 · 2.00 GB");
  assert.equal(s.busy, 1);
  assert.equal(s.attention, 1);
  assert.equal(s.missing, false);
  libs[2].stats.missing_count = 1;
  assert.equal(summarizeLibraries(libs).missing, true);
  assert.deepEqual(summarizeLibraries([]), { facts: "0 个媒体库 · 0 个条目 · 0 B", busy: 0, attention: 0, missing: false });
});

test("配置备注：只说偏离默认或有待办的部分", () => {
  assert.deepEqual(configNotes(lib()), []);
  assert.deepEqual(configNotes(lib({ match_rules: [] })), [{ text: "未声明收藏范围", tone: "warn" }]);
  // 无刮削能力的库谈不上收藏范围
  assert.deepEqual(configNotes(lib({ match_rules: [], capabilities: { scraped: false, naming: false } })), []);
  assert.deepEqual(
    configNotes(lib({ exclude_from_home: true, realtime_watch: false })).map((n) => n.text),
    ["从首页排除", "实时监控关"],
  );
});

test("筛选：类型、搜索词（库名或根目录）、在跑任务 / 待处理", () => {
  const libs = [
    lib({ id: 1, name: "电影", kind: "movie" }),
    lib({ id: 2, name: "剧集", kind: "tv", scanning: true, root_paths: ["/mnt/nas2/剧集"] }),
    lib({
      id: 3,
      name: "演唱会",
      kind: "video",
      root_paths: ["/remote/media/演唱会"],
      stats: { item_count: 1, file_count: 1, total_size_bytes: 0, unidentified_count: 0, missing_count: 2 },
    }),
  ];
  const ids = (r) => r.map((l) => l.id);
  const f = (overrides) => ({ query: "", kind: null, focus: null, ...overrides });
  assert.deepEqual(ids(filterLibraries(libs, f())), [1, 2, 3]);
  assert.deepEqual(ids(filterLibraries(libs, f({ kind: "tv" }))), [2]);
  assert.deepEqual(ids(filterLibraries(libs, f({ query: "NAS2" }))), [2]);
  assert.deepEqual(ids(filterLibraries(libs, f({ query: "演唱" }))), [3]);
  assert.deepEqual(ids(filterLibraries(libs, f({ focus: "busy" }))), [2]);
  assert.deepEqual(ids(filterLibraries(libs, f({ focus: "attention" }))), [3]);
  assert.deepEqual(ids(filterLibraries(libs, f({ query: "电影", kind: "tv" }))), []);
  assert.equal(filterIsActive(f({ query: "  " })), false);
  assert.equal(filterIsActive(f({ kind: "movie" })), true);
  assert.equal(filterIsActive(f({ focus: "attention" })), true);
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
  assert.equal(accessLabel(lib()), "全部成员");
  // 只数成员：超管本人在不在范围内由锁图标表达，不计入 N
  assert.equal(accessLabel(lib({ access_mode: "selected", member_ids: [1, 2], admin_visible: false })), "指定成员 2");
  assert.equal(accessLabel(lib({ access_mode: "selected", member_ids: [1], admin_visible: true })), "指定成员 1");
  assert.equal(accessLabel(lib({ access_mode: "selected", member_ids: [], admin_visible: true })), "仅自己");
  assert.equal(accessLabel(lib({ access_mode: "selected", member_ids: [], admin_visible: false })), "无人可见");
  // 你看不到的库照样说清它开放给谁，不再退化成「仅管理」
  assert.equal(accessLabel(lib({ viewer_access: false })), "全部成员");
  // 胶囊只在偏离默认（全部成员且自己能看）时出现
  assert.equal(accessRestricted(lib()), false);
  assert.equal(accessRestricted(lib({ access_mode: "selected" })), true);
  assert.equal(accessRestricted(lib({ viewer_access: false })), true);
  assert.deepEqual(inventoryLabel(lib()), { primary: "10 部", secondary: "12 个文件" });
  assert.deepEqual(inventoryLabel(lib({ kind: "video" })), { primary: "10 个条目", secondary: "12 个文件" });
});

const chapterJob = (overrides = {}) => ({
  job_id: "job_1",
  status: "queued",
  processed: 0,
  total: 0,
  failed: 0,
  percent: null,
  stopping: false,
  ...overrides,
});

test("生成章节：排队、进行中带百分比与失败数、停止中", () => {
  // 曾经的问题：点了「生成章节」后管理页毫无反应，只有活动页看得到作业
  const queued = libraryStatus(lib({ chapter_job: chapterJob() }), ctx);
  assert.equal(queued.kind, "chapters");
  assert.equal(queued.tone, "busy");
  assert.equal(queued.title, "生成章节排队中");
  assert.equal(queued.detail, "等前面的任务跑完再开始");
  assert.equal(queued.percent, null);

  const running = chapterJob({ status: "running", processed: 35, total: 100, failed: 2 });
  const s = libraryStatus(lib({ chapter_job: running }), ctx);
  assert.equal(s.title, "正在生成章节 35%");
  assert.equal(s.detail, "35 / 100 · 2 个失败");
  assert.equal(s.percent, 35);

  // 刚开始跑、还没统计出分母：不给百分比也不写 0 / 0
  const starting = libraryStatus(lib({ chapter_job: chapterJob({ status: "running" }) }), ctx);
  assert.equal(starting.title, "正在生成章节");
  assert.equal(starting.detail, "正在统计待处理的文件数");

  const stopping = libraryStatus(
    lib({ chapter_job: chapterJob({ status: "cancelling", stopping: true, processed: 5, total: 9 }) }),
    ctx,
  );
  assert.equal(stopping.title, "正在停止生成章节");

  // 菜单项文案与状态列同一口径；没作业时就是动作名
  assert.equal(chapterJobLabel(null), "生成章节");
  assert.equal(chapterJobLabel(chapterJob()), "生成章节排队中");
  assert.equal(chapterJobLabel(running), "正在生成章节 35%");
  assert.ok(libraryIsBusy(lib({ chapter_job: chapterJob() })));
});

test("扫描中压过排队的章节作业（扫描先跑，章节排在它后面）", () => {
  const s = libraryStatus(
    lib({
      scanning: true,
      scan_progress: { phase: "ingesting", processed: 1, total: 4 },
      chapter_job: chapterJob(),
    }),
    ctx,
  );
  assert.equal(s.kind, "scan");
});
