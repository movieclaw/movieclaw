"use client";

import { useEffect, useState } from "react";
import type { Route } from "next";

import { PageNav } from "@/components/page-nav";
import { LibraryCollectionsView } from "@/components/library-collections-view";
import { listCollections, type Collection } from "@/lib/api/collections";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import { usePageTitle } from "@/lib/use-page-title";

/**
 * 跨库合集总览（/library/collections，docs/design/library-filtering.md 2 节）。
 *
 * **入口等到合集多了才露出**：一开始就在导航里多一个「合集」，用户点进去
 * 只有一两个自建的，那个位置就白占了。所以它不进侧边栏，从媒体库首页的
 * ⋯ 里进——想看全部合集的人自然找得到，其余人不受打扰。
 *
 * 分组按库，跨库的那些单独一组：合集挂在库下面是这套 IA 的基本决策，
 * 总览页要如实反映这件事，而不是把所有合集拍平成一片。
 */
export function AllCollectionsView() {
  usePageTitle("全部合集");
  const [rows, setRows] = useState<Collection[] | null>(null);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);

  useEffect(() => {
    let alive = true;
    listCollections()
      .then((all) => alive && setRows(all))
      .catch(() => alive && setRows([]));
    listLibraries()
      .then((all) => alive && setLibraries(all))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  if (rows === null) {
    return (
      <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
        <PageNav title="全部合集" fallback={{ label: "媒体库", href: "/library" as Route }} />
        <p className="mt-16 text-center text-ui text-[var(--text-muted)]">正在读取合集…</p>
      </div>
    );
  }

  const cross = rows.filter((row) => row.library_id === null);
  const ordered = libraries
    .map((library) => ({
      library,
      items: rows.filter((row) => row.library_id === library.id),
    }))
    .filter((group) => group.items.length > 0);

  // 页面自己出滚动容器：外壳的 main 不滚动（与收藏页、单库页同一约定），
  // 少了这一层，合集一多就滑不动
  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <PageNav title="全部合集" fallback={{ label: "媒体库", href: "/library" as Route }} />
      {rows.length === 0 ? (
        <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">
          还没有合集。在某个库里筛出一批片，点「存为合集」就能把这组条件留下来。
        </p>
      ) : (
        <div className="flex flex-col gap-8 pt-2">
          {cross.length > 0 && (
            <section>
              <h2 className="px-6 pb-1 text-ui font-semibold text-[var(--text-strong)] max-md:px-4">
                跨库
              </h2>
              <p className="px-6 pb-3 text-sub text-[var(--text-faint)] max-md:px-4">
                不属于任何一个库的手动名单
              </p>
              <LibraryCollectionsView collections={cross} libraryId={null} />
            </section>
          )}
          {ordered.map(({ library, items }) => (
            <section key={library.id}>
              <h2 className="px-6 pb-3 text-ui font-semibold text-[var(--text-strong)] max-md:px-4">
                {library.name}
              </h2>
              <LibraryCollectionsView collections={items} libraryId={library.id} />
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
