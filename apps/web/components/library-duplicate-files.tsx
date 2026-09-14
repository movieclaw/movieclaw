"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { ContentEmptyState } from "@/components/content-empty-state";
import { useConfirm, useToast } from "@/components/feedback";
import { SearchIcon, XIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import { Tooltip } from "@/components/tooltip";
import {
  type DuplicateFile,
  type DuplicateFilesData,
  type DuplicateGroup,
  type DuplicateItem,
  type DuplicateReviewKind,
  type DuplicateSeason,
  type DuplicateTier,
  type DuplicateUnit,
  type DuplicateVersion,
  type MediaLibrary,
  type TrashedBatchResult,
  listDuplicateFiles,
  resolveAllDuplicates,
  resolveDuplicates,
  startDuplicateScan,
} from "@/lib/api/libraries";
import { formatBytes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import {
  TIER_ACTION_LABELS,
  episodeLabel,
  fileNote,
  groupSummary,
  hiddenNote,
  isScanning,
  keepFileFacts,
  keepVersionFacts,
  qualitySegments,
  resolveResultText,
  scanNote,
  seasonHeadline,
  suggestedOf,
  tierFacts,
  versionCoverage,
} from "@/lib/library-duplicates";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/** 分页单位是条目（一部剧一块），不是文件 */
const PAGE_SIZE = 20;
/** 后端一次批量最多处理的文件数（与 api/routes/library_duplicates.BATCH_LIMIT 同） */
const BATCH_LIMIT = 500;

/** 文件行 / 版本行的四列：名字或规格 / 规格或覆盖 / 来源 / 动作。手机上叠成一列。 */
const ROW_GRID =
  "grid items-center gap-x-4 gap-y-1 max-md:grid-cols-1 md:grid-cols-[minmax(0,1.5fr)_minmax(0,1.1fr)_minmax(0,1fr)_auto]";

/** 三档的配色：只有「可以放心清理」用主色实心按钮，另两档都在动"有区别"的文件 */
const TIER_TONE: Record<DuplicateTier, string> = {
  safe: "border-[rgba(74,222,128,0.35)] bg-[rgba(74,222,128,0.06)]",
  suggested: "border-[rgba(232,201,138,0.3)] bg-[rgba(232,201,138,0.05)]",
  review: "border-white/[0.1] bg-white/[0.02]",
};

interface Filter {
  q: string;
  libraryId: number | null;
  itemId: number | null;
}

/** 当前站在哪一层：摘要页，还是某一档（可能再细到某一种取舍）的明细。 */
interface Focus {
  tier: DuplicateTier | null;
  reviewKind: DuplicateReviewKind | null;
}

const NO_FOCUS: Focus = { tier: null, reviewKind: null };

/**
 * 媒体库管理页的「重复文件」标签（docs/design/library-duplicate-files.md §5 / §9）。
 *
 * 页面分两层。**落地是一张摘要**：扫过没有、上次什么时候、三档各有多少活——
 * 可以放心清理 / 建议清理 / 需要你决定，最后一档再按取舍类型分组。点进某一档
 * 才是明细：堆内一个条目一块，电影块列文件行，剧集块按季列版本行或各集。
 *
 * 这两层是给"一万个文件扫出几千条重复"的库准备的：第一版落地就把几千条文件
 * 铺满一屏，用户知道有事要干却不知道从哪下手。分档的判据只有一条——**机器有
 * 没有把握**，它不改变任何判定，只决定先给你看什么。
 *
 * 数据全部来自上一轮扫描落库的结论，打开页面不会触发检测；只有扫描在跑时才
 * 轮询（看进度），跑完即停。
 */
export function LibraryDuplicateFiles({
  libraries,
  initialItemId = null,
  onCountChange,
}: {
  libraries: MediaLibrary[] | null;
  /** 条目详情页「处理重复」带来的 ?item=，只看这一个条目 */
  initialItemId?: number | null;
  onCountChange?: (total: number) => void;
}) {
  const confirm = useConfirm();
  const toast = useToast();

  const [filter, setFilter] = useState<Filter>({ q: "", libraryId: null, itemId: initialItemId });
  const [focus, setFocus] = useState<Focus>(NO_FOCUS);
  const [queryDraft, setQueryDraft] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<DuplicateFilesData | null>(null);
  const [failed, setFailed] = useState(false);
  // 「展开各集」只活在客户端；翻页 / 改筛选即重置
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => {
      setFilter((f) => (f.q === queryDraft ? f : { ...f, q: queryDraft }));
    }, 300);
    return () => clearTimeout(timer);
  }, [queryDraft]);

  // 明细层：选了某一档，或者从条目详情页带 ?item= 进来（只看那一个条目）
  const detail = focus.tier !== null || filter.itemId !== null;

  const reloadSeq = useRef(0);
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    listDuplicateFiles(
      {
        tier: focus.tier,
        review_kind: focus.reviewKind,
        q: filter.q.trim() || undefined,
        library_id: filter.libraryId,
        media_item_id: filter.itemId,
      },
      // 摘要层不拉明细：一条聚合查询就够，几千条重复也是一瞬间
      { limit: detail ? PAGE_SIZE : 0, offset: detail ? offset : 0 },
    )
      .then((next) => {
        if (seq !== reloadSeq.current) return;
        setFailed(false);
        setData((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
        if (filter.itemId === null && filter.libraryId === null && !filter.q.trim()) {
          onCountChange?.(next.total_files);
        }
      })
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, [detail, filter, focus, offset, onCountChange]);

  useEffect(() => {
    reload();
  }, [reload]);
  // 页面上的数字是扫描落下的结论，不会自己变——只有扫描在跑时才需要盯着看进度
  const scanning = data !== null && isScanning(data.scan);
  useVisiblePolling(reload, scanning ? 3_000 : null);

  const filterKey = `${filter.q}|${filter.libraryId}|${filter.itemId}|${focus.tier}|${focus.reviewKind}`;
  useEffect(() => {
    setOffset(0);
  }, [filterKey]);
  useEffect(() => {
    setExpanded(new Set());
  }, [filterKey, offset]);
  useEffect(() => {
    if (data && detail && data.items.length === 0 && offset > 0 && data.total_items > 0) {
      setOffset(Math.max(0, Math.floor((data.total_items - 1) / PAGE_SIZE) * PAGE_SIZE));
    }
  }, [data, detail, offset]);

  const filterActive = Boolean(filter.q.trim()) || filter.libraryId !== null || filter.itemId !== null;
  const itemTitle = useMemo(
    () => data?.items.find((it) => it.media_item.id === filter.itemId)?.media_item.title ?? null,
    [data, filter.itemId],
  );
  const focusGroup = useMemo<DuplicateGroup | null>(() => {
    if (data === null || focus.tier === null) return null;
    if (focus.reviewKind !== null) {
      return data.review_groups.find((g) => g.key === focus.reviewKind) ?? null;
    }
    return data.tiers.find((g) => g.key === focus.tier) ?? null;
  }, [data, focus]);

  // ---- 动作 --------------------------------------------------------------

  const run = useCallback(
    async (action: () => Promise<TrashedBatchResult>, successText?: string) => {
      if (busy) return;
      setBusy(true);
      try {
        const result = await action();
        const text = successText ?? resolveResultText(result);
        (result.failed.length > 0 ? toast.error : toast.success)(text);
        reload();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "操作失败，请稍后重试");
      } finally {
        setBusy(false);
      }
    },
    [busy, reload, toast],
  );

  /** 「开始扫描」：后台作业算一轮，页面盯着进度。 */
  const startScan = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const started = await startDuplicateScan();
      toast.success(started.created ? "已开始扫描重复文件" : "扫描正在进行中");
      reload();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "启动扫描失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }, [busy, reload, toast]);

  /** 「留这个」：留下这一个文件，单元里其余移入回收站。 */
  const keepFile = async (item: DuplicateItem, unit: DuplicateUnit, file: DuplicateFile) => {
    const facts = keepFileFacts(unit, file);
    const ok = await confirm({
      title: "留下这个，其余移入回收站？",
      description: `留下 ${file.file_name}。移入回收站的文件 7 天内可恢复。`,
      bullets: facts.gone.map((f) => `${f.file_name} · ${f.quality_label} · ${formatBytes(f.size_bytes)}`),
      confirmLabel: `移入回收站 · ${facts.gone.length}`,
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    await run(() =>
      resolveDuplicates({
        media_item_id: item.media_item.id,
        season_number: unit.season_number,
        episode_number: unit.episode_number,
        keep_file_id: file.id,
      }),
    );
  };

  /** 「整季留这个版本」：每集留该版本，缺该版本的集留建议保留者。 */
  const keepVersion = async (item: DuplicateItem, season: DuplicateSeason, version: DuplicateVersion) => {
    const facts = keepVersionFacts(season, version);
    const bullets = facts.gone.slice(0, 8).map((f) => `${f.file_name} · ${f.quality_label}`);
    if (facts.gone.length > 8) bullets.push(`… 共 ${facts.gone.length} 个文件`);
    const missing = facts.missingEpisodes.map(episodeLabel).join("、");
    const ok = await confirm({
      title: `《${item.media_item.title}》S${String(season.season_number).padStart(2, "0")} 整季留下这个版本？`,
      description:
        `留下 ${version.quality_label} · ${version.origin_label}（${version.episodes.length} 集）。` +
        (missing ? ` ${missing} 没有这个版本，这 ${facts.missingEpisodes.length} 集保留原有文件。` : "") +
        ` 移入回收站 ${facts.gone.length} 个文件 · ${formatBytes(facts.bytes)}，7 天内可恢复。`,
      bullets,
      confirmLabel: `移入回收站 · ${facts.gone.length}`,
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    await run(() =>
      resolveDuplicates({
        media_item_id: item.media_item.id,
        season_number: season.season_number,
        keep_version: version.key,
      }),
    );
  };

  /** 「都留着」：这些版本都是我要的，单元不再列出（直到下一轮扫描发现新文件）。可撤销，不弹确认。 */
  const keepAll = (item: DuplicateItem, season: DuplicateSeason) =>
    run(
      () =>
        resolveDuplicates({
          media_item_id: item.media_item.id,
          season_number: season.season_number,
          episode_number: item.media_item.kind === "tv" ? null : 0,
          keep_all: true,
        }),
      item.media_item.kind === "tv" ? "整季都留着：不再列出，直到有新文件进来" : "都留着：不再列出，直到有新文件进来",
    );

  /** 一整档 / 一组按「建议保留」清理。除「可以放心清理」外都逐条列出会清掉的东西。 */
  const cleanGroup = async (tier: DuplicateTier, reviewKind: DuplicateReviewKind | null, group: DuplicateGroup) => {
    if (!data) return;
    const facts = tierFacts(data, group);
    const libraryName = filter.libraryId !== null ? libraries?.find((l) => l.id === filter.libraryId)?.name : null;
    const scope = libraryName ? `「${libraryName}」库` : "全部库";
    const ok = await confirm({
      title: tier === "safe" ? `清理${scope}一模一样的文件？` : `按建议清理「${group.label}」？`,
      description:
        `${scope} · ${group.files} 个文件 · ${formatBytes(group.bytes)} · ` +
        "每个单元留下「建议保留」的那个 · 7 天内可在回收站恢复。" +
        (tier === "safe" ? "" : " 这些文件与保留者有区别；想留的请先取消，回去点那个单元的「都留着」。") +
        (group.files > BATCH_LIMIT ? ` 一次最多处理 ${BATCH_LIMIT} 个，剩下的再点一次。` : ""),
      bullets: tier === "safe" ? undefined : facts.lines,
      confirmLabel: `移入回收站 · ${Math.min(group.files, BATCH_LIMIT)}`,
      cancelLabel: "先不",
      tone: tier === "safe" ? "default" : "danger",
    });
    if (!ok) return;
    await run(() => resolveAllDuplicates({ tier, review_kind: reviewKind, library_id: filter.libraryId }));
  };

  /** 一整组「都留着」：同一种取舍只回答一次，不动文件，可在文件区逐个撤销。 */
  const keepGroup = async (tier: DuplicateTier, reviewKind: DuplicateReviewKind | null, group: DuplicateGroup) => {
    const ok = await confirm({
      title: `「${group.label}」都留着？`,
      description: `${group.units} 个单元的文件全部保留、不再列为重复。不会动任何文件，之后可以在条目详情页逐个撤销。`,
      confirmLabel: `都留着 · ${group.units}`,
      cancelLabel: "先不",
    });
    if (!ok) return;
    await run(
      () => resolveAllDuplicates({ tier, review_kind: reviewKind, library_id: filter.libraryId, keep_all: true }),
      `已标记「都留着」：${group.units} 个单元不再列为重复`,
    );
  };

  const toggleExpanded = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  // ---- 渲染 --------------------------------------------------------------

  if (data === null && !failed) {
    return (
      <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
        <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
        正在读取扫描结果…
      </div>
    );
  }
  if (data === null) {
    return (
      <div className="mt-16 flex flex-col items-center gap-3 text-center">
        <p className="text-ui text-[var(--text-muted)]">重复文件加载失败</p>
        <button type="button" onClick={reload} className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]">
          重试
        </button>
      </div>
    );
  }

  const scanBar = (
    <ScanBar scan={data.scan} busy={busy} scanning={scanning} onScan={startScan} />
  );

  // 从来没扫过：页面上只有一件事可做（扫描失败过的不算——那要让人看见失败原因）
  if (data.scan.status === null && !scanning) {
    return (
      <>
        {scanBar}
        <ContentEmptyState
          variant="library"
          title="还没有扫描过重复文件"
          description={
            "重复检测要逐个文件比对指纹与规格，是一件要跑一会儿的事，所以由你来触发。" +
            "扫完之后这里会按「可以放心清理 / 建议清理 / 需要你决定」分好，你再决定先做哪一档。"
          }
          action={
            <button
              type="button"
              disabled={busy}
              onClick={startScan}
              className="flex h-9 items-center rounded-full border border-[var(--accent)] bg-[var(--accent)] px-4 text-ui font-medium text-[#0a0b10] transition hover:opacity-90 disabled:opacity-40"
            >
              开始扫描
            </button>
          }
        />
      </>
    );
  }
  if (data.total_units === 0 && !filterActive && !scanning) {
    return (
      <>
        {scanBar}
        <ContentEmptyState
          variant="library"
          title="没有重复文件"
          description={
            "每部电影、每一集都只有一个在位文件。" + (hiddenNote(data.scan) ? ` ${hiddenNote(data.scan)}。` : "")
          }
        />
      </>
    );
  }

  const pageCount = Math.max(1, Math.ceil(data.total_items / PAGE_SIZE));
  const pageIndex = Math.floor(offset / PAGE_SIZE);
  const libraryChips = (libraries ?? []).filter(
    (l) => l.id === filter.libraryId || (libraries ?? []).length > 1,
  );

  return (
    <>
      {scanBar}
      {failed && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {/* 筛选：搜索 / 库胶囊 / 只看某条目（详情页带来的） */}
      <div className="mt-4 flex flex-wrap items-center gap-2.5 px-6 max-md:px-4">
        <label className="flex h-9 min-w-[220px] flex-1 items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.04] px-3 text-ui text-[var(--text-muted)] focus-within:border-[var(--accent)]/60 max-md:min-w-0 max-md:basis-full sm:max-w-[300px]">
          <SearchIcon className="size-4 shrink-0" />
          <input
            type="search"
            value={queryDraft}
            onChange={(e) => setQueryDraft(e.target.value)}
            placeholder="按片名或剧名搜索"
            aria-label="搜索重复文件"
            className="min-w-0 flex-1 bg-transparent text-[var(--text)] outline-none placeholder:text-[var(--text-faint)]"
          />
          {queryDraft && (
            <button type="button" aria-label="清除搜索" onClick={() => setQueryDraft("")} className="grid size-5 place-items-center rounded-full hover:bg-white/[0.1]">
              <XIcon className="size-3" />
            </button>
          )}
        </label>
        <div className="flex flex-wrap items-center gap-1.5 max-md:flex-nowrap max-md:overflow-x-auto max-md:pb-1">
          <Chip active={filter.libraryId === null} onClick={() => setFilter((f) => ({ ...f, libraryId: null }))}>
            全部库
          </Chip>
          {libraryChips.map((lib) => (
            <Chip
              key={lib.id}
              active={filter.libraryId === lib.id}
              onClick={() => setFilter((f) => ({ ...f, libraryId: f.libraryId === lib.id ? null : lib.id }))}
            >
              {lib.name}
            </Chip>
          ))}
          {filter.itemId !== null && (
            <Chip active onClick={() => setFilter((f) => ({ ...f, itemId: null }))}>
              只看{itemTitle ? `《${itemTitle}》` : "这个条目"} <XIcon className="size-3" />
            </Chip>
          )}
        </div>
      </div>

      {!detail ? (
        <TierSummary
          data={data}
          busy={busy}
          onOpen={(tier, reviewKind) => setFocus({ tier, reviewKind })}
          onClean={cleanGroup}
          onKeepAll={keepGroup}
        />
      ) : (
        <section className="mx-6 mt-5 max-md:mx-4" aria-label={focusGroup?.label ?? "重复文件"}>
          <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1.5 px-0.5">
            <div className="flex min-w-0 items-baseline gap-2.5">
              {focus.tier !== null && (
                <button
                  type="button"
                  onClick={() => setFocus(NO_FOCUS)}
                  className="shrink-0 rounded-full px-2 py-0.5 text-caption text-[var(--text-muted)] transition hover:bg-white/[0.06] hover:text-[var(--text)]"
                >
                  ‹ 返回摘要
                </button>
              )}
              <h2 className="truncate text-ui font-semibold text-[var(--text)]">
                {focusGroup?.label ?? (itemTitle ? `《${itemTitle}》的重复文件` : "重复文件")}
              </h2>
              {focusGroup && (
                <span className="text-caption font-normal text-[var(--text-faint)] tabular-nums">
                  {groupSummary(focusGroup)}
                </span>
              )}
            </div>
            {focus.tier !== null && focusGroup !== null && focusGroup.files > 0 && (
              <div className="flex items-center gap-1.5">
                {focus.tier === "review" && (
                  <ActionButton disabled={busy} onClick={() => keepGroup(focus.tier!, focus.reviewKind, focusGroup)}>
                    整组都留着
                  </ActionButton>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => cleanGroup(focus.tier!, focus.reviewKind, focusGroup)}
                  className={`flex h-8 items-center rounded-full border px-3 text-caption font-medium transition disabled:opacity-40 ${
                    focus.tier === "safe"
                      ? "border-[var(--accent)] bg-[var(--accent)] text-[#0a0b10] hover:opacity-90"
                      : "border-white/[0.15] text-[var(--text)] hover:bg-white/[0.08]"
                  }`}
                >
                  {TIER_ACTION_LABELS[focus.tier]} · {focusGroup.files}
                </button>
              </div>
            )}
            {focusGroup?.hint && <p className="basis-full text-caption text-[var(--text-faint)]">{focusGroup.hint}</p>}
          </div>

          {data.items.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-white/[0.08] px-4 py-7 text-center text-ui text-[var(--text-faint)]">
              这里已经没有待处理的重复文件
              {filterActive && (
                <button
                  type="button"
                  onClick={() => {
                    setQueryDraft("");
                    setFilter({ q: "", libraryId: null, itemId: null });
                  }}
                  className="ml-2 text-[var(--info)] hover:underline"
                >
                  清除筛选
                </button>
              )}
            </div>
          ) : (
            data.items.flatMap((item) =>
              item.seasons.map((season) => {
                const key = `${item.media_item.id}:${season.season_number}:${season.bucket}`;
                return (
                  <SeasonBlock
                    key={key}
                    item={item}
                    season={season}
                    expanded={expanded.has(key)}
                    busy={busy}
                    onToggleExpanded={() => toggleExpanded(key)}
                    onKeepFile={(unit, file) => keepFile(item, unit, file)}
                    onKeepVersion={(version) => keepVersion(item, season, version)}
                    onKeepAll={() => keepAll(item, season)}
                  />
                );
              }),
            )
          )}

          <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-caption text-[var(--text-faint)] tabular-nums">
            <span>
              {[hiddenNote(data.scan), "清理的文件进回收站，7 天内可恢复"].filter(Boolean).join(" · ")}
              {data.total_items > PAGE_SIZE && ` · 第 ${offset + 1}–${offset + data.items.length} 个条目，共 ${data.total_items} 个`}
            </span>
            {pageCount > 1 && <Pager page={pageIndex} count={pageCount} onChange={(p) => setOffset(p * PAGE_SIZE)} />}
          </div>
        </section>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// 头部：扫描状态与「开始扫描」
// ---------------------------------------------------------------------------

/**
 * 页面第一行永远回答同一个问题：这份结果是什么时候算的、要不要重算一次。
 *
 * 重复检测是"跑一会儿的事"，和扫描媒体库同一种性质——所以它有自己的按钮、
 * 自己的进度，而不是在你打开页面时偷偷算一遍让你等。
 */
function ScanBar({
  scan,
  busy,
  scanning,
  onScan,
}: {
  scan: DuplicateFilesData["scan"];
  busy: boolean;
  scanning: boolean;
  onScan: () => void;
}) {
  return (
    <div className="mt-5 flex flex-wrap items-center justify-between gap-x-3 gap-y-2 px-6 max-md:px-4">
      <div className="flex min-w-0 items-center gap-2 text-ui text-[var(--text-muted)]">
        {scanning && <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />}
        <span className="truncate">{scanNote(scan)}</span>
        {scanning && scan.percent !== null && (
          <span className="shrink-0 text-caption tabular-nums text-[var(--text-faint)]">{Math.round(scan.percent)}%</span>
        )}
      </div>
      <button
        type="button"
        disabled={busy || scanning}
        onClick={onScan}
        className="btn-glass h-8 shrink-0 px-3.5 text-caption font-medium text-[var(--text)] disabled:opacity-40"
      >
        {scanning ? "扫描中…" : scan.scanned_at ? "重新扫描" : "开始扫描"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 摘要：三张卡，外加「需要你决定」的取舍分组
// ---------------------------------------------------------------------------

/**
 * 落地页只回答一句话：**先做哪一档**。
 *
 * 三档从上到下就是建议的处理顺序——先把没风险的清掉（一个按钮），再看机器
 * 有把握的（清单可以逐条看），最后才是真正要你花心思的那些；最后一档按取舍
 * 类型分组，同一种取舍一次回答一批，而不是在几百个单元上重复同一个决定。
 */
function TierSummary({
  data,
  busy,
  onOpen,
  onClean,
  onKeepAll,
}: {
  data: DuplicateFilesData;
  busy: boolean;
  onOpen: (tier: DuplicateTier, reviewKind: DuplicateReviewKind | null) => void;
  onClean: (tier: DuplicateTier, reviewKind: DuplicateReviewKind | null, group: DuplicateGroup) => void;
  onKeepAll: (tier: DuplicateTier, reviewKind: DuplicateReviewKind | null, group: DuplicateGroup) => void;
}) {
  return (
    <div className="mx-6 mt-5 flex flex-col gap-2.5 max-md:mx-4">
      <p className="px-0.5 text-sub text-[var(--text-muted)]">
        一共 <b className="font-semibold text-[var(--text)] tabular-nums">{data.total_units}</b> 个单元有重复，
        按建议处理可清掉 <b className="font-semibold text-[var(--text)] tabular-nums">{data.total_files}</b> 个文件、
        腾出 <b className="font-semibold text-[var(--text)] tabular-nums">{formatBytes(data.total_bytes)}</b>。
        从上往下做：
      </p>

      {data.tiers.map((tier) => {
        const key = tier.key as DuplicateTier;
        const empty = tier.units === 0;
        return (
          <div key={tier.key} className={`rounded-2xl border px-4 py-3.5 ${empty ? "border-white/[0.06] bg-white/[0.01] opacity-60" : TIER_TONE[key]}`}>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1.5">
              <h3 className="flex items-baseline gap-2.5 text-ui font-semibold text-[var(--text)]">
                {tier.label}
                <span className="text-caption font-normal text-[var(--text-faint)] tabular-nums">
                  {empty ? "没有" : groupSummary(tier)}
                </span>
              </h3>
              {!empty && (
                <div className="flex items-center gap-1.5">
                  <ActionButton disabled={busy} onClick={() => onOpen(key, null)}>
                    逐个看
                  </ActionButton>
                  {key !== "review" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onClean(key, null, tier)}
                      className={`flex h-7 items-center rounded-full border px-3 text-caption font-medium transition disabled:opacity-40 ${
                        key === "safe"
                          ? "border-[var(--accent)] bg-[var(--accent)] text-[#0a0b10] hover:opacity-90"
                          : "border-white/[0.15] text-[var(--text)] hover:bg-white/[0.08]"
                      }`}
                    >
                      {TIER_ACTION_LABELS[key]} · {tier.files}
                    </button>
                  )}
                </div>
              )}
              <p className="basis-full text-caption text-[var(--text-faint)]">{tier.hint}</p>
            </div>

            {/* 「需要你决定」再按取舍类型分组：同一种取舍一次回答一批 */}
            {key === "review" && data.review_groups.length > 0 && (
              <div className="mt-2.5 flex flex-col gap-1.5 border-t border-white/[0.06] pt-2.5">
                {data.review_groups.map((group) => (
                  <div key={group.key} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                    <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2">
                      <span className="text-sub text-[var(--text)]">{group.label}</span>
                      <span className="text-caption text-[var(--text-faint)] tabular-nums">{groupSummary(group)}</span>
                      <span className="basis-full text-caption text-[var(--text-faint)]">{group.hint}</span>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      <ActionButton disabled={busy} onClick={() => onOpen("review", group.key as DuplicateReviewKind)}>
                        逐个看
                      </ActionButton>
                      <ActionButton disabled={busy} onClick={() => onKeepAll("review", group.key as DuplicateReviewKind, group)}>
                        都留着
                      </ActionButton>
                      <ActionButton disabled={busy} onClick={() => onClean("review", group.key as DuplicateReviewKind, group)}>
                        按建议清 · {group.files}
                      </ActionButton>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {hiddenNote(data.scan) && (
        <p className="px-0.5 text-caption text-[var(--text-faint)]">{hiddenNote(data.scan)}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 块：一个条目的一季（电影就是条目本身）
// ---------------------------------------------------------------------------

function SeasonBlock({
  item,
  season,
  expanded,
  busy,
  onToggleExpanded,
  onKeepFile,
  onKeepVersion,
  onKeepAll,
}: {
  item: DuplicateItem;
  season: DuplicateSeason;
  expanded: boolean;
  busy: boolean;
  onToggleExpanded: () => void;
  onKeepFile: (unit: DuplicateUnit, file: DuplicateFile) => void;
  onKeepVersion: (version: DuplicateVersion) => void;
  onKeepAll: () => void;
}) {
  const media = item.media_item;
  const isTv = media.kind === "tv";
  const headline = seasonHeadline(season, media.kind);
  const showVersions = season.uniform && !expanded;
  const suggestedVersion = season.versions.find((v) => v.suggested) ?? null;

  return (
    <div className="mb-2.5 overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.02]">
      <div className="flex items-center gap-2.5 border-b border-white/[0.07] bg-white/[0.02] px-4 py-2.5">
        <PosterImage
          src={imageUrl(media.poster_url)}
          alt=""
          className="h-[38px] w-[26px] shrink-0 rounded-[4px] border border-white/[0.08] object-cover"
        />
        <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <Link href={`/library/${item.library.id}/item/${media.id}` as Route} className="truncate text-ui font-semibold text-[var(--text)] hover:underline">
            {media.title}
          </Link>
          {media.year && <span className="text-caption text-[var(--text-faint)]">{media.year}</span>}
          <span className="text-caption text-[var(--text-faint)]">
            {item.library.name}
            {headline && (
              <>
                {" · "}
                <span className="text-[var(--text)]">{headline}</span>
              </>
            )}
          </span>
        </div>
        {season.uniform && (
          <button type="button" onClick={onToggleExpanded} className="shrink-0 rounded-full px-2.5 py-1 text-caption text-[var(--text-muted)] hover:bg-white/[0.06] hover:text-[var(--text)]">
            {expanded ? "收起各集" : "展开各集"}
          </button>
        )}
      </div>

      {showVersions ? (
        <div className="divide-y divide-white/[0.06]">
          {season.versions.map((version) => (
            <div key={version.key} className={`px-4 py-2.5 text-sub ${ROW_GRID}`}>
              <QualityLine
                segments={version.quality_label.split(" ").map((text, i) => ({
                  text,
                  diff: suggestedVersion !== null && !version.suggested && suggestedVersion.quality_label.split(" ")[i] !== text,
                }))}
              />
              <span className="text-[var(--text-muted)] tabular-nums">
                <b className="font-semibold text-[var(--text)]">{versionCoverage(version).split(" · ")[0]}</b>
                {" · "}
                {versionCoverage(version).split(" · ")[1]}
              </span>
              <span className="truncate text-[var(--text-muted)]">{version.origin_label}</span>
              <div className="flex items-center justify-end gap-1.5 max-md:justify-start">
                {version.suggested && <Tag tone="keep">建议保留</Tag>}
                <ActionButton disabled={busy} onClick={() => onKeepVersion(version)}>
                  整季留这个
                </ActionButton>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="max-h-[520px] overflow-y-auto">
          {season.units.map((unit) => (
            <div key={`${unit.season_number}-${unit.episode_number}`}>
              {isTv && (
                <div className="border-b border-white/[0.06] bg-white/[0.012] px-4 py-1 text-micro tracking-wide text-[var(--text-faint)]">
                  {episodeLabel(unit.episode_number)}
                </div>
              )}
              <div className="divide-y divide-white/[0.06]">
                {unit.files.map((file) => (
                  <FileRow key={file.id} unit={unit} file={file} busy={busy} onKeep={() => onKeepFile(unit, file)} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="flex justify-end border-t border-white/[0.06] bg-black/[0.15] px-4 py-1.5">
        <button
          type="button"
          disabled={busy}
          onClick={onKeepAll}
          className="rounded-full px-2.5 py-1 text-caption text-[var(--text-muted)] transition hover:bg-white/[0.06] hover:text-[var(--text)] disabled:opacity-40"
        >
          {isTv ? "整季都留着" : "都留着"}
        </button>
      </div>
    </div>
  );
}

/** 文件行：文件名 / 规格（与建议保留者不同的维度加亮）/ 来源 / 建议保留标签 + 留这个。 */
function FileRow({ unit, file, busy, onKeep }: { unit: DuplicateUnit; file: DuplicateFile; busy: boolean; onKeep: () => void }) {
  const reference = suggestedOf(unit);
  const note = fileNote(file);
  const live = unit.files.filter((f) => !f.kept_at).length;
  return (
    <div className={`px-4 py-2 text-sub ${ROW_GRID} ${file.kept_at ? "opacity-55" : ""}`}>
      <Tooltip content={<span className="tnum break-all font-mono text-caption leading-5">{file.file_path}</span>} maxWidth={520}>
        <span className="block truncate font-mono text-caption text-[var(--text)]">{file.file_name}</span>
      </Tooltip>
      <QualityLine segments={qualitySegments(file, reference)} />
      <span className="min-w-0 truncate text-[var(--text-muted)]">
        {file.origin.label}
        {note && <span className="block truncate text-caption text-[var(--text-faint)]">{note}</span>}
      </span>
      <div className="flex items-center justify-end gap-1.5 max-md:justify-start">
        {file.suggested && <Tag tone="keep">建议保留</Tag>}
        {file.kept_at && <Tag tone="kept">你留下的</Tag>}
        {(live > 1 || unit.files.length > 1) && (
          <ActionButton disabled={busy} onClick={onKeep}>
            留这个
          </ActionButton>
        )}
      </div>
    </div>
  );
}

function QualityLine({ segments }: { segments: { text: string; diff: boolean }[] }) {
  return (
    <span className="truncate text-[var(--text-muted)] tabular-nums">
      {segments.map((seg, i) => (
        <span key={`${seg.text}-${i}`}>
          {i > 0 && <span aria-hidden> · </span>}
          <span className={seg.diff ? "font-semibold text-[var(--warn)]" : i === 0 ? "font-semibold text-[var(--text)]" : undefined}>
            {seg.text}
          </span>
        </span>
      ))}
    </span>
  );
}

function Tag({ tone, children }: { tone: "keep" | "kept"; children: React.ReactNode }) {
  return (
    <span
      className={`inline-block whitespace-nowrap rounded-full border px-1.5 text-micro leading-[18px] ${
        tone === "keep" ? "border-[rgba(74,222,128,0.4)] text-[var(--ok)]" : "border-[rgba(232,201,138,0.45)] text-[#e8c98a]"
      }`}
    >
      {children}
    </span>
  );
}

function ActionButton({ disabled, onClick, children }: { disabled?: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="whitespace-nowrap rounded-full border border-white/[0.15] px-2.5 py-0.5 text-caption font-medium text-[var(--text)] transition hover:bg-white/[0.08] disabled:opacity-40"
    >
      {children}
    </button>
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

function Pager({ page, count, onChange }: { page: number; count: number; onChange: (page: number) => void }) {
  const btn = "grid h-7 min-w-7 place-items-center rounded-lg border text-caption tabular-nums transition";
  return (
    <nav aria-label="分页" className="flex items-center gap-1">
      <button type="button" disabled={page === 0} onClick={() => onChange(page - 1)} className={`${btn} border-transparent text-[var(--text-muted)] hover:bg-white/[0.06] disabled:opacity-40`} aria-label="上一页">
        ‹
      </button>
      <span className="px-1.5 text-[var(--text-muted)]">
        {page + 1} / {count}
      </span>
      <button type="button" disabled={page >= count - 1} onClick={() => onChange(page + 1)} className={`${btn} border-transparent text-[var(--text-muted)] hover:bg-white/[0.06] disabled:opacity-40`} aria-label="下一页">
        ›
      </button>
    </nav>
  );
}
