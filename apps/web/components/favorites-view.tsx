"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";

import { useToast } from "@/components/feedback";
import { MasonryIcon, MoreIcon, PosterGridIcon } from "@/components/icons";
import { InventoryCell, WALL_GRID_POSTER, WallLoadMore } from "@/components/library-detail-view";
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

  // —— 图床浏览模式 —— //
  const [galleryGroups, setGalleryGroups] = useState<LibraryGalleryGroup[]>([]);
  const [galleryHasMore, setGalleryHasMore] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  // 已请求到的作品数（按页长推进，不按拿到的组数——没图的作品也占一组）
  const galleryLoaded = useRef(0);
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

  // 切进图廊（或进页面时偏好就在图廊）：拉第一页。海报墙那一份照常自己拉，
  // 切回去时首屏已经在手上
  useEffect(() => {
    if (!galleryPreferred || galleryLoaded.current > 0) return;
    loadMoreGallery();
  }, [galleryPreferred, loadMoreGallery]);

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
              <div className={`mt-6 ${WALL_GRID_POSTER}`}>
                {items.map((item) => (
                  <InventoryCell
                    key={item.media_item_id}
                    item={item}
                    libraryId={item.library_id}
                    frameAspect={FAVORITES_FRAME_ASPECT}
                  />
                ))}
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
