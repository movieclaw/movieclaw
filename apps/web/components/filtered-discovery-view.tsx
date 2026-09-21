"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PosterCard } from "@/components/poster-card";
import { fetchDiscoveryGenres, fetchFilteredDiscovery } from "@/lib/api/discover";
import { useTheme } from "@/lib/ui-prefs";
import {
  discoveryFilterCount,
  discoveryFilterLabels,
  type DiscoveryFilters,
} from "@/lib/discovery-filters";
import type { MediaItem, MediaType } from "@/lib/media-types";

export function FilteredDiscoveryView({
  mediaType,
  filters,
  onClear,
}: {
  mediaType: MediaType;
  filters: DiscoveryFilters;
  onClear: () => void;
}) {
  const [items, setItems] = useState<MediaItem[]>([]);
  const [nextPage, setNextPage] = useState(1);
  const [totalResults, setTotalResults] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadingRef = useRef(false);
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const [genreNames, setGenreNames] = useState<ReadonlyMap<number, string>>(new Map());
  // 页面左右留白随主题走栅格：Netflix 主题放弃居中栏、与发现页内容行同走全幅
  // 4vw 左基线；银玻璃维持居中 1500px 栏 + px-6
  const isNf = useTheme().id === "netflix";

  const loadPage = useCallback(async (page: number) => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const result = await fetchFilteredDiscovery(mediaType, filters, page, {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setItems((current) => {
        const known = new Set(current.map((item) => item.titleRef ?? `${item.source}:${item.id}`));
        return [
          ...current,
          ...result.items.filter((item) => !known.has(item.titleRef ?? `${item.source}:${item.id}`)),
        ];
      });
      setTotalResults(result.totalResults);
      setHasMore(result.hasMore);
      setNextPage(result.page + 1);
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError((reason as Error).message || "筛选结果加载失败，请稍后重试");
      }
    } finally {
      if (!controller.signal.aborted) setLoading(false);
      loadingRef.current = false;
    }
  }, [filters, mediaType]);

  useEffect(() => {
    void loadPage(1);
    return () => controllerRef.current?.abort();
  }, [loadPage]);

  useEffect(() => {
    if (filters.genreIds.length === 0) return;
    const controller = new AbortController();
    fetchDiscoveryGenres(mediaType, { signal: controller.signal })
      .then((genres) => {
        if (!controller.signal.aborted) {
          setGenreNames(new Map(genres.map((genre) => [genre.id, genre.name])));
        }
      })
      .catch(() => {
        // 类型名称只是结果页的辅助文案，失败时保留“几个类型”的可用回退。
      });
    return () => controller.abort();
  }, [filters.genreIds, mediaType]);

  useEffect(() => {
    const target = loadMoreRef.current;
    if (!target || !hasMore || loading || error) return;
    const scrollRoot = target.closest<HTMLElement>("[data-scroll-root]");
    if (!scrollRoot) return;
    const preloadDistance = Math.max(800, Math.round(scrollRoot.clientHeight * 1.25));
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) void loadPage(nextPage);
      },
      {
        root: scrollRoot,
        rootMargin: `${preloadDistance}px 0px`,
      },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [error, hasMore, loadPage, loading, nextPage]);

  const activeCount = discoveryFilterCount(filters);
  const filterLabels = useMemo(
    () => discoveryFilterLabels(filters, genreNames),
    [filters, genreNames],
  );
  return (
    <main
      className={
        isNf ? "w-full page-inset pb-12" : "mx-auto w-full max-w-[1500px] page-inset pb-12"
      }
    >
      <header className="mb-7 flex items-end justify-between gap-4 max-md:mb-5">
        <div>
          <p className="text-sub font-semibold tracking-[0.16em] text-[var(--accent-2)]">
            TMDB DISCOVER
          </p>
          <h1 className="mt-1 text-3xl font-bold tracking-[-0.03em] text-[var(--text)] max-md:text-2xl">
            筛选结果
          </h1>
          <p className="mt-2 text-body text-[var(--text-muted)]">
            {totalResults > 0
              ? `找到 ${totalResults.toLocaleString("zh-CN")} 部，已加载 ${items.length} 部`
              : `已启用 ${activeCount} 项筛选`}
          </p>
          <div className="mt-3 flex flex-wrap gap-2" aria-label="当前筛选条件">
            {filterLabels.map((label) => (
              <span
                key={label}
                className="rounded-full border border-white/[0.08] bg-white/[0.05] px-2.5 py-1 text-sub font-semibold text-[var(--text-muted)]"
              >
                {label}
              </span>
            ))}
          </div>
        </div>
        <button
          type="button"
          onClick={onClear}
          className="shrink-0 text-ui font-semibold text-[var(--accent-2)] transition hover:text-white"
        >
          清除全部
        </button>
      </header>

      {items.length > 0 && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-7 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8">
          {items.map((item) => (
            <PosterCard key={item.titleRef ?? `${item.source}:${item.id}`} item={item} />
          ))}
        </div>
      )}

      {loading && items.length === 0 && <FilteredSkeleton />}
      {!loading && !error && items.length === 0 && (
        <div className="py-24 text-center text-body text-[var(--text-muted)]">没有符合条件的影片</div>
      )}
      {error && (
        <div className="mt-12 rounded-2xl border border-white/10 bg-black/25 p-8 text-center">
          <p className="text-body text-[var(--text-muted)]">{error}</p>
          <button type="button" onClick={() => void loadPage(nextPage)} className="btn-accent mt-4 h-9 rounded-full px-5 text-ui font-semibold">
            重试
          </button>
        </div>
      )}
      <div ref={loadMoreRef} className="h-px" aria-hidden="true" />
      <p className="sr-only" role="status">
        {loading && items.length > 0
          ? "正在加载更多影片"
          : !hasMore && items.length > 0
            ? `已加载全部 ${items.length} 部影片`
            : ""}
      </p>
    </main>
  );
}

function FilteredSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-7 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8" aria-busy="true">
      {Array.from({ length: 16 }, (_, index) => (
        <div key={index} className="aspect-[2/3] animate-pulse rounded-2xl bg-white/[0.05] ring-1 ring-white/10" />
      ))}
    </div>
  );
}
