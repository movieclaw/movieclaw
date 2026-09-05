"use client";

import { useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { GripIcon, LockIcon, MoreIcon } from "@/components/icons";
import { LIBRARY_KIND_META } from "@/components/library-view";
import { type MediaLibrary, SCAN_PHASE_LABELS } from "@/lib/api/libraries";
import { publicEnv } from "@/lib/env";
import {
  type LibraryStatus,
  type LibraryStatusTone,
  accessLabel,
  inventoryLabel,
  libraryStatus,
} from "@/lib/library-manage";
import { LIBRARY_KIND_LABELS } from "@/lib/media-types";
import { formatRelativeTime } from "@/lib/time";

/** 桌面端表格的列宽：拖拽柄 / 库 / 根目录 / 库存 / 状态 / 可见范围 / 操作。
 *  表头与每一行共用同一份，列才能对齐。 */
export const MANAGE_GRID_COLS =
  "grid-cols-[28px_minmax(0,1.6fr)_minmax(0,1.4fr)_minmax(96px,.8fr)_minmax(180px,1.3fr)_minmax(84px,.6fr)_40px]";

/** 状态圆点：灰 = 空闲，蓝 = 任务进行中，黄 = 有待处理，红 = 有缺失。 */
const TONE_DOT: Record<LibraryStatusTone, string> = {
  idle: "bg-white/30",
  busy: "bg-[var(--info)] shadow-[0_0_0_3px_rgba(127,176,255,0.18)]",
  pending: "bg-[var(--warn)]",
  missing: "bg-[var(--danger)]",
};

/** 一行库能触发的全部操作；是否可用由行内按当前状态判定。 */
export interface LibraryRowActions {
  onToggleScan: (library: MediaLibrary) => void;
  onOpenPending: (library: MediaLibrary) => void;
  onOrganize: (library: MediaLibrary) => void;
  onToggleRefresh: (library: MediaLibrary) => void;
  onEdit: (library: MediaLibrary) => void;
  onSetDefault: (library: MediaLibrary) => void;
  onToggleHome: (library: MediaLibrary) => void;
  onDelete: (library: MediaLibrary) => void;
  /** 手机端没有拖拽：菜单里给「调整顺序」入口；桌面端不传则不渲染 */
  onReorder?: () => void;
}

/** 拖拽/键盘换位的接线；筛选中或手机端不传，行首不渲染拖拽柄。 */
export interface LibraryRowDrag {
  dragging: boolean;
  /** 另一行正拖到本行上方（落点提示） */
  over: boolean;
  onDragStart: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
  onDragEnd: () => void;
  /** 键盘换位：Alt+↑ / Alt+↓ */
  onMoveKey: (offset: -1 | 1) => void;
}

const STATUS_CTX = { phaseLabels: SCAN_PHASE_LABELS, relativeTime: formatRelativeTime };

/**
 * 管理页的一行：桌面端是表格行（grid 列与表头对齐），手机端同一组件切成卡片。
 * 行内不放独立按钮，所有操作收进右侧的单一 ··· 菜单（用户拍板：按钮统一收进菜单）。
 */
export function LibraryManageRow({
  library,
  actions,
  drag,
}: {
  library: MediaLibrary;
  actions: LibraryRowActions;
  drag: LibraryRowDrag | null;
}) {
  const status = libraryStatus(library, STATUS_CTX);
  const inventory = inventoryLabel(library);
  const meta = LIBRARY_KIND_META[library.kind];
  const scopeSummary = describeScope(library);

  return (
    <div
      data-library-row={library.id}
      onDragOver={drag?.onDragOver}
      onDrop={drag?.onDrop}
      className={`group/row relative grid items-center gap-4 px-4 py-3 transition-colors hover:bg-white/[0.025] ${MANAGE_GRID_COLS} max-md:grid-cols-[minmax(0,1fr)_40px] max-md:gap-x-3 max-md:gap-y-2 max-md:px-4 max-md:py-3.5 ${
        drag?.dragging ? "opacity-40" : ""
      } ${drag?.over ? "shadow-[inset_0_2px_0_0_var(--accent-2)]" : ""}`}
    >
      {/* 拖拽柄：只在桌面端且未筛选时出现；键盘用户 Alt+↑/↓ 换位 */}
      <div className="max-md:hidden">
        {drag ? (
          <button
            type="button"
            draggable
            onDragStart={drag.onDragStart}
            onDragEnd={drag.onDragEnd}
            onKeyDown={(e) => {
              if (!e.altKey) return;
              if (e.key === "ArrowUp") {
                e.preventDefault();
                drag.onMoveKey(-1);
              } else if (e.key === "ArrowDown") {
                e.preventDefault();
                drag.onMoveKey(1);
              }
            }}
            aria-label={`拖动调整「${library.name}」的展示顺序（或按 Alt + 上下方向键）`}
            className="grid size-7 cursor-grab place-items-center rounded-md text-white/30 outline-none transition hover:bg-white/[0.06] hover:text-white/70 focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] active:cursor-grabbing"
          >
            <GripIcon className="size-4" />
          </button>
        ) : (
          <span className="block size-7" aria-hidden />
        )}
      </div>

      {/* 库：缩略图 + 名称（进单库页）+ 默认标；第二行类型 · 收藏范围 · 首页展示 */}
      <div className="flex min-w-0 items-center gap-3">
        <LibraryThumb library={library} Icon={meta.Icon} />
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Link
              href={`/library/${library.id}` as Route}
              className="truncate text-ui font-semibold text-[var(--text)] hover:underline"
            >
              {library.name}
            </Link>
            {library.is_default && (
              <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.08] px-1.5 py-px text-micro font-semibold text-white/75">
                默认
              </span>
            )}
          </div>
          <div className="truncate text-caption text-[var(--text-faint)]">{scopeSummary}</div>
        </div>
      </div>

      {/* 根目录：主根 + 其余折叠成「+N」，悬停 title 展开全部 */}
      <div
        className="min-w-0 font-mono text-caption text-[var(--text-muted)] max-md:col-span-2 max-md:text-micro"
        title={library.root_paths.join("\n")}
      >
        <div className="truncate">{library.root_paths[0] ?? "—"}</div>
        {library.root_paths.length > 1 && (
          <div className="truncate text-[var(--text-faint)]">
            +{library.root_paths.length - 1} · {library.root_paths[1]}
          </div>
        )}
      </div>

      {/* 库存 */}
      <div className="whitespace-nowrap text-ui tabular-nums max-md:col-span-2 max-md:text-caption max-md:text-[var(--text-muted)]">
        {inventory.primary}
        <span className="ml-1.5 text-caption text-[var(--text-faint)]">{inventory.secondary}</span>
      </div>

      {/* 状态 */}
      <StatusCell status={status} />

      {/* 可见范围 */}
      <div className="whitespace-nowrap text-ui text-[var(--text-muted)] max-md:hidden">
        {library.viewer_access ? (
          accessLabel(library)
        ) : (
          <span className="inline-flex items-center gap-1 rounded-full border border-white/[0.14] px-2 py-px text-caption">
            <LockIcon className="size-3" />
            仅管理
          </span>
        )}
      </div>

      {/* 操作：唯一的 ··· 菜单 */}
      <div className="flex justify-end max-md:col-start-2 max-md:row-start-1">
        <RowMenu library={library} status={status} actions={actions} />
      </div>
    </div>
  );
}

function describeScope(library: MediaLibrary): string {
  const parts = [LIBRARY_KIND_LABELS[library.kind]];
  if (!library.viewer_access) parts.push("仅管理");
  if (library.match_rules.length > 0) parts.push(`收藏范围 ${library.match_rules.length} 项条件`);
  else if (library.capabilities.scraped) parts.push("未声明收藏范围");
  parts.push(library.exclude_from_home ? "从首页排除" : "在首页展示");
  return parts.join(" · ");
}

/** 小缩略图：服务端拼贴图（与首页卡片同源），失败或空库退回类型图标。 */
function LibraryThumb({ library, Icon }: { library: MediaLibrary; Icon: typeof LockIcon }) {
  const [failed, setFailed] = useState(false);
  const showImage = library.viewer_access && library.stats.item_count > 0 && !failed;
  return (
    <div className="relative h-8 w-12 shrink-0 overflow-hidden rounded-md border border-white/[0.08] bg-gradient-to-br from-[#1c2230] to-[#10131c]">
      {showImage ? (
        <img
          src={`${publicEnv.apiBaseUrl}/libraries/${library.id}/cover`}
          alt=""
          loading="lazy"
          className="size-full object-cover"
          onError={() => setFailed(true)}
        />
      ) : (
        <div className="grid size-full place-items-center text-white/25">
          {library.viewer_access ? <Icon className="size-4" /> : <LockIcon className="size-3.5" />}
        </div>
      )}
    </div>
  );
}

function StatusCell({ status }: { status: LibraryStatus }) {
  return (
    <div className="min-w-0 max-md:col-span-2">
      <div className="flex items-center gap-2 text-ui">
        <span className={`size-1.5 shrink-0 rounded-full ${TONE_DOT[status.tone]}`} />
        <span className="truncate">{status.title}</span>
      </div>
      {status.percent !== null && (
        <div className="mt-1 h-[3px] w-full max-w-[160px] overflow-hidden rounded-full bg-white/[0.1]">
          <div
            className="h-full rounded-full bg-[var(--info)] transition-[width] duration-500"
            style={{ width: `${status.percent}%` }}
          />
        </div>
      )}
      {status.detail && (
        <div className="mt-0.5 truncate text-caption text-[var(--text-faint)]">{status.detail}</div>
      )}
    </div>
  );
}

/**
 * 行尾 ··· 菜单：现有首页卡片菜单与单库页菜单的并集，不新增功能。
 * 与单库页一样用 Radix DropdownMenu（Portal + 碰撞检测，不被表格裁切）。
 */
function RowMenu({
  library,
  status,
  actions,
}: {
  library: MediaLibrary;
  status: LibraryStatus;
  actions: LibraryRowActions;
}) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  const scanning = library.scanning;
  const organizing = library.organizing;
  const refreshing = Boolean(library.metadata_refresh?.refreshing);
  const busy = scanning || organizing || refreshing;
  // 重识别占同一把库级锁但不接受中途停止（后端会拒绝），入口置灰并如实标出
  const stoppable = scanning && library.scan_progress?.phase !== "reidentifying";
  const pending = library.stats.unidentified_count + library.stats.missing_count;
  const caps = library.capabilities;
  const pct = status.percent === null ? "" : ` ${status.percent}%`;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={`「${library.name}」的操作`}
          className="relative grid size-8 place-items-center rounded-full border border-white/[0.09] bg-white/[0.04] text-white/80 transition hover:bg-white/[0.1] hover:text-white data-[state=open]:bg-white/[0.14] data-[state=open]:text-white"
        >
          <MoreIcon className="size-[18px]" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[12rem] p-1"
        >
          <DropdownMenu.Item
            onSelect={() => actions.onToggleScan(library)}
            disabled={(busy && !scanning) || (scanning && !stoppable)}
            className={itemClass}
          >
            {!scanning
              ? "扫描库"
              : stoppable
                ? `停止扫描${pct}`
                : `${SCAN_PHASE_LABELS[library.scan_progress?.phase ?? "ingesting"]}…`}
          </DropdownMenu.Item>
          {/* 待处理常驻：计数为 0 也可进（已忽略清单只有这里能到） */}
          {caps.scraped && (
            <DropdownMenu.Item onSelect={() => actions.onOpenPending(library)} className={itemClass}>
              待处理{pending > 0 ? ` ${pending}` : ""}
            </DropdownMenu.Item>
          )}
          {caps.naming && (
            <DropdownMenu.Item
              onSelect={() => actions.onOrganize(library)}
              disabled={busy && !organizing}
              className={itemClass}
            >
              {organizing ? `整理中…${pct}` : "整理文件名"}
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item
            onSelect={() => actions.onToggleRefresh(library)}
            disabled={busy && !refreshing}
            className={itemClass}
          >
            {refreshing ? `停止刷新${pct}` : caps.scraped ? "刷新元数据" : "重新生成缩略图"}
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
          {/* 扫描/整理正按当前根路径读写台账，期间不允许改库配置 */}
          <DropdownMenu.Item
            onSelect={() => actions.onEdit(library)}
            disabled={scanning || organizing}
            className={itemClass}
          >
            编辑库
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={() => actions.onSetDefault(library)}
            disabled={library.is_default}
            className={itemClass}
          >
            {library.is_default ? "已是默认库" : "设为默认库"}
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={() => actions.onToggleHome(library)} className={itemClass}>
            {library.exclude_from_home ? "在首页展示" : "从首页排除"}
          </DropdownMenu.Item>
          {actions.onReorder && (
            <DropdownMenu.Item onSelect={actions.onReorder} className={itemClass}>
              调整顺序
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
          <DropdownMenu.Item
            onSelect={() => actions.onDelete(library)}
            disabled={scanning || organizing}
            className={`${itemClass} !text-[var(--danger)] data-[highlighted]:!bg-[rgba(255,107,107,0.12)]`}
          >
            删除库
            <span className="ml-auto pl-3 text-caption text-[var(--text-faint)]">不动磁盘</span>
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
