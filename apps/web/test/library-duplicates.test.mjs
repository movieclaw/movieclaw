import assert from "node:assert/strict";
import test from "node:test";

import {
  bucketFacts,
  bucketSummary,
  fileNote,
  hiddenNote,
  keepFileFacts,
  keepVersionFacts,
  qualitySegments,
  resolveResultText,
  seasonHeadline,
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

test("seasonHeadline / bucketSummary / hiddenNote", () => {
  const season = { season_number: 1, bucket: "identical", uniform: true, versions: [], units: [unit, unit, unit] };
  assert.equal(seasonHeadline(season, "tv"), "S01 · 3 集有重复");
  assert.equal(seasonHeadline(season, "movie"), "");
  const data = {
    identical: { units: 3, files: 3, bytes: 3 * 1024 ** 3 },
    versions: { units: 1, files: 1, bytes: 4 * 1024 ** 3 },
    upgrading_units: 2,
    keep_old_items: 1,
    total_items: 2,
    items: [
      { library: { id: 1, name: "剧集" }, media_item: { id: 7, title: "权力的游戏", year: 2011, kind: "tv", poster_url: null }, seasons: [season] },
      { library: { id: 2, name: "电影" }, media_item: { id: 1, title: "九门", year: 2025, kind: "movie", poster_url: null }, seasons: [{ season_number: 0, bucket: "versions", uniform: false, versions: [], units: [unit] }] },
    ],
  };
  assert.equal(bucketSummary(data, "identical"), "1 个条目 · 3 个多余文件 · 3.00 GB");
  assert.equal(bucketSummary(data, "versions"), "1 个条目 · 1 个单元 · 1 个可能多余的文件");
  assert.equal(hiddenNote(data), "2 个单元正在洗版验证中、1 个条目按规则组「保留共存」，不在这里显示");
  assert.equal(hiddenNote({ ...data, upgrading_units: 0, keep_old_items: 0 }), "");
  const facts = bucketFacts(data, "versions");
  assert.deepEqual(facts.lines, ["九门 · 1 个文件 · 1080p WEB-DL"]);
});

test("resolveResultText", () => {
  assert.equal(resolveResultText({ done: 5, failed: [], remaining: 0 }), "已移入回收站 5 个文件");
  assert.equal(
    resolveResultText({ done: 4, failed: [{ id: 1, file_name: "x", error: "权限不足" }], remaining: 3 }),
    "已移入回收站 4 个文件，1 个失败：权限不足，还有 3 个未处理（再点一次即可）",
  );
});
