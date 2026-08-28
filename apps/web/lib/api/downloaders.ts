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

/** 已适配的下载器类型（与后端 movieclaw_db ClientType 对应）。 */
export type DownloaderClientType = "qbittorrent" | "transmission";

/** 连接验证状态（与站点配置共用同一状态机语义）。 */
export type DownloaderStatus = "pending" | "verifying" | "active" | "failed";

/**
 * 一条路径映射：movieclaw 视角的目录前缀 → 下载器视角的对应前缀。
 * 跨容器/跨主机部署同一块盘两边路径不同时配置；视角一致留空。
 */
export interface PathMapping {
  /** movieclaw 上的路径前缀（目录弹窗选择） */
  local: string;
  /** 下载器上的对应路径前缀（手动填写） */
  remote: string;
}

/** 已配置下载器的对外视图（见 schemas.downloader.DownloaderView，脱敏无密码）。 */
export interface ConfiguredDownloader {
  id: number;
  name: string;
  client_type: DownloaderClientType;
  url: string;
  username: string | null;
  save_path: string | null;
  /** 路径映射（movieclaw 路径 → 下载器路径）；null = 两边视角一致 */
  path_mappings: PathMapping[] | null;
  enabled: boolean;
  /** 是否为默认下载器（一键下载不选目标时投给它） */
  is_default: boolean;
  status: DownloaderStatus;
  /** 是否可用 = 已启用且连接测试通过 */
  usable: boolean;
  /** 最近一次连接成功获取的版本号，如 "v5.0.2" */
  version: string | null;
  last_error: string | null;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

export type DownloadTaskState =
  | "downloading"
  | "stalled"
  | "queued"
  | "paused"
  | "checking"
  | "completed"
  | "error"
  | "missing"
  | "unknown";

/** 追踪单元的工单状态；分批入库时同一个种子里各集会停在不同状态。 */
export type DownloadTaskUnitStatus = "wanted" | "grabbed" | "downloaded" | "imported";

export interface DownloadTaskSubscription {
  id: number;
  media_item_id: number;
  media_title: string;
  media_kind: string;
  poster_url: string | null;
  /**
   * 投递目的。洗版（upgrade）任务要洗的集在投递前就已入库，"已入库"是前提
   * 而不是成果——进度必须改用 `units[].replaced` 的替换口径讲，否则会出现
   * 「有 16 集已入库」配 0% 进度的自相矛盾。
   */
  purpose: "download" | "upgrade";
  /**
   * 洗版成功后是否保留旧版本共存（规则组的收藏家模式）。
   * false=旧版本移入回收站、保留期满自动清理——替换发生前就要说清楚。
   */
  upgrade_keep_old: boolean;
  /** 种子声明覆盖的**全集**（含已入库的集），不是"还欠哪些集"。 */
  units: {
    season_number: number;
    episode_number: number;
    status: DownloadTaskUnitStatus;
    /** 洗版任务专用：该集是否已被本种子替换完成；补缺下载恒为 false。 */
    replaced: boolean;
    /**
     * 内容核验证明种子里没有这一集（声明的覆盖范围与实际文件不符）。这一集
     * 已退回重新寻找资源，等这个种子永远等不到——卡片要显示为需关注。
     */
    content_missing: boolean;
  }[];
}

/**
 * 下载完成后**还没发生**的那段路，由后端按当前媒体库与监听导入配置推导。
 * 任务中心据此把「等待入库 · 下一步」换成真实步骤链；推不出时整体为 null。
 */
export interface DownloadTaskPlan {
  /** watch=监听导入搬运；inplace=已在库内目录，扫描即入账；downloader_default=不会自动入库 */
  mode: "watch" | "inplace" | "downloader_default";
  /** 搬运策略；mode=watch 才有 */
  strategy: "hardlink" | "copy" | null;
  /** 目标库名；库未定或不进库时为 null */
  library_name: string | null;
  /** 落点目录 */
  dest_path: string | null;
  /** 整理后是否进入媒体库（自定义目录规则为 false） */
  enters_library: boolean;
}

/** 下载器实时任务；订阅/手动来源由后端按 infohash 关联，不复制下载状态。 */
export interface DownloadTask {
  id: string;
  info_hash: string;
  name: string | null;
  downloader_id: number | null;
  downloader_name: string | null;
  downloader_type: DownloaderClientType | null;
  progress: number | null;
  size_bytes: number | null;
  dlspeed_bytes: number | null;
  /** 当前上传速度（字节/秒）；未知为 null */
  upspeed_bytes: number | null;
  /** 累计上传量（字节）；刷流分组汇总用，未知为 null */
  uploaded_bytes: number | null;
  /** 已完成字节；刷流分组汇总用，未知为 null */
  completed_bytes: number | null;
  eta_seconds: number | null;
  state: DownloadTaskState;
  /** boost = 自动刷分享率抢下的种子：无媒体身份与入库流转，折叠为独立分组 */
  source: "subscription" | "manual" | "boost" | "external";
  /** 来源站点 ID / 显示名；movieclaw 投递的任务才有，外部任务为 null */
  site_id: string | null;
  site_name: string | null;
  /** 站点种子详情页 URL；能定位到站内种子时提供，供浏览器新窗口打开 */
  page_url: string | null;
  /** 投递时快照的画面规格与片源（如 2160p / WEB-DL）；未知为 null */
  resolution: string | null;
  media_source: string | null;
  /** 是否原盘 Remux；与 media_source 正交（Remux 的片源仍是 UHD Blu-ray） */
  remux: boolean;
  media_item_id: number | null;
  media_title: string | null;
  media_kind: string | null;
  poster_url: string | null;
  plan: DownloadTaskPlan | null;
  subscriptions: DownloadTaskSubscription[];
  rescue_state:
    | "active"
    | "replacement_pending"
    | "trial"
    | "cleanup_pending"
    | "retained"
    | "completed"
    | null;
  no_progress_seconds: number | null;
  can_replace: boolean;
  replacement_due_at: string | null;
  rescue_message: string | null;
}

export interface DownloadTaskSource {
  id: number;
  name: string;
  client_type: DownloaderClientType;
  status: "active" | "disabled" | "unavailable" | "error";
  message: string | null;
  task_count: number;
}

export interface DownloadTaskSnapshot {
  items: DownloadTask[];
  sources: DownloadTaskSource[];
}

/** 新增/更新下载器的请求体（见 schemas.downloader.DownloaderPayload）。 */
export interface DownloaderPayload {
  name: string;
  client_type: DownloaderClientType;
  url: string;
  username?: string | null;
  password?: string | null;
  save_path?: string | null;
  path_mappings?: PathMapping[] | null;
  enabled?: boolean;
}

/** 列出所有已配置的下载器及连接状态。 */
export function listDownloaders(init?: RequestInit): Promise<ConfiguredDownloader[]> {
  return unwrap(request<ApiEnvelope<ConfiguredDownloader[]>>("/downloaders", init));
}

/** 任务中心快照：所有下载器的活跃任务、仍待入库任务与来源健康状态。 */
export function listDownloadTasks(init?: RequestInit): Promise<DownloadTaskSnapshot> {
  return unwrap(request<ApiEnvelope<DownloadTaskSnapshot>>("/downloaders/tasks", init));
}

/**
 * 从指定下载器移除一个种子任务。默认保留数据文件；只有确认弹窗中显式
 * 选择后才传 deleteFiles=true，避免误删正在下载或做种的数据。
 */
export function deleteDownloadTask(
  downloaderId: number,
  infoHash: string,
  deleteFiles = false,
): Promise<{ downloader_id: number; info_hash: string; delete_files: boolean }> {
  const query = deleteFiles ? "?delete_files=true" : "";
  return unwrap(
    request<
      ApiEnvelope<{ downloader_id: number; info_hash: string; delete_files: boolean }>
    >(
      `/downloaders/${downloaderId}/torrents/${encodeURIComponent(infoHash)}${query}`,
      { method: "DELETE" },
    ),
  );
}

/** 立即为 15 分钟无进度的订阅任务执行真实跨站换源搜索。 */
export function replaceDownloadTask(
  downloaderId: number,
  infoHash: string,
): Promise<{ downloader_id: number; info_hash: string; attempt_id: number }> {
  return unwrap(
    request<
      ApiEnvelope<{ downloader_id: number; info_hash: string; attempt_id: number }>
    >(`/downloaders/${downloaderId}/torrents/${encodeURIComponent(infoHash)}/replace`, {
      method: "POST",
    }),
  );
}

/** 获取单个下载器详情（用于轮询连接测试进度）。 */
export function getDownloader(id: number, init?: RequestInit): Promise<ConfiguredDownloader> {
  return unwrap(request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}`, init));
}

/** 添加一个下载器（保存后后端异步测试连接）。 */
export function createDownloader(payload: DownloaderPayload): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>("/downloaders", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 更新下载器配置（更新后后端重新测试连接）。 */
export function updateDownloader(
  id: number,
  payload: DownloaderPayload,
): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 下载器全局限制（见 schemas.downloader.DownloaderLimitsView）。
 *  限速单位字节/秒，null=不限；max_active_torrents 为 qBittorrent 独有。 */
export interface DownloaderLimits {
  download_limit_bytes: number | null;
  upload_limit_bytes: number | null;
  alt_speed_enabled: boolean | null;
  queue_enabled: boolean | null;
  max_active_downloads: number | null;
  max_active_uploads: number | null;
  max_active_torrents: number | null;
}

/** 实时读取下载器的全局限速与任务队列上限。 */
export function getDownloaderLimits(id: number): Promise<DownloaderLimits> {
  return unwrap(request<ApiEnvelope<DownloaderLimits>>(`/downloaders/${id}/limits`));
}

/** 写入下载器全局限制，返回回读的生效值。限速 null=取消；其余 null=不改。 */
export function setDownloaderLimits(
  id: number,
  limits: DownloaderLimits,
): Promise<DownloaderLimits> {
  return unwrap(
    request<ApiEnvelope<DownloaderLimits>>(`/downloaders/${id}/limits`, {
      method: "PUT",
      body: JSON.stringify(limits),
    }),
  );
}

/** 启用 / 停用下载器。 */
export function setDownloaderEnabled(
  id: number,
  enabled: boolean,
): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  );
}

/** 设为默认下载器。注意：其他条目的 is_default 会随之变化，调用后应整体刷新列表。 */
export function setDefaultDownloader(id: number): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}/default`, { method: "POST" }),
  );
}

/** 手动重新测试一次连接。 */
export function reverifyDownloader(id: number): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}/verify`, { method: "POST" }),
  );
}

/** 删除下载器配置。 */
export function deleteDownloader(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/downloaders/${id}`, { method: "DELETE" }),
  );
}

/** 手动提交下载的请求体（见 schemas.downloader.DownloadSubmitPayload）。 */
export interface DownloadSubmitPayload {
  /** 种子所属站点 ID（TorrentHit.site_id） */
  site_id: string;
  /** 种子下载入口（TorrentHit.download_url） */
  download_url: string;
  /** 站点内种子 ID（TorrentHit.torrent_id）：任务中心据此提供「打开种子页」 */
  torrent_id?: string | null;
  /** 入库目标库（可选）：带上后保存目录由库推导（主根/标题 (年份)） */
  library_id?: number | null;
  /** 条目标题（推导条目子目录用；身份未确认时不要带） */
  title?: string | null;
  year?: number | null;
  /** 种子副标题（识别线索：中文片名/「全N集」帮扫描器收敛拼音命名种子） */
  subtitle?: string | null;
  /** 下载弹窗手选的保存目录（movieclaw 视角，优先于库推导） */
  save_path?: string | null;
  /** 指定投递到哪台下载器（配了多台按需分流）；缺省用默认下载器 */
  downloader_id?: number | null;
  /** 服务端按已确认的 TMDB 身份重新匹配媒体库并选择监听导入目录 */
  auto_route?: boolean;
  /** 智能入库的媒体类型，与 tmdb_id 一起构成后端路由输入 */
  media_kind?: "movie" | "tv";
  /** 智能入库已确认的 TMDB 条目 ID */
  tmdb_id?: number;
}

/** 手动下载识别未收敛时返回的 TMDB 候选（供界面解释为何不自动投递）。 */
export interface ManualDownloadTargetCandidate {
  tmdb_id: number;
  title: string;
  year: number | null;
  episode_count: number | null;
}

/** 手动搜索种子的「识别 → 库路由 → 监听投递目录」预检结果。 */
export interface ManualDownloadTarget {
  status: "ready" | "ambiguous" | "not_found";
  tmdb_id: number | null;
  candidates: ManualDownloadTargetCandidate[];
  library_id: number | null;
  library_name: string | null;
  mode: "watch" | "inplace" | "downloader_default" | null;
  path: string | null;
  /** 条目目录的完整路径预览（后端按生效的命名模板渲染），展示落点时用它 */
  entry_dir: string | null;
  staging_path: string | null;
  route_matched: boolean | null;
  route_reason: string | null;
  ok: boolean;
  warning: string | null;
}

/** 预演一条搜索结果能否被可靠识别并投递到匹配库的监听目录。 */
export function resolveManualDownloadTarget(payload: {
  kind: "movie" | "tv";
  title: string;
  year: number;
  subtitle?: string | null;
  /** 用这台下载器的路径映射预检；缺省沿用后端默认下载器语义 */
  downloader_id?: number | null;
  /** 歧义时由用户确认的、且必须属于本次候选的 TMDB ID */
  selected_tmdb_id?: number | null;
}): Promise<ManualDownloadTarget> {
  return unwrap(
    request<ApiEnvelope<ManualDownloadTarget>>("/downloaders/resolve-target", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 手动提交下载的结果（见 schemas.downloader.DownloadSubmitView）。 */
export interface DownloadSubmitResult {
  info_hash: string | null;
  name: string;
  /** 种子提交前已存在于下载器（幂等，未重复添加） */
  already_exists: boolean;
  downloader_id: number;
  downloader_name: string;
  /** 实际使用的保存目录（null = 下载器自身默认目录） */
  save_path: string | null;
}

/**
 * 把一条搜索结果种子提交到下载器：后端带站点登录态取回 .torrent 再递交。
 * 保存目标按 auto_route / save_path / library_id / 下载器默认目录的互斥选择决定；
 * Web 的智能选项必须先经 resolveManualDownloadTarget 收敛身份并通过预检。
 * 失败抛 HttpError，message 为可读中文。
 */
export function submitTorrentDownload(
  payload: DownloadSubmitPayload,
): Promise<DownloadSubmitResult> {
  return unwrap(
    request<ApiEnvelope<DownloadSubmitResult>>("/downloaders/submit", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}
