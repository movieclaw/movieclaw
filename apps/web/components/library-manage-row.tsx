"use client";

import { useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { GripIcon, LockIcon, MoreIcon } from "@/components/icons";
import { LIBRARY_KIND_META } from "@/components/library-kind-meta";
import { type MediaLibrary, SCAN_PHASE_LABELS } from "@/lib/api/libraries";
import { publicEnv } from "@/lib/env";
import {
  type LibraryStatus,
  type LibraryStatusTone,
  accessLabel,
  accessRestricted,
  configNotes,
  inventoryLabel,
  chapterJobLabel,
  libraryStatus,
} from "@/lib/library-manage";
import { LIBRARY_KIND_LABELS } from "@/lib/media-types";
import { formatRelativeTime } from "@/lib/time";

/**
 * 桌面端一行分四个区：拖拽柄 / 库（弹性，吃掉全部剩余宽度）/ 库存（定宽右对齐）/
 * 状态（定宽的一条竖向「状态道」，上下行的状态在同一列对齐，眼睛顺着往下扫就行）/ 操作。
 * 库名之外的信息全部收进库名下方那一行小字，不再各占一列——七列并排时每列都被
 * 挤到只剩截断，反而谁也看不清。
 */
const GRID_COLS = "grid-cols-[28px_minmax(0,1fr)_96px_minmax(184px,224px)_40px]";

/** 状态圆点：蓝 = 任务进行中，黄 = 有待处理，红 = 有缺失；空闲不画点（见 StatusCell）。 */
const TONE_DOT: Record<LibraryStatusTone, string> = {
  idle: "bg-white/30",
  busy: "bg-[var(--info)] shadow-[0_0_0_3px_rgba(127,176,255,0.18)]",
  pending: "bg-[var(--warn)]",
  missing: "bg-[var(--danger)]",
};

/** 待处理胶囊的底色：黄 = 待识别，红 = 有缺失。文字直接用状态色，不做白字压色底。 */
const TONE_PILL: Record<"pending" | "missing", string> = {
  pending: "border-[rgba(245,196,81,0.35)] bg-[rgba(245,196,81,0.1)] text-[var(--warn)]",
  missing: "border-[rgba(255,107,107,0.35)] bg-[rgba(255,107,107,0.1)] text-[var(--danger)]",
};

/** 一行库能触发的全部操作；是否可用由行内按当前状态判定。 */
export interface LibraryRowActions {
  onToggleScan: (library: MediaLibrary) => void;
  onOpenPending: (library: MediaLibrary) => void;
  onOrganize: (library: MediaLibrary) => void;
  onToggleRefresh: (library: MediaLibrary) => void;
  /** 整库生成章节（库开了开关才给入口）；是否重做已有的在确认弹窗里勾选 */
  onChapterImages: (library: MediaLibrary) => void;
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
  /** 另一行正拖到本行：落在本行之前还是之后（落点提示线画在上沿或下沿）；null = 没拖到本行 */
  over: "before" | "after" | null;
  onDragStart: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
  onDragEnd: () => void;
  /** 键盘换位：Alt+↑ / Alt+↓ */
  onMoveKey: (offset: -1 | 1) => void;
}

const STATUS_CTX = { phaseLabels: SCAN_PHASE_LABELS, relativeTime: formatRelativeTime };

/**
 * 管理页的一行：桌面端是列表行（grid 四区），手机端同一组件切成卡片。
 *
 * 一行只有两个视觉重心：左边的库名（白色、加粗），右边的状态（有事才带色）。
 * 类型、根目录、配置备注一律是库名下的小字；库存是安静的定宽数字。
 * 行内不放独立按钮，所有操作收进右侧的单一 ··· 菜单（用户拍板：按钮统一收进菜单）；
 * 唯一的例外是「待识别 / 缺失」胶囊本身可点——它是这页真正要你动手的信号，
 * 点它直达单库页的待处理清单，不必再去菜单里找。
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
  const notes = configNotes(library);

  return (
    <div
      role="listitem"
      data-library-row={library.id}
      onDragOver={drag?.onDragOver}
      onDrop={drag?.onDrop}
      className={`group/row relative grid items-center gap-4 px-4 py-3 transition-colors hover:bg-white/[0.025] ${GRID_COLS} max-md:grid-cols-[minmax(0,1fr)_40px] max-md:gap-x-3 max-md:gap-y-2.5 max-md:py-3.5 ${
        drag?.dragging ? "opacity-40" : ""
      } ${
        drag?.over === "before"
          ? "shadow-[inset_0_2px_0_0_var(--accent-2)]"
          : drag?.over === "after"
            ? "shadow-[inset_0_-2px_0_0_var(--accent-2)]"
            : ""
      }`}
    >
      {/* 拖拽柄：只在桌面端且未筛选时存在，指针停在行上或键盘聚焦时才现形——
          十几行的把手一直亮着只是一列噪音；键盘用户 Alt+↑/↓ 换位 */}
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
            className="grid size-7 cursor-grab place-items-center rounded-md text-white/40 opacity-0 outline-none transition hover:bg-white/[0.06] hover:text-white/80 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] active:cursor-grabbing group-hover/row:opacity-100"
          >
            <GripIcon className="size-4" />
          </button>
        ) : (
          <span className="block size-7" aria-hidden />
        )}
      </div>

      {/* 库：缩略图 + 库名（进单库页）+ 「默认」标 + 偏离默认时的可见范围胶囊；
          第二行小字：类型 · 根目录（多根折叠成 +N，悬停展开）· 需要留意的配置 */}
      <div className="flex min-w-0 items-center gap-3">
        <LibraryThumb library={library} Icon={meta.Icon} />
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <Link
              href={`/library/${library.id}` as Route}
              className="truncate text-ui font-semibold text-white hover:underline"
            >
              {library.name}
            </Link>
            {library.is_default && <span className={BADGE}>默认</span>}
            {accessRestricted(library) && <AccessChip library={library} />}
          </div>
          {/* 第二行小字的顺序是「类型 · 备注 · 根目录」：根目录最长，放最后，窄屏下
              整体换到下一行而不是把备注拆散，只有一行都放不下时才截尾（悬停 title
              里有全文）。手机端根目录不在这里：库名块只留两行，与缩略图等高 */}
          <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-caption text-[var(--text-faint)]">
            <span>{LIBRARY_KIND_LABELS[library.kind]}</span>
            {/* 手机端没有库存列，主数字并进这行 */}
            <span className="hidden max-md:inline">· {inventory.primary}</span>
            {notes.map((note) => (
              <span key={note.text} className="inline-flex items-center gap-x-1.5">
                <span aria-hidden>·</span>
                <span className={note.tone === "warn" ? "text-[var(--warn)]" : undefined}>
                  {note.text}
                </span>
              </span>
            ))}
            <span className="flex min-w-0 max-w-full items-center gap-x-1.5 max-md:hidden">
              <span aria-hidden className="shrink-0">
                ·
              </span>
              <RootPath library={library} />
            </span>
          </div>
        </div>
      </div>

      {/* 手机端根目录：独占卡片一整行，放在库名块下方——库名块只有两行时缩略图
          与文字等高、不再上下露边，路径也拿到整个卡片宽度，不必早早截尾 */}
      <div className="hidden min-w-0 items-center gap-x-1.5 text-caption text-[var(--text-faint)] max-md:col-span-2 max-md:-mt-0.5 max-md:flex">
        <RootPath library={library} />
      </div>

      {/* 库存：定宽右对齐的两行数字，都是次要信息，不抢库名与状态的戏 */}
      <div className="whitespace-nowrap text-right tabular-nums max-md:hidden">
        <div className="text-sub text-[var(--text-muted)]">{inventory.primary}</div>
        <div className="text-caption text-[var(--text-faint)]">{inventory.secondary}</div>
      </div>

      {/* 状态 */}
      <StatusCell library={library} status={status} />

      {/* 操作：唯一的 ··· 菜单 */}
      <div className="flex justify-end max-md:col-start-2 max-md:row-start-1">
        <RowMenu library={library} status={status} actions={actions} />
      </div>
    </div>
  );
}

/** 库名旁的小标签样式：「默认」与可见范围胶囊共用一套。 */
const BADGE =
  "inline-flex shrink-0 items-center gap-1 rounded-full border border-white/[0.14] bg-white/[0.08] px-1.5 py-px text-micro font-semibold text-white/75";

/** 可见范围胶囊：只在偏离默认时出现；文案只说库开放给谁，你本人不在范围内时前面带一把锁。 */
function AccessChip({ library }: { library: MediaLibrary }) {
  return (
    <span
      className={BADGE}
      title={library.viewer_access ? undefined : "你不在这个库的浏览范围内，只能管理"}
    >
      {!library.viewer_access && <LockIcon className="size-2.5" aria-label="你不在浏览范围内" />}
      {accessLabel(library)}
    </span>
  );
}

/** 主根目录（等宽）+ 多根时的「+N 个根目录」；桌面端接在小字行末，手机端独占一行，两处共用。 */
function RootPath({ library }: { library: MediaLibrary }) {
  const extra = library.root_paths.length - 1;
  return (
    <>
      <span
        className="min-w-0 truncate font-mono text-[var(--text-muted)]"
        title={library.root_paths.join("\n")}
      >
        {library.root_paths[0] ?? "—"}
      </span>
      {extra > 0 && (
        <span className="shrink-0" title={library.root_paths.slice(1).join("\n")}>
          +{extra} 个根目录
        </span>
      )}
    </>
  );
}

/** 小缩略图：服务端拼贴图（与首页卡片同源），失败或空库退回类型图标。 */
function LibraryThumb({ library, Icon }: { library: MediaLibrary; Icon: typeof LockIcon }) {
  const [failed, setFailed] = useState(false);
  const showImage = library.viewer_access && library.stats.item_count > 0 && !failed;
  return (
    <div className="relative h-11 w-[72px] shrink-0 overflow-hidden rounded-lg border border-white/[0.08] bg-gradient-to-br from-[#1c2230] to-[#10131c]">
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

/**
 * 状态道：三种形态。
 * - 空闲：只有「最近扫描 X 前」一行灰字，没有圆点——空闲不是需要看的状态；
 * - 任务进行中：蓝点 + 阶段词与百分比 + 进度条 + 分子分母；
 * - 待识别 / 缺失：一枚带色胶囊，有刮削能力的库点它直达待处理清单。
 */
function StatusCell({ library, status }: { library: MediaLibrary; status: LibraryStatus }) {
  if (status.kind === "idle") {
    return (
      <div className="min-w-0 truncate text-caption text-[var(--text-faint)] max-md:col-span-2">
        {status.detail}
      </div>
    );
  }
  if (status.tone === "busy") {
    return (
      <div className="min-w-0 max-md:col-span-2">
        <div className="flex items-center gap-2 text-sub text-white/90">
          <span className={`size-1.5 shrink-0 rounded-full ${TONE_DOT.busy}`} />
          <span className="truncate">{status.title}</span>
        </div>
        {/* 进度条：桌面端受状态道宽度约束；手机端状态格已跨整行，通栏填满卡片 */}
        {status.percent !== null && (
          <div className="mt-1 h-[3px] w-full max-w-[200px] overflow-hidden rounded-full bg-white/[0.1] max-md:max-w-none">
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
  const tone = status.tone === "missing" ? "missing" : "pending";
  const pillClass = `inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-caption font-semibold ${TONE_PILL[tone]}`;
  const inner = (
    <>
      <span className={`size-1.5 shrink-0 rounded-full ${TONE_DOT[tone]}`} />
      <span className="truncate">{status.title}</span>
    </>
  );
  return (
    <div className="min-w-0 max-md:col-span-2 max-md:flex max-md:flex-wrap max-md:items-center max-md:gap-x-3 max-md:gap-y-1">
      {/* 待处理清单只有刮削型库才有（与 ··· 菜单里「待处理」的显隐同一条件） */}
      {library.capabilities.scraped ? (
        <Link
          href={`/library/${library.id}?pending=1` as Route}
          title="打开待处理清单"
          className={`${pillClass} outline-none transition hover:brightness-125 focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]`}
        >
          {inner}
        </Link>
      ) : (
        <span className={pillClass}>{inner}</span>
      )}
      <div className="mt-1 truncate text-caption text-[var(--text-faint)] max-md:mt-0">
        {status.detail}
      </div>
    </div>
  );
}

/**
 * 行尾 ··· 菜单：现有首页卡片菜单与单库页菜单的并集，不新增功能。
 * 与单库页一样用 Radix DropdownMenu（Portal + 碰撞检测，不被列表裁切）。
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
  // 库快照只有**文件**级计数（待识别 + 缺失）；单库页菜单里的「待处理 N」数的是
  // 分组（一部剧几十集算一件），两个数天然不同——这里把单位写明，避免被当成同一口径
  const pendingFiles = library.stats.unidentified_count + library.stats.missing_count;
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
              待处理{pendingFiles > 0 ? ` · ${pendingFiles} 个文件` : ""}
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
            {refreshing ? `停止刷新${pct}` : caps.scraped ? "刷新元数据" : "重新生成封面"}
          </DropdownMenu.Item>
          {library.extract_chapter_images && (
            <DropdownMenu.Item
              onSelect={() => actions.onChapterImages(library)}
              // 作业排队/进行中时置灰并如实写状态（后端同库只跑一份，再点也是返回现有作业）
              disabled={busy || library.chapter_job !== null}
              className={itemClass}
            >
              {chapterJobLabel(library.chapter_job)}
            </DropdownMenu.Item>
          )}
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
