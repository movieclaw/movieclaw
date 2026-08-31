import { resolveRequestUrl, redirectToLoginOn401, HttpError, request } from "@/lib/http";

/** Agent 执行事件（见 movieclaw_agent.events.AgentEvent）。 */
export type AgentEventType =
  | "agent_start"
  | "thinking_delta"
  | "text_delta"
  | "tool_call_start"
  | "tool_call_delta"
  | "tool_call"
  | "tool_result"
  | "context_compacted"
  | "agent_done"
  | "agent_error"
  | "agent_cancelled";

export interface AgentToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

/** 工具执行回执（tool_result 事件的载荷）。 */
export interface AgentToolResult {
  tool_call_id: string;
  name: string;
  /** 喂回模型的结果文本（事件里截断到 2000 字） */
  output: string;
  is_error: boolean;
  elapsed_ms: number;
}

/** 上下文压缩回执（context_compacted 事件的载荷；token 数为估算值）。 */
export interface AgentCompaction {
  summary: string;
  tokens_before: number;
  tokens_after: number;
}

/** agent_done 的终态载荷（text/thinking 为最后一步产出，usage 为全程累计）。 */
export interface AgentDone {
  text: string | null;
  thinking: string | null;
  finish_reason: string | null;
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  /** 模型调用步数（agent loop 的轮数） */
  steps: number;
  model: string;
  provider: string;
  elapsed_ms: number;
}

export interface AgentEvent {
  type: AgentEventType;
  /** thinking_delta / text_delta / tool_call_delta 的增量文本 */
  delta?: string;
  /** tool_call_start：仅含 id/name；tool_call：参数完整的调用 */
  tool_call?: AgentToolCall;
  /** tool_call_delta：增量所属的工具调用 id */
  tool_call_id?: string;
  tool_result?: AgentToolResult;
  /** context_compacted：压缩回执 */
  compaction?: AgentCompaction;
  /** agent_start：实际路由到的供应商与模型 */
  provider?: string;
  model?: string;
  result?: AgentDone;
  error?: string;
}

/* —— 服务端会话（JSONL 转录的投影，见 movieclaw_api.schemas.agent）—— */

/** 转录消息的内容块（movieclaw_llm ContentPart 的前端投影）。
 *  image 块只带引用（attachment_id）：字节永不进转录接口，
 *  渲染时经 sessionAttachmentUrl 取图。 */
export type AgentContentPart =
  | { type: "text"; text: string }
  | { type: "thinking"; text: string }
  | {
      type: "image";
      url?: string | null;
      data?: string | null;
      media_type?: string | null;
      attachment_id?: string | null;
      name?: string | null;
    };

/** 转录里的一次工具调用（参数已由协议层解析为对象）。 */
export interface AgentTranscriptToolCall {
  id: string;
  name: string;
  arguments?: Record<string, unknown>;
  /** 供应商返回的原始参数 JSON；arguments 解析失败时用它兜底展示 */
  raw_arguments?: string;
}

/** 转录里的 LLM API 原样消息（按 role 分发渲染）。 */
export interface AgentTranscriptMessage {
  role: "system" | "user" | "assistant" | "tool";
  content?: string | AgentContentPart[];
  /** 仅 assistant */
  tool_calls?: AgentTranscriptToolCall[] | null;
  /** 仅 tool：结果所属的调用 id（合并进对应调用卡片） */
  tool_call_id?: string | null;
  name?: string | null;
}

/** 会话详情里的一条消息 entry（信封 + API 格式消息）。 */
export interface SessionMessageEntry {
  type: "message";
  message_id: string;
  parent_id?: string;
  timestamp: string;
  message: AgentTranscriptMessage;
  /** 以下仅 assistant 消息携带（运行元数据） */
  model?: string | null;
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | null;
  /** 约定含 "aborted"：该步产出时运行被取消 */
  finish_reason?: string | null;
  /** user 消息生效的思维链档位；null/缺省 = 模型默认 */
  thinking_level?: string | null;
}

/** 会话详情里的一条压缩行；replacement_history 是续聊所用的完整替代上下文。 */
export interface SessionCompactionEntry {
  type: "compaction";
  compaction_id: string;
  parent_id?: string;
  timestamp: string;
  summary: string;
  tokens_before?: number;
  tokens_after?: number;
  replacement_history: AgentTranscriptMessage[];
}

/** 从另一会话创建独立新会话时写入的上下文快照。 */
export interface SessionHandoffEntry {
  type: "handoff";
  handoff_id: string;
  parent_id?: string;
  timestamp: string;
  source_session_id: string;
  source_leaf_id?: string | null;
  source_title?: string | null;
  /** 完整快照只在 fork 创建响应里下发一次；常规轨迹读取固定为空数组。页面只展示来源卡片，不重复渲染旧消息。 */
  replacement_history: AgentTranscriptMessage[];
}

export type SessionAnyEntry =
  | SessionMessageEntry
  | SessionCompactionEntry
  | SessionHandoffEntry;

/** 会话列表项；后台运行编号是服务端实现细节，不进入公开协议。 */
export interface SessionSummary {
  id: string;
  title: string | null;
  last_prompt: string | null;
  entry_count: number;
  running: boolean;
  created_at: string;
  updated_at: string;
}

export interface SessionTranscript {
  session: SessionSummary;
  entries: SessionAnyEntry[];
}

interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

/** 图片附件上传成功的回执；attachment_id 随后交给 startSession 引用。 */
export interface AgentAttachmentUpload {
  attachment_id: string;
  name: string;
  width: number;
  height: number;
  bytes: number;
}

/** 上传一张图片附件（先上传拿 id，发消息时引用；24h 未引用会被服务端回收）。 */
export async function uploadSessionAttachment(
  file: Blob,
  name: string,
): Promise<AgentAttachmentUpload> {
  const form = new FormData();
  form.append("file", file, name);
  const response = await request<ApiEnvelope<AgentAttachmentUpload>>(
    "/sessions/attachments",
    { method: "POST", body: form },
  );
  return response.data;
}

/** 会话内附件的取图地址（immutable 内容，浏览器可长期缓存）。 */
export function sessionAttachmentUrl(sessionId: string, attachmentId: string): string {
  return resolveRequestUrl(`/sessions/${sessionId}/attachments/${attachmentId}`);
}

/** 提交一条用户消息；不传 sessionId 新建会话，传入则继续已有会话。
 *  attachments 为已上传的图片附件编号（内容块由服务端组装）；
 *  thinkingLevel 为思维链档位（"default" 清回模型默认，不传沿用上一条）。 */
export async function startSession(
  content: string,
  sessionId?: string,
  attachments?: string[],
  thinkingLevel?: string,
): Promise<{ sessionId: string; messageId: string }> {
  const body: {
    content: string;
    session_id?: string;
    attachments?: string[];
    thinking_level?: string;
  } = { content };
  if (sessionId) body.session_id = sessionId;
  if (attachments && attachments.length > 0) body.attachments = attachments;
  if (thinkingLevel) body.thinking_level = thinkingLevel;
  const response = await request<ApiEnvelope<{ session_id: string; message_id: string }>>(
    "/sessions",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
  return { sessionId: response.data.session_id, messageId: response.data.message_id };
}

/** 一个可显式调用的 Agent 技能（composer 加号菜单的数据源）。 */
export interface AgentSkill {
  /** 技能名，也是 /skill:名字 占位符里的名字 */
  name: string;
  description: string;
  /** builtin=随产品内置，user=用户技能目录 */
  scope: string;
}

/** 可显式调用的技能清单（服务端每次现扫，改技能即生效，因此不做缓存）。 */
export async function listSkills(): Promise<AgentSkill[]> {
  const response = await request<ApiEnvelope<AgentSkill[]>>("/skills");
  return response.data;
}

/** 最近会话列表（按最后活跃时间倒序，limit/offset 分页）。 */
export async function listSessions(
  params: { limit?: number; offset?: number } = {},
): Promise<SessionSummary[]> {
  const query = new URLSearchParams();
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset) query.set("offset", String(params.offset));
  const suffix = query.size > 0 ? `?${query}` : "";
  const response = await request<ApiEnvelope<SessionSummary[]>>(`/sessions${suffix}`);
  return response.data;
}

/** 会话详情（完整消息 entry 回放）。 */
export async function getSessionTranscript(sessionId: string): Promise<SessionTranscript> {
  const response = await request<ApiEnvelope<SessionTranscript>>(`/sessions/${sessionId}`);
  return response.data;
}

/** 从源会话的有效上下文创建独立的新会话；不触发模型运行。 */
export async function forkSession(sessionId: string): Promise<SessionTranscript> {
  const response = await request<ApiEnvelope<SessionTranscript>>(`/sessions/${sessionId}/fork`, {
    method: "POST",
  });
  return response.data;
}

/** 重命名会话（标题只改索引元数据，转录内容不变）。 */
export async function renameSession(
  sessionId: string,
  title: string,
): Promise<void> {
  await request<ApiEnvelope<SessionSummary>>(`/sessions/${sessionId}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

/** 重新提交指定用户消息；content 为空时原文重试，否则用新内容替换。 */
export async function retrySessionMessage(
  sessionId: string,
  messageId: string,
  content?: string,
): Promise<{ sessionId: string; messageId: string }> {
  const body: { message_id: string; content?: string } = { message_id: messageId };
  if (content) body.content = content;
  const response = await request<ApiEnvelope<{ session_id: string; message_id: string }>>(
    `/sessions/${sessionId}/retry`,
    { method: "POST", body: JSON.stringify(body) },
  );
  return { sessionId: response.data.session_id, messageId: response.data.message_id };
}

/** 删除会话（转录文件与索引一并删除；运行中的会话会被服务端拒绝）。 */
export async function deleteSession(sessionId: string): Promise<void> {
  await request<ApiEnvelope<Record<string, never>>>(`/sessions/${sessionId}`, {
    method: "DELETE",
  });
}

/** 幂等请求停止后台运行；真正的终态由 stream 中的 agent_cancelled 确认。 */
export async function stopSession(sessionId: string): Promise<void> {
  await request<ApiEnvelope<Record<string, never>>>(`/sessions/${sessionId}/stop`, {
    method: "POST",
  });
}

function isTerminal(event: AgentEvent): boolean {
  return (
    event.type === "agent_done" ||
    event.type === "agent_error" ||
    event.type === "agent_cancelled"
  );
}

async function responseError(response: Response): Promise<HttpError> {
  let message = `Request failed with status ${response.status}`;
  let details: unknown = null;
  try {
    details = await response.json();
    if (details && typeof details === "object" && "message" in details) {
      message = String((details as { message: unknown }).message);
    }
  } catch {
    // 非 JSON 错误体，保留默认 message
  }
  redirectToLoginOn401(response.status);
  return new HttpError(message, response.status, details);
}

/** 可被 AbortSignal 打断的重连等待，避免页面卸载后残留定时器。 */
function waitBeforeRetry(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * 订阅一次后台运行的 SSE 事件。
 *
 * 每个数据帧必须带递增 ``id``。连接意外结束时用 Last-Event-ID 续传，服务端
 * 因此只回放缺失部分；HTTP 错误（含运行过期 404）直接交给调用方，只有网络
 * 中断才指数退避重试。函数收到终态事件后返回，不会再次连接已完成的运行。
 */
export async function streamSession(
  sessionId: string,
  onEvent: (event: AgentEvent) => void,
  opts?: { signal?: AbortSignal; afterEventId?: number },
): Promise<void> {
  let lastEventId = opts?.afterEventId ?? 0;
  let retryDelay = 500;

  for (;;) {
    let response: Response;
    try {
      const headers = new Headers({ Accept: "text/event-stream" });
      if (lastEventId > 0) headers.set("Last-Event-ID", String(lastEventId));
      response = await fetch(resolveRequestUrl(`/sessions/${sessionId}/events`), {
        headers,
        signal: opts?.signal,
      });
    } catch (error) {
      if ((error as Error).name === "AbortError") throw error;
      await waitBeforeRetry(retryDelay, opts?.signal);
      retryDelay = Math.min(retryDelay * 2, 5000);
      continue;
    }

    if (!response.ok) throw await responseError(response);
    if (!response.body) {
      throw new HttpError("当前环境不支持流式响应", response.status, null);
    }

    retryDelay = 500;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminal = false;

    const dispatch = (block: string) => {
      let id = 0;
      let eventName = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("id: ")) id = Number(line.slice(4).trim());
        else if (line.startsWith("event: ")) eventName = line.slice(7).trim();
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      // SSE 注释心跳没有 id/event/data，直接忽略。
      if (!id || !eventName || !data || id <= lastEventId) return;
      const event = JSON.parse(data) as AgentEvent;
      onEvent(event);
      lastEventId = id;
      terminal = isTerminal(event);
    };

    let streamError: unknown = null;
    try {
      while (!terminal) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let separator: number;
        while ((separator = buffer.indexOf("\n\n")) !== -1) {
          dispatch(buffer.slice(0, separator));
          buffer = buffer.slice(separator + 2);
          if (terminal) break;
        }
      }
    } catch (error) {
      streamError = error;
    } finally {
      reader.releaseLock();
    }

    if (terminal) return;
    if ((streamError as Error | null)?.name === "AbortError") throw streamError;
    // 包括 reader.read() 在传输中途抛出的网络错误：保留 lastEventId，按同一
    // 退避策略重新 GET，服务端只补发尚未确认的事件。
    await waitBeforeRetry(retryDelay, opts?.signal);
    retryDelay = Math.min(retryDelay * 2, 5000);
  }
}
