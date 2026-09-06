"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import Link from "next/link";

import { listLlmProviders } from "@/lib/api/llm";

/* —— 全局 LLM 能力门禁。
     判定与后端 acquire_llm_router 对齐——只看「是否至少接入了一个实例」
     （GET /llm/providers 非空），验证失败的实例后端仍会尝试使用，不在前端
     拦截。由 Provider 统一探测，页面里的多个 AI 入口不重复发请求。 —— */

export type LlmCapabilityState = "checking" | "configured" | "missing" | "unavailable";

interface LlmCapabilityContextValue {
  state: LlmCapabilityState;
  /** 设置页新增或删除供应商后调用，让全部 AI 入口同步刷新。 */
  refresh: () => void;
}

const LlmCapabilityContext = createContext<LlmCapabilityContextValue | null>(null);

/** 工作台级供应商探测器；只包在登录后的 (app) 路由组，避免未登录请求。 */
export function LlmCapabilityProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<LlmCapabilityState>("checking");
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    setState("checking");
    listLlmProviders()
      .then((providers) => {
        if (!cancelled) setState(providers.length === 0 ? "missing" : "configured");
      })
      .catch(() => {
        // 探测接口异常不应误锁所有 AI 功能，保留服务端提交时的错误兜底。
        if (!cancelled) setState("unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [revision]);

  const value = useMemo(() => ({ state, refresh }), [state, refresh]);
  return <LlmCapabilityContext.Provider value={value}>{children}</LlmCapabilityContext.Provider>;
}

/** 读取共享能力状态；AI 入口必须位于工作台的 Provider 内。 */
export function useLlmCapability(): LlmCapabilityContextValue {
  const value = useContext(LlmCapabilityContext);
  if (value === null) {
    throw new Error("useLlmCapability 必须在 LlmCapabilityProvider 内使用");
  }
  return value;
}

/**
 * 探测模型供应商是否已配置。
 *
 * 返回三态：null = 探测中（不锁定，避免闪烁）；true = 已配置；false = 未配置。
 * 探测失败（如后端未就绪）按 null 处理，交给提交时的服务端错误兜底。
 */
export function useLlmConfigured(): boolean | null {
  const { state } = useLlmCapability();
  if (state === "configured") return true;
  if (state === "missing") return false;
  return null;
}

interface LlmSetupNoticeProps {
  /** 用户完成配置后能获得什么，使用面向用户的短语，如“生成字幕能力”。 */
  feature?: string;
  variant?: "surface" | "inline";
}

/** 未配置模型时的公共引导：解释能解锁什么，并提供唯一的设置入口。 */
export function LlmSetupNotice({
  feature = "AI 对话能力",
  variant = "surface",
}: LlmSetupNoticeProps) {
  const className =
    variant === "inline"
      ? "inline-flex flex-wrap items-center gap-x-1 rounded-md border border-[var(--warn)]/30 bg-[var(--warn)]/10 px-2 py-0.5 text-caption leading-5 text-[var(--warn)]"
      : "notice-surface mt-3 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-ui leading-5 text-[var(--text-muted)]";
  return (
    <p role="status" className={className}>
      接入 AI 模型后即可解锁{feature}。
      <Link href="/settings/llm" className="mx-0.5 text-[var(--accent)] hover:underline">
        去接入
      </Link>
    </p>
  );
}

/**
 * 声明式 AI 能力门禁。
 *
 * 检查中不短暂露出触发按钮；明确未配置时用公共引导替换；探测接口异常时
 * fail-open，由后端预检继续兜底，避免一次状态请求故障锁死全部功能。
 */
export function LlmCapabilityGate({
  feature,
  noticeVariant = "surface",
  children,
}: {
  feature: string;
  noticeVariant?: LlmSetupNoticeProps["variant"];
  children: ReactNode;
}) {
  const { state } = useLlmCapability();
  if (state === "checking") return null;
  if (state === "missing") {
    return <LlmSetupNotice feature={feature} variant={noticeVariant} />;
  }
  return children;
}
