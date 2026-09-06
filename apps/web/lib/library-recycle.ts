/**
 * 媒体库管理页「回收站」标签的纯函数：倒计时分档、摘要文案、品质行、原因合并、
 * 选择态。设计见 docs/design/library-recycle-bin.md §2。
 *
 * 与 library-manage.ts 同一约定：放在这里是为了能用 node --test 直接跑单测，
 * 因此只允许 `import type` 与无依赖的纯模块（format.ts），不能引入 `@/` 别名
 * 或浏览器依赖。
 */

import type { TrashedFile, TrashedFilesData, TrashedItem } from "./api/libraries";
import { formatBytes } from "./format.ts";

/** 倒计时的语气：soon = 24 小时内（警示色）；never = 不自动清理（弱化）。 */
export interface Countdown {
  text: string;
  tone: "soon" | "normal" | "never";
}

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

/**
 * 单个到期时间 → 倒计时文案。直读 purge_after，展示精度与清理周期无关。
 * 与条目详情页文件区的 purgeCountdown 同一分档，这里去掉「预计 … 自动清理」的
 * 包装——列头「自动清理 · 操作」已经说明了这一列是什么。
 */
export function countdown(purgeAfter: string | null, now: number = Date.now()): Countdown {
  if (!purgeAfter) return { text: "不自动清理", tone: "never" };
  const ms = new Date(purgeAfter).getTime() - now;
  if (ms <= 0) return { text: "即将清理", tone: "soon" };
  const days = Math.floor(ms / DAY);
  const hours = Math.floor((ms % DAY) / HOUR);
  const tone = ms <= DAY ? "soon" : "normal";
  if (days > 0) return { text: hours > 0 ? `${days} 天 ${hours} 小时后` : `${days} 天后`, tone };
  if (hours > 0) return { text: `${hours} 小时后`, tone };
  return { text: "1 小时内", tone };
}

/**
 * 条目行的倒计时：取组内最早到期；多文件时带「最早」前缀，
 * 悬停提示写最晚到期（由调用方拼绝对时间）。
 */
export function itemCountdown(item: TrashedItem, now: number = Date.now()): Countdown {
  const base = countdown(item.earliest_purge_after, now);
  if (item.file_count > 1 && base.tone !== "never") {
    return { ...base, text: `最早 ${base.text}` };
  }
  return base;
}

/** 摘要行的分段：调用方把 tone=soon 的一段着警示色。 */
export interface SummarySegment {
  text: string;
  tone?: "soon" | "faint";
}

/**
 * 摘要行：`N 个文件 · M 个条目 · 总大小 · K 个将在 24 小时内自动清理 · J 个仍在原位`，
 * 后两段为 0 时省去。摘要按文件计数（回答"多大、多急"），分页按条目计数。
 */
export function summarySegments(data: TrashedFilesData): SummarySegment[] {
  const segments: SummarySegment[] = [
    { text: `${data.total_files} 个文件` },
    { text: `${data.total_items} 个条目` },
    { text: formatBytes(data.total_bytes) },
  ];
  if (data.due_within_24h > 0) {
    segments.push({ text: `${data.due_within_24h} 个将在 24 小时内自动清理`, tone: "soon" });
  }
  if (data.kept_in_place > 0) {
    segments.push({ text: `${data.kept_in_place} 个仍在原位`, tone: "faint" });
  }
  return segments;
}

/** 审计快照 reason 词表 → 胶囊 / 原因列文案；未知值原样返回。 */
export function reasonLabel(reason: string): string {
  switch (reason) {
    case "upgrade_replaced":
      return "洗版替换";
    case "upgrade_refuted":
      return "洗版证伪";
    case "manual":
      return "手动删除";
    case "unknown":
      return "其他";
    default:
      return reason;
  }
}

/** 条目行的原因：组内 note 一致时写整句；混合时写计数 `洗版证伪 4 · 洗版替换 2`。 */
export function itemReasonText(item: TrashedItem): string {
  if (item.note) return item.note;
  const entries = Object.entries(item.reasons).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return "";
  if (entries.length === 1) return reasonLabel(entries[0][0]);
  return entries.map(([reason, count]) => `${reasonLabel(reason)} ${count}`).join(" · ");
}

const VIDEO_CODEC_LABELS: Record<string, string> = {
  hevc: "HEVC",
  h264: "H.264",
  h265: "HEVC",
  av1: "AV1",
  vc1: "VC-1",
  mpeg2video: "MPEG-2",
  vp9: "VP9",
};

/** 探测层的编码名 → 惯用写法（与条目详情页同一张表）。 */
export function videoCodecLabel(codec: string | null): string | null {
  if (!codec) return null;
  return VIDEO_CODEC_LABELS[codec.toLowerCase()] ?? codec.toUpperCase();
}

/** 品质行：第一段是加粗的档位（可带计数），HDR 徽标单列，其余弱化以 · 相连。 */
export interface QualityLine {
  /** 「分辨率 片源」；组内混合时每段带计数，如 `1080p WEB-DL 4` */
  tiers: { label: string; count: number | null }[];
  hdr: string[];
  /** 大小 · 编码[ 位深] · 音轨 · 制作组，缺项省略 */
  rest: string[];
}

/** 单个文件的品质档位「分辨率 片源」，两者都没探到时写「未知规格」。 */
export function fileTier(file: TrashedFile): string {
  return [file.resolution, file.media_source].filter(Boolean).join(" ") || "未知规格";
}

/** 单个文件的品质行（电影单文件行、展开的每集）。 */
export function fileQualityLine(file: TrashedFile): QualityLine {
  const codec = videoCodecLabel(file.video_codec);
  return {
    tiers: [{ label: fileTier(file), count: null }],
    hdr: file.hdr ? [file.hdr] : [],
    rest: [
      formatBytes(file.size_bytes),
      codec ? (file.bit_depth && file.bit_depth > 8 ? `${codec} ${file.bit_depth}bit` : codec) : null,
      file.audio_label,
      file.release_group,
    ].filter((part): part is string => Boolean(part)),
  };
}

/**
 * 条目行的品质汇总：档位一致写一种，混合写带计数的并列；大小为合计，
 * 编码 / 音轨 / 制作组去重后用 / 并列。
 */
export function itemQualityLine(item: TrashedItem): QualityLine {
  const tierEntries = Object.entries(item.quality.tiers).sort((a, b) => b[1] - a[1]);
  const mixed = tierEntries.length > 1;
  return {
    tiers: tierEntries.map(([label, count]) => ({ label, count: mixed ? count : null })),
    hdr: item.quality.hdr,
    rest: [
      formatBytes(item.total_bytes),
      item.quality.video_codecs.map(videoCodecLabel).filter(Boolean).join(" / ") || null,
      item.quality.audio_labels.join(" / ") || null,
      item.quality.release_groups.join(" / ") || null,
    ].filter((part): part is string => Boolean(part)),
  };
}

/** S01E03 形态的集号；电影（0,0）返回空串。 */
export function episodeCode(season: number, episode: number): string {
  if (season === 0 && episode === 0) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `S${pad(season)}E${pad(episode)}`;
}

/** 涉及的季：连续写区间 `S01–S03`，不连续写列表 `S01, S03`；电影为空串。 */
export function seasonsLabel(seasons: number[]): string {
  if (seasons.length === 0) return "";
  const sorted = [...seasons].sort((a, b) => a - b);
  const pad = (n: number) => `S${String(n).padStart(2, "0")}`;
  if (sorted.length === 1) return pad(sorted[0]);
  const contiguous = sorted.every((s, i) => i === 0 || s === sorted[i - 1] + 1);
  return contiguous ? `${pad(sorted[0])}–${pad(sorted[sorted.length - 1])}` : sorted.map(pad).join(", ");
}

/** 条目行「文件」格第一行：单文件写文件名（由调用方渲染），多文件写 `24 集 · 3 季` / `2 个版本`。 */
export function itemFilesSummary(item: TrashedItem): string {
  if (item.media_item?.kind === "tv") {
    const seasons = item.seasons.length;
    return seasons > 0 ? `${item.file_count} 集 · ${seasons} 季` : `${item.file_count} 集`;
  }
  return `${item.file_count} 个版本`;
}

/** 条目复选框的三态：整组选中 / 部分选中（半选 –）/ 未选。 */
export function itemSelection(item: TrashedItem, selected: ReadonlySet<number>): "all" | "some" | "none" {
  let picked = 0;
  for (const file of item.files) if (selected.has(file.id)) picked += 1;
  if (picked === 0) return "none";
  return picked === item.files.length ? "all" : "some";
}

/** 勾选集合的摘要：文件数与合计大小（批量条用）。 */
export function selectionSummary(
  items: readonly TrashedItem[],
  selected: ReadonlySet<number>,
): { count: number; bytes: number } {
  let count = 0;
  let bytes = 0;
  for (const item of items) {
    for (const file of item.files) {
      if (selected.has(file.id)) {
        count += 1;
        bytes += file.size_bytes;
      }
    }
  }
  return { count, bytes };
}

/** 批量结果 → 一句 toast 回执：「已清理 55 个文件，2 个失败，还有 N 个未处理」。 */
export function batchResultText(
  verb: "清理" | "恢复",
  result: { done: number; failed: { length: number }; remaining?: number },
): string {
  let text = `已${verb} ${result.done} 个文件`;
  if (result.failed.length > 0) text += `，${result.failed.length} 个失败`;
  if (result.remaining) text += `，还有 ${result.remaining} 个未处理，再点一次即可`;
  return text;
}
