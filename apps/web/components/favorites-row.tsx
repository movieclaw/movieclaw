"use client";

import { useState } from "react";

import type { Route } from "next";

import { HScroller } from "@/components/h-scroller";
import { ChevronDownIcon, HeartIcon } from "@/components/icons";
import { PosterCard } from "@/components/poster-card";
import type { MediaItem } from "@/lib/media-types";

/**
 * 超过这个数才给「展开」：一行横滚在桌面端大约放得下 8 张海报，少于此数
 * 本来就一屏尽收，展开成网格只是把同样几张卡换个排法。
 */
export const FAVORITES_EXPAND_THRESHOLD = 8;

/**
 * 媒体库首页顶部的「我的收藏」分区。
 *
 * 数据是 playback_state 里的收藏列，与 Jellyfin 客户端点的心同一份：在 Infuse
 * 里点过的这里就有。默认一行横滚（与「最近添加」同一规格的海报卡），收藏多了
 * 标题行右端出现「展开全部」，切成自动换行的网格把全部收藏铺开；再点收起。
 * 没有收藏时整段隐藏，不给从未点过心的用户留一个空分区。
 */
export function FavoritesRow({
  items,
  total,
  hrefOf,
}: {
  /** 已转成海报卡形态的收藏（最近收藏在前）；null = 还没拉到 */
  items: MediaItem[] | null;
  /** 去重后的收藏总数（items 可能受服务端 limit 截断） */
  total: number;
  hrefOf: (item: MediaItem) => Route | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!items?.length) return null;
  const expandable = items.length > FAVORITES_EXPAND_THRESHOLD;

  const cards = items.map((item) => (
    <div
      key={`favorite-${item.id}`}
      className="w-[152px] shrink-0 max-md:w-[126px] xl:w-[164px]"
    >
      <PosterCard item={item} action="none" href={hrefOf(item)} revealInfoOnTouch />
    </div>
  ));

  return (
    <section className="mt-8 max-md:mt-6" aria-labelledby="favorites-title">
      <div className="flex items-center justify-between gap-4 px-6 max-md:px-4">
        <h3
          id="favorites-title"
          className="text-on-image flex items-center gap-2 text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]"
        >
          <HeartIcon className="size-[18px] text-[var(--danger)]" fill="currentColor" />
          我的收藏
          <span className="tnum text-sub font-medium text-[var(--text-faint)]">{total}</span>
        </h3>
        {expandable && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="flex shrink-0 items-center gap-1 text-sub font-semibold text-[var(--text-muted)] transition hover:text-[var(--text)]"
          >
            {expanded ? "收起" : `展开全部 ${total} 部`}
            <ChevronDownIcon
              className={`size-4 transition-transform ${expanded ? "rotate-180" : ""}`}
            />
          </button>
        )}
      </div>
      {expanded ? (
        <div className="mt-3 flex flex-wrap gap-4 px-6 pb-1 pt-1 max-md:gap-3 max-md:px-4">
          {cards}
        </div>
      ) : (
        <HScroller className="mt-3 gap-4 px-6 pb-1 pt-1 max-md:gap-3 max-md:px-4">{cards}</HScroller>
      )}
    </section>
  );
}
