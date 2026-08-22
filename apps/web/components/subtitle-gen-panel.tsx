"use client";

/**
 * 字幕规格行里的 AI 翻译入口。
 *
 * 交互分成两个明确阶段：点击后先同步预检（不调用 LLM），在弹窗里讲清
 * 参考字幕、目标语言、输出文件与预计成本；用户确认后才启动后台任务。
 * 后台状态只读取全站 Job Provider；SSE 即时更新、低频轮询兜底，Web、CLI
 * 与 Agent 发起的任务在这里呈现完全一致。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { Route } from "next";
import { useRouter } from "next/navigation";

import { Modal } from "@/components/modal";
import { LlmCapabilityGate } from "@/components/llm-gate";
import { Tooltip } from "@/components/tooltip";
import { WandIcon } from "@/components/icons";
import { useAgentConversations } from "@/lib/agent-conversations";
import type { LibraryItemFile } from "@/lib/api/libraries";
import {
  generateSubtitles,
  previewSubtitleGeneration,
  type SubtitleGenerationPreview,
  type SubtitleSourceCandidate,
} from "@/lib/api/subtitle-gen";
import { cancelJob, type JobStatus, type JobView } from "@/lib/api/jobs";
import { usePermissions } from "@/lib/permissions";
import { useJobs } from "@/lib/jobs";

const OUTPUT_LANGUAGES = [
  { token: "chs", label: "简体中文" },
  { token: "cht", label: "繁体中文" },
  { token: "eng", label: "英语" },
  { token: "jpn", label: "日语" },
  { token: "kor", label: "韩语" },
  { token: "fre", label: "法语" },
  { token: "ger", label: "德语" },
  { token: "spa", label: "西班牙语" },
  { token: "ita", label: "意大利语" },
  { token: "por", label: "葡萄牙语" },
  { token: "rus", label: "俄语" },
  { token: "tha", label: "泰语" },
] as const;
type OutputLanguage = (typeof OUTPUT_LANGUAGES)[number]["token"];

function outputLanguageLabel(token: string | null | undefined): string {
  return OUTPUT_LANGUAGES.find((language) => language.token === token)?.label ?? token ?? "字幕";
}

function outputLabel(target: string, secondary: string | null): string {
  const primary = outputLanguageLabel(target);
  return secondary ? `${primary} + ${outputLanguageLabel(secondary)}双语` : primary;
}

function nextSecondary(target: string, current: string): OutputLanguage {
  if (target !== current) return current as OutputLanguage;
  return OUTPUT_LANGUAGES.find((language) => language.token !== target)?.token ?? "eng";
}

function isAiSubtitle(filename: string | null): boolean {
  if (!filename) return false;
  return filename
    .toLowerCase()
    .split(".")
    .some((part) => part === "ai" || part.startsWith("ai-"));
}

// 发现页 Banner 主按钮的紧凑变体：保留胶囊比例与连续文案，但不撑高字幕规格行。
const AI_BADGE_BASE =
  "tnum inline-flex h-8 shrink-0 items-center gap-1.5 rounded-full px-3.5 text-sub " +
  "font-semibold transition-[background-color,color,box-shadow] " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] " +
  "disabled:pointer-events-none disabled:opacity-50";
// 生成入口是真实操作按钮，使用主按钮银色与普通字幕类型标签拉开层级。
const AI_BADGE_SILVER = "btn-accent";
const AI_BADGE_RUNNING =
  "bg-[var(--info)]/[0.14] text-[var(--info)] hover:bg-[var(--info)]/[0.21] hover:shadow-[0_4px_12px_rgba(0,0,0,0.16)]";
const AI_BADGE_FAILED =
  "bg-[#ff9f9f]/[0.08] text-[#ffb4b4] hover:bg-[#ff9f9f]/[0.14] hover:shadow-[0_4px_12px_rgba(0,0,0,0.16)]";
const CANCEL_BUTTON =
  "rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-ui text-white/80 " +
  "transition hover:bg-white/[0.1] disabled:pointer-events-none disabled:opacity-40";
const CONFIRM_BUTTON =
  "rounded-lg bg-[#8fdcff] px-4 py-2 text-ui font-semibold text-[#071018] transition " +
  "hover:bg-[#b4e9ff] disabled:pointer-events-none disabled:opacity-40";
const AGENT_BUTTON =
  "rounded-lg border border-[var(--info)]/35 bg-[var(--info)]/[0.08] px-4 py-2 text-ui " +
  "font-medium text-[var(--info)] transition hover:bg-[var(--info)]/[0.14] " +
  "disabled:pointer-events-none disabled:opacity-40";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后再试";
}

function languageLabel(language: string | null): string {
  const labels: Record<string, string> = {
    chi: "中文",
    chs: "简体中文",
    cht: "繁体中文",
    eng: "英语",
    fre: "法语",
    fra: "法语",
    ger: "德语",
    ita: "意大利语",
    jpn: "日语",
    kor: "韩语",
    por: "葡萄牙语",
    rus: "俄语",
    spa: "西班牙语",
    tha: "泰语",
  };
  return language ? (labels[language] ?? language.toUpperCase()) : "未知语言";
}

function formatLabel(format: string | null): string {
  if (!format) return "未知格式";
  const labels: Record<string, string> = {
    hdmv_pgs_subtitle: "PGS",
    dvd_subtitle: "VobSub",
    subrip: "SRT",
    srt: "SRT",
    ass: "ASS",
    ssa: "SSA",
    webvtt: "VTT",
    vtt: "VTT",
  };
  return labels[format.toLowerCase()] ?? format.toUpperCase();
}

function candidateLabel(candidate: SubtitleSourceCandidate): string {
  const location = candidate.kind === "embedded" ? "内封" : "外挂";
  const identity =
    candidate.kind === "embedded"
      ? `轨道 ${Number(candidate.key) + 1}`
      : candidate.key;
  const conversion = candidate.requires_ocr ? " · 需先识别" : "";
  const provenanceLabels: Record<SubtitleSourceCandidate["provenance"], string> = {
    original: "原始字幕",
    pgs_ocr: "图片字幕识别结果",
    ai: "AI 字幕",
    ai_bilingual: "AI 双语成品",
  };
  return `${languageLabel(candidate.language)} · ${formatLabel(candidate.format)} · ${provenanceLabels[candidate.provenance]} · ${location} ${identity}${conversion}`;
}

function candidateKey(candidate: SubtitleSourceCandidate): string {
  return `${candidate.kind}:${candidate.key}`;
}

function tokenEstimate(tokens: number): string {
  if (tokens < 1000) return `约 ${tokens.toLocaleString()} token`;
  return `约 ${(tokens / 1000).toFixed(tokens < 10_000 ? 1 : 0)}k token`;
}

const PROGRESS_STAGES = [
  { phases: ["preparing", "ocr", "syncing"], label: "准备并检查字幕" },
  { phases: ["glossary"], label: "统一人名与术语" },
  { phases: ["translating"], label: "翻译对白" },
  { phases: ["validating", "compressing"], label: "检查字幕质量" },
  { phases: ["writing", "refreshing"], label: "保存并更新字幕" },
] as const;

interface SubtitleProgress {
  phase: string;
  message: string;
  percent: number | null;
  done_blocks: number;
  total_blocks: number;
  done_events: number;
  total_events: number;
  active_blocks: number[];
  parallelism: number;
  oldest_active_seconds: number;
  last_completed_seconds_ago: number | null;
  validation_retries: number;
  rate_limit_count: number;
  model_requests: number;
  model_tokens: number;
  uses_ocr: boolean;
  target_language: string | null;
  secondary_language: string | null;
  source_candidate_key: string | null;
  elapsed_seconds: number;
}

const RUNNING_JOB_STATUSES = new Set<JobStatus>([
  "queued",
  "running",
  "retry_wait",
  "cancelling",
  "waiting",
]);

function detailNumber(details: Record<string, unknown>, key: string): number {
  const value = details[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function detailString(details: Record<string, unknown>, key: string): string | null {
  const value = details[key];
  return typeof value === "string" && value ? value : null;
}

function subtitleProgress(job: JobView | null): SubtitleProgress | null {
  if (!job) return null;
  const details = job.progress.details;
  const activeBlocks = Array.isArray(details.active_blocks)
    ? details.active_blocks.filter((value): value is number => typeof value === "number")
    : [];
  const startedAt = job.started_at ? Date.parse(job.started_at) : Number.NaN;
  return {
    phase: job.progress.phase,
    message: job.progress.message,
    percent: job.progress.percent,
    done_blocks: detailNumber(details, "done_blocks"),
    total_blocks: detailNumber(details, "total_blocks"),
    done_events: detailNumber(details, "done_events"),
    total_events: detailNumber(details, "total_events"),
    active_blocks: activeBlocks,
    parallelism: detailNumber(details, "parallelism"),
    oldest_active_seconds: detailNumber(details, "oldest_active_seconds"),
    last_completed_seconds_ago:
      typeof details.last_completed_seconds_ago === "number"
        ? details.last_completed_seconds_ago
        : null,
    validation_retries: detailNumber(details, "validation_retries"),
    rate_limit_count: detailNumber(details, "rate_limit_count"),
    model_requests: detailNumber(job.usage, "request_count"),
    model_tokens: detailNumber(job.usage, "total_tokens"),
    uses_ocr: details.uses_ocr === true,
    target_language:
      detailString(details, "target_language") ??
      (typeof job.input_data.target_language === "string" ? job.input_data.target_language : null),
    secondary_language:
      detailString(details, "secondary_language") ??
      (typeof job.input_data.secondary_language === "string"
        ? job.input_data.secondary_language
        : null),
    source_candidate_key:
      detailString(details, "source_candidate_key") ??
      (typeof job.input_data.source_candidate_key === "string"
        ? job.input_data.source_candidate_key
        : null),
    elapsed_seconds: Number.isFinite(startedAt)
      ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
      : 0,
  };
}

function progressPercent(progress: SubtitleProgress | null): number | null {
  if (!progress || progress.percent === null) return null;
  return Math.min(100, Math.floor(progress.percent));
}

function progressStageIndex(progress: SubtitleProgress): number {
  const index = PROGRESS_STAGES.findIndex((stage) =>
    (stage.phases as readonly string[]).includes(progress.phase ?? "preparing"),
  );
  return index >= 0 ? index : 0;
}

function formatElapsed(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds || 0));
  if (safeSeconds < 60) return `${safeSeconds} 秒`;
  const minutes = Math.floor(safeSeconds / 60);
  if (minutes < 60) return `${minutes} 分 ${safeSeconds % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function sourceKeyLabel(key: string | null): string | null {
  if (!key) return null;
  if (key.startsWith("embedded:")) {
    const index = Number(key.slice("embedded:".length));
    return Number.isFinite(index) ? `内封轨道 ${index + 1}` : "内封字幕";
  }
  return key.startsWith("external:") ? key.slice("external:".length) : key;
}

function runningBadgeText(progress: SubtitleProgress | null): string {
  const phaseLabels: Record<string, string> = {
    preparing: "准备中",
    ocr: "识别中",
    syncing: "同步检查",
    glossary: "术语分析",
    validating: "质量检查",
    compressing: "质量优化",
    writing: "保存中",
    refreshing: "更新中",
  };
  if (progress?.phase === "translating") {
    const percent = progressPercent(progress);
    return percent === null ? "翻译中" : `AI ${percent}%`;
  }
  return phaseLabels[progress?.phase ?? "preparing"] ?? "生成中";
}

function ProgressDetails({
  progress,
  compact = false,
}: {
  progress: SubtitleProgress;
  compact?: boolean;
}) {
  const percent = progressPercent(progress);
  const currentStage = progressStageIndex(progress);
  const activeBlocks = progress.active_blocks ?? [];
  const doneEvents = progress.done_events ?? 0;
  const totalEvents = progress.total_events ?? 0;
  const reference = sourceKeyLabel(progress.source_candidate_key);

  return (
    <div className={compact ? "space-y-2.5" : "space-y-3.5"}>
      <div className="flex items-start justify-between gap-3">
        <p className="font-semibold text-[var(--info)]">{progress.message || "正在准备生成任务"}</p>
        {percent !== null && <span className="tnum shrink-0 font-semibold text-[var(--info)]">{percent}%</span>}
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.08]">
        <div
          className={`h-full rounded-full bg-[var(--info)] transition-all ${percent === null ? "w-1/3 animate-pulse" : ""}`}
          style={percent === null ? undefined : { width: `${percent}%` }}
        />
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-caption text-[var(--text-muted)]">
        {progress.total_blocks > 0 && (
          <span className="tnum">翻译块 {progress.done_blocks}/{progress.total_blocks}</span>
        )}
        {totalEvents > 0 && <span className="tnum">对白 {doneEvents}/{totalEvents} 条</span>}
        {reference && <span className="max-w-full truncate" title={reference}>参考 {reference}</span>}
        {progress.model_requests > 0 && (
          <span className="tnum">
            模型调用 {progress.model_requests} 次
            {progress.model_tokens > 0 ? ` · ${progress.model_tokens.toLocaleString()} token` : ""}
          </span>
        )}
        <span className="tnum">已用时 {formatElapsed(progress.elapsed_seconds ?? 0)}</span>
      </div>
      {activeBlocks.length > 0 && (
        <p className="text-caption text-[var(--info)]/85">
          {progress.parallelism > 1 ? `并发上限 ${progress.parallelism} 路 · ` : ""}
          正在处理第 {activeBlocks.join("、")} 块
          {progress.oldest_active_seconds > 0
            ? ` · 最早一块已等待 ${formatElapsed(progress.oldest_active_seconds)}`
            : ""}
        </p>
      )}
      {(progress.rate_limit_count > 0 || progress.validation_retries > 0) && (
        <p className="text-caption leading-5 text-[#ffd89a]">
          {progress.rate_limit_count > 0
            ? `模型服务繁忙 ${progress.rate_limit_count} 次，系统已自动降速重试。`
            : ""}
          {progress.validation_retries > 0
            ? `有 ${progress.validation_retries} 次返回格式不完整，系统已自动纠正。`
            : ""}
        </p>
      )}
      <ol className={compact ? "space-y-1" : "space-y-1.5"} aria-label="字幕生成阶段">
        {PROGRESS_STAGES.map((stage, index) => {
          const completed = index < currentStage;
          const current = index === currentStage;
          const label = index === 0 && progress.uses_ocr ? "识别并检查图片字幕" : stage.label;
          return (
            <li
              key={stage.label}
              className={`flex items-center gap-2 text-caption ${
                current
                  ? "font-medium text-[var(--info)]"
                  : completed
                    ? "text-white/70"
                    : "text-white/35"
              }`}
            >
              <span
                className={`flex size-4 shrink-0 items-center justify-center rounded-full border text-[9px] ${
                  current
                    ? "border-[var(--info)] bg-[var(--info)]/15"
                    : completed
                      ? "border-[var(--ok)]/60 bg-[var(--ok)]/10 text-[#7cf0a4]"
                      : "border-white/15"
                }`}
              >
                {completed ? "✓" : index + 1}
              </span>
              <span>{label}</span>
              {current && <span className="ml-auto text-[var(--text-muted)]">进行中</span>}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function SubtitleGenPanel({
  file,
  onChanged,
}: {
  file: LibraryItemFile;
  /** 产物落盘后回调（详情页借此重拉字幕清单，新字幕立刻可见） */
  onChanged?: () => void;
}) {
  const router = useRouter();
  const { start: startAgent } = useAgentConversations();
  const { isAdmin } = usePermissions();
  const { latestFor, upsert } = useJobs();
  const [targetLanguage, setTargetLanguage] = useState<OutputLanguage>("chs");
  const [bilingual, setBilingual] = useState(false);
  const [secondaryLanguage, setSecondaryLanguage] = useState<OutputLanguage>("eng");
  const [sourceCandidateKey, setSourceCandidateKey] = useState<string | null>(null);
  const [preview, setPreview] = useState<SubtitleGenerationPreview | null>(null);
  const [pgsOcrLanguage, setPgsOcrLanguage] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<"preview" | "status">("preview");
  const [previewing, setPreviewing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [agentStarting, setAgentStarting] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const previewRequestRef = useRef(0);
  const sharedJob = latestFor("library_file", file.id, "subtitle.generate");
  const sharedJobId = sharedJob?.id;
  const sharedJobStatus = sharedJob?.status;
  const progress = subtitleProgress(sharedJob);
  const running = sharedJobStatus ? RUNNING_JOB_STATUSES.has(sharedJobStatus) : false;
  const hasTerminalIssue =
    sharedJobStatus === "blocked" ||
    sharedJobStatus === "failed" ||
    sharedJobStatus === "cancelled";
  const previousJobRef = useRef({ id: sharedJobId, status: sharedJobStatus });

  // 只在当前页面亲眼看到同一任务从活跃态变为成功时刷新字幕台账；初次挂载
  // 读到历史成功任务不触发多余请求。依赖只用 primitive，避免对象替换抖动。
  useEffect(() => {
    const previous = previousJobRef.current;
    if (
      sharedJobId &&
      previous.id === sharedJobId &&
      previous.status &&
      RUNNING_JOB_STATUSES.has(previous.status) &&
      sharedJobStatus === "succeeded"
    ) {
      onChanged?.();
    }
    previousJobRef.current = { id: sharedJobId, status: sharedJobStatus };
  }, [onChanged, sharedJobId, sharedJobStatus]);

  const loadPreview = useCallback(async (
    target: string,
    secondary: string | null,
    sourceKey: string | null = null,
  ) => {
    const requestId = previewRequestRef.current + 1;
    previewRequestRef.current = requestId;
    setDialogMode("preview");
    setDialogOpen(true);
    setPreview(null);
    setPgsOcrLanguage("");
    setRequestError(null);
    setPreviewing(true);
    try {
      const result = await previewSubtitleGeneration(file.id, target, secondary, sourceKey);
      if (previewRequestRef.current !== requestId) return;
      setPreview(result);
      setSourceCandidateKey(result.selected_source_key);
      setPgsOcrLanguage(result.pgs_conversion?.ocr_language ?? "");
    } catch (error) {
      if (previewRequestRef.current !== requestId) return;
      setRequestError(errorMessage(error));
    } finally {
      if (previewRequestRef.current === requestId) setPreviewing(false);
    }
  }, [file.id]);

  const openAction = useCallback(() => {
    if (running || hasTerminalIssue) {
      setDialogMode("status");
      setDialogOpen(true);
      return;
    }
    void loadPreview(targetLanguage, bilingual ? secondaryLanguage : null);
  }, [bilingual, hasTerminalIssue, loadPreview, running, secondaryLanguage, targetLanguage]);

  const onStart = useCallback(async () => {
    setStarting(true);
    setRequestError(null);
    const convertPgs =
      preview?.blocker?.code === "pgs_conversion_required" &&
      preview.pgs_conversion?.available === true;
    try {
      const started = await generateSubtitles(file.id, targetLanguage, {
        convertPgs,
        pgsOcrLanguage: convertPgs ? pgsOcrLanguage || null : null,
        secondaryLanguage: bilingual ? secondaryLanguage : null,
        sourceCandidateKey,
      });
      upsert(started);
      setDialogOpen(false);
    } catch (error) {
      // 确认到真正入队之间文件可能变化，错误留在弹窗里让用户看清。
      setRequestError(errorMessage(error));
    } finally {
      setStarting(false);
    }
  }, [
    bilingual,
    file.id,
    pgsOcrLanguage,
    preview,
    secondaryLanguage,
    sourceCandidateKey,
    targetLanguage,
    upsert,
  ]);

  const onStop = useCallback(async () => {
    setStopping(true);
    setRequestError(null);
    try {
      if (!sharedJobId) return;
      upsert(await cancelJob(sharedJobId));
    } catch (error) {
      setRequestError(errorMessage(error));
    } finally {
      setStopping(false);
    }
  }, [sharedJobId, upsert]);

  const handOffToAgent = useCallback(
    async (reason: string) => {
      setAgentStarting(true);
      setRequestError(null);
      const conversion = preview?.pgs_conversion;
      const prompt = [
        "请帮我处理 MovieClaw 的 AI 字幕生成问题。",
        `文件：${file.file_name}`,
        `文件台账 ID：${file.id}`,
        `文件路径：${file.file_path}`,
        `当前问题：${reason}`,
        conversion
          ? `预检环境：${conversion.platform} ${conversion.architecture}${conversion.engine ? ` · ${conversion.engine}` : " · 未找到可用识别引擎"}`
          : null,
        conversion?.message ? `预检诊断：${conversion.message}` : null,
        ...(preview?.blocker?.suggestions.map((suggestion) => `已有建议：${suggestion}`) ?? []),
        "请先判断原因；如果能通过 MovieClaw 的工具安全解决，请直接执行，否则给出明确的操作步骤。不要修改影片原文件。",
      ]
        .filter(Boolean)
        .join("\n");

      try {
        const id = await startAgent(prompt);
        setDialogOpen(false);
        router.push(`/sessions/${id}` as Route);
      } catch (error) {
        setRequestError(`无法启动 Agent：${errorMessage(error)}`);
      } finally {
        setAgentStarting(false);
      }
    },
    [file.file_name, file.file_path, file.id, preview, router, startAgent],
  );

  if (!isAdmin || file.missing) return null;

  const jobSucceeded = sharedJobStatus === "succeeded";
  const resultMessage =
    sharedJob?.error?.message ??
    (typeof sharedJob?.result?.message === "string" ? sharedJob.result.message : null) ??
    "任务已经结束。";
  const generated = file.subtitle_streams.some(
    (subtitle) => subtitle.external && isAiSubtitle(subtitle.file_name),
  );
  const chosen = preview?.candidates.find(
    (candidate) => candidateKey(candidate) === preview.chosen_key,
  );
  const selectedCandidate = preview?.candidates.find(
    (candidate) => candidateKey(candidate) === preview.selected_source_key,
  );
  const pgsConversion = preview?.pgs_conversion;
  const canPreparePgs =
    preview?.blocker?.code === "pgs_conversion_required" &&
    pgsConversion?.available === true;
  const pgsLanguageReady =
    !pgsConversion?.language_confirmation_required || Boolean(pgsOcrLanguage);
  const canConvertPgs = canPreparePgs && pgsLanguageReady;

  let badgeClass = AI_BADGE_SILVER;
  let badgeText = generated || jobSucceeded ? "新建 AI 版本" : "AI 生成";
  if (previewing) {
    badgeText = "正在检查";
  } else if (running) {
    badgeClass = AI_BADGE_RUNNING;
    badgeText = runningBadgeText(progress);
  } else if (generated || jobSucceeded) {
    badgeClass = AI_BADGE_SILVER;
  } else if (hasTerminalIssue) {
    badgeClass = AI_BADGE_FAILED;
    badgeText = "AI 未完成";
  }
  const runningTarget = progress?.target_language || targetLanguage;
  const runningSecondary = progress?.secondary_language ?? null;
  const activeOutputLabel = outputLabel(runningTarget, runningSecondary);
  const badgeLabel = running ? `${activeOutputLabel} · ${badgeText}` : badgeText;

  const runningTooltip = running && progress ? <ProgressDetails progress={progress} compact /> : null;

  const actionButton = (
    <button
      type="button"
      className={`${AI_BADGE_BASE} ${badgeClass}`}
      disabled={previewing}
      aria-label={running ? `${badgeLabel}，${progress?.message || "正在运行"}` : badgeLabel}
      onClick={openAction}
    >
      {running ? (
        <span className="size-1.5 animate-pulse rounded-full bg-current" />
      ) : (
        <WandIcon className="size-3.5" />
      )}
      <span>{badgeLabel}</span>
    </button>
  );

  return (
    <>
      {/* 生成前直接进确认弹窗，不用 tooltip 重复解释；只有后台任务运行时，
          固定状态按钮才承载进度 tooltip。打开状态弹窗后立即卸掉 tooltip，
          避免浮层残留在 Modal 上方。 */}
      {running || hasTerminalIssue ? (
        !dialogOpen ? (
          <Tooltip content={runningTooltip} maxWidth={460}>
            {actionButton}
          </Tooltip>
        ) : (
          actionButton
        )
      ) : (
        <LlmCapabilityGate feature="生成字幕能力" noticeVariant="inline">
          {actionButton}
        </LlmCapabilityGate>
      )}

      <Modal
        open={dialogOpen}
        onClose={() => !starting && !agentStarting && setDialogOpen(false)}
        label={dialogMode === "status" ? "AI 字幕生成状态" : "生成 AI 字幕"}
        width="lg"
        panelClassName="max-h-[88vh] overflow-y-auto"
      >
        <div className="p-5 max-md:p-4">
          <div className="flex items-start gap-3">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-[var(--info)]/30 bg-[var(--info)]/[0.12] text-caption font-bold text-[#a9e2ff]">
              AI
            </span>
            <div>
              <h2 className="text-title-sm font-bold text-white">
                {dialogMode === "status"
                  ? running
                    ? `正在生成${activeOutputLabel}`
                    : "AI 字幕任务"
                  : "生成 AI 字幕"}
              </h2>
              <p className="mt-1 text-sub leading-5 text-[var(--text-muted)]">
                {dialogMode === "status"
                  ? running
                    ? "后台运行，离开页面不会中断。"
                    : "任务详情与处理建议。"
                  : "确认后才调用 AI，并在后台生成。"}
              </p>
            </div>
          </div>

          {dialogMode === "status" ? (
            <div className="mt-6 space-y-4">
              {running && progress ? (
                <>
                  <div className="rounded-xl border border-[var(--info)]/25 bg-[var(--info)]/[0.07] p-4">
                    <ProgressDetails progress={progress} />
                  </div>
                  {requestError && (
                    <p className="rounded-xl border border-[#ff9f9f]/30 bg-[#ff9f9f]/[0.08] px-3.5 py-2.5 text-sub leading-5 text-[#ffb4b4]">
                      {requestError}
                    </p>
                  )}
                  <div className="flex justify-end gap-2.5">
                    <button type="button" className={CANCEL_BUTTON} onClick={() => setDialogOpen(false)}>
                      关闭
                    </button>
                    <button
                      type="button"
                      className="rounded-lg border border-[#ff9f9f]/30 bg-[#ff9f9f]/[0.08] px-4 py-2 text-ui text-[#ffb4b4] transition hover:bg-[#ff9f9f]/[0.14] disabled:opacity-40"
                      disabled={stopping}
                      onClick={() => void onStop()}
                    >
                      {stopping ? "正在请求停止…" : "停止生成"}
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div
                    className={`rounded-xl border p-4 ${
                      jobSucceeded
                        ? "border-[var(--ok)]/30 bg-[var(--ok)]/[0.08] text-[#7cf0a4]"
                        : "border-[#ff9f9f]/30 bg-[#ff9f9f]/[0.08] text-[#ffb4b4]"
                    }`}
                  >
                    <p className="text-ui font-semibold">
                      {jobSucceeded ? "字幕生成完成" : "字幕生成未完成"}
                    </p>
                    <p className="mt-1.5 text-sub leading-5 opacity-85">
                      {resultMessage}
                    </p>
                  </div>
                  {requestError && (
                    <p className="rounded-xl border border-[#ff9f9f]/30 bg-[#ff9f9f]/[0.08] px-3.5 py-2.5 text-sub leading-5 text-[#ffb4b4]">
                      {requestError}
                    </p>
                  )}
                  <div className="flex justify-end gap-2.5">
                    {!jobSucceeded && (
                      <>
                        <button
                          type="button"
                          className={CONFIRM_BUTTON}
                          onClick={() =>
                            void loadPreview(
                              targetLanguage,
                              bilingual ? secondaryLanguage : null,
                            )
                          }
                        >
                          重新预检
                        </button>
                        <button
                          type="button"
                          className={AGENT_BUTTON}
                          disabled={agentStarting}
                          onClick={() => void handOffToAgent(resultMessage)}
                        >
                          {agentStarting ? "正在交给 Agent…" : "交给 Agent 处理"}
                        </button>
                      </>
                    )}
                    <button type="button" className={CANCEL_BUTTON} onClick={() => setDialogOpen(false)}>
                      关闭
                    </button>
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="mt-5">
              <div className="mb-4 rounded-xl border border-white/[0.08] bg-white/[0.035] p-3.5">
                <p className="mb-3 text-sub font-semibold text-white/85">输出语言</p>
                <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
                  <label className="block">
                    <span className="mb-1.5 block text-caption text-[var(--text-muted)]">
                      {bilingual ? "第一行语言" : "目标语言"}
                    </span>
                    <select
                      value={targetLanguage}
                      disabled={starting}
                      onChange={(event) => {
                        const target = event.target.value as OutputLanguage;
                        const secondary = nextSecondary(target, secondaryLanguage);
                        setTargetLanguage(target);
                        setSecondaryLanguage(secondary);
                        void loadPreview(target, bilingual ? secondary : null);
                      }}
                      className="w-full rounded-lg border border-white/10 bg-[#151820] px-3 py-2 text-ui text-white outline-none focus:border-[var(--info)]/60 disabled:opacity-50"
                    >
                      {OUTPUT_LANGUAGES.map((language) => (
                        <option key={language.token} value={language.token}>
                          {language.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  {bilingual && (
                    <label className="block">
                      <span className="mb-1.5 block text-caption text-[var(--text-muted)]">
                        第二行语言
                      </span>
                      <select
                        value={secondaryLanguage}
                        disabled={starting}
                        onChange={(event) => {
                          const secondary = event.target.value as OutputLanguage;
                          setSecondaryLanguage(secondary);
                          void loadPreview(targetLanguage, secondary);
                        }}
                        className="w-full rounded-lg border border-white/10 bg-[#151820] px-3 py-2 text-ui text-white outline-none focus:border-[var(--info)]/60 disabled:opacity-50"
                      >
                        {OUTPUT_LANGUAGES.map((language) => (
                          <option
                            key={language.token}
                            value={language.token}
                            disabled={language.token === targetLanguage}
                          >
                            {language.label}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </div>
                <label className="mt-3 inline-flex cursor-pointer items-center gap-2 text-sub text-white/75">
                  <input
                    type="checkbox"
                    checked={bilingual}
                    disabled={starting}
                    onChange={(event) => {
                      const enabled = event.target.checked;
                      const secondary = nextSecondary(targetLanguage, secondaryLanguage);
                      setBilingual(enabled);
                      setSecondaryLanguage(secondary);
                      void loadPreview(targetLanguage, enabled ? secondary : null);
                    }}
                    className="size-4 accent-[var(--info)]"
                  />
                  生成双语字幕
                </label>
                {bilingual && (
                  <p className="mt-2 text-caption text-[var(--text-faint)]">
                    每条字幕固定两行，上下顺序按这里的选择生成。
                  </p>
                )}
              </div>

              {previewing && (
                <div className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-4">
                  <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-[var(--info)]" />
                  <p className="text-ui font-medium text-white">正在检查参考字幕，不会调用 AI…</p>
                </div>
              )}

              {!previewing && preview && preview.candidates.length > 0 && (
                <div className="mb-4 rounded-xl border border-white/[0.08] bg-white/[0.035] p-3.5">
                  <label className="block">
                    <span className="mb-1.5 block text-sub font-semibold text-white/85">
                      参考字幕
                    </span>
                    <select
                      value={sourceCandidateKey ?? ""}
                      disabled={starting || !preview.candidates.some((candidate) => candidate.selectable)}
                      onChange={(event) =>
                        void loadPreview(
                          targetLanguage,
                          bilingual ? secondaryLanguage : null,
                          event.target.value || null,
                        )
                      }
                      className="w-full rounded-lg border border-white/10 bg-[#151820] px-3 py-2 text-ui text-white outline-none focus:border-[var(--info)]/60 disabled:opacity-50"
                    >
                      {!sourceCandidateKey && <option value="">没有可用的参考字幕</option>}
                      {preview.candidates.map((candidate) => (
                        <option
                          key={candidateKey(candidate)}
                          value={candidateKey(candidate)}
                          disabled={!candidate.selectable}
                        >
                          {candidateLabel(candidate)}
                          {!candidate.selectable ? `（不可用：${candidate.excluded ?? "不支持"}）` : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p className="mt-2 text-caption leading-5 text-[var(--text-faint)]">
                    默认优先英语。也可以指定其他内封或外挂字幕；选择 PGS 时会先识别文字，再开始
                    AI 翻译。
                  </p>
                  {selectedCandidate?.requires_ocr && (
                    <p className="mt-1.5 text-caption text-[var(--warn)]/85">
                      当前选择的是图片字幕，需要先完成文字识别。
                    </p>
                  )}
                </div>
              )}

              {!previewing && requestError && (
                <div className="rounded-xl border border-[#ff9f9f]/30 bg-[#ff9f9f]/[0.08] px-4 py-3 text-sub leading-6 text-[#ffb4b4]">
                  {requestError}
                </div>
              )}

              {!previewing && canPreparePgs && pgsConversion && (
                <div className="space-y-3.5">
                  <div className="rounded-xl border border-[var(--warn)]/30 bg-[var(--warn)]/[0.08] p-3.5">
                    <p className="text-ui font-semibold text-[var(--warn)]">
                      {pgsConversion.language_confirmation_required
                        ? "请选择原字幕语言"
                        : "先识别图片字幕"}
                    </p>
                    <p className="mt-1 text-sub leading-5 text-[var(--warn)]/85">
                      这份字幕是图片。MovieClaw 会先识别其中的文字，确认内容完整后再生成
                      {outputLabel(targetLanguage, bilingual ? secondaryLanguage : null)}字幕。
                    </p>
                  </div>
                  <div className="rounded-xl border border-white/[0.08] bg-white/[0.035] px-3.5 py-3 text-sub leading-5 text-[var(--text-muted)]">
                    {pgsConversion.language_confirmation_required ? (
                      <label className="block">
                        <span className="mb-1.5 block font-medium text-white/85">原字幕语言</span>
                        <select
                          value={pgsOcrLanguage}
                          onChange={(event) => setPgsOcrLanguage(event.target.value)}
                          className="w-full rounded-lg border border-white/10 bg-[#151820] px-3 py-2 text-ui text-white outline-none focus:border-[var(--info)]/60"
                        >
                          <option value="">请选择字幕语言</option>
                          {pgsConversion.language_options.map((option) => (
                            <option key={option.code} value={option.code}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                        <span className="mt-1.5 block text-caption text-[var(--text-faint)]">
                          请选择画面中实际显示的语言。
                        </span>
                      </label>
                    ) : (
                      <p className="text-white/75">
                        原字幕语言：{pgsConversion.ocr_language_label ?? "已自动识别"}
                      </p>
                    )}
                    <p className="mt-2 border-t border-white/[0.07] pt-2">
                      原影片和字幕不会被修改。识别结果可能有少量错字，完成后建议抽查人名与特殊字体。
                    </p>
                  </div>
                </div>
              )}

              {!previewing && preview?.blocker && !canPreparePgs && (
                <div className="space-y-3.5">
                  <div className="rounded-xl border border-[#ff9f9f]/30 bg-[#ff9f9f]/[0.08] p-3.5">
                    <p className="text-ui font-semibold text-[#ffb4b4]">{preview.blocker.title}</p>
                    <p className="mt-1 text-sub leading-5 text-[#ffb4b4]/85">
                      {preview.blocker.message}
                    </p>
                  </div>
                  {preview.blocker.suggestions.length > 0 && (
                    <div>
                      <p className="mb-1.5 text-sub font-medium text-white/85">可以这样处理</p>
                      <ul className="list-disc space-y-1 pl-5 text-sub leading-5 text-white/75">
                        {preview.blocker.suggestions.map((suggestion) => (
                          <li key={suggestion}>{suggestion}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {pgsConversion && (
                    <details className="rounded-xl border border-white/[0.08] bg-white/[0.025] px-3.5 py-2.5 text-sub text-[var(--text-muted)]">
                      <summary className="cursor-pointer select-none text-white/70">查看诊断信息</summary>
                      <div className="mt-2 space-y-1 border-t border-white/[0.07] pt-2 leading-5">
                        <p>
                          运行环境：{pgsConversion.platform} {pgsConversion.architecture}
                          {pgsConversion.engine ? ` · ${pgsConversion.engine}` : " · 未找到可用识别引擎"}
                        </p>
                        {pgsConversion.message !== preview.blocker.message && (
                          <p>{pgsConversion.message}</p>
                        )}
                      </div>
                    </details>
                  )}
                </div>
              )}

              {!previewing && preview && !preview.blocker && chosen && (
                <div className="space-y-3.5">
                  <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] p-3.5">
                    <div className="flex flex-wrap items-center gap-2 text-ui font-semibold text-white">
                      <span>{candidateLabel(chosen)}</span>
                      <span className="text-[var(--text-faint)]">→</span>
                      <span className="text-[var(--info)]">
                        {outputLabel(targetLanguage, bilingual ? secondaryLanguage : null)}
                      </span>
                    </div>
                    <p className="tnum mt-1.5 text-sub text-[var(--text-muted)]">
                      {preview.event_count.toLocaleString()} 条对白 · {tokenEstimate(preview.estimated_tokens)}
                    </p>
                  </div>

                  <p className="text-sub leading-5 text-[var(--text-muted)]">
                    生成同目录
                    <span className="tnum ml-1 break-all text-white/80">
                      {preview.output_filename ?? "规范命名的 AI 字幕文件"}
                    </span>
                    ，原字幕不变；离开页面不影响生成。
                  </p>

                  {preview.already_generated && (
                    <p className="rounded-lg border border-[var(--warn)]/25 bg-[var(--warn)]/[0.07] px-3 py-2 text-sub text-[var(--warn)]/90">
                      已有 AI 字幕，将被覆盖。
                    </p>
                  )}
                </div>
              )}

              {!previewing && (
                <div className="mt-5 flex justify-end gap-2.5">
                  <button type="button" className={CANCEL_BUTTON} onClick={() => setDialogOpen(false)}>
                    {(preview?.blocker && !canPreparePgs) || requestError ? "关闭" : "取消"}
                  </button>
                  {((preview?.blocker && !canPreparePgs) || (!preview && requestError)) && (
                    <button
                      type="button"
                      className={CONFIRM_BUTTON}
                      onClick={() =>
                        void loadPreview(
                          targetLanguage,
                          bilingual ? secondaryLanguage : null,
                        )
                      }
                    >
                      重新检查
                    </button>
                  )}
                  {((preview?.blocker && !canPreparePgs) || requestError) && (
                    <button
                      type="button"
                      className={AGENT_BUTTON}
                      disabled={agentStarting}
                      onClick={() =>
                        void handOffToAgent(
                          requestError ?? preview?.blocker?.message ?? "字幕生成预检没有通过",
                        )
                      }
                    >
                      {agentStarting ? "正在交给 Agent…" : "交给 Agent 处理"}
                    </button>
                  )}
                  {canPreparePgs && (
                    <button
                      type="button"
                      className={CONFIRM_BUTTON}
                      disabled={starting || !canConvertPgs}
                      onClick={() => void onStart()}
                    >
                      {starting
                        ? "正在启动…"
                        : `开始生成${outputLabel(targetLanguage, bilingual ? secondaryLanguage : null)}`}
                    </button>
                  )}
                  {preview && !preview.blocker && chosen && (
                    <button
                      type="button"
                      className={CONFIRM_BUTTON}
                      disabled={starting}
                      onClick={() => void onStart()}
                    >
                      {starting
                        ? "正在启动…"
                        : `确认生成${outputLabel(targetLanguage, bilingual ? secondaryLanguage : null)}`}
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </Modal>
    </>
  );
}
