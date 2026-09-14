import assert from "node:assert/strict";
import test from "node:test";

import {
  fileNote,
  groupSummary,
  hiddenNote,
  isScanning,
  keepFileFacts,
  keepVersionFacts,
  qualitySegments,
  relativeTime,
  resolveResultText,
  scanNote,
  seasonHeadline,
  tierFacts,
} from "../lib/library-duplicates.ts";

const origin = (kind, label) => ({ kind, label, detail: null });

function file(overrides = {}) {
  return {
    id: 1,
    file_name: "a.mkv",
    file_path: "/m/a.mkv",
    quality_label: "2160p WEB-DL",
    size_bytes: 8 * 1024 ** 3,
    bit_rate: 20_000_000,
    resolution: "2160p",
    media_source: "WEB-DL",
    hdr: null,
    video_codec: "hevc",
    audio_label: "DDP 5.1",
    origin: origin("subscription", "订阅《九门》自动投递"),
    version_key: "2160p WEB-DL|订阅《九门》自动投递",
    suggested: true,
    suggest_reason: "档位最高",
    kept_at: null,
    ...overrides,
  };
}

const A = file();
const B = file({ id: 2, file_name: "b.mkv", quality_label: "1080p WEB-DL", size_bytes: 4 * 1024 ** 3, suggested: false, suggest_reason: null, origin: origin("scan", "存量扫描发现"), version_key: "1080p WEB-DL|存量扫描发现", audio_label: "AAC 2.0" });
const unit = { season_number: 0, episode_number: 0, bucket: "versions", files: [A, B] };

test("qualitySegments 高亮与建议保留者不同的维度", () => {
  const segs = qualitySegments(B, A);
  assert.deepEqual(
    segs.map((s) => [s.text, s.diff]),
    [["1080p", true], ["WEB-DL", false], ["AAC 2.0", true], ["4.00 GB", false], ["20.0 Mbps", false]],
  );
  // 建议保留者自己不高亮
  assert.ok(qualitySegments(A, A).every((s) => !s.diff));
});

test("fileNote：建议保留写依据，都留着写你留下的", () => {
  assert.equal(fileNote(A), "建议保留 · 档位最高");
  assert.equal(fileNote(B), "");
  assert.equal(fileNote({ ...B, kept_at: "2026-09-13T00:00:00Z" }), "你留下的");
});

test("keepFileFacts：留下一个，其余进回收站", () => {
  const facts = keepFileFacts(unit, A);
  assert.equal(facts.keepName, "a.mkv");
  assert.deepEqual(facts.gone.map((f) => f.id), [2]);
  assert.equal(facts.bytes, 4 * 1024 ** 3);
});

test("keepVersionFacts：缺该版本的集留建议保留者，并报出集号", () => {
  const e1 = { season_number: 1, episode_number: 1, bucket: "versions", files: [file({ id: 11 }), file({ id: 12, suggested: false, version_key: "1080p|扫描", size_bytes: 1 })] };
  const e2 = { season_number: 1, episode_number: 2, bucket: "versions", files: [file({ id: 21 }), file({ id: 22, suggested: false, version_key: "720p|扫描", size_bytes: 1 })] };
  const season = { season_number: 1, bucket: "versions", uniform: true, versions: [], units: [e1, e2] };
  const v = { key: "1080p|扫描", quality_label: "1080p", origin_label: "扫描", episodes: [1], bytes: 1, suggested: false };
  const facts = keepVersionFacts(season, v);
  // E01 留 1080p（清 2160p）；E02 没有这个版本 → 留建议保留者 2160p（清 720p）
  assert.deepEqual(facts.gone.map((f) => f.id), [11, 22]);
  assert.deepEqual(facts.missingEpisodes, [2]);
});

const scan = (overrides = {}) => ({
  status: "succeeded",
  job_id: "job_1",
  message: null,
  percent: null,
  scanned_at: "2026-09-14T02:00:00",
  upgrading_units: 2,
  keep_old_items: 1,
  ...overrides,
});

test("seasonHeadline / groupSummary / hiddenNote", () => {
  const season = { season_number: 1, bucket: "identical", uniform: true, versions: [], units: [unit, unit, unit] };
  assert.equal(seasonHeadline(season, "tv"), "S01 · 3 集有重复");
  assert.equal(seasonHeadline(season, "movie"), "");

  assert.equal(
    groupSummary({ key: "safe", label: "可以放心清理", hint: "", units: 3, files: 3, bytes: 3 * 1024 ** 3 }),
    "3 个单元 · 3 个文件 · 3.00 GB",
  );
  // 没有活就不写数字——空档的卡片上写「没有」，不写「0 个单元 · 0 个文件」
  assert.equal(groupSummary({ key: "safe", label: "", hint: "", units: 0, files: 0, bytes: 0 }), "");

  assert.equal(hiddenNote(scan()), "2 个单元正在洗版验证中、1 个条目按规则组「保留共存」，不在这里显示");
  assert.equal(hiddenNote(scan({ upgrading_units: 0, keep_old_items: 0 })), "");
});

test("扫描状态那一行：从未扫描 / 正在跑 / 上次扫描于何时", () => {
  assert.equal(scanNote(scan({ status: null, scanned_at: null })), "还没有扫描过");
  assert.equal(isScanning(scan({ status: null })), false);
  assert.equal(isScanning(scan({ status: "running" })), true);
  assert.equal(scanNote(scan({ status: "running", message: "比对文件指纹" })), "正在扫描 · 比对文件指纹");
  assert.equal(scanNote(scan({ status: "queued", message: null })), "正在扫描…");
  // 失败要说出来，别静默退回"还没有扫描过"——用户会一直按按钮却不知道为什么
  assert.equal(
    scanNote(scan({ status: "failed", message: "磁盘不可用", scanned_at: null })),
    "上次扫描失败：磁盘不可用",
  );
  const now = new Date("2026-09-14T02:30:00Z");
  assert.equal(scanNote(scan(), now), "上次扫描：30 分钟前");
  // 后端的时间是不带时区的 UTC 朴素时间，不能被当成本地时间读
  assert.equal(relativeTime("2026-09-14T02:29:30", now), "刚刚");
  assert.equal(relativeTime("2026-09-13T20:30:00", now), "6 小时前");
  assert.equal(relativeTime("2026-09-13T02:30:00", now), "1 天前");
  assert.equal(relativeTime("2026-09-04T02:30:00", now), "10 天前");
});

test("tierFacts：确认弹窗逐条列出本页会清掉什么", () => {
  const season = { season_number: 1, bucket: "identical", uniform: true, versions: [], units: [unit, unit, unit] };
  const data = {
    scan: scan(),
    tiers: [],
    review_groups: [],
    total_units: 4,
    total_files: 4,
    total_bytes: 7 * 1024 ** 3,
    total_items: 2,
    items: [
      { library: { id: 1, name: "剧集" }, media_item: { id: 7, title: "权力的游戏", year: 2011, kind: "tv", poster_url: null }, seasons: [season] },
      { library: { id: 2, name: "电影" }, media_item: { id: 1, title: "九门", year: 2025, kind: "movie", poster_url: null }, seasons: [{ season_number: 0, bucket: "versions", uniform: false, versions: [], units: [unit] }] },
    ],
  };
  const group = { key: "suggested", label: "建议清理", hint: "", units: 4, files: 4, bytes: 4 * 1024 ** 3 };
  const facts = tierFacts(data, group);
  assert.deepEqual(facts.lines, ["权力的游戏 S01 · 3 个文件 · 1080p WEB-DL", "九门 · 1 个文件 · 1080p WEB-DL"]);
  // 总数来自摘要而不是本页：本页只是清单，后端一次最多清 500 个
  assert.equal(facts.files, 4);
});

test("resolveResultText", () => {
  assert.equal(resolveResultText({ done: 5, failed: [], remaining: 0 }), "已移入回收站 5 个文件");
  assert.equal(
    resolveResultText({ done: 4, failed: [{ id: 1, file_name: "x", error: "权限不足" }], remaining: 3 }),
    "已移入回收站 4 个文件，1 个失败：权限不足，还有 3 个未处理（再点一次即可）",
  );
});
