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

/**
 * 各设置分区的异常/待办计数（见 routes/settings_health.py）。
 *
 * 前三个是**异常**（出事了要修，红点），最后一个是**待办**
 * （有人在等你操作，蓝点）——判定口径与各分区页面同源。
 */
export interface SettingsHealth {
  /** 验证失败的资源站点数（status=failed） */
  sites_failed: number;
  /** 连接失败的下载器数（status=failed） */
  downloaders_failed: number;
  /** 需重新绑定的推送通道账号数（微信/TG/Discord 的 stale 账号） */
  im_push_need_rebind: number;
  /** 待批准的设备接入请求数 */
  device_requests_pending: number;
}

/** 读取设置健康聚合（管理员专属）。 */
export function getSettingsHealth(): Promise<SettingsHealth> {
  return unwrap(request<ApiEnvelope<SettingsHealth>>("/settings/health"));
}
