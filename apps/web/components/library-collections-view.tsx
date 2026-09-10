"use client";

import type { Route } from "next";
import Link from "next/link";

import { PosterImage } from "@/components/poster-image";
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
  /** 当前所在的库；跨库总览页传 null，每格按自己的 library_id 落地 */
  libraryId: number | null;
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
  // 分组：用户自己存的在前，自动生成的系列在后。一个 300 部的库可能有 40+ 个
  // 系列，平铺在一起的话**用户存的那三五个就没了**——那才是他花心思配出来的
  const mine = collections.filter((row) => row.kind !== "series");
  const series = collections.filter((row) => row.kind === "series");
  // 只有一类时不出标题：一个标题盖着全部内容是纯噪音
  const grouped = mine.length > 0 && series.length > 0;
  return (
    <div className="flex flex-col gap-7">
      <CollectionGrid
        title={grouped ? "我的合集" : undefined}
        collections={mine}
        libraryId={libraryId}
      />
      <CollectionGrid
        title={grouped ? "系列" : undefined}
        collections={series}
        libraryId={libraryId}
      />
    </div>
  );
}

/** 一组合集（带可选的组标题）。空组不占位。 */
function CollectionGrid({
  title,
  collections,
  libraryId,
}: {
  title?: string;
  collections: Collection[];
  libraryId: number | null;
}) {
  if (collections.length === 0) return null;
  return (
    <section>
      {title && (
        <h2 className="px-6 pb-3 text-sub font-medium tracking-wide text-[var(--text-faint)] max-md:px-4">
          {title}
        </h2>
      )}
      <div className="grid gap-x-4 gap-y-7 px-6 [grid-template-columns:repeat(auto-fill,minmax(168px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:px-4 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]">
        {collections.map((collection) => (
          <CollectionCell key={collection.id} collection={collection} libraryId={libraryId} />
        ))}
      </div>
    </section>
  );
}

/** 一格合集：2:3 竖框（与海报同规格，混排时不会长短不齐）+ 名字 + 数量。 */
function CollectionCell({
  collection,
  libraryId,
}: {
  collection: Collection;
  libraryId: number | null;
}) {
  // 跨库合集没有"所属库"，落到 /library/c/{id}；单库的照旧带着库号走，
  // 这样从库页点进去再返回还落回那个库
  const owner = libraryId ?? collection.library_id;
  return (
    <Link
      href={
        (owner === null
          ? `/library/c/${collection.id}`
          : `/library/${owner}/c/${collection.id}`) as Route
      }
      className={`group block focus-visible:outline-none ${collection.hidden ? "opacity-45" : ""}`}
    >
      <CollectionCover collection={collection} />
      <div className="mt-2 min-w-0">
        <p className="truncate text-ui font-medium text-[var(--text-strong)]">{collection.name}</p>
        <p className="mt-0.5 text-sub text-[var(--text-faint)]">
          {collection.item_count} 部
          {/* 分得清合集从哪来：系列是自动长出来的一整套，自建的是用户存的一组
              条件。**这里不显示「缺 2 部」**——一屏几十个红角标是压迫感不是
              帮助，缺片信息留在详情页（设计文档 6.5.2） */}
          {collection.kind === "series" ? (
            <span className="ml-1.5">· 系列</span>
          ) : (
            /* 规则驱动的合集会自己长——这件事要在卡片上说清楚，否则用户
               会以为数字是当初存下来的那个快照 */
            collection.rule_driven && <span className="ml-1.5">· 自动收录</span>
          )}
          {collection.visibility === "private" && <span className="ml-1.5">· 只有我</span>}
          {/* 只有开着「显示已隐藏的合集」时才会出现在这里；标出来用户才知道
              点进去要做什么（把它恢复回来） */}
          {collection.hidden && <span className="ml-1.5">· 已隐藏</span>}
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
        // 用全站统一的 PosterImage，不要自己写 <img>：卡片这类格子常处在
        // content-visibility 跳过态，Chromium 不给里面的 loading="lazy" 做
        // 相交判定，图会一直不发请求（见 poster-image.tsx 的模块注释）
        //
        // 几何：每张都比框窄一截（FRONT_WIDTH），后面的依次右移、上下内缩、
        // 压暗，于是右边露出两片窄边——「这是一叠」的信号就靠它，不靠改宽高比。
        // 最前那张必须**窄于框**，否则它会把身后两张整片盖住，看着与单张无异。
        shown.map((cover, index) => (
          <div
            key={cover.url}
            className="absolute overflow-hidden rounded-xl"
            style={{
              left: `${index * STACK_STEP}%`,
              width: `${FRONT_WIDTH}%`,
              top: `${index * STACK_INSET}%`,
              bottom: `${index * STACK_INSET}%`,
              zIndex: shown.length - index,
              filter: index === 0 ? undefined : "brightness(0.55)",
            }}
          >
            <PosterImage
              src={imageUrl(cover.url)}
              alt=""
              className="absolute inset-0 size-full object-cover"
            />
          </div>
        ))
      )}
    </div>
  );
}

/** 最前那张占框宽的比例：留出右边那两片窄边。 */
const FRONT_WIDTH = 86;
/** 每往后一张右移多少（框宽的百分比）。 */
const STACK_STEP = 7;
/** 每往后一张上下各内缩多少（框高的百分比）：越靠后越"矮"，才有纵深。 */
const STACK_INSET = 2.5;
