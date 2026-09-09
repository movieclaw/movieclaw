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
}

/**
 * 合集列表。默认**不返回成员为 0 的合集**——点进去空无一物的合集是纯粹的死路，
 * 只有管理界面才需要 includeEmpty。
 */
export function listCollections(params?: {
  libraryId?: number;
  includeEmpty?: boolean;
}): Promise<Collection[]> {
  const query = new URLSearchParams();
  if (params?.libraryId !== undefined) query.set("library_id", String(params.libraryId));
  if (params?.includeEmpty) query.set("include_empty", "true");
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

/** 把合集的规则设为某个库的收藏范围（同一份条件的第三个时态）。 */
export function applyCollectionToLibrary(id: number, libraryId: number): Promise<void> {
  return unwrap(
    request<ApiEnvelope<void>>(`/collections/${id}/apply-to-library?library_id=${libraryId}`, {
      method: "POST",
    }),
  );
}
