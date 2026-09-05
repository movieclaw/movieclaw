"use client";

import { useEffect, useState } from "react";
import type { Route } from "next";

import { PosterCardVisual, type PosterVisualItem } from "@/components/poster-card";
import { libraryCardAction } from "@/components/library-view";
import type { LibraryItem, LibrarySearchGroup } from "@/lib/api/libraries";
import { searchLibraryItems } from "@/lib/api/search";
import { imageUrl } from "@/lib/image-proxy";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/**
 * 搜索结果页「媒体库」垂直：跨全部媒体库搜索已入库条目，按库分区展示。
 *
 * 与影视（MediaSearchResults）、站点资源（SearchResults）并列挂在 /search
 * 页的选项卡下，回答的问题是「这部片我有没有」。数据全在本地（标题/原名
 * 子串匹配），毫秒级返回，没有快照与历史——搜自己的库是翻家底，不值得回放。
 *
 * 空态的出口指向「影视」垂直：库里没有 ≈ 想要但还没入手，下一步自然是
 * 去影视条目搜索并订阅/下载。
 */
export function LibrarySearchResults({
  keyword,
  onSwitchToMedia,
}: {
  keyword: string;
  /** 切到「影视」垂直（空态时的出口：库里没有 → 去找来） */
  onSwitchToMedia?: () => void;
}) {
  const scrollRef = useScrollRestoration(`search:library:${keyword}`);
  // null = 加载中；[] = 无结果；error 非空 = 请求失败
  const [groups, setGroups] = useState<LibrarySearchGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setGroups(null);
    setError(null);
    searchLibraryItems(keyword)
      .then((list) => {
        if (!cancelled) setGroups(list);
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message || "媒体库搜索失败，请稍后重试");
      });
    return () => {
      cancelled = true;
    };
  }, [keyword]);

  const empty = groups !== null && groups.length === 0;

  return (
    <div className="relative flex h-full flex-col">
      {/* 状态行：与另外两个垂直的头部同构（关键词） */}
      <header className="shrink-0 px-6 pb-3 pt-4 max-md:px-4 max-md:pt-3">
        <h1 className="text-on-image text-title-lg font-semibold tracking-[-0.01em] text-white">
          “{keyword}”
        </h1>
      </header>

      <div
        ref={scrollRef}
        className="scroll-thin scroll-safe relative min-h-0 flex-1 overflow-y-auto px-6 pb-6 max-md:px-4"
      >
        {groups === null && !error && <LibrarySearchSkeleton />}
        {(error || empty) && (
          <div className="flex flex-col items-center pt-24 text-center">
            <p className="text-on-image text-body-lg font-semibold text-white">
              {error ? "媒体库搜索出错" : "媒体库中没有找到相关影片"}
            </p>
            <p className="text-on-image mt-1.5 text-sub text-[rgba(243,245,249,0.7)]">
              {error ?? "已入库条目按标题和原名匹配；库里还没有的片子，去影视条目里找。"}
            </p>
            {onSwitchToMedia && (
              <button
                type="button"
                onClick={onSwitchToMedia}
                className="btn-accent mt-5 rounded-full px-4 py-1.5 text-sub font-semibold"
              >
                搜索影视条目
              </button>
            )}
          </div>
        )}
        {groups !== null && groups.length > 0 && (
          <div className="flex flex-col gap-7">
            {groups.map((group) => (
              <section key={group.library_id}>
                <div className="mb-3 flex items-center gap-2.5">
                  <span className="rounded-full bg-black/30 px-2.5 py-0.5 text-caption text-[var(--accent)] backdrop-blur-sm">
                    {group.library_name}
                  </span>
                  <span className="text-on-image text-sub text-[rgba(243,245,249,0.75)]">
                    共 {group.items.length} 条结果
                  </span>
                </div>
                <div className="grid gap-x-4 gap-y-7 pt-1 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))]">
                  {group.items.map((item) => (
                    <LibraryResultCell
                      key={item.media_item_id}
                      item={item}
                      libraryId={group.library_id}
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** 一格结果：海报卡链到库内条目详情，格下标注库存概况（与单库海报墙同口径）。 */
function LibraryResultCell({ item, libraryId }: { item: LibraryItem; libraryId: number }) {
  const visual: PosterVisualItem = {
    id: item.tmdb_id != null ? String(item.tmdb_id) : `local:${item.media_item_id}`,
    source: "tmdb",
    type: item.kind === "video" ? undefined : item.kind,
    title: item.title,
    year: item.year ?? undefined,
    rating: 0,
    aspect: item.primary_aspect,
    posterUrl: imageUrl(item.poster_url),
  };
  const parts: string[] = [];
  if (item.kind === "tv" && item.seasons.length > 0) {
    parts.push(
      item.seasons.length === 1
        ? `第 ${item.seasons[0]} 季 · ${item.episode_count} 集`
        : `${item.seasons.length} 季 · ${item.episode_count} 集`,
    );
  }
  if (item.resolutions.length > 0) parts.push(item.resolutions.join("/"));
  return (
    <div className="min-w-0">
      <PosterCardVisual
        item={visual}
        href={`/library/${libraryId}/item/${item.media_item_id}` as Route}
        action={libraryCardAction(item)}
      />
      {parts.length > 0 && (
        <p className="text-on-image mt-1.5 truncate text-caption text-[var(--text-muted)]">
          {parts.join(" · ")}
        </p>
      )}
    </div>
  );
}

function LibrarySearchSkeleton() {
  return (
    <div className="grid gap-x-4 gap-y-7 pt-1 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))]">
      {Array.from({ length: 7 }, (_, index) => (
        <div key={index} className="aspect-[2/3] animate-pulse rounded-2xl bg-white/[0.05]" />
      ))}
    </div>
  );
}
