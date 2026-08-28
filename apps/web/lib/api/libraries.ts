import { request } from "@/lib/http";
import type { MediaType } from "@/lib/media-types";

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
 * 媒体库（见 schemas.library.LibraryView）：
 * "我拥有哪些影视内容、放在哪里"的权威定义。订阅/手动下载按
 * "入库到哪个库"确定保存路径（主根/标题 (年份)）。
 */
export interface LibraryStats {
  /** 在位且已识别的媒体条目数 */
  item_count: number;
  /** 在位文件总数（含待识别） */
  file_count: number;
  /** 在位文件的逻辑大小总和 */
  total_size_bytes: number;
  /** 在位待识别文件数（不含已忽略） */
  unidentified_count: number;
  /** 标记 missing 的文件数（缺失清单入口） */
  missing_count: number;
  /** 在位且已忽略的文件数（不再参与识别，可在已忽略清单恢复） */
  ignored_count: number;
}

/**
 * 收藏范围条件（docs/design/library-routing.md）：条件间 AND、条件内任一匹配。
 * genres 存 TMDB 类型 **ID**（语言无关），origin_countries 存 ISO 国家码。
 */
export interface MatchRule {
  field: "genres" | "origin_countries";
  op: "any_of";
  values: (number | string)[];
}

export interface MediaLibrary {
  id: number;
  name: string;
  /** 每库单一类型（movie / tv），创建后不可改 */
  kind: MediaType;
  /** 根路径列表（绝对路径），第一个为主根——新入库落在这里 */
  root_paths: string[];
  primary_root: string | null;
  /** 是否为该类型的默认库（订阅/手动下载不选库时用它） */
  is_default: boolean;
  /** 收藏范围声明；空=未声明（只作为显式指定或默认库兜底的目标） */
  match_rules: MatchRule[];
  /** 扫描后自动清理已确认丢失的库存记录（只删台账不动磁盘，不可恢复） */
  auto_clear_missing: boolean;
  /** 是否启用实时文件监控（关闭后靠定期对账与手动扫描，SMB/NFS 建议关） */
  realtime_watch: boolean;
  /** 库级刮削覆盖；空对象 = 全跟全局设置 */
  scrape_overrides?: Record<string, unknown>;
  /** 库存统计快照（台账变化时重算，列表查询不扫描文件台账） */
  stats: LibraryStats;
  /** 是否正在扫描 */
  scanning: boolean;
  /** 扫描实时进度（没在扫为 null）——前端在库封面上画进度环 */
  scan_progress: ScanProgress | null;
  /** 最近一次扫描结论（扫描常毫秒级完成，靠它给"点了有反应"的反馈） */
  last_scan: LastScan | null;
  /** 是否正在整理文件名（批量规范化改名） */
  organizing: boolean;
  /** 整理实时进度（没在整理为 null），与扫描进度同构 */
  organize_progress: ScanProgress | null;
  /** 最近一次整理结论（整理对话框的完成页数据源） */
  last_organize: LastOrganize | null;
  /**
   * 整库元数据刷新状态；没在刷为 null。随库列表一并下发，媒体库首页的
   * 卡片因此不必额外请求就能显示刷新进度。
   */
  metadata_refresh: MetadataRefreshProgress | null;
  created_at: string;
  updated_at: string;
}

/**
 * 扫描类任务的阶段（后端 library_scan.ScanPhase；organizing 为整理任务复用）。
 * 一次「扫描」内部分好几段，每段的分子分母不同——文案必须按阶段选，
 * 否则文件扫完后还要跑几分钟图片资产，界面会僵在「已处理 = 总数」上。
 */
export type ScanPhase =
  | "walking" // 盘点根路径下的文件（分母未知）
  | "ingesting" // 逐文件走识别链、写台账
  | "probing" // 补探缺介质规格的在位行（分母 = 待补探的台账行数）
  | "assets" // 收尾补齐图片资产（分母 = 本轮新挂锚的条目数）
  | "reidentifying" // 单条目重识别（占同一把库级锁，不可中途停止）
  | "organizing"; // 批量规范化改名

/** 各阶段的界面文案（唯一出处，卡片与详情页共用）。 */
export const SCAN_PHASE_LABELS: Record<ScanPhase, string> = {
  walking: "正在盘点文件",
  ingesting: "正在扫描",
  probing: "正在补探画质与音轨",
  assets: "正在补齐海报与剧照",
  reidentifying: "正在重新识别条目",
  organizing: "正在整理文件名",
};

/** 阶段对应的一句补充说明（详情页胶囊用，讲清"现在等的是什么"）。 */
export const SCAN_PHASE_HINTS: Record<ScanPhase, string> = {
  walking: "正在统计待处理的文件数",
  ingesting: "识别到的内容会自动入库",
  probing: "文件已全部入库，正在读取文件本体的规格",
  assets: "文件已全部入库，正在下载图片",
  reidentifying: "完成后条目身份会更新",
  organizing: "完成后自动刷新",
};

/** 扫描/整理的实时进度。 */
export interface ScanProgress {
  /** 进行中的阶段——决定文案；分子分母随阶段变化 */
  phase: ScanPhase;
  processed: number;
  /** 总数；0 表示分母未知（如盘点阶段），前端画不确定态转圈 */
  total: number;
}

/** 最近一次扫描的结论。 */
export interface LastScan {
  finished_at: string;
  scanned: number;
  identified: number;
  unidentified: number;
  marked_missing: number;
  /** 本轮自动清理出台账的丢失记录数（库开了自动清理才非 0） */
  cleared_missing: number;
  /** 疑似写入中暂缓入账的文件数（稍后自动补扫） */
  deferred: number;
  /** 识别重试数：在位但待识别的文件重走识别链（不算新入账） */
  retried: number;
  /** 本轮扫描被用户手动停止（未扫完，剩余文件下次扫描继续） */
  cancelled: boolean;
  errors: string[];
}

/** 最近一次整理的结论。 */
export interface LastOrganize {
  finished_at: string;
  /** 改名归位的主文件数 */
  renamed: number;
  /** 跟随改名的附属文件数（字幕、分集剧照等） */
  sidecars_renamed: number;
  /** 跟随条目目录改名的镜像资产数（海报/背景/季海报/条目 NFO） */
  entry_assets_moved: number;
  /** 本就符合规范、无需动作的文件数 */
  already_ok: number;
  /** 计划阶段跳过的文件数 */
  skipped: number;
  /** 搬空后清理掉的目录数 */
  removed_dirs: number;
  errors: string[];
}

/** 整理预览里跟随主文件改名的附属文件（字幕等）。 */
export interface OrganizeSidecar {
  source_path: string;
  target_path: string;
}

/** 整理预览里的一条改名计划：旧路径 → 规范路径。 */
export interface OrganizeRename {
  file_id: number;
  /** 所属条目——按条目分组展示 */
  media_item_id: number;
  title: string;
  year: number | null;
  source_path: string;
  target_path: string;
  /** 相对所在库根的旧路径（展示用） */
  source_rel: string;
  /** 相对所在库根的规范路径（展示用） */
  target_rel: string;
  size_bytes: number;
  sidecars: OrganizeSidecar[];
}

/** 整理预览里的一条跳过说明。 */
export interface OrganizeSkip {
  file_path: string;
  reason: string;
}

/** 整理预览：完整的「将要发生什么」清单，用户确认后才执行。 */
export interface OrganizePreview {
  /** 台账在位文件总数（= 改名 + 已规范 + 跳过） */
  total: number;
  /** 已符合规范命名的文件数 */
  already_ok: number;
  renames: OrganizeRename[];
  skips: OrganizeSkip[];
  /**
   * 条目目录改名时跟着搬的镜像资产（poster.jpg / fanart.jpg /
   * seasonNN-poster.jpg / movie.nfo / tvshow.nfo）——不搬走旧目录就清不掉。
   */
  entry_assets: OrganizeSidecar[];
}

/** 库内一个媒体条目的库存聚合（单库海报墙的一格）。 */
export interface LibraryItem {
  media_item_id: number;
  kind: MediaType;
  tmdb_id: number;
  title: string;
  year: number | null;
  poster_url: string | null;
  file_count: number;
  total_size_bytes: number;
  /** 在库的季号列表（电影为空） */
  seasons: number[];
  /** 去重的 (季,集) 单元数（电影为 0） */
  episode_count: number;
  /** 去重的介质规格标签（如 ["2160p"]），探测不到为空 */
  resolutions: string[];
  /** 标记 missing 的文件数（>0 时提示） */
  missing_count: number;
  /** 剧集播出状态：airing=在播 / ended=完结；电影或状态未知为 null */
  air_status: "airing" | "ended" | null;
  /** 已播出但所有媒体库里都没有的正季集数（电影恒 0）——「补齐缺集」的依据 */
  missing_episode_count: number;
  /** 最近一次文件入账时间（ISO 字符串），首页「最近添加」排序依据 */
  added_at: string | null;
  /** 最近一次可追溯入库批次；旧台账和电影为 null */
  recent_addition: {
    season_count: number;
    episode_count: number;
    /** 仅涉及一季时有值 */
    season_number: number | null;
    /** 仅同季连续批次有值 */
    first_episode_number: number | null;
    last_episode_number: number | null;
    complete_season: boolean;
  } | null;
  /** 本库在位剧集相对 TMDB 季集结构的完整度摘要；电影为 null */
  inventory_summary: {
    /** 在位正季数；仅特别篇时为 0 */
    season_count: number;
    episode_count: number;
    /** 只覆盖一季时有值；0 表示特别篇 */
    season_number: number | null;
    /** 摘要覆盖季的官方总集数；元数据不完整时为 null */
    total_episode_count: number | null;
    all_seasons_owned: boolean;
    all_episodes_owned: boolean;
  } | null;
  /** 在位但尚未探出介质规格的文件数——扫描补探阶段据此把「还在处理」的条目排到墙前面 */
  probe_pending_count: number;
}

/** 收敛器判不了时留下的一个候选（点一下即可认领）。 */
export interface UnidentifiedCandidate {
  tmdb_id: number;
  title: string;
  year: number | null;
  /** 该候选对应季的集数——同名双版本（正片 46 集 / 送审版 51 集）靠它一眼区分 */
  episode_count: number | null;
  /** 本地证据对它的佐证（年份相同 / 本地 N 集吻合…） */
  reasons: string[];
}

/** 待识别清单的一个文件。 */
export interface UnidentifiedFile {
  id: number;
  library_id: number;
  library_name: string;
  file_path: string;
  size_bytes: number;
  season_number: number;
  episode_number: number;
  /** 识别失败原因整句（悬停/展开查看；清单上只显示标签）；旧数据可能为 null */
  reason: string | null;
  /** 失败分类：决定标签措辞、配色与可用动作；旧数据为 null */
  code: UnidentifiedCode | null;
  candidates: UnidentifiedCandidate[];
}

/** 识别失败的分类（见 movieclaw_db.models.library_file.UnidentifiedCode）。 */
export type UnidentifiedCode =
  | "unparsable"
  | "tmdb_unreachable"
  | "ambiguous"
  | "no_match"
  | "kind_mismatch";

/** 待识别清单的一组：同一条目目录下的文件（一部剧几十集算一组）。 */
export interface UnidentifiedGroup {
  /** 分组键：条目目录绝对路径；裸文件用文件自身路径 */
  key: string;
  /** 展示名：条目目录名（裸文件为文件名） */
  label: string;
  library_id: number;
  library_name: string;
  file_count: number;
  total_size_bytes: number;
  /** 组内共同的失败原因整句（悬停查看） */
  reason: string | null;
  /** 组内共同的失败分类：决定标签与配色 */
  code: UnidentifiedCode | null;
  /** 组内共同的候选：点一下整组认领 */
  candidates: UnidentifiedCandidate[];
  files: UnidentifiedFile[];
}

/** 创建/更新库的请求体。kind 仅创建时生效。 */
export interface LibraryPayload {
  name: string;
  kind: MediaType;
  root_paths: string[];
  /** 收藏范围条件；缺省/空=不声明 */
  match_rules?: MatchRule[];
  /** 扫描后自动清理已确认丢失的库存记录；不传=不改动（新建时默认关） */
  auto_clear_missing?: boolean;
  /** 是否启用实时文件监控；不传=不改动（新建时默认开） */
  realtime_watch?: boolean;
  /** 库级刮削覆盖（命名模板与目录写入细项）；不传=不改动，空对象=清空覆盖。
   *  选图与语言不支持按库覆盖——它们的产物跨库共享一份 */
  scrape_overrides?: Record<string, unknown>;
}

/** 收藏范围的可选项（后端唯一真相源：类型 chips 与区域预设）。 */
export interface RoutingOptions {
  movie_genres: { id: number; label: string }[];
  tv_genres: { id: number; label: string }[];
  region_presets: { key: string; label: string; countries: string[] }[];
  /** 国家码 → 中文名（映射外的少见国家直接显示码） */
  country_names: Record<string, string>;
}

let _routingOptionsCache: Promise<RoutingOptions> | null = null;

/** 收藏范围可选项（进程内缓存：静态常量，一次拉取全程复用）。 */
export function listLibraryRoutingOptions(): Promise<RoutingOptions> {
  if (!_routingOptionsCache) {
    _routingOptionsCache = unwrap(
      request<ApiEnvelope<RoutingOptions>>("/libraries/routing-options"),
    ).catch((e) => {
      _routingOptionsCache = null;
      throw e;
    });
  }
  return _routingOptionsCache;
}

/** 列出全部媒体库（可按类型过滤）。 */
export function listLibraries(kind?: MediaType, init?: RequestInit): Promise<MediaLibrary[]> {
  const qs = kind ? `?kind=${kind}` : "";
  return unwrap(request<ApiEnvelope<MediaLibrary[]>>(`/libraries${qs}`, init));
}

// 默认库的轻量缓存：搜索结果页大量下载按钮共享一次 /libraries 请求，
// 60 秒后过期重取（库配置变更不常见，误差可接受）。
let _libraryCache: { at: number; promise: Promise<MediaLibrary[]> } | null = null;

/** 某类型的默认库（带 60s 缓存）；没有任何库或请求失败返回 null。 */
export async function defaultLibraryFor(kind: MediaType | null | undefined): Promise<MediaLibrary | null> {
  if (!kind) return null;
  const now = Date.now();
  if (!_libraryCache || now - _libraryCache.at > 60_000) {
    _libraryCache = { at: now, promise: listLibraries() };
  }
  try {
    const libs = await _libraryCache.promise;
    return libs.find((l) => l.kind === kind && l.is_default) ?? null;
  } catch {
    _libraryCache = null;
    return null;
  }
}

/** 创建媒体库（该类型首个库自动成为默认）。 */
export function createLibrary(payload: LibraryPayload): Promise<MediaLibrary> {
  return unwrap(
    request<ApiEnvelope<MediaLibrary>>("/libraries", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 更新媒体库（名称与根路径）。 */
export function updateLibrary(id: number, payload: LibraryPayload): Promise<MediaLibrary> {
  return unwrap(
    request<ApiEnvelope<MediaLibrary>>(`/libraries/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 设为该类型的默认库。同类型其他库的默认标记随之取消，调用后应整体刷新列表。 */
export function setDefaultLibrary(id: number): Promise<MediaLibrary> {
  return unwrap(
    request<ApiEnvelope<MediaLibrary>>(`/libraries/${id}/default-selection`, {
      method: "POST",
    }),
  );
}

/**
 * 重排媒体库展示顺序（决定首页卡片与「最近添加」分区的排列）。
 * 必须一次传入全部库的 id——漏/多/重复都会被后端拒绝。
 */
export function reorderLibraries(orderedIds: number[]): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/libraries/display-order`, {
      method: "PUT",
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),
  );
}

/** 删除媒体库（不动磁盘文件；其订阅回落到该类型默认库）。 */
export function deleteLibrary(id: number): Promise<Record<string, never>> {
  return unwrap(request<ApiEnvelope<Record<string, never>>>(`/libraries/${id}`, { method: "DELETE" }));
}

/** 海报墙排序：按标题 / 最近入账 / 待补探优先（扫描补探阶段）。 */
export type LibraryItemSort = "title" | "added_at" | "probing";

/**
 * 库内媒体条目的库存聚合（单库海报墙数据源）。
 *
 * 排序与分页都在服务端做：调用方要几格就取几格——首页「最近添加」只要 20 格，
 * 海报墙滚动加载一次要一屏，不给 limit 才是整库（几千条目要几百 KB，别默认走这条）。
 */
export function listLibraryItems(
  id: number,
  params?: { sort?: LibraryItemSort; limit?: number; offset?: number },
): Promise<LibraryItem[]> {
  const query = new URLSearchParams();
  if (params?.sort) query.set("sort", params.sort);
  if (params?.limit !== undefined) query.set("limit", String(params.limit));
  if (params?.offset) query.set("offset", String(params.offset));
  const suffix = query.size > 0 ? `?${query}` : "";
  return unwrap(request<ApiEnvelope<LibraryItem[]>>(`/libraries/${id}/items${suffix}`));
}

/** 库内条目 id 集合：海报墙分页后仍要整份「已入库」名单（判定订阅是否还在追踪中）。 */
export function listLibraryItemIds(id: number): Promise<number[]> {
  return unwrap(request<ApiEnvelope<number[]>>(`/libraries/${id}/item-ids`));
}

/** 媒体库搜索结果的一组：一个库内命中关键词的条目（组内按标题拼音排序）。 */
export interface LibrarySearchGroup {
  library_id: number;
  library_name: string;
  kind: MediaType;
  items: LibraryItem[];
}

/** 海报墙 A-Z 索引条的一档（按标题排序下的首字母分组）。 */
export interface LibraryIndexEntry {
  /** 首字母档：A-Z；数字/符号/假名等落不进的归 # */
  initial: string;
  count: number;
  /** 该档第一格的位置——即 listLibraryItems 的 offset 取值 */
  offset: number;
}

/** 海报墙的首字母索引（只回非空档）。中文按拼音首字母分档，与按标题排序同源。 */
export function listLibraryItemIndex(id: number): Promise<LibraryIndexEntry[]> {
  return unwrap(request<ApiEnvelope<LibraryIndexEntry[]>>(`/libraries/${id}/item-index`));
}

/** 触发一次可恢复的库扫描；重复点击复用同一条后台作业。 */
export function startLibraryScan(
  id: number,
): Promise<{ started: boolean; message: string; job_id: string; created: boolean }> {
  return unwrap(
    request<
      ApiEnvelope<{ started: boolean; message: string; job_id: string; created: boolean }>
    >(`/libraries/${id}/scan`, { method: "POST" }),
  );
}

/** 停止进行中的扫描（已入账的保留，剩余文件下次扫描继续；没在扫返回 409）。 */
export function stopLibraryScan(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/libraries/${id}/scan/stop`, {
      method: "POST",
    }),
  );
}

/** 获取单个媒体库详情（整理对话框轮询进度用）。 */
export function getLibrary(id: number): Promise<MediaLibrary> {
  return unwrap(request<ApiEnvelope<MediaLibrary>>(`/libraries/${id}`));
}

/** 预览整理计划：每个文件改成什么名、哪些跳过及原因（只读，不动磁盘）。 */
export function previewLibraryOrganize(id: number): Promise<OrganizePreview> {
  return unwrap(
    request<ApiEnvelope<OrganizePreview>>(`/libraries/${id}/file-organization-preview`, {
      method: "POST",
    }),
  );
}

/** 开始整理：按规范命名批量改名归位（后台执行，与扫描互斥）。 */
export interface PersistentJobStart {
  started: boolean;
  message?: string;
  job_id: string;
  created: boolean;
}

export function startLibraryOrganize(id: number): Promise<PersistentJobStart> {
  return unwrap(
    request<ApiEnvelope<PersistentJobStart>>(`/libraries/${id}/file-organizations`, {
      method: "POST",
    }),
  );
}

/** 整库刷新中正在处理的一部片（并发若干路，故是列表）。 */
export interface RefreshActive {
  media_item_id: number;
  title: string;
  /** 当前阶段：拉取 TMDB 档案 / 写入元数据 / 下载图片 / 写入媒体目录 */
  phase: string;
}

/** 整库元数据刷新的实时状态（全量重刷很慢，状态尽量完整）。 */
export interface MetadataRefreshProgress {
  refreshing: boolean;
  /** 已完成条目数（含失败） */
  processed: number;
  total: number;
  /** 刮削失败的条目数（多为 TMDB 不可达） */
  failed: number;
  /** 已请求停止，正在收尾 */
  stopping: boolean;
  /** 正在处理的条目及其阶段 */
  active: RefreshActive[];
}

/**
 * 整库刷新元数据：全部已识别条目**全量重刮**（重拉档案 + 按当前尺寸档位
 * 重下图片 + 覆盖媒体目录镜像）。后台执行、并发 3 路；重复触发复用同一作业。
 */
export function startLibraryMetadataRefresh(id: number): Promise<PersistentJobStart> {
  return unwrap(
    request<ApiEnvelope<PersistentJobStart>>(`/libraries/${id}/metadata/refresh`, {
      method: "POST",
    }),
  );
}

/** 停止进行中的整库元数据刷新（已刷完的保留）。 */
export function stopLibraryMetadataRefresh(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/libraries/${id}/metadata/refresh/stop`, {
      method: "POST",
    }),
  );
}

/** 整库元数据刷新进度（刷新进行中轮询用）。 */
export function getMetadataRefreshProgress(id: number): Promise<MetadataRefreshProgress> {
  return unwrap(
    request<ApiEnvelope<MetadataRefreshProgress>>(`/libraries/${id}/metadata/refresh/progress`),
  );
}

/** 刷新单个条目的元数据：强制重刮 TMDB 并重新下载图片（后台执行）。 */
export function refreshItemMetadata(
  libraryId: number,
  mediaItemId: number,
): Promise<PersistentJobStart> {
  return unwrap(
    request<ApiEnvelope<PersistentJobStart>>(
      `/libraries/${libraryId}/items/${mediaItemId}/metadata/refresh`,
      { method: "POST" },
    ),
  );
}

/** 「更换图片」弹层里的一张候选图。 */
export interface ArtworkCandidate {
  /** TMDB 图片路径（选定时原样回传） */
  file_path: string;
  /** 缩略预览地址（TMDB 图床绝对地址，展示前需 cachedImageUrl） */
  preview_url: string;
  width: number | null;
  height: number | null;
  /** 图上文字的语言；null=无文字（背景首选这类） */
  language: string | null;
  vote_average: number | null;
  vote_count: number | null;
}

/** 条目的候选图集合（排序与自动选图一致）。 */
export interface ArtworkCandidates {
  posters: ArtworkCandidate[];
  backdrops: ArtworkCandidate[];
  /** 实际在用的图路径——标「当前」用它比对，不能用"列表第一张"推断 */
  current_poster: string | null;
  current_backdrop: string | null;
  /** 已手动选定，刷新不会覆盖 */
  poster_locked: boolean;
  backdrop_locked: boolean;
}

/** 条目的候选海报/背景（「更换图片」弹层数据源）。 */
export function listArtworkCandidates(
  libraryId: number,
  mediaItemId: number,
): Promise<ArtworkCandidates> {
  return unwrap(
    request<ApiEnvelope<ArtworkCandidates>>(
      `/libraries/${libraryId}/items/${mediaItemId}/artwork/candidates`,
    ),
  );
}

/**
 * 选定海报/背景：当场落盘并覆盖媒体目录，此后刷新不再覆盖。
 * `filePath` 传 null = 解锁并恢复自动选图。
 */
export function selectArtwork(
  libraryId: number,
  mediaItemId: number,
  kind: "poster" | "backdrop",
  filePath: string | null,
): Promise<{ locked: boolean }> {
  return unwrap(
    request<ApiEnvelope<{ locked: boolean }>>(
      `/libraries/${libraryId}/items/${mediaItemId}/artwork/select`,
      { method: "POST", body: JSON.stringify({ kind, file_path: filePath }) },
    ),
  );
}

/** 待识别清单，按条目目录分组（不含已忽略，可按库过滤）。 */
export function listUnidentifiedLibraryFiles(libraryId?: number): Promise<UnidentifiedGroup[]> {
  const qs = libraryId != null ? `?library_id=${libraryId}` : "";
  return unwrap(
    request<ApiEnvelope<UnidentifiedGroup[]>>(
      `/libraries/identification/unidentified-files${qs}`,
    ),
  );
}

/** 已忽略清单（用户说过「别再问」的文件），同样按条目目录分组。 */
export function listIgnoredLibraryFiles(libraryId?: number): Promise<UnidentifiedGroup[]> {
  const qs = libraryId != null ? `?library_id=${libraryId}` : "";
  return unwrap(
    request<ApiEnvelope<UnidentifiedGroup[]>>(`/libraries/identification/ignored-files${qs}`),
  );
}

/** 恢复已忽略的文件：清掉忽略标记，重新参与识别。 */
export function restoreIgnoredLibraryFiles(fileIds: number[]): Promise<{ restored: number }> {
  return unwrap(
    request<ApiEnvelope<{ restored: number }>>(
      `/libraries/identification/ignored-file-restorations`,
      {
        method: "POST",
        body: JSON.stringify({ file_ids: fileIds }),
      },
    ),
  );
}

/** 认领待识别文件：挂到指定 TMDB 条目。 */
export function assignLibraryFileToTitle(
  fileId: number,
  payload: { title_ref: string; season_number?: number; episode_number?: number },
): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/libraries/identification/files/${fileId}/title-assignment`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  );
}

/** 整组认领：一次把多个待识别文件挂到同一个 TMDB 条目（各自沿用已解析的季集号）。 */
export function assignLibraryFilesToTitle(
  fileIds: number[],
  titleRef: string,
): Promise<{ claimed: number }> {
  return unwrap(
    request<ApiEnvelope<{ claimed: number }>>(
      `/libraries/identification/file-title-assignments`,
      {
        method: "POST",
        body: JSON.stringify({ file_ids: fileIds, title_ref: titleRef }),
      },
    ),
  );
}

/**
 * 标为「非独立作品」：摘掉身份锚并忽略（花絮/预告被高置信错挂时的出口）。
 * 不动磁盘，可在「已忽略」清单恢复。
 */
export function markLibraryFilesAsExtras(fileIds: number[]): Promise<{ detached: number }> {
  return unwrap(
    request<ApiEnvelope<{ detached: number }>>(
      `/libraries/identification/files/mark-as-extras`,
      { method: "POST", body: JSON.stringify({ file_ids: fileIds }) },
    ),
  );
}

/** 身份复核里的一方（现身份 / 建议身份）。 */
export interface ReviewItemInfo {
  media_item_id: number;
  tmdb_id: number | null;
  title: string;
  year: number | null;
  poster_url: string | null;
}

/** 身份复核清单的一组：识别器升级后新旧结论不一致、等用户拍板的文件。 */
export interface ReviewGroup {
  key: string;
  /** 展示名：条目目录名（裸文件为文件名） */
  label: string;
  library_id: number;
  library_name: string;
  file_count: number;
  total_size_bytes: number;
  file_ids: number[];
  /** 现挂身份 */
  current: ReviewItemInfo;
  /** 新识别器给出的建议身份 */
  suggestion: ReviewItemInfo;
}

/** 身份复核清单（可按库过滤）。 */
export function listLibraryIdentityReviewCases(libraryId?: number): Promise<ReviewGroup[]> {
  const qs = libraryId != null ? `?library_id=${libraryId}` : "";
  return unwrap(
    request<ApiEnvelope<ReviewGroup[]>>(`/libraries/identification/review-cases${qs}`),
  );
}

/** 身份复核拍板：明确采纳建议或维持现状；整组生效。 */
export function resolveLibraryIdentityReview(
  fileIds: number[],
  decision: "accept_suggestion" | "keep_current",
): Promise<{ resolved: number }> {
  return unwrap(
    request<ApiEnvelope<{ resolved: number }>>(
      `/libraries/identification/review-decisions`,
      { method: "POST", body: JSON.stringify({ file_ids: fileIds, decision }) },
    ),
  );
}

/** 从台账忽略一个待识别文件（不动磁盘）。 */
export function ignoreUnidentifiedLibraryFile(fileId: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/libraries/identification/files/${fileId}/ignore`,
      { method: "POST" },
    ),
  );
}

/** 批量忽略整库的待识别文件（可恢复，不动磁盘）。 */
export function ignoreAllUnidentifiedLibraryFiles(
  libraryId: number,
): Promise<{ cleared: number }> {
  return unwrap(
    request<ApiEnvelope<{ cleared: number }>>(
      `/libraries/identification/unidentified-file-ignores`,
      { method: "POST", body: JSON.stringify({ library_id: libraryId }) },
    ),
  );
}

/** 片源标注预览的一行：将被标注的文件（片源未知或此前人工标注）。 */
export interface MediaSourceAnnotationCandidate {
  file_id: number;
  file_name: string;
  episode_number: number;
  size_bytes: number;
  media_source: string | null;
  media_source_manual: boolean;
}

/** 列出整季片源标注将影响的文件（弹窗预览用）。 */
export function listMediaSourceAnnotationCandidates(
  mediaItemId: number,
  seasonNumber: number,
): Promise<MediaSourceAnnotationCandidate[]> {
  const params = new URLSearchParams({
    media_item_id: String(mediaItemId),
    season_number: String(seasonNumber),
  });
  return unwrap(
    request<ApiEnvelope<MediaSourceAnnotationCandidate[]>>(
      `/libraries/media-source-annotations/candidates?${params}`,
    ),
  );
}

/** 整季人工标注片源（docs/design/media-source-annotation.md）；电影季号传 0。 */
export function annotateMediaSource(
  mediaItemId: number,
  seasonNumber: number,
  mediaSource: string,
): Promise<{ files: number; snapshots: number }> {
  return unwrap(
    request<ApiEnvelope<{ files: number; snapshots: number }>>(
      `/libraries/media-source-annotations`,
      {
        method: "POST",
        body: JSON.stringify({
          media_item_id: mediaItemId,
          season_number: seasonNumber,
          media_source: mediaSource,
        }),
      },
    ),
  );
}

/** 缺失清单里的一个文件。 */
export interface MissingFile {
  id: number;
  file_path: string;
  season_number: number;
  episode_number: number;
  size_bytes: number;
}

/** 缺失清单的一行：按媒体条目聚合。 */
export interface MissingItem {
  media_item_id: number;
  kind: MediaType;
  tmdb_id: number;
  title: string;
  year: number | null;
  poster_url: string | null;
  /** 该条目已有订阅时给出——清理前提示（订阅可能重新下回来） */
  subscription_id: number | null;
  files: MissingFile[];
}

/** 缺失清单（文件已不在磁盘的库存，按条目聚合）。 */
export function listMissingLibraryFiles(libraryId: number): Promise<MissingItem[]> {
  return unwrap(request<ApiEnvelope<MissingItem[]>>(`/libraries/${libraryId}/missing`));
}

/** 清理缺失记录（只删台账，绝不动磁盘）；不传 mediaItemId 清整库。 */
export function clearMissingLibraryRecords(
  libraryId: number,
  mediaItemId?: number,
): Promise<{ cleared: number }> {
  return unwrap(
    request<ApiEnvelope<{ cleared: number }>>(`/libraries/missing-record-clearances`, {
      method: "POST",
      body: JSON.stringify({ library_id: libraryId, media_item_id: mediaItemId ?? null }),
    }),
  );
}

/** 一条音轨（ffprobe 探测；字段 null=该项探不出）。 */
export interface AudioStream {
  codec: string | null;
  /** 编码档次（如 DTS-HD MA），比 codec 更贴近用户认知 */
  profile: string | null;
  channels: number | null;
  /** 声道布局（如 5.1(side)） */
  channel_layout: string | null;
  /** 语言标签（ISO 639，如 chi/eng） */
  language: string | null;
  title: string | null;
  default: boolean;
}

/** 一条字幕：内封轨（ffprobe）或外挂文件（目录发现）。 */
export interface SubtitleStream {
  /** 内封轨编码（subrip/ass/pgs…）；外挂为文件扩展名 */
  codec: string | null;
  language: string | null;
  title: string | null;
  forced: boolean;
  default: boolean;
  /** 是否外挂字幕文件 */
  external: boolean;
  /** 外挂字幕的文件名 */
  file_name: string | null;
}

/** 字幕预览中的一条时间轴对白。 */
export interface SubtitleCue {
  start_ms: number;
  end_ms: number;
  text: string;
}

/** 字幕标签点击后加载的结构化预览。 */
export interface SubtitlePreview {
  track: string;
  format: string | null;
  event_count: number;
  cues: SubtitleCue[];
}

/** 条目详情页的一个物理文件（一个版本 / 一集）。 */
export interface LibraryItemFile {
  id: number;
  file_path: string;
  file_name: string;
  size_bytes: number;
  container: string | null;
  resolution: string | null;
  video_codec: string | null;
  hdr: string | null;
  bit_depth: number | null;
  duration_seconds: number | null;
  bit_rate: number | null;
  frame_rate: number | null;
  color_space: string | null;
  media_source: string | null;
  /** 片源为人工标注（含 user-lowest 哨兵），自动解析不会覆盖 */
  media_source_manual: boolean;
  release_group: string | null;
  /** imported（入库管线）/ scanned（存量扫描） */
  source: string;
  season_number: number;
  episode_number: number;
  /** 文件当前不在磁盘（missing 标记） */
  missing: boolean;
  /** 生命周期：in_place 在位 / missing 缺失 / trashed 待回收 */
  state: "in_place" | "missing" | "trashed";
  /** 待回收的预计自动清理时间；null 且 trashed = 做种保护，不自动删 */
  purge_after: string | null;
  /** 待回收原因（中文整句，含触发方） */
  trash_note: string | null;
  /** 音轨列表；null=尚未探测（ffprobe 缺失或文件不可达） */
  audio_streams: AudioStream[] | null;
  /** 字幕列表：内封轨 + 外挂文件 */
  subtitle_streams: SubtitleStream[];
  added_at: string;
}

/** 本地刮削（NFO）的一位演员。 */
export interface LocalActor {
  name: string;
  role: string | null;
  thumb_url: string | null;
  /** TMDB 影人 ID：有值时详情页把这一格链到人物页（/people/[id]）；
   *  第三方刮削器写的老 NFO 里可能没有，后端会尽力按姓名从库内档案回填 */
  tmdb_person_id: number | null;
}

/** 从库内人物关系表读取的一位导演，与 Jellyfin People 数据同源。 */
export interface LocalDirector {
  name: string;
  thumb_url: string | null;
  tmdb_person_id: number;
}

/** 条目的展示元数据：本地 NFO > 库内刮削档案 > TMDB 实时兜底。 */
export interface LocalMeta {
  plot: string | null;
  rating: number | null;
  runtime_minutes: number | null;
  genres: string[];
  directors: string[];
  /** 旧条目未建立人物关系时为空，页面回退 directors 姓名占位。 */
  director_credits: LocalDirector[];
  actors: LocalActor[];
  /** 来源 NFO 文件名（source=nfo 时给出） */
  nfo_name: string;
  /** 信息出处：nfo=本地刮削 / db=库内档案 / tmdb=实时兜底 */
  source: "nfo" | "db" | "tmdb";
}

/**
 * 条目详情页的完整数据。图片 URL 两种形态：本地美术图是 /libraries/... 的
 * API 相对路径（resolveRequestUrl 解析），TMDB 图床是 http 绝对地址
 * （cachedImageUrl 走缓存代理）。
 */
export interface LibraryItemDetail {
  media_item_id: number;
  kind: MediaType;
  tmdb_id: number;
  imdb_id: string | null;
  douban_id: string | null;
  title: string;
  original_title: string;
  year: number | null;
  poster_url: string | null;
  backdrop_url: string | null;
  /** NFO 本地刮削元数据；目录里没有可用 NFO 时为 null */
  local_meta: LocalMeta | null;
  /** 条目在磁盘上的目录（删除确认时展示） */
  entry_dirs: string[];
  files: LibraryItemFile[];
  file_count: number;
  total_size_bytes: number;
  /** 季号列表：库内实有 ∪ 元数据季集结构（电影为空） */
  seasons: number[];
  /**
   * 该条目正在后台刮削元数据。状态在服务端，所以离开页面 / 刷新浏览器 /
   * 换台设备打开，都能看到"还在刮"并等到它结束。
   */
  scraping: boolean;
  /** 刮削当前阶段（与整库刷新同一套文案）；没在刮为 null */
  scraping_phase: string | null;
}

/** 剧集分集区的一集（季集结构 + 本地分集刮削 + TMDB 兜底的合并结果）。 */
export interface LibraryEpisode {
  episode_number: number;
  name: string | null;
  /** 分集简介 */
  overview: string | null;
  air_date: string | null;
  /** 分集剧照：本地缩略图接口相对路径或 TMDB 图床地址；无为 null */
  still_url: string | null;
  /** 该集有在位文件；false=缺集或文件缺失（置灰展示） */
  owned: boolean;
  /** 该集的台账文件 id（在详情的 files 里查规格） */
  file_ids: number[];
  /** 当前观看者上次看到的位置（毫秒）；0=没看过或已重置 */
  position_ms: number;
  /** 当前观看者已看完该集（右上角绿色对勾，与首页最近观看同款） */
  played: boolean;
  /** 观看进度 1~99；已看完由 played 表达，不给百分比 */
  progress_percent: number | null;
}

/** 一季的分集清单。 */
export interface SeasonEpisodes {
  season_number: number;
  episodes: LibraryEpisode[];
}

/** 剧集条目一季的分集清单（分集横滚区数据源）。 */
export function getItemEpisodes(
  libraryId: number,
  mediaItemId: number,
  seasonNumber: number,
): Promise<SeasonEpisodes> {
  return unwrap(
    request<ApiEnvelope<SeasonEpisodes>>(
      `/libraries/${libraryId}/items/${mediaItemId}/episodes?season_number=${seasonNumber}`,
    ),
  );
}

/** 单条目重新识别的结论。 */
export interface ReidentifyResult {
  total: number;
  identified: number;
  unidentified: number;
  skipped_missing: number;
  /** TMDB 网络类失败、保留原身份的文件数（修复网络后可重试） */
  kept_on_error: number;
  /** 识别结果与原身份是否不同 */
  changed: boolean;
  /** 全部文件收敛到的新条目；识别失败或分裂为多个条目时为 null */
  new_media_item_id: number | null;
  new_title: string | null;
  /** 身份由目录名 tmdbid 标记或 NFO 钉死——结果不满意需先改标记/NFO 或人工认领 */
  pinned_identity: boolean;
  message: string;
}

/** 条目真实删除的结论。 */
export interface ItemDeleteResult {
  /** 实际从磁盘删除的目录/文件 */
  removed_paths: string[];
  rows_deleted: number;
  freed_bytes: number;
  errors: string[];
}

/** 条目详情：基本信息 + NFO 本地刮削元数据 + 逐文件真实介质规格。 */
export function getLibraryItemDetail(
  libraryId: number,
  mediaItemId: number,
): Promise<LibraryItemDetail> {
  return unwrap(
    request<ApiEnvelope<LibraryItemDetail>>(`/libraries/${libraryId}/items/${mediaItemId}`),
  );
}

/** 读取一条外挂或文本内封字幕，并返回去样式后的时间轴对白。 */
export function getSubtitlePreview(
  fileId: number,
  track: string,
  signal?: AbortSignal,
): Promise<SubtitlePreview> {
  const query = new URLSearchParams({ track });
  return unwrap(
    request<ApiEnvelope<SubtitlePreview>>(
      `/libraries/files/${fileId}/subtitles/preview?${query.toString()}`,
      { signal },
    ),
  );
}

/** 删除一个外挂字幕文件后的回执。 */
export interface SubtitleDeleteResult {
  /** 已删除的字幕文件完整路径 */
  path: string;
  freed_bytes: number;
}

/**
 * 从磁盘删除一个**外挂**字幕文件（AI 生成的字幕同样是外挂 sidecar）。
 * 内封轨长在视频容器里，不走这条路径；调用前必须把文件名列给用户确认。
 */
export function deleteExternalSubtitle(
  fileId: number,
  filename: string,
): Promise<SubtitleDeleteResult> {
  const query = new URLSearchParams({ filename });
  return unwrap(
    request<ApiEnvelope<SubtitleDeleteResult>>(
      `/libraries/files/${fileId}/subtitles?${query.toString()}`,
      { method: "DELETE" },
    ),
  );
}

/**
 * 从磁盘**彻底删除**条目——整个刮削目录（视频+NFO+海报+字幕）一起清除。
 * 全站唯一会动磁盘的删除接口，调用前必须经过明确的二次确认。
 */
export function deleteLibraryItem(
  libraryId: number,
  mediaItemId: number,
): Promise<ItemDeleteResult> {
  return unwrap(
    request<ApiEnvelope<ItemDeleteResult>>(`/libraries/${libraryId}/items/${mediaItemId}`, {
      method: "DELETE",
    }),
  );
}

/**
 * 从磁盘删除条目的**单个文件**（含同名 NFO/字幕/图片附属文件）——多版本
 * 洗掉一个、删某集重下的出口。同样会真删磁盘，调用前必须明确二次确认；
 * 该文件是条目在本库的最后一个文件时会升级为整条目删除（确认界面须告知）。
 */
/** 恢复待回收的文件为在位版本（原路径被占用时后端报可读错误）。 */
export function restoreLibraryFile(
  libraryId: number,
  mediaItemId: number,
  fileId: number,
): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/libraries/${libraryId}/items/${mediaItemId}/files/${fileId}/restore`,
      { method: "POST" },
    ),
  );
}

/** 立即清理待回收的文件（真删磁盘；做种保护形态需先向用户确认断种风险）。 */
export function purgeLibraryFile(
  libraryId: number,
  mediaItemId: number,
  fileId: number,
): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/libraries/${libraryId}/items/${mediaItemId}/files/${fileId}/purge`,
      { method: "POST" },
    ),
  );
}

export function deleteLibraryFile(
  libraryId: number,
  mediaItemId: number,
  fileId: number,
): Promise<ItemDeleteResult> {
  return unwrap(
    request<ApiEnvelope<ItemDeleteResult>>(
      `/libraries/${libraryId}/items/${mediaItemId}/files/${fileId}`,
      { method: "DELETE" },
    ),
  );
}

/** 「修正识别结果」预览里一组文件的识别结论。 */
export interface ReidentifyOutcome {
  media_item_id: number | null;
  tmdb_id: number | null;
  title: string | null;
  year: number | null;
  poster_url: string | null;
  /** 身份来源：path_tag=目录名标记 / nfo / resolved=名称收敛 */
  source: string | null;
  /** 结论与条目现有身份一致 */
  same_as_current: boolean;
  /** 没命中时的中文原因 */
  reason: string | null;
  /** 没命中时的失败分类 */
  code: string | null;
  candidates: UnidentifiedCandidate[];
}

/** 预览的一组：识别结论相同的文件聚成一条，用户按组拍板。 */
export interface ReidentifyGroup {
  key: string;
  outcome: ReidentifyOutcome;
  file_ids: number[];
  file_count: number;
  total_size_bytes: number;
  sample_names: string[];
}

/** 「修正识别结果」第一阶段的结论——尚未落库，关掉面板则台账零改动。 */
export interface ReidentifyPreview {
  /** 条目当前挂着的身份 */
  current: ReviewItemInfo;
  movie: boolean;
  groups: ReidentifyGroup[];
  skipped_missing: number;
  /** 身份被目录名 tmdbid 标记或 NFO 钉死——改了还会被扫描改回去 */
  pinned_identity: boolean;
  /** 有文件因 TMDB 不通而无结论，此刻不宜拍板 */
  unreachable: boolean;
  /** 「自己搜」的预填词（识别链解析出的片名） */
  search_seed: string;
}

/**
 * 修正识别结果（预览）：重走识别链只出结论，**不改任何台账**。
 * 拍板由 assignLibraryFilesToTitle（改挂）/ markLibraryFilesAsExtras（花絮）落库。
 */
export function previewItemReidentification(
  libraryId: number,
  mediaItemId: number,
): Promise<ReidentifyPreview> {
  return unwrap(
    request<ApiEnvelope<ReidentifyPreview>>(
      `/libraries/${libraryId}/items/${mediaItemId}/reidentification-preview`,
      { method: "POST" },
    ),
  );
}

/** 重新识别条目：全部在位文件重走识别链（NFO → 名称解析 → TMDB 收敛）。 */
export function reidentifyLibraryItem(
  libraryId: number,
  mediaItemId: number,
): Promise<ReidentifyResult> {
  return unwrap(
    request<ApiEnvelope<ReidentifyResult>>(
      `/libraries/${libraryId}/items/${mediaItemId}/reidentifications`,
      { method: "POST" },
    ),
  );
}

/* ---------------------------------------------------------------------- */
/* 条目转移：分错库的作品换个库（磁盘目录 + 台账一起搬）                      */
/* ---------------------------------------------------------------------- */

/** 转移预览里的一个搬运单元。 */
export interface TransferMove {
  source_path: string;
  target_path: string;
  /** true=整个条目目录搬走；false=只搬这一个文件（目录里混着别的条目时） */
  is_dir: boolean;
  size_bytes: number;
  file_count: number;
}

/** 转移预览：完整的「将要发生什么」，用户确认后才执行。 */
export interface TransferPreview {
  target_library_id: number;
  target_library_name: string;
  /** 目标库主根——条目目录会搬到这里 */
  target_root: string;
  moves: TransferMove[];
  skips: { file_path: string; reason: string }[];
  total_bytes: number;
  /** 缺失文件数（磁盘无实体，只随迁台账归属） */
  missing_count: number;
  /** 目标与源不在同一块盘：退化为完整复制（耗时，且断开与做种目录的硬链接） */
  cross_device: boolean;
  /** 阻断性问题（如目标已有同名目录）；非空则不给执行 */
  blocked: string[];
}

/** 转移进度 / 最近一次结论（弹窗轮询同一个接口从进行中走到结论页）。 */
export interface TransferStatus {
  running: boolean;
  media_item_id: number | null;
  title: string | null;
  target_library_id: number | null;
  processed: number;
  total: number;
  finished_at: string | null;
  target_library_name: string | null;
  moved_paths: string[];
  files_relocated: number;
  bytes_moved: number;
  removed_dirs: number;
  /** 该片的订阅是否一并改挂到目标库（后续剧集直接投新库） */
  subscription_moved: boolean;
  errors: string[];
}

/** 预览把条目转移到目标库会发生什么（只读，不动磁盘）。 */
export function previewItemTransfer(
  libraryId: number,
  mediaItemId: number,
  targetLibraryId: number,
): Promise<TransferPreview> {
  return unwrap(
    request<ApiEnvelope<TransferPreview>>(
      `/libraries/${libraryId}/items/${mediaItemId}/transfer-preview?target_library_id=${targetLibraryId}`,
    ),
  );
}

/**
 * 转移条目到另一个媒体库：整个条目目录搬到目标库主根，台账随迁。
 * 后台执行，进度与结论走 {@link getLibraryItemTransferStatus}。
 */
export function transferLibraryItem(
  libraryId: number,
  mediaItemId: number,
  targetLibraryId: number,
): Promise<PersistentJobStart> {
  return unwrap(
    request<ApiEnvelope<PersistentJobStart>>(
      `/libraries/${libraryId}/items/${mediaItemId}/transfers`,
      { method: "POST", body: JSON.stringify({ target_library_id: targetLibraryId }) },
    ),
  );
}

/** 条目转移的实时进度与最近一次结论（源库/目标库任一侧查到的都是同一份）。 */
export function getLibraryItemTransferStatus(libraryId: number): Promise<TransferStatus> {
  return unwrap(
    request<ApiEnvelope<TransferStatus>>(`/libraries/${libraryId}/item-transfer-status`),
  );
}

/** 重新下载：缺失单元交回订阅管线（无订阅则按缺失季自动创建）。 */
export function redownloadMissing(
  libraryId: number,
  mediaItemId: number,
): Promise<{ subscription_id: number; requeued: number }> {
  return unwrap(
    request<ApiEnvelope<{ subscription_id: number; requeued: number }>>(
      `/libraries/missing-redownloads`,
      {
        method: "POST",
        body: JSON.stringify({ library_id: libraryId, media_item_id: mediaItemId }),
      },
    ),
  );
}
