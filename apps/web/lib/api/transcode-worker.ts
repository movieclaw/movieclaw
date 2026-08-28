import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse）。 */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

export type RemoteTranscodeBaseUrlSource =
  | "remote_transcode_setting"
  | "system_external_url"
  | "unset"
  | string;

export interface RemoteTranscodeConfigView {
  enabled: boolean;
  /** 仅返回是否已配置，不返回 Worker Token 明文。 */
  worker_token_configured: boolean;
  /** 实际使用的远程转码根地址。 */
  base_url: string;
  /** 网页配置的远程转码专用地址；为空时跟随系统外部访问地址。 */
  base_url_override: string;
  base_url_source: RemoteTranscodeBaseUrlSource;
  max_artifact_bytes: number;
  ready: boolean;
  issues: string[];
}

export interface RemoteTranscodeConfigPayload {
  enabled: boolean;
  /** null=保持当前专用地址，空字符串=清除并回退系统外部访问地址。 */
  base_url?: string | null;
  /** null=保持当前令牌，空字符串=清除令牌。 */
  worker_token?: string | null;
  max_artifact_bytes: number;
}

/** 读取远程转码配置（管理员接口）。 */
export function getRemoteTranscodeConfig(): Promise<RemoteTranscodeConfigView> {
  return unwrap(
    request<ApiEnvelope<RemoteTranscodeConfigView>>("/transcode-worker/config"),
  );
}

/** 保存远程转码配置，保存后立即生效。 */
export function saveRemoteTranscodeConfig(
  payload: RemoteTranscodeConfigPayload,
): Promise<RemoteTranscodeConfigView> {
  return unwrap(
    request<ApiEnvelope<RemoteTranscodeConfigView>>("/transcode-worker/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}
