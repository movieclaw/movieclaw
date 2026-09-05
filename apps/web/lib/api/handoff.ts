import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

// ---------------------------------------------------------------------------
// 待处理事项 → Agent 诊断工单（见 movieclaw_api.api.routes.agent_handoff）
// 后端按类型组装"发生了什么 / 已判定什么 / 已自动做过什么 / 你能做什么"，
// 并在生成那一刻跑一遍现场自检；前端拿到文本后照常走 session.start 起会话。
// ---------------------------------------------------------------------------

export type HandoffKind = "notice" | "download" | "job";

export interface HandoffPrompt {
  title: string;
  prompt: string;
}

/** ref：notice 为告警 id，download 为 info_hash，job 为任务 id */
export async function fetchHandoffPrompt(kind: HandoffKind, ref: string): Promise<HandoffPrompt> {
  const res = await request<ApiEnvelope<HandoffPrompt>>("/agent-handoff", {
    method: "POST",
    body: JSON.stringify({ kind, ref }),
  });
  return res.data;
}
