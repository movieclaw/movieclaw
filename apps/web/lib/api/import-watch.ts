import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/**
 * 监听导入规则（媒体库之上的独立功能）：监听一个源目录，目录里下载完成
 * 的内容自动识别并按规范命名搬进目标库主根（硬链接/复制）。
 */
export interface ImportWatchRule {
  id: number;
  /** 源目录（绝对路径，不得与任何库根路径重叠） */
  source_path: string;
  /** 搬运策略：硬链接（零占用需与目标库主根同盘）/ 复制（可跨盘） */
  strategy: "hardlink" | "copy";
  /** 目标库；null=自动路由或自定义目录 */
  library_id: number | null;
  library_name: string | null;
  /** 自定义目录目标（其余目标为 null）：识别改名后落该目录、不进入任何媒体库 */
  target_path: string | null;
  /** 自动路由/自定义目录的媒体类型（指定库时为 null） */
  kind: "movie" | "tv" | "video" | null;
  /** 目标展示名：库名 /「自动路由（电影/剧集）」/「自定义目录 …」 */
  target_label: string;
  /** 是否整理存量（false=跳过存量只处理新增，存量条目被标记为已忽略） */
  process_existing: boolean;
  /** 台账状态计数（imported/pending/failed/skipped/ignored → 条目数） */
  stats: Record<string, number>;
  /** 已入库条目累计入库的文件数（剧集一条目是一个季包，条目数说不清入了几集） */
  imported_files: number;
  /** 已入库的作品数（同一部剧的多季、多版本合并为一部）——「已入库」展示这个数 */
  imported_works: number;
  created_at: string;
}

/** 台账条目状态：处理结论分档（pending 等人拍板，failed 退避重试）。 */
export type IngestEntryStatus = "imported" | "pending" | "failed" | "skipped" | "ignored";

/** 监听目录一个条目的处理台账行。 */
export interface IngestEntryRow {
  id: number;
  /** 条目名（源目录顶层的文件/目录名） */
  name: string;
  entry_path: string;
  status: IngestEntryStatus;
  /** 处理结论（中文） */
  message: string | null;
  imported_count: number;
  attempted_at: string;
}

/** 一条规则的台账清单 + 各状态计数。 */
export interface IngestEntriesData {
  counts: Record<string, number>;
  entries: IngestEntryRow[];
}

/**
 * 创建/更新规则的请求体。目标三态：library_id 指定库；target_path 自定义
 * 目录（与 library_id 互斥）；两者皆空即自动路由。后两态 kind 必填。
 */
export interface ImportWatchPayload {
  source_path: string;
  strategy: "hardlink" | "copy";
  library_id: number | null;
  kind?: "movie" | "tv" | "video" | null;
  target_path?: string | null;
  /** 是否整理存量；false=跳过规则生效时已有的内容，只处理新增 */
  process_existing?: boolean;
}

/** 列出全部监听导入规则。 */
export function listImportWatchRules(): Promise<ImportWatchRule[]> {
  return unwrap(request<ApiEnvelope<ImportWatchRule[]>>("/import-watch"));
}

/** 创建规则（硬链接策略保存即做同盘检测，跨盘会被拒绝并提示改复制）。 */
export function createImportWatchRule(payload: ImportWatchPayload): Promise<ImportWatchRule> {
  return unwrap(
    request<ApiEnvelope<ImportWatchRule>>("/import-watch", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 更新规则。 */
export function updateImportWatchRule(
  id: number,
  payload: ImportWatchPayload,
): Promise<ImportWatchRule> {
  return unwrap(
    request<ApiEnvelope<ImportWatchRule>>(`/import-watch/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 删除规则（不动磁盘，仅停止监听）。 */
export function deleteImportWatchRule(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/import-watch/${id}`, { method: "DELETE" }),
  );
}

/** 一条规则的台账清单（可按状态过滤）。 */
export function listIngestEntries(
  ruleId: number,
  status?: IngestEntryStatus,
): Promise<IngestEntriesData> {
  const query = status ? `?status=${status}` : "";
  return unwrap(
    request<ApiEnvelope<IngestEntriesData>>(`/import-watch/${ruleId}/entries${query}`),
  );
}

/** 忽略条目：永久跳过（可恢复）。 */
export function ignoreIngestEntry(entryId: number): Promise<IngestEntryRow> {
  return unwrap(
    request<ApiEnvelope<IngestEntryRow>>(`/import-watch/entries/${entryId}/ignore`, {
      method: "POST",
    }),
  );
}

/** 恢复条目：重新进入处理流程。 */
export function restoreIngestEntry(entryId: number): Promise<IngestEntryRow> {
  return unwrap(
    request<ApiEnvelope<IngestEntryRow>>(`/import-watch/entries/${entryId}/restore`, {
      method: "POST",
    }),
  );
}

/** 认领条目：钉到指定 TMDB 身份并立即入库；返回处理后的台账行（含结论）。 */
export function claimIngestEntry(entryId: number, tmdbId: number): Promise<IngestEntryRow> {
  return unwrap(
    request<ApiEnvelope<IngestEntryRow>>(`/import-watch/entries/${entryId}/claim`, {
      method: "POST",
      body: JSON.stringify({ tmdb_id: tmdbId }),
    }),
  );
}
