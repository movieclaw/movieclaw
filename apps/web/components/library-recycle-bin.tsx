"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { ContentEmptyState } from "@/components/content-empty-state";
import { useConfirm, useToast } from "@/components/feedback";
import { ChevronDownIcon, SearchIcon, XIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import { Tooltip } from "@/components/tooltip";
import {
  type TrashedFile,
  type TrashedFilesData,
  type TrashedFilter,
  type TrashedItem,
  listTrashedFiles,
  purgeTrashedFiles,
  restoreTrashedFiles,
} from "@/lib/api/libraries";
import { formatBytes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import {
  type Countdown,
  type QualityLine,
  batchResultText,
  countdown,
  episodeCode,
  fileQualityLine,
  itemCountdown,
  itemFilesSummary,
  itemQualityLine,
  itemReasonText,
  itemSelection,
  reasonLabel,
  seasonsLabel,
  selectionSummary,
  summarySegments,
} from "@/lib/library-recycle";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { useIsMobile } from "@/lib/use-media-query";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/** 分页单位是条目（一部剧一行），不是文件 */
const PAGE_SIZE = 20;

/** 桌面端五列：复选框 / 条目 / 文件 · 品质 / 原因 / 自动清理 · 操作。表头与行共用。 */
const GRID_COLS =
  "grid-cols-[28px_minmax(0,1.1fr)_minmax(0,2fr)_minmax(0,1.15fr)_minmax(150px,auto)]";

interface Filter {
  q: string;
  libraryId: number | null;
  reason: string | null;
}
const EMPTY_FILTER: Filter = { q: "", libraryId: null, reason: null };

function toApiFilter(filter: Filter): TrashedFilter {
  return { q: filter.q.trim() || undefined, library_id: filter.libraryId, reason: filter.reason };
}

/**
 * 媒体库管理页的「回收站」标签（docs/design/library-recycle-bin.md）。
 *
 * 跨库汇总全部待回收文件，一个条目一行、可展开到每集；摘要行常驻回答"多少、
 * 多大、多急"，倒计时贴着「恢复 / 清理」按钮（"不管它会怎样" 对 "你可以怎样"）；
 * 勾选后底部悬浮批量条。列表只打一个接口，聚合口径与「立即清理全部」的作用域
 * 是同一份数字。
 */
export function LibraryRecycleBin({ onCountChange }: { onCountChange?: (total: number) => void }) {
  const confirm = useConfirm();
  const toast = useToast();
  const isMobile = useIsMobile();

  const [filter, setFilter] = useState<Filter>(EMPTY_FILTER);
  // 搜索框即时回显，请求按 300ms 去抖
  const [queryDraft, setQueryDraft] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<TrashedFilesData | null>(null);
  const [failed, setFailed] = useState(false);
  // 展开的条目键与勾选的文件 id 都只活在客户端；翻页 / 改筛选即重置
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => {
      setFilter((f) => (f.q === queryDraft ? f : { ...f, q: queryDraft }));
    }, 300);
    return () => clearTimeout(timer);
  }, [queryDraft]);

  // 乱序守卫：与管理页库列表同一套，慢响应不能覆盖新一轮结果
  const reloadSeq = useRef(0);
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    listTrashedFiles(toApiFilter(filter), { limit: PAGE_SIZE, offset })
      .then((next) => {
        if (seq !== reloadSeq.current) return;
        setFailed(false);
        setData((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
        onCountChange?.(next.total_files);
      })
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, [filter, offset, onCountChange]);

  useEffect(() => {
    reload();
  }, [reload]);
  useVisiblePolling(reload, 30_000);

  // 筛选变化回到第一页；翻页与筛选都清掉展开 / 勾选态
  const filterKey = `${filter.q}|${filter.libraryId}|${filter.reason}`;
  useEffect(() => {
    setOffset(0);
  }, [filterKey]);
  useEffect(() => {
    setExpanded(new Set());
    setSelected(new Set());
  }, [filterKey, offset]);

  // 当前页在磁盘上已被清掉（如清理全部后本页为空但总数不为 0）：回退到最后一页
  useEffect(() => {
    if (!data) return;
    if (data.items.length === 0 && offset > 0 && data.total_items > 0) {
      setOffset(Math.max(0, Math.floor((data.total_items - 1) / PAGE_SIZE) * PAGE_SIZE));
    }
  }, [data, offset]);

  const items = useMemo(() => data?.items ?? [], [data]);
  const selection = useMemo(() => selectionSummary(items, selected), [items, selected]);
  const filterActive = Boolean(filter.q.trim()) || filter.libraryId !== null || filter.reason !== null;

  // ---- 动作 --------------------------------------------------------------

  const runRestore = useCallback(
    async (ids: number[]) => {
      if (ids.length === 0 || busy) return;
      setBusy(true);
      try {
        const result = await restoreTrashedFiles(ids);
        (result.failed.length > 0 ? toast.error : toast.success)(batchResultText("恢复", result));
        setSelected((prev) => {
          const next = new Set(prev);
          for (const id of ids) next.delete(id);
          return next;
        });
        reload();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "恢复失败，请稍后重试");
      } finally {
        setBusy(false);
      }
    },
    [busy, reload, toast],
  );

  /**
   * 清理确认：条目行的「清理 N」、批量条的「立即清理所选」、展开行的「清理」、
   * 页级的「立即清理全部」复用同一个弹窗，只是数字与作用域不同。
   */
  const runPurge = useCallback(
    async (
      scope: { ids: number[] } | { filter: TrashedFilter },
      facts: { title: string; files: number; items: number; bytes: number; kept: number; wholeFilter: boolean },
    ) => {
      if (facts.files === 0 || busy) return;
      const bullets = [
        `${facts.files} 个文件` + (facts.items > 1 ? `，涉及 ${facts.items} 个条目` : ""),
        `释放空间 ${formatBytes(facts.bytes)}`,
      ];
      if (facts.kept > 0) bullets.push(`${facts.kept} 个仍在原位（移入回收站失败的文件，按当前路径删除）`);
      const ok = await confirm({
        title: facts.title,
        description:
          (facts.wholeFilter ? "会立即从磁盘删除当前筛选命中的全部文件，不只是本页。" : "会立即从磁盘删除。") +
          " 删除不可撤销；若其中有文件仍在下载器里做种，删除会中断做种任务。",
        bullets,
        confirmLabel: `清理 ${facts.files} 个文件`,
        cancelLabel: "先不",
        tone: "danger",
      });
      if (!ok) return;
      setBusy(true);
      try {
        const result = await purgeTrashedFiles(scope);
        (result.failed.length > 0 ? toast.error : toast.success)(batchResultText("清理", result));
        setSelected(new Set());
        reload();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "清理失败，请稍后重试");
      } finally {
        setBusy(false);
      }
    },
    [busy, confirm, reload, toast],
  );

  const purgeFiles = (files: TrashedFile[], title: string, itemCount = 1) =>
    runPurge(
      { ids: files.map((f) => f.id) },
      {
        title,
        files: files.length,
        items: itemCount,
        bytes: files.reduce((sum, f) => sum + f.size_bytes, 0),
        kept: files.filter((f) => f.kept_in_place).length,
        wholeFilter: false,
      },
    );

  const purgeAll = () => {
    if (!data) return;
    const libraryName = filter.libraryId !== null ? data.by_library.find((l) => l.library_id === filter.libraryId)?.name : null;
    runPurge(
      { filter: toApiFilter(filter) },
      {
        title: libraryName
          ? `清理「${libraryName}」库的全部待回收文件？`
          : filterActive
            ? "清理当前筛选下的全部待回收文件？"
            : "清理全部待回收文件？",
        files: data.total_files,
        items: data.total_items,
        bytes: data.total_bytes,
        kept: data.kept_in_place,
        wholeFilter: true,
      },
    );
  };

  const purgeSelected = () => {
    const files = items.flatMap((it) => it.files.filter((f) => selected.has(f.id)));
    const itemCount = items.filter((it) => it.files.some((f) => selected.has(f.id))).length;
    purgeFiles(files, `清理所选的 ${files.length} 个文件？`, itemCount);
  };

  const toggleExpanded = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const toggleItem = (item: TrashedItem) =>
    setSelected((prev) => {
      const next = new Set(prev);
      const all = itemSelection(item, prev) === "all";
      for (const f of item.files) {
        if (all) next.delete(f.id);
        else next.add(f.id);
      }
      return next;
    });

  const toggleFile = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const pageAll = items.length > 0 && items.every((it) => itemSelection(it, selected) === "all");
  const pageSome = !pageAll && items.some((it) => itemSelection(it, selected) !== "none");
  const togglePage = () =>
    setSelected(pageAll ? new Set() : new Set(items.flatMap((it) => it.files.map((f) => f.id))));

  // ---- 渲染 --------------------------------------------------------------

  if (data === null && !failed) {
    return (
      <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
        <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
        正在加载回收站…
      </div>
    );
  }
  if (data === null) {
    return (
      <div className="mt-16 flex flex-col items-center gap-3 text-center">
        <p className="text-ui text-[var(--text-muted)]">回收站加载失败</p>
        <button type="button" onClick={reload} className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]">
          重试
        </button>
      </div>
    );
  }

  if (data.total_files === 0 && !filterActive) {
    return (
      <ContentEmptyState
        variant="library"
        title="回收站是空的"
        description="洗版替换下来的旧版本会先停在这里 7 天再自动删除，期间可以恢复；文件本身放在各库根目录的 .movieclaw-trash 里。"
      />
    );
  }

  const pageStart = offset + 1;
  const pageEnd = offset + items.length;
  const pageCount = Math.max(1, Math.ceil(data.total_items / PAGE_SIZE));
  const pageIndex = Math.floor(offset / PAGE_SIZE);

  return (
    <>
      {failed && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {/* 摘要行：一句话回答"多少、多大、多急"；右侧是页面唯一的页级动作 */}
      <div className="mt-5 flex flex-wrap items-center justify-between gap-2 px-6 text-ui text-[var(--text-muted)] tabular-nums max-md:px-4">
        <span className="flex flex-wrap items-center gap-x-1.5">
          {summarySegments(data).map((seg, i) => (
            <span key={seg.text} className="inline-flex items-center gap-x-1.5">
              {i > 0 && <span aria-hidden>·</span>}
              <span
                className={
                  seg.tone === "soon"
                    ? "text-[var(--warn)]"
                    : seg.tone === "faint"
                      ? "text-[var(--text-faint)]"
                      : i < 3
                        ? "text-[var(--text)]"
                        : undefined
                }
              >
                {seg.text}
              </span>
            </span>
          ))}
        </span>
        <button
          type="button"
          disabled={busy || data.total_files === 0}
          onClick={purgeAll}
          className="flex h-8 items-center rounded-full border border-[rgba(255,107,107,0.35)] px-3 text-caption font-medium text-[var(--danger)] transition hover:bg-[rgba(255,107,107,0.12)] disabled:opacity-40"
        >
          立即清理全部 · {data.total_files}
        </button>
      </div>

      {/* 筛选：搜索 / 库胶囊 / 原因胶囊（分面计数，切换一组不归零另一组） */}
      <div className="mt-3 flex flex-wrap items-center gap-2.5 px-6 max-md:px-4">
        <label className="flex h-9 min-w-[220px] flex-1 items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.04] px-3 text-ui text-[var(--text-muted)] focus-within:border-[var(--accent)]/60 max-md:min-w-0 max-md:basis-full sm:max-w-[300px]">
          <SearchIcon className="size-4 shrink-0" />
          <input
            type="search"
            value={queryDraft}
            onChange={(e) => setQueryDraft(e.target.value)}
            placeholder="按片名、剧名或文件名搜索"
            aria-label="搜索回收站"
            className="min-w-0 flex-1 bg-transparent text-[var(--text)] outline-none placeholder:text-[var(--text-faint)]"
          />
          {queryDraft && (
            <button
              type="button"
              aria-label="清除搜索"
              onClick={() => setQueryDraft("")}
              className="grid size-5 place-items-center rounded-full hover:bg-white/[0.1]"
            >
              <XIcon className="size-3" />
            </button>
          )}
        </label>
        <div className="flex flex-wrap items-center gap-1.5 max-md:flex-nowrap max-md:overflow-x-auto max-md:pb-1">
          <Chip active={filter.libraryId === null} onClick={() => setFilter((f) => ({ ...f, libraryId: null }))}>
            全部库 {data.by_library.reduce((sum, l) => sum + l.count, 0)}
          </Chip>
          {data.by_library.map((lib) => (
            <Chip
              key={lib.library_id}
              active={filter.libraryId === lib.library_id}
              onClick={() =>
                setFilter((f) => ({ ...f, libraryId: f.libraryId === lib.library_id ? null : lib.library_id }))
              }
            >
              {lib.name} {lib.count}
            </Chip>
          ))}
          {data.by_reason.length > 0 && <span className="mx-1 h-4 w-px bg-white/[0.1]" aria-hidden />}
          {data.by_reason.map((r) => (
            <Chip
              key={r.reason}
              active={filter.reason === r.reason}
              onClick={() => setFilter((f) => ({ ...f, reason: f.reason === r.reason ? null : r.reason }))}
            >
              {reasonLabel(r.reason)} {r.count}
            </Chip>
          ))}
        </div>
      </div>

      {/* 列表：桌面端表格、手机端卡片 */}
      <div
        role="table"
        aria-label="待回收文件"
        className="mx-6 mt-4 overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.02] max-md:mx-4"
      >
        {!isMobile && (
          <div
            role="row"
            className={`grid items-center gap-4 border-b border-white/[0.07] px-4 py-2.5 text-caption font-medium text-[var(--text-faint)] ${GRID_COLS}`}
          >
            <span role="columnheader">
              <TriCheckbox state={pageAll ? "all" : pageSome ? "some" : "none"} onChange={togglePage} label="全选本页" />
            </span>
            <span role="columnheader">条目</span>
            <span role="columnheader">文件 · 品质</span>
            <span role="columnheader">原因</span>
            <span role="columnheader" className="text-right">
              自动清理 · 操作
            </span>
          </div>
        )}
        {items.length === 0 ? (
          <div className="px-4 py-10 text-center text-ui text-[var(--text-muted)]">
            没有符合条件的待回收文件
            <button
              type="button"
              onClick={() => {
                setQueryDraft("");
                setFilter(EMPTY_FILTER);
              }}
              className="ml-2 text-[var(--info)] hover:underline"
            >
              清除筛选
            </button>
          </div>
        ) : (
          <div role="rowgroup" className="divide-y divide-white/[0.06]">
            {items.map((item) =>
              isMobile ? (
                <ItemCard
                  key={item.key}
                  item={item}
                  expanded={expanded.has(item.key)}
                  selected={selected}
                  busy={busy}
                  onToggleExpanded={() => toggleExpanded(item.key)}
                  onToggleItem={() => toggleItem(item)}
                  onToggleFile={toggleFile}
                  onRestore={(files) => runRestore(files.map((f) => f.id))}
                  onPurge={(files, title) => purgeFiles(files, title)}
                />
              ) : (
                <ItemRow
                  key={item.key}
                  item={item}
                  expanded={expanded.has(item.key)}
                  selected={selected}
                  busy={busy}
                  onToggleExpanded={() => toggleExpanded(item.key)}
                  onToggleItem={() => toggleItem(item)}
                  onToggleFile={toggleFile}
                  onRestore={(files) => runRestore(files.map((f) => f.id))}
                  onPurge={(files, title) => purgeFiles(files, title)}
                />
              ),
            )}
          </div>
        )}
      </div>

      {/* 页脚：范围 + 保留期说明 + 页码 */}
      <div className="mx-6 mt-3 flex flex-wrap items-center justify-between gap-2 text-caption text-[var(--text-faint)] tabular-nums max-md:mx-4">
        <span>
          {items.length > 0 ? `第 ${pageStart}–${pageEnd} 个条目，共 ${data.total_items} 个条目 · ${data.total_files} 个文件` : ""}
          {items.length > 0 && " · "}
          回收站内文件保留 7 天后自动删除
        </span>
        {pageCount > 1 && (
          <Pager page={pageIndex} count={pageCount} onChange={(p) => setOffset(p * PAGE_SIZE)} />
        )}
      </div>

      {/* 勾选后的批量条：底部悬浮，不顶掉摘要行 */}
      {selection.count > 0 && (
        <div className="sticky bottom-4 z-10 mx-auto mt-4 flex w-max max-w-full items-center gap-3 rounded-full border border-white/[0.15] bg-[#1b1f2b] py-2 pl-4 pr-2.5 text-ui shadow-[0_14px_40px_rgba(0,0,0,0.55)]">
          <span className="whitespace-nowrap tabular-nums">
            已选 <b className="font-semibold">{selection.count}</b> 个文件 · {formatBytes(selection.bytes)}
          </span>
          <div className="flex items-center gap-1.5">
            <ActionButton disabled={busy} onClick={() => runRestore([...selected])}>
              {isMobile ? "恢复" : "恢复所选"}
            </ActionButton>
            <ActionButton danger disabled={busy} onClick={purgeSelected}>
              {isMobile ? "清理" : "立即清理所选"}
            </ActionButton>
            <button
              type="button"
              onClick={() => setSelected(new Set())}
              className="rounded-full px-2.5 py-1 text-caption text-[var(--text-muted)] hover:text-[var(--text)]"
            >
              取消
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// 行组件
// ---------------------------------------------------------------------------

interface RowProps {
  item: TrashedItem;
  expanded: boolean;
  selected: ReadonlySet<number>;
  busy: boolean;
  onToggleExpanded: () => void;
  onToggleItem: () => void;
  onToggleFile: (id: number) => void;
  onRestore: (files: TrashedFile[]) => void;
  onPurge: (files: TrashedFile[], title: string) => void;
}

function itemTitle(item: TrashedItem): string {
  return item.media_item?.title ?? item.files[0]?.file_name ?? "未识别文件";
}

function purgeTitle(item: TrashedItem): string {
  const title = itemTitle(item);
  return item.file_count > 1 ? `清理「${title}」的 ${item.file_count} 个待回收文件？` : `清理「${title}」的待回收文件？`;
}

/** 桌面端条目行 + 展开的文件行。 */
function ItemRow(props: RowProps) {
  const { item, expanded, selected, busy, onToggleExpanded, onToggleItem, onToggleFile, onRestore, onPurge } = props;
  const multi = item.file_count > 1;
  const single = !multi ? item.files[0] : null;
  const quality = single ? fileQualityLine(single) : itemQualityLine(item);
  const due = itemCountdown(item);
  const dueTip = dueTooltip(item);
  const failedFile = single?.last_error ?? null;

  return (
    <div role="rowgroup" className={single?.last_error ? "bg-[rgba(255,107,107,0.06)]" : undefined}>
      <div
        role="row"
        className={`grid items-center gap-4 px-4 py-3 transition-colors hover:bg-white/[0.025] ${GRID_COLS} ${
          itemSelection(item, selected) === "all" ? "bg-[rgba(205,214,230,0.07)]" : ""
        }`}
      >
        <div role="cell">
          <TriCheckbox state={itemSelection(item, selected)} onChange={onToggleItem} label={`选择「${itemTitle(item)}」`} />
        </div>
        <ItemCell item={item} multi={multi} expanded={expanded} onToggleExpanded={onToggleExpanded} />
        <div role="cell" className="min-w-0">
          {single ? (
            <FileName file={single} />
          ) : (
            <div className="truncate text-ui">
              <b className="font-semibold">{itemFilesSummary(item).split(" · ")[0]}</b>
              {itemFilesSummary(item).includes(" · ") && (
                <span className="text-[var(--text-muted)]"> · {itemFilesSummary(item).split(" · ")[1]}</span>
              )}
            </div>
          )}
          <QualityText line={quality} />
          {single?.kept_in_place && <KeptBadge />}
          {failedFile && <p className="mt-1 truncate text-caption text-[var(--danger)]">上次清理失败：{failedFile}</p>}
        </div>
        <div role="cell" className="min-w-0">
          <div className="truncate text-ui">{itemReasonText(item)}</div>
          <div className="truncate text-caption text-[var(--text-faint)]">
            {[item.trigger_label, item.latest_trashed_at ? formatRelativeTime(item.latest_trashed_at) : null]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
        <OpsCell
          due={due}
          tip={dueTip}
          count={multi ? item.file_count : null}
          busy={busy}
          onRestore={() => onRestore(item.files)}
          onPurge={() => onPurge(item.files, purgeTitle(item))}
        />
      </div>

      {expanded &&
        multi &&
        item.files.map((file, index) => (
          <FileRow
            key={file.id}
            item={item}
            file={file}
            last={index === item.files.length - 1}
            checked={selected.has(file.id)}
            busy={busy}
            onToggle={() => onToggleFile(file.id)}
            onRestore={() => onRestore([file])}
            onPurge={() => onPurge([file], `清理「${file.file_name}」？`)}
          />
        ))}
    </div>
  );
}

/** 展开的文件行：文件格跨「条目 + 文件」两列，第二行以「集号 集名」开头。 */
function FileRow({
  item,
  file,
  last,
  checked,
  busy,
  onToggle,
  onRestore,
  onPurge,
}: {
  item: TrashedItem;
  file: TrashedFile;
  last: boolean;
  checked: boolean;
  busy: boolean;
  onToggle: () => void;
  onRestore: () => void;
  onPurge: () => void;
}) {
  const quality = fileQualityLine(file);
  const due = countdown(file.purge_after);
  const code = episodeCode(file.season_number, file.episode_number);
  // 原因只在与条目行不同时写出（如某集实际被 WEB-DL 而非 BluRay 替换）
  const ownNote = item.note === null && file.note ? file.note : null;
  return (
    <div
      role="row"
      className={`grid items-center gap-4 bg-white/[0.018] px-4 py-2 text-sub ${GRID_COLS} ${
        checked ? "bg-[rgba(205,214,230,0.07)]" : ""
      } ${last ? "border-b border-white/[0.06]" : ""}`}
    >
      <div role="cell">
        <TriCheckbox state={checked ? "all" : "none"} onChange={onToggle} label={`选择「${file.file_name}」`} />
      </div>
      <div role="cell" className="col-span-2 min-w-0 pl-[58px]">
        <FileName file={file} muted />
        <div className="truncate text-caption text-[var(--text-faint)]">
          {code && (
            <>
              <span className="font-mono font-semibold text-[var(--text)]">{code}</span>
              {file.episode_title && <span className="ml-1.5 text-[var(--text-muted)]">{file.episode_title}</span>}
              <span aria-hidden> · </span>
            </>
          )}
          <QualityText line={quality} inline muted />
        </div>
        {file.kept_in_place && <KeptBadge />}
        {file.last_error && <p className="mt-0.5 truncate text-caption text-[var(--danger)]">上次清理失败：{file.last_error}</p>}
      </div>
      <div role="cell" className="min-w-0 truncate text-caption text-[var(--text-faint)]">
        {ownNote}
      </div>
      <OpsCell
        due={due}
        tip={file.purge_after ? formatDateTime(file.purge_after) : null}
        count={null}
        busy={busy}
        compact
        onRestore={onRestore}
        onPurge={onPurge}
      />
    </div>
  );
}

/** 手机端：一个条目一卡，读法与桌面同构（种子名 / 品质 / 原因 / 倒计时 + 按钮）。 */
function ItemCard(props: RowProps) {
  const { item, expanded, selected, busy, onToggleExpanded, onToggleItem, onToggleFile, onRestore, onPurge } = props;
  const multi = item.file_count > 1;
  const single = !multi ? item.files[0] : null;
  const quality = single ? fileQualityLine(single) : itemQualityLine(item);
  const due = itemCountdown(item);
  return (
    <div className={`px-4 py-3.5 ${itemSelection(item, selected) === "all" ? "bg-[rgba(205,214,230,0.07)]" : ""}`}>
      <div className="flex items-center gap-3">
        <TriCheckbox state={itemSelection(item, selected)} onChange={onToggleItem} label={`选择「${itemTitle(item)}」`} />
        <ItemIdentity item={item} />
      </div>
      {single ? (
        <FileName file={single} clamp className="mt-2" />
      ) : (
        <button type="button" onClick={onToggleExpanded} className="mt-2 flex items-center gap-1.5 text-ui">
          <ChevronDownIcon className={`size-3.5 text-[var(--text-faint)] transition-transform ${expanded ? "" : "-rotate-90"}`} />
          <b className="font-semibold">{itemFilesSummary(item)}</b>
          <span className="text-caption text-[var(--text-faint)]">{expanded ? "收起" : "展开"}</span>
        </button>
      )}
      <div className="mt-1 text-caption text-[var(--text-faint)]">
        <QualityText line={quality} inline wrap />
      </div>
      {single?.kept_in_place && <KeptBadge />}
      {single?.last_error && <p className="mt-1 text-caption text-[var(--danger)]">上次清理失败：{single.last_error}</p>}
      <div className="mt-1 text-sub text-[var(--text-muted)]">{itemReasonText(item)}</div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <DueText due={due} suffix="自动清理" />
        <div className="flex items-center gap-1.5">
          <ActionButton disabled={busy} onClick={() => onRestore(item.files)}>
            恢复{multi ? ` ${item.file_count}` : ""}
          </ActionButton>
          <ActionButton danger disabled={busy} onClick={() => onPurge(item.files, purgeTitle(item))}>
            清理{multi ? ` ${item.file_count}` : ""}
          </ActionButton>
        </div>
      </div>
      {expanded && multi && (
        <div className="mt-2 divide-y divide-white/[0.06] border-t border-white/[0.06]">
          {item.files.map((file) => {
            const code = episodeCode(file.season_number, file.episode_number);
            const fileDue = countdown(file.purge_after);
            return (
              <div key={file.id} className="grid grid-cols-[16px_minmax(0,1fr)_auto] gap-x-2 gap-y-0.5 py-2 text-caption">
                <TriCheckbox
                  state={selected.has(file.id) ? "all" : "none"}
                  onChange={() => onToggleFile(file.id)}
                  label={`选择「${file.file_name}」`}
                  className="mt-0.5"
                />
                <FileName file={file} muted clamp />
                <DueText due={fileDue} className="row-span-2 self-start text-right" short />
                <div className="col-start-2 text-[var(--text-faint)]">
                  {code && (
                    <>
                      <span className="font-mono font-semibold text-[var(--text)]">{code}</span>
                      {file.episode_title && <span className="ml-1.5 text-[var(--text-muted)]">{file.episode_title}</span>}
                      <span aria-hidden> · </span>
                    </>
                  )}
                  <QualityText line={fileQualityLine(file)} inline muted wrap />
                </div>
                <div className="col-start-2 flex items-center gap-1.5 pt-1">
                  <ActionButton small disabled={busy} onClick={() => onRestore([file])}>
                    恢复
                  </ActionButton>
                  <ActionButton small danger disabled={busy} onClick={() => onPurge([file], `清理「${file.file_name}」？`)}>
                    清理
                  </ActionButton>
                  {file.kept_in_place && <KeptBadge inline />}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 格子
// ---------------------------------------------------------------------------

/** 条目格：›（多文件时可点）· 海报 · 片名 + 年份 / 库名 + 季。 */
function ItemCell({
  item,
  multi,
  expanded,
  onToggleExpanded,
}: {
  item: TrashedItem;
  multi: boolean;
  expanded: boolean;
  onToggleExpanded: () => void;
}) {
  return (
    <div role="cell" className="flex min-w-0 items-center gap-2">
      {multi ? (
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={expanded ? "收起文件" : `展开 ${item.file_count} 个文件`}
          onClick={onToggleExpanded}
          className="grid size-5 shrink-0 place-items-center rounded text-[var(--text-faint)] hover:bg-white/[0.08] hover:text-[var(--text)]"
        >
          <ChevronDownIcon className={`size-3.5 transition-transform ${expanded ? "" : "-rotate-90"}`} />
        </button>
      ) : (
        <span className="size-5 shrink-0" aria-hidden />
      )}
      <ItemIdentity item={item} />
    </div>
  );
}

/** 海报 + 片名（链到条目详情页）+ 年份，第二行库名 · 季。 */
function ItemIdentity({ item }: { item: TrashedItem }) {
  const media = item.media_item;
  const seasons = seasonsLabel(item.seasons);
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <PosterImage
        src={media ? imageUrl(media.poster_url) : null}
        alt=""
        className="h-11 w-[30px] shrink-0 rounded-[5px] border border-white/[0.08] object-cover"
      />
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 truncate text-ui font-semibold text-[var(--text)]">
          {media ? (
            <Link href={`/library/${item.library.id}/item/${media.id}` as Route} className="truncate hover:underline">
              {media.title}
            </Link>
          ) : (
            <span className="truncate">{itemTitle(item)}</span>
          )}
          {media?.year && <span className="shrink-0 font-normal text-[var(--text-faint)]">{media.year}</span>}
        </div>
        <div className="truncate text-caption text-[var(--text-faint)]">
          {item.library.name}
          {seasons && ` · ${seasons}`}
        </div>
      </div>
    </div>
  );
}

/**
 * 文件名：等宽、整格宽度。点它（桌面悬停也行）弹出完整存放路径——「原路径」是恢复
 * 回去的位置，「现在的位置」是回收站内的当前路径；手机端没有悬停，所以用项目统一的
 * Tooltip 走点击，而不是原生 title。
 */
function FileName({
  file,
  muted = false,
  clamp = false,
  className = "",
}: {
  file: TrashedFile;
  muted?: boolean;
  /** 手机端：允许折两行不截断 */
  clamp?: boolean;
  className?: string;
}) {
  return (
    <Tooltip
      openOnClick
      maxWidth={560}
      content={
        <div className="space-y-2 text-caption leading-5">
          <div>
            <div className="text-[var(--text-faint)]">原路径（恢复回这里）</div>
            <div className="break-all font-mono text-[var(--text)]">
              {file.trash_original_path ?? file.file_path}
            </div>
          </div>
          <div>
            <div className="text-[var(--text-faint)]">现在的位置</div>
            <div className="break-all font-mono text-[var(--text)]">
              {file.kept_in_place
                ? "仍在原路径（移入回收站失败，清理时按这个路径删除）"
                : file.file_path}
            </div>
          </div>
        </div>
      }
    >
      <button
        type="button"
        aria-label={`查看「${file.file_name}」的存放路径`}
        className={`block max-w-full text-left font-mono text-caption decoration-white/30 hover:underline ${
          muted ? "text-[var(--text-muted)]" : "text-[var(--text)]"
        } ${
          clamp
            ? "overflow-hidden break-all leading-5 [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2]"
            : "w-full truncate"
        } ${className}`}
      >
        {file.file_name}
      </button>
    </Tooltip>
  );
}

/** 品质行：档位加粗（混合带计数）· HDR 徽标 · 大小 · 编码 · 音轨 · 制作组。 */
function QualityText({
  line,
  inline = false,
  muted = false,
  wrap = false,
}: {
  line: QualityLine;
  inline?: boolean;
  muted?: boolean;
  wrap?: boolean;
}) {
  const body = (
    <>
      {line.tiers.map((tier, i) => (
        <span key={tier.label}>
          {i > 0 && <span className="text-[var(--text-faint)]"> · </span>}
          <b className={`${muted ? "font-medium text-[var(--text-muted)]" : "font-semibold text-[var(--text)]"}`}>{tier.label}</b>
          {tier.count !== null && <span className="ml-1 text-micro text-[var(--text-faint)]">{tier.count}</span>}
        </span>
      ))}
      {line.hdr.map((h) => (
        <span
          key={h}
          className="ml-1.5 rounded-full border border-[rgba(232,201,138,0.45)] px-1.5 text-micro font-semibold tracking-wide text-[#e8c98a]"
        >
          {h}
        </span>
      ))}
      {line.rest.map((part, i) => (
        <span key={`${part}-${i}`} className="text-[var(--text-faint)]">
          {" · "}
          {i === 0 ? <span className="text-[var(--text-muted)]">{part}</span> : part}
        </span>
      ))}
    </>
  );
  if (inline) return <span className={wrap ? "" : "whitespace-nowrap"}>{body}</span>;
  return <div className={`mt-0.5 text-caption ${wrap ? "" : "truncate"}`}>{body}</div>;
}

function KeptBadge({ inline = false }: { inline?: boolean }) {
  return (
    <span
      title="移入回收站失败，文件仍在原路径；清理按当前路径删除"
      className={`inline-flex rounded-full border border-[rgba(245,196,81,0.4)] px-1.5 text-micro text-[var(--warn)] ${inline ? "" : "mt-1"}`}
    >
      原地
    </span>
  );
}

function dueTooltip(item: TrashedItem): string | null {
  if (!item.earliest_purge_after) return null;
  if (item.file_count > 1 && item.latest_purge_after) {
    return `最早 ${formatDateTime(item.earliest_purge_after)} · 最晚 ${formatDateTime(item.latest_purge_after)}`;
  }
  return formatDateTime(item.earliest_purge_after);
}

function DueText({
  due,
  suffix,
  short = false,
  className = "",
}: {
  due: Countdown;
  suffix?: string;
  short?: boolean;
  className?: string;
}) {
  const tone =
    due.tone === "soon" ? "font-semibold text-[var(--warn)]" : due.tone === "never" ? "text-[var(--text-faint)]" : "text-[var(--text-muted)]";
  const text = short ? due.text.replace(/后$/, "").replace(/^最早 /, "") : due.text;
  return (
    <span className={`whitespace-nowrap text-caption tabular-nums ${tone} ${className}`}>
      {text}
      {suffix && due.tone !== "never" ? suffix : ""}
    </span>
  );
}

/** 「自动清理 · 操作」格：倒计时在按钮正上方，右对齐。 */
function OpsCell({
  due,
  tip,
  count,
  busy,
  compact = false,
  onRestore,
  onPurge,
}: {
  due: Countdown;
  tip: string | null;
  count: number | null;
  busy: boolean;
  compact?: boolean;
  onRestore: () => void;
  onPurge: () => void;
}) {
  return (
    <div role="cell" className="flex flex-col items-end gap-1">
      <span title={tip ?? undefined}>
        <DueText due={due} />
      </span>
      <div className="flex items-center gap-1.5">
        <ActionButton small={compact} disabled={busy} onClick={onRestore}>
          恢复{count ? ` ${count}` : ""}
        </ActionButton>
        <ActionButton small={compact} danger disabled={busy} onClick={onPurge}>
          清理{count ? ` ${count}` : ""}
        </ActionButton>
      </div>
    </div>
  );
}

function ActionButton({
  danger = false,
  small = false,
  disabled,
  onClick,
  children,
}: {
  danger?: boolean;
  small?: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`rounded-full border transition disabled:opacity-40 ${small ? "px-2 py-px text-micro" : "px-2.5 py-0.5 text-caption"} font-medium ${
        danger
          ? "border-[rgba(255,107,107,0.35)] text-[var(--danger)] hover:bg-[rgba(255,107,107,0.12)]"
          : "border-white/[0.15] text-[var(--text)] hover:bg-white/[0.08]"
      }`}
    >
      {children}
    </button>
  );
}

/** 三态复选框：整组 / 半选（–）/ 未选。原生 input 的 indeterminate 只能经 ref 设置。 */
function TriCheckbox({
  state,
  onChange,
  label,
  className = "",
}: {
  state: "all" | "some" | "none";
  onChange: () => void;
  label: string;
  className?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = state === "some";
  }, [state]);
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      checked={state === "all"}
      onChange={onChange}
      className={`size-4 cursor-pointer accent-[var(--accent)] ${className}`}
    />
  );
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`flex h-7 shrink-0 items-center gap-1.5 rounded-full border px-2.5 text-caption font-medium transition ${
        active
          ? "border-white/[0.2] bg-white/[0.14] text-[var(--text)]"
          : "border-white/[0.1] text-[var(--text-muted)] hover:bg-white/[0.06] hover:text-[var(--text)]"
      }`}
    >
      {children}
    </button>
  );
}

/** 页码：‹ 1 2 … 7 ›，当前页前后各两页，其余折成省略号。 */
function Pager({ page, count, onChange }: { page: number; count: number; onChange: (page: number) => void }) {
  const pages: (number | "…")[] = [];
  for (let i = 0; i < count; i += 1) {
    if (i === 0 || i === count - 1 || Math.abs(i - page) <= 2) pages.push(i);
    else if (pages[pages.length - 1] !== "…") pages.push("…");
  }
  const btn = "grid h-7 min-w-7 place-items-center rounded-lg border text-caption tabular-nums transition";
  return (
    <nav aria-label="分页" className="flex items-center gap-1">
      <button type="button" disabled={page === 0} onClick={() => onChange(page - 1)} className={`${btn} border-transparent text-[var(--text-muted)] hover:bg-white/[0.06] disabled:opacity-40`} aria-label="上一页">
        ‹
      </button>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`gap-${i}`} className="px-1 text-[var(--text-faint)]">
            …
          </span>
        ) : (
          <button
            key={p}
            type="button"
            aria-current={p === page ? "page" : undefined}
            onClick={() => onChange(p)}
            className={`${btn} ${p === page ? "border-white/[0.15] bg-white/[0.12] text-[var(--text)]" : "border-transparent text-[var(--text-muted)] hover:bg-white/[0.06]"}`}
          >
            {p + 1}
          </button>
        ),
      )}
      <button type="button" disabled={page >= count - 1} onClick={() => onChange(page + 1)} className={`${btn} border-transparent text-[var(--text-muted)] hover:bg-white/[0.06] disabled:opacity-40`} aria-label="下一页">
        ›
      </button>
    </nav>
  );
}
