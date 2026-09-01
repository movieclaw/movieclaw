"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { Composer } from "@/components/composer";
import { CopyButton, REVEAL_CLASS, useTapReveal } from "@/components/copy-button";
import { useConfirm, useToast } from "@/components/feedback";
import { HighlightedCode } from "@/components/highlighted-code";
import { LlmSetupNotice, useLlmConfigured } from "@/components/llm-gate";
import { ChevronRightIcon, PencilIcon } from "@/components/icons";
import { Markdown } from "@/components/markdown";
import type { CodeLang } from "@/lib/shiki";
import {
  type AgentConversation,
  type AgentProcessItem,
  type AgentTurn,
  type AgentTurnImage,
  type AgentTurnSegment,
  type AgentTurnToolCall,
  useAgentConversations,
} from "@/lib/agent-conversations";
import type { ComposerImage } from "@/lib/agent-attachments";
import { parseSkillTokens } from "@/lib/agent-skills";
import { useSkillNames } from "@/lib/skill-names";
import { sessionAttachmentUrl } from "@/lib/api/agent";
import { useDefaultModelThinkingLevels } from "@/lib/llm-thinking";
import { usePageChrome } from "@/lib/page-chrome";
import { usePageTitle } from "@/lib/use-page-title";

/**
 * Agent 会话页（/sessions/[id]）—— 对齐 ChatGPT / Claude 的对话交互：
 * 顶部标题条 + 可滚动消息列（用户右侧气泡 / Agent 左侧全宽正文）+ 底部固定
 * Composer。流式生成中支持停止；随时可追问下一轮（自动携带多轮历史）。
 *
 * 呈现取舍（刻意做减法）：
 * - 回复不挂模型头像。整栏正文本身就是「谁在说话」的最强信号，头像只会在
 *   每一轮里重复一次同样的信息，还把正文推离左边界；
 * - 轮次页脚只留耗时一项。供应商/模型/token 这些明细是排查信息不是阅读信息，
 *   收进 title 悬浮提示，需要时才看；
 * - 不写「正在启动」这类阶段旁白。进行中就用页脚的进度环 + 实时秒数表达，
 *   一个持续推进的动画比一句会过期的文案更可信。
 */
export function AgentConversationView({ conversationId }: { conversationId: string }) {
  const { get, open, send, stop, retry } = useAgentConversations();
  const conversation = get(conversationId);
  usePageTitle(conversation?.title);
  // 移动端全局顶栏：把会话标题挂上去顶替品牌字标（见 lib/page-chrome.tsx），
  // 页面自己的标题行随之只在桌面端保留——窄屏上不再吃「顶栏 + 标题」两行高度。
  // 标题可能先于详情就绪（侧栏最近会话已带），也会随自动命名更新，跟着值重挂即可。
  const chrome = usePageChrome();
  const title = conversation?.title;
  useEffect(() => {
    if (!chrome || !title) return;
    return chrome.setTopBarTitle(title);
  }, [chrome, title]);
  const [input, setInput] = useState("");
  // 思维链档位：undefined = 用户没动过选择器（发送时不传，服务端沿用会话
  // 上一条）；null = 显式「默认」；string = 显式档位。展示值回落到会话最近
  // 一轮的档位（转录信封回放）。
  const [thinkingChoice, setThinkingChoice] = useState<string | null | undefined>(undefined);
  const thinkingLevels = useDefaultModelThinkingLevels();
  const [retryTarget, setRetryTarget] = useState<{
    conversationId: string;
    messageId: string;
  } | null>(null);
  const [retrying, setRetrying] = useState(false);
  const activeRetryTarget =
    retryTarget?.conversationId === conversationId ? retryTarget : null;
  // 服务端详情加载失败的提示（404 = 会话不存在；其余为网络/服务错误）
  const [loadError, setLoadError] = useState<string | null>(null);
  const confirm = useConfirm();
  const toast = useToast();
  // 供应商被删除后打开旧会话：追问同样锁定并引导去设置（false = 明确未配置）
  const locked = useLlmConfigured() === false;

  // 打开会话：详情未加载时从服务端回放（running 会话同时重挂事件流）
  useEffect(() => {
    setLoadError(null);
    open(conversationId).catch((error) => {
      setLoadError((error as Error).message);
    });
  }, [conversationId, open]);

  // 自动滚动：仅当用户本就贴近底部时跟随新内容（ChatGPT 同款行为——
  // 用户上滚查看历史时不打断）。离开底部时给一个「回到底部」按钮，否则
  // 生成中的新内容在视野外持续增长，用户只能手动滚很久才追得上。
  const scrollRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && nearBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [conversation]);

  // 软键盘弹起时外壳按 --keyboard-inset 收缩（见 components/viewport-keyboard.tsx），
  // 消息列跟着变矮但 scrollTop 不变——本来贴底的最后几条会被顶出视野，用户
  // 一点输入框就「丢了上下文」。键盘每次伸缩后重新贴底（离开底部时不打扰）。
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const pin = () => {
      const el = scrollRef.current;
      if (el && nearBottomRef.current) el.scrollTop = el.scrollHeight;
    };
    vv.addEventListener("resize", pin);
    return () => vv.removeEventListener("resize", pin);
  }, []);

  /** 改写入口只进入本地编辑态，不提前触碰服务端历史。 */
  const handleEdit = useCallback(
    (messageId: string, original: string) => {
      setRetryTarget({ conversationId, messageId });
      setInput(original);
    },
    [conversationId],
  );

  if (loadError) {
    return (
      <div className="immersive-theme flex h-full items-center justify-center px-6">
        <div className="max-w-sm text-center">
          <p className="text-body font-medium text-[var(--text)]">无法打开会话</p>
          <p className="mt-2 text-sub leading-5 text-[var(--text-muted)]">{loadError}</p>
        </div>
      </div>
    );
  }

  if (!conversation?.loaded) {
    return (
      <div className="immersive-theme flex h-full items-center justify-center px-6">
        <p className="animate-pulse text-body text-[var(--text-muted)]">正在加载会话…</p>
      </div>
    );
  }

  const running = conversation.turns.some((t) => t.status === "running");
  // 会话当前生效的档位 = 最近一轮的信封值（乐观轮次可能没有，取最近的有值项）
  const sessionThinkingLevel =
    [...conversation.turns].reverse().find((t) => t.thinkingLevel !== undefined)
      ?.thinkingLevel ?? null;
  const displayedThinking = thinkingChoice === undefined ? sessionThinkingLevel : thinkingChoice;

  function submit(text: string, images: ComposerImage[]) {
    if (!activeRetryTarget) {
      setInput("");
      send(
        conversationId,
        text,
        images.map((image) => ({
          attachmentId: image.attachmentId,
          name: image.name,
          previewUrl: image.previewUrl,
        })),
        // 没动过选择器就不传（服务端沿用）；动过则显式传档位或 "default"
        thinkingChoice === undefined ? undefined : (thinkingChoice ?? "default"),
      );
      return;
    }
    void (async () => {
      const agreed = await confirm({
        title: "重新提交这条提问？",
        description:
          "这条提问及其之后的对话会被新问题替换，原记录无法恢复。",
        confirmLabel: "替换并重新提问",
        tone: "danger",
      });
      if (!agreed) return;
      setRetrying(true);
      try {
        await retry(conversationId, activeRetryTarget.messageId, text);
        setInput("");
        setRetryTarget(null);
      } catch (error) {
        toast.error((error as Error).message);
      } finally {
        setRetrying(false);
      }
    })();
  }

  return (
    // 沉浸页：内容直接铺在页面纯色底（.page-solid）上，不再包阅读面板卡片；
    // immersive-theme 在容器内切换为中性灰阶 + 系统字体的阅读配色
    <div className="immersive-theme flex h-full flex-col">
      {/* 顶部条：只留会话标题，且仅桌面端渲染——移动端标题已挂进全局顶栏
          （见上方 setTopBarTitle），这里再画一行就是重复信息。「生成中 / 就绪」
          的全局状态点已由每轮页脚的进度环取代——状态属于那一轮，放在正文旁边
          比钉在标题上更有指向性 */}
      <header className="flex h-14 shrink-0 items-center px-5 max-md:hidden">
        <h1 className="truncate text-body font-semibold tracking-[-0.01em]">
          {conversation.title}
        </h1>
      </header>

      {/* 消息列 */}
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            const near = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
            nearBottomRef.current = near;
            // 只在跨越阈值时落 state，滚动过程中不做无谓的重渲染
            setAtBottom((previous) => (previous === near ? previous : near));
          }}
          // overflow-x-hidden 兜底：任何子元素意外超宽（如未换行的长 token）都不许
          // 把消息列撑出横向滚动——移动端一旦横滚，整列内容会被推出屏幕左侧
          className="scroll-thin min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-6 py-6 max-md:px-4 max-md:py-4"
        >
          <div className="mx-auto max-w-3xl space-y-8">
            {conversation.handoff && <HandoffCard handoff={conversation.handoff} />}
            {conversation.turns.map((turn) => (
              // 运行中不给改写入口：服务端会拒绝替换正在写轨迹的会话
              <TurnView
                key={turn.id}
                turn={turn}
                sessionId={conversationId}
                onEdit={running || retrying ? undefined : handleEdit}
              />
            ))}
          </div>
        </div>

        {!atBottom && (
          <button
            type="button"
            aria-label="回到最新消息"
            onClick={() => {
              const el = scrollRef.current;
              if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
            }}
            className="absolute bottom-3 left-1/2 flex size-8 -translate-x-1/2 items-center justify-center rounded-full border border-white/10 bg-[#232325] text-[var(--text-muted)] shadow-lg transition-colors hover:text-[var(--text)]"
          >
            <ChevronRightIcon className="size-4 rotate-90" />
          </button>
        )}
      </div>

      {/* 底部输入：生成中可继续打字，发送键变停止键。
          移动端主区不再整体让位底部安全区（见 globals.css），贴底的输入行
          要自己把 Home 指示条的高度让出来，否则发送键会被指示条压住 */}
      <div className="shrink-0 px-4 pb-5 pt-2 max-md:px-3 max-md:pb-[calc(0.75rem+var(--safe-bottom))]">
        <div className="mx-auto max-w-3xl">
          {activeRetryTarget && (
            <div className="mb-2 flex items-center justify-between px-2 text-caption text-[var(--text-muted)]">
              <span>正在改写较早的提问，发送后将替换其后的对话</span>
              <button
                type="button"
                onClick={() => {
                  setRetryTarget(null);
                  setInput("");
                }}
                className="rounded-md px-2 py-1 text-[var(--text-faint)] transition-colors hover:text-[var(--text)]"
              >
                取消改写
              </button>
            </div>
          )}
          <Composer
            flat
            value={input}
            onChange={setInput}
            onSubmit={submit}
            // 改写模式不开图片入口：retry 默认沿用原消息的图，此时新加的图
            // 无处安放，藏起入口比静默丢弃诚实
            imageUpload={!activeRetryTarget}
            skillPicker
            thinkingLevels={thinkingLevels}
            thinkingValue={displayedThinking}
            onThinkingChange={setThinkingChoice}
            busy={running}
            onStop={() => stop(conversationId)}
            disabled={locked || (retrying && activeRetryTarget != null)}
            placeholder={
              locked
                ? "请先接入 AI 模型，再继续对话"
                : activeRetryTarget
                  ? "修改问题后发送，将从这里重新生成回答"
                  : undefined
            }
          />
          {locked && <LlmSetupNotice />}
        </div>
      </div>
    </div>
  );
}

/** 新会话只展示来源关系，不把旧消息重复画成当前会话里可重试的轮次。 */
function HandoffCard({
  handoff,
}: {
  handoff: NonNullable<AgentConversation["handoff"]>;
}) {
  const sourceLabel = handoff.sourceTitle || `会话 ${handoff.sourceSessionId.slice(0, 8)}`;
  return (
    <div className="flex items-center justify-between gap-4 rounded-xl border border-white/8 bg-white/[0.025] px-4 py-3 text-ui text-[var(--text-muted)] max-md:items-start">
      <div className="min-w-0">
        <p className="font-medium text-[var(--text)]">已从「{sourceLabel}」续接上下文</p>
        <p className="mt-1 text-caption leading-5 text-[var(--text-faint)]">
          这是一个独立的新会话；后续操作不会改写原会话。
        </p>
      </div>
      <Link
        href={`/sessions/${handoff.sourceSessionId}` as Route}
        className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-caption text-[var(--text-muted)] transition-colors hover:text-[var(--text)]"
      >
        查看原会话
        <ChevronRightIcon className="size-3.5" />
      </Link>
    </div>
  );
}

/* —— 单轮：用户气泡 + Agent 回应块 —— */

/** memo：流式更新只替换正在生成那一轮的 turn 对象（store 是逐轮不可变更新），
 *  历史轮次引用不变、比对通过即整轮跳过——长会话里没有它，每次增量都要
 *  重建全部历史轮次的元素树。 */
const TurnView = memo(function TurnView({
  turn,
  sessionId,
  onEdit,
}: {
  turn: AgentTurn;
  /** 会话 id：气泡里的图片按它拼附件下载地址 */
  sessionId: string;
  /** 改写本轮重问；不给（运行中）则气泡上不出现该入口 */
  onEdit?: (messageId: string, input: string) => void;
}) {
  const messageId = turn.messageId;
  return (
    <div className="group/turn space-y-3">
      {/* 用户消息：右侧玻璃气泡 */}
      <UserBubble
        text={turn.input}
        images={turn.images}
        sessionId={sessionId}
        onEdit={onEdit && messageId ? () => onEdit(messageId, turn.input) : undefined}
      />

      {/* Agent 回应：整栏正文，不挂头像、不套气泡（ChatGPT / Claude 同款版式） */}
      <div className="min-w-0 space-y-2.5">
        {turn.segments.map((segment, index) => {
          // 时间线按序渲染：process 折叠块与正文交替出现，压缩卡片作分隔
          const isLast = index === turn.segments.length - 1;
          const active = turn.status === "running" && isLast;
          if (segment.kind === "process") {
            return <ProcessBlock key={index} segment={segment} active={active} />;
          }
          if (segment.kind === "compaction") {
            return <CompactionCard key={index} segment={segment} />;
          }
          return (
            <div key={index}>
              <Markdown text={segment.text} />
              {active && <StreamingCursor />}
            </div>
          );
        })}

        {turn.status === "error" && (
          <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-3.5 py-2.5 text-ui leading-5 break-words text-[#ff6b6b]">
            {turn.error}
          </div>
        )}

        <TurnFooter turn={turn} />
      </div>
    </div>
  );
});

/**
 * 用户提问气泡 + 浮现的行内操作（复制 / 改写重问）。
 *
 * 单独拆成组件是为了把「点开/收起」的 state 关在这一条消息里——留在 TurnView
 * 里的话，点一下气泡会连带重渲染同一轮那一大段 Markdown 正文。
 *
 * 操作键落在气泡左侧而不是下方：下方要么常占一行高度（气泡与回答之间凭空
 * 多出一条空隙），要么浮现时把下文顶一下。左侧是右对齐气泡天然空出来的地方，
 * 不占额外高度、也不会推动任何内容。
 *
 * flex-row-reverse：DOM 顺序保持「先正文、后操作」（读屏与 Tab 顺序更自然），
 * 视觉上仍是气泡贴右、操作键在它左边。
 */
function UserBubble({
  text,
  images,
  sessionId,
  onEdit,
}: {
  text: string;
  images?: AgentTurnImage[];
  sessionId: string;
  onEdit?: () => void;
}) {
  const { revealProps, toggle } = useTapReveal();
  // /skill:名字 占位符渲染成技能 chip，正文只留用户自己的话（复制与
  // 改写重问仍用完整的 token 形态原文）。只拆已知技能：拼错/不存在的
  // token 服务端不会展开，气泡保留字面文本，不冒充「已调用」的 chip；
  // 名单加载完成前（null）暂不过滤，避免 chip 闪烁成字面文本
  const knownSkills = useSkillNames();
  const { names: skillNames, text: plainText } = parseSkillTokens(
    text,
    knownSkills ?? undefined,
  );
  return (
    <div className="group/copy flex flex-row-reverse items-end justify-start" {...revealProps}>
      {/* 触摸端点气泡浮现操作键（桌面端靠 hover，这一下点击是多余但无害的）。
          onClick 挂在气泡而非整行上：右对齐留出的空白不该也是热区。 */}
      <div
        onClick={toggle}
        className="selectable max-w-[80%] whitespace-pre-wrap break-words rounded-2xl bg-[var(--glass-fill-active)] px-4 py-3 text-body leading-6 text-[var(--text)]"
      >
        {skillNames.length > 0 && (
          <span
            className={`flex flex-wrap gap-1.5 ${plainText || images?.length ? "mb-2" : ""}`}
          >
            {skillNames.map((name) => (
              <span
                key={name}
                className="inline-flex items-center gap-1 rounded-lg bg-white/[0.08] px-2 py-0.5 text-caption text-[var(--text-muted)]"
              >
                ⚡ {name}
              </span>
            ))}
          </span>
        )}
        {images && images.length > 0 && (
          <span className={`flex flex-wrap gap-2 ${plainText ? "mb-2" : ""}`}>
            {images.map((image) => (
              // 乐观渲染优先本地 objectURL；回放走附件下载接口（immutable 缓存）。
              // 点击开新页看原图——转录页不再实现单独的灯箱。
              <a
                key={image.attachmentId}
                href={image.previewUrl ?? sessionAttachmentUrl(sessionId, image.attachmentId)}
                target="_blank"
                rel="noreferrer"
                onClick={(event) => event.stopPropagation()}
              >
                <img
                  src={image.previewUrl ?? sessionAttachmentUrl(sessionId, image.attachmentId)}
                  alt={image.name ?? "图片"}
                  loading="lazy"
                  className="max-h-40 max-w-[200px] rounded-lg border border-white/10 object-cover"
                />
              </a>
            ))}
          </span>
        )}
        {plainText}
      </div>
      <CopyButton
        text={text}
        className={`${REVEAL_CLASS} touch-target mb-1 mr-1 shrink-0 p-1 text-[var(--text-faint)] hover:text-[var(--text)]`}
      />
      {onEdit && (
        <button
          type="button"
          aria-label="改写这条提问"
          title="改写这条提问"
          onClick={(event) => {
            // 不冒泡到气泡的 toggle：否则点完操作键，浮现态立刻被切回去
            event.stopPropagation();
            onEdit();
          }}
          className={`${REVEAL_CLASS} touch-target mb-1 mr-1 shrink-0 rounded-md p-1 text-[var(--text-faint)] transition-colors hover:text-[var(--text)]`}
        >
          <PencilIcon className="size-3.5" />
        </button>
      )}
    </div>
  );
}

/* —— 轮次页脚：进度与耗时 —— */

/** 耗时文案：不足一分钟按秒（终态保留一位小数），超过则「x 分 y 秒」。 */
function formatDuration(ms: number, precise: boolean): string {
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 60) return `${precise ? seconds.toFixed(1) : Math.floor(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.floor(seconds - minutes * 60)} 秒`;
}

/** 进行中的实时耗时。秒级跳动即可——十分之一秒的滚动数字只会制造焦虑，
 *  「还在推进」这件事由进度环表达，秒数负责量级。 */
function useElapsed(startedAt: number, running: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  return now - startedAt;
}

/**
 * 一轮的收尾行：进行中 = 进度环 + 实时耗时；完成 = 最终耗时 + 复制。
 * 模型、供应商、token、步数只进 title 悬浮提示——它们是排查用的元数据，
 * 常驻上屏会和正文抢注意力。
 */
function TurnFooter({ turn }: { turn: AgentTurn }) {
  const running = turn.status === "running";
  const live = useElapsed(turn.startedAt, running);

  if (turn.status === "error") return null;
  if (running) {
    return (
      <div className="flex items-center gap-2 text-caption text-[var(--text-faint)]">
        <ProgressRing />
        <span className="tabular-nums">{formatDuration(live, false)}</span>
      </div>
    );
  }

  const result = turn.result;
  // 本轮跑完时有 result（精确到 0.1s）；从转录回放的历史轮次只能按消息
  // 时间戳估算（秒级），两者都没有就不占这一行
  const duration = result
    ? formatDuration(result.elapsed_ms, true)
    : turn.endedAt
      ? formatDuration(turn.endedAt - turn.startedAt, false)
      : null;
  const answer = turn.segments
    .filter((segment) => segment.kind === "text")
    .map((segment) => segment.text)
    .join("\n\n")
    .trim();
  if (!duration && !turn.stopped && !turn.interrupted && !answer) return null;

  return (
    <div className="flex items-center gap-2 text-caption text-[var(--text-faint)]">
      {turn.stopped && <span>已停止</span>}
      {!turn.stopped && turn.interrupted && <span title="本轮运行未正常结束（停机或异常中断），可直接继续对话">已中断</span>}
      {duration && (
        <span
          className="tabular-nums"
          title={
            result
              ? `${turn.provider ?? result.provider} · ${turn.model ?? result.model} · ${result.steps} 步 · ${result.usage.prompt_tokens}/${result.usage.completion_tokens} tokens`
              : undefined
          }
        >
          {duration}
        </span>
      )}
      {answer && (
        <CopyButton
          text={answer}
          label="复制"
          className="px-1 py-0.5 opacity-0 transition-opacity hover:text-[var(--text-muted)] focus-visible:opacity-100 group-hover/turn:opacity-100 max-md:opacity-100"
        />
      )}
    </div>
  );
}

/** 进度环：细环上的一段旋转弧。比闪烁的圆点更像「仍在推进」，
 *  也不需要一句会过期的文案来解释自己。 */
function ProgressRing() {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      className="size-3.5 animate-spin text-[var(--text-muted)] [animation-duration:0.9s]"
    >
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" opacity="0.25" />
      <path
        d="M8 2a6 6 0 0 1 6 6"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** 进行中的处理块此刻在做什么：取末尾条目推导实时状态。 */
function processStatus(items: AgentProcessItem[]): string {
  const tail = items[items.length - 1];
  if (!tail || tail.kind === "thinking") return "思考中…";
  if (tail.argsDone === false) return `准备调用 ${tail.name}…`;
  if (tail.output === undefined) return `正在执行 ${tail.name}…`;
  return "思考中…"; // 工具已返回，正在等模型的下一步
}

/** 已完成处理块的一句话总结，如「已思考，执行 1 次命令，调用 2 次工具」。 */
function processSummary(items: AgentProcessItem[]): string {
  const tools = items.filter((item) => item.kind === "tool");
  const commands = tools.filter((tool) => tool.name === "bash").length;
  const others = tools.length - commands;
  const parts: string[] = [];
  if (items.some((item) => item.kind === "thinking")) parts.push("思考");
  if (commands > 0) parts.push(`执行 ${commands} 次命令`);
  if (others > 0) parts.push(`调用 ${others} 次工具`);
  return parts.length > 0 ? `已${parts.join("，")}` : "处理过程";
}

/**
 * 处理过程折叠块（仿 Claude）：头部进行中显示实时状态、完成后显示一句话
 * 总结；点击展开思考与工具调用的混合列表（按实际发生顺序）。
 * 展开后是第二级折叠：工具调用只呈现单行摘要清单，点击某一行才展开
 * 该次调用的参数与输出——一轮动辄十几次调用，全量平铺等于没有排版。
 */
const ProcessBlock = memo(function ProcessBlock({ segment, active }: { segment: AgentTurnSegment & { kind: "process" }; active: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-sub font-medium text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)]"
      >
        <ChevronRightIcon className={`size-3.5 transition-transform ${open ? "rotate-90" : ""}`} />
        <span className={active ? "animate-pulse" : undefined}>
          {active ? processStatus(segment.items) : processSummary(segment.items)}
        </span>
      </button>
      {open && (
        <div className="mt-1.5 space-y-2 border-l-2 border-white/[0.08] pl-3">
          {segment.items.map((item, index) =>
            item.kind === "thinking" ? (
              // break-words 必须有：思考文本常出现资源名这种几十字符无空格的长
              // token（点号不是换行机会点），不断词会把整个消息列撑出横向溢出
              <p
                key={index}
                className="selectable whitespace-pre-wrap break-words text-sub leading-5 text-[var(--text-faint)]"
              >
                {item.text}
              </p>
            ) : (
              <ToolCallCard key={item.id} tool={item} />
            ),
          )}
        </div>
      )}
    </div>
  );
});

/**
 * 从定稿的 label（形如 `name({...json...})`）还原参数明细：
 * bash 直接取出 command 按 shell 展示，其余工具美化 JSON。
 * 解析失败（历史数据/异常参数）时原样展示括号内文本。
 */
function toolInput(tool: AgentTurnToolCall): { lang: CodeLang; code: string } | null {
  const raw = tool.label.slice(tool.name.length + 1, -1);
  if (!raw || raw === "{}") return null;
  try {
    const args = JSON.parse(raw) as Record<string, unknown>;
    if (tool.name === "bash" && typeof args.command === "string") {
      return { lang: "bash", code: args.command };
    }
    return { lang: "json", code: JSON.stringify(args, null, 2) };
  } catch {
    return { lang: "json", code: raw };
  }
}

/**
 * 单行参数摘要（清单态用）：bash 把命令压成一行，其余工具压成
 * 「键: 值」串。只求一眼认出这次调用在干什么，超出部分由 CSS 截断。
 */
function toolSummary(tool: AgentTurnToolCall): string {
  const raw = tool.label.slice(tool.name.length + 1, -1);
  if (!raw || raw === "{}") return "";
  try {
    const args = JSON.parse(raw) as Record<string, unknown>;
    if (tool.name === "bash" && typeof args.command === "string") {
      return args.command.replace(/\s+/g, " ").trim();
    }
    return Object.entries(args)
      .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
      .join(", ");
  } catch {
    return raw;
  }
}

/** 单次工具调用（对齐 Claude/Codex 的两级呈现）：默认只占一行——
 *  工具名 + 参数摘要 + 状态尾标（✓/✗/进度环），点击行内任意处才展开
 *  参数（shiki 高亮）与输出详情。
 *  memo：store 对工具条目也是不可变更新，未被触碰的行整行跳过；
 *  摘要与参数明细的 JSON.parse/stringify（大参数可达几 KB）按 tool 缓存，
 *  不再在每次渲染中内联执行。 */
const ToolCallCard = memo(function ToolCallCard({ tool }: { tool: AgentTurnToolCall }) {
  const [open, setOpen] = useState(false);
  const streaming = tool.argsDone === false;
  const input = useMemo(() => (streaming ? null : toolInput(tool)), [tool, streaming]);
  const summary = useMemo(() => (streaming ? "" : toolSummary(tool)), [tool, streaming]);

  return (
    <div className="min-w-0">
      <button
        type="button"
        // 参数生成中没有可展开的定稿详情，此时行只作进度展示、不可点击
        disabled={streaming}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full min-w-0 items-center gap-1.5 py-0.5 text-left font-mono text-caption disabled:cursor-default"
      >
        <ChevronRightIcon
          className={`size-3 shrink-0 text-[var(--text-faint)] transition-transform ${open ? "rotate-90" : ""}`}
        />
        <span className="shrink-0 text-[var(--accent-2)]">{tool.name}</span>
        {streaming ? (
          // 参数生成中：右锚定滚动显示最新生成的尾部——长参数溢出时新字符
          // 持续从右侧推入，肉眼可见任务仍在进行
          <span className="flex min-w-0 flex-1 justify-end overflow-hidden [mask-image:linear-gradient(to_right,transparent,black_24px)]">
            <span className="whitespace-nowrap text-[var(--text-faint)]">
              {tool.label.slice(tool.name.length + 1).slice(-300)}
            </span>
          </span>
        ) : (
          <span className="min-w-0 flex-1 truncate text-[var(--text-faint)]">{summary}</span>
        )}
        {streaming || tool.output === undefined ? (
          <span className="shrink-0">
            <ProgressRing />
          </span>
        ) : (
          <span className={`shrink-0 ${tool.isError ? "text-[#ff6b6b]" : "text-[var(--text-faint)]"}`}>
            {tool.isError ? "✗" : "✓"}
          </span>
        )}
      </button>
      {open && !streaming && (
        <div className="mt-1 min-w-0 rounded-lg border border-white/[0.05] bg-white/[0.02] px-2.5 py-1.5">
          {input && <HighlightedCode code={input.code} lang={input.lang} />}
          {tool.output === undefined ? (
            <p className="mt-0.5 text-caption text-[var(--text-faint)]">执行中…</p>
          ) : (
            <div
              // 行高用相对值：字号随语义 token 在移动端换档（11→13px），固定 16px 行高会挤死多行日志
              className={`selectable scroll-thin mt-1 max-h-44 overflow-y-auto whitespace-pre-wrap break-words font-mono text-caption leading-[1.45] ${
                tool.isError ? "text-[#ff6b6b]" : "text-[var(--text-muted)]"
              }`}
            >
              {tool.output}
            </div>
          )}
        </div>
      )}
    </div>
  );
});

/**
 * 上下文压缩分隔卡片：横线分隔 + 居中标签，点击展开交接摘要。
 * 它同时是「多次压缩会降低准确性」的可见信号——卡片越多，会话越该另起。
 */
const CompactionCard = memo(function CompactionCard({
  segment,
}: {
  segment: AgentTurnSegment & { kind: "compaction" };
}) {
  const [open, setOpen] = useState(false);
  const tokens =
    segment.tokensBefore != null && segment.tokensAfter != null
      ? `${segment.tokensBefore} → ${segment.tokensAfter} tokens`
      : null;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-caption text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)]"
      >
        <span className="h-px flex-1 bg-white/[0.08]" />
        <ChevronRightIcon className={`size-3 transition-transform ${open ? "rotate-90" : ""}`} />
        <span>已压缩上下文{tokens ? `（${tokens}）` : ""}</span>
        <span className="h-px flex-1 bg-white/[0.08]" />
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5 border-l-2 border-white/[0.08] pl-3">
          <p className="selectable whitespace-pre-wrap break-words text-sub leading-5 text-[var(--text-faint)]">
            {segment.summary}
          </p>
          <p className="text-caption text-[var(--text-faint)]">
            此前的对话已由交接摘要替代。多次压缩可能降低模型准确性，建议适时开启新会话。
          </p>
        </div>
      )}
    </div>
  );
});

/** 流式光标：正文尾部的呼吸圆点。 */
function StreamingCursor() {
  return (
    <span className="ml-0.5 inline-block size-[13px] translate-y-[2px] animate-pulse rounded-full bg-[var(--text-muted)]" />
  );
}
