/**
 * 媒体库管理页（/library/manage）的纯函数：状态归类、摘要、筛选、换位。
 *
 * 设计见 docs/design/library-manage.md §2.2。放在这里而不是组件里，是为了
 * 能用 node --test 直接跑单测（本目录的 .ts 纯模块由 node 原生剥类型执行），
 * 因此这里只允许 `import type` 与带扩展名的同目录纯模块，不能引入带 `@/`
 * 别名或浏览器依赖的模块。
 */

import type { ChapterJobProgress, MediaLibrary, ScanPhase } from "./api/libraries";
import { formatBytes } from "./format.ts";
import type { LibraryKind } from "./media-types";

/** 状态列的语气：决定圆点 / 胶囊颜色（灰 / 蓝 / 黄 / 红）。 */
export type LibraryStatusTone = "idle" | "busy" | "pending" | "missing";

/** 状态列的种类：决定文案模板与菜单里「扫描」「刷新」的当前形态。 */
export type LibraryStatusKind =
  | "scan"
  | "organize"
  | "refresh"
  | "chapters"
  | "importing"
  | "missing"
  | "unidentified"
  | "idle";

export interface LibraryStatus {
  tone: LibraryStatusTone;
  kind: LibraryStatusKind;
  /** 主文案（第一行） */
  title: string;
  /** 补充文案（第二行）；没有则为空串 */
  detail: string;
  /** 0-100 的进度百分比；分母未知或不是进度型状态时为 null */
  percent: number | null;
}

export interface LibraryStatusContext {
  /** 扫描阶段 → 文案（传 SCAN_PHASE_LABELS；纯模块不直接依赖 API 模块） */
  phaseLabels: Record<ScanPhase, string>;
  /** ISO 时间 → 「X 前」 */
  relativeTime: (iso: string) => string;
}

function percentOf(processed: number, total: number): number | null {
  if (total <= 0) return null;
  return Math.min(100, Math.round((processed / total) * 100));
}

/**
 * 「最近扫描 X 前 · 结论」。扫描常常毫秒级完成（已入库的文件秒过），列表上
 * 唯一能证明"点了有反应"的就是这一行——只写时间的话，一个本就最新的库扫完前后
 * 长得一模一样，用户会以为没点上。结论只挑用户关心的：新增了几个文件、标记了
 * 几个缺失；都没有就明说「无新文件」，手动停过的也如实标出。
 */
function lastScanDetail(library: MediaLibrary, ctx: LibraryStatusContext): string {
  const scan = library.last_scan;
  if (!scan) return "尚未扫描";
  const parts = [`最近扫描 ${ctx.relativeTime(scan.finished_at)}`];
  if (scan.cancelled) parts.push("手动停止");
  if (scan.scanned > 0) parts.push(`新增 ${scan.scanned} 个文件`);
  if (scan.marked_missing > 0) parts.push(`标记缺失 ${scan.marked_missing}`);
  if (!scan.cancelled && scan.scanned === 0 && scan.marked_missing === 0) parts.push("无新文件");
  return parts.join(" · ");
}

/**
 * 一行库的状态归类。优先级自上而下取第一个命中（§2.2 状态表）：
 * 扫描 → 整理 → 刷新元数据 → 生成章节 → 入库中 → 有缺失 → 有待识别 → 空闲。
 * 前三种长任务互斥（共用一把库级锁），所以先后顺序只是兜底；生成章节是
 * 低优先级后台作业，常排在它们后面，排在第四位正好表达"等前面的跑完"。
 */
export function libraryStatus(library: MediaLibrary, ctx: LibraryStatusContext): LibraryStatus {
  if (library.scanning) {
    const p = library.scan_progress;
    const percent = p ? percentOf(p.processed, p.total) : null;
    const label = p ? ctx.phaseLabels[p.phase] ?? "正在扫描" : "正在扫描";
    return {
      tone: "busy",
      kind: "scan",
      title: percent === null ? label : `${label} ${percent}%`,
      detail: p && p.total > 0 ? `${p.processed} / ${p.total}` : "正在统计待处理的文件数",
      percent,
    };
  }
  if (library.organizing) {
    const p = library.organize_progress;
    const percent = p ? percentOf(p.processed, p.total) : null;
    return {
      tone: "busy",
      kind: "organize",
      title: percent === null ? "正在整理文件名" : `正在整理文件名 ${percent}%`,
      detail: p && p.total > 0 ? `${p.processed} / ${p.total}` : "",
      percent,
    };
  }
  const refresh = library.metadata_refresh;
  if (refresh?.refreshing) {
    const percent = percentOf(refresh.processed, refresh.total);
    const active = refresh.active[0];
    return {
      tone: "busy",
      kind: "refresh",
      title: refresh.stopping
        ? "正在停止刷新"
        : percent === null
          ? "刷新元数据"
          : `刷新元数据 ${percent}%`,
      detail: active
        ? `正在处理「${active.title}」· ${active.phase}`
        : `${refresh.processed} / ${refresh.total}`,
      percent,
    };
  }
  const chapters = library.chapter_job;
  if (chapters) {
    const running = chapterJobRunning(chapters);
    const percent = running ? percentOf(chapters.processed, chapters.total) : null;
    return {
      tone: "busy",
      kind: "chapters",
      title: chapterJobLabel(chapters),
      detail: !running
        ? "等前面的任务跑完再开始"
        : chapters.total > 0
          ? `${chapters.processed} / ${chapters.total}${chapters.failed > 0 ? ` · ${chapters.failed} 个失败` : ""}`
          : "正在统计待处理的文件数",
      percent,
    };
  }
  const deferred = library.last_scan?.deferred ?? 0;
  if (deferred > 0) {
    return {
      tone: "busy",
      kind: "importing",
      title: `${deferred} 个新文件入库中`,
      detail: "等文件写完自动补扫",
      percent: null,
    };
  }
  const { unidentified_count: unidentified, missing_count: missing } = library.stats;
  if (missing > 0) {
    return {
      tone: "missing",
      kind: "missing",
      title:
        unidentified > 0 ? `${unidentified} 个待识别 · ${missing} 个缺失` : `${missing} 个缺失`,
      detail: lastScanDetail(library, ctx),
      percent: null,
    };
  }
  if (unidentified > 0) {
    return {
      tone: "pending",
      kind: "unidentified",
      title: `${unidentified} 个待识别`,
      detail: lastScanDetail(library, ctx),
      percent: null,
    };
  }
  // 空闲不是一种需要看的状态：只留「最近扫描 X 前」这行事实；实时监控关了
  // 才作为配置备注出现在库名下（见 configNotes），开着是默认，不占字
  return {
    tone: "idle",
    kind: "idle",
    title: "空闲",
    detail: lastScanDetail(library, ctx),
    percent: null,
  };
}

function chapterJobRunning(job: ChapterJobProgress): boolean {
  return job.status === "running" || job.status === "cancelling";
}

/**
 * 「生成章节」菜单项与状态列共用的一句话：没作业时是动作名，有作业时如实
 * 说到哪了——点了菜单后作业常要排在扫描/刷新后面，只写"生成章节"用户会以为没反应。
 */
export function chapterJobLabel(job: ChapterJobProgress | null | undefined): string {
  if (!job) return "生成章节";
  if (job.stopping) return "正在停止生成章节";
  if (!chapterJobRunning(job)) return "生成章节排队中";
  const percent = percentOf(job.processed, job.total);
  return percent === null ? "正在生成章节" : `正在生成章节 ${percent}%`;
}

/** 是否有长任务在跑（摘要行「N 个在跑任务」与筛选用）。 */
export function libraryIsBusy(library: MediaLibrary): boolean {
  return (
    library.scanning ||
    library.organizing ||
    Boolean(library.metadata_refresh?.refreshing) ||
    library.chapter_job !== null
  );
}

/**
 * 是否有要你动手的文件（待识别或缺失）。口径与 libraryStatus 的优先级一致：
 * 任务在跑或还在入库时状态归进度，不算「待处理」——否则摘要说 3 个库待处理，
 * 筛出来的行却显示扫描进度，对不上号。
 */
export function libraryNeedsAttention(library: MediaLibrary): boolean {
  if (libraryIsBusy(library) || (library.last_scan?.deferred ?? 0) > 0) return false;
  return library.stats.unidentified_count > 0 || library.stats.missing_count > 0;
}

/** 页头摘要：规模事实一句话，加上真正要你看的两个数。 */
export interface LibrarySummary {
  /** `18 个媒体库 · 4896 个条目 · 31.6 TB` */
  facts: string;
  /** 在跑任务的库数 */
  busy: number;
  /** 有待处理文件的库数 */
  attention: number;
  /** 待处理里是否含缺失文件（缺失比待识别更急，摘要胶囊随之用红） */
  missing: boolean;
}

export function summarizeLibraries(libraries: MediaLibrary[]): LibrarySummary {
  const items = libraries.reduce((sum, l) => sum + l.stats.item_count, 0);
  const bytes = libraries.reduce((sum, l) => sum + l.stats.total_size_bytes, 0);
  const needing = libraries.filter(libraryNeedsAttention);
  return {
    facts: `${libraries.length} 个媒体库 · ${items} 个条目 · ${formatBytes(bytes)}`,
    busy: libraries.filter(libraryIsBusy).length,
    attention: needing.length,
    missing: needing.some((l) => l.stats.missing_count > 0),
  };
}

/** 摘要胶囊对应的筛选：只看在跑任务的库 / 只看有待处理的库。 */
export type LibraryFocus = "busy" | "attention";

export interface LibraryFilter {
  /** 搜索词：匹配库名或任一根目录，大小写不敏感；空串不过滤 */
  query: string;
  /** 类型：null 为全部 */
  kind: LibraryKind | null;
  /** 状态：null 为全部 */
  focus: LibraryFocus | null;
}

export const EMPTY_FILTER: LibraryFilter = { query: "", kind: null, focus: null };

export function filterIsActive(filter: LibraryFilter): boolean {
  return filter.query.trim() !== "" || filter.kind !== null || filter.focus !== null;
}

/** 客户端筛选：库列表规模在几十以内，一次全拉后本地过滤即可。 */
export function filterLibraries(libraries: MediaLibrary[], filter: LibraryFilter): MediaLibrary[] {
  const q = filter.query.trim().toLowerCase();
  return libraries.filter((library) => {
    if (filter.kind !== null && library.kind !== filter.kind) return false;
    if (filter.focus === "busy" && !libraryIsBusy(library)) return false;
    if (filter.focus === "attention" && !libraryNeedsAttention(library)) return false;
    if (q === "") return true;
    if (library.name.toLowerCase().includes(q)) return true;
    return library.root_paths.some((root) => root.toLowerCase().includes(q));
  });
}

/**
 * 把 from 位置的元素挪到 to 位置（其余元素相对顺序不变），返回新数组。
 * 拖拽松手与键盘 Alt+↑/↓ 都走这里；越界或原地不动时返回原数组引用，
 * 调用方据此跳过提交。
 */
export function moveInList<T>(list: readonly T[], from: number, to: number): readonly T[] {
  if (from === to) return list;
  if (from < 0 || to < 0 || from >= list.length || to >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/** 可见范围的文案：只看库自己的开放模式（谁能浏览）。
 *  「你本人在不在浏览范围内」是另一个维度，由行内的锁图标表达，不混进这段文字——
 *  否则超管把自己摘出某个库后，管理页只剩「仅管理」三个字，看不出成员到底能不能看。 */
export function accessLabel(library: MediaLibrary): string {
  if (library.access_mode === "everyone") return "全部成员";
  const n = library.member_ids.length;
  if (n > 0) return `指定成员 ${n}`;
  return library.admin_visible ? "仅自己" : "无人可见";
}

/**
 * 可见范围是否偏离默认（对全部成员开放、你自己也能看）。
 * 只有偏离了才在行内挂胶囊：十几行都写着「全部成员」等于什么都没说，
 * 而唯一一行「指定成员 2」才是要被看见的。
 */
export function accessRestricted(library: MediaLibrary): boolean {
  return library.access_mode !== "everyone" || !library.viewer_access;
}

/**
 * 库名下的配置备注：只说偏离默认或有待办的部分，默认态不占字。
 * - 影视库没声明收藏范围是一个待办信号（自动入库不知道该把什么收进来），用警示色；
 *   声明了多少条不说——那是编辑弹窗里的事，列表上逐行重复「N 项条件」只是噪音。
 * - 「在首页展示」「实时监控开」是默认，不说；关了才说。
 */
export interface ConfigNote {
  text: string;
  tone?: "warn";
}

export function configNotes(library: MediaLibrary): ConfigNote[] {
  const notes: ConfigNote[] = [];
  if (library.capabilities.scraped && library.match_rules.length === 0) {
    notes.push({ text: "未声明收藏范围", tone: "warn" });
  }
  if (library.exclude_from_home) notes.push({ text: "从首页排除" });
  if (!library.realtime_watch) notes.push({ text: "实时监控关" });
  return notes;
}

/** 库存列的文案：影视库按「部」、图片库按「张」、其他库按「条目」。 */
export function inventoryLabel(library: MediaLibrary): { primary: string; secondary: string } {
  const unit = library.kind === "photo" ? "张" : library.kind === "video" ? "个条目" : "部";
  return {
    primary: `${library.stats.item_count} ${unit}`,
    secondary: `${library.stats.file_count} 个文件`,
  };
}
