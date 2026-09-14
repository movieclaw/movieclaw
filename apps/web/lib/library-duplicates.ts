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
  DuplicateReviewKind,
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

/**
 * 取舍分组行上的计数：`562 个单元 · 933 GB`。
 *
 * 比 `groupSummary` 少一段文件数——分组行要和名字、三个按钮共处一行，宽度是
 * 最紧的；"能腾出多少"比"几个文件"更能决定先做哪一组，而文件数在「按建议清 · N」
 * 上原样写着。
 */
export function compactSummary(group: DuplicateGroup): string {
  if (group.units === 0) return "";
  const parts = [`${group.units} 个单元`];
  if (group.bytes > 0) parts.push(formatBytes(group.bytes));
  return parts.join(" · ");
}

/**
 * 这个作用域给不给「按建议成批清理」。
 *
 * 一条规则三处用（摘要卡的档行、取舍分组行、明细层头部），散在 JSX 条件里迟早
 * 漏一处——真漏过：只在点进「规格不全」那一组时藏了按钮，**停在「需要你决定」
 * 整档**那条路径没管，而整档里混着「规格不全」的单元，一键清照样会碰到它们。
 *
 * 说不给的两种，理由是同一句：那些单元的定义就是"机器没有比较的依据"，拿一个
 * 机器自己声明做不出的判断去成批删文件，是三态铁律的反面。要成批处理就先点进
 * 一个**具体的**取舍组。
 */
export function allowsBulkClean(
  tier: DuplicateTier,
  reviewKind: DuplicateReviewKind | null,
): boolean {
  if (tier !== "review") return true;
  if (reviewKind === null) return false; // 整档：混着「规格不全」
  return reviewKind !== "unknown";
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

/**
 * 规格里**认得出是什么**的那几段：分辨率 / 片源 / HDR / 音轨。可以截断——
 * 截掉音轨编码不影响判断，截掉体积却会。
 */
export function specSegments(
  file: DuplicateFile,
  reference: DuplicateFile | null,
): QualitySegment[] {
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
  return segments;
}

/**
 * **体积与码率**：逐个清点时最硬的那两个数。
 *
 * 它们和上面那几段分开，是因为它们绝不能被截断。第一版把体积与码率放在规格行
 * 末尾，而那一行是 truncate 的——窄屏上真实渲染成
 * `2160p · WEB-DL · Dolby · Vision · AAC 2.0 · 1.22 G…`：体积被砍掉一半、码率
 * 整个消失，偏偏这两样才是"留哪个"最直接的依据。现在它们单独一段钉在行尾，
 * 要截也只截前面那些认名字的段。
 */
export function volumeSegments(file: DuplicateFile): QualitySegment[] {
  const segments: QualitySegment[] = [{ text: formatBytes(file.size_bytes), diff: false }];
  if (file.bit_rate) segments.push({ text: formatBitRate(file.bit_rate), diff: false });
  return segments;
}

export function qualitySegments(file: DuplicateFile, reference: DuplicateFile | null): QualitySegment[] {
  return [...specSegments(file, reference), ...volumeSegments(file)];
}

export function formatBitRate(bps: number): string {
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1)} Mbps`;
  return `${Math.round(bps / 1000)} kbps`;
}

/**
 * 一个文件的规格整句：`2160p · WEB-DL · AAC 2.0 · 1.01 GB · 3.1 Mbps`。
 * 与 `qualitySegments` 同一份内容，只是拼成一行用来做"几个文件是不是一样"的比对。
 */
export function specText(file: DuplicateFile): string {
  return qualitySegments(file, null)
    .map((s) => s.text)
    .join(" · ");
}

/**
 * 单元内**所有文件都一样**的规格与来源。
 *
 * 手机上一个文件行要竖着叠成四行，而重复文件最常见的样子恰恰是"规格与来源
 * 完全相同、只有文件名不同"（同一个包被扫进来两次、外部工具改过名）——那就是
 * 同样的两行文字各印一遍，占满屏幕却一个字都不帮用户做决定。相同的部分提到
 * 单元头上说一次，文件行只留下真正不同的东西。
 *
 * 只有一个文件时不算"共有"：没有比较对象，该显示的还是要显示。
 */
export interface SharedFacts {
  quality: string | null;
  origin: string | null;
}

export function sharedFacts(files: DuplicateFile[]): SharedFacts {
  if (files.length < 2) return { quality: null, origin: null };
  const specs = new Set(files.map(specText));
  const origins = new Set(files.map((f) => f.origin.label));
  return {
    quality: specs.size === 1 ? specText(files[0]) : null,
    origin: origins.size === 1 ? files[0].origin.label : null,
  };
}

/** 单元头上那句共有信息；没有共有的东西就是空串（这时文件行照常各说各的）。 */
export function sharedLine(files: DuplicateFile[]): string {
  const facts = sharedFacts(files);
  return [facts.quality, facts.origin].filter(Boolean).join(" · ");
}

/**
 * 单元内几个文件名的**最长公共前缀**。
 *
 * `三体 S01E16 - 2160p H.265 AAC ADWeb.mp4` 与 `三体 S01E16 - 2160p H.265 AAC.mp4`
 * 的区别只在最后几个字符，而窄屏上文件名是从尾部截断的——两行看起来会一模一样，
 * 用户根本无从选择。把公共前缀单独拎出来淡显并允许截断、差异的尾巴永远完整显示，
 * 窄到什么程度都还能一眼看出差在哪。
 *
 * 两个门槛：前缀短于 8 个字符不值得折（折了反而更碎）；任何一个文件的尾巴为空
 * 也不折（那一行会看起来是空的）。
 */
export function commonNamePrefix(files: DuplicateFile[]): string {
  if (files.length < 2) return "";
  const names = files.map((f) => f.file_name);
  const shortest = Math.min(...names.map((n) => n.length));
  let i = 0;
  while (i < shortest && names.every((n) => n[i] === names[0][i])) i += 1;
  if (i < 8 || names.some((n) => n.length === i)) return "";
  // 差异的尾巴是不截断的（截了就白折了），太长会把行撑破——那种名字直接不折，
  // 走普通的整名截断
  if (Math.max(...names.map((n) => n.length - i)) > 28) return "";
  return names[0].slice(0, i);
}

/** 同构季里几个版本行共有的来源；不同则为 null（这时每行各自显示）。 */
export function sharedVersionOrigin(versions: DuplicateVersion[]): string | null {
  if (versions.length < 2) return null;
  const origins = new Set(versions.map((v) => v.origin_label));
  return origins.size === 1 ? versions[0].origin_label : null;
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
      // 带上这一块会清掉多少——逐条核对时"多大"和"什么规格"一样是决定依据
      const bytes = formatBytes(extras.reduce((n, f) => n + f.size_bytes, 0));
      lines.push(`${head} · ${extras.length} 个文件 · ${bytes} · ${kinds}`);
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
