"use client";

/**
 * 「交给 AI 分析」：每条待处理事项的第二个按钮。
 *
 * 第一个按钮永远是那条事项确定性的出口（去处理 / 删除 / 认领）；这个按钮
 * 负责"我不知道该怎么办"的情况——后端按事项类型组装诊断工单（发生了什么、
 * 现场自检判定了什么、系统已经自动做过什么、与界面一致的动作清单），起一个
 * Agent 会话并跳过去。工单内容不在前端拼：Agent 能看到的证据必须比用户多，
 * 否则它只会得出和用户一样的错误结论。
 *
 * 未配置模型时整个不渲染（与 Job 卡片「交给 Agent」同口径）。
 */

import { useState } from "react";
import type { Route } from "next";
import { useRouter } from "next/navigation";

import { useToast } from "@/components/feedback";
import { useLlmCapability } from "@/components/llm-gate";
import { useAgentConversations } from "@/lib/agent-conversations";
import { fetchHandoffPrompt, type HandoffKind } from "@/lib/api/handoff";

export function HandoffButton({
  kind,
  refId,
  onBeforeNavigate,
  className = "",
}: {
  kind: HandoffKind;
  /** notice 为告警 id，download 为 info_hash，job 为任务 id */
  refId: string;
  /** 跳转前的收尾（关弹窗等） */
  onBeforeNavigate?: () => void;
  className?: string;
}) {
  const router = useRouter();
  const toast = useToast();
  const { start } = useAgentConversations();
  const { state: llmState } = useLlmCapability();
  const [busy, setBusy] = useState(false);
  if (llmState !== "configured" && llmState !== "unavailable") return null;

  async function handoff() {
    setBusy(true);
    try {
      const { prompt } = await fetchHandoffPrompt(kind, refId);
      const sessionId = await start(prompt);
      onBeforeNavigate?.();
      router.push(`/sessions/${sessionId}` as Route);
    } catch (caught) {
      toast.error((caught as Error).message || "无法发起 AI 分析");
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      onClick={handoff}
      disabled={busy}
      className={`btn-glass gap-1 px-3 py-1.5 text-sub font-medium text-[var(--text)] disabled:cursor-wait disabled:opacity-55 max-md:py-2 ${className}`}
    >
      {busy ? "正在整理上下文…" : "交给 AI 分析"}
    </button>
  );
}
