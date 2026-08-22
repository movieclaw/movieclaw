import { HttpError, redirectToLoginOn401, request, resolveRequestUrl } from "@/lib/http";
import {
  toDiscoveredSearchItem,
  toSearchItem,
  type MediaSearchItem,
  type MediaSearchItemDto,
  type TitleSearchData,
  type TitleSearchDto,
} from "@/lib/api/discover";
import type { LibrarySearchGroup } from "@/lib/api/libraries";
import type { SearchScope, SearchTab, TorrentCategory } from "@/lib/categories";
import type { MediaSource } from "@/lib/media-types";

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
 * 数据扩充层从标题/副标题推导的结构化属性（见 movieclaw_enrich.TorrentAttrs）。
 *
 * 全部字段"提取不到即空值"：标量为 null、列表为空数组，展示时按需隐藏。
 * complete 三态：true=明确标注全集，null=没有标注（不等于"不是全集"）。
 */
export interface TorrentAttrs {
  /** 影视类型；null=无法确定（后端由站点分类+季集观测联合推断，不猜） */
  media_type: "movie" | "tv" | null;
  /** 题材轴（与 media_type 正交）：anime / documentary / variety / music；null=普通真人影视 */
  content_type: string | null;
  /** 中文片名及别名（首个为主名）；提取不到为空数组 */
  titles_zh: string[];
  /** 外文片名及别名（首个为主名） */
  titles_en: string[];
  year: number | null;
  seasons: number[];
  episodes: number[];
  /** 总集数（"全12集" 的 12） */
  episodes_total: number | null;
  complete: boolean | null;
  /** 归一化分辨率：2160p / 1080p / 720p ... */
  resolution: string | null;
  /** 归一化编码：x265 / H.264 / HEVC / AV1 ... */
  video_codec: string | null;
  /** HDR 格式（可叠加）：DV / HDR10 / HDR10+ / HLG */
  hdr: string[];
  /** 片源：UHD Blu-ray / Blu-ray / WEB-DL / HDTV ... */
  media_source: string | null;
  remux: boolean;
  /** 音频编码：TrueHD / Atmos / DTS-HD MA / DDP ... */
  audio: string[];
  /** 标题/副标题明确声明的字幕语言（BCP 47；zh=泛称"中字"不猜简繁） */
  subtitle_languages: string[];
  /** 字幕载体：embedded（内封/软）/ hardcoded（内嵌/硬）/ external（外挂） */
  subtitle_carriers?: string[];
  /** 音轨语言（BCP 47：cmn=国语、yue=粤语…），含配音与原声声明 */
  audio_languages?: string[];
  /** 流媒体来源平台（规范值，展示名见 lib/platforms.ts）；与 release_group 正交 */
  platforms?: string[];
  release_group: string | null;
}

/**
 * 搜索命中的单条种子（见 movieclaw_api.schemas.search.TorrentHit）。
 *
 * 是 tracker TorrentListItem 的字段 + 来源站点标识 + 扩充属性。字段大多可选：
 * 解析不到即为空/0/默认值，前端展示时需容忍缺省。
 */
export interface TorrentHit {
  site_id: string;
  site_name: string;
  torrent_id: string;
  title: string;
  subtitle: string;
  category: TorrentCategory | null;
  site_category_name: string | null;
  size: string | null;
  size_bytes: number;
  seeders: number;
  leechers: number;
  snatched: number;
  upload_time: string | null;
  free: boolean;
  download_volume_factor: number;
  upload_volume_factor: number;
  /** H&R 考核三态：true=有考核，false=明确无考核，null=站点不提供该信息 */
  hit_and_run: boolean | null;
  detail_url: string | null;
  download_url: string | null;
  /** 海报图地址；多数站点不提供（null），图览模式只对非空项生效 */
  poster_url: string | null;
  /** 全部图片（海报 + 截图等），poster_url 是其中第一张；站点不提供时为空数组 */
  image_urls: string[];
  attrs: TorrentAttrs | null;
}

/** 单个站点在本次搜索里的执行情况（见 schemas.search.SiteSearchStatus）。 */
export interface SiteSearchStatus {
  site_id: string;
  site_name: string;
  count: number;
  /** 失败原因（可读中文）；成功为 null */
  error: string | null;
  /** 该站从发起到返回/失败的耗时（毫秒）；老快照数据可能为 null */
  elapsed_ms: number | null;
}

/** 跨站聚合搜索结果（见 schemas.search.SearchResponse）。 */
export interface SearchResponse {
  keyword: string;
  /** 请求方传入的展示名回显（分类中文名/预设名）；null=全部 */
  label: string | null;
  /** 分类组合回显；空=不限分类 */
  categories: TorrentCategory[];
  total: number;
  items: TorrentHit[];
  sites: SiteSearchStatus[];
}

/** 搜索历史的单条记录（见 schemas.search.SearchHistoryItem）。 */
export interface SearchHistoryItem {
  id: number;
  keyword: string;
  /** 搜索垂直；公开契约使用与 CLI 一致的领域名。 */
  vertical: "titles" | "torrents";
  /** 展示名快照（分类中文名/预设名）；null=全部 */
  label: string | null;
  /** 分类组合快照（已排序去重）；空=不限分类 */
  categories: TorrentCategory[];
  /** 站点组合快照；空=全部站点 */
  site_ids: string[];
  /** 该关键词+组合的累计搜索次数 */
  search_count: number;
  /** 最近一次搜索时间（带时区的 ISO 串），展示用 lib/time.ts 转换 */
  last_searched_at: string;
  /** 是否已有结果快照：有则点击历史进快照预览，无则直接发起实时搜索 */
  has_snapshot: boolean;
  /** 图览模式偏好（发起搜索时）：点历史重搜/看快照据此还原结果页展示模式 */
  poster_mode: boolean;
}

/** 获取最近的统一搜索历史（按最近搜索时间倒序，同关键词+范围已合并）。 */
export function listSearchHistory(limit = 10, init?: RequestInit): Promise<SearchHistoryItem[]> {
  return unwrap(
    request<ApiEnvelope<SearchHistoryItem[]>>(`/search/history?limit=${limit}`, init),
  );
}

/** 一次历史 PT 资源搜索保存的完整结果。 */
export interface TorrentSearchHistoryResults {
  vertical: "torrents";
  history_id: number;
  keyword: string;
  label: string | null;
  categories: TorrentCategory[];
  site_ids: string[];
  /** 快照生成时间（带时区的 ISO 串），提示条用 lib/time.ts 换算相对时间 */
  snapshot_at: string;
  total: number;
  /** 当次搜索的整体耗时（毫秒）；老快照/阻塞版搜索没有该数据时为 null */
  elapsed_ms: number | null;
  items: TorrentHit[];
  sites: SiteSearchStatus[];
}

/** 读取某条搜索历史的结果快照；历史不存在或尚无快照报 404（HttpError.status）。 */
export async function getTorrentSearchHistoryResults(
  historyId: number,
  init?: RequestInit,
): Promise<TorrentSearchHistoryResults> {
  const result = await unwrap(
    request<ApiEnvelope<SearchHistoryResults>>(`/search/history/${historyId}/results`, init),
  );
  if (result.vertical !== "torrents") throw new Error("该历史记录不是 PT 种子搜索");
  return result;
}

/** 一次历史影视条目搜索保存的完整结果。 */
interface TitleSearchHistoryResultsDto {
  vertical: "titles";
  history_id: number;
  keyword: string;
  /** 快照生成时间（带时区的 ISO 串），提示条用 lib/time.ts 换算相对时间 */
  snapshot_at: string;
  total: number;
  items: MediaSearchItemDto[];
}

export interface TitleSearchHistoryResults {
  vertical: "titles";
  history_id: number;
  keyword: string;
  snapshot_at: string;
  total: number;
  items: MediaSearchItem[];
}

type SearchHistoryResults = TorrentSearchHistoryResults | TitleSearchHistoryResultsDto;

/** 读取影视条目历史结果；路由与 PT 结果统一，由 vertical 做类型守卫。 */
export async function getTitleSearchHistoryResults(
  historyId: number,
  init?: RequestInit,
): Promise<TitleSearchHistoryResults> {
  const result = await unwrap(
    request<ApiEnvelope<SearchHistoryResults>>(`/search/history/${historyId}/results`, init),
  );
  if (result.vertical !== "titles") throw new Error("该历史记录不是影视条目搜索");
  return { ...result, items: result.items.map(toSearchItem) };
}

/** 删除单条搜索历史。 */
export function deleteSearchHistoryEntry(id: number): Promise<null> {
  return unwrap(request<ApiEnvelope<null>>(`/search/history/${id}`, { method: "DELETE" }));
}

/** 清空全部搜索历史。 */
export function clearSearchHistory(): Promise<null> {
  return unwrap(request<ApiEnvelope<null>>(`/search/history`, { method: "DELETE" }));
}

/** 资源搜索预设视图：内置分类与自定义站点组合的完整有序列表。 */
interface SearchPresetListView {
  presets: SearchTab[];
}

/** 列出资源搜索预设；含隐藏的内置分类。 */
export async function listSearchPresets(init?: RequestInit): Promise<SearchTab[]> {
  const view = await unwrap(
    request<ApiEnvelope<SearchPresetListView>>("/search/presets", init),
  );
  return view.presets;
}

/** 整体覆盖式保存资源搜索预设，返回后端规范化后的完整列表。 */
export async function updateSearchPresets(presets: SearchTab[]): Promise<SearchTab[]> {
  const view = await unwrap(
    request<ApiEnvelope<SearchPresetListView>>("/search/presets", {
      method: "PUT",
      body: JSON.stringify({ presets }),
    }),
  );
  return view.presets;
}

/** 搜索一个或全部影视来源；每个来源的成功/失败状态独立返回。 */
export async function searchTitles(
  query: string,
  options?: { provider?: MediaSource | "all"; saveHistory?: boolean },
  init?: RequestInit,
): Promise<TitleSearchData> {
  const dto = await unwrap(
    request<ApiEnvelope<TitleSearchDto>>("/search/titles", {
      ...init,
      method: "POST",
      headers: { "Content-Type": "application/json", ...init?.headers },
      body: JSON.stringify({
        query,
        provider: options?.provider ?? "all",
        save_history: options?.saveHistory ?? false,
      }),
    }),
  );
  return {
    items: dto.titles.map(toDiscoveredSearchItem),
    providers: dto.providers.map((status) => ({
      provider: status.provider,
      success: status.success,
      resultCount: status.result_count,
      message: status.message ?? undefined,
    })),
    historyId: dto.history_id ?? undefined,
  };
}

/** 搜索本地全部可见媒体库中的已入库条目。 */
export function searchLibraryItems(keyword: string): Promise<LibrarySearchGroup[]> {
  const query = new URLSearchParams({ keyword });
  return unwrap(request<ApiEnvelope<LibrarySearchGroup[]>>(`/search/library-items?${query}`));
}

export interface SearchParams {
  keyword: string;
  /** 搜索范围（标签换算而来）；不传等同「全部」 */
  scope?: SearchScope;
  page?: number;
}

/**
 * 跨「已启用且验证通过」的站点（可由 scope.siteIds 圈定子集）并发搜索种子资源。
 * 单站失败不影响整体，其原因见 `sites[].error`。
 */
export function searchTorrents(
  { keyword, scope, page }: SearchParams,
  init?: RequestInit,
): Promise<SearchResponse> {
  return unwrap(
    request<ApiEnvelope<SearchResponse>>(
      `/search/torrents?${searchParamsOf({ keyword, scope, page })}`,
      init,
    ),
  );
}

function searchParamsOf({ keyword, scope, page }: SearchParams): URLSearchParams {
  const params = new URLSearchParams({ keyword });
  for (const c of scope?.categories ?? []) params.append("categories", c);
  for (const s of scope?.siteIds ?? []) params.append("sites", s);
  if (scope?.label) params.set("label", scope.label);
  // 无痕搜索（自定义分类的隐私开关）：后端据此跳过搜索历史落库
  if (scope?.skipHistory) params.set("no_history", "true");
  // 图览模式偏好随历史留存：点历史重搜/看快照时后端回传，前端据此还原展示模式
  if (scope?.posterMode) params.set("poster_mode", "true");
  if (page && page > 1) params.set("page", String(page));
  return params;
}

/* —— SSE 流式搜索（GET /search/torrents/stream） —— */

/** `start` 事件的站点清单项 / `site_start` 事件的载荷（见 schemas.search.SearchStreamSite）。 */
export interface SearchStreamSite {
  site_id: string;
  site_name: string;
}

/** `start` 事件：宣告本次搜索的范围与参与站点。 */
export interface SearchStreamStart {
  keyword: string;
  label: string | null;
  categories: TorrentCategory[];
  page: number;
  sites: SearchStreamSite[];
}

/** `site_result` 事件：单站搜索成功，携带该站全部命中。 */
export interface SiteStreamResult {
  site_id: string;
  site_name: string;
  count: number;
  /** 该站从发起到返回的耗时（毫秒） */
  elapsed_ms: number;
  items: TorrentHit[];
}

/** `site_error` 事件：单站搜索失败（可读中文原因），不影响其它站点。 */
export interface SiteStreamError {
  site_id: string;
  site_name: string;
  error: string;
  elapsed_ms: number;
}

/** `done` 事件：所有站点均已返回的整体汇总。 */
export interface SearchStreamDone {
  total: number;
  elapsed_ms: number;
  sites: SiteSearchStatus[];
}

/** 流式搜索事件的可辨识联合，事件序列：start → site_start×N → (site_result|site_error)×N → done。 */
export type SearchStreamEvent =
  | { type: "start"; data: SearchStreamStart }
  | { type: "site_start"; data: SearchStreamSite }
  | { type: "site_result"; data: SiteStreamResult }
  | { type: "site_error"; data: SiteStreamError }
  | { type: "done"; data: SearchStreamDone };

/**
 * 流式跨站搜索：快的站点先出结果，逐事件回调 `onEvent`，全部结束后 resolve。
 *
 * 用 fetch + ReadableStream 手动解析 SSE 而非 EventSource：后者断线会自动重连、
 * 把同一次搜索重放一遍；搜索是一次性动作，失败应该由用户显式重试。
 * 取消上一次搜索走 `init.signal`（AbortController），中止会以 AbortError reject。
 */
export async function streamSearchTorrents(
  params: SearchParams,
  onEvent: (event: SearchStreamEvent) => void,
  init?: RequestInit,
): Promise<void> {
  const response = await fetch(resolveRequestUrl(`/search/torrents/stream?${searchParamsOf(params)}`), {
    ...init,
    headers: { Accept: "text/event-stream" },
  });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    let details: unknown = null;
    try {
      details = await response.json();
      if (details && typeof details === "object" && "message" in details) {
        message = String((details as { message: unknown }).message);
      }
    } catch {
      // 非 JSON 错误体（如网关页），保留默认 message
    }
    redirectToLoginOn401(response.status);
    throw new HttpError(message, response.status, details);
  }
  if (!response.body) {
    throw new HttpError("当前环境不支持流式响应", response.status, null);
  }

  // SSE 帧以空行分隔；块内 `event:` 行给事件名、`data:` 行给 JSON 载荷
  const dispatch = (block: string) => {
    let event = "";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (!event || !data) return; // 注释行 / 心跳等非事件块，忽略
    onEvent({ type: event, data: JSON.parse(data) } as SearchStreamEvent);
  };

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      dispatch(buffer.slice(0, sep));
      buffer = buffer.slice(sep + 2);
    }
  }
}
