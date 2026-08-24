import { request } from "@/lib/http";
import type { ConfiguredSite, SiteAuthType } from "@/lib/api/extension";

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
// 目录（可选项）：系统支持的可配置站点及其授权要求
// ---------------------------------------------------------------------------

/** 某授权类型及其要求用户填写的字段（见 schemas.site.AuthTypeRequirement）。 */
export interface AuthTypeRequirement {
  auth_type: SiteAuthType;
  required_fields: string[];
}

/** 目录项：一个系统支持的可配置站点（见 schemas.site.CatalogItem）。 */
export interface CatalogItem {
  site_id: string;
  display_name: string;
  base_url: string;
  supported_auth_types: AuthTypeRequirement[];
}

/** 列出系统支持的可配置站点（供前端渲染"可添加"列表与表单）。 */
export function listSiteCatalog(init?: RequestInit): Promise<CatalogItem[]> {
  return unwrap(request<ApiEnvelope<CatalogItem[]>>("/sites/catalog", init));
}

// ---------------------------------------------------------------------------
// 已配置站点：CRUD + 验证
// ---------------------------------------------------------------------------

/** 列出所有已配置站点及其验证状态。 */
export function listConfiguredSites(init?: RequestInit): Promise<ConfiguredSite[]> {
  return unwrap(request<ApiEnvelope<ConfiguredSite[]>>("/sites", init));
}

/** 站点种子缓存与同步节奏统计（见 schemas.site.SiteSyncStatsView）。 */
export interface SiteSyncStats {
  torrent_count: number;
  tracking_since: string | null;
  /** 上次同步完成时间；null = 从未同步过 */
  last_sync_at: string | null;
  /** 上次同步成功时间；null = 从未成功（站点故障期间 last_sync_at 仍推进，此值停留） */
  last_success_at: string | null;
  /** 下次同步到期时刻；null = 立即到期（新站等待首刷） */
  next_sync_at: string | null;
  sync_interval_seconds: number | null;
  last_new_count: number | null;
  /** 上次同步失败原因；null = 上次同步成功 */
  last_error: string | null;
  /** 连续同步失败次数；成功清零 */
  consecutive_failures: number;
}

/** 按 site_id 返回各站点的本地缓存统计；从未同步过的站点没有条目。 */
export function listSiteSyncStats(init?: RequestInit): Promise<Record<string, SiteSyncStats>> {
  return unwrap(request<ApiEnvelope<Record<string, SiteSyncStats>>>("/sites/sync-stats", init));
}

/** 获取单个已配置站点详情（用于轮询验证进度）。 */
export function getConfiguredSite(siteId: string, init?: RequestInit): Promise<ConfiguredSite> {
  return unwrap(request<ApiEnvelope<ConfiguredSite>>(`/sites/${siteId}`, init));
}

/** 配置站点时提交的授权信息。按 auth_type 只需填对应字段。 */
export interface SiteConfigPayload {
  auth_type: SiteAuthType;
  cookie?: string | null;
  api_key?: string | null;
  username?: string | null;
  password?: string | null;
  enabled?: boolean;
}

/** 新增配置一个站点（保存后后端异步验证）。 */
export function configureSite(
  siteId: string,
  payload: SiteConfigPayload,
): Promise<ConfiguredSite> {
  return unwrap(
    request<ApiEnvelope<ConfiguredSite>>("/sites", {
      method: "POST",
      body: JSON.stringify({ site_id: siteId, ...payload }),
    }),
  );
}

/** 更新已配置站点的授权信息（更新后后端重新异步验证）。 */
export function updateSite(siteId: string, payload: SiteConfigPayload): Promise<ConfiguredSite> {
  return unwrap(
    request<ApiEnvelope<ConfiguredSite>>(`/sites/${siteId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 启用 / 停用站点。 */
export function setSiteEnabled(siteId: string, enabled: boolean): Promise<ConfiguredSite> {
  return unwrap(
    request<ApiEnvelope<ConfiguredSite>>(`/sites/${siteId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  );
}

/** 打开 / 关闭站点保护（订阅链路绕开该站，手动搜索/下载不受影响）。 */
export function setSiteProtection(siteId: string, isProtected: boolean): Promise<ConfiguredSite> {
  return unwrap(
    request<ApiEnvelope<ConfiguredSite>>(`/sites/${siteId}/protection`, {
      method: "PATCH",
      body: JSON.stringify({ protected: isProtected }),
    }),
  );
}

/** 设置自动刷分享率：开关 + 存储预算 + 汰换保留期（省略的字段不修改）。 */
export function setSiteRatioBoost(
  siteId: string,
  enabled: boolean,
  budgetBytes?: number,
  holdDays?: number,
): Promise<ConfiguredSite> {
  return unwrap(
    request<ApiEnvelope<ConfiguredSite>>(`/sites/${siteId}/ratio-boost`, {
      method: "PATCH",
      body: JSON.stringify({
        enabled,
        budget_bytes: budgetBytes ?? null,
        hold_days: holdDays ?? null,
      }),
    }),
  );
}

/** 暂停 / 恢复站点刷流：暂停把在池做种压到极低上传限速（给看视频等前台
 *  流量让出上行）并停止汰换与拉新种，任务与数据全部保留；恢复解除限速。 */
export function setSiteBoostPaused(siteId: string, paused: boolean): Promise<ConfiguredSite> {
  return unwrap(
    request<ApiEnvelope<ConfiguredSite>>(`/sites/${siteId}/ratio-boost/pause`, {
      method: "PATCH",
      body: JSON.stringify({ paused }),
    }),
  );
}

/** 单个站点的刷流运行统计（见 schemas.site.SiteBoostStatsView）。 */
export interface SiteBoostStats {
  /** 在池刷流任务数 */
  active_count: number;
  /** 在池任务占用的预算（字节） */
  used_bytes: number;
  /** 当前预算（字节） */
  budget_bytes: number;
  /** 刷流累计上传量（含已汰换任务的历史贡献，字节） */
  uploaded_bytes_total: number;
  /** 累计汰换任务数 */
  evicted_count: number;
  /** 近 24 小时上传量（字节） */
  uploaded_bytes_24h: number;
  /** 近 24 小时平均在池体积（字节） */
  avg_used_bytes_24h: number;
  /** 近 7 天上传量（字节） */
  uploaded_bytes_7d: number;
  /** 近 7 天平均在池体积（字节） */
  avg_used_bytes_7d: number;
}

/** 按 site_id 返回各站点的刷流统计；从未刷流且未开启的站点没有条目。 */
export function listSiteBoostStats(init?: RequestInit): Promise<Record<string, SiteBoostStats>> {
  return unwrap(request<ApiEnvelope<Record<string, SiteBoostStats>>>("/sites/boost-stats", init));
}

/** 手动重新触发一次验证。 */
export function reverifySite(siteId: string): Promise<ConfiguredSite> {
  return unwrap(
    request<ApiEnvelope<ConfiguredSite>>(`/sites/${siteId}/verify`, { method: "POST" }),
  );
}

/** 删除站点配置（连带清理 cookie 缓存）。 */
export function deleteSite(siteId: string): Promise<{ site_id: string }> {
  return unwrap(
    request<ApiEnvelope<{ site_id: string }>>(`/sites/${siteId}`, { method: "DELETE" }),
  );
}
