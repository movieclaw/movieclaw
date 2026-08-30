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

/** 客户端形态。worker 只能转码，cli 与 manual 无形态上限。 */
export type DeviceClientType = "worker" | "cli" | "manual" | string;

/**
 * 一条待批准的接入请求（见 schemas.auth.DeviceRequestView）。
 *
 * 这是用户做批准决定的全部依据：谁在请求、从哪来、码是多少。所以四个字段
 * 都要原样展示，不做省略。
 *
 * 真正的安全控制是 user_code——它要和设备屏幕上显示的那串对上。source_ip 是
 * 辅助线索，而且**可能为空串**：容器桥接网络会把源地址 NAT 成网桥网关，
 * 那时它认不出任何设备，服务端会如实返回空（见 api/client_address.py），
 * 界面必须说「无法确定」而不是补一个占位地址。
 */
export interface DeviceRequestView {
  user_code: string;
  client_type: DeviceClientType;
  client_name: string;
  /** 可能为空串：容器网络改写了源地址，认不出是哪台机器。 */
  source_ip: string;
  expires_in: number;
}

/** 一枚已签发的令牌（见 schemas.auth.ApiTokenView）；明文永不回读。 */
export interface DeviceTokenView {
  id: string;
  name: string;
  created_at: string;
  client_type: DeviceClientType;
  last_used_at: string | null;
}

/** 列出待批准的接入请求（管理员会话专属）。 */
export function listDeviceRequests(): Promise<DeviceRequestView[]> {
  return unwrap(request<ApiEnvelope<DeviceRequestView[]>>("/auth/devices/requests"));
}

/** 批准一台设备接入：服务端此刻才签发令牌，等设备来兑换。 */
export function approveDeviceRequest(userCode: string): Promise<null> {
  return unwrap(
    request<ApiEnvelope<null>>(
      `/auth/devices/requests/${encodeURIComponent(userCode)}/approve`,
      { method: "POST" },
    ),
  );
}

/** 拒绝一台设备接入：不生成任何令牌。 */
export function denyDeviceRequest(userCode: string): Promise<null> {
  return unwrap(
    request<ApiEnvelope<null>>(
      `/auth/devices/requests/${encodeURIComponent(userCode)}/deny`,
      { method: "POST" },
    ),
  );
}

/** 列出已连接的设备（即已签发且未吊销的令牌）。 */
export function listDevices(): Promise<DeviceTokenView[]> {
  return unwrap(request<ApiEnvelope<DeviceTokenView[]>>("/auth/tokens"));
}

/** 吊销一台设备：立即失效，不影响其他设备。 */
export function revokeDevice(tokenId: string): Promise<null> {
  return unwrap(
    request<ApiEnvelope<null>>(`/auth/tokens/${encodeURIComponent(tokenId)}`, {
      method: "DELETE",
    }),
  );
}
