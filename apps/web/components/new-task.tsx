"use client";

import { useState } from "react";

import type { Route } from "next";
import { useRouter } from "next/navigation";

import { Composer } from "@/components/composer";
import { LlmSetupNotice, useLlmConfigured } from "@/components/llm-gate";
import type { ComposerImage } from "@/lib/agent-attachments";
import { useAgentConversations } from "@/lib/agent-conversations";
import { useDefaultModelThinkingLevels } from "@/lib/llm-thinking";

/* —— 新任务（路由 /）：仅一个居中输入框，大图氛围页直出。
     发起任务 = 创建会话并立即跳转到会话页（/sessions/[id]），流式过程在会话页渲染。 —— */
export function NewTask() {
  const router = useRouter();
  const { start } = useAgentConversations();
  const [input, setInput] = useState("");
  // 新会话没有可沿用的历史，null 即「默认」；用户切换后显式随消息提交
  const [thinkingChoice, setThinkingChoice] = useState<string | null>(null);
  const thinkingLevels = useDefaultModelThinkingLevels();
  // 创建会话需等服务端返回 session_id 才能跳转；等待期锁住输入框
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 未接入模型供应商时锁定输入框并引导去设置（false = 明确未配置）
  const llmConfigured = useLlmConfigured();
  const locked = llmConfigured === false;

  function submit(text: string, images: ComposerImage[]) {
    setCreating(true);
    setError(null);
    start(
      text,
      images.map((image) => ({
        attachmentId: image.attachmentId,
        name: image.name,
        previewUrl: image.previewUrl,
      })),
      thinkingChoice ?? undefined,
    )
      .then((id) => {
        router.push(`/sessions/${id}` as Route);
      })
      .catch((e) => {
        setError((e as Error).message);
        setCreating(false);
      });
  }

  return (
    <div className="flex h-full flex-col">
      <div className="scroll-thin flex-1 overflow-y-auto">
        <div className="mx-auto flex min-h-full max-w-2xl flex-col justify-center px-6 py-12 max-md:px-4 max-md:py-8">
          <Composer
            autoFocus={!locked}
            value={input}
            onChange={setInput}
            onSubmit={submit}
            imageUpload
            skillPicker
            thinkingLevels={thinkingLevels}
            thinkingValue={thinkingChoice}
            onThinkingChange={setThinkingChoice}
            busy={creating}
            disabled={locked}
            placeholder={locked ? "请先接入 AI 模型，再开始对话" : undefined}
          />
          {locked && <LlmSetupNotice />}
          {error && (
            <p className="notice-surface mt-3 rounded-xl border border-[#ff6b6b]/35 px-3.5 py-2.5 text-ui leading-5 text-[#ff6b6b]">
              创建会话失败：{error}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
