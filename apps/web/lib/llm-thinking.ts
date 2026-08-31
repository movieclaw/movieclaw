"use client";

/**
 * 思维链档位的前端公共件：文案映射 + 默认模型菜单获取。
 *
 * 菜单由服务端按模型的 thinking_control 声明推导（LlmModelInfo.thinking_levels），
 * 前端不理解方言；空菜单 = 隐藏选择器（docs/design/agent-thinking-level.md）。
 */

import { useEffect, useState } from "react";

import { getLlmProvider, listLlmPresets, type LlmModelInfo } from "@/lib/api/llm";

/** 档位文案（对齐 maka 的短单词标签）；「默认」由选择器的空值表达。 */
export const THINKING_LEVEL_LABELS: Record<string, string> = {
  off: "关",
  minimal: "最少",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "超高",
  max: "最高",
};

/** 会话页与首页共享同一份默认模型菜单，模块级缓存避免每次挂载都请求。 */
let cachedMenu: Promise<string[]> | null = null;

async function fetchDefaultModelMenu(): Promise<string[]> {
  const [config, presets] = await Promise.all([getLlmProvider(), listLlmPresets()]);
  if (!config) return [];
  const presetCatalog: LlmModelInfo[] =
    presets.find((p) => p.id === config.provider_type)?.models ?? [];
  // 补录条目按 id 覆盖预设（与后端 _catalog 同口径），先查补录
  const model =
    config.extra_models.find((m) => m.id === config.default_model) ??
    presetCatalog.find((m) => m.id === config.default_model);
  return model?.thinking_levels ?? [];
}

/** 当前默认模型的思考档位菜单；加载中/未配置/出错一律 []（隐藏选择器）。 */
export function useDefaultModelThinkingLevels(): string[] {
  const [levels, setLevels] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    cachedMenu ??= fetchDefaultModelMenu().catch(() => {
      cachedMenu = null; // 失败不缓存，下次挂载重试
      return [];
    });
    void cachedMenu.then((menu) => {
      if (!cancelled) setLevels(menu);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return levels;
}

/** 设置页保存供应商配置后调用：默认模型可能已变，作废菜单缓存。 */
export function invalidateThinkingMenuCache(): void {
  cachedMenu = null;
}
