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
  /** 网页「高级」里填了覆盖地址。 */
  | "remote_transcode_setting"
  /** 默认：取源/回传用接单 Worker 自己连上来的地址，没有静态值。 */
  | "worker_connection"
  | string;

export interface RemoteTranscodeConfigView {
  enabled: boolean;
  /** 静态配置出的远程转码根地址；为空表示自动跟随 Worker 连上来的地址。 */
  base_url: string;
  /** 网页配置的远程转码专用地址覆盖项；为空表示不覆盖。 */
  base_url_override: string;
  base_url_source: RemoteTranscodeBaseUrlSource;
  max_artifact_bytes: number;
  ready: boolean;
  issues: string[];
}

export interface RemoteTranscodeConfigPayload {
  enabled: boolean;
  /** null=保持当前专用地址，空字符串=清除覆盖、回到自动。 */
  base_url?: string | null;
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

