import assert from "node:assert/strict";
import test from "node:test";

import {
  batchResultText,
  countdown,
  episodeCode,
  fileQualityLine,
  itemCountdown,
  itemFilesSummary,
  itemQualityLine,
  itemReasonText,
  itemSelection,
  seasonsLabel,
  selectionSummary,
  summarySegments,
} from "../lib/library-recycle.ts";

const NOW = Date.parse("2026-09-06T12:00:00Z");
const iso = (hours) => new Date(NOW + hours * 3_600_000).toISOString();

function file(overrides = {}) {
  return {
    id: 1,
    file_name: "GoT.S01E03.720p.HDTV.x264-CTU.mkv",
    file_path: "/tv/.movieclaw-trash/GoT.S01E03.720p.HDTV.x264-CTU.mkv",
    trash_original_path: "/tv/GoT/GoT.S01E03.720p.HDTV.x264-CTU.mkv",
    kept_in_place: false,
    size_bytes: 1.1 * 1024 ** 3,
    resolution: "720p",
    media_source: "HDTV",
    hdr: null,
    video_codec: "h264",
    bit_depth: 8,
    audio_label: "AC3 5.1",
    release_group: "CTU",
    season_number: 1,
    episode_number: 3,
    episode_title: "雪诺大人",
    trashed_at: iso(-144),
    purge_after: iso(9),
    reason: "upgrade_replaced",
    note: "洗版替换：720p HDTV → 1080p BluRay",
    last_error: null,
    ...overrides,
  };
}

function item(overrides = {}) {
  const files = overrides.files ?? [file(), file({ id: 2, episode_number: 4 })];
  return {
    key: "3021",
    library: { id: 2, name: "剧集" },
    media_item: { id: 3021, title: "权力的游戏", year: 2011, kind: "tv", poster_url: null },
    seasons: [1, 2, 3],
    file_count: files.length,
    total_bytes: files.reduce((sum, f) => sum + f.size_bytes, 0),
    earliest_purge_after: iso(9),
    latest_purge_after: iso(72),
    reasons: { upgrade_replaced: files.length },
    note: "洗版替换：720p HDTV → 1080p BluRay",
    trigger_label: "《权力的游戏》订阅洗版",
    latest_trashed_at: iso(-144),
    quality: {
      tiers: { "720p HDTV": files.length },
      hdr: [],
      video_codecs: ["h264"],
      audio_labels: ["AC3 5.1"],
      release_groups: ["CTU"],
    },
    ...overrides,
    files,
  };
}

test("倒计时分档：24 小时内警示、天+小时、不自动清理、已到期", () => {
  assert.deepEqual(countdown(iso(6), NOW), { text: "6 小时后", tone: "soon" });
  assert.deepEqual(countdown(iso(24), NOW), { text: "1 天后", tone: "soon" });
  assert.deepEqual(countdown(iso(76), NOW), { text: "3 天 4 小时后", tone: "normal" });
  assert.deepEqual(countdown(iso(0.5), NOW), { text: "1 小时内", tone: "soon" });
  assert.deepEqual(countdown(iso(-1), NOW), { text: "即将清理", tone: "soon" });
  assert.deepEqual(countdown(null, NOW), { text: "不自动清理", tone: "never" });
});

test("条目行倒计时：多文件带「最早」前缀，单文件与不自动清理不带", () => {
  assert.equal(itemCountdown(item(), NOW).text, "最早 9 小时后");
  assert.equal(itemCountdown(item({ files: [file()] }), NOW).text, "9 小时后");
  assert.equal(
    itemCountdown(item({ earliest_purge_after: null }), NOW).text,
    "不自动清理",
  );
});

test("摘要行：按文件计数，24 小时内与原地两段为 0 时省去", () => {
  const full = summarySegments({
    total_files: 57,
    total_items: 31,
    total_bytes: 53.2 * 1024 ** 3,
    due_within_24h: 12,
    kept_in_place: 1,
    by_library: [],
    by_reason: [],
    items: [],
  });
  assert.deepEqual(
    full.map((s) => s.text),
    ["57 个文件", "31 个条目", "53.20 GB", "12 个将在 24 小时内自动清理", "1 个仍在原位"],
  );
  assert.equal(full[3].tone, "soon");
  const bare = summarySegments({
    total_files: 3,
    total_items: 1,
    total_bytes: 0,
    due_within_24h: 0,
    kept_in_place: 0,
    by_library: [],
    by_reason: [],
    items: [],
  });
  assert.equal(bare.length, 3);
});

test("原因列：组内一致写整句，混合写计数并按数量降序", () => {
  assert.equal(itemReasonText(item()), "洗版替换：720p HDTV → 1080p BluRay");
  assert.equal(
    itemReasonText(item({ note: null, reasons: { upgrade_refuted: 4, upgrade_replaced: 2 } })),
    "洗版证伪 4 · 洗版替换 2",
  );
  assert.equal(itemReasonText(item({ note: null, reasons: { manual: 1 } })), "手动删除");
});

test("品质行：单文件档位 + 大小 · 编码 · 音轨 · 制作组；10bit 才标位深", () => {
  const line = fileQualityLine(file());
  assert.deepEqual(line.tiers, [{ label: "720p HDTV", count: null }]);
  assert.deepEqual(line.rest, ["1.10 GB", "H.264", "AC3 5.1", "CTU"]);
  const hdr = fileQualityLine(
    file({ resolution: "2160p", media_source: "BluRay", hdr: "HDR10", video_codec: "hevc", bit_depth: 10, audio_label: null, release_group: null, size_bytes: 22.6 * 1024 ** 3 }),
  );
  assert.deepEqual(hdr.tiers[0].label, "2160p BluRay");
  assert.deepEqual(hdr.hdr, ["HDR10"]);
  assert.deepEqual(hdr.rest, ["22.60 GB", "HEVC 10bit"]);
  assert.equal(fileQualityLine(file({ resolution: null, media_source: null })).tiers[0].label, "未知规格");
});

test("品质行：组内一致写一种，混合带计数；编码/音轨/制作组用 / 并列", () => {
  assert.deepEqual(itemQualityLine(item()).tiers, [{ label: "720p HDTV", count: null }]);
  const mixed = itemQualityLine(
    item({
      quality: {
        tiers: { "1080p WEB-DL": 4, "1080p BluRay": 2 },
        hdr: [],
        video_codecs: ["h264"],
        audio_labels: ["AC3 5.1", "DTS 5.1"],
        release_groups: ["NTb", "FLUX"],
      },
    }),
  );
  assert.deepEqual(mixed.tiers, [
    { label: "1080p WEB-DL", count: 4 },
    { label: "1080p BluRay", count: 2 },
  ]);
  assert.deepEqual(mixed.rest.slice(1), ["H.264", "AC3 5.1 / DTS 5.1", "NTb / FLUX"]);
});

test("集号与季区间", () => {
  assert.equal(episodeCode(1, 3), "S01E03");
  assert.equal(episodeCode(0, 0), "");
  assert.equal(seasonsLabel([1, 2, 3]), "S01–S03");
  assert.equal(seasonsLabel([2]), "S02");
  assert.equal(seasonsLabel([1, 3]), "S01, S03");
  assert.equal(seasonsLabel([]), "");
});

test("文件格第一行：剧集写 N 集 · N 季，电影多版本写 N 个版本", () => {
  assert.equal(itemFilesSummary(item()), "2 集 · 3 季");
  assert.equal(
    itemFilesSummary(item({ media_item: { id: 1, title: "奥本海默", year: 2023, kind: "movie", poster_url: null }, seasons: [] })),
    "2 个版本",
  );
});

test("选择态：整组 / 半选 / 未选，批量条按文件计数与合计大小", () => {
  const it = item();
  assert.equal(itemSelection(it, new Set()), "none");
  assert.equal(itemSelection(it, new Set([1])), "some");
  assert.equal(itemSelection(it, new Set([1, 2])), "all");
  assert.deepEqual(selectionSummary([it], new Set([1])), { count: 1, bytes: it.files[0].size_bytes });
});

test("批量回执一句话", () => {
  assert.equal(batchResultText("清理", { done: 55, failed: [{}, {}], remaining: 0 }), "已清理 55 个文件，2 个失败");
  assert.equal(batchResultText("清理", { done: 500, failed: [], remaining: 12 }), "已清理 500 个文件，还有 12 个未处理，再点一次即可");
  assert.equal(batchResultText("恢复", { done: 2, failed: [] }), "已恢复 2 个文件");
});
