"use client";

import { useState } from "react";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import {
  ActivityIcon,
  ChevronRightIcon,
  MoreIcon,
} from "@/components/icons";
import { useLlmCapability } from "@/components/llm-gate";
import { Modal } from "@/components/modal";
import { OverflowText } from "@/components/overflow-text";
import { useAgentConversations } from "@/lib/agent-conversations";
import {
  cancelJob,
  dismissJob,
  isSystemCancelled,
  retryJob,
  undismissJob,
  type JobStatus,
  type JobView,
} from "@/lib/api/jobs";
import { isDismissed } from "@/lib/job-attention";
import { formatBytes, formatDuration } from "@/lib/format";
import { useJobs } from "@/lib/jobs";
import { taskActivityBadge, useTaskActivity } from "@/lib/task-activity";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { usePermissions } from "@/lib/permissions";

export const JOB_TYPE_LABELS: Record<string, string> = {
  "subtitle.generate": "生成 AI 字幕",
  "library.scan": "扫描媒体库",
  "library.metadata.refresh": "刷新媒体库元数据",
  "media.metadata.refresh": "刷新条目元数据",
  "library.organize": "整理媒体库文件",
  "library.transfer": "转移媒体库条目",
  "library.ingest": "自动整理入库",
};

export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "进行中",
  retry_wait: "等待重试",
  cancelling: "正在取消",
  waiting: "等待前置任务",
  blocked: "需要处理",
  succeeded: "已完成",
  failed: "未完成",
  cancelled: "已取消",
};

const JOB_PHASE_LABELS: Record<string, string> = {
  queued: "等待执行",
  preparing: "准备",
  ocr: "识别图形字幕",
  syncing: "校准时间轴",
  glossary: "整理术语",
  translating: "翻译字幕",
  validating: "检查译文",
  compressing: "压缩字幕",
  writing: "写入文件",
  refreshing: "刷新元数据",
  walking: "遍历文件",
  ingesting: "识别入账",
  probing: "分析媒体",
  assets: "补齐资产",
  organizing: "整理文件",
  transferring: "搬运文件",
  identifying: "识别条目",
  waiting_library: "等待媒体库",
  waiting_stable: "等待下载稳定",
  finalizing: "收尾",
  retry_wait: "等待重试",
  blocked: "等待处理",
  completed: "已完成",
  cancelled: "已取消",
};

const LANGUAGE_LABELS: Record<string, string> = {
  chs: "简体中文",
  cht: "繁体中文",
  eng: "英语",
  jpn: "日语",
  kor: "韩语",
  fre: "法语",
  ger: "德语",
  spa: "西班牙语",
  ita: "意大利语",
  por: "葡萄牙语",
  rus: "俄语",
  tha: "泰语",
};

const STATUS_STYLE: Record<JobStatus, { dot: string; border?: string }> = {
  queued: { dot: "bg-white/40" },
  running: { dot: "animate-pulse bg-[var(--info)]" },
  retry_wait: { dot: "bg-[var(--warn)]" },
  cancelling: { dot: "bg-[var(--warn)]" },
  waiting: { dot: "bg-white/40" },
  blocked: {
    dot: "bg-[var(--danger)]",
    border: "border-[var(--danger)]/20",
  },
  succeeded: { dot: "bg-[var(--ok)]" },
  failed: {
    dot: "bg-[var(--danger)]",
    border: "border-[var(--danger)]/20",
  },
  cancelled: { dot: "bg-white/30" },
};

const SUPPORTED_ACTIONS = new Set([
  "retry_job",
  "open_settings",
  "inspect_logs",
  "update_runtime",
  "handoff_agent",
]);

function detailNumber(details: Record<string, unknown>, key: string): number | null {
  const value = details[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function detailString(details: Record<string, unknown>, key: string): string | null {
  const value = details[key];
  return typeof value === "string" && value ? value : null;
}

function detailStrings(details: Record<string, unknown>, key: string): string[] {
  const value = details[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function detailCount(details: Record<string, unknown>, key: string): number {
  const value = details[key];
  if (Array.isArray(value)) return value.length;
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function jobString(job: JobView, key: string): string | null {
  return detailString(job.progress.details, key) ?? detailString(job.input_data, key);
}

/** 字幕任务的目标语言来自已确认输入，运行期 details 只作为更新后的优先快照。 */
function subtitleLanguageLabel(job: JobView): string | null {
  if (job.job_type !== "subtitle.generate") return null;
  const target = jobString(job, "target_language");
  if (!target) return null;
  const primary = LANGUAGE_LABELS[target] ?? target;
  const secondary = jobString(job, "secondary_language");
  return secondary
    ? `${primary} + ${LANGUAGE_LABELS[secondary] ?? secondary}（双语）`
    : primary;
}

interface SubtitleUsageSummary {
  requests: number;
  failedRequests: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  cacheReadTokens: number;
  totalDurationMs: number;
  maxDurationMs: number;
}

function subtitleUsageSummary(job: JobView): SubtitleUsageSummary | null {
  if (job.job_type !== "subtitle.generate") return null;
  const summary = {
    requests: detailNumber(job.usage, "request_count") ?? 0,
    failedRequests: detailNumber(job.usage, "failed_request_count") ?? 0,
    promptTokens: detailNumber(job.usage, "prompt_tokens") ?? 0,
    completionTokens: detailNumber(job.usage, "completion_tokens") ?? 0,
    totalTokens: detailNumber(job.usage, "total_tokens") ?? 0,
    cacheReadTokens: detailNumber(job.usage, "cache_read_tokens") ?? 0,
    totalDurationMs: detailNumber(job.usage, "total_duration_ms") ?? 0,
    maxDurationMs: detailNumber(job.usage, "max_duration_ms") ?? 0,
  };
  return summary.requests > 0 || summary.totalTokens > 0 || summary.totalDurationMs > 0
    ? summary
    : null;
}

function formatInteger(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatModelDuration(milliseconds: number): string {
  if (milliseconds <= 0) return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} 毫秒`;
  return formatDuration(Math.max(1, Math.round(milliseconds / 1000)));
}

function originLabel(job: JobView): string {
  const origin =
    job.origin === "cli"
      ? "CLI"
      : job.origin === "agent"
        ? "智能体"
        : job.origin === "scheduler" || job.origin === "system"
          ? "系统自动"
          : "网页";
  return job.actor_name ? `${origin} · ${job.actor_name}` : origin;
}

function progressAmount(job: JobView): string | null {
  const { current, total } = job.progress;
  if (current == null || total == null) return null;
  if (job.job_type === "library.ingest") {
    return `${formatBytes(current)} / ${formatBytes(total)}`;
  }
  const unit =
    job.job_type === "subtitle.generate"
      ? "个分块"
      : job.job_type === "library.scan"
        ? "个文件"
        : job.job_type.includes("metadata.refresh")
          ? "个条目"
          : job.job_type === "library.organize"
            ? "个文件"
            : job.job_type === "library.transfer"
              ? "个路径"
              : "项";
  return `${current} / ${total} ${unit}`;
}

function jobDetailItems(job: JobView): Array<{ label: string; alert?: boolean }> {
  const details = job.progress.details;
  const items: Array<{ label: string; alert?: boolean }> = [];

  if (job.job_type === "subtitle.generate") {
    const language = subtitleLanguageLabel(job);
    const doneEvents = detailNumber(details, "done_events");
    const totalEvents = detailNumber(details, "total_events");
    const parallelism = detailNumber(details, "parallelism");
    const rateLimits = detailNumber(details, "rate_limit_count");
    if (language) items.push({ label: LANGUAGE_LABELS[language] ?? language });
    if (doneEvents != null && totalEvents) {
      items.push({ label: `${doneEvents} / ${totalEvents} 条字幕` });
    }
    if (details.uses_ocr === true) items.push({ label: "PGS OCR" });
    if (parallelism && parallelism > 1) items.push({ label: `${parallelism} 路并行` });
    if (rateLimits) items.push({ label: `限流重试 ${rateLimits} 次`, alert: true });
  } else if (job.job_type === "library.scan") {
    const identified = detailNumber(details, "identified");
    const unidentified = detailNumber(details, "unidentified");
    const probed = detailNumber(details, "probed");
    const missing = detailNumber(details, "marked_missing");
    const errors = detailCount(details, "errors");
    if (identified) items.push({ label: `识别 ${identified} 个` });
    if (probed) items.push({ label: `补探 ${probed} 个` });
    if (unidentified) items.push({ label: `待识别 ${unidentified} 个`, alert: true });
    if (missing) items.push({ label: `缺失 ${missing} 个`, alert: true });
    if (errors) items.push({ label: `${errors} 个问题`, alert: true });
  } else if (job.job_type.includes("metadata.refresh")) {
    const failed = detailNumber(details, "failed");
    if (failed) items.push({ label: `${failed} 个未完成`, alert: true });
  } else if (job.job_type === "library.organize") {
    const errors = detailCount(details, "errors");
    if (errors) items.push({ label: `${errors} 个问题`, alert: true });
  } else if (job.job_type === "library.transfer") {
    const moved = detailNumber(details, "bytes_moved");
    const totalBytes = detailNumber(details, "total_bytes");
    if (moved) items.push({ label: `已搬运 ${formatBytes(moved)}` });
    else if (totalBytes) items.push({ label: `共 ${formatBytes(totalBytes)}` });
    if (details.cross_device === true) items.push({ label: "跨盘复制" });
  } else if (job.job_type === "library.ingest") {
    const fileName = detailString(details, "file_name");
    if (fileName) items.push({ label: fileName });
  }

  return items.slice(0, 4);
}

/** 任务中心统一状态点：紧贴标题展示状态，完整文字通过 title 与无障碍标签保留。 */
export function TaskStatusDot({
  label,
  dotClass,
}: {
  label: string;
  dotClass: string;
}) {
  return (
    <span
      title={label}
      role="img"
      aria-label={`状态：${label}`}
      className={`size-2.5 shrink-0 rounded-full ${dotClass}`}
    >
      <span className="sr-only">{label}</span>
    </span>
  );
}

interface TaskActionMenuItem {
  id: string;
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  tone?: "default" | "danger";
}

/** 标准任务卡片操作入口：固定在右上角，避免不同任务类型各自发明布局。 */
export function TaskActionsMenu({
  ariaLabel,
  disabled = false,
  items,
}: {
  ariaLabel: string;
  disabled?: boolean;
  items: TaskActionMenuItem[];
}) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-sub font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[disabled]:pointer-events-none " +
    "data-[disabled]:opacity-40";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={ariaLabel}
          disabled={disabled}
          className="glass-row flex size-7 !w-7 items-center justify-center p-0 text-white/55 data-[state=open]:!bg-[var(--glass-fill-active)] data-[state=open]:!text-[var(--text)] disabled:opacity-40"
        >
          <MoreIcon className="size-4 max-md:size-5" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[9rem] !rounded-xl p-1"
        >
          {items.map((item) => (
            <DropdownMenu.Item
              key={item.id}
              onSelect={item.onSelect}
              disabled={item.disabled}
              className={`${itemClass} ${
                item.tone === "danger"
                  ? "!text-[#ff6b6b] data-[highlighted]:!bg-[#ff6b6b]/10"
                  : "text-white/75"
              }`}
            >
              {item.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function StatusDot({ status }: { status: JobStatus }) {
  const style = STATUS_STYLE[status];
  return (
    <TaskStatusDot
      label={JOB_STATUS_LABELS[status] ?? status}
      dotClass={style.dot}
    />
  );
}

export function JobCard({ job, onNavigate }: { job: JobView; onNavigate: () => void }) {
  const router = useRouter();
  const { start: startAgent } = useAgentConversations();
  const { upsert } = useJobs();
  const { state: llmState } = useLlmCapability();
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [usageExpanded, setUsageExpanded] = useState(false);
  const [dismissOpen, setDismissOpen] = useState(false);
  const active = ["queued", "running", "retry_wait", "cancelling", "waiting"].includes(
    job.status,
  );
  const executing = job.status === "running" || job.status === "cancelling";
  const waitingProgress = job.progress.mode === "waiting";
  // 「需要处理」是**当下要不要用户动手**，不是"这件事失败过"。用户已经拍板
  // 不处理，红框与「需要处理：」前缀就该一起撤下——否则忽略了还在报警，
  // 等于没忽略。失败这个事实由状态点与摘要文案继续如实承担。
  const attention =
    (job.status === "blocked" || job.status === "failed") && !isDismissed(job);
  const cancellable = active && job.status !== "cancelling";
  // 忽略只开给失败任务：其余状态各有正确出口，不该被"藏起来"顶替。活跃任务
  // （含 blocked）要真正终结得走取消——它们仍占着去重键与资源锁，光藏掉提醒
  // 会留下一个谁也看不见、却挡着同名任务再次创建的幽灵。
  const dismissable = job.status === "failed" && !isDismissed(job);
  const dismissed = isDismissed(job);
  const percent = job.progress.percent;
  const statusStyle = STATUS_STYLE[job.status];
  const details = jobDetailItems(job);
  const importedFiles =
    job.job_type === "library.ingest"
      ? detailStrings(job.progress.details, "imported_files")
      : [];
  const hasDomainAlert = details.some((item) => item.alert);
  const subtitleUsage = subtitleUsageSummary(job);
  const compact =
    (job.status === "succeeded" || job.status === "cancelled") && !hasDomainAlert;
  const cardBorder =
    statusStyle.border ?? (hasDomainAlert ? "border-amber-300/15" : "border-white/[0.08]");
  const progressAccent =
    job.status === "succeeded"
      ? "bg-[var(--ok)]"
      : job.status === "failed" || job.status === "blocked"
        ? "bg-[var(--danger)]"
        : job.status === "retry_wait" || job.status === "cancelling"
          ? "bg-[var(--warn)]"
          : "bg-[var(--info)]";
  const amount = progressAmount(job);
  const phase = JOB_PHASE_LABELS[job.progress.phase] ?? job.progress.phase;
  const phaseStep =
    job.progress.phase_index != null && job.progress.phase_count != null
      ? `${job.progress.phase_index}/${job.progress.phase_count}`
      : null;
  const jobTypeLabel = JOB_TYPE_LABELS[job.job_type] || job.job_type;
  const title = job.subject || jobTypeLabel;
  const completedSubtitleLanguage =
    job.status === "succeeded" ? subtitleLanguageLabel(job) : null;
  const completedLibraryScan =
    job.job_type === "library.scan" && job.status === "succeeded";
  const summaryMessage = completedSubtitleLanguage
    ? `已生成${completedSubtitleLanguage}字幕`
    : job.error?.message ?? job.progress.message;
  const actions = (job.error?.actions ?? []).filter(
    (action) =>
      SUPPORTED_ACTIONS.has(action.type) &&
      (action.type !== "handoff_agent" ||
        llmState === "configured" ||
        llmState === "unavailable"),
  );
  if (
    job.status === "cancelled" &&
    !isSystemCancelled(job) &&
    !actions.some((action) => action.type === "retry_job")
  ) {
    actions.unshift({ type: "retry_job", label: "重新执行" });
  }

  async function runAction(type: string, target?: string) {
    setBusyAction(type);
    setActionError(null);
    try {
      if (type === "retry_job") {
        upsert(await retryJob(job.id));
        return;
      }
      if (type === "handoff_agent") {
        const sessionId = await startAgent(
          [
            "请帮我诊断并处理 MovieClaw 后台任务问题。",
            `任务 ID：${job.id}`,
            `任务类型：${job.job_type}`,
            `当前状态：${job.status}`,
            `错误代码：${job.error?.code ?? "未知"}`,
            `错误说明：${job.error?.message ?? job.progress.message}`,
            "请先查询任务详情和事件时间线；能安全处理就直接处理，否则给出明确步骤。",
          ].join("\n"),
        );
        onNavigate();
        router.push(`/sessions/${sessionId}` as Route);
        return;
      }
      const section =
        type === "inspect_logs"
          ? "logs"
          : type === "update_runtime"
            ? "app"
            : target || "app";
      onNavigate();
      router.push(`/settings/${section}` as Route);
    } catch (error) {
      setActionError((error as Error).message || "操作未完成，请稍后重试");
    } finally {
      setBusyAction(null);
    }
  }

  async function dismissCurrentJob(muteSource: boolean) {
    setBusyAction("dismiss");
    setActionError(null);
    try {
      upsert(await dismissJob(job.id, muteSource));
      setDismissOpen(false);
    } catch (error) {
      setActionError((error as Error).message || "忽略失败，请稍后重试");
    } finally {
      setBusyAction(null);
    }
  }

  async function undismissCurrentJob() {
    setBusyAction("undismiss");
    setActionError(null);
    try {
      upsert(await undismissJob(job.id));
    } catch (error) {
      setActionError((error as Error).message || "撤销忽略失败，请稍后重试");
    } finally {
      setBusyAction(null);
    }
  }

  async function cancelCurrentJob() {
    setBusyAction("cancel");
    setActionError(null);
    try {
      upsert(await cancelJob(job.id));
    } catch (error) {
      setActionError((error as Error).message || "取消任务失败，请稍后重试");
    } finally {
      setBusyAction(null);
    }
  }

  const menuItems: TaskActionMenuItem[] = [
    ...(cancellable
      ? [
          {
            id: "cancel",
            label: busyAction === "cancel" ? "正在取消…" : "取消任务",
            onSelect: () => void cancelCurrentJob(),
          },
        ]
      : []),
    ...actions.map((action) => ({
      id: `${action.type}:${action.target ?? ""}`,
      label: busyAction === action.type ? "处理中…" : action.label,
      onSelect: () => void runAction(action.type, action.target),
    })),
    // 「忽略」排在补救动作之后：先给出路（重试 / 看日志 / 交给 Agent），
    // 最后才给出口。这是用户在此前唯一缺的那一项——失败任务没有任何办法
    // 从「需要处理」里下架，红角标于是永不熄灭（issue #221）。
    ...(dismissable
      ? [
          {
            id: "dismiss",
            label: "忽略这个任务",
            onSelect: () => setDismissOpen(true),
          },
        ]
      : []),
    ...(dismissed
      ? [
          {
            id: "undismiss",
            label: busyAction === "undismiss" ? "撤销中…" : "撤销忽略",
            onSelect: () => void undismissCurrentJob(),
          },
        ]
      : []),
  ];

  return (
    <article
      className={`overflow-hidden rounded-2xl border bg-[rgba(14,16,22,0.52)] ${cardBorder} ${compact ? "px-3.5 py-2.5" : "p-4"}`}
    >
      <div className="min-w-0">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 flex-1 items-center gap-2.5">
            <StatusDot status={job.status} />
            {job.subject && (
              <span className="shrink-0 rounded-md border border-white/[0.08] bg-white/[0.045] px-1.5 py-0.5 text-micro font-medium text-white/55">
                {jobTypeLabel}
              </span>
            )}
            <h3 className="min-w-0 text-ui font-semibold leading-5 text-white/90">
              <OverflowText>{title}</OverflowText>
            </h3>
          </div>
          {menuItems.length > 0 && (
            <TaskActionsMenu
              ariaLabel={`${title}的更多操作`}
              disabled={busyAction !== null}
              items={menuItems}
            />
          )}
        </div>

        <div
          className={`${compact ? "mt-1 text-caption leading-5" : "mt-2.5 text-sub leading-5"} ${
            attention
              ? "rounded-xl border border-[var(--danger)]/15 bg-[var(--danger)]/[0.06] px-3 py-2.5 text-[var(--danger)]"
              : compact
                ? "text-white/50"
                : "text-white/60"
          }`}
        >
          <OverflowText
            lines={compact ? 1 : 2}
            className={compact ? "break-words" : "min-h-10 break-words"}
            tooltipContent={
              <span className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
                {attention && <span className="mr-1.5 font-semibold">需要处理：</span>}
                {summaryMessage}
              </span>
            }
          >
            {attention && <span className="mr-1.5 font-semibold">需要处理：</span>}
            {summaryMessage}
          </OverflowText>
        </div>

        {importedFiles.length > 0 && (
          <IngestImportedFiles files={importedFiles} />
        )}

        {!compact &&
          !completedLibraryScan &&
          (percent != null || executing || job.status === "retry_wait") && (
            <div className="mt-3">
              <div className="mb-1.5 flex items-center justify-between gap-3 text-caption">
                <OverflowText className="font-medium text-white/60">
                  {phase}
                  {phaseStep && <span className="ml-1.5 text-white/30">阶段 {phaseStep}</span>}
                </OverflowText>
                <span className="tnum shrink-0 text-white/50">
                  {percent != null
                    ? `${Math.round(percent)}%`
                    : job.status === "retry_wait"
                      ? "等待继续"
                      : waitingProgress
                        ? "等待中"
                        : "处理中"}
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.07]">
                <div
                  className={`${progressAccent} h-full rounded-full transition-[width] duration-700 ${
                    percent == null
                      ? job.status === "retry_wait" || waitingProgress
                        ? "w-1/3 opacity-35"
                        : "w-1/3 animate-pulse opacity-70"
                      : ""
                  }`}
                  style={
                    percent == null
                      ? undefined
                      : { width: `${Math.min(100, Math.max(percent > 0 ? 1 : 0, percent))}%` }
                  }
                />
              </div>
              {(amount || job.status === "retry_wait") && (
                <p className="tnum mt-1.5 text-caption text-white/35">
                  {amount}
                  {amount && job.status === "retry_wait" ? " · " : ""}
                  {job.status === "retry_wait"
                    ? `第 ${Math.max(1, job.attempt)} / ${job.max_attempts} 次尝试`
                    : ""}
                </p>
              )}
            </div>
        )}

        {!compact && details.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {details.map((item) => (
              <span
                key={item.label}
                className={`min-w-0 max-w-full rounded-md border px-2 py-1 text-caption ${
                  item.alert
                    ? "border-amber-300/15 bg-amber-300/[0.05] text-amber-100/75"
                    : "border-white/[0.07] bg-white/[0.035] text-white/45"
                }`}
              >
                <OverflowText>{item.label}</OverflowText>
              </span>
            ))}
          </div>
        )}

        {subtitleUsage && (
          <div className="mt-3 border-t border-white/[0.06] pt-2.5">
            <button
              type="button"
              aria-expanded={usageExpanded}
              onClick={() => setUsageExpanded((expanded) => !expanded)}
              className="flex w-full items-center justify-between gap-3 rounded-lg px-1 py-1 text-caption text-white/50 transition hover:text-white/75"
            >
              <span className="flex items-center gap-1.5 font-medium">
                <ChevronRightIcon
                  className={`size-3.5 transition-transform ${usageExpanded ? "rotate-90" : ""}`}
                />
                {usageExpanded ? "收起模型消耗" : "查看模型消耗"}
              </span>
              <OverflowText focusable={false} className="tnum text-right text-white/35">
                {formatInteger(subtitleUsage.totalTokens)} Token · {subtitleUsage.requests} 次请求
              </OverflowText>
            </button>
            {usageExpanded && (
              <div className="mt-2.5 rounded-xl border border-white/[0.07] bg-black/15 p-3">
                <div className="grid grid-cols-4 gap-3 max-md:grid-cols-2">
                  <UsageMetric label="总 Token" value={formatInteger(subtitleUsage.totalTokens)} />
                  <UsageMetric label="输入" value={formatInteger(subtitleUsage.promptTokens)} />
                  <UsageMetric label="输出" value={formatInteger(subtitleUsage.completionTokens)} />
                  <UsageMetric label="输入缓存" value={formatInteger(subtitleUsage.cacheReadTokens)} />
                </div>
                <p className="mt-3 flex flex-wrap gap-x-3 gap-y-1 border-t border-white/[0.06] pt-2.5 text-caption text-white/35">
                  {job.provider_ref && <span>{job.provider_ref}</span>}
                  <span>{subtitleUsage.requests} 次模型请求</span>
                  {subtitleUsage.failedRequests > 0 && (
                    <span className="text-amber-200/70">
                      {subtitleUsage.failedRequests} 次失败
                    </span>
                  )}
                  <span>累计响应 {formatModelDuration(subtitleUsage.totalDurationMs)}</span>
                  <span>最慢一次 {formatModelDuration(subtitleUsage.maxDurationMs)}</span>
                </p>
              </div>
            )}
          </div>
        )}

        {actionError && (
          <p className="mt-2 text-caption leading-5 text-[#ff9f9f]">{actionError}</p>
        )}
      </div>
      <footer className={`${compact ? "mt-1.5" : "mt-3"} flex flex-wrap items-center gap-2`}>
        <OverflowText
          alwaysShowTooltip
          tooltipContent={`${originLabel(job)} · ${formatDateTime(job.created_at)}`}
          className="text-micro text-white/25"
        >
          {originLabel(job)} · {formatRelativeTime(job.created_at)}
        </OverflowText>
        {dismissed && (
          <span className="ml-auto shrink-0 rounded-md border border-white/[0.08] bg-white/[0.04] px-1.5 py-0.5 text-micro text-white/40">
            已忽略
          </span>
        )}
      </footer>
      {dismissOpen && (
        <DismissJobDialog
          job={job}
          busy={busyAction === "dismiss"}
          onClose={() => {
            if (busyAction !== "dismiss") setDismissOpen(false);
          }}
          onConfirm={(muteSource) => void dismissCurrentJob(muteSource)}
        />
      )}
    </article>
  );
}

/** 有自动来源的任务类型：系统会周期性地重新建同样的任务，光忽略一次不管用。 */
const AUTO_RECREATED_JOB_TYPES: Record<string, string> = {
  "subtitle.generate": "不再自动为这个文件生成字幕",
};

/**
 * 忽略的二次确认。
 *
 * 这个弹窗真正要问的不是"你确定吗"，而是**忽略到哪一层**：
 *
 * - 只忽略这一条：任务从「需要处理」下架，但下一次入库扫描收尾还会为同一个
 *   文件建一条新的字幕任务、再失败一次——因为"本进程只自动试一次"是内存
 *   集合，重启即清零。用户会觉得忽略根本没生效（issue #221 的真实体验）。
 * - 连自动来源一起静音：这个决定写进数据库，跨重启有效。
 *
 * 因此对有自动来源的任务类型默认勾上——用户既然说了不处理，多半也不想每周
 * 再被同一件事叫醒一次；手动触发始终不受影响，随时可以自己再试。
 */
function DismissJobDialog({
  job,
  busy,
  onClose,
  onConfirm,
}: {
  job: JobView;
  busy: boolean;
  onClose: () => void;
  onConfirm: (muteSource: boolean) => void;
}) {
  const muteLabel = AUTO_RECREATED_JOB_TYPES[job.job_type];
  const [muteSource, setMuteSource] = useState(muteLabel != null);
  const title = job.subject || JOB_TYPE_LABELS[job.job_type] || job.job_type;

  return (
    <Modal open onClose={busy ? () => {} : onClose} label="忽略任务" topmost>
      <div className="p-6 max-md:p-5">
        <h2 className="text-title-sm font-bold text-white">忽略这个任务？</h2>
        <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">
          它会从「需要处理」下架，移到「已结束」。任务记录、失败原因和重试入口都还在，
          随时可以撤销忽略。
        </p>
        <p className="mt-3 break-words rounded-xl border border-white/[0.08] bg-white/[0.035] px-3.5 py-3 text-sub leading-6 text-white/75">
          {title}
        </p>
        {muteLabel && (
          <label
            className={`mt-4 flex cursor-pointer items-start gap-3 rounded-xl border px-3.5 py-3 transition ${
              muteSource
                ? "border-white/[0.16] bg-white/[0.07]"
                : "border-white/[0.08] bg-white/[0.025] hover:bg-white/[0.045]"
            }`}
          >
            <input
              type="checkbox"
              checked={muteSource}
              onChange={(event) => setMuteSource(event.target.checked)}
              className="mt-1 size-4 shrink-0 accent-white/70"
            />
            <span className="min-w-0">
              <span className="block text-ui font-semibold text-white/90">{muteLabel}</span>
              <span className="mt-0.5 block text-caption leading-5 text-white/50">
                不勾选的话，下次扫描媒体库时系统还会自动重新生成一次、再失败一次。
                手动生成不受影响，你随时可以自己再试。
              </span>
            </span>
          </label>
        )}
        <div className="mt-5 flex justify-end gap-2.5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-ui text-white/80 transition hover:bg-white/[0.1] disabled:opacity-40"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => onConfirm(muteSource)}
            disabled={busy}
            className="rounded-lg bg-white/90 px-4 py-2 text-ui font-medium text-black transition hover:bg-white disabled:opacity-40"
          >
            {busy ? "正在忽略…" : "忽略"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 本次作业实际新增的文件：单文件直接展示，多文件保留首项并允许展开核对。 */
function IngestImportedFiles({ files }: { files: string[] }) {
  if (files.length === 1) {
    return (
      <p className="mt-2.5 flex min-w-0 items-center gap-2 rounded-lg border border-white/[0.07] bg-white/[0.03] px-3 py-2 text-caption">
        <span className="shrink-0 text-white/40">本次新增文件</span>
        <OverflowText className="font-medium text-white/65">{files[0]}</OverflowText>
      </p>
    );
  }

  return (
    <details className="group mt-2.5 rounded-lg border border-white/[0.07] bg-white/[0.03]">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-caption [&::-webkit-details-marker]:hidden">
        <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-open:rotate-90" />
        <span className="shrink-0 text-white/40">本次新增 {files.length} 个文件</span>
        <OverflowText className="font-medium text-white/65">{files[0]}</OverflowText>
      </summary>
      <ul className="space-y-1 border-t border-white/[0.06] px-3 py-2.5 text-caption text-white/55">
        {files.map((file) => (
          <li key={file} title={file} className="break-all">
            {file}
          </li>
        ))}
      </ul>
    </details>
  );
}

function UsageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-micro text-white/30">{label}</p>
      <p className="tnum mt-1 text-sub font-semibold text-white/70">{value}</p>
    </div>
  );
}

/**
 * 侧栏「活动」入口。
 *
 * 角标只表达**当前最该被看见的那件事**，点击就直达那件事：有失败/阻塞的任务
 * 时亮警示色并落到任务视角的「需要处理」，只有进行中任务时落到「进行中」。
 * 角标数的是任务，落点就必须是任务视角对应的那一片——把人丢到「观看」再让他
 * 自己找"为什么有提醒"，等于让提醒自证失败。角标为空时才回到页面默认的观看。
 *
 * 计数走 useTaskActivity 这份共享口径，与活动页里的选项卡数字保证一致。
 */
export function JobCenter({ collapsed, active = false }: { collapsed: boolean; active?: boolean }) {
  const router = useRouter();
  const { isAdmin } = usePermissions();
  const activity = useTaskActivity();
  if (!isAdmin) return null;
  const { alert, count: badgeCount, href, hint } = taskActivityBadge(activity);
  return (
    <button
      type="button"
      onClick={() => router.push(href as Route)}
      data-active={active}
      title={collapsed ? `活动（${hint}）` : undefined}
      className={`glass-row nav-item py-2 max-md:py-2.5 ${collapsed ? "justify-center px-0" : "px-3"}`}
    >
      <span className="relative shrink-0">
        <ActivityIcon className="size-[18px] max-md:size-[22px]" />
        {badgeCount > 0 && (
          <span
            className={`absolute -right-1 -top-1 size-2 rounded-full ${
              alert ? "bg-[var(--danger)]" : "bg-[var(--info)]"
            }`}
          />
        )}
      </span>
      {!collapsed && (
        <>
          <span className="flex-1 text-ui font-medium">活动</span>
          {badgeCount > 0 && (
            <span
              className={`rounded-full px-1.5 py-0.5 text-[11px] font-semibold leading-none ${
                alert
                  ? "bg-[var(--danger)]/20 text-[var(--danger)]"
                  : "bg-[var(--info)]/20 text-[var(--info)]"
              }`}
            >
              {badgeCount}
            </span>
          )}
        </>
      )}
    </button>
  );
}
