"use client";

import { useCallback, useEffect, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { useConfirm } from "@/components/feedback";
import { InfoIcon, MoreIcon, RefreshIcon } from "@/components/icons";
import { Tooltip } from "@/components/tooltip";
import {
  cleanStorage,
  getStorageState,
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
 * 打开页面**不会**触发一次实时统计：遍历整个 data/ 在大库上要几十秒，所以后端
 * 给的是「上一次的快照 + 是否正在重算」，页面秒开并标出「统计于 N 分钟前」。
 * 要最新数字就点刷新：按钮转成「统计中」，旧数据继续留在页面上，后台算完
 * （轮询到 computing 变假）再整体替换，不会中途闪成空白或加载态。
 *
 * 清理的二次确认走全站统一的 useConfirm 弹窗（feedback.tsx），不用行内确认条：
 * 确认条会把下面的行整体推走，而且 ⋯ 菜单在列表靠下时它常常落在视口外——手机上
 * 尤其明显；弹窗还自带焦点陷阱、Esc 与点击外部关闭。
 *
 * 版式（手机优先）：一行三段「名称 + 一句话用途 | 占用 | ⋯」。清理动作全部收进
 * 行尾的 ⋯ 菜单——两个并排的文字按钮在 390px 宽的屏幕上会把名称挤成「图…」，
 * 而清理是低频动作，不值得常驻这么宽的位置；占用数字定宽右对齐，是每行的视觉
 * 锚点。「重建代价高」徽章挪到第二行与一句话用途同列，保证第一行永远是完整的
 * 目录名；完整说明与真实路径收进名称旁的信息图标（触屏点按也能展开）。三块内容：
 *   1. 磁盘概览：data/ 所在磁盘的分段条（应用数据 / 可回收缓存 / 其他 / 剩余）；
 *   2. 可清理的缓存：「清理孤儿条目」（媒体库里已不存在的条目，无损）与「全部清空」
 *      （重建代价高的目录在确认弹窗里标红警示）；
 *   3. 应用数据：只展示占用（用户资产或回退恢复源）。
 *   「未登记目录」块只在后端发现登记表之外的条目时出现。
 */

type Notice = { key: string; text: string; ok: boolean } | null;

/** 后台统计期间的轮询间隔 */
const POLL_MS = 2000;

export function AppStorageSection() {
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [computing, setComputing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const confirm = useConfirm();

  /** 读一次状态：拿到新快照才替换页面数据，没算完就只更新「统计中」标记。 */
  const load = useCallback(async (refresh: boolean) => {
    try {
      const state = await getStorageState(refresh);
      if (state.usage) setUsage(state.usage);
      setComputing(state.computing);
      setError(state.error);
    } catch (e) {
      setComputing(false);
      setError(e instanceof Error ? e.message : "读取占用信息失败");
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  // 后台正在统计时轮询，等新数据落地后自动替换页面上的旧数据
  useEffect(() => {
    if (!computing) return;
    const timer = setInterval(() => void load(false), POLL_MS);
    return () => clearInterval(timer);
  }, [computing, load]);

  /** 先弹确认，用户点了确认才真的清理。文案按模式与重建代价分档。 */
  const askClean = async (dir: DirUsage, mode: CleanMode) => {
    const expensive = dir.rebuild_cost === "expensive";
    const ok = await confirm(
      mode === "orphans"
        ? {
            title: `清理「${dir.title}」的孤儿条目？`,
            description: "只删除媒体库里已不存在的条目，正在使用的内容不受影响。",
            confirmLabel: "清理孤儿条目",
          }
        : {
            title: `清空「${dir.title}」？`,
            // 登记表的完整说明本身就写清了用途与重建代价，直接当后果说明；
            // 重建代价高的那几个另外靠红色确认键（tone: danger）加重提醒
            description: dir.description,
            confirmLabel:
              dir.bytes > 0 ? `清空并释放 ${formatBytes(dir.bytes)}` : "全部清空",
            tone: expensive ? "danger" : "default",
          },
    );
    if (ok) await doClean(dir.key, mode);
  };

  const doClean = async (key: string, mode: CleanMode) => {
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
      // 清理让后端快照标脏：这一次读取会拉起后台重算，占用数字随后被轮询替换
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

  // 数据是「上一次统计的结果」，因此时间与进行中状态必须始终摆在标题栏上；
  // 手机上这一行还要和刷新按钮挤在一起，文案保持短，超长时截断而不是挤走按钮。
  const computedAt = usage
    ? formatRelativeTime(new Date(usage.computed_at * 1000).toISOString())
    : null;
  const statusText = computedAt
    ? `统计于 ${computedAt}${computing ? " · 更新中" : ""}`
    : computing
      ? "首次统计中，可能要几十秒…"
      : "尚未统计";

  return (
    <div className="space-y-6">
      <section>
        <SectionHeader label="磁盘概览">
          <span className="truncate text-caption text-[var(--text-faint)]">
            {statusText}
          </span>
          <button
            type="button"
            onClick={() => void load(true)}
            disabled={computing}
            className="btn-glass shrink-0 gap-1 px-2.5 py-1 text-caption font-medium disabled:opacity-50"
          >
            <RefreshIcon
              className={`size-3 ${computing ? "animate-spin" : ""}`}
            />
            {computing ? "统计中" : "刷新"}
          </button>
        </SectionHeader>
        <DiskOverview usage={usage} error={error} />
      </section>

      {usage && usage.unregistered.length > 0 && (
        <section>
          <SectionHeader label="未登记目录" />
          <div className="rounded-2xl border border-amber-300/20 bg-amber-400/[0.07] px-4 py-4 sm:px-5">
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
          {!usage ? (
            <SkeletonRows count={4} />
          ) : (
            cacheDirs.map((d) => (
              <DirRow
                key={d.key}
                dir={d}
                busy={busyKey === d.key}
                notice={notice?.key === d.key ? notice : null}
                onClean={(mode) => void askClean(d, mode)}
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
          {!usage ? (
            <SkeletonRows count={6} />
          ) : (
            dataDirs.map((d) => (
              <DirRow
                key={d.key}
                dir={d}
                busy={false}
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
      <h3 className="group-label shrink-0">{label}</h3>
      {children && (
        <span className="flex min-w-0 items-center gap-2.5">{children}</span>
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
    <div className="css-glass !rounded-2xl px-4 py-4 sm:px-5">
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

/** 一行目录：名称 + 一句话用途 | 占用 | ⋯ 菜单；清理结果提示内联在行下。 */
function DirRow({
  dir,
  busy,
  notice,
  onClean,
}: {
  dir: DirUsage;
  busy: boolean;
  notice: Notice;
  /** 只有可清理的目录传；点菜单项即发起确认（弹窗在上层） */
  onClean?: (mode: CleanMode) => void;
}) {
  const expensive = dir.rebuild_cost === "expensive";
  const actionable = dir.group === "cache" && !!onClean;
  return (
    <div className="px-4 py-3 sm:px-5">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="truncate text-ui font-medium text-[var(--text)]">
              {dir.title}
            </span>
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
              openOnClick
            >
              <button
                type="button"
                aria-label={`「${dir.title}」的说明`}
                className="flex shrink-0 text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
              >
                <InfoIcon className="size-[14px]" />
              </button>
            </Tooltip>
          </span>
          <span className="mt-0.5 flex items-center gap-1.5 text-caption text-[var(--text-faint)]">
            {expensive && (
              <span className="shrink-0 rounded-full bg-amber-400/10 px-1.5 py-px text-micro font-medium text-amber-300/90">
                重建代价高
              </span>
            )}
            <span className="truncate">{dir.summary}</span>
          </span>
        </div>
        <span
          className={`tnum shrink-0 text-right text-ui font-semibold ${
            dir.exists && dir.bytes > 0
              ? "text-[var(--text)]"
              : "text-[var(--text-faint)]"
          }`}
        >
          {dir.exists ? formatBytes(dir.bytes) : "—"}
        </span>
        {actionable && (
          <RowActionsMenu
            dir={dir}
            busy={busy}
            expensive={expensive}
            onClean={onClean}
          />
        )}
      </div>

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

/**
 * 行尾 ⋯ 菜单：两个清理动作收在这里。
 *
 * 与媒体库列表行（library-manage-row.tsx）用同一套 Radix DropdownMenu 与样式，
 * Portal 渲染保证浮层不被卡片的 overflow/backdrop-filter 裁掉。清理进行中时
 * 图标换成转圈并禁用入口，行内不再需要「清理中…」这样的长文案占位。
 */
function RowActionsMenu({
  dir,
  busy,
  expensive,
  onClean,
}: {
  dir: DirUsage;
  busy: boolean;
  expensive: boolean;
  onClean: (mode: CleanMode) => void;
}) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={`「${dir.title}」的清理操作`}
          disabled={busy || !dir.exists}
          className="grid size-8 shrink-0 place-items-center rounded-full border border-white/[0.09] bg-white/[0.04] text-white/80 transition hover:bg-white/[0.1] hover:text-white disabled:opacity-40 data-[state=open]:bg-white/[0.14] data-[state=open]:text-white"
        >
          {busy ? (
            <RefreshIcon className="size-4 animate-spin" />
          ) : (
            <MoreIcon className="size-[18px]" />
          )}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[10rem] p-1"
        >
          {dir.orphan_aware && (
            <DropdownMenu.Item
              onSelect={() => onClean("orphans")}
              className={itemClass}
            >
              清理孤儿条目
            </DropdownMenu.Item>
          )}
          {dir.clearable && (
            <DropdownMenu.Item
              onSelect={() => onClean("all")}
              className={`${itemClass}${expensive ? " !text-red-300/90" : ""}`}
            >
              全部清空
            </DropdownMenu.Item>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function SkeletonRows({ count }: { count: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3.5 sm:px-5">
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
