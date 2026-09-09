"use client";

import type { Route } from "next";
import Link from "next/link";

import type { Collection } from "@/lib/api/collections";
import { imageUrl } from "@/lib/image-proxy";

/**
 * 合集网格（docs/design/library-filtering.md 4.3）。
 *
 * **为什么是竖向网格而不是横滚行**：横滚行承载的是**内容**（Netflix 的每一行
 * 都是一排片名），不是**容器**。把合集摆成横滚行，默认只露 5~6 个，用户要
 * 一路推着看完——而"我有哪些合集"是一个需要一眼看全的问题。Plex 的 Collections、
 * Emby/Jellyfin 的 BoxSets 都是竖向网格，横滚行留给"某个合集里的片"。
 *
 * 卡片用成员海报拼封面：合集自己没有图，装的是什么就长什么样，这比任何
 * 生成的抽象封面都准。封面由服务端随列表一起给（见 collections 接口的 covers），
 * 客户端不为每个合集再请求一次成员。
 */
export function LibraryCollectionsView({
  collections,
  libraryId,
  emptyHint,
}: {
  collections: Collection[];
  libraryId: number;
  /** 一个合集都没有时说什么——不同入口的出路不一样，由调用方给 */
  emptyHint?: React.ReactNode;
}) {
  if (collections.length === 0) {
    return (
      <div className="px-6 py-16 text-center text-ui leading-7 text-[var(--text-muted)] max-md:px-4">
        {emptyHint ?? "还没有合集。筛出一批片之后，点「存为合集」就能把这组条件留下来。"}
      </div>
    );
  }
  return (
    <div className="grid gap-x-4 gap-y-7 px-6 [grid-template-columns:repeat(auto-fill,minmax(168px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:px-4 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]">
      {collections.map((collection) => (
        <CollectionCell key={collection.id} collection={collection} libraryId={libraryId} />
      ))}
    </div>
  );
}

/** 一格合集：2:3 竖框（与海报同规格，混排时不会长短不齐）+ 名字 + 数量。 */
function CollectionCell({
  collection,
  libraryId,
}: {
  collection: Collection;
  libraryId: number;
}) {
  return (
    <Link
      href={`/library/${libraryId}/c/${collection.id}` as Route}
      className="group block focus-visible:outline-none"
    >
      <CollectionCover collection={collection} />
      <div className="mt-2 min-w-0">
        <p className="truncate text-ui font-medium text-[var(--text-strong)]">{collection.name}</p>
        <p className="mt-0.5 text-sub text-[var(--text-faint)]">
          {collection.item_count} 部
          {/* 规则驱动的合集会自己长——这件事要在卡片上说清楚，否则用户
              会以为数字是当初存下来的那个快照 */}
          {collection.rule_driven && <span className="ml-1.5">· 自动收录</span>}
          {collection.visibility === "private" && <span className="ml-1.5">· 只有我</span>}
        </p>
      </div>
    </Link>
  );
}

/**
 * 封面：一张就铺满，多张就错落叠放。
 *
 * 叠放而不是九宫格：九宫格把每张海报切成邮票，谁也认不出；叠放保留了最前面
 * 那张的完整比例，后面两张只露一条边，读起来仍然是"一摞片"。
 */
function CollectionCover({ collection }: { collection: Collection }) {
  const shown = (collection.covers ?? []).slice(0, 3);
  return (
    <div className="relative aspect-[2/3] overflow-hidden rounded-xl bg-white/[0.04] ring-1 ring-white/[0.06] transition group-hover:ring-white/20">
      {shown.length === 0 ? (
        <div className="flex h-full items-center justify-center text-sub text-[var(--text-faint)]">
          暂无封面
        </div>
      ) : (
        shown.map((cover, index) => (
          <img
            key={cover.url}
            src={imageUrl(cover.url)}
            alt=""
            loading="lazy"
            // 第一张完整铺在最前，后面的向右挪出去一截、压暗，只从右边露一条
            className="absolute inset-y-0 h-full w-full object-cover"
            style={{
              left: `${index * 7}%`,
              zIndex: shown.length - index,
              filter: index === 0 ? undefined : "brightness(0.5)",
            }}
          />
        ))
      )}
    </div>
  );
}
