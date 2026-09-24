"use client";

import { useEffect, useState } from "react";

import type { Route } from "next";
import { useRouter } from "next/navigation";

import { Composer } from "@/components/composer";
import { LlmSetupNotice, useLlmConfigured } from "@/components/llm-gate";
import type { ComposerImage } from "@/lib/agent-attachments";
import { useAgentConversations } from "@/lib/agent-conversations";
import {
  type ComposerPrefs,
  loadComposerPrefs,
  reconcileComposerPrefs,
  saveComposerPrefs,
} from "@/lib/composer-prefs";
import { resolveModelOption, useLlmModelOptions } from "@/lib/llm-thinking";
import { usePageChrome } from "@/lib/page-chrome";
import { useTheme } from "@/lib/ui-prefs";
import { useIsMobile } from "@/lib/use-media-query";

/* —— 新任务（路由 / 与 /new）：仅一个居中输入框，大图氛围页直出。
     发起任务 = 创建会话并立即跳转到会话页（/sessions/[id]），流式过程在会话页渲染。

     银玻璃手机上（/new）换一副版式：一张「还没有消息的会话页」——顶栏标题
     「新会话」+ 返回键、正文空着、输入条钉在底部，与 /sessions/[id] 完全同构
     （2026-09-24 用户拍板，取代原先从底部升起的撰写面板：从「更多」面板点新会话
     得先收一张 sheet 再开一张，不如进一页再返回来得顺）。发出第一条消息后用
     replace 跳到会话页，浏览器后退不会再回到这张空页。 —— */
/** ``flat``：撰写台用纯色内嵌卡片而不是 WebGL 液态玻璃（会话页形态下恒为 flat：
 *  沉浸页是纯色底，WebGL 玻璃只会折射传给它的那张静态壁纸，与周围对不上）。 */
export function NewTask({ flat = false }: { flat?: boolean } = {}) {
  const router = useRouter();
  const { start } = useAgentConversations();
  const [input, setInput] = useState("");
  // 会话页形态：银玻璃手机专用；Netflix 手机与桌面仍是居中输入台
  const isMobile = useIsMobile();
  const theme = useTheme();
  const chatPage = isMobile && !theme.structural;
  // 顶栏标题「新会话」+ 返回键，返回落点与会话页一致（/my：手机上会话列表在「更多」）
  const chrome = usePageChrome();
  useEffect(() => {
    if (!chrome || !chatPage) return;
    return chrome.setTopBarTitle("新会话", { backHref: "/my" as Route });
  }, [chrome, chatPage]);
  // 新会话没有可沿用的历史：以本浏览器记住的上次选择为起点（null 即「默认」），
  // 用户一改就记下、并显式随消息提交。首帧按默认渲染、挂载后再读记忆，避免
  // 服务端渲染与浏览器首帧不一致
  const [choice, setChoice] = useState<ComposerPrefs>({ model: null, thinking: null });
  useEffect(() => {
    setChoice(loadComposerPrefs());
  }, []);
  const modelOptions = useLlmModelOptions();
  // 清单回来后校验记忆：模型被删了 / 档位不在菜单里的记忆直接丢弃，
  // 不能把一个服务端不认的引用提交上去
  useEffect(() => {
    setChoice((current) => reconcileComposerPrefs(current, modelOptions));
  }, [modelOptions]);
  const update = (next: ComposerPrefs) => {
    setChoice(next);
    saveComposerPrefs(next);
  };
  // 档位菜单随所选模型变化（未选即全局默认模型的菜单）
  const thinkingLevels = resolveModelOption(modelOptions, choice.model)?.thinking_levels ?? [];
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
      choice.thinking ?? undefined,
      choice.model ?? undefined,
    )
      .then((id) => {
        const target = `/sessions/${id}` as Route;
        // 会话页形态：这张空页是会话的「前一帧」，不该留在历史里
        if (chatPage) router.replace(target);
        else router.push(target);
      })
      .catch((e) => {
        setError((e as Error).message);
        setCreating(false);
      });
  }

  const composer = (
    <>
      <Composer
        flat={flat || chatPage}
        autoFocus={!locked}
        value={input}
        onChange={setInput}
        onSubmit={submit}
        imageUpload
        skillPicker
        modelOptions={modelOptions}
        modelValue={choice.model}
        // 换模型后旧档位可能不在新菜单里，清回默认
        onModelChange={(ref) => update({ model: ref, thinking: null })}
        thinkingLevels={thinkingLevels}
        thinkingValue={choice.thinking}
        onThinkingChange={(level) => update({ model: choice.model, thinking: level })}
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
    </>
  );

  if (chatPage) {
    return (
      // 与 agent-conversation-view.tsx 的外层/底部输入区同一套类名：沉浸配色、
      // 贴底输入行自己让出 Home 指示条的高度
      <div className="immersive-theme flex h-full flex-col">
        <div className="min-h-0 flex-1" />
        <div className="shrink-0 px-3 pb-[calc(0.75rem+var(--safe-bottom))] pt-2">
          <div className="mx-auto max-w-3xl">{composer}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="scroll-thin flex-1 overflow-y-auto">
        <div className="mx-auto flex min-h-full max-w-2xl flex-col justify-center px-6 py-12 max-md:px-4 max-md:py-8">
          {composer}
        </div>
      </div>
    </div>
  );
}
