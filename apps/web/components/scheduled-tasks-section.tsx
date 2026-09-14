"use client";

import { useCallback, useEffect, useState } from "react";

import { listLibraries } from "@/lib/api/libraries";
import {
  listScheduledTasks,
  type ScheduledTask,
  type ScheduledTaskUpdate,
  updateScheduledTask,
} from "@/lib/api/scheduled-tasks";
import {
  RECONCILE_NETWORK_SUGGESTED_SECONDS,
  dailyCron,
  dailyTimeOf,
  describeSchedule,
  suggestReconcileInterval,
} from "@/lib/scheduled-tasks";
import { formatDateTime, formatRelativeTime } from "@/lib/time";

/**
 * 设置 → 应用 → 定时任务：每个后台任务的周期与启停。
 *
 * 周期一直落在库里、此前却没有入口能改。这里给两种人能说清的形状——
 * 「每 N 小时」与「每天固定时刻」——其它 cron 照样能显示与保存（改表达式）。
 * 「媒体库对账」多一条建议：有库在网络挂载上（实时监控收不到变化，对账是唯一
 * 的变更感知）且周期比一小时长，就提示调到一小时——增量对账之后一轮只要几秒，
 * 频繁一点换来"新文件更快出现"。只建议，不替用户改。
 */
export function ScheduledTasksSection() {
  const [tasks, setTasks] = useState<ScheduledTask[] | null>(null);
  const [anyNetwork, setAnyNetwork] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const reload = useCallback(() => {
    Promise.all([listScheduledTasks(), listLibraries().catch(() => [])])
      .then(([rows, libs]) => {
        setTasks(rows);
        setAnyNetwork(libs.some((lib) => lib.network_mount));
        setError(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "加载失败"));
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);

  const save = useCallback(async (task: ScheduledTask, body: ScheduledTaskUpdate) => {
    setBusyKey(task.key);
    try {
      const updated = await updateScheduledTask(task.key, body);
      setTasks((prev) => prev?.map((t) => (t.key === task.key ? updated : t)) ?? null);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusyKey(null);
    }
  }, []);

  const reconcile = tasks?.find((t) => t.key === "library_reconcile") ?? null;
  const suggest = reconcile !== null && suggestReconcileInterval(reconcile, anyNetwork);

  return (
    <div className="space-y-5">
      <p className="text-sub text-[var(--text-muted)]">
        后台任务各自按周期运行；改动立即生效，不用重启。周期与启停按任务记在服务器上。
      </p>
      {error && <p className="text-sub text-[var(--danger)]">{error}</p>}
      {suggest && reconcile && (
        <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.05] px-4 py-3">
          <p className="min-w-0 flex-1 text-sub">
            有媒体库放在网络挂载上：实时监控收不到远端变化，新文件全靠「媒体库对账」发现。
            现在是{describeSchedule(reconcile)}，建议调到每 1 小时——增量对账通常只需几秒。
          </p>
          <button
            type="button"
            className="btn-glass px-3 py-1.5 text-sub"
            disabled={busyKey === reconcile.key}
            onClick={() =>
              save(reconcile, {
                enabled: true,
                trigger_type: "interval",
                interval_seconds: RECONCILE_NETWORK_SUGGESTED_SECONDS,
              })
            }
          >
            调到每 1 小时
          </button>
        </div>
      )}
      {tasks === null && !error && (
        <p className="text-sub text-[var(--text-muted)]">正在加载…</p>
      )}
      <div className="space-y-3">
        {tasks?.map((task) => (
          <TaskRow key={task.key} task={task} busy={busyKey === task.key} onSave={save} />
        ))}
      </div>
    </div>
  );
}

type Mode = "interval" | "daily" | "cron";

function modeOf(task: ScheduledTask): Mode {
  if (task.trigger_type === "interval") return "interval";
  return dailyTimeOf(task.cron_expr) ? "daily" : "cron";
}

function TaskRow({
  task,
  busy,
  onSave,
}: {
  task: ScheduledTask;
  busy: boolean;
  onSave: (task: ScheduledTask, body: ScheduledTaskUpdate) => Promise<void>;
}) {
  const [mode, setMode] = useState<Mode>(() => modeOf(task));
  const [hours, setHours] = useState(() =>
    task.interval_seconds ? Math.max(1, Math.round(task.interval_seconds / 3600)) : 6,
  );
  const [time, setTime] = useState(() => {
    const daily = dailyTimeOf(task.cron_expr);
    return daily
      ? `${String(daily.hour).padStart(2, "0")}:${String(daily.minute).padStart(2, "0")}`
      : "03:00";
  });
  const [cron, setCron] = useState(task.cron_expr ?? "");
  // 服务器那份变了（保存成功 / 别处改了）就把编辑态对齐回去
  useEffect(() => {
    setMode(modeOf(task));
    if (task.interval_seconds) setHours(Math.max(1, Math.round(task.interval_seconds / 3600)));
    const daily = dailyTimeOf(task.cron_expr);
    if (daily) {
      setTime(
        `${String(daily.hour).padStart(2, "0")}:${String(daily.minute).padStart(2, "0")}`,
      );
    }
    setCron(task.cron_expr ?? "");
  }, [task]);

  const draft: ScheduledTaskUpdate =
    mode === "interval"
      ? { enabled: task.enabled, trigger_type: "interval", interval_seconds: hours * 3600 }
      : mode === "daily"
        ? {
            enabled: task.enabled,
            trigger_type: "cron",
            cron_expr: dailyCron(Number(time.slice(0, 2)), Number(time.slice(3, 5))),
          }
        : { enabled: task.enabled, trigger_type: "cron", cron_expr: cron.trim() };
  const dirty =
    draft.trigger_type !== task.trigger_type ||
    (draft.trigger_type === "interval"
      ? draft.interval_seconds !== task.interval_seconds
      : draft.cron_expr !== task.cron_expr);

  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-ui font-medium">{task.title}</span>
            <span className="text-sub text-[var(--text-muted)]">{describeSchedule(task)}</span>
          </div>
          {task.description && (
            <p className="mt-0.5 text-sub text-[var(--text-muted)]">{task.description}</p>
          )}
          <p className="mt-1 text-sub text-[var(--text-muted)]">
            上次 {task.last_run_at ? formatRelativeTime(task.last_run_at) : "还没跑过"}
            {task.enabled && task.next_run_at ? ` · 下次 ${formatDateTime(task.next_run_at)}` : ""}
            {!task.enabled ? " · 已停用" : ""}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={task.enabled}
          aria-label={task.enabled ? "停用" : "启用"}
          disabled={busy}
          onClick={() => onSave(task, { ...draft, enabled: !task.enabled })}
          className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
            task.enabled ? "bg-[var(--accent)]" : "bg-white/20"
          }`}
        >
          <span
            className={`absolute top-0.5 size-5 rounded-full bg-white transition-transform ${
              task.enabled ? "translate-x-5" : "translate-x-0.5"
            }`}
          />
        </button>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-sub">
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as Mode)}
          className="rounded-lg bg-white/[0.08] px-2 py-1"
          aria-label="周期方式"
        >
          <option value="interval">每隔</option>
          <option value="daily">每天固定时刻</option>
          <option value="cron">cron 表达式</option>
        </select>
        {mode === "interval" && (
          <label className="flex items-center gap-1.5">
            <input
              type="number"
              min={1}
              max={168}
              value={hours}
              onChange={(e) => setHours(Math.max(1, Math.min(168, Number(e.target.value) || 1)))}
              className="w-16 rounded-lg bg-white/[0.08] px-2 py-1"
              aria-label="间隔小时数"
            />
            小时
          </label>
        )}
        {mode === "daily" && (
          <input
            type="time"
            value={time}
            onChange={(e) => setTime(e.target.value || "03:00")}
            className="rounded-lg bg-white/[0.08] px-2 py-1"
            aria-label="每天几点"
          />
        )}
        {mode === "cron" && (
          <input
            type="text"
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            placeholder="分 时 日 月 周，如 0 3 * * *"
            className="w-56 rounded-lg bg-white/[0.08] px-2 py-1 font-mono"
            aria-label="cron 表达式"
          />
        )}
        <button
          type="button"
          className="btn-glass px-3 py-1 disabled:opacity-40"
          disabled={busy || !dirty}
          onClick={() => onSave(task, draft)}
        >
          {busy ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}
