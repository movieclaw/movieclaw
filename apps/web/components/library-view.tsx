"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { Route } from "next";
import Link from "next/link";

import { BrandLoader } from "@/components/brand-loader";
import { ContentEmptyState } from "@/components/content-empty-state";
import { HScroller } from "@/components/h-scroller";
import { LIBRARY_KIND_META } from "@/components/library-kind-meta";
import {
  FilmIcon,
  GearIcon,
  ListIcon,
  PlusIcon,
} from "@/components/icons";
import { MediaRow } from "@/components/media-row";
import type { PosterCardAction } from "@/components/poster-card";
import { UpNextRow } from "@/components/up-next-row";
import {
  type LibraryItem,
  type MediaLibrary,
  listLibraries,
  listLibraryItems,
  SCAN_PHASE_LABELS,
} from "@/lib/api/libraries";
import { type Collection, listCollectionItems, listCollections } from "@/lib/api/collections";
import { withDeadline } from "@/lib/first-paint-deadline";
import {
  type FavoriteItem,
  type FavoritesPage,
  listFavorites,
  listUpNext,
  type UpNextItem,
} from "@/lib/api/playback";
import type { Subscription } from "@/lib/api/subscriptions";
import { publicEnv } from "@/lib/env";
import { favoriteLevelLabel } from "@/lib/favorites";
import {
  buildHomeRows,
  FAVORITES_SORT_PRESETS,
  type HomeRow,
  orderParamFor,
  rowTitle,
  SORT_PRESETS,
} from "@/lib/home-rows";
import { formatBytes } from "@/lib/format";
import { cardVariantFor, imageUrl } from "@/lib/image-proxy";
import { libraryInventoryAction } from "@/lib/library-inventory-summary";
import type { MediaItem } from "@/lib/media-types";
import { usePermissions } from "@/lib/permissions";
import { buildRecentAdditionOverlay } from "@/lib/recent-addition";
import { formatRelativeTime } from "@/lib/time";
import { useUiPrefs } from "@/lib/ui-prefs";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/** 每个库行 / 合集行的格数（也是本页向服务端要的条目数上限）。 */
const RECENT_COUNT = 20;
/** 「接下来继续」横滚行最多几张卡。 */
const UP_NEXT_COUNT = 20;
/** 「我的收藏」横滚行只放最近收藏的这么多部，更多的到 /library/favorites 看。 */
const FAVORITES_COUNT = 20;

/**
 * 库存条目的悬浮操作与本卡 hover 的完整度文案同源：季或集有一项未齐就
 * 「补齐缺集」；当前已知季集全部在库则「自动续订」，等待未来出现的新季。
 * 已在“我的订阅”中的条目由 PosterCardVisual 统一隐藏操作，不再显示
 * 没有决策价值的“已订阅”按钮。
 */
export function libraryCardAction(item: LibraryItem): PosterCardAction {
  return libraryInventoryAction(item.kind, item.inventory_summary);
}

/**
 * 订阅的实际归属库：显式指定优先，否则该类型的默认库。
 * 与后端 resolve_for_subscription 同一语义，库页与单库页共用。
 */
export function effectiveLibraryId(
  sub: Subscription,
  libraries: MediaLibrary[],
): number | null {
  if (sub.library_id != null) return sub.library_id;
  return libraries.find((l) => l.kind === sub.media.kind && l.is_default)?.id ?? null;
}

/** 媒体库首页摘要：只聚合接口随库返回的预计算快照，不触发额外请求。 */
export function libraryStatsSummary(libraries: MediaLibrary[] | null): string {
  if (libraries === null) return "正在汇总媒体库统计…";
  if (libraries.length === 0) return "还没有媒体库，创建后会在这里显示库存统计";
  const movieCount = libraries
    .filter((library) => library.kind === "movie")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const tvCount = libraries
    .filter((library) => library.kind === "tv")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const videoCount = libraries
    .filter((library) => library.kind === "video")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const totalSizeBytes = libraries.reduce(
    (total, library) => total + library.stats.total_size_bytes,
    0,
  );
  const videoPart = videoCount > 0 ? ` · ${videoCount} 个其他视频` : "";
  return `${libraries.length} 个媒体库 · ${movieCount} 部电影 · ${tvCount} 部剧集${videoPart} · 共占用 ${formatBytes(totalSizeBytes)} 存储空间`;
}

/**
 * 媒体库页（/library）：全部库的 Emby 风格卡片横排——**只做浏览入口**。
 *
 * 每张卡是一个库：封面用库内作品的海报做「货架」展示（最多 4 张站立海报
 * 带底部倒影，纯前端 CSS 合成、零后端开销），叠库名/类型/统计；
 * 点击进入单库海报墙（/library/[id]）。库的增删改/设默认/扫描/排序全部在
 * 管理页（/library/manage）完成，见 docs/design/library-manage.md——卡片上只留
 * 预告"马上会看到新内容"的信息：扫描进度环与「入库中」徽标。
 *
 * 数据源是 library_file 台账的**真实库存**（L3 起）：入库管线与存量扫描
 * 落账的文件聚合，不再用订阅占位。
 */
//: 首帧等合集列表的预算（毫秒）。见 reload 里的说明
const COLLECTIONS_FIRST_PAINT_BUDGET_MS = 1500;

/**
 * 上一次成功加载的首页数据（模块级，进程内存，跨路由驻留）。
 *
 * 顶栏「媒体库」等入口回到本页时组件会重挂载：没有这份快照，首帧只能画
 * 「正在加载…」的矮内容，滚动恢复（use-scroll-restoration）要等行数据到齐、
 * 内容撑到旧位置的高度才能写入 scrollTop——页面先在最上端闪一拍、再跳回
 * 离开处。快照让首帧直接以全量内容渲染，恢复就能在首次绘制前落位。
 * 数据仍照常重新拉取刷新，快照只是绘制起点，不承担缓存有效期职责。
 */
let lastLoadedHome: {
  libraries: MediaLibrary[];
  collections: Collection[];
  upNext: UpNextItem[];
  favorites: FavoritesPage;
  itemsByKey: Map<string, LibraryItem[]>;
} | null = null;

export function LibraryView({ hero }: { hero?: ReactNode }) {
  const { canManageLibraries } = usePermissions();
  // 首页的行清单存在界面偏好里（成员各存各的），应用启动时已随全站偏好拉过一次
  const { prefs } = useUiPrefs();
  const homePrefs = prefs.home;
  const scrollRef = useScrollRestoration("library");
  // 各状态初值取上次会话留存的快照（没有则走加载态），见 lastLoadedHome
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(
    () => lastLoadedHome?.libraries ?? null,
  );
  // 当前身份可见的合集：合并行清单要靠它认出合集行；数字大于零也决定
  // 「全部合集」入口露不露（空合集后端已经滤掉了）
  const [collections, setCollections] = useState<Collection[]>(
    () => lastLoadedHome?.collections ?? [],
  );
  // 库行 / 合集行 / 库卡片封面的条目按「取数键」缓存：同一个库同一种排序只请求
  // 一次（库卡片封面与默认的「最近添加」行共用 added_at 那一份）
  const [itemsByKey, setItemsByKey] = useState<Map<string, LibraryItem[]>>(
    () => lastLoadedHome?.itemsByKey ?? new Map(),
  );
  const [upNext, setUpNext] = useState<UpNextItem[] | null>(() => lastLoadedHome?.upNext ?? null);
  // 我的收藏：与接下来继续同一轮拉取、同一套失败策略（拉不到保留旧数据）
  const [favorites, setFavorites] = useState<FavoritesPage | null>(
    () => lastLoadedHome?.favorites ?? null,
  );
  const [failed, setFailed] = useState(false);

  // 轮询乱序守卫：扫描期间后端响应时间抖动大，上一轮的慢响应可能晚于
  // 下一轮到达，不作废就会用旧快照覆盖新状态（进度回跳、卡片状态闪烁）
  const reloadSeq = useRef(0);
  // 上一轮已拉过条目时的快照（库状态 + 要取哪些行 + 合集成员数）：都没变说明
  // 库存也不会变，不必再逐行全量拉一遍——否则空闲时每 30 秒也要打出 1 + N 个请求
  const lastSnapshot = useRef<string | null>(null);
  // 首帧只等这么久合集列表：它是首页最重的一条读（后台任务压着数据库时曾到
  // 十几秒），而库列表几十毫秒就回来。预算内回来照常；超时先用上一份把页面画
  // 出来，真正的结果晚到再整页补一轮。首帧之后不限预算——页面已经在了，多等
  // 一会儿合集不影响任何东西
  const painted = useRef(false);
  const lastCollections = useRef<Collection[]>([]);
  const reloadRef = useRef<() => void>(() => {});
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    Promise.all([
      listLibraries(),
      // 合集拿不到就当上一份还在——不拖垮首页，合集行下一轮轮询自动回来
      withDeadline(
        listCollections(),
        painted.current ? null : COLLECTIONS_FIRST_PAINT_BUDGET_MS,
        null,
      ),
    ])
      .then(async ([libs, colsOutcome]) => {
        if (seq !== reloadSeq.current) return;
        painted.current = true;
        setFailed(false);
        const collectionsNow = colsOutcome.value ?? lastCollections.current;
        lastCollections.current = collectionsNow;
        if (colsOutcome.late) {
          // 晚到的那份：没有更新一轮的 reload 抢先时整页再拉一次，
          // 合集行及其条目随之出现
          void colsOutcome.late.then((late) => {
            if (late && seq === reloadSeq.current) reloadRef.current();
          });
        }
        const collectionsSnapshot = JSON.stringify(collectionsNow);
        setCollections((prev) =>
          JSON.stringify(prev) === collectionsSnapshot ? prev : collectionsNow,
        );
        const libsSnapshot = JSON.stringify(libs);
        // 内容没变就复用旧引用，跳过整页卡片的无谓重渲染
        setLibraries((prev) => (prev && JSON.stringify(prev) === libsSnapshot ? prev : libs));

        // 只取**显示中的**行：隐藏的行不发请求
        const rows = buildHomeRows(homePrefs, libs, collectionsNow).filter((row) => !row.hidden);
        const favoritesRow = rows.find((row) => row.kind === "favorites");
        // 接下来继续 / 我的收藏随播放状态变，每轮都拉；失败不拖垮首页，保留旧数据
        const [latestUpNext, latestFavorites] = await Promise.all([
          rows.some((row) => row.kind === "up-next")
            ? listUpNext(UP_NEXT_COUNT).catch(() => null)
            : Promise.resolve(null),
          favoritesRow && favoritesRow.kind === "favorites"
            ? listFavorites(
                FAVORITES_COUNT,
                0,
                // 「未看优先」是首页这一行的默认：没看完的提前（全量页不传，保持收藏时间序）
                favoritesRow.sort === "unwatched_first",
                {
                  sort:
                    favoritesRow.sort === "unwatched_first" ? "favorited_at" : favoritesRow.sort,
                  // 反转了自然方向才带 order，与海报墙同一条规矩
                  order: orderParamFor(
                    FAVORITES_SORT_PRESETS[favoritesRow.sort].direction,
                    favoritesRow.reversed,
                  ),
                },
              ).catch(() => null)
            : Promise.resolve(null),
        ]);
        if (seq !== reloadSeq.current) return;
        if (latestUpNext !== null) setUpNext(latestUpNext);
        else setUpNext((previous) => previous ?? []);
        if (latestFavorites !== null) setFavorites(latestFavorites);
        else setFavorites((previous) => previous ?? { items: [], total: 0 });

        const fetches = rowFetches(rows, libs);
        const snapshot = `${libsSnapshot}|${[...fetches.keys()].join(",")}|${collectionsSnapshot}`;
        if (snapshot === lastSnapshot.current) return;
        const entries = await Promise.all(
          [...fetches].map(
            async ([key, fetch]) => [key, await fetch().catch((): LibraryItem[] => [])] as const,
          ),
        );
        if (seq !== reloadSeq.current) return;
        lastSnapshot.current = snapshot;
        setItemsByKey(new Map(entries));
      })
      // 瞬时失败不清已有数据：failed 只决定提示条，卡片继续用上一份快照，
      // 下一轮轮询成功即自动恢复（整页错误屏只留给一次都没加载成功的情况）
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, [homePrefs]);

  useEffect(() => {
    reloadRef.current = reload;
    reload();
  }, [reload]);

  // 成功到手的数据随手更新模块级快照，供下次重挂载首帧直出（见 lastLoadedHome）。
  // 只在 libraries 已加载时写：加载态/失败态不该顶掉上一份好数据。
  useEffect(() => {
    if (libraries === null) return;
    lastLoadedHome = {
      libraries,
      collections,
      upNext: upNext ?? [],
      favorites: favorites ?? { items: [], total: 0 },
      itemsByKey,
    };
  }, [libraries, collections, upNext, favorites, itemsByKey]);

  // 有库在扫描/整理时轮询刷新，任务完成即看到最新库存与文件名
  const busyAny = (libraries ?? []).some((l) => l.scanning || l.organizing);
  // 元数据刷新单独一档：它以分钟计，而本页每轮 reload 还要把每个库的条目
  // 列表拉一遍，用扫描那档 3 秒会打出上百次无谓请求；5 秒足够让进度环和
  // "到哪部了"看着在动（完整阶段列表在单库页的面板里）
  const refreshingAny = (libraries ?? []).some((l) => l.metadata_refresh?.refreshing);
  // 有文件写入中暂缓入账（拷贝/下载进行时 watchdog 已发现，等补扫落定）：
  // 中速轮询让「入库中」徽标与随后的库存变化自动呈现；完全空闲时低频兜底
  // ——后台自发的扫描（实时监控/定时对账）页面开着不动也能感知到
  const importingAny = (libraries ?? []).some(
    (l) => !l.scanning && !l.organizing && (l.last_scan?.deferred ?? 0) > 0,
  );
  // busy 刚结束后保持快轮询一小段再降速：监控去抖触发的连环扫描之间隔着
  // 几秒空档，一采样到空档就降去 30 秒档的话，下一轮扫描的开始要很久
  // 才被发现，卡片状态看起来就是"时隐时现"
  const [recentlyBusy, setRecentlyBusy] = useState(false);
  useEffect(() => {
    if (busyAny) {
      setRecentlyBusy(true);
      return;
    }
    if (!recentlyBusy) return;
    const timer = setTimeout(() => setRecentlyBusy(false), 12_000);
    return () => clearTimeout(timer);
  }, [busyAny, recentlyBusy]);
  // 页面隐藏时暂停轮询、恢复可见立即补一次（useVisiblePolling）
  useVisiblePolling(
    reload,
    busyAny || recentlyBusy ? 3000 : refreshingAny ? 5000 : importingAny ? 10_000 : 30_000,
  );

  // 卡片区只放当前身份能浏览的库：超管把自己摘出浏览范围的库（「仅管理」）
  // 对首页来说就是不存在，它只在管理页出现（带锁标）
  const visibleLibraries = useMemo(
    () => (libraries ?? []).filter((library) => library.viewer_access),
    [libraries],
  );

  // 首页 = 行清单：存过的按存的顺序，没存过的内置行与每库默认行补在后面
  // （规则见 lib/home-rows.ts）。这里再合并一次是为了渲染，与 reload 里取数
  // 用的是同一个纯函数，不会出现"取了 A 行、画了 B 行"
  const rows = useMemo(
    () => buildHomeRows(homePrefs, libraries ?? [], collections),
    [homePrefs, libraries, collections],
  );
  const visibleRows = useMemo(() => rows.filter((row) => !row.hidden), [rows]);
  const collectionCount = collections.length;
  // 「全部合集」入口默认挂在「我的媒体库」行的标题右侧。但那一行不是永远都在：
  // 用户可以在「自定义首页」里隐藏它，一个可见库都没有时整节也 return null。
  // 它一消失，入口就跟着没了——而 /library/collections 全站**只有这一个入口**
  // （合集页自己的 LibrarySectionSwitch 要先进得去才用得上），等于功能在 UI 上
  // 彻底不可达。这两种情况下把入口抬到页头动作区，保证始终有一条路进得去。
  const librariesRowVisible =
    rows.some((row) => row.kind === "libraries" && !row.hidden) && visibleLibraries.length > 0;
  const collectionsEntryInHeader = collectionCount > 0 && !librariesRowVisible;

  // 「我的收藏」横滚行：与库行同一张海报卡、同一个行组件，只把 hover
  // 层换成收藏的层级说明；落点是服务端解析好的可见库里的条目详情
  const favoriteRow = useMemo(() => {
    const items = favorites?.items ?? [];
    const hrefs = new Map(
      items.map((it) => [
        libraryItemKey(it),
        `/library/${it.library_id}/item/${it.media_item_id}` as Route,
      ]),
    );
    return {
      items: items.map(favoriteItemToMediaItem),
      hrefOf: (m: MediaItem) => hrefs.get(m.id),
    };
  }, [favorites]);

  /** 库行 / 合集行：服务端已按这一行的排序给到前 20，复用发现页的横滚海报行。
   *  已在库的条目点击进**媒体库条目详情**（本地刮削信息 + 片源规格 + 条目操作），
   *  与单库页库存格同一目标。只呈现入库上下文，订阅/补齐操作留在单库页。 */
  const contentRow = (row: HomeRow, moreHref: Route) => {
    const items = itemsByKey.get(rowFetchKey(row)) ?? [];
    // 空行整段隐藏：偏好决定「想不想看」，数据决定「有没有」
    if (items.length === 0) return null;
    const fallbackLibrary = row.kind === "library" ? row.library.id : row.kind === "collection" ? row.collection.library_id : null;
    const hrefs = new Map(
      items.map((it) => [
        libraryItemKey(it),
        `/library/${it.library_id ?? fallbackLibrary ?? 0}/item/${it.media_item_id}` as Route,
      ]),
    );
    return (
      <div key={row.id} className="mt-8 max-md:mt-6" data-testid={`home-row-${row.id}`}>
        <MediaRow
          row={{
            id: `home-${row.id}`,
            title: rowTitle(row),
            items: items.map(libraryItemToMediaItem),
          }}
          moreHref={moreHref}
          moreLabel="查看全部"
          cardAction="none"
          cardHref={(m) => hrefs.get(m.id)}
          cardRevealInfoOnTouch
        />
      </div>
    );
  };

  const renderRow = (row: HomeRow) => {
    switch (row.kind) {
      case "up-next":
        // 当前账号跨可见库聚合的播放状态；空列表时组件整段隐藏。
        // 清空观看记录的入口就在这一行的标题右侧，清完重新拉一次数据。
        return (
          <UpNextRow key={row.id} items={upNext} libraries={visibleLibraries} onCleared={reload} />
        );
      case "favorites":
        // 只横滚最近收藏的 20 部（网页与 Jellyfin 客户端点的心同一份），「查看全部」
        // 进与单库页同一套海报墙的 /library/favorites；没有收藏时整段隐藏
        if (favoriteRow.items.length === 0) return null;
        return (
          <div key={row.id} className="mt-8 max-md:mt-6" data-testid="favorites-row">
            <MediaRow
              row={{ id: "favorites", title: rowTitle(row), items: favoriteRow.items }}
              moreHref={"/library/favorites" as Route}
              moreLabel={`查看全部 ${favorites?.total ?? 0} 部`}
              cardAction="none"
              cardHref={favoriteRow.hrefOf}
              cardRevealInfoOnTouch
            />
          </div>
        );
      case "libraries":
        // 库卡片横排：库多了不换行堆高，改为一行横滚（与库行同一交互）
        if (visibleLibraries.length === 0) return null;
        return (
          <section key={row.id} className="mt-8 max-md:mt-6" aria-labelledby="my-libraries-title">
            <div className="flex items-center justify-between gap-4 px-6 max-md:px-4">
              <h3
                id="my-libraries-title"
                className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]"
              >
                {rowTitle(row)}
              </h3>
              {/* 「全部合集」的入口等到真有合集了才露出：一开始就摆在这儿，
                  用户点进去只有一片空白，那个位置就白占了（IA 那条决策）。
                  本行不可见时入口由页头动作区兜底，见 collectionsEntryInHeader */}
              {collectionCount > 0 && (
                <Link
                  href={"/library/collections" as Route}
                  className="shrink-0 text-ui text-[var(--text-faint)] transition hover:text-[var(--text)]"
                >
                  全部合集 ›
                </Link>
              )}
            </div>
            <HScroller className="mt-3 gap-5 px-6 pb-1 pt-1 max-md:gap-3.5 max-md:px-4">
              {visibleLibraries.map((library) => (
                <div
                  key={library.id}
                  data-library-card={library.id}
                  className="w-[268px] shrink-0 rounded-2xl max-md:w-[230px]"
                >
                  <LibraryCard
                    library={library}
                    items={itemsByKey.get(coverFetchKey(library.id)) ?? []}
                  />
                </div>
              ))}
            </HScroller>
          </section>
        );
      case "library":
        return contentRow(row, `/library/${row.library.id}` as Route);
      case "collection":
        return contentRow(
          row,
          (row.collection.library_id === null
            ? `/library/c/${row.collection.id}`
            : `/library/${row.collection.library_id}/c/${row.collection.id}`) as Route,
        );
    }
  };

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      {/* Netflix 主题的全出血 Billboard（原内容首页并入，见 library-hero.tsx）：
          挂在滚动容器内、跟随页面一起滚走，页头与行清单依次排在其后 */}
      {hero}
      {/* 页头：标题 + 统计，右侧是页面级操作「自定义首页」「管理媒体库」（SaaS 惯例：
          页面动作放标题行右端；分区标题行只留分区自己的东西）。首页上没有任何
          排序细节与行菜单——调整全部收进自定义页，首页只负责看 */}
      <div className="flex items-start justify-between gap-4 px-6 pt-7 max-md:px-4 max-md:pt-4">
        <div className="min-w-0">
          <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
            媒体库
          </h2>
          <p className="text-on-image mt-1.5 text-ui text-[var(--text-muted)] max-md:mt-1 max-md:line-clamp-2 max-md:text-sub">
            {failed && libraries === null
              ? "暂时无法获取媒体库统计，正在自动重试"
              : libraryStatsSummary(libraries === null ? null : visibleLibraries)}
          </p>
        </div>
        {/* 两个页面级动作都是图标钮：自定义首页（所有人）、管理媒体库（有权限的人） */}
        <div className="flex shrink-0 items-center gap-2">
          {collectionsEntryInHeader && (
            <Link
              href={"/library/collections" as Route}
              className="shrink-0 text-ui text-[var(--text-faint)] transition hover:text-[var(--text)]"
            >
              全部合集 ›
            </Link>
          )}
          <Link
            href={"/library/customize" as Route}
            aria-label="自定义首页"
            title="自定义首页"
            className="btn-glass mt-1 grid size-8 shrink-0 place-items-center !p-0 max-md:mt-0"
          >
            <ListIcon className="size-4" />
          </Link>
          {canManageLibraries && (
            <Link
              href={"/library/manage" as Route}
              aria-label="管理媒体库"
              title="管理媒体库"
              className="btn-glass mt-1 grid size-8 shrink-0 place-items-center !p-0 max-md:mt-0"
            >
              <GearIcon className="size-4" />
            </Link>
          )}
        </div>
      </div>

      {libraries === null && !failed && (
        <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <BrandLoader className="size-5" />
          正在加载媒体库…
        </div>
      )}

      {/* 只有一次都没加载成功过才整页报错；已有数据在手时，瞬时失败只挂
          提示条（stale-while-error），卡片照常展示上一份快照 */}
      {failed && libraries === null && (
        <div className="mt-16 flex flex-col items-center gap-3 text-center">
          <p className="text-ui text-[var(--text-muted)]">媒体库加载失败</p>
          <button
            type="button"
            onClick={reload}
            className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            重试
          </button>
        </div>
      )}

      {failed && libraries !== null && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {libraries !== null && libraries.length === 0 && (
        <ContentEmptyState
          variant="library"
          title={canManageLibraries ? "为收藏准备一个家" : "还没有可浏览的媒体库"}
          description={
            canManageLibraries
              ? "创建电影库或剧集库，选好根目录后，订阅完成的内容会自动整理到这里。"
              : "当前账号暂时没有可浏览的媒体库，请联系管理员分配媒体库权限。"
          }
          action={
            canManageLibraries ? (
              <Link
                href={"/library/manage?create=1" as Route}
                className="btn-accent flex items-center gap-1 rounded-full py-2 pl-3 pr-4 text-ui font-semibold"
              >
                <PlusIcon className="size-4" />
                创建第一个媒体库
              </Link>
            ) : undefined
          }
        />
      )}

      {/* 全部藏光时不出白页：给一个指回自定义页的空态 */}
      {libraries !== null && libraries.length > 0 && visibleRows.length === 0 && (
        <div
          className="mx-6 mt-16 rounded-2xl border border-dashed border-white/15 px-6 py-8 text-center max-md:mx-4"
          data-testid="home-all-hidden"
        >
          <p className="text-ui font-semibold text-[var(--text)]">首页空空如也</p>
          <p className="mt-1 text-sub text-[var(--text-muted)]">
            所有行都被隐藏了。到「自定义首页」挑几行回来，或恢复默认。
          </p>
          <Link
            href={"/library/customize" as Route}
            className="btn-glass mt-4 inline-flex px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            自定义首页
          </Link>
        </div>
      )}

      {libraries !== null && visibleRows.map(renderRow)}
    </div>
  );
}

/** 一行取数的缓存键：同一个库同一种排序同一个方向（同一个只看没看过的开关）只请求一次。 */
function rowFetchKey(row: HomeRow): string {
  if (row.kind === "library")
    return `lib:${row.library.id}:${row.sort}:${row.reversed}:${row.unwatched}`;
  if (row.kind === "collection") return `col:${row.collection.id}:${row.sort}:${row.reversed}`;
  return row.id;
}

/** 库卡片封面用的那批条目：最近入账的前几部，与默认的「最近添加」行共用一份。
 *  键的形状必须与 rowFetchKey 对库行算出来的一致，否则默认行会多打一次同样的请求。 */
function coverFetchKey(libraryId: number): string {
  return `lib:${libraryId}:added_at:false:false`;
}

/**
 * 显示中的行各自要打的请求，按缓存键去重。排序与截断都交给服务端——早先在这里
 * 拉整库再本地切片，一个几千部的库光这一个请求就是几百 KB。
 */
function rowFetches(
  rows: HomeRow[],
  libraries: MediaLibrary[],
): Map<string, () => Promise<LibraryItem[]>> {
  const fetches = new Map<string, () => Promise<LibraryItem[]>>();
  for (const row of rows) {
    if (row.kind === "library") {
      const { library, sort, reversed, unwatched } = row;
      // 「最近观看」行只要播过的：度量档把没播过的沉底而不是排除，取 20 条时
      // 看过的排完就轮到没播过的，首页这一行不能这样（w=seen）
      const watch = sort === "last_played" ? "seen" : unwatched ? "unwatched" : undefined;
      fetches.set(rowFetchKey(row), () =>
        listLibraryItems(library.id, {
          sort,
          // 反转了自然方向才带 order；不带时服务端按自然方向排，与加方向之前逐字相同
          order: orderParamFor(SORT_PRESETS[sort].direction, reversed),
          limit: RECENT_COUNT,
          filter: watch ? { watch } : undefined,
        }),
      );
    } else if (row.kind === "collection") {
      const { collection, sort, reversed } = row;
      fetches.set(rowFetchKey(row), () =>
        listCollectionItems(collection.id, {
          sort,
          order: orderParamFor(SORT_PRESETS[sort].direction, reversed),
          limit: RECENT_COUNT,
        }),
      );
    } else if (row.kind === "libraries") {
      for (const library of libraries) {
        // 不在自己浏览范围内的库（超管把自己摘掉了）只有管理权：卡片带锁、不拉条目——拉了也是 404
        if (!library.viewer_access) continue;
        const key = coverFetchKey(library.id);
        if (!fetches.has(key)) {
          fetches.set(key, () =>
            listLibraryItems(library.id, { sort: "added_at", limit: RECENT_COUNT }),
          );
        }
      }
    }
  }
  return fetches;
}

/**
 * 库存条目 → 发现页海报卡的数据形态。点击走 /media/{type}/{tmdb_id} 详情
 * （与单库页库存格同一目标）。卡片底部只留片名与年份；本批季集范围和入库
 * 时间进入 hover，不能拿累计库存季集数冒充新增内容。海报不打清晰度徽章。
 */
/** 海报卡的 id：TMDB 条目用 tmdb_id（订阅状态按它对齐），本地条目没有外部 id，
 *  用带前缀的条目 id 占位——只用来当 Map 键与 React key，不会被当成 TMDB id 请求。 */
function libraryItemKey(item: LibraryItem): string {
  return item.tmdb_id != null ? String(item.tmdb_id) : `local:${item.media_item_id}`;
}

function libraryItemToMediaItem(item: LibraryItem): MediaItem {
  const overlayDetails = buildRecentAdditionOverlay(
    item.kind,
    item.recent_addition,
    item.added_at ? `${formatRelativeTime(item.added_at)}入库` : null,
  );
  return {
    id: libraryItemKey(item),
    source: "tmdb",
    // 其他库条目没有发现页类型；卡片只当本地内容展示，不给订阅入口
    type: item.kind === "video" || item.kind === "photo" ? "movie" : item.kind,
    // 首页横滚行卡片规格统一为 2:3 竖框；其他库的横版封面按真实比例居中完整显示
    // （模糊铺底），不按真实比例撑宽卡片——横图等高排会是海报的 2.7 倍宽，整行太大
    imageAspect: item.primary_aspect,
    title: item.title,
    originalTitle: "",
    year: item.year ?? 0,
    rating: 0,
    genres: [],
    extent: "",
    badges: [],
    overview: "",
    overlayDetails,
    // 海报可能是本地刮削资产的相对路径（/images/assets/...），也可能是
    // TMDB 图床绝对地址——统一经 imageUrl 解析（补 API base / 走缓存代理）。
    // 取 poster-card 派生图而非原图：格子实测渲染 150~170 CSS px，328px 的
    // 预设覆盖 2x 屏绰绰有余，而原图是 500px 宽的刮削资产——一屏 60 格直出
    // 原图要 4.9 MB，取派生图只要 1.7 MB（实测单张 82KB → 29KB）。
    // 其他库的横版封面按比例取横卡预设，竖框会把它缩得太小
    posterUrl: imageUrl(item.poster_url, cardVariantFor(item.primary_aspect)),
  };
}

/** 收藏卡：海报卡形态同库存条目，hover 层只说「收藏的是哪一层」（整剧与电影不解释）。 */
function favoriteItemToMediaItem(item: FavoriteItem): MediaItem {
  const level = favoriteLevelLabel(
    item.kind,
    item.favorite_season_number,
    item.favorite_episode_number,
  );
  return {
    ...libraryItemToMediaItem(item),
    overlayDetails: level ? { primary: level } : undefined,
  };
}

/* —— 库卡片：海报货架封面 + 库名/徽标/计数，Emby「我的媒体」磁贴风 —— */

function LibraryCard({ library, items }: { library: MediaLibrary; items: LibraryItem[] }) {
  const meta = LIBRARY_KIND_META[library.kind];
  // 封面海报取最近入库的 4 部（items 已是服务端按最近入账排好的那批）
  const posters = items
    .map((s) => s.poster_url)
    .filter((u): u is string => Boolean(u))
    .slice(0, 4);
  // 扫描/整理/元数据刷新进行中：封面归进度环，其余状态徽标一律让位。
  // 三种长任务在卡片上同一套呈现——用户不该因为"哪种任务"而看不到进度
  const refreshingMeta = Boolean(library.metadata_refresh?.refreshing);
  const busy = library.scanning || library.organizing || refreshingMeta;
  // 写入中暂缓入账的文件数（watchdog 已发现、等拷贝/下载落定后自动补扫入库）
  const importing = busy ? 0 : (library.last_scan?.deferred ?? 0);

  return (
    <div className="group/lib relative">
      <Link
        href={`/library/${library.id}` as Route}
        scroll={false}
        aria-label={`打开「${library.name}」`}
        className="block overflow-hidden rounded-2xl ring-1 ring-white/10 outline-none transition duration-300 hover:ring-white/35 focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
      >
        <div className="relative aspect-[21/10] bg-[#0a0c12]">
          <LibraryCover libraryId={library.id} posters={posters} Icon={meta.Icon} />
          {/* 状态徽标叠在封面左下的倒影暗区：那块本就没有信息、又足够暗
              压得住字；标题行因此永远只有库名，长库名不会被徽标挤没。
              扫描/整理时封面归进度环，徽标让位（否则隔着蒙版透出来像脏渲染） */}
          {!busy && importing > 0 && (
            <div className="absolute inset-x-2.5 bottom-2 flex flex-wrap items-center gap-1.5">
              <span className="flex items-center gap-1.5 rounded-full border border-[var(--info)]/35 bg-black/55 px-2 py-0.5 text-micro font-semibold text-[var(--info)] backdrop-blur-md">
                <span className="size-1.5 animate-pulse rounded-full bg-[var(--info)]" />
                {importing} 个新文件入库中
              </span>
            </div>
          )}
          {busy && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-1 bg-black/55 backdrop-blur-[2px]">
              <ScanProgressRing
                progress={
                  library.scanning
                    ? library.scan_progress
                    : library.organizing
                      ? library.organize_progress
                      : library.metadata_refresh
                }
              />
              {/* 进度环只有百分比，说不清在干什么，补一行状态词。扫描内部
                  分阶段（盘点/入账/补图），阶段变了这里必须跟着变——否则
                  文件扫完后还要下几分钟图片，环停在 100% 配一句"扫描中"，
                  看起来就是卡死了 */}
              <span className="text-caption font-semibold text-white/85">
                {library.scanning
                  ? (SCAN_PHASE_LABELS[library.scan_progress?.phase ?? "ingesting"] ?? "扫描中")
                  : library.organizing
                    ? "整理中"
                    : "刷新元数据"}
              </span>
              {/* 刷新是全量重刷、以分钟计，多给一行"到哪部了"（并发若干路
                  时取第一部即可，完整列表在单库页的面板里） */}
              {refreshingMeta && library.metadata_refresh?.active?.[0] && (
                <span className="max-w-[86%] truncate text-micro text-white/60">
                  {library.metadata_refresh.active[0].title} ·{" "}
                  {library.metadata_refresh.active[0].phase}
                </span>
              )}
            </div>
          )}
        </div>
      </Link>

      {/* 库名：Emby 式放在封面下方居中，只与「默认」共处一行 */}
      <div className="mt-2.5 flex items-center justify-center gap-2 px-2">
        <h3 className="truncate text-body-lg font-semibold text-white">{library.name}</h3>
        {library.is_default && (
          <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.1] px-2 py-0.5 text-micro font-semibold text-white/80">
            默认
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * 封面「氛围光货架」：首张海报重模糊后铺满做氛围光晕（每个库有自己的
 * 色调），最多 4 张海报立体站排，底部倒影直接落在氛围暗底上表达
 * 「反光地面」；0 张=类型图标底纹。卡片 21/10 比例，海报占约 2/3。
 */
/** 扫描进度环：有分母画百分比，刚起步（进度未知）转圈占位。 */
/** 扫描/整理/元数据刷新共用的进度环（三者的进度都是 已处理/总数）。 */
function ScanProgressRing({ progress }: { progress: { processed: number; total: number } | null }) {
  const pct =
    progress && progress.total > 0
      ? Math.min(100, Math.round((progress.processed / progress.total) * 100))
      : null;
  const R = 26;
  const C = 2 * Math.PI * R;
  return (
    <div className="relative size-[72px]">
      <svg
        viewBox="0 0 64 64"
        className={`size-full -rotate-90 ${pct === null ? "animate-spin" : ""}`}
      >
        <circle cx="32" cy="32" r={R} fill="none" stroke="rgba(255,255,255,0.2)" strokeWidth="5" />
        <circle
          cx="32"
          cy="32"
          r={R}
          fill="none"
          stroke="white"
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={pct === null ? C * 0.75 : C * (1 - pct / 100)}
          className="transition-[stroke-dashoffset] duration-500 ease-out"
        />
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-ui font-semibold text-white">
        {pct === null ? "…" : `${pct}%`}
      </span>
    </div>
  );
}

function LibraryCover({
  libraryId,
  posters,
  Icon,
}: {
  libraryId: number;
  posters: string[];
  Icon: typeof FilmIcon;
}) {
  // 服务端渲染的「氛围光货架」拼贴（与 Jellyfin 兼容层给播放器的是同一张图）：
  // 一次 <img> 请求替代 9+ 张图的客户端合成，ETag 协商缓存，渲染显著更快。
  // 拼贴尚未生成/加载失败时回退到原客户端 CSS 货架（素材同源，观感一致）。
  const [collageFailed, setCollageFailed] = useState(false);
  if (posters.length === 0) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-[#1c2230] to-[#10131c]">
        <Icon className="size-12 text-white/[0.13]" />
      </div>
    );
  }
  if (!collageFailed) {
    return (
      <div className="absolute inset-0 overflow-hidden">
        <img
          src={`${publicEnv.apiBaseUrl}/libraries/${libraryId}/cover`}
          alt=""
          loading="lazy"
          className="absolute inset-0 size-full object-cover transition duration-300 group-hover/lib:scale-[1.02]"
          onError={() => setCollageFailed(true)}
        />
        {/* 悬停扫光沿用：一道斜向柔光掠过底部倒影区 */}
        <div className="pointer-events-none absolute -left-[45%] bottom-0 h-[25%] w-[45%] -skew-x-12 bg-gradient-to-r from-transparent via-white/[0.14] to-transparent transition-transform duration-700 ease-out group-hover/lib:translate-x-[350%]" />
      </div>
    );
  }
  return (
    <div className="absolute inset-0 overflow-hidden">
      {/* 氛围光：首图放大重模糊 + 提饱和，再整体压暗保证前景对比度 */}
      <img
        src={imageUrl(posters[0])}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        className="absolute inset-0 size-full scale-150 object-cover opacity-70 blur-3xl saturate-150"
      />
      <div className="absolute inset-0 bg-[#080a10]/50" />
      {/* 灯箱底光：首图模糊后以 screen 混合从底边向上发光，颜色天然
          取自海报主色；再叠一个中性地面光斑，像射灯打在舞台地面上 */}
      <img
        src={imageUrl(posters[0])}
        alt=""
        aria-hidden
        loading="lazy"
        referrerPolicy="no-referrer"
        className="absolute inset-x-0 bottom-0 h-1/2 w-full object-cover opacity-55 blur-3xl saturate-150 mix-blend-screen [mask-image:linear-gradient(to_top,black,transparent)]"
      />
      <div className="absolute inset-x-[8%] bottom-0 h-[28%] [background:radial-gradient(60%_100%_at_50%_100%,rgba(255,255,255,0.09),transparent_70%)]" />
      {/* 海报排：立在玻璃搁板上，悬停整排轻微上浮 */}
      <div className="absolute inset-x-0 top-[4.5%] flex justify-center gap-[2%] px-[2%]">
        {posters.map((url, i) => (
          <div
            key={i}
            className="w-[22.5%] shrink-0 transition duration-300 group-hover/lib:-translate-y-1"
          >
            <img
              src={imageUrl(url)}
              alt=""
              loading="lazy"
              referrerPolicy="no-referrer"
              className="aspect-[2/3] w-full rounded-[4px] object-cover shadow-[0_6px_18px_rgba(0,0,0,0.5)] ring-1 ring-white/20"
            />
            {/* 倒影：翻转副本贴着底边，向下快速渐隐。注意 mask 在元素本地
                坐标系生效、会跟着 scaleY(-1) 一起翻转，所以这里写 to top，
                翻转后在屏幕上才是「贴近海报处最实、向下淡出」 */}
            <img
              src={imageUrl(url)}
              alt=""
              aria-hidden
              loading="lazy"
              referrerPolicy="no-referrer"
              className="mt-[2px] aspect-[2/3] w-full -scale-y-100 rounded-[4px] object-cover opacity-55 blur-[1px] [mask-image:linear-gradient(to_top,rgba(0,0,0,0.7),transparent_26%)]"
            />
          </div>
        ))}
      </div>
      {/* 悬停扫光：一道斜向柔光从左扫到右掠过倒影区（transform 过渡
          实现单次扫过，移出卡片后自动滑回原位待命） */}
      <div className="pointer-events-none absolute -left-[45%] bottom-0 h-[25%] w-[45%] -skew-x-12 bg-gradient-to-r from-transparent via-white/[0.14] to-transparent transition-transform duration-700 ease-out group-hover/lib:translate-x-[350%]" />
    </div>
  );
}
