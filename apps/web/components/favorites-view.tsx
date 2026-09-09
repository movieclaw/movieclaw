"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";

import { useToast } from "@/components/feedback";
import { MasonryIcon, MoreIcon, PosterGridIcon } from "@/components/icons";
import { WallLoadMore, WallLoadPrev, WallRecallPill } from "@/components/wall-chrome";
import { PosterWall } from "@/components/poster-wall";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { remeasureWalls, usePhotoWallDensity } from "@/components/photo-wall";
import {
  GALLERY_LOAD_MARGIN,
  GALLERY_PAGE_SIZE,
  VideoGalleryLightbox,
  VideoGalleryWall,
  WallPrefItems,
  dedupeGalleryGroups,
  flattenGallery,
  useVideoGalleryGrouped,
  useVideoGalleryMode,
} from "@/components/video-gallery";
import type { LibraryGalleryGroup } from "@/lib/api/libraries";
import {
  type FavoriteItem,
  listFavorites,
  listFavoritesGallery,
  setPlaybackMarks,
} from "@/lib/api/playback";
import {
  firstVisibleAnchorId,
  isReentryAfterAbsence,
  wallRecallScope,
} from "@/lib/library-wall-recall";
import { usePageTitle } from "@/lib/use-page-title";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";
import { useWallRecall } from "@/lib/use-wall-recall";

/** 每次向服务端要的格数，与单库海报墙同一页长。 */
const PAGE_SIZE = 60;

/** 「回到上次浏览的位置」的记录键（lib/library-wall-recall.ts）。 */
const RECALL_SCOPE = wallRecallScope("favorites");

/**
 * 记录的形态口径（见 WallRecall.view）。
 *
 * 收藏页只有一种排序（最近收藏在前），海报墙与图廊又是同一份名单、同一种按
 * 作品分页——「第 300 个」在两种形态里指向同一部作品，因此共用一条记录：在
 * 图廊里看到哪，切回海报墙照样跳得回去。单库页两种墙的排序不同，那边才必须
 * 分开记。
 */
const RECALL_VIEW = "favorites";

/**
 * 「全部收藏」页离开再返回时的会话快照。
 *
 * 与单库页的 lib/library-detail-snapshot.ts 是同一件事：这面墙与作品详情页是
 * 两个路由，Next 切走时组件会被卸载。返回时若只补第一页，容器就矮到装不下
 * 离开时的滚动位置，lib/use-scroll-restoration.ts 既找不到锚点、又等不到足够
 * 的高度，超时后只能放弃——人被甩回墙首。这正是「收藏页没有滚动记忆、和别的
 * 媒体库不一样」的由来（单库页早已有快照，收藏页一直没有）。
 *
 * 一个账号只有一面收藏墙，一份模块级快照就够，不必像单库页那样按 id 存 Map。
 * 它只活在当前浏览会话里（刷新页面即失效），与滚动位置同一口径。
 */
interface FavoritesSnapshot {
  /** 海报墙已加载的整个窗口 */
  items: FavoriteItem[];
  total: number;
  /** 这份窗口在整份名单里的起点（「回到上次位置」跳过来后不为 0） */
  wallStart: number;
  /** 图床浏览模式已加载的整个窗口；空数组 = 这一次浏览没进过图廊 */
  galleryGroups: LibraryGalleryGroup[];
  galleryHasMore: boolean;
  /** 图廊窗口的起点，与 wallStart 同一个意思 */
  galleryStart: number;
  /** 已向服务端请求到第几个作品（绝对位置；没图的作品也占一组） */
  galleryLoaded: number;
}

let snapshot: FavoritesSnapshot | null = null;

/** 本页 ⋯ 菜单的行样式（与全站各处菜单同一副长相）。 */
const ITEM_CLASS =
  "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
  "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)]";

/**
 * 整面墙钉死的框比例：2:3 竖版海报。
 *
 * 收藏是跨库的一面墙，横版封面（其他库的 16:9 抓帧）和竖版海报必然混在一起。
 * 单库页遇到这种情况会把墙切成竖横两区各自对齐，但收藏页排的是「最近收藏的
 * 在前」——按比例分区等于把收藏顺序打散，人再也找不到刚点的那部。
 *
 * 所以这里照搬最近观看那一行的做法：**框比例固定，主图按真实比例居中完整显示、
 * 同图放大模糊铺底**（PosterCardVisual 的 letterbox 分支）。每格等高、片名落在
 * 一条线上，横版封面也不会被裁掉两边。首页横滚的「我的收藏」本来就是这么排的，
 * 点「查看全部」进来的这面墙从此与它同一形态。
 */
const FAVORITES_FRAME_ASPECT = 2 / 3;

/**
 * 「全部收藏」页（/library/favorites）：当前账号收藏的全部作品。数据是
 * playback_state 里的收藏列，与 Jellyfin 客户端点的心同一份；每格的详情落点
 * 是服务端解析好的可见库。首页只横滚最近 20 部，多的到这里看。
 *
 * 与单库页一样有两种浏览形态，顶栏那颗键来回切（偏好与单库页共用一份，
 * 见 useVideoGalleryMode）：
 *   - 海报墙：同一套格子（InventoryCell）、同一种滚动加载，区别只在框比例
 *     统一钉死（见 FAVORITES_FRAME_ASPECT）；
 *   - 图床浏览：同一套瀑布流与灯箱（video-gallery），数据换成
 *     /playback/favorites/gallery——收藏作品的海报 / 剧照 / 章节场景图。
 *
 * 两种形态各自分页、互不干扰；海报墙那一份始终会拉（页头的总数与切回来时的
 * 首屏都靠它），与单库页同一处理。
 *
 * 位置记忆也与单库页同一套，两层各管一段（谁都不覆盖谁）：
 *   - 会话内：从作品详情页返回时，快照把整个已加载窗口接回来，滚动位置由
 *     lib/use-scroll-restoration.ts 自动回位，不问用户；
 *   - 跨会话：关掉页面第二天再进来，底部弹一枚胶囊问要不要回到上次的位置
 *     （lib/library-wall-recall.ts），点了就把窗口整体换到那一段。
 */
export function FavoritesView() {
  usePageTitle("我的收藏");
  const toast = useToast();
  const initialSnapshot = snapshot;
  // 本次是不是「重新进入」：首帧没有会话快照 = 冷启动 / 刷新 / 从别处进来的；
  // 或者本次页面加载期间挂过很久后台（iOS PWA 恢复应用不重新加载页面，只能
  // 靠这条认）。重新进入时不自动回位，改由底部胶囊来问——两者同时来的话，
  // 人已经在原处了，胶囊就成了指着脚下的废话
  const freshEntry = useRef(initialSnapshot === null || isReentryAfterAbsence(RECALL_SCOPE));
  const [galleryPreferred, setGalleryMode] = useVideoGalleryMode();
  const [galleryGrouped, setGalleryGrouped] = useVideoGalleryGrouped();
  const [density, setDensity] = usePhotoWallDensity();
  // 两种墙的滚动锚点不同：海报墙一格一个条目，图廊一部作品十几张图按瓦片认
  const restoreScrollRef = useScrollRestoration("library:favorites", {
    anchorAttribute: galleryPreferred ? "data-gallery-tile-id" : "data-library-item-id",
    restore: !freshEntry.current,
  });
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);
  // 滚动恢复、位置记录与前置加载的滚动补偿共用同一个真实滚动容器，合并
  // callback ref，免得几套监听器各自去猜 window/document
  const scrollRef = useCallback(
    (node: HTMLDivElement | null) => {
      restoreScrollRef(node);
      setScrollElement(node);
    },
    [restoreScrollRef],
  );
  // 首帧就把上次的窗口接回来，滚动恢复才有落脚的高度
  const [items, setItems] = useState<FavoriteItem[] | null>(initialSnapshot?.items ?? null);
  const [total, setTotal] = useState(initialSnapshot?.total ?? 0);
  // 当前窗口在整份名单里的起点：0 = 墙首；「回到上次位置」跳过来后不为 0
  const [wallStart, setWallStart] = useState(initialSnapshot?.wallStart ?? 0);
  const [failed, setFailed] = useState(false);
  // 起点的 ref 版：几个回调每次都读最新值，不必为它重建回调链
  const wallOffset = useRef(initialSnapshot?.wallStart ?? 0);
  // 翻页请求进行中：哨兵重新观察时不重复发同一页
  const loading = useRef(false);
  // 进这一屏时快照里有多少格（挂载后本组件自己就会改写 snapshot，得先定格）
  const restoredCount = useRef(initialSnapshot?.items.length ?? 0);
  // 海报墙顶部的锚：跳转后滚回墙首，位置记录也按它量首个可见格
  const wallTop = useRef<HTMLDivElement>(null);

  /** 墙尾追加一页。 */
  const load = useCallback(async (offset: number) => {
    if (loading.current) return;
    loading.current = true;
    try {
      const page = await listFavorites(PAGE_SIZE, offset);
      setTotal(page.total);
      setItems((prev) => {
        if (!prev) return page.items;
        // 与前置加载/跳转交叠着回来时同一部可能拿到两次，按 id 去重再拼
        const seen = new Set(prev.map((i) => i.media_item_id));
        return [...prev, ...page.items.filter((i) => !seen.has(i.media_item_id))];
      });
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      loading.current = false;
    }
  }, []);

  /**
   * 按已加载的页数重拉整个窗口并整体替换：快照恢复出来的是离开这一屏时的旧
   * 数据，在详情页取消了收藏的那部要跟着消失。不缩窗口、不动滚动位置；请求
   * 失败就留着旧窗口，下次返回再对账。
   */
  const reload = useCallback(async (loadedCount: number) => {
    if (loading.current) return;
    loading.current = true;
    const start = wallOffset.current;
    try {
      const pages = await Promise.all(
        Array.from({ length: Math.ceil(loadedCount / PAGE_SIZE) }, (_, page) =>
          listFavorites(PAGE_SIZE, start + page * PAGE_SIZE),
        ),
      );
      const rows = pages.flatMap((page) => page.items);
      if (rows.length === 0 && start > 0) {
        // 这一段整个被取消收藏了（跳到过靠后的位置，回来时那里已经空了）：
        // 退回墙首重取，否则明明还有收藏，这一屏却什么都没有
        wallOffset.current = 0;
        setWallStart(0);
        const head = await listFavorites(PAGE_SIZE, 0);
        setTotal(head.total);
        setItems(head.items);
      } else {
        setTotal(pages[0].total);
        setItems(rows);
      }
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      loading.current = false;
    }
  }, []);

  useEffect(() => {
    if (restoredCount.current > 0) void reload(restoredCount.current);
    else void load(0);
  }, [load, reload]);

  const loaded = items?.length ?? 0;
  const hasMore = items !== null && wallStart + loaded < total;
  /** 收藏是跨库的一面墙，每一格落回它自己所属的库 */
  const ownerLibraryOf = useCallback((item: FavoriteItem) => item.library_id, []);
  const loadMore = useCallback(() => {
    void load(wallStart + loaded);
  }, [load, loaded, wallStart]);

  /* —— 向上补页 ——
     「回到上次位置」会把整个窗口换成从那一段开始的一页，窗口起点因此不为 0：
     上方明明还有作品，往上滑却是一堵墙。这里补上反方向的分页——墙顶的哨兵
     提前 600px 触发，把上一页接到已加载内容前面（与单库页同一条路）。 */
  const loadingPrev = useRef(false);
  // 前置加载前滚动容器的高度；null = 本次 items 变化不是前置加载，不必补偿
  const prependFrom = useRef<number | null>(null);
  const loadPrev = useCallback(() => {
    if (loadingPrev.current) return;
    const until = wallOffset.current;
    if (until <= 0) return; // 已经到墙首，上方没有东西可补
    loadingPrev.current = true;
    const from = Math.max(0, until - PAGE_SIZE);
    listFavorites(until - from, from)
      .then((page) => {
        // 一部都没拿到（这一段刚好被取消收藏取空了）：起点保持不动就此打住，
        // 否则起点一路往前挪、哨兵每次都重新观察，会把这段空区间反复请求
        if (page.items.length === 0) return;
        // 起点按**实拿到的条数**回推：拿少了（并发取消收藏）时窗口起点仍与
        // 已加载内容对得上，「上次位置」才不会整体错位
        const start = until - page.items.length;
        wallOffset.current = start;
        prependFrom.current = scrollElement?.scrollHeight ?? null;
        setTotal(page.total);
        setWallStart(start);
        setItems((current) => {
          const seen = new Set((current ?? []).map((i) => i.media_item_id));
          return [...page.items.filter((i) => !seen.has(i.media_item_id)), ...(current ?? [])];
        });
      })
      // 失败就先不补：哨兵还在墙顶，用户下次滑离再滑回来会重新触发
      .catch(() => {})
      .finally(() => {
        loadingPrev.current = false;
      });
  }, [scrollElement]);

  // 前置加载的滚动补偿：墙长高了多少就把 scrollTop 加回多少。放 layout effect
  // 里在绘制前完成；补完立刻让虚拟化窗口重量一次——滚动事件要到下一帧才来，
  // 不补这一下会闪一帧空墙
  useLayoutEffect(() => {
    const before = prependFrom.current;
    prependFrom.current = null;
    if (before === null || !scrollElement) return;
    const grown = scrollElement.scrollHeight - before;
    if (grown <= 0) return;
    scrollElement.scrollTop += grown;
    remeasureWalls();
  }, [items, scrollElement]);

  /**
   * 跳到整份名单里的某个位置：换掉整个窗口（而不是从头一页页追加过去），
   * 此后向下照常滚动加载，向上由墙顶哨兵把上文补回来（loadPrev）。
   */
  const jumpTo = useCallback((offset: number) => {
    // 跳转期间两头的哨兵都挡住，别让旧窗口的追加/补页插进来
    loading.current = true;
    loadingPrev.current = true;
    wallOffset.current = offset;
    listFavorites(PAGE_SIZE, offset)
      .then((page) => {
        setTotal(page.total);
        setWallStart(offset);
        setItems(page.items);
        // 瞬时而不是平滑：墙上的内容已经整段换掉，平滑滚过去的是一堆不存在的
        // 旧内容；落地后墙顶哨兵还会立刻补一页，那一下的滚动补偿会打断动画
        wallTop.current?.scrollIntoView({ block: "start", behavior: "instant" });
      })
      .catch(() => {})
      .finally(() => {
        loading.current = false;
        loadingPrev.current = false;
      });
  }, []);

  // —— 图床浏览模式 —— //
  const [galleryGroups, setGalleryGroups] = useState<LibraryGalleryGroup[]>(
    initialSnapshot?.galleryGroups ?? [],
  );
  const [galleryHasMore, setGalleryHasMore] = useState(initialSnapshot?.galleryHasMore ?? false);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  // 这份图廊窗口的起点（与海报墙的 wallStart 同一个意思）
  const galleryStart = useRef(initialSnapshot?.galleryStart ?? 0);
  // 已请求到的作品数（绝对位置，按页长推进，不按拿到的组数——没图的作品也占一组）
  const galleryLoaded = useRef(initialSnapshot?.galleryLoaded ?? 0);
  const galleryLoading = useRef(false);
  const galleryEntries = useMemo(() => flattenGallery(galleryGroups), [galleryGroups]);

  const loadMoreGallery = useCallback(() => {
    if (galleryLoading.current) return;
    galleryLoading.current = true;
    const offset = galleryLoaded.current;
    listFavoritesGallery({ limit: GALLERY_PAGE_SIZE, offset })
      .then((page) => {
        galleryLoaded.current = offset + page.length;
        setGalleryGroups((current) => dedupeGalleryGroups([...current, ...page]));
        setGalleryHasMore(page.length >= GALLERY_PAGE_SIZE);
      })
      .catch(() => setGalleryHasMore(false))
      .finally(() => {
        galleryLoading.current = false;
      });
  }, []);

  /** 图廊窗口的对账，与海报墙的 reload 同一个意思（整窗重拉、整体替换）。 */
  const refreshGallery = useCallback(() => {
    const start = galleryStart.current;
    const loadedGroups = galleryLoaded.current - start;
    if (loadedGroups <= 0 || galleryLoading.current) return;
    galleryLoading.current = true; // 对账期间别让滚动哨兵同时追加下一页
    Promise.all(
      Array.from({ length: Math.ceil(loadedGroups / GALLERY_PAGE_SIZE) }, (_, page) =>
        listFavoritesGallery({
          limit: GALLERY_PAGE_SIZE,
          offset: start + page * GALLERY_PAGE_SIZE,
        }),
      ),
    )
      .then((pages) => {
        galleryLoaded.current = start + pages.reduce((sum, page) => sum + page.length, 0);
        setGalleryGroups(dedupeGalleryGroups(pages.flat()));
        setGalleryHasMore((pages.at(-1)?.length ?? 0) >= GALLERY_PAGE_SIZE);
      })
      .catch(() => undefined)
      .finally(() => {
        galleryLoading.current = false;
      });
  }, []);

  /** 图廊跳到某个位置：与海报墙的 jumpTo 是同一件事——换掉整个窗口。 */
  const jumpGalleryTo = useCallback((offset: number) => {
    galleryLoading.current = true; // 跳转期间挡住滚动哨兵与对账，别让旧窗口的页插进来
    galleryStart.current = offset;
    listFavoritesGallery({ limit: GALLERY_PAGE_SIZE, offset })
      .then((page) => {
        galleryLoaded.current = offset + page.length;
        setGalleryGroups(dedupeGalleryGroups(page));
        setGalleryHasMore(page.length >= GALLERY_PAGE_SIZE);
        wallTop.current?.scrollIntoView({ block: "start", behavior: "smooth" });
      })
      .catch(() => {})
      .finally(() => {
        galleryLoading.current = false;
      });
  }, []);

  // 切进图廊（或进页面时偏好就在图廊）：第一次从头拉一页；快照带回窗口时
  // **不能**清空重拉——只补第一页的话容器矮到装不下离开时的滚动位置，人被
  // 甩回墙首（与单库页同一处理），改成按已加载的页数整窗对账。
  // 海报墙那一份照常自己拉，切回去时首屏已经在手上
  useEffect(() => {
    if (!galleryPreferred) return;
    if (galleryLoaded.current > 0) refreshGallery();
    else loadMoreGallery();
  }, [galleryPreferred, loadMoreGallery, refreshGallery]);

  /**
   * 灯箱里点心：取消收藏后**不把瓦片抽走**——正在看的这张图连同它所在的那一段
   * 会当场消失，翻页下标全乱。墙上的心跟着灭掉即可，下次进这一页时它自然不在了。
   */
  const toggleGalleryFavorite = useCallback(
    async (mediaItemId: number, next: boolean) => {
      const patch = (value: boolean) =>
        setGalleryGroups((current) =>
          current.map((g) => (g.media_item_id === mediaItemId ? { ...g, is_favorite: value } : g)),
        );
      patch(next);
      try {
        const marks = await setPlaybackMarks({ media_item_id: mediaItemId }, { favorite: next });
        patch(marks.is_favorite);
      } catch (e) {
        patch(!next);
        toast.error(e instanceof Error ? e.message : "收藏失败，请稍后重试");
      }
    },
    [toast],
  );

  // 布局提交后就更新快照，路由切换的下一棵树可以在首帧直接读到它；只在卸载
  // 时写是来不及的——新路由的首次 render 可能早于被动 effect 的 cleanup
  useLayoutEffect(() => {
    if (items === null) return;
    snapshot = {
      items,
      total,
      wallStart,
      galleryGroups,
      galleryHasMore,
      galleryStart: galleryStart.current,
      galleryLoaded: galleryLoaded.current,
    };
  }, [galleryGroups, galleryHasMore, items, total, wallStart]);

  // 收藏一部都没有时不给切换键：两种形态都是空页，多一颗键只会让人以为点了没反应
  const empty = items !== null && items.length === 0;
  const gallery = galleryPreferred && !empty;

  /* —— 「回到上次浏览的位置」（lib/library-wall-recall.ts）——
     收藏攒到几百部之后，滑到第几十屏是常态，关掉页面第二天再进来又从墙首
     开始。底部弹一枚胶囊问一句：要跳回去点它，不要就继续滑——滑够一屏胶囊
     自己让位，从那一刻起记的是新位置。会话内从详情页返回不弹（滚动恢复已经
     自动回位）。 */
  // 作品 id → 它在整份名单里的绝对位置。滚动时按首个可见格反查（图廊按瓦片
  // 所属的作品），DOM 上只挂 id，不必给每一格再算一遍下标
  const offsetById = useMemo(
    () =>
      new Map((items ?? []).map((item, index) => [String(item.media_item_id), wallStart + index])),
    [items, wallStart],
  );
  // galleryStart 只在窗口被整体换掉时变，与 galleryGroups 同一时刻，不必进依赖
  const galleryOffsetById = useMemo(
    () =>
      new Map(
        galleryGroups.map((group, index) => [
          String(group.media_item_id),
          galleryStart.current + index,
        ]),
      ),
    [galleryGroups],
  );
  const wallOffsetAt = useCallback(() => {
    const wall = wallTop.current;
    if (!wall || !scrollElement) return null;
    const id = firstVisibleAnchorId(
      wall,
      gallery ? "data-gallery-item-id" : "data-library-item-id",
      scrollElement.getBoundingClientRect().top,
    );
    return id === null ? null : ((gallery ? galleryOffsetById : offsetById).get(id) ?? null);
  }, [gallery, galleryOffsetById, offsetById, scrollElement]);
  // 久别回归时把这一屏复位：页面没重新加载，人还停在离开时的位置上，不回到
  // 顶部的话胶囊指着的就是脚下这一格。瞬时而不是平滑——这是「重新进入」，
  // 不是一次导航，几万像素的平滑动画只会让人以为页面失控了
  const resetWallToTop = useCallback(() => {
    scrollElement?.scrollTo({ top: 0, behavior: "instant" });
  }, [scrollElement]);
  const { recallOffset, dismissRecall } = useWallRecall({
    scope: RECALL_SCOPE,
    view: RECALL_VIEW,
    scroller: scrollElement,
    enabled: gallery ? galleryGroups.length > 0 : loaded > 0,
    offer: freshEntry.current,
    offsetAt: wallOffsetAt,
    onReenter: resetWallToTop,
  });
  // 记录可能指向已经不存在的位置（收藏被大批取消），跳过去只会是一面空墙
  const recallable = recallOffset !== null && recallOffset < total ? recallOffset : null;

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <PageNav
        title="我的收藏"
        fallback={{ label: "媒体库", href: "/library" as Route }}
        actions={
          empty ? undefined : (
            <>
              {/* 与单库页顶栏同一颗键、同一副图标：画的是**点过去会变成的那面墙** */}
              <button
                type="button"
                title={gallery ? "回到海报墙" : "图床浏览"}
                aria-label={gallery ? "回到海报墙" : "图床浏览"}
                aria-pressed={gallery}
                onClick={() => {
                  // 两种模式的灯箱翻的不是同一份列表，下标不能沿用
                  setLightboxIndex(null);
                  setGalleryMode(!gallery);
                }}
                className={`${PAGE_NAV_BUTTON_CLASS} ${gallery ? "bg-black/55 text-white" : ""}`}
              >
                {gallery ? (
                  <PosterGridIcon className="size-[18px] max-md:size-[22px]" />
                ) : (
                  <MasonryIcon className="size-[18px] max-md:size-[22px]" />
                )}
              </button>
              {/* 看图的两个偏好；海报墙上没有可调的，整个菜单也就不出现 */}
              {gallery && (
                <DropdownMenu.Root>
                  <DropdownMenu.Trigger asChild>
                    <button
                      type="button"
                      aria-label="浏览设置"
                      className={`${PAGE_NAV_BUTTON_CLASS} data-[state=open]:bg-black/55 data-[state=open]:text-white`}
                    >
                      <MoreIcon className="size-[18px] max-md:size-[22px]" />
                    </button>
                  </DropdownMenu.Trigger>
                  <DropdownMenu.Portal>
                    <DropdownMenu.Content
                      align="end"
                      sideOffset={6}
                      collisionPadding={12}
                      className="menu-surface z-50 min-w-[11rem] p-1"
                    >
                      <WallPrefItems
                        grouped={galleryGrouped}
                        onGroupedChange={setGalleryGrouped}
                        density={density}
                        onDensityChange={setDensity}
                        itemClass={ITEM_CLASS}
                      />
                    </DropdownMenu.Content>
                  </DropdownMenu.Portal>
                </DropdownMenu.Root>
              )}
            </>
          )
        }
      />
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
            {/* overflow-anchor:none：向上补页后墙会长高，浏览器自带的滚动锚定
                会跟着自己补一次 scrollTop，与我们按长高量做的补偿叠加就是跳两下
                （何况 Safari 根本没有滚动锚定）。这一段的位置全部自己算 */}
            <div ref={wallTop} className="mt-6 [overflow-anchor:none]">
              {/* 墙顶还有上文时向上补页：跳「回到上次位置」之后仍然能往上滑 */}
              {!gallery && <WallLoadPrev start={wallStart} onReach={loadPrev} />}
              {gallery ? (
                <VideoGalleryWall
                  groups={galleryGroups}
                  density={density}
                  grouped={galleryGrouped}
                  onOpen={setLightboxIndex}
                />
              ) : (
                <PosterWall
                  items={items}
                  libraryIdOf={ownerLibraryOf}
                  wide={false}
                  frameAspect={FAVORITES_FRAME_ASPECT}
                />
              )}
            </div>
            {/* 两种形态各自分页，哨兵按当前模式接线（图廊按作品数计） */}
            <WallLoadMore
              hasMore={gallery ? galleryHasMore : hasMore}
              loaded={gallery ? galleryLoaded.current - galleryStart.current : loaded}
              start={gallery ? galleryStart.current : wallStart}
              total={total}
              onReach={gallery ? loadMoreGallery : loadMore}
              rootMargin={gallery ? GALLERY_LOAD_MARGIN : undefined}
            />
          </>
        )}
      </div>
      {/* 上次滑到哪：进来时问一句要不要跳回去，不理它、往下滑一屏就自己消失 */}
      {recallable !== null && (
        <WallRecallPill
          onJump={() => {
            dismissRecall();
            if (gallery) jumpGalleryTo(recallable);
            else jumpTo(recallable);
          }}
          onDismiss={dismissRecall}
        />
      )}
      {gallery && lightboxIndex !== null && galleryEntries[lightboxIndex] && (
        <VideoGalleryLightbox
          entries={galleryEntries}
          index={lightboxIndex}
          hasMore={galleryHasMore}
          onIndexChange={setLightboxIndex}
          onReachEnd={loadMoreGallery}
          onToggleFavorite={toggleGalleryFavorite}
          onClose={() => setLightboxIndex(null)}
        />
      )}
    </div>
  );
}
