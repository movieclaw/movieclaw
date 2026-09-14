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
  type DuplicateBucket,
  type DuplicateFile,
  type DuplicateFilesData,
  type DuplicateItem,
  type DuplicateSeason,
  type DuplicateUnit,
  type DuplicateVersion,
  type MediaLibrary,
  type TrashedBatchResult,
  listDuplicateFiles,
  resolveAllDuplicates,
  resolveDuplicates,
} from "@/lib/api/libraries";
import { formatBytes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import {
  BUCKET_HINTS,
  BUCKET_LABELS,
  bucketFacts,
  bucketSummary,
  episodeLabel,
  fileNote,
  hiddenNote,
  keepFileFacts,
  keepVersionFacts,
  qualitySegments,
  resolveResultText,
  seasonHeadline,
  seasonsIn,
  suggestedOf,
  versionCoverage,
} from "@/lib/library-duplicates";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/** 分页单位是条目（一部剧一块），不是文件 */
const PAGE_SIZE = 20;

/** 文件行 / 版本行的四列：名字或规格 / 规格或覆盖 / 来源 / 动作。手机上叠成一列。 */
const ROW_GRID =
  "grid items-center gap-x-4 gap-y-1 max-md:grid-cols-1 md:grid-cols-[minmax(0,1.5fr)_minmax(0,1.1fr)_minmax(0,1fr)_auto]";

interface Filter {
  q: string;
  libraryId: number | null;
  itemId: number | null;
}

/**
 * 媒体库管理页的「重复文件」标签（docs/design/library-duplicate-files.md §5）。
 *
 * 页面只有两段：「一模一样」（机器确定没区别，一键清）与「不同版本」（有区别，
 * 每个单元回答一次：留哪个 / 都留着）。堆内一个条目一块，电影块列文件行，剧集块
 * 按季列版本行（同构季）或各集。没有策略、没有类别筛选、没有逐文件勾选。
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

  const reloadSeq = useRef(0);
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    listDuplicateFiles(
      { q: filter.q.trim() || undefined, library_id: filter.libraryId, media_item_id: filter.itemId },
      { limit: PAGE_SIZE, offset },
    )
      .then((next) => {
        if (seq !== reloadSeq.current) return;
        setFailed(false);
        setData((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
        if (filter.itemId === null && filter.libraryId === null && !filter.q.trim()) {
          onCountChange?.(next.identical.files + next.versions.files);
        }
      })
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, [filter, offset, onCountChange]);

  useEffect(() => {
    reload();
  }, [reload]);
  useVisiblePolling(reload, 30_000);

  const filterKey = `${filter.q}|${filter.libraryId}|${filter.itemId}`;
  useEffect(() => {
    setOffset(0);
  }, [filterKey]);
  useEffect(() => {
    setExpanded(new Set());
  }, [filterKey, offset]);
  useEffect(() => {
    if (data && data.items.length === 0 && offset > 0 && data.total_items > 0) {
      setOffset(Math.max(0, Math.floor((data.total_items - 1) / PAGE_SIZE) * PAGE_SIZE));
    }
  }, [data, offset]);

  const filterActive = Boolean(filter.q.trim()) || filter.libraryId !== null || filter.itemId !== null;
  const itemTitle = useMemo(
    () => data?.items.find((it) => it.media_item.id === filter.itemId)?.media_item.title ?? null,
    [data, filter.itemId],
  );

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

  /** 「都留着」：这些版本都是我要的，单元不再列出（直到有新文件进来）。可撤销，不弹确认。 */
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

  /** 整堆按「建议保留」清理。不同版本堆逐条列出会清掉的东西——这是唯一要认真看的确认。 */
  const resolveBucket = async (bucket: DuplicateBucket) => {
    if (!data) return;
    const facts = bucketFacts(data, bucket);
    const libraryName = filter.libraryId !== null ? libraries?.find((l) => l.id === filter.libraryId)?.name : null;
    const ok = await confirm({
      title:
        bucket === "identical"
          ? `清理${libraryName ? `「${libraryName}」库` : "全部"}一模一样的文件？`
          : `按建议清理${libraryName ? `「${libraryName}」库` : "全部"}不同版本？`,
      description:
        `${facts.files} 个文件 · ${formatBytes(facts.bytes)} · 每个单元留下「建议保留」的那个 · 7 天内可在回收站恢复。` +
        (bucket === "versions"
          ? " 这些文件与保留者有区别；想留的请先取消，回去点那个单元的「都留着」。"
          : "") +
        (facts.files > 500 ? " 一次最多处理 500 个，剩下的再点一次。" : ""),
      bullets: bucket === "versions" ? facts.lines : undefined,
      confirmLabel: `移入回收站 · ${Math.min(facts.files, 500)}`,
      cancelLabel: "先不",
      tone: bucket === "versions" ? "danger" : "default",
    });
    if (!ok) return;
    await run(() => resolveAllDuplicates({ bucket, library_id: filter.libraryId }));
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
        正在检查重复文件…
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
  if (data.total_items === 0 && !filterActive) {
    return (
      <ContentEmptyState
        variant="library"
        title="没有重复文件"
        description={
          "每部电影、每一集都只有一个在位文件。" + (hiddenNote(data) ? ` ${hiddenNote(data)}。` : "")
        }
      />
    );
  }

  const pageCount = Math.max(1, Math.ceil(data.total_items / PAGE_SIZE));
  const pageIndex = Math.floor(offset / PAGE_SIZE);
  const libraryChips = (libraries ?? []).filter((l) => data.items.some((it) => it.library.id === l.id) || l.id === filter.libraryId);

  return (
    <>
      {failed && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {/* 筛选：搜索 / 库胶囊 / 只看某条目（详情页带来的） */}
      <div className="mt-5 flex flex-wrap items-center gap-2.5 px-6 max-md:px-4">
        <label className="flex h-9 min-w-[220px] flex-1 items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.04] px-3 text-ui text-[var(--text-muted)] focus-within:border-[var(--accent)]/60 max-md:min-w-0 max-md:basis-full sm:max-w-[300px]">
          <SearchIcon className="size-4 shrink-0" />
          <input
            type="search"
            value={queryDraft}
            onChange={(e) => setQueryDraft(e.target.value)}
            placeholder="按片名、剧名或文件名搜索"
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

      {(["identical", "versions"] as const).map((bucket) => {
        const stats = data[bucket];
        const items = data.items.filter((it) => seasonsIn(it, bucket).length > 0);
        return (
          <section key={bucket} className="mx-6 mt-6 max-md:mx-4" aria-label={BUCKET_LABELS[bucket]}>
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-0.5">
              <h2 className="flex items-baseline gap-2.5 text-ui font-semibold text-[var(--text)]">
                {BUCKET_LABELS[bucket]}
                <span className="text-caption font-normal text-[var(--text-faint)] tabular-nums">
                  {bucketSummary(data, bucket)}
                </span>
              </h2>
              <button
                type="button"
                disabled={busy || stats.files === 0}
                onClick={() => resolveBucket(bucket)}
                className={`flex h-8 items-center rounded-full border px-3 text-caption font-medium transition disabled:opacity-40 ${
                  bucket === "identical"
                    ? "border-[var(--accent)] bg-[var(--accent)] text-[#0a0b10] hover:opacity-90"
                    : "border-white/[0.15] text-[var(--text)] hover:bg-white/[0.08]"
                }`}
              >
                {bucket === "identical" ? "全部清理" : "全部按建议清理"} · {stats.files}
              </button>
              <p className="basis-full text-caption text-[var(--text-faint)]">{BUCKET_HINTS[bucket]}</p>
            </div>
            {items.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-white/[0.08] px-4 py-7 text-center text-ui text-[var(--text-faint)]">
                {bucket === "identical" ? "没有一模一样的文件" : "没有待决定的单元"}
                {filterActive && stats.units === 0 && data.total_items === 0 && (
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
              items.flatMap((item) =>
                seasonsIn(item, bucket).map((season) => {
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
          </section>
        );
      })}

      <div className="mx-6 mt-4 flex flex-wrap items-center justify-between gap-2 text-caption text-[var(--text-faint)] tabular-nums max-md:mx-4">
        <span>
          {[hiddenNote(data), "清理的文件进回收站，7 天内可恢复"].filter(Boolean).join(" · ")}
          {data.total_items > PAGE_SIZE && ` · 第 ${offset + 1}–${offset + data.items.length} 个条目，共 ${data.total_items} 个`}
        </span>
        {pageCount > 1 && <Pager page={pageIndex} count={pageCount} onChange={(p) => setOffset(p * PAGE_SIZE)} />}
      </div>
    </>
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
