"use client";

/**
 * 「AI 设定」设置分区：什么场景用哪个模型。
 *
 * 与「模型接入」分开：接入回答「怎么连上」（实例、Key、端点），这里回答
 * 「智能体 / 字幕处理默认用哪个模型」。选项是所有已接入实例的模型清单
 * （GET /llm/models，同 id 跨实例冲突的项已带「（实例名）」标注）。
 * 服务端保证只要接入了供应商，两个默认就已有值（首次接入自动设为其目录
 * 第一个模型），所以这里显示的永远是真实存的值；一个都没接入时给空态引导。
 */

import { useEffect, useState } from "react";

import Link from "next/link";

import { useToast } from "@/components/feedback";
import { SparkIcon } from "@/components/icons";
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
      // 另一项若已失效（不在清单里）不能原样回传——服务端会整体拒绝；传 null 让它按推荐补齐
      const sibling = (ref: string | null) =>
        ref != null && options.some((o) => o.ref === ref) ? ref : null;
      const next = await updateLlmDefaults({
        agent_model: key === "agent_model" ? value : sibling(defaults.agent_model),
        subtitle_model: key === "subtitle_model" ? value : sibling(defaults.subtitle_model),
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
  // 已加载且一个供应商都没接入：清单为空即空态（与门禁同口径，不另发请求）
  const noProviders = defaults != null && options.length === 0;

  return (
    <div className="space-y-5">
      {!noProviders && (
        <p className="text-sub leading-6 text-[var(--text-muted)]">
          为不同场景各选一个默认模型。可选项来自
          <Link href="/settings/llm" className="mx-0.5 text-[var(--accent)] hover:underline">
            模型接入
          </Link>
          里所有已接入供应商的模型目录；首次接入时已自动设为该供应商目录里的第一个模型，可随时更改。
        </p>
      )}

      {error && (
        <div
          role="alert"
          className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]"
        >
          {error}
        </div>
      )}

      {defaults == null ? (
        <div className="h-[104px] animate-pulse rounded-xl bg-white/[0.04]" />
      ) : noProviders ? (
        /* 空态：没有供应商就没有模型可选，引导先去接入 */
        <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-12 text-center max-sm:px-4 max-sm:py-9">
          <span className="icon-chip size-12 !rounded-2xl">
            <SparkIcon className="size-6" />
          </span>
          <div>
            <p className="text-body font-medium text-[var(--text)]">还没有可选的模型</p>
            <p className="mt-1 max-w-md text-sub leading-relaxed text-[var(--text-muted)]">
              先在「模型接入」接入至少一家供应商。接入后这里会自动把智能体和字幕处理的默认模型
              设为该供应商目录里的第一个模型，你可以随时改成别的。
            </p>
          </div>
          <Link
            href="/settings/llm"
            className="btn-accent mt-1 min-h-10 rounded-full px-5 py-2 text-sub font-semibold max-sm:w-full"
          >
            去接入模型供应商
          </Link>
        </div>
      ) : (
        <div className="css-glass !rounded-2xl">
          {PURPOSES.map((purpose, i) => {
            const value = defaults[purpose.key];
            const effective = defaults[purpose.effectiveKey];
            // 正常情况下设定一定有值且在清单里（服务端维护）；只有预设目录变动
            // 这类漂移才会失效，此时提示并展示实际生效的兜底值
            const stale = value == null || !options.some((o) => o.ref === value);
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
                  value={stale ? "" : value}
                  disabled={saving != null}
                  onChange={(e) => void save(purpose.key, e.target.value || null)}
                  className="min-h-11 w-full appearance-none rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-[16px] text-[var(--text)] outline-none transition-colors focus:border-[var(--accent)]/60 disabled:opacity-50 sm:text-ui"
                >
                  {stale && (
                    <option value="" disabled>
                      请重新选择…
                    </option>
                  )}
                  {options.map((o) => (
                    <option key={o.ref} value={o.ref}>
                      {o.label}
                      {o.thinking_levels.length > 0 ? "（思考档位可控）" : ""}
                    </option>
                  ))}
                </select>
                {stale && (
                  <p className="text-caption leading-relaxed text-[var(--warn)]">
                    原设定的「{value ?? "（空）"}」已不在模型清单里，当前自动使用
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
