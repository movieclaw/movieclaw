"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";

import { useToast } from "@/components/feedback";
import { MasonryIcon, MoreIcon, PosterGridIcon } from "@/components/icons";
import { WallLoadMore } from "@/components/library-detail-view";
import { PosterWall } from "@/components/poster-wall";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { usePhotoWallDensity } from "@/components/photo-wall";
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
import { usePageTitle } from "@/lib/use-page-title";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/** 每次向服务端要的格数，与单库海报墙同一页长。 */
const PAGE_SIZE = 60;

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
  /** 海报墙已加载的整个窗口（收藏墙不支持跳转，起点恒为 0） */
  items: FavoriteItem[];
  total: number;
  /** 图床浏览模式已加载的整个窗口；空数组 = 这一次浏览没进过图廊 */
  galleryGroups: LibraryGalleryGroup[];
  galleryHasMore: boolean;
  /** 已向服务端请求到第几个作品（图廊按作品分页，没图的作品也占一组） */
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
 */
export function FavoritesView() {
  usePageTitle("我的收藏");
  const toast = useToast();
  const [galleryPreferred, setGalleryMode] = useVideoGalleryMode();
  const [galleryGrouped, setGalleryGrouped] = useVideoGalleryGrouped();
  const [density, setDensity] = usePhotoWallDensity();
  // 两种墙的滚动锚点不同：海报墙一格一个条目，图廊一部作品十几张图按瓦片认
  const scrollRef = useScrollRestoration("library:favorites", {
    anchorAttribute: galleryPreferred ? "data-gallery-tile-id" : "data-library-item-id",
  });
  // 首帧就把上次的窗口接回来，滚动恢复才有落脚的高度
  const [items, setItems] = useState<FavoriteItem[] | null>(snapshot?.items ?? null);
  const [total, setTotal] = useState(snapshot?.total ?? 0);
  const [failed, setFailed] = useState(false);
  // 翻页请求进行中：哨兵重新观察时不重复发同一页
  const loading = useRef(false);
  // 进这一屏时快照里有多少格（挂载后本组件自己就会改写 snapshot，得先定格）
  const restoredCount = useRef(snapshot?.items.length ?? 0);

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

  /**
   * 按已加载的页数重拉整个窗口并整体替换：快照恢复出来的是离开这一屏时的旧
   * 数据，在详情页取消了收藏的那部要跟着消失。不缩窗口、不动滚动位置；请求
   * 失败就留着旧窗口，下次返回再对账。
   */
  const reload = useCallback(async (loadedCount: number) => {
    if (loading.current) return;
    loading.current = true;
    try {
      const pages = await Promise.all(
        Array.from({ length: Math.ceil(loadedCount / PAGE_SIZE) }, (_, page) =>
          listFavorites(PAGE_SIZE, page * PAGE_SIZE),
        ),
      );
      setTotal(pages[0].total);
      setItems(pages.flatMap((page) => page.items));
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
  const hasMore = items !== null && loaded < total;
  /** 收藏是跨库的一面墙，每一格落回它自己所属的库 */
  const ownerLibraryOf = useCallback((item: FavoriteItem) => item.library_id, []);
  const loadMore = useCallback(() => {
    void load(loaded);
  }, [load, loaded]);

  // —— 图床浏览模式 —— //
  const [galleryGroups, setGalleryGroups] = useState<LibraryGalleryGroup[]>(
    snapshot?.galleryGroups ?? [],
  );
  const [galleryHasMore, setGalleryHasMore] = useState(snapshot?.galleryHasMore ?? false);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  // 已请求到的作品数（按页长推进，不按拿到的组数——没图的作品也占一组）
  const galleryLoaded = useRef(snapshot?.galleryLoaded ?? 0);
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
    const loaded = galleryLoaded.current;
    if (loaded <= 0 || galleryLoading.current) return;
    galleryLoading.current = true; // 对账期间别让滚动哨兵同时追加下一页
    Promise.all(
      Array.from({ length: Math.ceil(loaded / GALLERY_PAGE_SIZE) }, (_, page) =>
        listFavoritesGallery({ limit: GALLERY_PAGE_SIZE, offset: page * GALLERY_PAGE_SIZE }),
      ),
    )
      .then((pages) => {
        galleryLoaded.current = pages.reduce((sum, page) => sum + page.length, 0);
        setGalleryGroups(dedupeGalleryGroups(pages.flat()));
        setGalleryHasMore((pages.at(-1)?.length ?? 0) >= GALLERY_PAGE_SIZE);
      })
      .catch(() => undefined)
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
      galleryGroups,
      galleryHasMore,
      galleryLoaded: galleryLoaded.current,
    };
  }, [galleryGroups, galleryHasMore, items, total]);

  // 收藏一部都没有时不给切换键：两种形态都是空页，多一颗键只会让人以为点了没反应
  const empty = items !== null && items.length === 0;
  const gallery = galleryPreferred && !empty;

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
            {gallery ? (
              <div className="mt-6">
                <VideoGalleryWall
                  groups={galleryGroups}
                  density={density}
                  grouped={galleryGrouped}
                  onOpen={setLightboxIndex}
                />
              </div>
            ) : (
              <div className="mt-6">
                <PosterWall
                  items={items}
                  libraryIdOf={ownerLibraryOf}
                  wide={false}
                  frameAspect={FAVORITES_FRAME_ASPECT}
                />
              </div>
            )}
            {/* 两种形态各自分页，哨兵按当前模式接线（图廊按作品数计） */}
            <WallLoadMore
              hasMore={gallery ? galleryHasMore : hasMore}
              loaded={gallery ? galleryLoaded.current : loaded}
              start={0}
              total={total}
              onReach={gallery ? loadMoreGallery : loadMore}
              rootMargin={gallery ? GALLERY_LOAD_MARGIN : undefined}
            />
          </>
        )}
      </div>
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
