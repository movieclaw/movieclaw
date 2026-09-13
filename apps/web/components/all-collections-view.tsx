"use client";

import { useEffect, useMemo, useState } from "react";

import { LibraryCollectionsView } from "@/components/library-collections-view";
import { LibrarySectionSwitch } from "@/components/library-section-switch";
import { listCollections, type Collection } from "@/lib/api/collections";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import { usePageChrome } from "@/lib/page-chrome";
import { useIsMobile } from "@/lib/use-media-query";
import { usePageTitle } from "@/lib/use-page-title";

/**
 * 跨库合集总览（/library/collections，docs/design/library-filtering.md 2 节）。
 *
 * **它是媒体库首页的同级视角，不是子页**：页头与首页同一副长相（同一个「媒体库」
 * 大标题 + 右上角的 首页 / 合集 分段控件），来回切换靠那个控件，因此这里不挂
 * PageNav、也没有返回键——与发现、活动、订阅这些分区级页面一个规矩。改这一点前
 * 请先想清楚：挂了 PageNav，移动端全局顶栏会被撤掉，切换控件就没地方待了。
 *
 * 分组按库，跨库的那些单独一组：合集挂在库下面是这套 IA 的基本决策，
 * 总览页要如实反映这件事，而不是把所有合集拍平成一片。
 */
export function AllCollectionsView() {
  usePageTitle("全部合集");
  const [rows, setRows] = useState<Collection[] | null>(null);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");

  // 视角切换的挂载方式与媒体库首页逐字一致，两处必须一起改
  const chrome = usePageChrome();
  const isMobile = useIsMobile();
  const sectionSwitch = useMemo(
    () => (
      <LibrarySectionSwitch
        current="collections"
        className="mt-1 max-md:mt-0"
      />
    ),
    [],
  );
  const setTopBarActions = chrome?.setTopBarActions;
  useEffect(() => {
    if (!isMobile || !setTopBarActions) return;
    return setTopBarActions(sectionSwitch);
  }, [isMobile, sectionSwitch, setTopBarActions]);

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

  // 类型筛选：合集自己没有「是电影还是剧集」这个字段，也不该为此让后端逐个数成员——
  // 合集挂在库下面（这套 IA 的基本决策），库的 kind 就是它的类型，直接用。
  // 跨库合集（library_id=null）判不出类型，只在「全部」里出现；库里还有 video/photo
  // 这两档，它们既不是电影也不是剧集，同样只在「全部」里出现。
  const kindOfLibrary = new Map(libraries.map((lib) => [lib.id, lib.kind]));
  const matchesKind = (row: Collection) =>
    kindFilter === "all" ||
    (row.library_id !== null &&
      kindOfLibrary.get(row.library_id) === kindFilter);

  // 来源筛选：自建 vs 自动。`kind=user` 是自己攒的，其余（系列刮出来的 + 内置的
  // 「我的收藏」）都归「自动」——两档把清单切干净，数目加起来正好是全部，
  // 不留「哪一档都不属于」的零头。
  const matchesSource = (row: Collection) =>
    sourceFilter === "all" ||
    (sourceFilter === "user" ? row.kind === "user" : row.kind !== "user");

  const all = rows ?? [];
  const shown = all.filter((row) => matchesKind(row) && matchesSource(row));

  // 两个维度的计数都**带上另一个维度的筛选**（faceted）：显示的就是「点下去会得到几个」。
  // 算绝对数的话，选了「电影」再选「自建」会得到 0——而 0 的档本该是点不动的，
  // 「永不空货架」那条规矩就破了（你这份库里电影库恰好一个自建合集都没有）。
  const kindCounts: Record<KindFilter, number> = {
    all: all.filter(matchesSource).length,
    tv: all.filter(
      (row) =>
        matchesSource(row) && kindOfLibrary.get(row.library_id ?? -1) === "tv",
    ).length,
    movie: all.filter(
      (row) =>
        matchesSource(row) &&
        kindOfLibrary.get(row.library_id ?? -1) === "movie",
    ).length,
  };
  const sourceCounts: Record<SourceFilter, number> = {
    all: all.filter(matchesKind).length,
    user: all.filter((row) => matchesKind(row) && row.kind === "user").length,
    auto: all.filter((row) => matchesKind(row) && row.kind !== "user").length,
  };

  // 自建的排在最前：库里混着上百个刮出来的系列时，自己攒的那几个不该要翻着找。
  // 次序在来源之后不再干预，`sort` 是稳定的，同档内保持服务端给的 position 序。
  const sourceRank = (row: Collection) =>
    row.kind === "user" ? 0 : row.kind === "builtin" ? 1 : 2;
  const bySource = (items: Collection[]) =>
    [...items].sort((a, b) => sourceRank(a) - sourceRank(b));

  const cross = bySource(shown.filter((row) => row.library_id === null));
  const ordered = libraries
    .map((library) => ({
      library,
      items: bySource(shown.filter((row) => row.library_id === library.id)),
    }))
    .filter((group) => group.items.length > 0);

  const filtered = kindFilter !== "all" || sourceFilter !== "all";
  const summary =
    rows === null
      ? "正在读取合集…"
      : all.length === 0
        ? "还没有合集"
        : filtered
          ? `${shown.length} / ${all.length} 个合集 · 按库分组`
          : `${all.length} 个合集 · 按库分组`;

  const header = (
    <div className="flex items-start justify-between gap-4 px-6 pt-7 max-md:px-4 max-md:pt-4">
      <div className="min-w-0">
        <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
          媒体库
        </h2>
        <p className="text-on-image mt-1.5 text-ui text-[var(--text-muted)] max-md:mt-1 max-md:text-sub">
          {summary}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {!isMobile && sectionSwitch}
      </div>
    </div>
  );

  /* 筛选放在正文顶部、靠左，与单库墙的筛选条同一个位置感，而不是塞进右上角那一行：
     右上角是**视角切换**的地盘，两者不是一回事；窄屏的全局顶栏也已经排着
     ☰ + 字标 + 首页/合集 + 搜索，再多一组必然挤出屏幕。正文顶部两个断点同一份实现。 */
  const filterBar = (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-6 pt-4 max-md:px-4 max-md:pt-3">
      <ChipGroup
        label="类型"
        options={KIND_OPTIONS}
        value={kindFilter}
        counts={kindCounts}
        onChange={setKindFilter}
      />
      <ChipGroup
        label="来源"
        options={SOURCE_OPTIONS}
        value={sourceFilter}
        counts={sourceCounts}
        onChange={setSourceFilter}
      />
    </div>
  );

  // 页面自己出滚动容器：外壳的 main 不滚动（与收藏页、单库页同一约定），
  // 少了这一层，合集一多就滑不动
  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      {header}
      {rows === null ? (
        <p className="mt-16 text-center text-ui text-[var(--text-muted)]">
          正在读取合集…
        </p>
      ) : all.length === 0 ? (
        <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">
          还没有合集。在某个库里筛出一批片，点「存为合集」就能把这组条件留下来。
        </p>
      ) : (
        /* 没有「筛完为空」这一态：数为 0 的档在筛选条上就点不动（「永不空货架」，
           与单库墙的 PillGroup 同一条规矩），选中的档必然有货 */
        <>
          {filterBar}
          <div className="flex flex-col gap-8 pt-6 max-md:pt-4">
            {cross.length > 0 && (
              <section>
                {/* 页头现在有「媒体库」这个 h2，分区标题降一级，标题层级才是连着的 */}
                <h3 className="px-6 pb-1 text-ui font-semibold text-[var(--text-strong)] max-md:px-4">
                  跨库
                </h3>
                <p className="px-6 pb-3 text-sub text-[var(--text-faint)] max-md:px-4">
                  不属于任何一个库的手动名单
                </p>
                <LibraryCollectionsView collections={cross} libraryId={null} />
              </section>
            )}
            {ordered.map(({ library, items }) => (
              <section key={library.id}>
                <h3 className="px-6 pb-3 text-ui font-semibold text-[var(--text-strong)] max-md:px-4">
                  {library.name}
                </h3>
                <LibraryCollectionsView
                  collections={items}
                  libraryId={library.id}
                />
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/** 类型筛选的取值：合集自己没有类型，这里说的是它所属库的 kind。 */
type KindFilter = "all" | "tv" | "movie";

/** 来源筛选的取值：user=自己攒的；auto=刮出来的系列 + 内置的「我的收藏」。 */
type SourceFilter = "all" | "user" | "auto";

const KIND_OPTIONS: readonly { value: KindFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "tv", label: "剧集" },
  { value: "movie", label: "电影" },
];

const SOURCE_OPTIONS: readonly { value: SourceFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "user", label: "自建" },
  { value: "auto", label: "自动" },
];

/**
 * 一个筛选维度：标签 + 一排带计数的胶囊。
 *
 * **长相跟着媒体库的筛选条走**（`library-filter-bar.tsx` 的 `CollectionChips`
 * 与 `PillGroup`）：`h-7` 的无边框胶囊 + `text-caption` + 等宽计数，选中只是底色
 * 加重一档；标签也用 `PillGroup` 那一档小字。筛选在这套界面里是「轻」的东西，
 * 最初照搬发现页那组分段控件（外框胶囊、backdrop-blur、`text-sub` 加粗）压得太重——
 * 那副长相是给**视角切换**用的，视角切换是页面级的大事，筛选不是，不该同款。
 *
 * 沿用筛选条「永不空货架」的规矩：数为 0 的档压暗且点不动。计数由调用方按
 * faceted 口径算好（带上另一个维度的筛选），这里只负责画。
 */
function ChipGroup<T extends string>({
  label,
  options,
  value,
  counts,
  onChange,
}: {
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  counts: Record<T, number>;
  onChange: (next: T) => void;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      <span className="text-caption text-[var(--text-faint)]">{label}</span>
      {options.map((option) => {
        const on = value === option.value;
        const dead = counts[option.value] === 0 && !on;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={on}
            disabled={dead}
            onClick={() => onChange(option.value)}
            className={`flex h-7 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-caption transition disabled:pointer-events-none disabled:opacity-30 ${
              on
                ? "bg-white/[0.16] text-white"
                : "bg-white/[0.05] text-white/70 hover:bg-white/[0.10] hover:text-white"
            }`}
          >
            {option.label}
            <span className="font-mono tabular-nums text-white/40">
              {counts[option.value]}
            </span>
          </button>
        );
      })}
    </div>
  );
}
