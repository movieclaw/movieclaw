/**
 * 媒体库管理页（/library/manage）的纯函数：状态归类、筛选、换位。
 *
 * 设计见 docs/design/library-manage.md §2.2。放在这里而不是组件里，是为了
 * 能用 node --test 直接跑单测（本目录的 .ts 纯模块由 node 原生剥类型执行），
 * 因此这里只允许 `import type`，不能引入带 `@/` 别名或浏览器依赖的模块。
 */

import type { MediaLibrary, ScanPhase } from "./api/libraries";
import type { LibraryKind } from "./media-types";

/** 状态列的语气：决定圆点颜色（灰 / 蓝 / 黄 / 红）。 */
export type LibraryStatusTone = "idle" | "busy" | "pending" | "missing";

/** 状态列的种类：决定文案模板与菜单里「扫描」「刷新」的当前形态。 */
export type LibraryStatusKind =
  | "scan"
  | "organize"
  | "refresh"
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

function lastScanDetail(library: MediaLibrary, ctx: LibraryStatusContext): string {
  return library.last_scan ? `最近扫描 ${ctx.relativeTime(library.last_scan.finished_at)}` : "尚未扫描";
}

/**
 * 一行库的状态归类。优先级自上而下取第一个命中（§2.2 状态表）：
 * 扫描 → 整理 → 刷新元数据 → 入库中 → 有缺失 → 有待识别 → 空闲。
 * 三种长任务互斥（共用一把库级锁），所以先后顺序只是兜底。
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
  return {
    tone: "idle",
    kind: "idle",
    title: "空闲",
    detail: `${lastScanDetail(library, ctx)} · 实时监控${library.realtime_watch ? "开" : "关"}`,
    percent: null,
  };
}

/** 是否有长任务在跑（工具栏「N 个在跑任务」与筛选用）。 */
export function libraryIsBusy(library: MediaLibrary): boolean {
  return (
    library.scanning || library.organizing || Boolean(library.metadata_refresh?.refreshing)
  );
}

export interface LibraryFilter {
  /** 搜索词：匹配库名或任一根目录，大小写不敏感；空串不过滤 */
  query: string;
  /** 类型：null 为全部 */
  kind: LibraryKind | null;
  /** 只看在跑任务的库 */
  busyOnly: boolean;
}

export const EMPTY_FILTER: LibraryFilter = { query: "", kind: null, busyOnly: false };

export function filterIsActive(filter: LibraryFilter): boolean {
  return filter.query.trim() !== "" || filter.kind !== null || filter.busyOnly;
}

/** 客户端筛选：库列表规模在几十以内，一次全拉后本地过滤即可。 */
export function filterLibraries(libraries: MediaLibrary[], filter: LibraryFilter): MediaLibrary[] {
  const q = filter.query.trim().toLowerCase();
  return libraries.filter((library) => {
    if (filter.kind !== null && library.kind !== filter.kind) return false;
    if (filter.busyOnly && !libraryIsBusy(library)) return false;
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

/** 可见范围列的文案。 */
export function accessLabel(library: MediaLibrary): string {
  if (!library.viewer_access) return "仅管理";
  if (library.access_mode === "everyone") return "全员";
  const n = library.member_ids.length + (library.admin_visible ? 1 : 0);
  return `指定成员 ${n}`;
}

/** 库存列的文案：影视库按「部」、其他库按「条目」。 */
export function inventoryLabel(library: MediaLibrary): { primary: string; secondary: string } {
  const unit = library.kind === "video" ? "个条目" : "部";
  return {
    primary: `${library.stats.item_count} ${unit}`,
    secondary: `${library.stats.file_count} 个文件`,
  };
}
