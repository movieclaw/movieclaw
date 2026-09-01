"use client";

/**
 * Agent 会话的全站客户端状态（服务端会话模型）。
 *
 * 事实源在服务端（JSONL 转录 + SQLite 索引），本 store 只是它的渲染缓存：
 * - 会话列表来自 GET /sessions（仅摘要，消息派生轮次为空的「未加载」壳）；
 * - 打开某会话时用详情接口把 entries 回放成 AgentTurn 时间线；
 * - 开始与继续统一调用 POST /sessions；session_id 决定是否续接，历史由服务端重建；
 * - running 的会话直接用 session_id 重新挂上 SSE：正在运行那一轮丢弃已落盘
 *   的局部产出、从事件 0 完整回放，避免转录快照与事件游标不同步造成重复。
 *
 * 后台运行与 SSE 连接保持解耦：公开层始终只使用 sessionId；网络
 * 中断由 SSE 客户端按事件序号续传，关闭页面也不会取消后端任务。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type AgentDone,
  type AgentEvent,
  type AgentTranscriptMessage,
  type SessionAnyEntry,
  type SessionSummary,
  type SessionTranscript,
  deleteSession,
  forkSession,
  getSessionTranscript,
  listSessions,
  renameSession,
  retrySessionMessage,
  startSession,
  stopSession,
  streamSession,
} from "@/lib/api/agent";
import { parseSkillTokens, toTokenForm } from "@/lib/agent-skills";
import { HttpError } from "@/lib/http";
import { nanoid } from "nanoid";
import { usePermissions } from "@/lib/permissions";

/** 一次工具调用及其执行回执（tool_call_start 创建、tool_call_delta 逐片
 * 追加参数、tool_call 定稿参数、tool_result 补全回执）。 */
export interface AgentTurnToolCall {
  id: string;
  /** 工具名，如 write / bash；处理过程块的状态与总结按它分类 */
  name: string;
  /** 展示用摘要，如 search({"q":"沙丘"})；参数生成中为逐片追加的半成品 */
  label: string;
  /** 参数是否已生成完整（tool_call 事件到达）；undefined 视为已完整（回放数据） */
  argsDone?: boolean;
  /** 执行回执（未返回时为 undefined = 执行中） */
  output?: string;
  isError?: boolean;
  elapsedMs?: number;
}

/** 处理过程条目：一段思维链或一次工具调用，按实际发生顺序排列。 */
export type AgentProcessItem =
  | { kind: "thinking"; text: string }
  | ({ kind: "tool" } & AgentTurnToolCall);

/**
 * 时间线段（仿 Claude 的呈现模型）：
 * - process：连续的思考/工具活动折叠为一个「处理过程」块；
 * - text：模型的正文输出；
 * - compaction：一次上下文压缩（分隔卡片，摘要可展开）。
 * agent loop 中 process/text 交替出现（每步：思考/调工具 → 可能穿插正文 → 下一步）。
 */
export type AgentTurnSegment =
  | { kind: "process"; items: AgentProcessItem[] }
  | { kind: "text"; text: string }
  | { kind: "compaction"; summary: string; tokensBefore?: number; tokensAfter?: number };

/** 用户消息携带的一张图片。previewUrl 是发送方本地的 objectURL（乐观渲染）；
 *  回放数据没有它，渲染方按 attachmentId 走会话附件下载接口取图。 */
export interface AgentTurnImage {
  attachmentId: string;
  name?: string;
  previewUrl?: string;
}

/** 一轮对话：用户输入 + Agent 的完整产出。 */
export interface AgentTurn {
  id: string;
  /** 开启本展示轮次的 user message 稳定编号，是「改写重问」的 retry 锚点。
   *  Turn 只是前端从消息序列派生的展示分组，不是服务端协议实体。 */
  messageId?: string;
  input: string;
  /** 随本轮用户消息发送的图片（气泡里渲染缩略图）；无图时省略 */
  images?: AgentTurnImage[];
  /** 本轮生效的思维链档位（null=模型默认）；回放数据必有值，乐观轮次
   *  仅在发送时显式指定过才有 */
  thinkingLevel?: string | null;
  status: "running" | "done" | "error";
  /** 本轮开始时刻（epoch ms）：进行中据此显示实时耗时；回放时取用户消息的转录时间戳 */
  startedAt: number;
  /** 回放专用的结束时刻（本轮最后一条转录消息的时间戳）。历史轮次没有 result
   *  里的精确 elapsed_ms，用它估算耗时，页脚才不会只有历史会话是空的 */
  endedAt?: number;
  /** agent_start 事件带回的实际路由信息 */
  provider?: string;
  model?: string;
  /** 按发生顺序排列的产出时间线（处理过程块与正文交替） */
  segments: AgentTurnSegment[];
  result?: AgentDone;
  error?: string;
  /** 用户主动停止：status 为 done 但结果不完整 */
  stopped?: boolean;
  /** 回放派生的中断标记：本轮没有以「无工具调用的 assistant 正文」正常收
   *  尾（停机/崩溃/模型报错都长这样），页脚提示「已中断」让用户知道这轮
   *  不完整、可以直接继续发消息 */
  interrupted?: boolean;
}

export interface AgentConversation {
  /** 服务端会话 id（路由 /sessions/[id] 与所有会话动作都用它） */
  id: string;
  title: string;
  updatedAt: number;
  /** 是否有存活的运行（列表摘要派生；详情加载后随本地 turn 状态刷新） */
  running: boolean;
  turns: AgentTurn[];
  /** 详情是否已从服务端回放；false 表示只有列表摘要壳 */
  loaded: boolean;
  /** 从旧会话续开时的来源快照；只用于展示来源，实际上下文由服务端持久化。 */
  handoff?: {
    sourceSessionId: string;
    sourceTitle?: string;
  };
}

interface AgentConversationsValue {
  /** 按最近更新倒序的会话列表（侧栏「最近会话」的数据源） */
  conversations: AgentConversation[];
  /** 服务端还有更早的会话没取回（侧栏据此决定是否继续触底加载） */
  hasMore: boolean;
  /** 正在取下一页（避免触底时重复发起，也用于渲染加载提示） */
  loadingMore: boolean;
  /** 取下一页会话摘要并追加到列表尾部；没有更多或正在加载时是空操作 */
  loadMore: () => void;
  get: (id: string) => AgentConversation | undefined;
  /** 打开会话：详情未加载时从服务端回放，running 时用会话 id 重挂 SSE */
  open: (id: string) => Promise<void>;
  /** 新建服务端会话并发起首轮运行，成功后返回会话 id（调用方跳转 /sessions/[id]）。
   *  thinkingLevel 是提交给服务端的线上值（档位或 "default"；undefined=沿用） */
  start: (input: string, images?: AgentTurnImage[], thinkingLevel?: string) => Promise<string>;
  /** 从已有会话上下文创建独立新会话，不启动模型，成功后返回新会话 id。 */
  fork: (conversationId: string) => Promise<string>;
  /** 在既有会话中追问一轮（历史由服务端从转录重建，只传 session_id） */
  send: (
    conversationId: string,
    input: string,
    images?: AgentTurnImage[],
    thinkingLevel?: string,
  ) => void;
  /** 请求后端停止当前正在生成的轮次。 */
  stop: (conversationId: string) => void;
  /** 重命名会话（改索引元数据，成功后同步本地标题）。 */
  rename: (conversationId: string, title: string) => Promise<void>;
  /** 重新提交指定用户消息；content 为空时原文重试，否则替换问题后重试。 */
  retry: (conversationId: string, messageId: string, content?: string) => Promise<void>;
  /** 彻底删除会话（服务端转录与索引一并删除；运行中的会话会被服务端拒绝）。 */
  remove: (conversationId: string) => Promise<void>;
}

const Ctx = createContext<AgentConversationsValue | null>(null);

/** 会话列表的分页页长：首屏与滚动加载共用一个页长，offset 按已取回条数推进。 */
const PAGE_SIZE = 20;

/** 没有任何消息可供命名时的会话名（服务端把 title 置空，这里是渲染兜底）。 */
const UNNAMED_TITLE = "未命名会话";

/* —— 转录 entry → AgentTurn 时间线的回放映射 —— */

/** 提取消息的正文纯文本（字符串或 text 块）。 */
function messageText(message: AgentTranscriptMessage): string {
  if (typeof message.content === "string") return message.content;
  return (message.content ?? [])
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");
}

/** 提取用户消息里的图片引用（image 块；无 attachment_id 的历史块跳过）。 */
function messageImages(message: AgentTranscriptMessage): AgentTurnImage[] {
  if (typeof message.content === "string") return [];
  return (message.content ?? [])
    .filter((part) => part.type === "image" && part.attachment_id)
    .map((part) => ({
      attachmentId: (part as { attachment_id: string }).attachment_id,
      name: (part as { name?: string | null }).name ?? undefined,
    }));
}

/** 提取消息里的思考内容（thinking 块，仅 assistant 消息可能携带）。 */
function messageThinking(message: AgentTranscriptMessage): string {
  if (typeof message.content === "string") return "";
  return (message.content ?? [])
    .filter((part) => part.type === "thinking")
    .map((part) => part.text)
    .join("");
}

/**
 * 把转录 entries 派生成展示 turn 列表：user 消息开启新一轮；assistant 的思考、
 * 正文、tool_calls 按「thinking → text → 工具」的顺序并入时间线（与流式
 * 时的事件顺序一致）；tool 消息按 tool_call_id 合并进对应调用卡片。
 */
function entriesToTurns(entries: SessionAnyEntry[]): AgentTurn[] {
  const turns: AgentTurn[] = [];
  // 每轮是否已「正常收尾」：终答 = 无 tool_calls 的 assistant 正文。没等到
  // 终答的轮次（停机被中断、模型报错断流、用户在流式前停止）在循环后统一
  // 标记 interrupted，页脚提示这轮不完整
  const closed: boolean[] = [];
  for (const entry of entries) {
    // handoff 是会话级来源卡片，旧消息已作为服务端上下文快照保存，不能在
    // 新会话里再次派生成可重试的历史轮次。
    if (entry.type === "handoff") continue;
    if (entry.type === "compaction") {
      // 压缩行没有 message：作为分隔卡片并入上一轮时间线（会话首条必是
      // user 消息，正常不会出现无归属的压缩行；万一有也直接跳过）
      const turn = turns[turns.length - 1];
      if (!turn) continue;
      turns[turns.length - 1] = {
        ...turn,
        segments: [
          ...turn.segments,
          {
            kind: "compaction",
            summary: entry.summary,
            tokensBefore: entry.tokens_before,
            tokensAfter: entry.tokens_after,
          },
        ],
        endedAt: Date.parse(entry.timestamp),
      };
      continue;
    }
    const message = entry.message;
    if (message.role === "user") {
      const images = messageImages(message);
      turns.push({
        id: entry.message_id,
        messageId: entry.message_id,
        // 显式技能调用的展开块还原成 /skill:名字 token 形态：气泡渲染 chip、
        // 改写重问可直接编辑、重发时服务端重新展开（agent-skills.md §9.4）
        input: toTokenForm(messageText(message)),
        ...(images.length > 0 ? { images } : {}),
        thinkingLevel: entry.thinking_level ?? null,
        status: "done",
        segments: [],
        startedAt: Date.parse(entry.timestamp),
      });
      closed.push(false);
      continue;
    }
    // system 不入转录；万一出现无归属轮次的孤儿消息也直接跳过
    let turn = turns[turns.length - 1];
    if (!turn) continue;
    if (message.role === "assistant") {
      const thinking = messageThinking(message);
      if (thinking) turn = appendThinking(turn, thinking);
      const text = messageText(message);
      if (text) turn = appendText(turn, text);
      for (const call of message.tool_calls ?? []) {
        const args = call.arguments && Object.keys(call.arguments).length > 0
          ? JSON.stringify(call.arguments)
          : (call.raw_arguments ?? "{}");
        turn = appendTool(turn, {
          id: call.id,
          name: call.name,
          label: `${call.name}(${args})`,
        });
      }
      if (entry.finish_reason === "aborted") turn = { ...turn, stopped: true };
      // 终答（无工具调用且非中断半截）意味着本轮完整走完
      closed[turns.length - 1] =
        !(message.tool_calls ?? []).length && entry.finish_reason !== "aborted";
    } else if (message.role === "tool") {
      turn =
        patchTool(
          turn,
          (tool) => tool.id === message.tool_call_id,
          () => ({ output: messageText(message) }),
        ) ?? turn;
    }
    // 轮内每来一条消息就把结束时刻推后，最终停在本轮最后一条上
    turns[turns.length - 1] = { ...turn, endedAt: Date.parse(entry.timestamp) };
  }
  return turns.map((turn, index) =>
    closed[index] || turn.stopped ? turn : { ...turn, interrupted: true },
  );
}

/** 列表摘要 → 未加载的会话壳（turns 留空，打开时再回放详情）。 */
function conversationFromSummary(summary: SessionSummary): AgentConversation {
  return {
    id: summary.id,
    title: summary.title ?? summary.last_prompt ?? UNNAMED_TITLE,
    updatedAt: Date.parse(summary.updated_at),
    running: summary.running,
    turns: [],
    loaded: false,
  };
}

/** 详情 → 完整会话。running 时最后一轮重置为空，
 * 由调用方从事件 0 回放（转录里该轮已落盘的局部产出全部丢弃）。 */
function conversationFromDetail(detail: SessionTranscript): AgentConversation {
  const summary = detail.session;
  const turns = entriesToTurns(detail.entries);
  const handoffEntry = detail.entries.find((entry) => entry.type === "handoff");
  const running = summary.running;
  if (running) {
    const last = turns[turns.length - 1];
    if (last) {
      turns[turns.length - 1] = {
        ...last,
        status: "running",
        segments: [],
        endedAt: undefined,
        result: undefined,
        error: undefined,
        stopped: undefined,
        interrupted: undefined,
      };
    }
  }
  return {
    id: summary.id,
    title: summary.title ?? summary.last_prompt ?? UNNAMED_TITLE,
    updatedAt: Date.parse(summary.updated_at),
    running,
    turns,
    loaded: true,
    handoff:
      handoffEntry?.type === "handoff"
        ? {
            sourceSessionId: handoffEntry.source_session_id,
            sourceTitle: handoffEntry.source_title ?? undefined,
          }
        : undefined,
  };
}

/* —— segments 时间线的不可变更新工具 ——
 * 归约规则：思考/工具事件并入末尾的 process 块（没有则新开一个）；
 * 正文增量并入末尾的 text 段（没有则新开一个）。由此天然形成
 * 「处理过程块 ↔ 正文」交替的时间线，与 loop 的实际执行顺序一致。 */

/** 思维链增量：并入当前 process 块末尾的思考条目，或新开条目/块。 */
function appendThinking(turn: AgentTurn, delta: string): AgentTurn {
  const segments = [...turn.segments];
  const last = segments[segments.length - 1];
  if (last?.kind === "process") {
    const items = [...last.items];
    const tail = items[items.length - 1];
    if (tail?.kind === "thinking") {
      items[items.length - 1] = { ...tail, text: tail.text + delta };
    } else {
      items.push({ kind: "thinking", text: delta });
    }
    segments[segments.length - 1] = { ...last, items };
  } else {
    segments.push({ kind: "process", items: [{ kind: "thinking", text: delta }] });
  }
  return { ...turn, segments };
}

/** 正文增量：并入末尾 text 段，或新开一段（上一段是 process 块时）。 */
function appendText(turn: AgentTurn, delta: string): AgentTurn {
  const segments = [...turn.segments];
  const last = segments[segments.length - 1];
  if (last?.kind === "text") {
    segments[segments.length - 1] = { ...last, text: last.text + delta };
  } else {
    segments.push({ kind: "text", text: delta });
  }
  return { ...turn, segments };
}

/** 追加一个工具条目到当前 process 块（没有则新开一块）。 */
function appendTool(turn: AgentTurn, tool: AgentTurnToolCall): AgentTurn {
  const segments = [...turn.segments];
  const last = segments[segments.length - 1];
  const item: AgentProcessItem = { kind: "tool", ...tool };
  if (last?.kind === "process") {
    segments[segments.length - 1] = { ...last, items: [...last.items, item] };
  } else {
    segments.push({ kind: "process", items: [item] });
  }
  return { ...turn, segments };
}

/** 从后往前找到首个匹配的工具条目并打补丁；找不到返回 null。 */
function patchTool(
  turn: AgentTurn,
  match: (tool: AgentTurnToolCall) => boolean,
  patch: (tool: AgentTurnToolCall) => Partial<AgentTurnToolCall>,
): AgentTurn | null {
  for (let s = turn.segments.length - 1; s >= 0; s -= 1) {
    const segment = turn.segments[s];
    if (segment.kind !== "process") continue;
    for (let i = segment.items.length - 1; i >= 0; i -= 1) {
      const item = segment.items[i];
      if (item.kind !== "tool" || !match(item)) continue;
      const segments = [...turn.segments];
      const items = [...segment.items];
      items[i] = { ...item, ...patch(item) };
      segments[s] = { ...segment, items };
      return { ...turn, segments };
    }
  }
  return null;
}

function applyAgentEvent(turn: AgentTurn, event: AgentEvent): AgentTurn {
  switch (event.type) {
    case "agent_start":
      return { ...turn, provider: event.provider, model: event.model };
    case "thinking_delta":
      return event.delta ? appendThinking(turn, event.delta) : turn;
    case "text_delta":
      return event.delta ? appendText(turn, event.delta) : turn;
    case "tool_call_start":
      // 名称一确定就落一个条目，用户立刻能看到「正在调用哪个工具」
      return event.tool_call
        ? appendTool(turn, {
            id: event.tool_call.id,
            name: event.tool_call.name,
            label: `${event.tool_call.name}(`,
            argsDone: false,
          })
        : turn;
    case "tool_call_delta": {
      const delta = event.delta;
      if (!delta) return turn;
      // 按 id 归属增量；个别端点分片不带 id 时，退化为最后一个参数未完成的调用
      return (
        patchTool(
          turn,
          (tool) => tool.id === event.tool_call_id,
          (tool) => ({ label: tool.label + delta }),
        ) ??
        patchTool(
          turn,
          (tool) => tool.argsDone === false,
          (tool) => ({ label: tool.label + delta }),
        ) ??
        turn
      );
    }
    case "tool_call": {
      const call = event.tool_call;
      if (!call) return turn;
      // 参数定稿：用解析后的完整参数重写 label，替换流式期间的半成品
      const label = `${call.name}(${JSON.stringify(call.arguments)})`;
      return (
        patchTool(
          turn,
          (tool) => tool.id === call.id,
          () => ({ label, argsDone: true }),
        ) ?? appendTool(turn, { id: call.id, name: call.name, label, argsDone: true })
      );
    }
    case "tool_result": {
      const result = event.tool_result;
      if (!result) return turn;
      return (
        patchTool(
          turn,
          (tool) => tool.id === result.tool_call_id,
          () => ({
            output: result.output,
            isError: result.is_error,
            elapsedMs: result.elapsed_ms,
          }),
        ) ?? turn
      );
    }
    case "context_compacted":
      // 压缩卡片自成一段：其后的思考/正文会另开新块，时间线保持交替结构
      return event.compaction
        ? {
            ...turn,
            segments: [
              ...turn.segments,
              {
                kind: "compaction",
                summary: event.compaction.summary,
                tokensBefore: event.compaction.tokens_before,
                tokensAfter: event.compaction.tokens_after,
              },
            ],
          }
        : turn;
    case "agent_done":
      return { ...turn, status: "done", result: event.result };
    case "agent_error":
      return { ...turn, status: "error", error: event.error ?? "运行失败，原因未知" };
    case "agent_cancelled":
      return { ...turn, status: "done", stopped: true };
    default:
      return turn;
  }
}

export function AgentConversationsProvider({ children }: { children: React.ReactNode }) {
  const { isAdmin } = usePermissions();
  const [conversations, setConversations] = useState<AgentConversation[]>([]);
  // sessionId → 当前 SSE 读取器；仅用于页面卸载时关闭连接，不负责停止后台运行。
  const controllers = useRef(new Map<string, AbortController>());
  // 会话 id → 进行中的详情加载；并发 open 同一会话时复用同一个请求
  const pendingLoads = useRef(new Map<string, Promise<void>>());
  const conversationsRef = useRef(conversations);
  conversationsRef.current = conversations;
  // —— 分页游标 ——
  // 已从服务端取回的条数 = 下一页的 offset。用「取回条数」而不是「列表长度」：
  // 本地新建的会话也在列表里，用长度当 offset 会跳过服务端的真实数据。
  const fetchedCount = useRef(0);
  // 初始为 false：首页请求完成前不允许 loadMore。否则侧栏的「列表没撑满就补页」
  // effect 会在挂载时与首页请求并发拉同一批数据（loadingRef 只挡 loadMore 自己），
  // 两边都把 fetchedCount 加 20，下一页 offset 直接跳到 40，第 21–40 条永远丢失。
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // setState 是异步的，触底事件会连发好几次——并发闸门必须是 ref
  const loadingRef = useRef(false);

  // 挂载时拉取首页会话列表；本地已有的会话（正在流式）以本地为准。
  useEffect(() => {
    let cancelled = false;
    if (!isAdmin) return;
    void listSessions({ limit: PAGE_SIZE })
      .then((items) => {
        if (cancelled) return;
        fetchedCount.current = items.length;
        setHasMore(items.length === PAGE_SIZE);
        setConversations((previous) => {
          const local = new Map(previous.map((c) => [c.id, c]));
          const merged = items.map((item) => {
            const existing = local.get(item.id);
            if (existing) {
              local.delete(item.id);
              return existing;
            }
            return conversationFromSummary(item);
          });
          // 本地刚创建、尚未进入列表接口结果的会话保留在最前
          return [...local.values(), ...merged];
        });
      })
      .catch((error) => {
        console.warn("加载 Agent 会话列表失败", error);
      });
    const activeControllers = controllers.current;
    const activeFlushTimer = flushTimer;
    return () => {
      cancelled = true;
      for (const controller of activeControllers.values()) controller.abort();
      activeControllers.clear();
      if (activeFlushTimer.current !== null) window.clearTimeout(activeFlushTimer.current);
    };
  }, [isAdmin]);

  /** 取下一页会话摘要追加到列表尾部（侧栏滚动触底时调用）。
   *
   * 按 id 去重：offset 分页期间若有会话被顶到前面（新活跃），后一页可能
   * 重复返回已在列表里的条目——重复项直接丢弃，宁可漏一条也不渲染重复行。
   * 失败只记日志：侧栏的历史列表加载不动没必要打断用户当前的操作。 */
  const loadMore = useCallback(() => {
    if (loadingRef.current || !hasMore) return;
    loadingRef.current = true;
    setLoadingMore(true);
    void listSessions({ limit: PAGE_SIZE, offset: fetchedCount.current })
      .then((items) => {
        fetchedCount.current += items.length;
        setHasMore(items.length === PAGE_SIZE);
        setConversations((previous) => {
          const known = new Set(previous.map((c) => c.id));
          const fresh = items
            .filter((item) => !known.has(item.id))
            .map(conversationFromSummary);
          return fresh.length > 0 ? [...previous, ...fresh] : previous;
        });
      })
      .catch((error) => {
        console.warn("加载更多 Agent 会话失败", error);
      })
      .finally(() => {
        loadingRef.current = false;
        setLoadingMore(false);
      });
  }, [hasMore]);

  /** 对某会话中某轮做不可变更新，并刷新 updatedAt 与派生的 running。 */
  const updateTurn = useCallback(
    (conversationId: string, turnId: string, patch: (turn: AgentTurn) => AgentTurn) => {
      setConversations((previous) =>
        previous.map((conversation) => {
          if (conversation.id !== conversationId) return conversation;
          const turns = conversation.turns.map((turn) =>
            turn.id === turnId ? patch(turn) : turn,
          );
          return {
            ...conversation,
            updatedAt: Date.now(),
            running: turns.some((turn) => turn.status === "running"),
            turns,
          };
        }),
      );
    },
    [],
  );

  /* —— SSE 事件的批量合并 ——
   * 流式生成时 text_delta / thinking_delta 每秒可达几十次，逐个 setState 会让
   * 所有 context 消费者（侧栏、会话页的整条时间线）以同样频率重渲染，生成一条
   * 长回复就是数千次 React diff。这里按轮次缓冲事件、每 80ms 合并应用一次
   * （视觉上仍是流畅的打字机效果）；终态事件（完成/出错/取消）立即冲刷，
   * 状态切换不吃这 80ms 延迟。 */
  const eventBuffers = useRef(
    new Map<string, { conversationId: string; turnId: string; events: AgentEvent[] }>(),
  );
  const flushTimer = useRef<number | null>(null);

  const flushEvents = useCallback(() => {
    if (flushTimer.current !== null) {
      window.clearTimeout(flushTimer.current);
      flushTimer.current = null;
    }
    if (eventBuffers.current.size === 0) return;
    const buffers = [...eventBuffers.current.values()];
    eventBuffers.current.clear();
    setConversations((previous) =>
      previous.map((conversation) => {
        const mine = buffers.filter((buffer) => buffer.conversationId === conversation.id);
        if (mine.length === 0) return conversation;
        let turns = conversation.turns;
        for (const buffer of mine) {
          turns = turns.map((turn) =>
            turn.id === buffer.turnId ? buffer.events.reduce(applyAgentEvent, turn) : turn,
          );
        }
        return {
          ...conversation,
          updatedAt: Date.now(),
          running: turns.some((turn) => turn.status === "running"),
          turns,
        };
      }),
    );
  }, []);

  const enqueueEvent = useCallback(
    (conversationId: string, turnId: string, event: AgentEvent) => {
      const key = `${conversationId}:${turnId}`;
      const buffer = eventBuffers.current.get(key) ?? { conversationId, turnId, events: [] };
      buffer.events.push(event);
      eventBuffers.current.set(key, buffer);
      const terminal =
        event.type === "agent_done" ||
        event.type === "agent_error" ||
        event.type === "agent_cancelled";
      if (terminal) {
        flushEvents();
      } else if (flushTimer.current === null) {
        flushTimer.current = window.setTimeout(flushEvents, 80);
      }
    },
    [flushEvents],
  );

  /** 跟随会话当前一轮；HTTP 错误才会结束，网络抖动由 API 层续传。 */
  const connectSession = useCallback(
    (conversationId: string, turnId: string) => {
      controllers.current.get(conversationId)?.abort();
      const controller = new AbortController();
      controllers.current.set(conversationId, controller);

      void streamSession(
        conversationId,
        (event) => {
          enqueueEvent(conversationId, turnId, event);
        },
        { signal: controller.signal },
      )
        .catch((error) => {
          if ((error as Error).name === "AbortError") return;
          const message =
            error instanceof HttpError && error.status === 404
              ? "运行记录不存在或已过期，可能是服务已重启，请重新发起任务"
              : (error as Error).message;
          // 先冲刷缓冲中的事件再落错误态，保证时间线不乱序
          flushEvents();
          updateTurn(conversationId, turnId, (turn) => ({
            ...turn,
            status: "error",
            error: message,
          }));
        })
        .finally(() => {
          if (controllers.current.get(conversationId) === controller) {
            controllers.current.delete(conversationId);
          }
        });
    },
    [enqueueEvent, flushEvents, updateTurn],
  );

  /** 打开会话：已加载则直接返回；否则拉详情回放，running 时重挂 SSE。
   * 加载失败会向调用方抛出（并允许重试），不落任何本地状态。 */
  const open = useCallback(
    (id: string) => {
      const existing = conversationsRef.current.find((c) => c.id === id);
      if (existing?.loaded) return Promise.resolve();
      const pending = pendingLoads.current.get(id);
      if (pending) return pending;

      const promise = getSessionTranscript(id)
        .then((detail) => {
          const conversation = conversationFromDetail(detail);
          setConversations((previous) => {
            const rest = previous.filter((c) => c.id !== id);
            return [conversation, ...rest];
          });
          if (conversation.running) {
            const turn = conversation.turns[conversation.turns.length - 1];
            if (turn) connectSession(id, turn.id);
          }
        })
        .finally(() => {
          pendingLoads.current.delete(id);
        });
      pendingLoads.current.set(id, promise);
      return promise;
    },
    [connectSession],
  );

  /** 继续会话：先落本地展示轮次，再取得持久化消息编号并连接 SSE。 */
  const runTurn = useCallback(
    (
      conversationId: string,
      turnId: string,
      input: string,
      images?: AgentTurnImage[],
      thinkingLevel?: string,
    ) => {
      void startSession(
        input,
        conversationId,
        images?.map((image) => image.attachmentId),
        thinkingLevel,
      )
        .then(({ messageId }) => {
          updateTurn(conversationId, turnId, (current) => ({
            ...current,
            messageId,
          }));
          connectSession(conversationId, turnId);
        })
        .catch((error) => {
          updateTurn(conversationId, turnId, (current) => ({
            ...current,
            status: "error",
            error: (error as Error).message,
          }));
        });
    },
    [connectSession, updateTurn],
  );

  const start = useCallback(
    async (input: string, images?: AgentTurnImage[], thinkingLevel?: string) => {
      // 新建必须等服务端分配 session_id 才能得到路由地址，因此这一步是
      // 同步等待的；创建失败直接抛给调用方（如尚未配置模型供应商）。
      const { sessionId, messageId } = await startSession(
        input,
        undefined,
        images?.map((image) => image.attachmentId),
        thinkingLevel,
      );
      const turnId = nanoid();
      const { names: skillNames, text: plainInput } = parseSkillTokens(input);
      const optimisticTitle = skillNames.length > 0 ? `[技能] ${plainInput}`.trim() : input;
      setConversations((previous) => [
        {
          id: sessionId,
          // 标题取首轮输入的前 30 字（服务端索引同款朴素策略；纯图消息占位；
          // 技能 token 折叠成 [技能] 占位，与服务端预览同口径）
          title: optimisticTitle.slice(0, 30) || (images?.length ? "[图片]" : ""),
          updatedAt: Date.now(),
          running: true,
          turns: [
            {
              id: turnId,
              messageId,
              input,
              ...(images && images.length > 0 ? { images } : {}),
              // 与 send 同口径：跳转到会话页后选择器要能显示这一轮的档位，
              // 不能等转录回放（SPA 导航不会重拉已 loaded 的会话）
              ...(thinkingLevel !== undefined
                ? { thinkingLevel: thinkingLevel === "default" ? null : thinkingLevel }
                : {}),
              status: "running",
              segments: [],
              startedAt: Date.now(),
            },
          ],
          loaded: true,
        },
        ...previous,
      ]);
      connectSession(sessionId, turnId);
      return sessionId;
    },
    [connectSession],
  );

  const forkConversation = useCallback(async (conversationId: string) => {
    const detail = await forkSession(conversationId);
    const conversation = conversationFromDetail(detail);
    setConversations((previous) => [
      conversation,
      ...previous.filter((item) => item.id !== conversation.id),
    ]);
    return conversation.id;
  }, []);

  const send = useCallback(
    (
      conversationId: string,
      input: string,
      images?: AgentTurnImage[],
      thinkingLevel?: string,
    ) => {
      const turnId = nanoid();
      setConversations((previous) =>
        previous.map((conversation) =>
          conversation.id === conversationId
            ? {
                ...conversation,
                updatedAt: Date.now(),
                running: true,
                // 会话被截空后重新开口的第一句，同 start 那样给它命名
                // （服务端也是这条规则：title 为空时由下一条消息回填）
                title:
                  conversation.turns.length === 0 ? input.slice(0, 30) : conversation.title,
                turns: [
                  ...conversation.turns,
                  {
                    id: turnId,
                    input,
                    ...(images && images.length > 0 ? { images } : {}),
                    // 线上值 "default" 即模型默认（null）；未显式指定时留
                    // undefined，回放后由转录信封给出真值
                    ...(thinkingLevel !== undefined
                      ? { thinkingLevel: thinkingLevel === "default" ? null : thinkingLevel }
                      : {}),
                    status: "running",
                    segments: [],
                    startedAt: Date.now(),
                  },
                ],
              }
            : conversation,
        ),
      );
      runTurn(conversationId, turnId, input, images, thinkingLevel);
    },
    [runTurn],
  );

  const stop = useCallback((conversationId: string) => {
    void stopSession(conversationId).catch((error) => {
      // 保持 SSE 连接和 running 状态；停止失败时 Agent 可能仍在执行，不能在
      // 客户端伪造终态。后续真实终态仍会通过事件流正常落到界面。
      console.warn("停止 AI 会话失败，请稍后重试", error);
    });
  }, []);

  const rename = useCallback(async (conversationId: string, title: string) => {
    await renameSession(conversationId, title);
    setConversations((previous) =>
      previous.map((conversation) =>
        conversation.id === conversationId ? { ...conversation, title } : conversation,
      ),
    );
  }, []);

  /**
   * 原文重试或改写重问：服务端一次完成旧轨迹丢弃与新消息提交，成功后本地才
   * 替换时间线。请求失败时保留原对话，避免界面与服务端事实源失步。
   */
  const retry = useCallback(
    async (conversationId: string, messageId: string, content?: string) => {
      const conversation = conversationsRef.current.find((item) => item.id === conversationId);
      const index = conversation?.turns.findIndex((turn) => turn.messageId === messageId) ?? -1;
      if (!conversation || index < 0) throw new Error("这条提问已不在当前会话里");
      const input = content ?? conversation.turns[index].input;
      // 服务端 retry 不传 attachments 即沿用原消息的图；本地轮次同样保留
      const images = conversation.turns[index].images;
      const accepted = await retrySessionMessage(conversationId, messageId, content);
      const turnId = nanoid();
      setConversations((previous) =>
        previous.map((item) =>
          item.id === conversationId
            ? {
                ...item,
                updatedAt: Date.now(),
                running: true,
                title: index === 0 ? input.slice(0, 30) : item.title,
                turns: [
                  ...item.turns.slice(0, index),
                  {
                    id: turnId,
                    messageId: accepted.messageId,
                    input,
                    ...(images && images.length > 0 ? { images } : {}),
                    // 服务端 retry 不传档位即沿用原消息，本地轮次同样保留
                    thinkingLevel: conversation.turns[index].thinkingLevel,
                    status: "running",
                    segments: [],
                    startedAt: Date.now(),
                  },
                ],
              }
            : item,
        ),
      );
      connectSession(conversationId, turnId);
    },
    [connectSession],
  );

  const remove = useCallback(async (conversationId: string) => {
    // 服务端会拒绝删除运行中的会话（400），错误交给调用方提示
    await deleteSession(conversationId);
    pendingLoads.current.delete(conversationId);
    // 服务端少了一行，后续分页的 offset 要跟着回退一格，否则下一页会跳过一条
    fetchedCount.current = Math.max(0, fetchedCount.current - 1);
    setConversations((previous) =>
      previous.filter((conversation) => conversation.id !== conversationId),
    );
  }, []);

  const value = useMemo<AgentConversationsValue>(
    () => ({
      conversations: [...conversations].sort((a, b) => b.updatedAt - a.updatedAt),
      hasMore,
      loadingMore,
      loadMore,
      get: (id) => conversations.find((conversation) => conversation.id === id),
      open,
      start,
      fork: forkConversation,
      send,
      stop,
      rename,
      retry,
      remove,
    }),
    [
      conversations,
      hasMore,
      loadingMore,
      loadMore,
      open,
      start,
      forkConversation,
      send,
      stop,
      rename,
      retry,
      remove,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAgentConversations(): AgentConversationsValue {
  const context = useContext(Ctx);
  if (!context) throw new Error("useAgentConversations 必须在 AgentConversationsProvider 内使用");
  return context;
}
