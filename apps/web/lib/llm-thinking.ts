"use client";

/**
 * 对话框「模型」与「思维链」两个选择器的前端公共件：
 * 模型清单获取（模块级缓存）+ 档位文案映射 + 按所选模型取档位菜单。
 *
 * 模型清单由服务端拍平（GET /llm/models）：同一模型 id 只在一个实例里有时
 * ref 与展示都是裸 id，出现在多个实例里时 ref 为「实例名/模型id」、展示加
 * 括号标注实例名——前端不做去重判断，只照单渲染。
 * 档位菜单随选项一起下发（thinking_levels），前端不理解方言；空菜单 =
 * 隐藏选择器（docs/design/agent-thinking-level.md）。
 */

import { useEffect, useState } from "react";

import { listLlmModels, type LlmModelOption } from "@/lib/api/llm";

/** 统一词汇表的强度顺序（越靠后越深）：滑杆按它排刻度，服务端下发的菜单也按它归一。 */
export const THINKING_LEVEL_ORDER = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

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

/** 会话页与首页共享同一份模型清单，模块级缓存避免每次挂载都请求。 */
let cachedOptions: Promise<LlmModelOption[]> | null = null;

/** 对话框可选的模型清单；加载中/未配置/出错一律 []（隐藏选择器）。 */
export function useLlmModelOptions(): LlmModelOption[] {
  const [options, setOptions] = useState<LlmModelOption[]>([]);
  useEffect(() => {
    let cancelled = false;
    cachedOptions ??= listLlmModels().catch(() => {
      cachedOptions = null; // 失败不缓存，下次挂载重试
      return [];
    });
    void cachedOptions.then((list) => {
      if (!cancelled) setOptions(list);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return options;
}

/** 选择器当前生效的选项：显式引用命中的项，否则全局默认项；清单为空时 undefined。 */
export function resolveModelOption(
  options: LlmModelOption[],
  ref: string | null | undefined,
): LlmModelOption | undefined {
  if (ref) {
    const hit = options.find((o) => o.ref === ref);
    if (hit) return hit;
  }
  return options.find((o) => o.is_default) ?? options[0];
}

/** 设置页增删改实例后调用：清单与默认模型可能已变，作废缓存。 */
export function invalidateModelOptionsCache(): void {
  cachedOptions = null;
}
