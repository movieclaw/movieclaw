/**
 * 媒体库管理页「重复文件」标签的纯函数：块标题、版本行 / 文件行的文案、规格差异
 * 高亮、确认弹窗的事实清单、批量结果文案。设计见 docs/design/library-duplicate-files.md。
 *
 * 与 library-recycle.ts 同一约定：只允许 `import type` 与无依赖的纯模块（format.ts），
 * 能用 node --test 直接跑单测。
 */

import type {
  DuplicateFile,
  DuplicateFilesData,
  DuplicateGroup,
  DuplicateScanState,
  DuplicateSeason,
  DuplicateTier,
  DuplicateUnit,
  DuplicateVersion,
  TrashedBatchResult,
} from "./api/libraries";
import { formatBytes } from "./format.ts";

const pad = (n: number) => String(n).padStart(2, "0");

/** 集号 `E03`；电影为空串。 */
export function episodeLabel(episode: number): string {
  return episode > 0 ? `E${pad(episode)}` : "";
}

/** 块标题的第二段：剧集 `S01 · 3 集有重复`（重复集少于全季时不知道全季，只写重复集）；电影空。 */
export function seasonHeadline(season: DuplicateSeason, kind: string): string {
  if (kind !== "tv") return "";
  return `S${pad(season.season_number)} · ${season.units.length} 集有重复`;
}

/** 一档 / 一组卡片上的计数：`12 个单元 · 14 个文件 · 31 GB`；没有活时返回空串。 */
export function groupSummary(group: DuplicateGroup): string {
  if (group.units === 0) return "";
  const parts = [`${group.units} 个单元`, `${group.files} 个文件`];
  if (group.bytes > 0) parts.push(formatBytes(group.bytes));
  return parts.join(" · ");
}

/** 一档的动作按钮该写什么：`safe` 是没风险的清理，另两档都在动"有区别"的文件。 */
export const TIER_ACTION_LABELS: Record<DuplicateTier, string> = {
  safe: "全部清理",
  suggested: "全部按建议清理",
  review: "全部按建议清理",
};

/** 页脚那句"不在这里显示的"：两段都为 0 时返回空串。 */
export function hiddenNote(scan: DuplicateScanState): string {
  const parts: string[] = [];
  if (scan.upgrading_units > 0) parts.push(`${scan.upgrading_units} 个单元正在洗版验证中`);
  if (scan.keep_old_items > 0) parts.push(`${scan.keep_old_items} 个条目按规则组「保留共存」`);
  return parts.length ? `${parts.join("、")}，不在这里显示` : "";
}

/** 扫描状态那一行：从未扫描 / 正在跑（带进度）/ 上次没跑成 / 上次扫描于何时。 */
export function scanNote(scan: DuplicateScanState, now: Date = new Date()): string {
  if (isScanning(scan)) return scan.message ? `正在扫描 · ${scan.message}` : "正在扫描…";
  if (scan.status === "failed" || scan.status === "cancelled") {
    const why = scan.message ? `：${scan.message}` : "";
    const had = scan.scanned_at ? `，下面是 ${relativeTime(scan.scanned_at, now)}的结果` : "";
    return `上次扫描${scan.status === "failed" ? "失败" : "被取消"}${why}${had}`;
  }
  if (!scan.scanned_at) return "还没有扫描过";
  return `上次扫描：${relativeTime(scan.scanned_at, now)}`;
}

export function isScanning(scan: DuplicateScanState): boolean {
  return scan.status !== null && scan.status !== "succeeded" && scan.status !== "failed" && scan.status !== "cancelled";
}

/** 「3 分钟前」「2 小时前」「3 天前」——精确到秒没有意义，用户只想知道"新不新鲜"。 */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  const seconds = Math.max(0, Math.round((now.getTime() - then.getTime()) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`;
  return then.toLocaleDateString("zh-CN");
}

/**
 * 规格文案的分段：`2160p WEB-DL DV` → ["2160p", "WEB-DL", "DV"]，再拼上大小与码率。
 * 每段带 `diff` 标记：与建议保留者对应位不同的段加亮——"差就差在这一个维度上"。
 */
export interface QualitySegment {
  text: string;
  diff: boolean;
}

export function qualitySegments(file: DuplicateFile, reference: DuplicateFile | null): QualitySegment[] {
  const own = file.quality_label.split(" ");
  const ref = reference && reference !== file ? reference.quality_label.split(" ") : null;
  const segments: QualitySegment[] = own.map((text, i) => ({
    text,
    diff: ref !== null && ref[i] !== text,
  }));
  if (file.audio_label) {
    segments.push({
      text: file.audio_label,
      diff: ref !== null && reference !== null && reference.audio_label !== file.audio_label,
    });
  }
  segments.push({ text: formatBytes(file.size_bytes), diff: false });
  if (file.bit_rate) segments.push({ text: formatBitRate(file.bit_rate), diff: false });
  return segments;
}

export function formatBitRate(bps: number): string {
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1)} Mbps`;
  return `${Math.round(bps / 1000)} kbps`;
}

/** 单元里的建议保留者（没有时取第一个，后端保证一定有）。 */
export function suggestedOf(unit: DuplicateUnit): DuplicateFile {
  return unit.files.find((f) => f.suggested) ?? unit.files[0];
}

/** 文件行第二行的说明：建议保留写依据；「都留着」过的写"你留下的"；否则空。 */
export function fileNote(file: DuplicateFile): string {
  if (file.kept_at) return "你留下的";
  if (file.suggested && file.suggest_reason) return `建议保留 · ${file.suggest_reason}`;
  return "";
}

/** 版本行：`3 集 · 30.9 GB`。 */
export function versionCoverage(version: DuplicateVersion): string {
  return `${version.episodes.length} 集 · ${formatBytes(version.bytes)}`;
}

/** 「留这个」确认弹窗的事实：留下谁、清掉几个、多大。 */
export interface KeepFacts {
  keepName: string;
  gone: DuplicateFile[];
  bytes: number;
}

export function keepFileFacts(unit: DuplicateUnit, keep: DuplicateFile): KeepFacts {
  const gone = unit.files.filter((f) => f.id !== keep.id);
  return { keepName: keep.file_name, gone, bytes: gone.reduce((n, f) => n + f.size_bytes, 0) };
}

/**
 * 「整季留这个版本」确认弹窗的事实：每集留该版本，缺该版本的集留建议保留者；
 * 返回将被清掉的文件与"没有这个版本"的集号。
 */
export interface KeepVersionFacts {
  version: DuplicateVersion;
  gone: DuplicateFile[];
  bytes: number;
  missingEpisodes: number[];
}

export function keepVersionFacts(season: DuplicateSeason, version: DuplicateVersion): KeepVersionFacts {
  const gone: DuplicateFile[] = [];
  const missing: number[] = [];
  for (const unit of season.units) {
    const target = unit.files.find((f) => f.version_key === version.key) ?? null;
    const keep = target ?? suggestedOf(unit);
    if (target === null) missing.push(unit.episode_number);
    for (const f of unit.files) if (f.id !== keep.id && !f.kept_at) gone.push(f);
  }
  return { version, gone, bytes: gone.reduce((n, f) => n + f.size_bytes, 0), missingEpisodes: missing };
}

/**
 * 整档按建议清理的确认清单：本页看得到的条目逐条列出会清掉什么。
 *
 * 只列本页——后端一次最多清 500 个文件，把几千条全拉回来铺进弹窗，用户一样
 * 看不完。弹窗另写明总数，让人知道"这不是全部"。
 */
export interface TierFacts {
  files: number;
  bytes: number;
  lines: string[];
}

export function tierFacts(data: DuplicateFilesData, group: DuplicateGroup | null): TierFacts {
  const lines: string[] = [];
  for (const item of data.items) {
    for (const season of item.seasons) {
      const extras = season.units.flatMap((u) => u.files.filter((f) => !f.suggested && !f.kept_at));
      if (extras.length === 0) continue;
      const head = item.media_item.kind === "tv" ? `${item.media_item.title} S${pad(season.season_number)}` : item.media_item.title;
      const kinds = [...new Set(extras.map((f) => f.quality_label))].join(" / ");
      lines.push(`${head} · ${extras.length} 个文件 · ${kinds}`);
    }
  }
  return { files: group?.files ?? 0, bytes: group?.bytes ?? 0, lines };
}

/** 批量结果 toast：`已移入回收站 5 个文件` / `…，2 个失败` / `…，还有 N 个未处理`。 */
export function resolveResultText(result: TrashedBatchResult): string {
  let text = `已移入回收站 ${result.done} 个文件`;
  if (result.failed.length > 0) text += `，${result.failed.length} 个失败：${result.failed[0].error}`;
  if (result.remaining > 0) text += `，还有 ${result.remaining} 个未处理（再点一次即可）`;
  return text;
}
