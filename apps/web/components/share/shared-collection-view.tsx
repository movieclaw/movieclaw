"use client";

import { useEffect, useState } from "react";

import { PosterImage } from "@/components/poster-image";
import { getSharedCollection, type SharedCollection } from "@/lib/api/shares";
import { usePageTitle } from "@/lib/use-page-title";

/**
 * 合集分享页的名单（docs/design/library-filtering.md F4）。
 *
 * 与站内的合集网格是两回事，所以**没有复用它**：站内那份要处理隐藏、系列分组、
 * 「设为收藏范围」这些只有登录用户才有的概念；访客看到的只该是"这几部片"。
 * 把站内组件塞一堆 optional 进来适配访客，比各写各的更难读。
 *
 * 成员由服务端每次现算：规则驱动的合集会自己长，朋友明天再打开这条链接，
 * 新入库的片就在里面了。
 */
export function SharedCollectionView({
  slug,
  onOpen,
}: {
  slug: string;
  /** 点一格 → 由外层切到那一部的影片页（同一条链接，不换路由） */
  onOpen: (mediaItemId: number) => void;
}) {
  const [data, setData] = useState<SharedCollection | null>(null);
  const [failed, setFailed] = useState(false);
  usePageTitle(data?.name);

  useEffect(() => {
    let alive = true;
    getSharedCollection(slug)
      .then((row) => alive && setData(row))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [slug]);

  if (failed) {
    return (
      <p className="mt-24 text-center text-ui text-white/60">这条分享暂时打不开，请稍后再试。</p>
    );
  }
  if (data === null) {
    return <p className="mt-24 text-center text-ui text-white/50">正在打开分享…</p>;
  }

  return (
    <div className="mx-auto w-full max-w-5xl px-6 py-8 max-md:px-4 max-md:py-6">
      <h1 className="text-title font-semibold text-white">{data.name}</h1>
      <p className="mt-1 text-ui text-white/50">{data.item_count} 部</p>

      {data.items.length === 0 ? (
        <p className="mt-16 text-center text-ui text-white/50">这个合集现在一部都没有。</p>
      ) : (
        <div className="mt-6 grid gap-x-4 gap-y-7 [grid-template-columns:repeat(auto-fill,minmax(150px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:[grid-template-columns:repeat(auto-fill,minmax(120px,1fr))]">
          {data.items.map((item) => (
            <button
              key={item.media_item_id}
              type="button"
              onClick={() => onOpen(item.media_item_id)}
              className="group block text-left focus-visible:outline-none"
            >
              <div className="relative aspect-[2/3] overflow-hidden rounded-xl bg-white/[0.04] ring-1 ring-white/[0.06] transition group-hover:ring-white/25">
                {item.poster_url ? (
                  <PosterImage
                    src={item.poster_url}
                    alt=""
                    className="absolute inset-0 size-full object-cover"
                  />
                ) : (
                  <span className="flex h-full items-center justify-center text-sub text-white/30">
                    暂无海报
                  </span>
                )}
              </div>
              <p className="mt-2 truncate text-ui font-medium text-white">{item.title}</p>
              {item.year !== null && (
                <p className="mt-0.5 text-sub text-white/40">{item.year}</p>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
