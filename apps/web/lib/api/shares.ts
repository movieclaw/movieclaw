import { request } from "@/lib/http";
import type { LibraryKind } from "@/lib/media-types";
import type {
  AudioStream,
  LibraryChapter,
  LocalMeta,
  SeasonEpisodes,
  SubtitleStream,
} from "@/lib/api/libraries";

/**
 * 影片分享（docs/design/media-share.md）的接口客户端。
 *
 * 两组：管理侧（超管，条目的分享创建 / 查询 / 取消与全部分享列表）与
 * 访客侧（`/share/{slug}/…` 公开通道，靠 slug 与解锁 Cookie 而不是账号）。
 */

interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

// ---------------------------------------------------------------------------
// 管理侧
// ---------------------------------------------------------------------------

export interface ShareView {
  id: number;
  slug: string;
  /** 分享链接；未配置外部访问地址时为相对路径 /s/{slug}，前端用当前 origin 补全 */
  url: string;
  /** 范围二选一：条目分享给 media_item_id，合集分享给 collection_id */
  media_item_id: number | null;
  collection_id: number | null;
  library_id: number | null;
  title: string;
  kind: LibraryKind | null;
  year: number | null;
  poster_url: string | null;
  /** 合集分享此刻有几部；条目分享为 null */
  item_count: number | null;
  /** 访问密码原文；无密码为 null */
  password: string | null;
  expires_at: string;
  created_at: string;
  view_count: number;
  last_accessed_at: string | null;
}

export interface ShareCreateResult {
  share: ShareView;
  /** 条目已有有效分享：原样返回它，没有新建 */
  existed: boolean;
}

export async function getItemShare(
  libraryId: number,
  mediaItemId: number,
): Promise<ShareView | null> {
  const response = await request<ApiEnvelope<ShareView | null>>(
    `/libraries/${libraryId}/items/${mediaItemId}/share`,
  );
  return response.data;
}

export async function createItemShare(
  libraryId: number,
  mediaItemId: number,
  body: { expires_in_days: number; password: string | null },
): Promise<ShareCreateResult> {
  const response = await request<ApiEnvelope<ShareView>>(
    `/libraries/${libraryId}/items/${mediaItemId}/share`,
    { method: "POST", body: JSON.stringify(body) },
  );
  return { share: response.data, existed: response.code === "SHARE_EXISTS" };
}

export async function revokeItemShare(libraryId: number, mediaItemId: number): Promise<void> {
  await request<ApiEnvelope<unknown>>(`/libraries/${libraryId}/items/${mediaItemId}/share`, {
    method: "DELETE",
  });
}

export async function listShares(): Promise<ShareView[]> {
  const response = await request<ApiEnvelope<ShareView[]>>("/shares");
  return response.data;
}

export async function revokeShare(shareId: number): Promise<void> {
  await request<ApiEnvelope<unknown>>(`/shares/${shareId}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// 访客侧
// ---------------------------------------------------------------------------

export interface SharePublic {
  requires_password: boolean;
  /** 无密码恒为 true；有密码时表示本浏览器已解锁 */
  unlocked: boolean;
  expires_at: string;
  /** 被分享的条目 id；解锁之前、或分享的是合集时为 null（播放页据此起播） */
  media_item_id: number | null;
  /** 被分享的合集 id；解锁之前、或分享的是条目时为 null */
  collection_id: number | null;
}

/** 合集分享页上的一格。 */
export interface SharedCollectionItem {
  media_item_id: number;
  title: string;
  year: number | null;
  kind: LibraryKind;
  poster_url: string | null;
}

/** 合集分享页：名字 + 此刻的成员（规则驱动的合集会自己长，所以是现算的）。 */
export interface SharedCollection {
  name: string;
  item_count: number;
  items: SharedCollectionItem[];
}

/** 访客能看到的一个文件：只有规格与章节，没有路径与文件名。 */
export interface SharedFile {
  id: number;
  size_bytes: number;
  container: string | null;
  resolution: string | null;
  video_codec: string | null;
  hdr: string | null;
  bit_depth: number | null;
  duration_seconds: number | null;
  media_source: string | null;
  season_number: number;
  episode_number: number;
  missing: boolean;
  state: "in_place" | "missing" | "trashed";
  audio_streams: AudioStream[] | null;
  subtitle_streams: SubtitleStream[];
  chapters: LibraryChapter[] | null;
}

export interface SharedItem {
  media_item_id: number;
  kind: LibraryKind;
  /** 外部词条锚点：分享页底部「相关链接」；本地条目为 null */
  tmdb_id: number | null;
  imdb_id: string | null;
  douban_id: string | null;
  title: string;
  original_title: string;
  year: number | null;
  poster_url: string | null;
  backdrop_url: string | null;
  primary_aspect: number;
  local_meta: LocalMeta | null;
  files: SharedFile[];
  seasons: number[];
  expires_at: string;
}

function base(slug: string): string {
  return `/share/${encodeURIComponent(slug)}`;
}

export async function probeShare(slug: string): Promise<SharePublic> {
  const response = await request<ApiEnvelope<SharePublic>>(base(slug));
  return response.data;
}

export async function unlockShare(slug: string, password: string): Promise<SharePublic> {
  const response = await request<ApiEnvelope<SharePublic>>(`${base(slug)}/unlock`, {
    method: "POST",
    body: JSON.stringify({ password }),
  });
  return response.data;
}

/** 合集分享的成员名单（每次访问现算）。 */
export async function getSharedCollection(slug: string): Promise<SharedCollection> {
  const response = await request<ApiEnvelope<SharedCollection>>(`${base(slug)}/collection`);
  return response.data;
}

/** ``item`` 只有合集分享才要给：条目分享的范围就那一个，服务端忽略它。 */
export async function getSharedItem(slug: string, item?: number): Promise<SharedItem> {
  const suffix = item === undefined ? "" : `?item=${item}`;
  const response = await request<ApiEnvelope<SharedItem>>(`${base(slug)}/item${suffix}`);
  return response.data;
}

export async function getSharedEpisodes(
  slug: string,
  seasonNumber: number,
  item?: number,
): Promise<SeasonEpisodes> {
  const suffix = item === undefined ? "" : `&item=${item}`;
  const response = await request<ApiEnvelope<SeasonEpisodes>>(
    `${base(slug)}/episodes?season_number=${seasonNumber}${suffix}`,
  );
  return response.data;
}

// ---------------------------------------------------------------------------
// 合集分享的管理侧
// ---------------------------------------------------------------------------

export async function getCollectionShare(collectionId: number): Promise<ShareView | null> {
  const response = await request<ApiEnvelope<ShareView | null>>(
    `/collections/${collectionId}/share`,
  );
  return response.data;
}

export async function createCollectionShare(
  collectionId: number,
  body: { expires_in_days: number; password: string | null },
): Promise<ShareCreateResult> {
  const response = await request<ApiEnvelope<ShareView>>(`/collections/${collectionId}/share`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  return { share: response.data, existed: response.code === "SHARE_EXISTS" };
}

export async function revokeCollectionShare(collectionId: number): Promise<void> {
  await request<ApiEnvelope<unknown>>(`/collections/${collectionId}/share`, { method: "DELETE" });
}
