"use client";

import { useCallback, useEffect, useState } from "react";

import { InfoIcon, RefreshIcon } from "@/components/icons";
import { Tooltip } from "@/components/tooltip";
import {
  cleanStorage,
  getStorageUsage,
  type CleanMode,
  type DirUsage,
  type StorageUsage,
} from "@/lib/api/storage";
import { formatBytes } from "@/lib/format";
import { formatRelativeTime } from "@/lib/time";

/**
 * 缓存管理（设置 → 更新与维护，「缓存管理」标签）。
 *
 * 这一页是后端登记表（services/storage/registry.py）的视图：每个 data/ 下的目录
 * 由后端给出名称、一句话用途、完整说明、占用、能否清理，前端只负责分组渲染与
 * 确认交互——业务新增一种缓存不需要改这里。
 *
 * 版式：每一行固定三列「名称 + 一句话用途 | 占用 | 动作」，占用列定宽右对齐、
 * 动作列定宽，让数字与按钮在整组里竖向对齐；完整说明与真实路径收进标题旁的
 * 信息图标里，行内不堆长段落。三块内容：
 *   1. 磁盘概览：data/ 所在磁盘的分段条（应用数据 / 可回收缓存 / 其他 / 剩余）；
 *   2. 可清理的缓存：「清理孤儿」（媒体库里已不存在的条目，无损）与「全部清空」
 *      （重建代价高的目录标红并二次确认）；
 *   3. 应用数据：只展示占用（用户资产或回退恢复源）。
 *   「未登记目录」块只在后端发现登记表之外的条目时出现。
 */

type Pending = { key: string; mode: CleanMode } | null;
type Notice = { key: string; text: string; ok: boolean } | null;

export function AppStorageSection() {
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  const load = useCallback(async (refresh: boolean) => {
    setLoading(true);
    setError(null);
    try {
      setUsage(await getStorageUsage(refresh));
    } catch (e) {
      setError(e instanceof Error ? e.message : "读取占用信息失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  const doClean = async (key: string, mode: CleanMode) => {
    setPending(null);
    setBusyKey(key);
    setNotice(null);
    try {
      const result = await cleanStorage(key, mode);
      const parts = [
        `已释放 ${formatBytes(result.freed_bytes)}，删除 ${result.removed} 项`,
      ];
      if (result.skipped_busy > 0)
        parts.push(`跳过正在使用的 ${result.skipped_busy} 项`);
      setNotice({ key, text: parts.join("，"), ok: true });
      await load(false);
    } catch (e) {
      setNotice({
        key,
        text: e instanceof Error ? e.message : "清理失败",
        ok: false,
      });
    } finally {
      setBusyKey(null);
    }
  };

  const cacheDirs = usage?.dirs.filter((d) => d.group === "cache") ?? [];
  const dataDirs = usage?.dirs.filter((d) => d.group === "data") ?? [];

  return (
    <div className="space-y-6">
      <section>
        <SectionHeader label="磁盘概览">
          {usage && (
            <span className="text-caption text-[var(--text-faint)]">
              统计于{" "}
              {formatRelativeTime(
                new Date(usage.computed_at * 1000).toISOString(),
              )}
            </span>
          )}
          <button
            type="button"
            onClick={() => void load(true)}
            disabled={loading}
            className="btn-glass gap-1 px-2.5 py-1 text-caption font-medium disabled:opacity-50"
          >
            <RefreshIcon
              className={`size-3 ${loading ? "animate-spin" : ""}`}
            />
            {loading ? "统计中" : "刷新"}
          </button>
        </SectionHeader>
        <DiskOverview usage={usage} error={error} />
      </section>

      {usage && usage.unregistered.length > 0 && (
        <section>
          <SectionHeader label="未登记目录" />
          <div className="rounded-2xl border border-amber-300/20 bg-amber-400/[0.07] px-5 py-4">
            <p className="text-sub text-amber-100/85">
              数据目录下出现了程序未登记的条目，不会被统计或清理。请把路径反馈给开发者。
            </p>
            <ul className="mt-2.5 space-y-1.5">
              {usage.unregistered.map((u) => (
                <li
                  key={u.path}
                  className="flex items-center justify-between gap-4 text-sub"
                >
                  <span
                    className="truncate font-mono text-amber-100/70"
                    title={u.path}
                  >
                    {relativeTo(u.path, usage.data_root)}
                  </span>
                  <span className="tnum shrink-0 text-amber-100/70">
                    {formatBytes(u.bytes)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </section>
      )}

      <section>
        <SectionHeader label="可清理的缓存">
          {usage && (
            <span className="tnum text-caption text-[var(--text-faint)]">
              合计 {formatBytes(usage.cache_bytes)}
            </span>
          )}
        </SectionHeader>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {loading && !usage ? (
            <SkeletonRows count={4} />
          ) : (
            cacheDirs.map((d) => (
              <DirRow
                key={d.key}
                dir={d}
                busy={busyKey === d.key}
                pending={pending?.key === d.key ? pending.mode : null}
                notice={notice?.key === d.key ? notice : null}
                onAsk={(mode) => setPending({ key: d.key, mode })}
                onCancel={() => setPending(null)}
                onConfirm={(mode) => void doClean(d.key, mode)}
              />
            ))
          )}
        </div>
      </section>

      <section>
        <SectionHeader label="应用数据">
          {usage && (
            <span className="tnum text-caption text-[var(--text-faint)]">
              合计 {formatBytes(usage.data_bytes)} · 只展示，不提供删除
            </span>
          )}
        </SectionHeader>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {loading && !usage ? (
            <SkeletonRows count={6} />
          ) : (
            dataDirs.map((d) => (
              <DirRow
                key={d.key}
                dir={d}
                busy={false}
                pending={null}
                notice={null}
              />
            ))
          )}
        </div>
      </section>
    </div>
  );
}

/** 分组标题行：左侧小标签，右侧可放合计/时间/刷新等次要信息。 */
function SectionHeader({
  label,
  children,
}: {
  label: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-2.5 flex items-center justify-between gap-3 px-1">
      <h3 className="group-label">{label}</h3>
      {children && (
        <span className="flex items-center gap-2.5">{children}</span>
      )}
    </div>
  );
}

/** 磁盘概览：剩余空间 + 分段条 + 图例。 */
function DiskOverview({
  usage,
  error,
}: {
  usage: StorageUsage | null;
  error: string | null;
}) {
  const total = usage?.disk_total ?? 0;
  const cache = usage?.cache_bytes ?? 0;
  const data = usage?.data_bytes ?? 0;
  const free = usage?.disk_free ?? 0;
  // data/ 之外的其它占用（系统、其它应用）= 已用 − 本应用数据 − 缓存，负数按 0
  const other = Math.max(0, (usage?.disk_used ?? 0) - cache - data);
  const pct = (n: number) => (total > 0 ? `${(n / total) * 100}%` : "0%");
  const segments = [
    { label: "应用数据", value: data, color: "bg-sky-400/85" },
    { label: "可回收缓存", value: cache, color: "bg-amber-400/85" },
    { label: "其他占用", value: other, color: "bg-white/20" },
    { label: "剩余", value: free, color: "bg-white/[0.07]" },
  ];

  return (
    <div className="css-glass !rounded-2xl px-5 py-4">
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          <p className="text-ui font-medium text-[var(--text)]">
            数据目录所在磁盘
          </p>
          <p
            className="mt-0.5 truncate font-mono text-caption text-[var(--text-faint)]"
            title={usage?.data_root}
          >
            {usage?.data_root ?? "…"}
          </p>
        </div>
        <div className="shrink-0 text-right">
          <p className="tnum text-title font-semibold leading-none text-[var(--text)]">
            {usage ? formatBytes(free) : "—"}
          </p>
          <p className="mt-1 tnum text-caption text-[var(--text-faint)]">
            剩余 · 共 {usage ? formatBytes(total) : "—"}
          </p>
        </div>
      </div>

      <div className="mt-4 flex h-2 w-full gap-px overflow-hidden rounded-full bg-white/[0.07]">
        {segments.slice(0, 3).map((s) => (
          <div
            key={s.label}
            className={`${s.color} transition-[width]`}
            style={{ width: pct(s.value) }}
          />
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5">
        {segments.map((s) => (
          <div
            key={s.label}
            className="flex items-center gap-2 whitespace-nowrap"
          >
            <span className={`size-2 shrink-0 rounded-full ${s.color}`} />
            <span className="text-caption text-[var(--text-muted)]">
              {s.label}
            </span>
            <span className="tnum text-sub font-medium text-[var(--text)]">
              {usage ? formatBytes(s.value) : "—"}
            </span>
          </div>
        ))}
      </div>
      {error && <p className="mt-3 text-sub text-red-300/90">{error}</p>}
    </div>
  );
}

/** 一行目录：名称 + 一句话用途 | 占用 | 动作；确认条与结果提示内联在行下。 */
function DirRow({
  dir,
  busy,
  pending,
  notice,
  onAsk,
  onCancel,
  onConfirm,
}: {
  dir: DirUsage;
  busy: boolean;
  pending: CleanMode | null;
  notice: Notice;
  onAsk?: (mode: CleanMode) => void;
  onCancel?: () => void;
  onConfirm?: (mode: CleanMode) => void;
}) {
  const expensive = dir.rebuild_cost === "expensive";
  const actionable =
    dir.group === "cache" && !!onAsk && !!onConfirm && !!onCancel;
  return (
    <div className="px-5 py-3">
      <div className="flex items-center gap-4">
        <div className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="truncate text-ui font-medium text-[var(--text)]">
              {dir.title}
            </span>
            {expensive && (
              <span className="shrink-0 rounded-full bg-amber-400/10 px-1.5 py-px text-micro font-medium text-amber-300/90">
                重建代价高
              </span>
            )}
            <Tooltip
              content={
                <>
                  <p>{dir.description}</p>
                  <p className="mt-1.5 break-all font-mono text-caption text-[var(--text-muted)]">
                    {dir.path}
                  </p>
                </>
              }
              placement="top"
              maxWidth={360}
            >
              <button
                type="button"
                aria-label="说明"
                className="flex shrink-0 text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
              >
                <InfoIcon className="size-[14px]" />
              </button>
            </Tooltip>
          </span>
          <p className="mt-0.5 truncate text-caption text-[var(--text-faint)]">
            {dir.summary}
          </p>
        </div>
        <span
          className={`tnum w-20 shrink-0 text-right text-sub font-medium ${
            dir.exists && dir.bytes > 0
              ? "text-[var(--text)]"
              : "text-[var(--text-faint)]"
          }`}
        >
          {dir.exists ? formatBytes(dir.bytes) : "—"}
        </span>
        {actionable && (
          <span className="flex w-[11.5rem] shrink-0 items-center justify-end gap-1.5">
            {dir.orphan_aware && (
              <button
                type="button"
                onClick={() => onAsk("orphans")}
                disabled={busy || !dir.exists}
                className="btn-glass px-2.5 py-1 text-caption font-medium"
              >
                清理孤儿
              </button>
            )}
            {dir.clearable && (
              <button
                type="button"
                onClick={() => onAsk("all")}
                disabled={busy || !dir.exists}
                className={`btn-glass px-2.5 py-1 text-caption font-medium ${
                  expensive ? "text-red-300/90 hover:text-red-200" : ""
                }`}
              >
                {busy ? "清理中…" : "全部清空"}
              </button>
            )}
          </span>
        )}
      </div>

      {pending && actionable && (
        <div
          className={`mt-2.5 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-3.5 py-2.5 ${
            pending === "all" && expensive
              ? "border-red-300/20 bg-red-400/[0.08]"
              : "border-white/[0.08] bg-white/[0.04]"
          }`}
        >
          <p
            className={`text-sub ${
              pending === "all" && expensive
                ? "text-red-200/90"
                : "text-[var(--text-muted)]"
            }`}
          >
            {pending === "orphans"
              ? `只删除媒体库里已不存在的条目，正在使用的内容不受影响。确认清理「${dir.title}」的孤儿条目？`
              : expensive
                ? `「${dir.title}」重建代价较高，清空后需要重新生成。确认全部清空？`
                : `确认清空「${dir.title}」？${dir.description}`}
          </p>
          <span className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={() => onConfirm(pending)}
              className="btn-accent rounded-full px-3 py-1 text-caption font-semibold"
            >
              确认
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="btn-glass px-2.5 py-1 text-caption font-medium"
            >
              取消
            </button>
          </span>
        </div>
      )}
      {notice && (
        <p
          className={`mt-1.5 text-caption ${notice.ok ? "text-emerald-300/85" : "text-red-300/90"}`}
        >
          {notice.text}
        </p>
      )}
    </div>
  );
}

function SkeletonRows({ count }: { count: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-5 py-3.5">
          <div className="flex-1 space-y-1.5">
            <div className="h-3 w-28 animate-pulse rounded bg-white/[0.08]" />
            <div className="h-2.5 w-48 animate-pulse rounded bg-white/[0.05]" />
          </div>
          <div className="h-3 w-14 animate-pulse rounded bg-white/[0.08]" />
        </div>
      ))}
    </>
  );
}

/** 未登记条目只显示相对 data/ 的短路径，完整路径放 title。 */
function relativeTo(path: string, root: string): string {
  const prefix = root.endsWith("/") ? root : `${root}/`;
  return path.startsWith(prefix) ? path.slice(prefix.length) : path;
}
