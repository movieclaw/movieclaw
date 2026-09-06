"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ContentEmptyState } from "@/components/content-empty-state";
import { useConfirm, useToast } from "@/components/feedback";
import { ChevronDownIcon, PlusIcon, SearchIcon, XIcon } from "@/components/icons";
import { LibraryFormDialog } from "@/components/library-form-dialog";
import {
  type LibraryRowActions,
  type LibraryRowDrag,
  LibraryManageRow,
} from "@/components/library-manage-row";
import { LibraryOrganizeDialog } from "@/components/library-organize-dialog";
import { LibraryRecycleBin } from "@/components/library-recycle-bin";
import { LibraryShares } from "@/components/library-shares";
import { listShares } from "@/lib/api/shares";
import { Modal } from "@/components/modal";
import { PageNav } from "@/components/page-nav";
import {
  type MediaLibrary,
  deleteLibrary,
  listLibraries,
  listTrashedFiles,
  reorderLibraries,
  setDefaultLibrary,
  startLibraryChapterImages,
  startLibraryMetadataRefresh,
  startLibraryScan,
  stopLibraryMetadataRefresh,
  stopLibraryScan,
  updateLibrary,
} from "@/lib/api/libraries";
import {
  chapterImagesConfirm,
  refreshLibraryConfirm,
  scanLibraryConfirm,
} from "@/lib/library-confirm";
import { useJobs } from "@/lib/jobs";
import { routingOverlapWarnings } from "@/lib/library-routing-warnings";
import {
  EMPTY_FILTER,
  type LibraryFilter,
  type LibraryFocus,
  filterIsActive,
  filterLibraries,
  moveInList,
  summarizeLibraries,
} from "@/lib/library-manage";
import { LIBRARY_KIND_LABELS, type LibraryKind } from "@/lib/media-types";
import { usePermissions } from "@/lib/permissions";
import { useIsMobile } from "@/lib/use-media-query";
import { useTabParam } from "@/lib/use-tab-param";
import { useVisiblePolling } from "@/lib/use-visible-polling";

const KIND_ORDER: LibraryKind[] = ["movie", "tv", "video", "photo"];

/** 指针落在目标行的上半还是下半：决定放到它之前还是之后。 */
function dropPosition(e: React.DragEvent): "before" | "after" {
  const rect = e.currentTarget.getBoundingClientRect();
  return e.clientY < rect.top + rect.height / 2 ? "before" : "after";
}

/**
 * 媒体库管理页（/library/manage）：一库一行的纵向列表，库多了只是变长。
 *
 * 页面回答的第一个问题是「有没有事要我管」：页头摘要里只挂两枚带色胶囊——
 * 在跑任务、有待处理文件——点即筛选；两样都没有就写「一切正常」。列表行只有
 * 两个视觉重心（库名、状态），其余信息是库名下的小字。
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
  const toast = useToast();
  const isMobile = useIsMobile();

  // 标签栏：「媒体库」与「回收站」（docs/design/library-recycle-bin.md §2）；
  // ?tab=recycle 深链直达，切换写回地址栏
  const [tab, setTab] = useTabParam(["libraries", "recycle", "shares"] as const, "libraries");
  // 回收站标签上的计数：一次 limit=1 的列表请求只为拿 total_files（一条索引计数查询），
  // 不给库统计快照加列——进出回收站的写路径都不在统计重算之列，加列必陈旧
  const [recycleCount, setRecycleCount] = useState<number | null>(null);
  const reloadRecycleCount = useCallback(() => {
    listTrashedFiles({}, { limit: 1, offset: 0 })
      .then((d) => setRecycleCount(d.total_files))
      .catch(() => {});
  }, []);
  useEffect(() => {
    reloadRecycleCount();
  }, [reloadRecycleCount]);
  // 回收站标签激活时列表本身会回报计数，这里只在看库列表时低频轮询
  useVisiblePolling(reloadRecycleCount, tab === "recycle" ? null : 30_000);
  // 「分享」标签计数（docs/design/media-share.md §5.4）：有效分享一共几条，
  // 与回收站同款——列表激活时由列表回报，其余时候低频轮询
  const [shareCount, setShareCount] = useState<number | null>(null);
  const reloadShareCount = useCallback(() => {
    listShares()
      .then((rows) => setShareCount(rows.length))
      .catch(() => {});
  }, []);
  useEffect(() => {
    reloadShareCount();
  }, [reloadShareCount]);
  useVisiblePolling(reloadShareCount, tab === "shares" ? null : 30_000);

  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [failed, setFailed] = useState(false);
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

  // 不是从这页发起的任务（实时监控触发的自动扫描、CLI、另一台设备点的）只靠轮询要
  // 等到下个周期才被发现，空闲时最长 30 秒。JobsProvider 的 SSE 一收到任务事件就会
  // 更新 activeJobs，这里盯着「库相关活跃作业的 id + 状态」这份指纹：作业出现、状态
  // 变化、结束都立即重拉一次库列表；进度更新仍交给轮询（否则每条进度都触发一次请求）
  const { activeJobs } = useJobs();
  const libraryJobsKey = useMemo(
    () =>
      activeJobs
        .filter((job) => job.resources.some((r) => r.resource_type === "library"))
        .map((job) => `${job.id}:${job.status}`)
        .join("|"),
    [activeJobs],
  );
  const seenJobsKey = useRef(libraryJobsKey);
  useEffect(() => {
    if (seenJobsKey.current === libraryJobsKey) return;
    seenJobsKey.current = libraryJobsKey;
    reload();
  }, [libraryJobsKey, reload]);

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
  const summary = useMemo(() => summarizeLibraries(libraries ?? []), [libraries]);
  /** 页头摘要胶囊即筛选：再点一次取消；在回收站标签上点则先切回库列表 */
  const toggleFocus = (focus: LibraryFocus) => {
    setFilter((f) => ({ ...f, focus: f.focus === focus ? null : focus }));
    setTab("libraries");
  };
  const kindCounts = useMemo(() => {
    const counts = new Map<LibraryKind, number>();
    for (const l of libraries ?? []) counts.set(l.kind, (counts.get(l.kind) ?? 0) + 1);
    return counts;
  }, [libraries]);

  /** 动作统一收口：成功后立刻拉一次列表（可选给一句回执），失败用 toast 报后端的
   *  中文错误——列表可能很长，用户在底部点的按钮，顶部横条根本看不见 */
  const run = useCallback(
    (action: Promise<unknown>, done?: string) => {
      void action
        .then(() => {
          reload();
          if (done) toast.success(done);
        })
        .catch((e) => toast.error((e as Error).message));
    },
    [reload, toast],
  );

  /** 提交新顺序：先乐观换位，失败回滚。全量 id 一次提交（后端接口要求） */
  const commitOrder = useCallback(
    (next: readonly MediaLibrary[]) => {
      const prev = libraries;
      setLibraries([...next]);
      void reorderLibraries(next.map((l) => l.id))
        .then(() => {
          reload();
          toast.success("顺序已更新，首页「我的媒体库」同步生效");
        })
        .catch((e) => {
          setLibraries(prev);
          toast.error((e as Error).message);
        });
    },
    [libraries, reload, toast],
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
        // 重操作先确认；停止不确认——停止本身就是在纠正。
        // 开始要给一句回执：已是最新的库扫描毫秒级就结束，行内只会从
        // 「最近扫描 3 分钟前」变成「几秒前」，没有这句用户会以为没点上
        void confirm(scanLibraryConfirm(library.name)).then((ok) => {
          if (ok) run(startLibraryScan(library.id), `已开始扫描「${library.name}」`);
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
      onChapterImages: (library) => {
        void confirm(chapterImagesConfirm(library.name)).then(({ ok, checked }) => {
          if (ok) run(startLibraryChapterImages(library.id, { force: checked }));
        });
      },
      onEdit: (library) => setEditing(library),
      onSetDefault: (library) =>
        run(setDefaultLibrary(library.id), `已将「${library.name}」设为默认库`),
      // 只改这一个字段：payload 里没传的字段后端按"不改动"处理
      onToggleHome: (library) =>
        run(
          updateLibrary(library.id, {
            name: library.name,
            kind: library.kind,
            root_paths: library.root_paths,
            exclude_from_home: !library.exclude_from_home,
          }),
          library.exclude_from_home ? `「${library.name}」已在首页展示` : `「${library.name}」已从首页排除`,
        ),
      onDelete: (library) => {
        void confirm({
          title: `删除媒体库「${library.name}」？`,
          description: "磁盘文件不受影响，挂在它上面的订阅将回落到该类型的默认库。",
          confirmLabel: "删除库",
          tone: "danger",
        }).then((ok) => {
          if (ok) run(deleteLibrary(library.id), `已删除媒体库「${library.name}」`);
        });
      },
      onReorder: isMobile ? () => setReorderOpen(true) : undefined,
    }),
    [confirm, isMobile, router, run],
  );

  // —— 拖拽排序（桌面端、未筛选时）——
  const [dragId, setDragId] = useState<number | null>(null);
  // 拖到哪一行、落在它之前还是之后（按指针在行内的上下半判定）
  const [over, setOver] = useState<{ id: number; pos: "before" | "after" } | null>(null);
  const dragEnabled = !isMobile && !filterIsActive(filter) && (libraries?.length ?? 0) > 1;
  const dragFor = (library: MediaLibrary): LibraryRowDrag | null => {
    if (!dragEnabled || !libraries) return null;
    const index = libraries.findIndex((l) => l.id === library.id);
    return {
      dragging: dragId === library.id,
      over: over?.id === library.id && dragId !== library.id ? over.pos : null,
      onDragStart: (e) => {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", String(library.id));
        // 拖影用整行而不是那颗小把手，用户才看得出自己拖的是哪个库
        const row = (e.currentTarget as HTMLElement).closest("[data-library-row]");
        if (row instanceof HTMLElement) e.dataTransfer.setDragImage(row, 24, row.offsetHeight / 2);
        setDragId(library.id);
      },
      onDragOver: (e) => {
        if (dragId === null) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        const pos = dropPosition(e);
        if (over?.id !== library.id || over.pos !== pos) setOver({ id: library.id, pos });
      },
      onDrop: (e) => {
        e.preventDefault();
        if (dragId !== null && dragId !== library.id) {
          const from = libraries.findIndex((l) => l.id === dragId);
          const before = dropPosition(e) === "before";
          // 目标位置以「拿走被拖的那一行之后」的列表计：从上往下拖时目标行会前移一位
          const to = before ? (from < index ? index - 1 : index) : from < index ? index : index + 1;
          moveLibrary(dragId, to);
        }
        setDragId(null);
        setOver(null);
      },
      onDragEnd: () => {
        setDragId(null);
        setOver(null);
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
      <PageNav title="媒体库管理" fallback={{ label: "媒体库", href: "/library" as Route }} />

      {/* 页头：标题 + 说明，右侧是页面级动作「创建媒体库」（与首页「管理媒体库」
          同一位置约定：页面动作放标题行右端，顶栏只留返回与吸顶标题） */}
      <div className="px-6 pt-3 max-md:px-4">
        <div className="flex items-start justify-between gap-4">
          <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
            媒体库管理
          </h2>
          <button
            type="button"
            onClick={() => setEditing("new")}
            className="btn-accent mt-1 flex h-9 shrink-0 items-center gap-1 rounded-full py-0 pl-3 pr-4 text-ui font-semibold max-md:mt-0"
          >
            <PlusIcon className="size-4" />
            创建媒体库
          </button>
        </div>
        {/* 副标题是一行活的摘要，独占一行（不与按钮争宽，手机端才不会把数字挤断）：
            规模事实之后紧跟这页真正要你看的两件事——在跑任务与待处理文件——做成带色胶囊，
            点即筛选；两样都没有就明说「一切正常」 */}
        <div className="text-on-image mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-ui text-[var(--text-muted)] max-md:text-sub">
          <span>
            {libraries === null
              ? "正在汇总媒体库…"
              : libraries.length === 0
                ? "还没有媒体库"
                : summary.facts}
          </span>
          {summary.busy > 0 && (
            <FilterChip active={filter.focus === "busy"} onClick={() => toggleFocus("busy")}>
              <span className="size-1.5 rounded-full bg-[var(--info)]" />
              {summary.busy} 个在跑任务
            </FilterChip>
          )}
          {summary.attention > 0 && (
            <FilterChip
              active={filter.focus === "attention"}
              onClick={() => toggleFocus("attention")}
            >
              <span
                className={`size-1.5 rounded-full ${summary.missing ? "bg-[var(--danger)]" : "bg-[var(--warn)]"}`}
              />
              {summary.attention} 个库有待处理文件
            </FilterChip>
          )}
          {libraries !== null &&
            libraries.length > 0 &&
            summary.busy === 0 &&
            summary.attention === 0 && <span className="text-[var(--text-faint)]">一切正常</span>}
        </div>
      </div>

      {/* 标签栏：媒体库 / 回收站。回收站计数为 0 时标签照常渲染（入口要被看见），只是不带数字 */}
      <div className="mt-4 flex gap-1.5 px-6 max-md:px-4" role="tablist">
        {(
          [
            { id: "libraries" as const, label: "媒体库", count: libraries?.length ?? null },
            { id: "recycle" as const, label: "回收站", count: recycleCount },
            { id: "shares" as const, label: "分享", count: shareCount },
          ] as const
        ).map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={t.id === tab}
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-sub font-medium transition-colors ${
              t.id === tab
                ? "bg-white/[0.14] text-white"
                : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
            }`}
          >
            {t.label}
            {t.count !== null && t.count > 0 && (
              <span className={`tabular-nums ${t.id === tab ? "text-white/70" : "text-[var(--text-faint)]"}`}>
                {t.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {tab === "recycle" && <LibraryRecycleBin onCountChange={setRecycleCount} />}
      {tab === "shares" && <LibraryShares onCountChange={setShareCount} />}

      {tab === "libraries" && failed && libraries !== null && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {tab === "libraries" && libraries === null && !failed && (
        <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载媒体库…
        </div>
      )}
      {tab === "libraries" && failed && libraries === null && (
        <div className="mt-16 flex flex-col items-center gap-3 text-center">
          <p className="text-ui text-[var(--text-muted)]">媒体库加载失败</p>
          <button type="button" onClick={reload} className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]">
            重试
          </button>
        </div>
      )}

      {tab === "libraries" && libraries !== null && libraries.length === 0 && (
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

      {tab === "libraries" && libraries !== null && libraries.length > 0 && (
        <>
          {/* 工具栏：搜索 / 类型筛选（状态筛选在页头摘要的胶囊上） */}
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

          {/* 列表：不设表头——一行只有库名 / 库存 / 状态三样，各自的形态已经说明了自己是什么，
              一条表头只会让它更像一张表。筛选空结果时给「清除筛选」 */}
          <div className="mx-6 mt-4 overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.02] max-md:mx-4">
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
              <div role="list" aria-label="媒体库列表" className="divide-y divide-white/[0.06]">
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

          {/* 底部只留一句排序提示；状态胶囊自带文字，不需要颜色图例 */}
          <p className="mx-6 mt-3 text-caption text-[var(--text-faint)] max-md:mx-4">
            {isMobile
              ? "顺序即首页「我的媒体库」的展示顺序，在 ··· 菜单里「调整顺序」"
              : dragEnabled
                ? "把指针停在行上，拖动行首的把手调整首页「我的媒体库」的展示顺序，松手即保存"
                : filterIsActive(filter)
                  ? "清除筛选后可拖拽排序"
                  : ""}
          </p>
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
