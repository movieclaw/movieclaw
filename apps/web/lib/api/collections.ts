import { request } from "@/lib/http";
import type { LibraryItem } from "@/lib/api/libraries";
import type { FilterRule } from "@/lib/library-filter";

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
 * 一个合集（见 schemas.library.CollectionView）。
 *
 * 合集是**存好的筛选**：``rules`` 与 ``library.match_rules`` 同构，与海报墙的
 * 筛选条件也是同一套字段。形态（会不会自己长、能不能改）是后端**推导**出来的，
 * 前端不要再按 name/builtin 自己猜一遍。
 */
export interface Collection {
  id: number;
  name: string;
  /** 所属库；null=跨库合集 */
  library_id: number | null;
  rules: FilterRule[];
  /** 合集内默认排序 */
  sort: string;
  /** household=全家可见 / private=只有我 */
  visibility: "household" | "private";
  /** 内置合集标识（如 favorites:12）；null=用户创建 */
  builtin: string | null;
  /** 合集从哪来：user=用户自建 / builtin=内置 / series=按作品系列自动生成。
   *  后端推导好给我们，前端不要去解 builtin 那个字符串——那等于把规则抄第二遍 */
  kind: "user" | "builtin" | "series";
  /** 已隐藏。自动生成的合集删不掉（下次 ensure 又长回来），那颗按钮落成墓碑 */
  hidden: boolean;
  /** 能不能改规则 */
  editable: boolean;
  /** 规则驱动（会自己长）还是名单驱动（固定的一份名单） */
  rule_driven: boolean;
  /** 当前观看者能看到的成员数 */
  item_count: number;
  /** 封面取哪部作品；null=取首个成员 */
  cover_item_id: number | null;
  /** 卡片封面素材（前若干个成员的海报），服务端随列表一并给出 */
  covers: CollectionCover[];
  position: number;
}

/** 合集卡片的一张封面图：合集自己没有图，封面就是成员的海报。 */
export interface CollectionCover {
  url: string;
  /** 微缩占位图 data URI；缺图时为 null */
  blur: string | null;
}

/** 创建 / 更新合集的请求体：不传的字段一律「不改动」。 */
export interface CollectionPayload {
  name?: string;
  library_id?: number | null;
  rules?: FilterRule[];
  sort?: string;
  visibility?: "household" | "private";
  /** 固定名单（「固定当前这 N 部」就是把此刻的命中集快照过来） */
  item_ids?: number[];
  /** 创建时把 rules 此刻的命中集固化成名单（此后不再自动收录）。
   *  由服务端定格，客户端因此不必把上千个 id 拉下来再传回去 */
  snapshot?: boolean;
  /** 隐藏 / 取消隐藏 */
  hidden?: boolean;
}

/** 系列里的一部作品：库里有没有、在追没在追。 */
export interface SeriesPart {
  tmdb_id: number;
  title: string;
  release_date: string | null;
  poster_url: string | null;
  /** 库里已有的那条；null=缺这一部 */
  media_item_id: number | null;
  /** 已经在追（有订阅） */
  subscribed: boolean;
}

/** 系列合集的「已有 N / 共 M」与缺片名单。 */
export interface CollectionSeries {
  series_name: string | null;
  owned_count: number;
  total: number;
  image_url: string | null;
  parts: SeriesPart[];
  /** 拉到上游档案了吗；false=没配 TMDB / 网络不通 / 本地系列没有上游档案 */
  available: boolean;
}

/**
 * 合集列表。默认**不返回成员为 0 的合集**——点进去空无一物的合集是纯粹的死路，
 * 只有管理界面才需要 includeEmpty。
 */
export function listCollections(params?: {
  libraryId?: number;
  includeEmpty?: boolean;
  includeHidden?: boolean;
}): Promise<Collection[]> {
  const query = new URLSearchParams();
  if (params?.libraryId !== undefined) query.set("library_id", String(params.libraryId));
  if (params?.includeEmpty) query.set("include_empty", "true");
  if (params?.includeHidden) query.set("include_hidden", "true");
  const suffix = query.size > 0 ? `?${query}` : "";
  return unwrap(request<ApiEnvelope<Collection[]>>(`/collections${suffix}`));
}

export function getCollection(id: number): Promise<Collection> {
  return unwrap(request<ApiEnvelope<Collection>>(`/collections/${id}`));
}

export function createCollection(payload: CollectionPayload): Promise<Collection> {
  return unwrap(
    request<ApiEnvelope<Collection>>("/collections", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

export function updateCollection(id: number, payload: CollectionPayload): Promise<Collection> {
  return unwrap(
    request<ApiEnvelope<Collection>>(`/collections/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

export function deleteCollection(id: number): Promise<void> {
  return unwrap(request<ApiEnvelope<void>>(`/collections/${id}`, { method: "DELETE" }));
}

/** 合集成员：与单库海报墙同一份聚合，卡片因此长得一模一样。 */
export function listCollectionItems(
  id: number,
  params?: { limit?: number; offset?: number },
): Promise<LibraryItem[]> {
  const query = new URLSearchParams();
  if (params?.limit !== undefined) query.set("limit", String(params.limit));
  if (params?.offset) query.set("offset", String(params.offset));
  const suffix = query.size > 0 ? `?${query}` : "";
  return unwrap(request<ApiEnvelope<LibraryItem[]>>(`/collections/${id}/items${suffix}`));
}

/**
 * 把作品加进手动合集。**幂等**：已经在里面的直接忽略，不报错。
 *
 * 只有名单驱动的合集能加——规则驱动的成员是条件求值出来的，手工塞进去会
 * 静默消失，所以服务端直接拒绝并说明出路。
 */
export function addCollectionItems(id: number, mediaItemIds: number[]): Promise<Collection> {
  return unwrap(
    request<ApiEnvelope<Collection>>(`/collections/${id}/items`, {
      method: "POST",
      body: JSON.stringify({ media_item_ids: mediaItemIds }),
    }),
  );
}

/** 把一部作品移出手动合集（不动作品本身）。 */
export function removeCollectionItem(id: number, mediaItemId: number): Promise<Collection> {
  return unwrap(
    request<ApiEnvelope<Collection>>(`/collections/${id}/items/${mediaItemId}`, {
      method: "DELETE",
    }),
  );
}

/** 拖拽出来的顺序整体覆盖；没传的成员按原序留在末尾。 */
export function reorderCollectionItems(
  id: number,
  mediaItemIds: number[],
): Promise<Collection> {
  return unwrap(
    request<ApiEnvelope<Collection>>(`/collections/${id}/order`, {
      method: "PUT",
      body: JSON.stringify({ media_item_ids: mediaItemIds }),
    }),
  );
}

/** 系列合集的缺片名单（懒加载：打开详情页才拉一次上游档案）。 */
export function getCollectionSeries(id: number): Promise<CollectionSeries> {
  return unwrap(request<ApiEnvelope<CollectionSeries>>(`/collections/${id}/series`));
}

/** 把合集的规则设为某个库的收藏范围（同一份条件的第三个时态）。 */
export function applyCollectionToLibrary(id: number, libraryId: number): Promise<void> {
  return unwrap(
    request<ApiEnvelope<void>>(`/collections/${id}/apply-to-library?library_id=${libraryId}`, {
      method: "POST",
    }),
  );
}
