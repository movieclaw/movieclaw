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

/** 已绑定的微信账号（见 schemas.channels.WeixinAccountView）。 */
export interface WeixinAccount {
  account_id: string;
  /** 扫码绑定人的微信用户 id（白名单：只有此人能对话） */
  bound_user_id: string | null;
  /** active=正常；stale=凭据失效需重新扫码 */
  status: "active" | "stale";
  /** 当前进程内收发循环是否在运行 */
  running: boolean;
  last_error: string | null;
  bound_at: string;
}

/** 绑定流程状态（见 schemas.channels.WeixinBindingStatusView 的注释）。 */
export type WeixinBindingStatus =
  | "pending"
  | "scanned"
  | "need_verify_code"
  | "confirmed"
  | "already_bound"
  | "expired"
  | "failed";

export interface WeixinBindingStart {
  challenge_id: string;
  qrcode_url: string;
  /** 服务端渲染好的二维码 SVG data URL，<img> 直接显示 */
  qrcode_image: string;
  message: string;
}

export interface WeixinBindingSnapshot {
  challenge_id: string;
  status: WeixinBindingStatus;
  message: string;
  qrcode_url: string;
  qrcode_image: string;
  account: WeixinAccount | null;
}

export function listWeixinAccounts(init?: RequestInit): Promise<WeixinAccount[]> {
  return unwrap(request<ApiEnvelope<WeixinAccount[]>>("/channels/weixin/accounts", init));
}

export function startWeixinBinding(): Promise<WeixinBindingStart> {
  return unwrap(
    request<ApiEnvelope<WeixinBindingStart>>("/channels/weixin/bindings", { method: "POST" }),
  );
}

export function getWeixinBindingStatus(challengeId: string): Promise<WeixinBindingSnapshot> {
  return unwrap(
    request<ApiEnvelope<WeixinBindingSnapshot>>(
      `/channels/weixin/bindings/${encodeURIComponent(challengeId)}`,
    ),
  );
}

export function submitWeixinVerifyCode(
  challengeId: string,
  code: string,
): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/channels/weixin/bindings/${encodeURIComponent(challengeId)}/verify-code`,
      { method: "POST", body: JSON.stringify({ code }) },
    ),
  );
}

export function unbindWeixinAccount(accountId: string): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/channels/weixin/accounts/${encodeURIComponent(accountId)}`,
      { method: "DELETE" },
    ),
  );
}

// ---------------------------------------------------------------------------
// Telegram / Discord / 飞书（见 api/routes/channels_im.py）
// ---------------------------------------------------------------------------

export type ImChannelId = "telegram" | "discord" | "feishu";

/** 已绑定的 TG/Discord/飞书账号（见 schemas.channels.ImAccountView）。 */
export interface ImAccount {
  channel_id: string;
  account_id: string;
  /** 完成配对的用户 id（白名单，同时是推送目标） */
  bound_user_id: string | null;
  status: "active" | "stale";
  running: boolean;
  last_error: string | null;
  bound_at: string;
}

/** 配对绑定状态（发起返回与轮询同一结构）。 */
export interface ImBinding {
  challenge_id: string;
  status: "pending" | "confirmed" | "expired" | "failed";
  /** 面板展示的 6 位配对码：用户私聊 bot 发这串数字完成绑定 */
  pair_code: string;
  bot_name: string;
  message: string;
  account: ImAccount | null;
}

export function listImAccounts(channel: ImChannelId): Promise<ImAccount[]> {
  return unwrap(request<ApiEnvelope<ImAccount[]>>(`/channels/im/${channel}/accounts`));
}

export function startImBinding(channel: ImChannelId, token: string): Promise<ImBinding> {
  return unwrap(
    request<ApiEnvelope<ImBinding>>(`/channels/im/${channel}/bindings`, {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  );
}

export function getImBindingStatus(channel: ImChannelId, challengeId: string): Promise<ImBinding> {
  return unwrap(
    request<ApiEnvelope<ImBinding>>(
      `/channels/im/${channel}/bindings/${encodeURIComponent(challengeId)}`,
    ),
  );
}

export function unbindImAccount(
  channel: ImChannelId,
  accountId: string,
): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/channels/im/${channel}/accounts/${encodeURIComponent(accountId)}`,
      { method: "DELETE" },
    ),
  );
}

/**
 * 接入飞书群自定义机器人：粘贴 Webhook 地址即绑即用（服务端发欢迎消息验真，
 * 无配对码、无轮询）。secret 为签名校验密钥，未开启签名校验传空。
 */
export function startFeishuBinding(webhookUrl: string, secret: string): Promise<ImAccount> {
  return unwrap(
    request<ApiEnvelope<ImAccount>>(`/channels/im/feishu/bindings`, {
      method: "POST",
      body: JSON.stringify({ webhook_url: webhookUrl, secret }),
    }),
  );
}

export function sendChannelPushTest(text?: string): Promise<{ sent: number }> {
  return unwrap(
    request<ApiEnvelope<{ sent: number }>>(`/channels/im/push-test`, {
      method: "POST",
      body: JSON.stringify({ text: text ?? "" }),
    }),
  );
}

/** 推送内容开关（哪些系统事件会推送到已绑定通道）。 */
export interface ChannelPushConfig {
  /** 订阅命中并投递下载时推送 */
  push_dispatch: boolean;
  /** 下载完成整理入库时推送 */
  push_imported: boolean;
}

export function getChannelPushConfig(): Promise<ChannelPushConfig> {
  return unwrap(request<ApiEnvelope<ChannelPushConfig>>(`/channels/im/push-config`));
}

export function updateChannelPushConfig(config: ChannelPushConfig): Promise<ChannelPushConfig> {
  return unwrap(
    request<ApiEnvelope<ChannelPushConfig>>(`/channels/im/push-config`, {
      method: "PUT",
      body: JSON.stringify(config),
    }),
  );
}
