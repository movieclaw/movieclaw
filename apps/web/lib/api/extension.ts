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

// ---------------------------------------------------------------------------
// 同步令牌
// ---------------------------------------------------------------------------

export interface SyncTokenView {
  enabled: boolean;
  token: string | null;
  created_at: string | null;
}

export function getSyncToken(init?: RequestInit): Promise<SyncTokenView> {
  return unwrap(request<ApiEnvelope<SyncTokenView>>("/extension/token", init));
}

export function generateSyncToken(): Promise<SyncTokenView> {
  return unwrap(request<ApiEnvelope<SyncTokenView>>("/extension/token", { method: "POST" }));
}

export function revokeSyncToken(): Promise<SyncTokenView> {
  return unwrap(request<ApiEnvelope<SyncTokenView>>("/extension/token", { method: "DELETE" }));
}

// ---------------------------------------------------------------------------
// 已配置站点（用于展示插件同步的"最近活动"）
// ---------------------------------------------------------------------------

export type SiteAuthType = "cookie" | "apikey" | "credential";
export type SiteStatus = "pending" | "verifying" | "active" | "failed";

/** 站点用户资料快照（见 schemas.site.SiteUserProfileView）：验证成功时随手抓取。 */
export interface SiteUserProfile {
  username: string;
  user_class: string;
  uploaded_bytes: number;
  downloaded_bytes: number;
  /** null = 站点未提供；0 有实际含义（无上传），展示时用「—」区分 */
  ratio: number | null;
  bonus: number | null;
  seeding_count: number;
  leeching_count: number;
  fetched_at: string;
}

export interface ConfiguredSite {
  site_id: string;
  auth_type: SiteAuthType;
  enabled: boolean;
  status: SiteStatus;
  usable: boolean;
  /** 站点保护开关：打开后订阅链路绕开该站，手动搜索/下载不受影响 */
  protected: boolean;
  /** 自动刷分享率开关 */
  boost_enabled: boolean;
  /** 刷流暂停中：做种压到极低上传限速，停止汰换与拉新种（任务保留） */
  boost_paused: boolean;
  /** 刷流存储预算（字节） */
  boost_budget_bytes: number;
  /** 刷流汰换最低保留天数（H&R 安全垫）；0=不保护，自由汰换 */
  boost_hold_days: number;
  last_verified_at: string | null;
  last_checked_at: string | null;
  last_error: string | null;
  /** 用户资料快照；从未验证成功过则为 null */
  profile: SiteUserProfile | null;
  created_at: string;
  updated_at: string;
}

/** 列出所有已配置站点（本页只关心 cookie 授权的，即插件同步管理的那些）。 */
export function listConfiguredSites(init?: RequestInit): Promise<ConfiguredSite[]> {
  return unwrap(request<ApiEnvelope<ConfiguredSite[]>>("/sites", init));
}
