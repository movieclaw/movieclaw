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

/** 单个已连接 Worker 的运行状态（见 RemoteWorkerRegistry.snapshot）。 */
export interface RemoteTranscodeWorker {
  worker_id: string;
  worker_version: string | null;
  arch: string | null;
  platform: string | null;
  ffmpeg_version: string | null;
  backends: string[];
  max_jobs: number;
  active_jobs: number;
  draining: boolean;
  /** 距最近一次收到该 Worker 消息的秒数。 */
  last_seen_seconds: number;
  online: boolean;
}

export interface RemoteTranscodeStatus {
  enabled: boolean;
  base_url_configured: boolean;
  ready: boolean;
  workers: RemoteTranscodeWorker[];
}

/**
 * 读取远程转码运行状态（管理员接口）。
 *
 * 配置项「填没填」和 Worker「连没连上」是两件事：前者看 config.ready，后者只能
 * 看这里。少了它，用户配完只能去开一部片、打开播放诊断面板才知道成没成功。
 */
export function getRemoteTranscodeStatus(): Promise<RemoteTranscodeStatus> {
  return unwrap(
    request<ApiEnvelope<RemoteTranscodeStatus>>("/transcode-worker/status"),
  );
}

/** 配对码前缀，必须与 macOS Worker 的 PairingCode.prefix 一致。 */
const PAIRING_CODE_PREFIX = "movieclaw-worker-v1.";

/**
 * 在浏览器本地拼出配对码，供用户粘贴到 Worker App。
 *
 * 刻意不做成接口：配对码含 Token 明文，等价于凭据本身。页面只在用户刚生成或
 * 刚输入 Token 的那一刻手里有明文，此时本地拼装即可，服务端因此仍然可以坚持
 * 「令牌只写不读」——没有任何接口能把它读回来。
 */
export function buildPairingCode(baseURL: string, token: string): string {
  const payload = JSON.stringify({ url: baseURL, token });
  // TextEncoder + btoa：直接 btoa 处理非 ASCII 会抛错，地址里可能有非 ASCII 主机名
  const bytes = new TextEncoder().encode(payload);
  const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
  const base64url = btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
  return PAIRING_CODE_PREFIX + base64url;
}

/** 生成一个高熵 Worker Token。 */
export function generateWorkerToken(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  // base64url：可双击全选、不含容易被终端或聊天工具转义的字符
  const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}
