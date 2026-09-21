"use client";

import { useEffect, useState } from "react";

import { FilterIcon, XIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { PAGE_NAV_BUTTON_CLASS } from "@/components/page-nav";
import { fetchDiscoveryGenres, type DiscoveryGenre } from "@/lib/api/discover";
import {
  DISCOVERY_COUNTRIES,
  discoveryFilterCount,
  EMPTY_DISCOVERY_FILTERS,
  type DiscoveryFilters,
} from "@/lib/discovery-filters";
import type { MediaType } from "@/lib/media-types";

const genreCache = new Map<MediaType, DiscoveryGenre[]>();
const selectClass =
  "h-10 w-full rounded-xl border border-white/10 bg-black/30 px-3 text-body text-[var(--text)] outline-none focus:border-white/25";

export function DiscoveryFilterControl({
  mediaType,
  filters,
  currentYear,
  onApply,
  compact = false,
  disabled = false,
}: {
  mediaType: MediaType;
  filters: DiscoveryFilters;
  currentYear: number;
  onApply: (filters: DiscoveryFilters) => void;
  /** 移动端顶栏的图标形态（40px 圆钮 + 角标），与文字形态同开一个弹窗 */
  compact?: boolean;
  /** 豆瓣源不支持筛选：禁用置灰原地保留（不整体隐藏，避免顶栏跳动重排） */
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(filters);
  const [genres, setGenres] = useState<DiscoveryGenre[]>(() => genreCache.get(mediaType) ?? []);
  const [genreError, setGenreError] = useState(false);
  const activeCount = discoveryFilterCount(filters);

  useEffect(() => {
    if (!open) return;
    setDraft(filters);
    const cached = genreCache.get(mediaType);
    if (cached) {
      setGenres(cached);
      return;
    }
    const controller = new AbortController();
    setGenreError(false);
    fetchDiscoveryGenres(mediaType, { signal: controller.signal })
      .then((items) => {
        genreCache.set(mediaType, items);
        if (!controller.signal.aborted) setGenres(items);
      })
      .catch(() => {
        if (!controller.signal.aborted) setGenreError(true);
      });
    return () => controller.abort();
  }, [filters, mediaType, open]);

  const toggleGenre = (id: number) => {
    setDraft((current) => ({
      ...current,
      genreIds: current.genreIds.includes(id)
        ? current.genreIds.filter((value) => value !== id)
        : [...current.genreIds, id],
    }));
  };

  const apply = () => {
    onApply(draft);
    setOpen(false);
  };

  return (
    <>
      {compact ? (
        /* 移动端顶栏的紧凑档：去掉「筛选」文字换成图标 + 角标计数——顶栏要
           同时装下电影/剧集切换、筛选、数据源切换与搜索键，文字按钮放不下。
           圆钮规格直接复用全站 PAGE_NAV_BUTTON_CLASS，与顶栏搜索键完全同款
           （鼠标 36px / 触屏 44px），保证同一行圆键永远一样大、一套玻璃配方。
           豆瓣源下禁用置灰。 */
        <button
          type="button"
          onClick={() => setOpen(true)}
          disabled={disabled}
          title={disabled ? "筛选仅 TMDB 源支持" : undefined}
          className={`relative shrink-0 ${PAGE_NAV_BUTTON_CLASS} ${
            disabled ? "pointer-events-none opacity-40" : ""
          }`}
          aria-label={activeCount > 0 ? `筛选，已启用 ${activeCount} 项` : "筛选影片"}
        >
          <FilterIcon className="size-[18px]" />
          {activeCount > 0 && (
            <span className="tnum absolute -right-1 -top-1 flex size-[18px] items-center justify-center rounded-full bg-[var(--accent)] text-micro font-bold text-black">
              {activeCount}
            </span>
          )}
        </button>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          disabled={disabled}
          title={disabled ? "筛选仅 TMDB 源支持" : undefined}
          className={`relative flex h-10 shrink-0 items-center rounded-full border border-white/10 bg-black/35 px-4 text-sub font-semibold text-[var(--text-muted)] backdrop-blur-xl transition ${
            disabled ? "cursor-not-allowed opacity-40" : "hover:border-white/20 hover:text-white"
          }`}
          aria-label={activeCount > 0 ? `筛选，已启用 ${activeCount} 项` : "筛选影片"}
        >
          筛选
          {activeCount > 0 && (
            <span className="tnum ml-2 flex size-5 items-center justify-center rounded-full bg-[var(--accent)] text-micro font-bold text-black">
              {activeCount}
            </span>
          )}
        </button>
      )}
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        label="组合筛选影片"
        width="2xl"
        panelClassName="flex max-h-[82vh] flex-col"
      >
        <div className="flex items-center justify-between border-b border-white/[0.07] px-6 py-5 max-md:px-4">
          <div>
            <h2 className="text-body-lg font-semibold text-[var(--text)]">组合发现</h2>
            <p className="mt-1 text-sub text-[var(--text-muted)]">筛选会写入网址，可直接分享或收藏</p>
          </div>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="icon-chip flex size-9"
            aria-label="关闭筛选"
          >
            <XIcon className="size-4" />
          </button>
        </div>

        <div className="scroll-thin flex-1 space-y-6 overflow-y-auto px-6 py-5 max-md:px-4">
          <fieldset>
            <legend className="mb-3 text-sub font-semibold text-[var(--text-muted)]">类型（可多选）</legend>
            {genreError ? (
              <p className="text-sub text-red-300">类型加载失败，其他筛选仍可使用</p>
            ) : genres.length === 0 ? (
              <div className="h-16 animate-pulse rounded-xl bg-white/[0.05]" />
            ) : (
              <div className="flex flex-wrap gap-2">
                {genres.map((genre) => {
                  const selected = draft.genreIds.includes(genre.id);
                  return (
                    <button
                      key={genre.id}
                      type="button"
                      aria-pressed={selected}
                      onClick={() => toggleGenre(genre.id)}
                      className={`rounded-full border px-3 py-1.5 text-sub font-semibold transition ${
                        selected
                          ? "border-white/25 bg-white/15 text-white"
                          : "border-white/[0.07] bg-black/20 text-[var(--text-muted)] hover:text-white"
                      }`}
                    >
                      {genre.name}
                    </button>
                  );
                })}
              </div>
            )}
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <FilterSelect
              label="国家 / 地区"
              value={draft.originCountry ?? ""}
              onChange={(value) => setDraft((current) => ({ ...current, originCountry: value || undefined }))}
              options={DISCOVERY_COUNTRIES.map(([value, label]) => ({ value, label }))}
            />
            <FilterSelect
              label={mediaType === "movie" ? "上映年份" : "首播年份"}
              value={draft.year ? String(draft.year) : ""}
              onChange={(value) => setDraft((current) => ({ ...current, year: value ? Number(value) : undefined }))}
              options={Array.from({ length: Math.max(1, currentYear - 1873) }, (_, index) => {
                const year = currentYear - index;
                return { value: String(year), label: `${year} 年` };
              })}
            />
            <FilterSelect
              label="最低评分"
              value={draft.ratingGte === undefined ? "" : String(draft.ratingGte)}
              onChange={(value) => setDraft((current) => ({ ...current, ratingGte: value ? Number(value) : undefined }))}
              options={[6, 7, 8, 9].map((value) => ({ value: String(value), label: `${value} 分以上` }))}
            />
            <FilterSelect
              label={mediaType === "movie" ? "最长片长" : "最长单集时长"}
              value={draft.runtimeLte ? String(draft.runtimeLte) : ""}
              onChange={(value) => setDraft((current) => ({ ...current, runtimeLte: value ? Number(value) : undefined }))}
              options={[60, 90, 120, 150].map((value) => ({ value: String(value), label: `${value} 分钟以内` }))}
            />
            <FilterSelect
              label="排序"
              value={draft.sort}
              onChange={(value) => setDraft((current) => ({ ...current, sort: value as DiscoveryFilters["sort"] }))}
              options={[
                { value: "popular", label: "热门优先" },
                { value: "rating", label: "评分优先" },
                { value: "newest", label: "最新优先" },
                { value: "most-rated", label: "最多评分" },
              ]}
              allowEmpty={false}
            />
          </div>
        </div>

        <div className="flex items-center justify-between border-t border-white/[0.07] px-6 py-4 max-md:px-4">
          <button
            type="button"
            onClick={() => setDraft(EMPTY_DISCOVERY_FILTERS)}
            className="text-ui font-semibold text-[var(--text-muted)] transition hover:text-white"
          >
            清空
          </button>
          <button type="button" onClick={apply} className="btn-accent h-10 rounded-full px-6 text-ui font-semibold">
            查看结果
          </button>
        </div>
      </Modal>
    </>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
  allowEmpty = true,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  allowEmpty?: boolean;
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-sub font-semibold text-[var(--text-muted)]">{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)} className={selectClass}>
        {allowEmpty && <option value="">不限</option>}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
