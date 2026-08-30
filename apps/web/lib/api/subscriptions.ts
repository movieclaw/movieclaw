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

/** 订阅状态（见 movieclaw_db SubscriptionStatus）：completed 是派生值 */
export type SubscriptionStatus = "active" | "paused" | "completed";

/** 工单状态机（见 movieclaw_db WantedStatus） */
export type WantedStatus = "wanted" | "grabbed" | "downloaded" | "imported";

/** 条目摘要（见 schemas.subscription.MediaBrief） */
export interface SubscriptionMedia {
  media_item_id: number;
  kind: MediaType;
  tmdb_id: number;
  douban_id: string | null;
  title: string;
  original_title: string;
  year: number | null;
  poster_url: string | null;
  status: string | null;
}

/** 弹层季选择器的一行 */
export interface SeasonOverview {
  season_number: number;
  name: string;
  air_date: string | null;
  episode_count: number | null;
  /** 已播集数（air_date<=今天） */
  aired_count: number;
  /** 媒体库已有的集数（库存 H） */
  owned_count: number;
}

/** 豆瓣收敛歧义时的确认候选 */
export interface ResolveCandidate {
  tmdb_id: number;
  /** 选定该候选后用于创建订阅的稳定引用 */
  title_ref: string;
  title: string;
  original_title: string;
  year: number | null;
  poster_url: string | null;
}

/** 订阅预检结果：ready 可直接渲染弹层 / ambiguous 先选候选 / not_found 无法订阅 */
export interface PrepareResult {
  status: "ready" | "ambiguous" | "not_found";
  media: SubscriptionMedia | null;
  seasons: SeasonOverview[];
  existing_subscription_id: number | null;
  /** 电影：媒体库里已有本片（弹层提示，不拦订阅） */
  movie_owned: boolean;
  /**
   * 建议预勾选的季号：豆瓣把剧集按季拆条目，点进「中餐厅 第十季」要订的就是那一季。
   * 服务端只在条目确实季专属时给出（且季号是 TMDB 的，与豆瓣季号未必相同）；
   * 为空表示无结论，按「全部已播正季」的原默认规则勾选。
   */
  suggested_seasons: number[];
  candidates: ResolveCandidate[];
}

export interface SubscriptionProgress {
  total: number;
  /** 缺口：还没搞到的单元数 */
  wanted: number;
  grabbed: number;
  downloaded: number;
  /** 已整理入库（终态） */
  imported: number;
  /** 已入库但仍在洗版中的单元数（详情与列表接口同口径计算；海报墙青点据此显示） */
  upgrading: number;
}

/** 单元的洗版派生状态（见 movieclaw_api WantedUpgradeView）；标签由后端生成。 */
export interface WantedUpgrade {
  /** 是否洗版中（可证明低于目标且未熔断） */
  active: boolean;
  /** 当前版本档位标签（如「1080p WEB-DL」） */
  current_label: string;
  /** 洗版目标档位标签（如「1080p Remux」） */
  target_label: string;
  /** 已洗版搜索次数 */
  search_attempts: number;
  /** 无法确认档位（证明不了低于目标也证明不了达标）：不参与自动洗版，可手动选种替换 */
  indeterminate: boolean;
}

export interface Subscription {
  id: number;
  media: SubscriptionMedia;
  status: SubscriptionStatus;
  selected_seasons: number[];
  follow_future: boolean;
  rule_set_id: number;
  /** 入库目标库；null = 该类型的默认库 */
  library_id: number | null;
  progress: SubscriptionProgress;
  /** 剧集按季收录统计；电影为空数组 */
  season_collection: SeasonOverview[];
  created_at: string;
  updated_at: string;
}

/** 订阅首页的一行待入库摘要（今天，或今天没有安排时窗口内最近的一天）。 */
export interface TodaySubscriptionArrival {
  subscription_id: number;
  wanted_id: number;
  media_title: string;
  media_kind: "movie" | "tv";
  season_number: number;
  episode_number: number;
  status: Extract<WantedStatus, "wanted" | "grabbed" | "downloaded">;
  air_date: string | null;
  /** 预计入库/播出的站点日历日（YYYY-MM-DD），用于展示日期。 */
  expected_day: string;
  /** expected_day 距今天几天（0=今天）。站点日历口径由后端算好，前端不复制时区规则。 */
  days_ahead: number;
  release_forecast: ReleaseForecast | null;
  /** 后端按站点游标和礼貌间隔计算的下一次有效预测探测时间。 */
  next_probe_at: string | null;
  info_hash: string | null;
  grabbed_at: string | null;
  downloaded_at: string | null;
  estimated_release_to_import_minutes: number;
  estimated_download_to_import_minutes: number;
}

export interface ReleaseForecastSite {
  site_id: string;
  predicted_at: string;
  window_start: string;
  window_end: string;
  lag_minutes: number;
  coverage_count: number;
  probe_times: string[];
}

/** 单集资源发布时间预测快照；它是可重算的调度结论，不是新的观测事实源。 */
export interface ReleaseForecast {
  version: number;
  generated_at: string;
  target_air_date: string;
  predicted_at: string;
  window_start: string;
  window_end: string;
  confidence: "bootstrap" | "growing" | "stable" | "volatile";
  sample_count: number;
  cadence_days: number;
  basis_units: [number, number][];
  basis_torrent_row_ids: number[];
  sites: ReleaseForecastSite[];
  first: {
    generated_at: string;
    predicted_at: string;
    window_start: string;
    window_end: string;
    confidence: string;
    sample_count: number;
  };
}

/** 一集最近一次成功投递所用资源的发布→索引→提交时间链。 */
export interface ResourceTiming {
  site_id: string;
  torrent_id: string;
  publish_time: string | null;
  first_seen_at: string | null;
  submitted_at: string;
  publish_to_seen_seconds: number | null;
  seen_to_submit_seconds: number | null;
  publish_to_submit_seconds: number | null;
  dry_run: boolean;
}

export interface WantedItem {
  id: number;
  season_number: number;
  episode_number: number;
  status: WantedStatus;
  air_date: string | null;
  priority: number;
  next_search_at: string | null;
  search_attempts: number;
  last_search_at: string | null;
  release_forecast: ReleaseForecast | null;
  resource_timing: ResourceTiming | null;
  grabbed_at: string | null;
  downloaded_at: string | null;
  imported_at: string | null;
  /** 在途工单锚定的种子 hash；据此与 listActiveSubscriptionDownloads 的进度组对上 */
  info_hash: string | null;
  /** 单集履历注解：最近一次候选被拒原因（含站点与种子名）；null=从未被拒 */
  last_reject_reason: string | null;
  /** 单集履历注解：最近一次投递的种子（站点 · 标题）；null=未投递 */
  grab_title: string | null;
  /** 洗版派生状态；规则组未配洗版目标或单元未入库时为 null */
  upgrade: WantedUpgrade | null;
}

export interface SubscriptionDetail extends Subscription {
  wanted: WantedItem[];
}

/** 规则组过滤条件（见 movieclaw_matcher.RuleSetSpec）：全部键可缺省=不限。 */
export interface RuleSetSpec {
  /** 允许的分辨率；列表顺序即偏好顺序（排前面的评分更高） */
  resolutions?: string[];
  /** 允许的片源档（remux/blu-ray/web-dl/rip/tv）；顺序即偏好，与分辨率一样参与选优 */
  media_sources?: string[];
  video_codecs?: string[];
  /** 流媒体平台白名单（规范值，见 lib/platforms.ts）；空=不限。与制作组各自独立生效 */
  platforms?: string[];
  /** 流媒体平台黑名单（规范值）；命中即排除，优先于白名单 */
  platforms_block?: string[];
  release_groups_allow?: string[];
  release_groups_block?: string[];
  /** HDR 白名单兼偏好序（DV/HDR10+/HDR10/HLG/SDR，SDR=未标注）；任一命中即过 */
  hdr_levels?: string[];
  /** HDR 黑名单：资源带其中任一格式即排除 */
  hdr_block?: string[];
  /** [兼容层] 旧的 HDR 三态；hdr_levels/hdr_block 为空时后端才消费它 */
  hdr?: "any" | "require" | "forbid";
  /** [兼容层] 旧的 DV 三态；同上 */
  dv?: "any" | "require" | "forbid";
  free_only?: boolean;
  min_seeders?: number | null;
  /** 体积区间按「每集均摊」评估：整季包用总体积 ÷ 集数比较 */
  size_min_mb?: number | null;
  size_max_mb?: number | null;
  exclude_hr?: boolean;
  hr_unknown_policy?: "lenient" | "strict";
  /** 要求的字幕语言（BCP 47，任一命中即过；"zh" 含简/繁/未标简繁）；空=不限 */
  subtitle_languages_require?: string[];
  /** 要求的音轨语言（cmn=国语、yue=粤语…）；空=不限 */
  audio_languages_require?: string[];
  /** 洗版目标片源档；缺省=不洗版（docs/design/quality-upgrade.md §3.2） */
  upgrade_source?: "web-dl" | "blu-ray" | "remux" | null;
  /** 洗版目标分辨率；缺省=分辨率偏好首选（都缺省则 1080p），须在 resolutions 内 */
  cutoff_resolution?: string | null;
  /** 洗到新版本后保留旧版本（多版本共存，收藏家模式）；缺省=旧版本进回收站 */
  upgrade_keep_old?: boolean;
  /** 参与洗版比较的维度及优先级（顺序即位次）；缺省 ["resolution","source"] */
  upgrade_ladder?: string[];
  /** [预留] 站点白名单；空=全部启用站点 */
  sites?: string[];
}

export interface RuleSet {
  id: number;
  name: string;
  is_default: boolean;
  spec: RuleSetSpec;
  /** 正在引用本组的订阅数；>0 时后端禁删 */
  reference_count: number;
}

export interface SubscriptionTargetPreviewPayload {
  /** Discover 返回的影视条目稳定引用 */
  title_ref: string;
}

export interface CreateSubscriptionPayload {
  /** Discover 返回或歧义候选确认后的稳定引用 */
  title_ref: string;
  /** 从豆瓣候选改选 TMDB 条目时保留原始豆瓣身份 */
  source_title_ref?: string | null;
  selected_seasons?: number[];
  follow_future?: boolean;
  rule_set_id?: number | null;
  /** 入库目标库；缺省用该类型的默认库 */
  library_id?: number | null;
}

export interface CreateSubscriptionResult {
  subscription: SubscriptionDetail;
  /** 管理员可见的下载与入库路由预检；成员调用时为空 */
  download_routing: DispatchPreview | null;
}

/** 投递路由预检（见 schemas.subscription.DispatchPreviewView）。 */
export interface DispatchPreview {
  /** watch=投监听导入目录 / inplace=直接下载进库 / downloader_default=下载器默认目录 */
  mode: "watch" | "inplace" | "downloader_default";
  /** movieclaw 视角的投递基底目录 */
  path: string | null;
  /**
   * 条目目录的完整路径预览（后端按生效的命名模板渲染）。
   * 展示"直接下载到 …"时用它，**不要**自己拼 `${title} (${year})`：
   * 命名模板可全局/按库自定义，自己拼会与真实落点不一致。
   */
  entry_dir: string | null;
  /** 命中自定义目录规则时的整理落点：完成后整理到该目录（不直接入库），外部流转回库根后入账 */
  staging_path: string | null;
  /** 解析出的目标库（未选库时=收藏范围路由的结论，弹窗据此预选） */
  library_id: number | null;
  library_name: string | null;
  downloader_name: string | null;
  /** 收藏范围路由结论：true=命中声明库 / false=默认库兜底；未走路由为 null */
  route_matched: boolean | null;
  /** 路由理由（中文整句，徽标直接展示） */
  route_reason: string | null;
  /** 按当前配置投递能否顺利入库 */
  ok: boolean;
  /** 不 ok 时的中文指引 */
  warning: string | null;
}

/**
 * 投递路由预检：订阅弹窗选库时调用，预演下载会落到哪、能否自动入库。
 * `libraryId` 为 null 且带 `tmdbId` 时由后端按收藏范围路由选库并返回理由。
 */
export function previewSubscriptionDownloadRouting(
  kind: string,
  libraryId: number | null,
  tmdbId?: number | null,
  identity?: { title?: string | null; year?: number | null },
): Promise<DispatchPreview> {
  const params = new URLSearchParams({ kind });
  if (libraryId !== null) params.set("library_id", String(libraryId));
  if (tmdbId != null) params.set("tmdb_id", String(tmdbId));
  // 标题/年份只用于渲染 entry_dir（条目目录预览），不影响路由与投递目录
  if (identity?.title) params.set("title", identity.title);
  if (identity?.year != null) params.set("year", String(identity.year));
  return unwrap(
    request<ApiEnvelope<DispatchPreview>>(`/subscriptions/download-routing-preview?${params}`),
  );
}

/** 订阅链路体检里的一段检查结论。 */
export interface PipelineCheck {
  key: string;
  /** 段落名（「下载器」「路径映射」「硬链接同盘」…） */
  label: string;
  /** ok=正常 / warn=能转但降级 / error=会失败，必须修 */
  status: "ok" | "warn" | "error";
  /** 中文事实陈述，直接展示 */
  detail: string;
  /** 修复去处：设置分区 id（downloaders/import-watch）或 libraries（媒体库页） */
  fix_section: string | null;
}

/** 一个库的完整入库链路结论。 */
export interface LibraryPipeline {
  library_id: number;
  library_name: string;
  kind: "movie" | "tv";
  is_default: boolean;
  /** watch=投监听目录 / inplace=直下库根 / downloader_default=下载器默认目录 */
  mode: "watch" | "inplace" | "downloader_default";
  path: string | null;
  /** 库主根（入库节点的落点展示） */
  library_root: string | null;
  /** 命中自定义目录规则时的整理落点：非空时转移段不直接入库，外部流转后回库根入账 */
  staging_path: string | null;
  status: "ok" | "warn" | "error";
  /** 「订阅命中本库会发生什么」的一句话叙事（全绿时的正向可预期） */
  narrative: string;
  checks: PipelineCheck[];
}

/** 修复卡里的一个可选修法。 */
export interface FixOption {
  title: string;
  /** 为什么这么做 / 适合谁（帮用户在多个选项间取舍；可为空串） */
  why: string;
  /** 具体做什么，含建议值 */
  steps: string;
  /** 修复去处（与 PipelineCheck.fix_section 同词表，前端映射到路由） */
  fix_section: string;
  fix_label: string;
  /** 跳转预填参数：目标设置页读取后自动填表单，免去誊写路径 */
  fix_params: Record<string, string> | null;
}

/** 按根因聚合的问题卡：一个根因 = 一张卡，不随受影响的库数膨胀。 */
export interface PipelineIssue {
  /** 与被聚合检查项的 PipelineCheck.key 同词表（库卡片据此隐藏重复红项） */
  key: string;
  status: "warn" | "error";
  title: string;
  detail: string;
  affected_libraries: string[];
  options: FixOption[];
}

/** 订阅链路体检整体结论（订阅设定页与订阅列表警示横幅共用）。 */
export interface PipelineHealth {
  /** 整体状态：库链路 + 全局段（站点/下载器）的最坏值 */
  status: "ok" | "warn" | "error";
  /** 链路有 error 的库数 */
  error_count: number;
  warn_count: number;
  /** 资源搜索段（全局，链路第一环） */
  site_check: PipelineCheck;
  /** 是否有可用的默认下载器 */
  downloader_ok: boolean;
  /** 是否配置过站点（无论当前可用与否）——开局清单只看它 */
  sites_configured: boolean;
  /** 是否配置过下载器（同上语义） */
  downloaders_configured: boolean;
  /** 按根因聚合的问题卡（error 在前）——置顶展示为「需要处理 N 件事」 */
  issues: PipelineIssue[];
  libraries: LibraryPipeline[];
}

/** 订阅链路体检：逐库预演「投递 → 转移 → 入库」，与真实投递同源判定。 */
export function checkSubscriptionAutomationReadiness(
  init?: RequestInit,
): Promise<PipelineHealth> {
  return unwrap(request<ApiEnvelope<PipelineHealth>>("/subscriptions/automation-readiness", init));
}

/** Web 表单内部预览：按 Discover title_ref 返回季集、库存和现有订阅状态。 */
export function previewSubscriptionTitle(
  payload: SubscriptionTargetPreviewPayload,
): Promise<PrepareResult> {
  return unwrap(
    request<ApiEnvelope<PrepareResult>>("/subscriptions/title-preview", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 创建订阅（同条目重复订阅幂等返回已有）。 */
export function createSubscription(
  payload: CreateSubscriptionPayload,
): Promise<SubscriptionDetail> {
  return unwrap(
    request<ApiEnvelope<CreateSubscriptionResult>>("/subscriptions", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  ).then((result) => result.subscription);
}

/** 订阅列表（含工单进度）。kind 缺省返回全部。 */
export function listSubscriptions(
  kind?: MediaType,
  init?: RequestInit,
): Promise<Subscription[]> {
  const query = kind ? `?kind=${kind}` : "";
  return unwrap(request<ApiEnvelope<Subscription[]>>(`/subscriptions${query}`, init));
}

/**
 * 最近一次可能入库的订阅内容；预计耗时已按各订阅历史校准。
 * 今天没有安排时后端自动回退到一周内最近的那一天（``days_ahead`` > 0）。
 */
export function listTodaySubscriptionArrivals(
  init?: RequestInit,
): Promise<TodaySubscriptionArrival[]> {
  return unwrap(
    request<ApiEnvelope<TodaySubscriptionArrival[]>>(
      "/subscriptions/today-arrivals",
      init,
    ),
  );
}

/** 订阅详情（含工单明细）。 */
export function getSubscription(id: number): Promise<SubscriptionDetail> {
  return unwrap(request<ApiEnvelope<SubscriptionDetail>>(`/subscriptions/${id}`));
}

/** 修改订阅（季选择/自动续订/规则组，后端 diff 重算工单）。 */
export function updateSubscription(
  id: number,
  payload: {
    selected_seasons?: number[];
    follow_future?: boolean;
    rule_set_id?: number;
    /** 换入库目标库；显式传 null=清除指定、改回按默认库路由；缺省不变 */
    library_id?: number | null;
  },
): Promise<SubscriptionDetail> {
  return unwrap(
    request<ApiEnvelope<SubscriptionDetail>>(`/subscriptions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  );
}

/** 立即搜索：缺口工单跳过冷却重新排队（暂停中/无可搜缺口时后端报可读错误）。 */
export function searchMissingSubscriptionResources(
  id: number,
): Promise<{ reset_count: number }> {
  return unwrap(
    request<ApiEnvelope<{ reset_count: number }>>(
      `/subscriptions/${id}/missing-resource-searches`,
      { method: "POST" },
    ),
  );
}

/** 一轮洗版体检里单个单元的结论（见 schemas.subscription.UpgradeRunUnitView）。 */
export interface UpgradeRunUnit {
  season_number: number;
  episode_number: number;
  /** upgradable=已排入立即搜索 / at_cutoff=已达目标 / in_flight=已在洗 /
   *  not_comparable=无法识别当前版本 / missing=缺失（照常走缺口下载） */
  state: "upgradable" | "at_cutoff" | "in_flight" | "not_comparable" | "missing";
  current_label: string | null;
  target_label: string;
}

/** 一轮洗版的体检报告（见 schemas.subscription.UpgradeRunView）。 */
export interface UpgradeRunReport {
  target_label: string;
  rule_set_id: number;
  /** 中文整句摘要，直接展示 */
  summary: string;
  counts: Record<UpgradeRunUnit["state"], number>;
  units: UpgradeRunUnit[];
}

/**
 * 触发一轮洗版：可选先换规则组（组必须配置洗版目标），随后物化存量工单、
 * 逐集体检并把可洗单元排入立即搜索。暂停中的订阅后端报可读错误。
 */
export function runSubscriptionUpgradeRound(
  id: number,
  ruleSetId?: number | null,
): Promise<UpgradeRunReport> {
  return unwrap(
    request<ApiEnvelope<UpgradeRunReport>>(`/subscriptions/${id}/upgrade-runs`, {
      method: "POST",
      body: JSON.stringify(ruleSetId != null ? { rule_set_id: ruleSetId } : {}),
    }),
  );
}

/** 手动选种的种子字段（搜索结果行原样回传，attrs 即搜索链路的服务端解析）。 */
export interface GrabPayload {
  site_id: string;
  torrent_id: string;
  title: string;
  subtitle?: string;
  category?: string | null;
  attrs?: Record<string, unknown> | null;
  download_url?: string | null;
  size_bytes?: number | null;
  seeders?: number | null;
  is_free?: boolean | null;
  hit_and_run?: boolean | null;
  publish_time?: string | null;
}

/** 手动选种：把一条搜索结果直接投给订阅（跳过规则组过滤；身份匹配照常）。 */
export function downloadSelectedTorrentForSubscription(
  id: number,
  payload: GrabPayload,
): Promise<{ units: { season_number: number; episode_number: number }[] }> {
  return unwrap(
    request<ApiEnvelope<{ units: { season_number: number; episode_number: number }[] }>>(
      `/subscriptions/${id}/selected-torrent-downloads`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  );
}

/** 暂停 / 恢复订阅。 */
export function setSubscriptionTrackingState(
  id: number,
  state: "active" | "paused",
): Promise<SubscriptionDetail> {
  return unwrap(
    request<ApiEnvelope<SubscriptionDetail>>(`/subscriptions/${id}/tracking-state`, {
      method: "PATCH",
      body: JSON.stringify({ state }),
    }),
  );
}

/** 独立开启 / 关闭剧集自动续订。 */
export function setSubscriptionFollowFuture(
  id: number,
  enabled: boolean,
): Promise<SubscriptionDetail> {
  return unwrap(
    request<ApiEnvelope<SubscriptionDetail>>(`/subscriptions/${id}/follow-future`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  );
}

/** 成员停止关注；服务端保证不影响仍在追踪的其他成员。 */
export function unsubscribeFromSubscription(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/subscriptions/${id}/following`, {
      method: "DELETE",
    }),
  );
}

/** 管理员永久删除订阅与追踪工单（不影响已下载内容）。 */
export function deleteSubscriptionPermanently(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/subscriptions/${id}`, {
      method: "DELETE",
    }),
  );
}

/** 订阅在途种子的实时下载快照（详情页轮询展示进度/速度/ETA）。 */
export interface SubscriptionDownload {
  info_hash: string;
  /** 下载器中的任务名；missing 时为空 */
  name: string | null;
  /** 0.0 ~ 1.0；missing 时为空 */
  progress: number | null;
  size_bytes: number | null;
  dlspeed_bytes: number | null;
  eta_seconds: number | null;
  /** missing = 种子已不在任何可用下载器中（救援巡检稍后会退回工单重找） */
  state: "downloading" | "stalled" | "paused" | "completed" | "error" | "missing" | "unknown";
  downloader_name: string | null;
  units: { season_number: number; episode_number: number }[];
}

/** 订阅在途种子的实时下载进度（纯读快照，逐个查询下载器）。 */
export function listActiveSubscriptionDownloads(
  id: number,
  init?: RequestInit,
): Promise<SubscriptionDownload[]> {
  return unwrap(
    request<ApiEnvelope<SubscriptionDownload[]>>(
      `/subscriptions/${id}/active-downloads`,
      init,
    ),
  );
}

/** 订阅活动记录：message 是完整中文句子，时间线直接展示。 */
export interface SubscriptionActivity {
  id: number;
  type:
    | "created"
    | "adjusted"
    | "paused"
    | "resumed"
    | "completed"
    | "reopened"
    | "searched"
    | "match_accepted"
    | "match_rejected"
    | "grabbed"
    | "dispatch_failed"
    | "wanted_added"
    | "downloaded"
    | "imported"
    | "import_failed"
    | "download_stalled"
    | "replacement_searched"
    | "replacement_trial"
    | "replacement_promoted"
    | "replacement_cleanup"
    | "upgrade_grabbed"
    | "upgraded"
    | "upgrade_verify_failed"
    | "spec_mismatch";
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
  /** 管线类活动所属工单；生命周期变更、搜索轮次等订阅级活动为 null */
  wanted_item_id: number | null;
}

/** 订阅活动时间线（系统对该订阅做过的每个动作，时间倒序）。 */
export function listSubscriptionActivities(
  id: number,
  limit = 100,
): Promise<SubscriptionActivity[]> {
  return unwrap(
    request<ApiEnvelope<SubscriptionActivity[]>>(
      `/subscriptions/${id}/activities?limit=${limit}`,
    ),
  );
}

/** 规则组列表（首次访问后端自动创建默认组）。 */
export function listRuleSets(init?: RequestInit): Promise<RuleSet[]> {
  return unwrap(request<ApiEnvelope<RuleSet[]>>("/rule-sets", init));
}

export function createRuleSet(name: string, spec: RuleSetSpec): Promise<RuleSet> {
  return unwrap(
    request<ApiEnvelope<RuleSet>>("/rule-sets", {
      method: "POST",
      body: JSON.stringify({ name, spec }),
    }),
  );
}

/** 更新规则组（只影响之后的匹配评估，不追溯已投递的工单）。 */
export function updateRuleSet(id: number, name: string, spec: RuleSetSpec): Promise<RuleSet> {
  return unwrap(
    request<ApiEnvelope<RuleSet>>(`/rule-sets/${id}`, {
      method: "PUT",
      body: JSON.stringify({ name, spec }),
    }),
  );
}

/** 设为默认规则组（新订阅未指定规则组时使用；不改已有订阅的挂靠）。 */
export function setDefaultRuleSet(id: number): Promise<RuleSet> {
  return unwrap(
    request<ApiEnvelope<RuleSet>>(`/rule-sets/${id}/default`, { method: "POST" }),
  );
}

/** 删除规则组（默认组与被订阅引用的组后端会拒绝，错误信息可直接展示）。 */
export function deleteRuleSet(id: number): Promise<void> {
  return unwrap(request<ApiEnvelope<void>>(`/rule-sets/${id}`, { method: "DELETE" }));
}
