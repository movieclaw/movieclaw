"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ContentEmptyState } from "@/components/content-empty-state";
import { useConfirm } from "@/components/feedback";
import { ChevronDownIcon, PlusIcon, SearchIcon, XIcon } from "@/components/icons";
import { LibraryFormDialog } from "@/components/library-form-dialog";
import {
  type LibraryRowActions,
  type LibraryRowDrag,
  LibraryManageRow,
  MANAGE_GRID_COLS,
} from "@/components/library-manage-row";
import { LibraryOrganizeDialog } from "@/components/library-organize-dialog";
import { routingOverlapWarnings } from "@/components/library-view";
import { Modal } from "@/components/modal";
import { PageNav } from "@/components/page-nav";
import {
  type MediaLibrary,
  deleteLibrary,
  listLibraries,
  reorderLibraries,
  setDefaultLibrary,
  startLibraryMetadataRefresh,
  startLibraryScan,
  stopLibraryMetadataRefresh,
  stopLibraryScan,
  updateLibrary,
} from "@/lib/api/libraries";
import { refreshLibraryConfirm, scanLibraryConfirm } from "@/lib/library-confirm";
import {
  EMPTY_FILTER,
  type LibraryFilter,
  filterIsActive,
  filterLibraries,
  libraryIsBusy,
  moveInList,
} from "@/lib/library-manage";
import { LIBRARY_KIND_LABELS, type LibraryKind } from "@/lib/media-types";
import { usePermissions } from "@/lib/permissions";
import { useIsMobile } from "@/lib/use-media-query";
import { useVisiblePolling } from "@/lib/use-visible-polling";

const KIND_ORDER: LibraryKind[] = ["movie", "tv", "video"];

/**
 * 媒体库管理页（/library/manage）：一库一行的纵向列表，库多了只是变长。
 *
 * 首页（/library）只做浏览入口；建库、编辑、扫描、整理、刷新、设默认、
 * 首页展示开关、排序、删除全部在这里完成。设计见 docs/design/library-manage.md。
 *
 * 数据只用 listLibraries 一个接口（随库下发的统计快照与任务进度足够填满
 * 状态列），不逐库拉条目——首页为了封面拼图才要拉，这里的缩略图走服务端拼贴图。
 */
export function LibraryManageView() {
  const { canManageLibraries } = usePermissions();
  const router = useRouter();
  const confirm = useConfirm();
  const isMobile = useIsMobile();

  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<LibraryFilter>(EMPTY_FILTER);
  // 弹窗态：新增（"new"）/ 编辑（库对象）/ 关闭(null)
  const [editing, setEditing] = useState<MediaLibrary | "new" | null>(null);
  const [organizeTarget, setOrganizeTarget] = useState<MediaLibrary | null>(null);
  const [reorderOpen, setReorderOpen] = useState(false);
  // 新建成功后把新行滚进视野（纵向列表末尾可能在首屏外）
  const [revealId, setRevealId] = useState<number | null>(null);

  // 轮询乱序守卫：与首页同一套——扫描期间慢响应可能晚于下一轮到达
  const reloadSeq = useRef(0);
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    listLibraries()
      .then((libs) => {
        if (seq !== reloadSeq.current) return;
        setFailed(false);
        const snapshot = JSON.stringify(libs);
        setLibraries((prev) => (prev && JSON.stringify(prev) === snapshot ? prev : libs));
      })
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // 首页空状态的「创建第一个媒体库」落到 /library/manage?create=1：进页即开建库弹窗。
  // 读 location 而不是 useSearchParams（全站惯例，免去 Suspense 边界）；读完把参数抹掉，
  // 刷新页面不会再弹
  useEffect(() => {
    if (!canManageLibraries) return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("create") !== "1") return;
    setEditing("new");
    params.delete("create");
    const rest = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
  }, [canManageLibraries]);

  // 轮询节奏与首页一致：任务中 3 秒 / 刷新元数据 5 秒 / 入库中 10 秒 / 空闲 30 秒
  const busyAny = (libraries ?? []).some((l) => l.scanning || l.organizing);
  const refreshingAny = (libraries ?? []).some((l) => l.metadata_refresh?.refreshing);
  const importingAny = (libraries ?? []).some(
    (l) => !l.scanning && !l.organizing && (l.last_scan?.deferred ?? 0) > 0,
  );
  const [recentlyBusy, setRecentlyBusy] = useState(false);
  useEffect(() => {
    if (busyAny) {
      setRecentlyBusy(true);
      return;
    }
    if (!recentlyBusy) return;
    const timer = setTimeout(() => setRecentlyBusy(false), 12_000);
    return () => clearTimeout(timer);
  }, [busyAny, recentlyBusy]);
  useVisiblePolling(
    reload,
    busyAny || recentlyBusy ? 3000 : refreshingAny ? 5000 : importingAny ? 10_000 : 30_000,
  );

  useEffect(() => {
    if (revealId === null) return;
    document
      .querySelector(`[data-library-row="${revealId}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
    setRevealId(null);
  }, [revealId, libraries]);

  const warnings = useMemo(() => routingOverlapWarnings(libraries ?? []), [libraries]);
  const visible = useMemo(() => filterLibraries(libraries ?? [], filter), [libraries, filter]);
  const busyCount = (libraries ?? []).filter(libraryIsBusy).length;
  const kindCounts = useMemo(() => {
    const counts = new Map<LibraryKind, number>();
    for (const l of libraries ?? []) counts.set(l.kind, (counts.get(l.kind) ?? 0) + 1);
    return counts;
  }, [libraries]);

  /** 动作统一收口：成功后立刻拉一次列表，失败把后端的中文错误挂到顶部提示条 */
  const run = useCallback(
    (action: Promise<unknown>) => {
      setError(null);
      void action.then(reload).catch((e) => setError((e as Error).message));
    },
    [reload],
  );

  /** 提交新顺序：先乐观换位，失败回滚。全量 id 一次提交（后端接口要求） */
  const commitOrder = useCallback(
    (next: readonly MediaLibrary[]) => {
      const prev = libraries;
      setLibraries([...next]);
      setError(null);
      void reorderLibraries(next.map((l) => l.id))
        .then(reload)
        .catch((e) => {
          setLibraries(prev);
          setError((e as Error).message);
        });
    },
    [libraries, reload],
  );

  const moveLibrary = useCallback(
    (libraryId: number, to: number) => {
      if (!libraries) return;
      const from = libraries.findIndex((l) => l.id === libraryId);
      const next = moveInList(libraries, from, to);
      if (next !== libraries) commitOrder(next);
    },
    [libraries, commitOrder],
  );

  const actions: LibraryRowActions = useMemo(
    () => ({
      onToggleScan: (library) => {
        if (library.scanning) {
          run(stopLibraryScan(library.id));
          return;
        }
        // 重操作先确认；停止不确认——停止本身就是在纠正
        void confirm(scanLibraryConfirm(library.name)).then((ok) => {
          if (ok) run(startLibraryScan(library.id));
        });
      },
      onOpenPending: (library) => {
        router.push(`/library/${library.id}?pending=1` as Route);
      },
      onOrganize: (library) => setOrganizeTarget(library),
      onToggleRefresh: (library) => {
        if (library.metadata_refresh?.refreshing) {
          run(stopLibraryMetadataRefresh(library.id));
          return;
        }
        void confirm(refreshLibraryConfirm(library.name)).then((ok) => {
          if (ok) run(startLibraryMetadataRefresh(library.id));
        });
      },
      onEdit: (library) => setEditing(library),
      onSetDefault: (library) => run(setDefaultLibrary(library.id)),
      // 只改这一个字段：payload 里没传的字段后端按"不改动"处理
      onToggleHome: (library) =>
        run(
          updateLibrary(library.id, {
            name: library.name,
            kind: library.kind,
            root_paths: library.root_paths,
            exclude_from_home: !library.exclude_from_home,
          }),
        ),
      onDelete: (library) => {
        void confirm({
          title: `删除媒体库「${library.name}」？`,
          description: "磁盘文件不受影响，挂在它上面的订阅将回落到该类型的默认库。",
          confirmLabel: "删除库",
          tone: "danger",
        }).then((ok) => {
          if (ok) run(deleteLibrary(library.id));
        });
      },
      onReorder: isMobile ? () => setReorderOpen(true) : undefined,
    }),
    [confirm, isMobile, router, run],
  );

  // —— 拖拽排序（桌面端、未筛选时）——
  const [dragId, setDragId] = useState<number | null>(null);
  const [overId, setOverId] = useState<number | null>(null);
  const dragEnabled = !isMobile && !filterIsActive(filter) && (libraries?.length ?? 0) > 1;
  const dragFor = (library: MediaLibrary): LibraryRowDrag | null => {
    if (!dragEnabled || !libraries) return null;
    const index = libraries.findIndex((l) => l.id === library.id);
    return {
      dragging: dragId === library.id,
      over: overId === library.id && dragId !== library.id,
      onDragStart: (e) => {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", String(library.id));
        setDragId(library.id);
      },
      onDragOver: (e) => {
        if (dragId === null) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (overId !== library.id) setOverId(library.id);
      },
      onDrop: (e) => {
        e.preventDefault();
        if (dragId !== null && dragId !== library.id) moveLibrary(dragId, index);
        setDragId(null);
        setOverId(null);
      },
      onDragEnd: () => {
        setDragId(null);
        setOverId(null);
      },
      onMoveKey: (offset) => moveLibrary(library.id, index + offset),
    };
  };

  if (!canManageLibraries) {
    return (
      <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
        <PageNav title="媒体库管理" fallback={{ label: "媒体库", href: "/library" as Route }} />
        <ContentEmptyState
          variant="library"
          title="没有管理权限"
          description="媒体库的创建、扫描与排序由管理员负责；你可以回到媒体库继续浏览。"
          action={
            <Link href={"/library" as Route} className="btn-glass px-4 py-2 text-ui font-medium">
              返回媒体库
            </Link>
          }
        />
      </div>
    );
  }

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <PageNav
        title="媒体库管理"
        fallback={{ label: "媒体库", href: "/library" as Route }}
        actions={
          <button
            type="button"
            onClick={() => setEditing("new")}
            className="btn-accent flex h-9 items-center gap-1 rounded-full py-0 pl-3 pr-4 text-ui font-semibold max-md:h-11"
          >
            <PlusIcon className="size-4" />
            添加媒体库
          </button>
        }
      />

      <div className="px-6 pt-3 max-md:px-4">
        <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
          媒体库管理
        </h2>
        <p className="text-on-image mt-1.5 text-ui text-[var(--text-muted)] max-md:text-sub">
          库负责盘点与守护；这里改的是库本身，浏览内容请回媒体库首页。
        </p>
      </div>

      {error && (
        <div className="mx-6 mt-4 rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b] max-md:mx-4">
          {error}
        </div>
      )}
      {failed && libraries !== null && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {libraries === null && !failed && (
        <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载媒体库…
        </div>
      )}
      {failed && libraries === null && (
        <div className="mt-16 flex flex-col items-center gap-3 text-center">
          <p className="text-ui text-[var(--text-muted)]">媒体库加载失败</p>
          <button type="button" onClick={reload} className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]">
            重试
          </button>
        </div>
      )}

      {libraries !== null && libraries.length === 0 && (
        <ContentEmptyState
          variant="library"
          title="为收藏准备一个家"
          description="创建电影库或剧集库，选好根目录后，订阅完成的内容会自动整理到这里。"
          action={
            <button
              type="button"
              onClick={() => setEditing("new")}
              className="btn-accent flex items-center gap-1 rounded-full py-2 pl-3 pr-4 text-ui font-semibold"
            >
              <PlusIcon className="size-4" />
              创建第一个媒体库
            </button>
          }
        />
      )}

      {libraries !== null && libraries.length > 0 && (
        <>
          {/* 工具栏：搜索 / 类型筛选 / 在跑任务 */}
          <div className="mt-5 flex flex-wrap items-center gap-2.5 px-6 max-md:px-4">
            <label className="flex h-9 min-w-[220px] flex-1 items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.04] px-3 text-ui text-[var(--text-muted)] focus-within:border-[var(--accent)]/60 max-md:min-w-0 max-md:basis-full sm:max-w-[320px]">
              <SearchIcon className="size-4 shrink-0" />
              <input
                type="search"
                value={filter.query}
                onChange={(e) => setFilter((f) => ({ ...f, query: e.target.value }))}
                placeholder="按库名或根目录搜索"
                aria-label="搜索媒体库"
                className="min-w-0 flex-1 bg-transparent text-[var(--text)] outline-none placeholder:text-[var(--text-faint)]"
              />
              {filter.query && (
                <button
                  type="button"
                  aria-label="清除搜索"
                  onClick={() => setFilter((f) => ({ ...f, query: "" }))}
                  className="grid size-5 place-items-center rounded-full hover:bg-white/[0.1]"
                >
                  <XIcon className="size-3" />
                </button>
              )}
            </label>
            <div className="flex flex-wrap items-center gap-1.5">
              <FilterChip
                active={filter.kind === null}
                onClick={() => setFilter((f) => ({ ...f, kind: null }))}
              >
                全部 {libraries.length}
              </FilterChip>
              {KIND_ORDER.filter((k) => (kindCounts.get(k) ?? 0) > 0).map((k) => (
                <FilterChip
                  key={k}
                  active={filter.kind === k}
                  onClick={() => setFilter((f) => ({ ...f, kind: f.kind === k ? null : k }))}
                >
                  {LIBRARY_KIND_LABELS[k]} {kindCounts.get(k)}
                </FilterChip>
              ))}
            </div>
            {busyCount > 0 && (
              <FilterChip
                active={filter.busyOnly}
                onClick={() => setFilter((f) => ({ ...f, busyOnly: !f.busyOnly }))}
                className="ml-auto"
              >
                <span className="size-1.5 rounded-full bg-[var(--info)]" />
                {busyCount} 个在跑任务
              </FilterChip>
            )}
          </div>

          {/* 收藏范围重叠提示：只读不阻断，原在首页，现在只在这里出现 */}
          {warnings.map((w) => (
            <div
              key={w}
              className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-relaxed text-amber-200 max-md:mx-4"
            >
              {w}
            </div>
          ))}

          {/* 列表 */}
          <div className="mx-6 mt-4 overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.02] max-md:mx-4">
            <div
              className={`grid gap-4 border-b border-white/[0.07] px-4 py-2.5 text-caption font-medium text-[var(--text-faint)] max-md:hidden ${MANAGE_GRID_COLS}`}
            >
              <span />
              <span>库</span>
              <span>根目录</span>
              <span>库存</span>
              <span>状态</span>
              <span>可见范围</span>
              <span className="text-right">操作</span>
            </div>
            {visible.length === 0 ? (
              <div className="px-4 py-10 text-center text-ui text-[var(--text-muted)]">
                没有符合条件的媒体库
                <button
                  type="button"
                  onClick={() => setFilter(EMPTY_FILTER)}
                  className="ml-2 text-[var(--info)] hover:underline"
                >
                  清除筛选
                </button>
              </div>
            ) : (
              <div className="divide-y divide-white/[0.06]">
                {visible.map((library) => (
                  <LibraryManageRow
                    key={library.id}
                    library={library}
                    actions={actions}
                    drag={dragFor(library)}
                  />
                ))}
              </div>
            )}
          </div>

          <div className="mx-6 mt-3 flex flex-wrap items-center justify-between gap-2 text-caption text-[var(--text-faint)] max-md:mx-4">
            <span>
              {isMobile
                ? "顺序即首页「我的媒体库」的展示顺序，在 ··· 菜单里「调整顺序」"
                : dragEnabled
                  ? "拖动行首的把手调整首页「我的媒体库」的展示顺序，松手即保存"
                  : filterIsActive(filter)
                    ? "清除筛选后可拖拽排序"
                    : ""}
            </span>
            <span className="flex items-center gap-3">
              <Legend className="bg-white/30">空闲</Legend>
              <Legend className="bg-[var(--info)]">任务进行中</Legend>
              <Legend className="bg-[var(--warn)]">有待处理</Legend>
              <Legend className="bg-[var(--danger)]">有缺失</Legend>
            </span>
          </div>
        </>
      )}

      <LibraryFormDialog
        state={editing}
        onClose={() => setEditing(null)}
        onSaved={(saved) => {
          const isNew = editing === "new";
          setEditing(null);
          if (isNew) setRevealId(saved.id);
          reload();
        }}
      />
      <LibraryOrganizeDialog
        library={organizeTarget}
        onClose={() => setOrganizeTarget(null)}
        onChanged={reload}
      />
      {libraries && (
        <ReorderDialog
          open={reorderOpen}
          libraries={libraries}
          onClose={() => setReorderOpen(false)}
          onSubmit={(next) => {
            setReorderOpen(false);
            commitOrder(next);
          }}
        />
      )}
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  className = "",
  children,
}: {
  active: boolean;
  onClick: () => void;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-caption font-medium transition ${
        active
          ? "border-white/[0.2] bg-white/[0.14] text-[var(--text)]"
          : "border-white/[0.1] text-[var(--text-muted)] hover:bg-white/[0.06] hover:text-[var(--text)]"
      } ${className}`}
    >
      {children}
    </button>
  );
}

function Legend({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`size-1.5 rounded-full ${className}`} />
      {children}
    </span>
  );
}

/**
 * 手机端的排序弹窗：没有拖拽，上下箭头换位，确认后一次提交整单。
 * 弹窗内部持有一份顺序草稿，取消不影响列表。
 */
function ReorderDialog({
  open,
  libraries,
  onClose,
  onSubmit,
}: {
  open: boolean;
  libraries: MediaLibrary[];
  onClose: () => void;
  onSubmit: (next: readonly MediaLibrary[]) => void;
}) {
  const [draft, setDraft] = useState<readonly MediaLibrary[]>(libraries);
  useEffect(() => {
    if (open) setDraft(libraries);
  }, [open, libraries]);
  const changed = draft.some((l, i) => l.id !== libraries[i]?.id);
  return (
    <Modal open={open} onClose={onClose} label="调整媒体库顺序">
      <div className="p-5">
        <h2 className="text-title font-bold text-white">调整顺序</h2>
        <p className="mt-1 text-sub text-[var(--text-muted)]">这也是首页「我的媒体库」的展示顺序。</p>
        <ol className="mt-4 divide-y divide-white/[0.06] overflow-hidden rounded-xl border border-white/[0.08]">
          {draft.map((library, index) => (
            <li key={library.id} className="flex items-center gap-3 px-3 py-2.5">
              <span className="w-5 text-caption tabular-nums text-[var(--text-faint)]">{index + 1}</span>
              <span className="min-w-0 flex-1 truncate text-ui font-medium">{library.name}</span>
              <button
                type="button"
                aria-label={`「${library.name}」上移`}
                disabled={index === 0}
                onClick={() => setDraft((d) => moveInList(d, index, index - 1))}
                className="grid size-8 place-items-center rounded-full border border-white/[0.09] text-white/75 disabled:opacity-30"
              >
                <ChevronDownIcon className="size-4 rotate-180" />
              </button>
              <button
                type="button"
                aria-label={`「${library.name}」下移`}
                disabled={index === draft.length - 1}
                onClick={() => setDraft((d) => moveInList(d, index, index + 1))}
                className="grid size-8 place-items-center rounded-full border border-white/[0.09] text-white/75 disabled:opacity-30"
              >
                <ChevronDownIcon className="size-4" />
              </button>
            </li>
          ))}
        </ol>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="btn-glass px-4 py-2 text-ui font-medium">
            取消
          </button>
          <button
            type="button"
            disabled={!changed}
            onClick={() => onSubmit(draft)}
            className="btn-accent rounded-full px-4 py-2 text-ui font-semibold disabled:opacity-40"
          >
            保存顺序
          </button>
        </div>
      </div>
    </Modal>
  );
}
