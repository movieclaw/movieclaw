"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { Route } from "next";

import { InventoryCell, WALL_GRID_POSTER, WallLoadMore } from "@/components/library-detail-view";
import { PageNav } from "@/components/page-nav";
import { type FavoriteItem, listFavorites } from "@/lib/api/playback";
import { usePageTitle } from "@/lib/use-page-title";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/** 每次向服务端要的格数，与单库海报墙同一页长。 */
const PAGE_SIZE = 60;

/**
 * 「全部收藏」页（/library/favorites）：当前账号收藏的全部作品，海报墙形态
 * 与单库页完全一致——同一套格子（InventoryCell）、同一种滚动加载。数据是
 * playback_state 里的收藏列，与 Jellyfin 客户端点的心同一份；每格的详情落点
 * 是服务端解析好的可见库。首页只横滚最近 20 部，多的到这里看。
 */
export function FavoritesView() {
  usePageTitle("我的收藏");
  const scrollRef = useScrollRestoration("library:favorites");
  const [items, setItems] = useState<FavoriteItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [failed, setFailed] = useState(false);
  // 翻页请求进行中：哨兵重新观察时不重复发同一页
  const loading = useRef(false);

  const load = useCallback(async (offset: number) => {
    if (loading.current) return;
    loading.current = true;
    try {
      const page = await listFavorites(PAGE_SIZE, offset);
      setTotal(page.total);
      setItems((prev) => (offset === 0 || !prev ? page.items : [...prev, ...page.items]));
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      loading.current = false;
    }
  }, []);

  useEffect(() => {
    void load(0);
  }, [load]);

  const loaded = items?.length ?? 0;
  const hasMore = items !== null && loaded < total;
  const loadMore = useCallback(() => {
    void load(loaded);
  }, [load, loaded]);

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <PageNav title="我的收藏" fallback={{ label: "媒体库", href: "/library" as Route }} />
      <div className="px-6 max-md:px-4">
        <h2 className="text-on-image truncate text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[20px]">
          我的收藏
        </h2>
        <p className="text-on-image mt-1.5 truncate text-ui text-[var(--text-muted)] max-md:text-sub">
          {items === null
            ? "正在读取收藏…"
            : total > 0
              ? `${total} 部作品 · 最近收藏的在前 · 与 Jellyfin 客户端里点的心同一份`
              : "还没有收藏。在影片页点心，或在 Jellyfin 客户端里收藏，都会出现在这里。"}
        </p>

        {failed && items === null && (
          <div className="mt-16 flex flex-col items-center gap-3 text-center">
            <p className="text-ui text-[var(--text-muted)]">收藏加载失败</p>
            <button
              type="button"
              onClick={() => void load(0)}
              className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
            >
              重试
            </button>
          </div>
        )}

        {items !== null && items.length > 0 && (
          <>
            <div className={`mt-6 ${WALL_GRID_POSTER}`}>
              {items.map((item) => (
                <InventoryCell
                  key={item.media_item_id}
                  item={item}
                  libraryId={item.library_id}
                />
              ))}
            </div>
            <WallLoadMore
              hasMore={hasMore}
              loaded={loaded}
              start={0}
              total={total}
              onReach={loadMore}
            />
          </>
        )}
      </div>
    </div>
  );
}
