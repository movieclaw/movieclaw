import { publicEnv } from "@/lib/env";
import type { TrickplayIndex } from "@/lib/player/trickplay";
import { HttpError, request, resolveRequestUrl } from "@/lib/http";
import type { LibraryEpisode } from "@/lib/api/libraries";
import type { MediaType } from "@/lib/media-types";

/**
 * 取流/字幕地址的最终解析。
 *
 * 后端给的是**服务器根相对路径**（`/api/v1/playback/...`，token 已在里面），
 * 与 `request()` 吃的「API 相对路径」不是一回事，不能过 `resolveRequestUrl`
 * ——那会把前缀拼两遍。默认部署（同源 + Next 反代）直接可用；只有把
 * `NEXT_PUBLIC_API_BASE_URL` 指到别的源时才需要补上源。
 */
export function resolveStreamUrl(path: string): string {
  if (/^https?:\/\//.test(path)) return path;
  const base = publicEnv.apiBaseUrl;
  if (!/^https?:\/\//.test(base)) return path;
  return new URL(path, base).toString();
}

interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

export interface RecentWatchItem {
  media_item_id: number;
  library_id: number;
  kind: MediaType;
  title: string;
  year: number | null;
  poster_url: string | null;
  /** 电影横向背景剧照；缺失时前端用竖版海报生成模糊铺底。 */
  backdrop_url: string | null;
  /** 剧集最近播放那一集的 16:9 剧照；电影恒为 null。 */
  episode_still_url: string | null;
  season_number: number;
  episode_number: number;
  episode_title: string | null;
  /** 同一媒体库中排在最近播放那一集之后、仍在位且从未看过的分集数；电影恒为 0。 */
  unwatched_ahead_count: number;
  position_ms: number;
  duration_ms: number | null;
  progress_percent: number | null;
  played: boolean;
  play_count: number;
  last_played_at: string;
}

/** 当前账号在可见媒体库中的最近观看作品。 */
export async function listRecentWatch(limit = 20): Promise<RecentWatchItem[]> {
  const response = await request<ApiEnvelope<{ items: RecentWatchItem[] }>>(
    `/playback/recent?limit=${limit}`,
  );
  return response.data.items;
}

// ---------------------------------------------------------------------------
// 任务中心「媒体库」分类（管理员运维视角）
// ---------------------------------------------------------------------------

export interface MediaActivityTarget {
  media_item_id: number;
  /** 详情页落点；作品没有在位文件时为 null（此时不渲染跳转）。 */
  library_id: number | null;
  kind: MediaType;
  title: string;
  year: number | null;
  poster_url: string | null;
  season_number: number;
  episode_number: number;
  episode_title: string | null;
}

export interface PlaybackFileSpec {
  resolution: string | null;
  video_codec: string | null;
  hdr: string | null;
  container: string | null;
  bit_rate: number | null;
  size_bytes: number | null;
}

export interface ActivePlaybackSession {
  device_id: string;
  member_name: string;
  client: string;
  device_name: string;
  client_version: string;
  media: MediaActivityTarget;
  position_ms: number | null;
  duration_ms: number | null;
  progress_percent: number | null;
  paused: boolean;
  /** local = 本地文件直连（速率可测）；remote = 网盘直链等不经过服务器的播放。 */
  play_method: "local" | "remote";
  rate_bytes_per_second: number | null;
  bytes_sent: number | null;
  connections: number;
  file: PlaybackFileSpec | null;
  started_at: string;
  last_report_at: string;
}

export interface ActiveFileDownload {
  device_id: string;
  member_name: string;
  client: string;
  device_name: string;
  media: MediaActivityTarget | null;
  file_name: string;
  size_bytes: number;
  bytes_sent: number;
  rate_bytes_per_second: number;
  /** 同一设备同一文件的多条 Range 连接（断点续传）聚合后的连接数。 */
  connections: number;
  /** 已下载到文件的哪个字节位置（Range 起点 + 本次已传）。 */
  position_bytes: number;
  /** 由位置换算的完成百分比；文件大小未知时为 null。 */
  progress_percent: number | null;
  started_at: string;
}

export interface PlaybackDevice {
  device_id: string;
  device_name: string;
  client: string;
  client_version: string;
  member_name: string;
  last_seen_at: string | null;
  online: boolean;
}

export interface MediaRecentPlay {
  member_name: string;
  media: MediaActivityTarget;
  position_ms: number;
  duration_ms: number | null;
  progress_percent: number | null;
  played: boolean;
  play_count: number;
  last_played_at: string;
}

export interface MediaActivitySnapshot {
  sessions: ActivePlaybackSession[];
  downloads: ActiveFileDownload[];
  devices: PlaybackDevice[];
  recent: MediaRecentPlay[];
}

/** 任务中心媒体库分类的完整快照：正在播放/下载、设备清单与全成员最近观看。 */
export async function fetchMediaActivity(): Promise<MediaActivitySnapshot> {
  const response = await request<ApiEnvelope<MediaActivitySnapshot>>("/playback/activity");
  return response.data;
}

/** 注销一台播放器设备：凭据即刻失效，其正在进行的播放与取流一并停止。 */
export async function revokePlaybackDevice(deviceId: string): Promise<string> {
  const response = await request<ApiEnvelope<null>>(
    `/playback/devices/${encodeURIComponent(deviceId)}`,
    { method: "DELETE" },
  );
  return response.message;
}

// ---------------------------------------------------------------------------
// 网页播放器（docs/design/web-player.md）
// ---------------------------------------------------------------------------

/**
 * 客户端解码能力快照。**字段名与后端 ClientCapabilityIn 严格对应**，
 * codec 用的是归一化的编码家族名（h264 / hevc / aac …），不是浏览器探测时
 * 用的 RFC 6381 串（avc1.640028）——服务端要拿它和 ffprobe 落库的 codec_name
 * 直接比对。翻译在 lib/player/capability.ts 里做。
 */
export type MseKind = "full" | "managed" | "none";

export interface ClientCapability {
  video: { codec: string; max_height: number; smooth: boolean; power_efficient: boolean }[];
  audio: { codec: string; max_channels: number }[];
  /** "mp4" = 能 <video src> 直出；"hls-fmp4" = 有 MSE 或原生 HLS */
  containers: string[];
  hdr_passthrough: boolean;
  /** "full" | "managed"（iOS 的 ManagedMediaSource）| "none" */
  mse: MseKind;
  is_mobile: boolean;
  /** 能原生播 HLS（Safari/AVPlayer）。服务端不消费，前端选引擎用 */
  native_hls: boolean;
}

/** 播放单元：电影用 (0, 0) 哨兵，与台账、playback_state 的约定一致。 */
export interface PlaybackUnit {
  media_item_id: number;
  season_number?: number;
  episode_number?: number;
}

export interface VideoPlan {
  /** "copy" | "transcode" */
  action: string;
  codec: string | null;
  height: number | null;
  tone_map: boolean;
  /** 非空 = 该字幕轨被烧录进画面（用户显式选中 PGS 触发，Emby 语义的
   * 「字幕压制」）。前端据此：不再旁挂渲染这条轨、菜单选中态指向它、
   * 画中画补丁轨跳过、诊断面板显示「字幕压制」。 */
  burn_subtitle: string | null;
}

export interface AudioPlan {
  action: string;
  track_ref: string | null;
  codec: string | null;
  channels: number | null;
  downmix: boolean;
}

/** 文件里的一条可选音轨。`AudioPlan` 说的是「这次放哪条」，这个是候选列表。 */
export interface AudioTrack {
  /** 中性轨引用：embedded:<下标> */
  ref: string;
  codec: string | null;
  channels: number | null;
  language: string | null;
  is_default: boolean;
}

export interface SubtitlePlan {
  /** 中性轨引用：external:<文件名> / embedded:<下标> */
  track_ref: string;
  /** "vtt"（服务端转好的文本轨）| "ass"（原样下发交 JASSUB）| "pgs"（P0 不渲染） */
  kind: string;
  language: string | null;
  is_default: boolean;
  /** 本机 AI 生成的字幕（翻译 / 双语），字幕菜单据此打标 */
  is_ai?: boolean;
}

/**
 * 决策结果的三态并集。`outcome` 决定其余字段哪些有值——分支处理别漏，
 * consent 与 rejected 都带面向用户的中文 `reason`。
 */
export interface PlaybackDecision {
  outcome: "plan" | "consent" | "rejected";
  tier: number | null;
  file_id: number | null;
  container: string | null;
  video: VideoPlan | null;
  audio: AudioPlan | null;
  /** 这个文件里全部可选音轨（含当前这条），前端据此渲染音轨菜单 */
  audio_tracks: AudioTrack[];
  subtitles: SubtitlePlan[];
  /** 本次是降档重来的结果，记录原本失败的档位（§6.3） */
  degraded_from: number | null;
  cost_hint: string | null;
  /** 当前账号能不能自己去开软件转码开关（只有超管可以） */
  can_self_enable: boolean | null;
  setting_namespace: string | null;
  setting_key: string | null;
  reason: string;
  suggestion: string | null;
}

/** 源文件的客观规格（台账真值），诊断面板「源 → 处理」层次的左半边 */
export interface PlaybackSource {
  container: string | null;
  resolution: string | null;
  video_codec: string | null;
  hdr: string | null;
  /** 总码率（bps） */
  bit_rate: number | null;
  frame_rate: number | null;
  size_bytes: number | null;
}

export interface PlaybackArtifactUpload {
  name: string;
  status: number;
  received_bytes: number;
  content_length: number | null;
  transfer_encoding: string | null;
  occurred_at_ms: number;
}

/** 播放诊断接口的脱敏快照；不会包含源地址、签名 URL 或 Worker token。 */
export interface PlaybackDiagnostics {
  session_state: string;
  session_error: string | null;
  processing_mode: string;
  execution_location: string;
  backend: string | null;
  encoder: string | null;
  worker_id: string | null;
  worker_version: string | null;
  worker_platform: string | null;
  worker_arch: string | null;
  ffmpeg_version: string | null;
  worker_online: boolean | null;
  worker_last_seen_seconds: number | null;
  job_id: string | null;
  attempt_id: string | null;
  job_state: string | null;
  job_out_time_ms: number | null;
  job_speed: string | null;
  job_phase: string | null;
  job_exit_code: number | null;
  job_error: string | null;
  job_stderr_tail: string | null;
  head_segment: number | null;
  highest_produced_segment: number | null;
  requested_segment: number | null;
  served_segment: number | null;
  segment_wait_ms: number | null;
  segment_status: number | null;
  pending_segments: number[];
  failed_segments: number[];
  historical_failed_segments: number[];
  recent_uploads: PlaybackArtifactUpload[];
  cache_bytes: number;
  total_segments: number | null;
}

export interface PlaybackSession {
  decision: PlaybackDecision;
  /** 档 0 没有会话（原文件直出），此处为 null */
  session_id: string | null;
  /** 可直接喂给 <video src> 或 hls.js 的地址，已带签名 token */
  stream_url: string | null;
  /** 会话时间轴的零点在文件里的位置。**文件时间 = start_ms + currentTime**。
   * timeline="file"（VOD 预生成列表）时它只是建议起播位置：分片时间戳
   * 本身是文件绝对时间，currentTime 即文件时间，换算参照点为 0 */
  start_ms: number;
  /** 时间轴语义：session = 会话相对（旧）；file = 文件绝对（VOD，§12） */
  timeline?: "session" | "file";
  /** master 播放列表（带 WEBVTT 字幕组），仅 VOD 会话有——iOS 原生 HLS 用 */
  master_url?: string | null;
  /** 旁挂字幕地址（已带 token），与 decision.subtitles 一一对应 */
  subtitle_urls: string[];
  /** 实际使用的硬件加速后端；null = 纯软件（直通档不经编码器，同样为 null） */
  hw_backend: string | null;
  /** 本单元的观看状态快照（§6.10）。续播点已由服务端并入 start_ms，这里
   * 整份带回给前端预填时间轴、恢复字幕记忆——起播不再单独问 /resume */
  watch: PlaybackWatchState | null;
  /** 选中文件的源规格（台账真值），诊断面板「源 → 处理」层次用（§6.5） */
  source: PlaybackSource | null;
}

interface DecideBody extends PlaybackUnit {
  file_id?: number;
  capability: ClientCapability;
  /** 运行期降档回路：带上已失败的档位，服务端跳过它们（§6.3） */
  failed_tiers?: number[];
  start_ms?: number;
  /**
   * 用户点选的音轨。给了服务端就认它、不再自动换轨。
   *
   * **换轨要换会话**：音轨在开会话时就被 ffmpeg `-map` 进命令里了，改不了；
   * 而且选了非默认轨会把档 0 顶成档 1（直出时浏览器只放默认轨）。所以前端
   * 换轨走的是和 seek 出界一样的「带着当前位置重开会话」那条路。
   */
  audio_track?: string;
  /** 用户选中的字幕轨。只在指向内封 PGS 轨时改变决策——视频转码并把字幕
   * 烧录进画面（Emby 语义）。缺省 = 服务端用观看状态里记住的轨；
   * "off" 显式不烧。文本轨传了也无妨（服务端 no-op），顺带刷新轨记忆。 */
  subtitle_track?: string;
  /** 画质上限（如 720）。上限而非目标：源不超就照常直通。省略 = 自动 */
  max_height?: number;
}

/** 只问「该怎么放」，不起会话。用于播放前的档位预览与诊断。 */
export async function decidePlayback(body: DecideBody): Promise<PlaybackDecision> {
  const response = await request<ApiEnvelope<PlaybackDecision>>("/playback/decide", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return response.data;
}

/** 判定档位并（需要时）起转码会话，返回可直接播放的地址。 */
export async function startPlaybackSession(body: DecideBody): Promise<PlaybackSession> {
  const response = await request<ApiEnvelope<PlaybackSession>>("/playback/sessions", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return response.data;
}

/** 读取当前会话诊断；从播放 URL 复用已有取流 token，不另发放凭据。 */
export async function fetchPlaybackDiagnostics(
  sessionId: string,
  streamUrl: string,
): Promise<PlaybackDiagnostics> {
  const resolvedUrl = new URL(
    resolveStreamUrl(streamUrl),
    typeof window === "undefined" ? "http://localhost" : window.location.origin,
  );
  const token = resolvedUrl.searchParams.get("token");
  if (!token) {
    throw new Error("播放地址缺少诊断凭据");
  }
  const response = await request<ApiEnvelope<PlaybackDiagnostics>>(
    `/playback/sessions/${encodeURIComponent(sessionId)}/diagnostics?token=${encodeURIComponent(token)}`,
  );
  return response.data;
}

/**
 * 会话续命，兼探活。用户关页面不会发任何信号，服务端的超时回收是唯一可靠
 * 兜底，所以播放中必须定时喂这个。
 *
 * 返回值分三态，**调用方必须区分**：
 *
 * - `true`  —— 会话还在；
 * - `false` —— 服务端明确说它没了（404，多半是页面被浏览器冻在后台、心跳
 *   被节流到喂不上，会话让巡检回收了）。此时继续放下去只会卡在「正在缓冲」，
 *   要原地重开一个会话；
 * - `null`  —— 这次请求本身没成功（断网、超时、服务端 5xx）。**不能当成
 *   会话没了**：网络抖一下就把流掐掉重开，代价比多等一轮心跳大得多。
 */
export async function pingPlaybackSession(sessionId: string): Promise<boolean | null> {
  try {
    await request<ApiEnvelope<unknown>>(
      `/playback/sessions/${encodeURIComponent(sessionId)}/ping`,
      { method: "POST" },
    );
    return true;
  } catch (error) {
    return error instanceof HttpError && error.status === 404 ? false : null;
  }
}

/**
 * 播放器客户端事件上报：把浏览器侧现场（MediaError 详情、播放器状态）落
 * 服务端日志。iPhone 上没有可看的控制台，这是拿到客户端真相的唯一通道。
 * fire-and-forget：上报失败绝不能影响播放本身。
 */
export function reportPlaybackClientLog(event: string, detail: Record<string, unknown>): void {
  void request<ApiEnvelope<unknown>>("/playback/client-log", {
    method: "POST",
    body: JSON.stringify({ event, detail }),
  }).catch(() => undefined);
}

/** 结束会话，掐断 ffmpeg 并清掉临时分片。 */
export async function stopPlaybackSession(sessionId: string): Promise<void> {
  await request<ApiEnvelope<unknown>>(
    `/playback/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}

/**
 * 页面卸载时结束会话。
 *
 * 普通 fetch 在 pagehide 之后会被浏览器直接取消，请求根本发不出去；
 * `keepalive` 让它脱离文档生命周期继续发完。sendBeacon 在这里不可用——
 * 它只能发 POST，而结束会话是 DELETE。
 *
 * 这条链路失败也不是灾难：服务端有心跳超时回收兜底（§4.1），这里只是让
 * 转码进程早几十秒结束、少占一次并发额度。因此异常一律吞掉。
 */
export function stopPlaybackSessionOnUnload(sessionId: string): void {
  try {
    void fetch(
      resolveRequestUrl(`/playback/sessions/${encodeURIComponent(sessionId)}`),
      { method: "DELETE", keepalive: true },
    );
  } catch {
    // 卸载路径上无处呈现错误，静默即可
  }
}

export interface PlaybackWatchState {
  position_ms: number;
  played: boolean;
  play_count: number;
  /** 服务端算出的片长；已看判定的分母始终以它为准 */
  duration_ms: number | null;
  audio_track: string | null;
  subtitle_track: string | null;
}

/** 播放页要的条目信息（§6.10）：路由只带 media_item_id，库归属服务端解析。 */
export interface PlaybackItemInfo {
  media_item_id: number;
  /** 该条目对当前成员可见的库 id；退出播放跳回条目页用 */
  library_id: number;
  kind: "movie" | "tv";
  title: string;
  year: number | null;
  poster_url: string | null;
}

export async function getPlaybackItem(mediaItemId: number): Promise<PlaybackItemInfo> {
  const response = await request<ApiEnvelope<PlaybackItemInfo>>(
    `/playback/items/${mediaItemId}`,
  );
  return response.data;
}

/** 播放页一季的分集清单（切集/上一集下一集数据源），形状与库详情页同构。 */
export async function getPlaybackItemEpisodes(
  mediaItemId: number,
  seasonNumber: number,
): Promise<{ season_number: number; episodes: LibraryEpisode[] }> {
  const response = await request<
    ApiEnvelope<{ season_number: number; episodes: LibraryEpisode[] }>
  >(`/playback/items/${mediaItemId}/episodes?season_number=${seasonNumber}`);
  return response.data;
}

/** 起播前问「上次看到哪、用的哪条轨」。从未播过返回全零，不是错误。 */
export async function fetchResumeState(unit: PlaybackUnit): Promise<PlaybackWatchState> {
  const params = new URLSearchParams({
    media_item_id: String(unit.media_item_id),
    season_number: String(unit.season_number ?? 0),
    episode_number: String(unit.episode_number ?? 0),
  });
  const response = await request<ApiEnvelope<PlaybackWatchState>>(
    `/playback/resume?${params}`,
  );
  return response.data;
}

export interface PlaybackProgressBody extends PlaybackUnit {
  event: "start" | "progress" | "stop";
  /** 播到文件的哪个位置。**不报（undefined）视同播到结尾**，与报 0 不同。 */
  position_ms?: number;
  audio_track?: string;
  subtitle_track?: string;
}

/** 上报观看进度（开始 / 心跳 / 停止同一入口）。 */
export async function reportPlaybackProgress(
  body: PlaybackProgressBody,
): Promise<PlaybackWatchState> {
  const response = await request<ApiEnvelope<PlaybackWatchState>>("/playback/progress", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return response.data;
}

/**
 * 页面卸载时上报最后位置。
 *
 * 与结束会话不同，这一条**不能丢**——丢了用户下次续播就会回到十几秒前，
 * 是最容易被察觉的体验缺陷。`sendBeacon` 是唯一能在卸载路径上活下来的
 * 通道（普通 fetch 会被直接取消），它发的是 POST，正合进度接口。
 * Blob 必须带 `application/json` 类型，否则 FastAPI 会因 Content-Type
 * 不对而拒收。
 */
export function reportPlaybackProgressOnUnload(body: PlaybackProgressBody): void {
  if (typeof navigator === "undefined" || !navigator.sendBeacon) return;
  const blob = new Blob([JSON.stringify(body)], { type: "application/json" });
  navigator.sendBeacon(resolveRequestUrl("/playback/progress"), blob);
}

/** 播放策略。数字上限（并发/高度/缓存配额）由服务端按机器规格自动推导，
    不再是配置项；这里只剩软件转码开关（由播放页同意弹窗翻开）。 */
export interface PlaybackPolicy {
  software_transcode_enabled: boolean;
  /** 实测结果而非配置项——用户改不了自己有没有显卡 */
  hardware_available: boolean;
  hw_backends: string[];
}

/** 按字段增量保存：没给的项保持原值，不会覆盖别处刚改的设置。 */
export async function savePlaybackPolicy(
  changes: Partial<Omit<PlaybackPolicy, "hardware_available" | "hw_backends">>,
): Promise<PlaybackPolicy> {
  const response = await request<ApiEnvelope<PlaybackPolicy>>("/playback/policy", {
    method: "PUT",
    body: JSON.stringify(changes),
  });
  return response.data;
}


/**
 * ASS 字幕依赖的内嵌字体地址。
 *
 * 番剧的 ASS 把字体作为附件放在 MKV 里，不喂给 JASSUB 就会回退成默认字体，
 * 排版、字号、描边全走样——这是「ASS 能播」和「ASS 播得对」的差距。
 *
 * 懒加载：抽取要通读整个容器，只在真的要渲染 ASS 轨时才调。失败不是灾难，
 * JASSUB 还有兜底字体，所以调用方一律吞掉异常。
 */
export async function fetchEmbeddedFonts(subtitleUrl: string): Promise<string[]> {
  // 复用字幕地址上的签名 token：两者是同一个文件的同一份授权
  const url = new URL(subtitleUrl, "http://placeholder");
  const token = url.searchParams.get("token");
  const fileId = url.pathname.match(/\/files\/(\d+)\//)?.[1];
  if (!token || !fileId) return [];
  const response = await request<ApiEnvelope<{ fonts: string[] }>>(
    `/playback/files/${fileId}/fonts?token=${encodeURIComponent(token)}`,
  );
  return response.data.fonts;
}


/**
 * 进度条缩略图索引。
 *
 * 生成要通读整个容器，是开会话时在服务端后台起的——没就绪就是 `ready:false`，
 * 表现为没有预览，不影响播放。所以调用方轮询几次即可，失败一律吞掉。
 */
export async function fetchTrickplay(subtitleOrStreamUrl: string): Promise<TrickplayIndex | null> {
  const url = new URL(subtitleOrStreamUrl, "http://placeholder");
  const token = url.searchParams.get("token");
  const fileId = url.pathname.match(/\/files\/(\d+)\//)?.[1];
  if (!token || !fileId) return null;
  const response = await request<ApiEnvelope<TrickplayIndex>>(
    `/playback/files/${fileId}/trickplay?token=${encodeURIComponent(token)}`,
  );
  return response.data;
}


export interface PlaybackMetricPayload {
  library_file_id: number | null;
  tier: number;
  degraded_from: number | null;
  engine: string;
  hw_backend: string;
  ttff_ms: number | null;
  rebuffer_ms: number;
  rebuffer_count: number;
  seek_count: number;
  dropped_frames: number | null;
  total_frames: number | null;
  watched_ms: number;
}

/**
 * 上报一次播放的质量快照。
 *
 * 只落本地——写进自建实例自己的数据库，绝不外发。失败无所谓（指标是趋势
 * 数据），所以调用方一律吞掉；页面卸载路径用 sendBeacon 保证发得出去。
 */
export async function reportPlaybackMetric(payload: PlaybackMetricPayload): Promise<void> {
  await request<ApiEnvelope<unknown>>("/playback/metrics", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** 卸载路径上的上报：普通 fetch 会被浏览器直接取消。 */
export function reportPlaybackMetricOnUnload(payload: PlaybackMetricPayload): void {
  try {
    navigator.sendBeacon?.(
      resolveRequestUrl("/playback/metrics"),
      new Blob([JSON.stringify(payload)], { type: "application/json" }),
    );
  } catch {
    // 卸载路径上无处呈现错误
  }
}
