"use client";

import { useCallback, useEffect, useState } from "react";

import { useConfirm } from "@/components/feedback";
import { SparkIcon } from "@/components/icons";
import { useLlmCapability } from "@/components/llm-gate";
import {
  type LlmModelInfo,
  type LlmPreset,
  type LlmProviderConfig,
  type LlmProviderPayload,
  type LlmProviderStatus,
  type LlmProviderType,
  deleteLlmProvider,
  getLlmProvider,
  listLlmPresets,
  reverifyLlmProvider,
  saveLlmProvider,
} from "@/lib/api/llm";
import { invalidateThinkingMenuCache } from "@/lib/llm-thinking";
import { formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/** 连接状态 → 展示文案与颜色（与站点/下载器配置同语言） */
const STATUS_META: Record<LlmProviderStatus, { label: string; color: string }> = {
  active: { label: "已连接", color: "var(--ok)" },
  verifying: { label: "测试中", color: "var(--info)" },
  pending: { label: "待测试", color: "#c0c4cc" },
  failed: { label: "连接失败", color: "var(--danger)" },
};

/** 需要轮询测试进度的中间态 */
const IN_PROGRESS: LlmProviderStatus[] = ["pending", "verifying"];

/**
 * 「AI 模型」设置分区。
 *
 * 与下载器分区的差异：LLM 供应商是**单例配置**——只需要接入一个就够用，
 * 没有多实例列表。未配置时是空态引导，已配置时是一张状态卡片；
 * 保存后后端用所选模型发一次最小对话验证，前端对中间态轮询刷新。
 */
export function LlmConfigSection() {
  const confirm = useConfirm();
  const { refresh: refreshLlmCapability } = useLlmCapability();
  const [config, setConfig] = useState<LlmProviderConfig | null>(null);
  const [presets, setPresets] = useState<LlmPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 是否展开配置表单（新增与编辑共用）
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setConfig(await getLlmProvider());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // 预设目录是静态数据，进分区拉一次即可
    listLlmPresets()
      .then(setPresets)
      .catch((e) => setError((e as Error).message));
  }, [load]);

  // 处于 pending/verifying 时轮询刷新，直到落定（页面隐藏时暂停）
  const inProgress = config != null && IN_PROGRESS.includes(config.status);
  useVisiblePolling(
    () => {
      void getLlmProvider()
        .then(setConfig)
        .catch(() => {
          /* 轮询失败静默重试，不打断页面 */
        });
    },
    inProgress ? 2000 : null,
  );

  async function guard(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const preset = presets.find((p) => p.id === config?.provider_type);

  return (
    <div className="space-y-5">
      {error && (
        <div
          role="alert"
          className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]"
        >
          {error}
        </div>
      )}

      {!editing && (
        <p className="text-sub leading-relaxed text-[var(--text-muted)]">
          {loading
            ? "加载中…"
            : config == null
              ? "接入一个大语言模型供应商，AI 能力（智能搜索、内容识别等）将由它驱动。"
              : "系统会在保存后发送一条测试消息，确认当前模型可以正常使用。"}
        </p>
      )}

      {loading ? (
        <div className="h-[104px] animate-pulse rounded-xl bg-white/[0.04]" />
      ) : editing ? (
        /* 编辑态只保留表单，避免状态卡在小屏重复占据首屏 */
        <div className="css-glass !rounded-2xl p-5 max-sm:p-4">
          <div className="mb-6">
            <p className="text-body font-semibold text-[var(--text)]">
              {config ? "编辑模型接入" : "接入模型供应商"}
            </p>
            <p className="mt-1 text-sub leading-relaxed text-[var(--text-muted)]">
              保存后系统会自动测试连接。
            </p>
          </div>
          <LlmProviderForm
            config={config}
            presets={presets}
            onSubmit={async (payload) => {
              setConfig(await saveLlmProvider(payload));
              // 默认模型/目录可能已变，让会话输入框的思考档位菜单重新拉取
              invalidateThinkingMenuCache();
              setEditing(false);
              refreshLlmCapability();
            }}
            onCancel={() => setEditing(false)}
            onError={setError}
          />
        </div>
      ) : config == null && !editing ? (
        /* 空态：未配置 */
        <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-12 text-center max-sm:px-4 max-sm:py-9">
          <span className="icon-chip size-12 !rounded-2xl">
            <SparkIcon className="size-6" />
          </span>
          <div>
            <p className="text-body font-medium text-[var(--text)]">还没有接入模型供应商</p>
            <p className="mt-1 text-sub text-[var(--text-muted)]">
              支持 OpenAI、阿里云百炼，以及任何 OpenAI 兼容端点（如自建 vLLM / Ollama）。
            </p>
          </div>
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="btn-accent mt-1 min-h-10 rounded-full px-5 py-2 text-sub font-semibold max-sm:w-full"
          >
            接入模型供应商
          </button>
        </div>
      ) : (
        config != null && (
          /* 已配置：单张状态卡片 */
          <div className="css-glass overflow-hidden !rounded-2xl">
            <div className="p-4 max-sm:p-3.5">
              <div className="flex items-start gap-3.5">
                <span className="icon-chip size-10 !rounded-xl">
                  <SparkIcon className="size-5" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-body font-semibold text-[var(--text)]">
                      {preset?.display_name ?? config.provider_type}
                    </p>
                    <StatusPill status={config.status} />
                  </div>
                  <p
                    className={`mt-1 text-caption leading-relaxed ${
                      config.status === "failed"
                        ? "break-words text-[#ff9b9b]"
                        : "text-[var(--text-faint)]"
                    }`}
                  >
                    {config.status === "failed" && config.last_error
                      ? config.last_error
                      : [
                          config.default_model,
                          `上次检查 ${formatRelativeTime(config.last_checked_at)}`,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                  </p>
                </div>
              </div>
            </div>

            {/* 连接信息带：端点与默认模型，一眼可核对 */}
            <div className="grid grid-cols-2 gap-x-7 gap-y-3 border-t border-white/[0.06] px-4 py-3 max-sm:grid-cols-1 max-sm:px-3.5">
              <InfoStat
                label="API 端点"
                value={config.base_url ?? preset?.base_url ?? "官方默认"}
              />
              <InfoStat label="默认模型" value={config.default_model} />
              {/* 仅在用户覆盖过 UA 时展示——没配的用户不需要知道有这回事 */}
              {config.user_agent && <InfoStat label="User-Agent" value={config.user_agent} />}
            </div>

            {/* 操作独占一行，避免窄屏时与名称、状态互相挤压 */}
            <div className="grid grid-cols-3 gap-2 border-t border-white/[0.06] p-3 max-[360px]:grid-cols-1">
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="btn-glass min-h-10 px-3 py-2 text-sub font-medium"
              >
                编辑配置
              </button>
              <button
                type="button"
                disabled={busy || IN_PROGRESS.includes(config.status)}
                onClick={() => void guard(async () => setConfig(await reverifyLlmProvider()))}
                className="btn-glass min-h-10 px-3 py-2 text-sub font-medium disabled:opacity-40"
              >
                重新测试
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void guard(async () => {
                    if (
                      !(await confirm({
                        title: "删除模型供应商配置？",
                        description: "AI 助手与微信通道将无法继续对话，可随时重新接入。",
                        confirmLabel: "删除",
                        tone: "danger",
                      }))
                    ) {
                      return;
                    }
                    await deleteLlmProvider();
                    setConfig(null);
                    setEditing(false);
                    refreshLlmCapability();
                  })
                }
                className="btn-glass min-h-10 px-3 py-2 text-sub font-medium !text-[#ff7d7d] disabled:opacity-40"
              >
                删除配置
              </button>
            </div>
          </div>
        )
      )}
    </div>
  );
}

function StatusPill({ status }: { status: LlmProviderStatus }) {
  const meta = STATUS_META[status];
  return (
    <span
      className="flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-caption font-medium"
      style={{ background: `color-mix(in oklab, ${meta.color} 12%, transparent)`, color: meta.color }}
    >
      <span className="size-1.5 rounded-full" style={{ background: meta.color }} />
      {meta.label}
    </span>
  );
}

function InfoStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-micro text-[var(--text-faint)]">{label}</p>
      <p
        className="mt-0.5 break-all text-ui font-semibold leading-relaxed text-[var(--text)]"
        title={value}
      >
        {value}
      </p>
    </div>
  );
}

/* —— 配置表单：类型 + 端点 + Key + 模型，新增与编辑共用 —— */

interface LlmProviderFormProps {
  /** 当前配置；null 表示首次接入 */
  config: LlmProviderConfig | null;
  presets: LlmPreset[];
  onSubmit: (payload: LlmProviderPayload) => Promise<void>;
  onCancel: () => void;
  onError: (message: string) => void;
}

/** 下拉框里「新增模型」选项的哨兵值（不会与真实模型 id 冲突）。 */
const NEW_MODEL = "__new__";

/** 禁用浏览器自动填充/纠错的属性组，本分区所有输入框统一挂上。

    data-* 是各家密码管理器（1Password / LastPass / Bitwarden）的忽略标记；
    API Key 框另有专门处理：用 text + CSS 圆点遮罩代替 type=password，
    从根上避免浏览器把它识别成登录密码框（保存/填充弹窗的来源）。 */
const NO_AUTOFILL = {
  autoComplete: "off",
  autoCorrect: "off",
  autoCapitalize: "off",
  spellCheck: false,
  "data-1p-ignore": "true",
  "data-lpignore": "true",
  "data-bwignore": "true",
  "data-form-type": "other",
} as const;

/** 新增模型参数子表单的输入状态（数字字段以字符串暂存，提交时转换）。 */
interface NewModelDraft {
  id: string;
  contextWindow: string;
  maxInput: string;
  maxOutput: string;
  thinkingBudget: string;
  supportsTools: boolean;
  parallel: boolean;
  thinking: boolean;
  vision: boolean;
  video: boolean;
  /** 思考控制方言：none = 不可控（只展示思考内容，即不声明 thinking_control） */
  thinkingKind: "none" | "effort" | "budget" | "toggle";
  /** effort 制的原生档位子集（多选） */
  thinkingLevels: string[];
  /** 是否有真关闭协议（菜单出现「关」）；toggle 制隐含为真 */
  supportsOff: boolean;
}

const EMPTY_DRAFT: NewModelDraft = {
  id: "",
  contextWindow: "",
  maxInput: "",
  maxOutput: "",
  thinkingBudget: "",
  supportsTools: true,
  parallel: false,
  thinking: false,
  vision: false,
  video: false,
  thinkingKind: "none",
  thinkingLevels: [],
  supportsOff: false,
};

/** effort 制可声明的档位（词汇表排除 off——关闭走「支持关闭」勾选）。 */
const DECLARABLE_THINKING_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"] as const;

const THINKING_LEVEL_LABEL: Record<string, string> = {
  minimal: "最少",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "超高",
  max: "最高",
};

function LlmProviderForm({ config, presets, onSubmit, onCancel, onError }: LlmProviderFormProps) {
  const [busy, setBusy] = useState(false);
  const [providerType, setProviderType] = useState<LlmProviderType>(
    config?.provider_type ?? "bailian",
  );
  const [baseUrl, setBaseUrl] = useState(config?.base_url ?? "");
  const [userAgent, setUserAgent] = useState(config?.user_agent ?? "");
  const [advancedOpen, setAdvancedOpen] = useState(Boolean(config?.user_agent));
  // 出于安全后端不回传 Key，编辑时需重新填写
  const [apiKey, setApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [model, setModel] = useState(config?.default_model ?? "");
  // 本地已保存的自定义模型目录（随配置持久化）；保存时整体回传，避免丢失
  const [extraModels] = useState<LlmModelInfo[]>(config?.extra_models ?? []);
  const [draft, setDraft] = useState<NewModelDraft>(EMPTY_DRAFT);

  const preset = presets.find((p) => p.id === providerType);
  // 严格规则：有内置目录的供应商（官方渠道）只能从目录选模型，本地自定义
  // 模型与「新增模型」均不出现——官方渠道的模型集合以目录为准（后端同样拦）。
  // 自定义模型只属于无目录的兼容端点类；切换供应商时不删除，切回即恢复。
  const catalog = preset?.models ?? [];
  const isCustomEndpoint = catalog.length === 0;
  const extras = isCustomEndpoint ? extraModels : [];
  // 无内置目录的端点（openai_compat）：把其它预设目录的模型也纳入可选——
  // 换端点（代理/自建网关）时模型名与参数往往一致，没必要让用户重填一遍。
  // 选中这类「借用」模型保存时，参数会复制进本配置的自定义目录（见 submit）。
  const borrowedGroups = isCustomEndpoint
    ? presets
        .filter((p) => p.id !== providerType && p.models.length > 0)
        .map((p) => ({
          label: p.display_name,
          models: p.models.filter((m) => !extras.some((e) => e.id === m.id)),
        }))
        .filter((g) => g.models.length > 0)
    : [];
  const borrowedModels = borrowedGroups.flatMap((g) => g.models);
  const options = [...catalog, ...extras, ...borrowedModels];
  const selected = options.find((m) => m.id === model);
  const isNew = model === NEW_MODEL;

  const needBaseUrl = preset?.requires_base_url ?? false;
  // 端点没有预设默认值 = 用户自定义接入点（OpenAI 可配镜像 / 通用兼容端点），
  // 只有这类才需要端点与 User-Agent 输入；官方固定渠道两者都不展示
  const canCustomizeEndpoint = !preset?.base_url;
  // 新增模型的必填校验：id / 上下文 / 最大输出；开思考则思考预算也必填；
  // 档位直传制至少声明一档（空声明 = 没有菜单，等于没声明）
  const draftValid =
    draft.id.trim().length > 0 &&
    Number(draft.contextWindow) > 0 &&
    Number(draft.maxOutput) > 0 &&
    (!draft.thinking || Number(draft.thinkingBudget) > 0) &&
    (draft.thinkingKind !== "effort" || draft.thinkingLevels.length > 0);
  const baseUrlValid =
    baseUrl.trim() === "" ? !needBaseUrl : /^https?:\/\/.+/.test(baseUrl.trim());
  // 请求头值只能是可打印 ASCII（与后端校验同一判据），空即用 SDK 默认
  const userAgentValid = userAgent.trim() === "" || /^[\x20-\x7e]+$/.test(userAgent.trim());
  const submitHint =
    apiKey.trim().length === 0
      ? "请填写 API Key。"
      : !baseUrlValid
        ? "请填写以 http:// 或 https:// 开头的完整 API 端点。"
        : isNew && !draftValid
          ? draft.thinkingKind === "effort" && draft.thinkingLevels.length === 0
            ? "档位直传模式至少要勾选一个可选档位。"
            : "请补全新模型参数中标有 * 的项目。"
          : !isNew && model.trim().length === 0
            ? "请选择默认模型。"
            : !userAgentValid
              ? "User-Agent 包含不支持的字符，请检查高级设置。"
              : null;
  const canSubmit = submitHint == null;

  function submit() {
    let defaultModel = model.trim();
    let nextExtras = extraModels;
    if (isNew) {
      const custom: LlmModelInfo = {
        id: draft.id.trim(),
        context_window: Number(draft.contextWindow),
        max_input_tokens: draft.maxInput ? Number(draft.maxInput) : null,
        max_output_tokens: Number(draft.maxOutput),
        supports_tools: draft.supportsTools,
        supports_parallel_tool_calls: draft.parallel,
        supports_thinking: draft.thinking,
        max_thinking_tokens: draft.thinking ? Number(draft.thinkingBudget) : null,
        thinking_control:
          !draft.thinking || draft.thinkingKind === "none"
            ? null
            : {
                kind: draft.thinkingKind,
                levels: draft.thinkingKind === "effort" ? draft.thinkingLevels : [],
                // toggle 的意义就是可关，隐含 supports_off；其余按勾选
                supports_off: draft.thinkingKind === "toggle" ? true : draft.supportsOff,
              },
        modalities: [
          "text",
          ...(draft.vision ? ["image"] : []),
          ...(draft.video ? ["video"] : []),
        ],
      };
      defaultModel = custom.id;
      // 同 id 覆盖旧条目：改参数重存是常见操作
      nextExtras = [...extraModels.filter((m) => m.id !== custom.id), custom];
    } else {
      // 选中的是其它预设目录「借用」来的模型：把参数复制进本配置的自定义目录，
      // 使配置自洽（后端校验默认模型必须能在预设目录或 extra_models 里找到）
      const borrowed = borrowedModels.find((m) => m.id === defaultModel);
      if (borrowed && !extraModels.some((m) => m.id === borrowed.id)) {
        nextExtras = [...extraModels, borrowed];
      }
    }
    setBusy(true);
    void onSubmit({
      provider_type: providerType,
      base_url: baseUrl.trim() || null,
      // 端点固定的官方渠道不开放 UA 配置，一律回传 null，避免切换供应商后
      // 残留上一个自定义端点的 UA
      user_agent: (canCustomizeEndpoint && userAgent.trim()) || null,
      api_key: apiKey.trim(),
      default_model: defaultModel,
      extra_models: nextExtras,
    })
      .catch((e) => onError((e as Error).message))
      .finally(() => setBusy(false));
  }

  const inputClass =
    "min-h-11 w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 " +
    "text-[16px] text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] " +
    "focus:border-[var(--accent)]/60 sm:text-ui";
  const labelClass = "mb-1.5 block text-sub font-medium text-[var(--text-muted)]";

  return (
    <div className="space-y-6">
      {/* 供应商会持续增加，使用原生下拉框控制首屏长度；移动端同时获得系统选择器。 */}
      <div className="min-w-0 space-y-3">
        <label htmlFor="llm-provider" className={labelClass}>
          供应商
        </label>
        <select
          id="llm-provider"
          value={providerType}
          disabled={presets.length === 0}
          aria-describedby={preset ? "llm-provider-hint" : undefined}
          aria-busy={presets.length === 0}
          onChange={(event) => {
            const nextProvider = event.target.value as LlmProviderType;
            if (providerType === nextProvider) return;
            setProviderType(nextProvider);
            setBaseUrl("");
            setUserAgent("");
            setAdvancedOpen(false);
            setModel("");
          }}
          className={`${inputClass} cursor-pointer disabled:cursor-wait disabled:opacity-50`}
        >
          {presets.length === 0 && <option value={providerType}>正在加载供应商…</option>}
          {presets.map((item) => (
            <option key={item.id} value={item.id}>
              {item.display_name}
            </option>
          ))}
        </select>
        {preset && (
          <div
            id="llm-provider-hint"
            className="rounded-xl border border-white/[0.06] bg-white/[0.025] px-3.5 py-3"
          >
            <p className="text-ui font-semibold text-[var(--text)]">{preset.display_name}</p>
            <p className="mt-0.5 text-caption leading-relaxed text-[var(--text-faint)]">
              {providerHint(preset)}
            </p>
          </div>
        )}
      </div>

      <div className="min-w-0 space-y-4 border-t border-white/[0.07] pt-6">
        {/* 端点固定的官方渠道（百炼/DeepSeek/Kimi/GLM）不展示端点输入；
            OpenAI 保留可选输入（代理/镜像场景），通用兼容端点必填。 */}
        {canCustomizeEndpoint && (
          <div>
            <label className={labelClass}>
              API 端点{needBaseUrl ? " *" : "（可选）"}
            </label>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={needBaseUrl ? "http://192.168.1.5:8000/v1" : "官方默认端点"}
              className={inputClass}
              {...NO_AUTOFILL}
            />
            {!needBaseUrl && (
              <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
                留空使用官方端点；使用代理或镜像时填写完整地址。
              </p>
            )}
          </div>
        )}

        <div>
          <label className={labelClass}>API Key *</label>
          {/* text + CSS 圆点遮罩：视觉等同密码框，但浏览器不识别为密码，
              不会触发「保存密码 / 自动填充」弹窗。 */}
          <div className="relative">
            <input
              type="text"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={config ? "出于安全，请重新填写" : "sk-…"}
              className={`${inputClass} pr-16 ${
                showApiKey ? "" : "[-webkit-text-security:disc]"
              }`}
              {...NO_AUTOFILL}
            />
            <button
              type="button"
              onClick={() => setShowApiKey((value) => !value)}
              className="absolute inset-y-1 right-1 rounded-lg px-3 text-sub font-medium text-[var(--text-muted)] transition-colors hover:bg-white/[0.06] hover:text-[var(--text)]"
              aria-label={showApiKey ? "隐藏 API Key" : "显示 API Key"}
            >
              {showApiKey ? "隐藏" : "显示"}
            </button>
          </div>
          {config && (
            <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
              已保存的密钥不会回显，修改其他配置时也需要重新填写。
            </p>
          )}
        </div>

        {/* User-Agent 是排障用低频字段，默认折叠以缩短主流程。 */}
        {canCustomizeEndpoint && (
          <details
            className="group rounded-xl border border-white/[0.07] bg-white/[0.025]"
            open={advancedOpen}
            onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
          >
            <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3.5 py-2 text-sub font-medium text-[var(--text-muted)] [&::-webkit-details-marker]:hidden">
              高级设置
              <span className="text-caption font-normal text-[var(--text-faint)] group-open:hidden">
                User-Agent
              </span>
              <span className="hidden text-caption font-normal text-[var(--text-faint)] group-open:inline">
                收起
              </span>
            </summary>
            <div className="border-t border-white/[0.06] px-3.5 pb-3.5 pt-3">
              <label className={labelClass}>User-Agent（可选）</label>
              <input
                type="text"
                value={userAgent}
                onChange={(e) => setUserAgent(e.target.value)}
                // 占位符直接展示留空时实际发送的 SDK 自带 UA（后端按 SDK 版本现算）。
                placeholder={preset?.default_user_agent ?? "留空使用 SDK 默认标识"}
                className={inputClass}
                {...NO_AUTOFILL}
              />
              <p
                className={`mt-1.5 text-caption leading-relaxed ${
                  userAgentValid ? "text-[var(--text-faint)]" : "text-[#ff6b6b]"
                }`}
              >
                {userAgentValid
                  ? "仅当网关或 WAF 对请求标识有要求时填写，留空使用上方显示的 SDK 默认值。"
                  : "只能包含可打印的 ASCII 字符（不能含换行或中文）。"}
              </p>
            </div>
          </details>
        )}
      </div>

      <div className="min-w-0 space-y-4 border-t border-white/[0.07] pt-6">
        <div>
          <label className={labelClass}>默认模型 *</label>
          <select
            aria-label="默认模型"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className={`${inputClass} appearance-none`}
          >
            <option value="" disabled>
              {options.length > 0 ? "请选择模型…" : "暂无可选模型，请新增…"}
            </option>
            {/* 已保存的模型不在任何目录时保留为可选项，编辑旧配置不至于被清空 */}
            {model && !selected && !isNew && <option value={model}>{model}（当前配置）</option>}
            {catalog.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}
                {modelHints(m) ? `（${modelHints(m)}）` : ""}
              </option>
            ))}
            {extras.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}（自定义{modelHints(m) ? ` · ${modelHints(m)}` : ""}）
              </option>
            ))}
            {/* 其它预设目录的模型（仅无目录端点展示）：选中后参数随保存复用 */}
            {borrowedGroups.map((g) => (
              <optgroup key={g.label} label={`${g.label} 目录（同名模型参数复用）`}>
                {g.models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                    {modelHints(m) ? `（${modelHints(m)}）` : ""}
                  </option>
                ))}
              </optgroup>
            ))}
            {isCustomEndpoint && (
              <option value={NEW_MODEL}>＋ 新增模型（需填写参数）…</option>
            )}
          </select>
          {selected && !isNew && (
            <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
              {modelSpecs(selected) || "该模型的详细规格以官方文档为准。"}
            </p>
          )}
        </div>

        {/* 新增模型的参数子表单：这些参数是 agent 做上下文/思考预算决策的依据，必填 */}
        {isNew && (
          <div className="space-y-3.5 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5 max-sm:p-3">
            <p className="text-sub font-medium text-[var(--text-muted)]">
              新模型参数
              <span className="mt-1 block font-normal leading-relaxed text-[var(--text-faint)]">
                按端点实际部署的模型规格填写，保存后计入本地模型目录
              </span>
            </p>
            <div>
              <label className={labelClass}>模型 id *</label>
              <input
                type="text"
                value={draft.id}
                onChange={(e) => setDraft({ ...draft, id: e.target.value })}
                placeholder="如：my-vllm-model"
                className={inputClass}
                {...NO_AUTOFILL}
              />
            </div>
            <div className="grid grid-cols-3 gap-3 max-md:grid-cols-1">
              <div>
                <label className={labelClass}>上下文长度 *</label>
                <input
                  type="number"
                  min={1}
                  value={draft.contextWindow}
                  onChange={(e) => setDraft({ ...draft, contextWindow: e.target.value })}
                  placeholder="131072"
                  className={inputClass}
                  {...NO_AUTOFILL}
                />
              </div>
              <div>
                <label className={labelClass}>最大输出 *</label>
                <input
                  type="number"
                  min={1}
                  value={draft.maxOutput}
                  onChange={(e) => setDraft({ ...draft, maxOutput: e.target.value })}
                  placeholder="8192"
                  className={inputClass}
                  {...NO_AUTOFILL}
                />
              </div>
              <div>
                <label className={labelClass}>最大输入（可选）</label>
                <input
                  type="number"
                  min={1}
                  value={draft.maxInput}
                  onChange={(e) => setDraft({ ...draft, maxInput: e.target.value })}
                  placeholder="不单独限制可留空"
                  className={inputClass}
                  {...NO_AUTOFILL}
                />
              </div>
            </div>
            <div className="flex flex-wrap gap-x-5 gap-y-2">
              <CheckField
                label="支持工具调用"
                checked={draft.supportsTools}
                onChange={(v) =>
                  setDraft({ ...draft, supportsTools: v, parallel: v && draft.parallel })
                }
              />
              <CheckField
                label="支持并发工具调用"
                checked={draft.parallel}
                disabled={!draft.supportsTools}
                onChange={(v) => setDraft({ ...draft, parallel: v })}
              />
              <CheckField
                label="输出思考内容"
                checked={draft.thinking}
                onChange={(v) =>
                  setDraft({ ...draft, thinking: v, ...(v ? {} : { thinkingKind: "none" as const }) })
                }
              />
              <CheckField
                label="支持图片输入"
                checked={draft.vision}
                onChange={(v) => setDraft({ ...draft, vision: v })}
              />
              <CheckField
                label="支持视频输入"
                checked={draft.video}
                onChange={(v) => setDraft({ ...draft, video: v })}
              />
            </div>
            {draft.thinking && (
              <div>
                <label className={labelClass}>思考预算上限 *</label>
                <input
                  type="number"
                  min={1}
                  value={draft.thinkingBudget}
                  onChange={(e) => setDraft({ ...draft, thinkingBudget: e.target.value })}
                  placeholder="如：81920"
                  className={inputClass}
                  {...NO_AUTOFILL}
                />
                <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
                  思维链可用的最大 token 数（thinking_budget 上限），超配供应商会直接报错。
                </p>
              </div>
            )}
            {draft.thinking && (
              <div>
                <label className={labelClass}>思考强度控制</label>
                <select
                  value={draft.thinkingKind}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      thinkingKind: e.target.value as NewModelDraft["thinkingKind"],
                    })
                  }
                  className={inputClass}
                >
                  <option value="none">不可控（只展示思考内容）</option>
                  <option value="effort">档位直传（reasoning_effort）</option>
                  <option value="budget">预算分段（enable_thinking + thinking_budget）</option>
                  <option value="toggle">仅开关（thinking.type）</option>
                </select>
                <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
                  按端点方言选择；声明后会话输入框出现思考档位选择器，
                  档位只从声明的菜单里选（不做就近换算）。
                </p>
                {draft.thinkingKind === "effort" && (
                  <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
                    {DECLARABLE_THINKING_LEVELS.map((level) => (
                      <CheckField
                        key={level}
                        label={THINKING_LEVEL_LABEL[level]}
                        checked={draft.thinkingLevels.includes(level)}
                        onChange={(v) =>
                          setDraft({
                            ...draft,
                            // 固定词汇表顺序归一，声明不是有序集合
                            thinkingLevels: DECLARABLE_THINKING_LEVELS.filter(
                              (l) => (l === level ? v : draft.thinkingLevels.includes(l)),
                            ),
                          })
                        }
                      />
                    ))}
                  </div>
                )}
                {(draft.thinkingKind === "effort" || draft.thinkingKind === "budget") && (
                  <div className="mt-3">
                    <CheckField
                      label="支持关闭思考"
                      checked={draft.supportsOff}
                      onChange={(v) => setDraft({ ...draft, supportsOff: v })}
                    />
                    <p className="mt-1 text-caption leading-relaxed text-[var(--text-faint)]">
                      仅当端点有真正的关闭参数时勾选（如 enable_thinking=false）；
                      勾选后档位菜单多出「关」。
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {submitHint && (
        <p role="status" className="text-caption leading-relaxed text-[var(--text-faint)]">
          {submitHint}
        </p>
      )}

      <div className="sticky bottom-0 z-10 -mx-4 -mb-4 flex items-center gap-3 border-t border-white/[0.08] bg-[rgba(17,20,27,0.94)] px-4 py-3 backdrop-blur-xl sm:static sm:mx-0 sm:mb-0 sm:justify-end sm:border-0 sm:bg-transparent sm:p-0 sm:pt-1 sm:backdrop-blur-none">
        <button
          type="button"
          onClick={onCancel}
          className="btn-glass min-h-11 flex-1 px-3.5 py-2 text-ui font-medium sm:flex-none"
        >
          取消
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={busy || !canSubmit}
          className="btn-accent min-h-11 flex-[1.6] rounded-full px-4.5 py-2 text-ui font-semibold disabled:opacity-40 sm:flex-none"
        >
          {busy ? "保存中…" : "保存并测试连接"}
        </button>
      </div>
    </div>
  );
}

/** 供应商卡片的副标题：固定端点显示域名，其余说明接入方式。 */
function providerHint(preset: LlmPreset): string {
  if (preset.id === "bailian") return "聚合 Qwen / DeepSeek / Kimi / GLM";
  if (preset.id === "openai") return "官方端点，可配代理";
  if (preset.requires_base_url) return "自建 vLLM / Ollama / 任意网关";
  if (preset.base_url) {
    try {
      return new URL(preset.base_url).host;
    } catch {
      return preset.base_url;
    }
  }
  return "官方端点";
}

/** 参数子表单里的复选项：文字标签 + 原生 checkbox。 */
function CheckField({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label
      className={`flex cursor-pointer items-center gap-1.5 text-sub text-[var(--text-muted)] ${
        disabled ? "cursor-not-allowed opacity-40" : ""
      }`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="size-3.5 accent-[var(--accent)]"
      />
      {label}
    </label>
  );
}

/** token 数 → 简短可读。2 的幂按 1024 进制（65536 → 64K，262144 → 256K），
 * 其余按十进制（1050000 → 1.05M）。 */
function formatTokens(n: number): string {
  if (n % 1024 === 0 && n < 1_000_000) return `${n / 1024}K`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 2)}M`;
  return `${Math.round(n / 1000)}K`;
}

/** 下拉选项后缀的能力短标（思考 / 视觉 / 视频 / 档位可控）。 */
function modelHints(model: LlmModelInfo): string {
  return [
    model.supports_thinking ? "思考" : null,
    (model.thinking_levels?.length ?? 0) > 0 ? "档位可控" : null,
    model.modalities.includes("image") ? "视觉" : null,
    model.modalities.includes("video") ? "视频" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** 选中模型的规格说明：上下文 / 输出上限 / 思考预算 / 并发工具，未公布的字段不显示。 */
function modelSpecs(model: LlmModelInfo): string {
  return [
    model.context_window ? `上下文 ${formatTokens(model.context_window)}` : null,
    model.max_output_tokens ? `最大输出 ${formatTokens(model.max_output_tokens)}` : null,
    model.max_thinking_tokens ? `思考预算 ${formatTokens(model.max_thinking_tokens)}` : null,
    model.supports_parallel_tool_calls ? "支持并发工具调用" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
