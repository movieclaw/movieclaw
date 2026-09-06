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
 * 由后端给出名称、用途、占用、能否清理，前端只负责分组渲染与确认交互——
 * 业务新增一种缓存不需要改这里。
 *
 * 三块内容：
 *   1. 磁盘概览：data/ 所在磁盘的分段条（不可回收数据 / 可回收缓存 / 其他 / 剩余），
 *      一眼看出「能省多少」；
 *   2. 可清理的缓存：每行两种动作——「清理孤儿」（媒体库里已不存在的条目，无损）
 *      与「全部清空」（重建代价高的目录用红色警示并二次确认）；
 *   3. 应用数据：只展示占用与说明，不提供删除（用户资产或回退恢复源）。
 *   另有「未登记目录」块，只在后端发现登记表之外的条目时出现，提示反馈给开发者。
 */

type Pending = { key: string; mode: CleanMode } | null;

export function AppStorageSection() {
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ key: string; text: string } | null>(
    null,
  );

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
      setNotice({ key, text: parts.join("，") });
      await load(false);
    } catch (e) {
      setNotice({ key, text: e instanceof Error ? e.message : "清理失败" });
    } finally {
      setBusyKey(null);
    }
  };

  const cacheDirs = usage?.dirs.filter((d) => d.group === "cache") ?? [];
  const dataDirs = usage?.dirs.filter((d) => d.group === "data") ?? [];

  return (
    <div className="space-y-5">
      <DiskOverview
        usage={usage}
        loading={loading}
        error={error}
        onRefresh={() => load(true)}
      />

      {usage && usage.unregistered.length > 0 && (
        <section>
          <h3 className="group-label mb-2.5 px-1">未登记目录</h3>
          <div className="rounded-xl border border-amber-300/25 bg-amber-400/10 px-4 py-3">
            <p className="text-sub text-amber-100/90">
              数据目录下出现了程序未登记的条目。它们不会被自动统计或清理，请把下面的路径
              反馈给开发者。
            </p>
            <ul className="mt-2 space-y-1">
              {usage.unregistered.map((u) => (
                <li
                  key={u.path}
                  className="flex justify-between gap-3 text-sub"
                >
                  <span className="truncate font-mono text-amber-100/80">
                    {u.path}
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
        <h3 className="group-label mb-2.5 px-1">可清理的缓存</h3>
        <div className="css-glass divide-y divide-white/[0.06] !rounded-2xl">
          {cacheDirs.map((d) => (
            <DirRow
              key={d.key}
              dir={d}
              busy={busyKey === d.key}
              pending={pending?.key === d.key ? pending.mode : null}
              notice={notice?.key === d.key ? notice.text : null}
              onAsk={(mode) => setPending({ key: d.key, mode })}
              onCancel={() => setPending(null)}
              onConfirm={(mode) => void doClean(d.key, mode)}
            />
          ))}
          {!loading && cacheDirs.length === 0 && (
            <p className="px-5 py-4 text-sub text-[var(--text-muted)]">
              暂无数据
            </p>
          )}
        </div>
      </section>

      <section>
        <h3 className="group-label mb-2.5 px-1">应用数据</h3>
        <p className="mb-2.5 px-1 text-sub text-[var(--text-muted)]">
          用户资产与运行状态，只展示占用，不提供删除。
        </p>
        <div className="css-glass divide-y divide-white/[0.06] !rounded-2xl">
          {dataDirs.map((d) => (
            <DirRow
              key={d.key}
              dir={d}
              busy={false}
              pending={null}
              notice={null}
            />
          ))}
        </div>
      </section>
    </div>
  );
}

/** 磁盘概览：分段条 + 统计时间与刷新。 */
function DiskOverview({
  usage,
  loading,
  error,
  onRefresh,
}: {
  usage: StorageUsage | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const total = usage?.disk_total ?? 0;
  const cache = usage?.cache_bytes ?? 0;
  const data = usage?.data_bytes ?? 0;
  const free = usage?.disk_free ?? 0;
  // data/ 之外的其它占用（系统、其它应用），= 已用 - 本应用数据 - 缓存，负数按 0
  const other = Math.max(0, (usage?.disk_used ?? 0) - cache - data);
  const pct = (n: number) => (total > 0 ? `${(n / total) * 100}%` : "0%");

  return (
    <section>
      <h3 className="group-label mb-2.5 px-1">磁盘概览</h3>
      <div className="css-glass !rounded-2xl px-5 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-ui font-medium text-[var(--text)]">
              数据目录所在磁盘
              {usage && (
                <span className="ml-2 text-sub font-normal text-[var(--text-muted)]">
                  剩余 {formatBytes(free)} / 共 {formatBytes(total)}
                </span>
              )}
            </p>
            <p className="mt-0.5 truncate font-mono text-sub text-[var(--text-faint)]">
              {usage?.data_root ?? ""}
            </p>
          </div>
          <span className="flex items-center gap-2 text-sub text-[var(--text-muted)]">
            {usage && (
              <span>
                统计于{" "}
                {formatRelativeTime(
                  new Date(usage.computed_at * 1000).toISOString(),
                )}
              </span>
            )}
            <button
              type="button"
              onClick={onRefresh}
              disabled={loading}
              aria-label="重新统计"
              className="btn-glass flex items-center gap-1.5 px-3 py-1.5 text-sub font-medium disabled:opacity-50"
            >
              <RefreshIcon
                className={`size-[14px] ${loading ? "animate-spin" : ""}`}
              />
              {loading ? "统计中…" : "刷新"}
            </button>
          </span>
        </div>

        <div className="mt-3 flex h-2.5 w-full overflow-hidden rounded-full bg-white/[0.08]">
          <div
            className="bg-sky-400/80"
            style={{ width: pct(data) }}
            title="应用数据"
          />
          <div
            className="bg-amber-400/80"
            style={{ width: pct(cache) }}
            title="可回收缓存"
          />
          <div
            className="bg-white/25"
            style={{ width: pct(other) }}
            title="其他占用"
          />
        </div>
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sub text-[var(--text-muted)]">
          <Legend color="bg-sky-400/80" label="应用数据" value={data} />
          <Legend color="bg-amber-400/80" label="可回收缓存" value={cache} />
          <Legend color="bg-white/25" label="其他占用" value={other} />
          <Legend color="bg-white/[0.08]" label="剩余" value={free} />
        </div>
        {error && <p className="mt-2 text-sub text-red-300/90">{error}</p>}
      </div>
    </section>
  );
}

function Legend({
  color,
  label,
  value,
}: {
  color: string;
  label: string;
  value: number;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`inline-block size-2 rounded-full ${color}`} />
      {label}
      <span className="tnum text-[var(--text)]">{formatBytes(value)}</span>
    </span>
  );
}

/** 一行目录：名称 + 说明 + 占用 + 动作；确认条与结果提示内联在行内。 */
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
  notice: string | null;
  onAsk?: (mode: CleanMode) => void;
  onCancel?: () => void;
  onConfirm?: (mode: CleanMode) => void;
}) {
  const expensive = dir.rebuild_cost === "expensive";
  const canAct = dir.group === "cache" && onAsk && onConfirm && onCancel;
  return (
    <div className="px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3 sm:flex-nowrap">
        <div className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="text-ui font-medium text-[var(--text)]">
              {dir.title}
            </span>
            <Tooltip
              content={
                <>
                  <p>{dir.description}</p>
                  <p className="mt-1.5 font-mono text-[var(--text-muted)]">
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
                className="flex text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
              >
                <InfoIcon className="size-[15px]" />
              </button>
            </Tooltip>
          </span>
          <p className="mt-0.5 line-clamp-2 text-sub text-[var(--text-muted)]">
            {dir.description}
          </p>
        </div>
        <span className="flex shrink-0 items-center gap-2">
          <span className="tnum w-20 text-right text-body font-medium text-[var(--text)]">
            {dir.exists ? formatBytes(dir.bytes) : "—"}
          </span>
          {canAct && dir.orphan_aware && (
            <button
              type="button"
              onClick={() => onAsk("orphans")}
              disabled={busy || !dir.exists}
              className="btn-glass px-3 py-1.5 text-sub font-medium disabled:opacity-50"
            >
              清理孤儿
            </button>
          )}
          {canAct && dir.clearable && (
            <button
              type="button"
              onClick={() => onAsk("all")}
              disabled={busy || !dir.exists}
              className={`btn-glass px-3 py-1.5 text-sub font-medium disabled:opacity-50 ${
                expensive ? "text-red-300/90 hover:text-red-200" : ""
              }`}
            >
              {busy ? "清理中…" : "全部清空"}
            </button>
          )}
        </span>
      </div>

      {pending && canAct && (
        <div
          className={`mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3 ${
            pending === "all" && expensive
              ? "border-red-300/25 bg-red-400/10"
              : "border-white/[0.1] bg-white/[0.05]"
          }`}
        >
          <p
            className={`text-sub ${pending === "all" && expensive ? "text-red-200/90" : "text-[var(--text)]"}`}
          >
            {pending === "orphans"
              ? `只删除媒体库里已不存在的条目，不影响正在使用的内容。确认清理「${dir.title}」的孤儿条目？`
              : expensive
                ? `「${dir.title}」重建代价较高，清空后需要重新生成。确认全部清空？`
                : `确认清空「${dir.title}」？${dir.description}`}
          </p>
          <span className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => onConfirm(pending)}
              className="btn-accent rounded-full px-3.5 py-1.5 text-sub font-semibold"
            >
              确认
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="btn-glass px-3 py-1.5 text-sub font-medium"
            >
              取消
            </button>
          </span>
        </div>
      )}
      {notice && (
        <p className="mt-2 text-sub text-[var(--text-muted)]">{notice}</p>
      )}
    </div>
  );
}
