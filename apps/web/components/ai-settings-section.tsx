"use client";

/**
 * 「AI 设定」设置分区：什么场景用哪个模型。
 *
 * 与「模型接入」分开：接入回答「怎么连上」（实例、Key、端点），这里回答
 * 「智能体 / 字幕处理默认用哪个模型」。选项是所有已接入实例的模型清单
 * （GET /llm/models，同 id 跨实例冲突的项已带「（实例名）」标注）。
 * 未设置时服务端按第一个实例的连接测试模型兜底，这里把兜底结果展示出来，
 * 让用户知道"不设也能用、现在用的是谁"。
 */

import { useEffect, useState } from "react";

import Link from "next/link";

import { useToast } from "@/components/feedback";
import { LlmSetupNotice, useLlmConfigured } from "@/components/llm-gate";
import {
  type LlmDefaults,
  type LlmModelOption,
  getLlmDefaults,
  listLlmModels,
  updateLlmDefaults,
} from "@/lib/api/llm";
import { invalidateModelOptionsCache } from "@/lib/llm-thinking";

/** 一个用途一行：字段名 + 文案 */
const PURPOSES: {
  key: "agent_model" | "subtitle_model";
  effectiveKey: "effective_agent_model" | "effective_subtitle_model";
  label: string;
  desc: string;
}[] = [
  {
    key: "agent_model",
    effectiveKey: "effective_agent_model",
    label: "智能体默认模型",
    desc: "对话框未选模型时、微信 / Telegram / Discord 对话、命令行不带 --model 时使用",
  },
  {
    key: "subtitle_model",
    effectiveKey: "effective_subtitle_model",
    label: "字幕处理默认模型",
    desc: "字幕翻译与生成任务使用；任务创建时固定，改动不影响已开始的任务",
  },
];

export function AiSettingsSection() {
  const toast = useToast();
  const configured = useLlmConfigured();
  const [defaults, setDefaults] = useState<LlmDefaults | null>(null);
  const [options, setOptions] = useState<LlmModelOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getLlmDefaults(), listLlmModels()])
      .then(([d, o]) => {
        setDefaults(d);
        setOptions(o);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  async function save(key: "agent_model" | "subtitle_model", value: string | null) {
    if (defaults == null) return;
    const previous = defaults;
    setDefaults({ ...defaults, [key]: value }); // 乐观更新，失败回滚
    setSaving(key);
    setError(null);
    try {
      const next = await updateLlmDefaults({
        agent_model: key === "agent_model" ? value : defaults.agent_model,
        subtitle_model: key === "subtitle_model" ? value : defaults.subtitle_model,
      });
      setDefaults(next);
      // 对话框的模型清单里 is_default 跟随智能体默认模型，作废缓存
      invalidateModelOptionsCache();
      toast.success("AI 设定已保存");
    } catch (e) {
      setDefaults(previous);
      setError((e as Error).message);
    } finally {
      setSaving(null);
    }
  }

  const labelOf = (ref: string | null) => options.find((o) => o.ref === ref)?.label ?? ref;

  return (
    <div className="space-y-5">
      <p className="text-sub leading-6 text-[var(--text-muted)]">
        为不同场景各选一个默认模型。可选项来自
        <Link href="/settings/llm" className="mx-0.5 text-[var(--accent)] hover:underline">
          模型接入
        </Link>
        里所有已接入供应商的模型目录；未设置时自动使用最早接入的供应商。
      </p>

      {error && (
        <div
          role="alert"
          className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]"
        >
          {error}
        </div>
      )}

      {configured === false && <LlmSetupNotice feature="AI 设定" />}

      {defaults == null ? (
        <div className="h-[104px] animate-pulse rounded-xl bg-white/[0.04]" />
      ) : (
        <div className="css-glass !rounded-2xl">
          {PURPOSES.map((purpose, i) => {
            const value = defaults[purpose.key];
            const effective = defaults[purpose.effectiveKey];
            // 设定指向的模型已不在清单里（实例被删）：提示并展示兜底结果
            const stale = value != null && !options.some((o) => o.ref === value);
            return (
              <div
                key={purpose.key}
                className={`space-y-2 p-4 max-sm:p-3.5 ${i > 0 ? "border-t border-white/[0.06]" : ""}`}
              >
                <div>
                  <p className="text-body font-medium text-[var(--text)]">{purpose.label}</p>
                  <p className="mt-0.5 text-caption leading-relaxed text-[var(--text-faint)]">
                    {purpose.desc}
                  </p>
                </div>
                <select
                  aria-label={purpose.label}
                  value={stale ? "" : (value ?? "")}
                  disabled={saving != null || options.length === 0}
                  onChange={(e) => void save(purpose.key, e.target.value || null)}
                  className="min-h-11 w-full appearance-none rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-[16px] text-[var(--text)] outline-none transition-colors focus:border-[var(--accent)]/60 disabled:opacity-50 sm:text-ui"
                >
                  <option value="">
                    {effective
                      ? `未设置（自动使用 ${labelOf(effective)}）`
                      : "未设置（尚未接入任何供应商）"}
                  </option>
                  {options.map((o) => (
                    <option key={o.ref} value={o.ref}>
                      {o.label}
                      {o.thinking_levels.length > 0 ? "（思考档位可控）" : ""}
                    </option>
                  ))}
                </select>
                {stale && (
                  <p className="text-caption leading-relaxed text-[var(--warn)]">
                    原设定的「{value}」已不可用（供应商已删除），当前自动使用
                    {labelOf(effective) ?? "无"}；请重新选择。
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
