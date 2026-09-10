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

/** 成员详情（见 schemas.member.MemberView）。 */
export interface MemberView {
  id: number;
  username: string;
  nickname: string;
  avatar_url: string | null;
  status: "active" | "disabled";
  /** 最近登录时间（ISO 字符串）；null=从未登录 */
  last_login_at: string | null;
  allow_subscribe: boolean;
  allow_search: boolean;
  allow_direct_download: boolean;
  /** true=全部库可见（含未来新建）；false=按 library_ids 白名单 */
  all_libraries: boolean;
  /** 内容年龄上限（岁）；null=不限。超出的作品在墙/搜索/合集/Jellyfin/详情/起播六处都看不到 */
  content_age_limit: number | null;
  /** 设了上限时，未分级的作品是否仍可见 */
  allow_unrated: boolean;
  library_ids: number[];
  /** true=全部站点可用；false=按 site_ids 白名单 */
  all_sites: boolean;
  site_ids: string[];
  created_at: string;
}

/** 编辑成员的可选字段；未提供的字段不改动，白名单为整体覆盖。 */
export interface MemberUpdatePayload {
  nickname?: string;
  allow_subscribe?: boolean;
  allow_search?: boolean;
  allow_direct_download?: boolean;
  all_libraries?: boolean;
  /** 内容年龄上限；**取消上限传 -1**（不传是「不改动」，两者不是一回事） */
  content_age_limit?: number;
  allow_unrated?: boolean;
  library_ids?: number[];
  all_sites?: boolean;
  site_ids?: string[];
}

export function listMembers(): Promise<MemberView[]> {
  return unwrap(request<ApiEnvelope<MemberView[]>>("/members"));
}

export function createMember(
  username: string,
  password: string,
  nickname: string,
): Promise<MemberView> {
  return unwrap(
    request<ApiEnvelope<MemberView>>("/members", {
      method: "POST",
      body: JSON.stringify({ username, password, nickname }),
    }),
  );
}

export function updateMember(id: number, payload: MemberUpdatePayload): Promise<MemberView> {
  return unwrap(
    request<ApiEnvelope<MemberView>>(`/members/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

export function setMemberStatus(id: number, enabled: boolean): Promise<MemberView> {
  return unwrap(
    request<ApiEnvelope<MemberView>>(`/members/${id}/status`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  );
}

/** 重置密码：返回新密码明文，仅此一次，请立即复制发给成员。 */
export function resetMemberPassword(
  id: number,
): Promise<{ id: number; username: string; password: string }> {
  return unwrap(
    request<ApiEnvelope<{ id: number; username: string; password: string }>>(
      `/members/${id}/reset-password`,
      { method: "POST" },
    ),
  );
}

export function deleteMember(id: number): Promise<void> {
  return unwrap(request<ApiEnvelope<void>>(`/members/${id}`, { method: "DELETE" }));
}
