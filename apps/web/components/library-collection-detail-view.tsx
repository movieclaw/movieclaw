"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";

import { CollectionOrderPanel } from "@/components/collection-order-panel";
import { ShareDialog } from "@/components/share-dialog";
import { useConfirm, usePrompt, useToast } from "@/components/feedback";
import { MasonryIcon, MoreIcon, PosterGridIcon } from "@/components/icons";
import { WallSortControl } from "@/components/library-filter-bar";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { usePhotoWallDensity } from "@/components/photo-wall";
import { PosterCard } from "@/components/poster-card";
import {
  InventoryCell,
  PosterWall,
  WALL_GRID_POSTER,
} from "@/components/poster-wall";
import { useSubscribeEntry } from "@/components/subscribe-entry";
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
import { WallLoadMore } from "@/components/wall-chrome";
import {
  applyCollectionToLibrary,
  deleteCollection,
  getCollection,
  getCollectionSeries,
  listCollectionGallery,
  listCollectionItems,
  listCollections,
  updateCollection,
  type Collection,
  type CollectionSeries,
  type CollectionSortParams,
  type SeriesPart,
} from "@/lib/api/collections";
import { getCollectionShare, type ShareView } from "@/lib/api/shares";
import {
  getLibraryFacets,
  type LibraryFacets,
  type LibraryGalleryGroup,
  type LibraryItem,
  listLibraries,
} from "@/lib/api/libraries";
import { setPlaybackMarks } from "@/lib/api/playback";
import { imageUrl } from "@/lib/image-proxy";
import type { MediaItem } from "@/lib/media-types";
import { LibraryFilterBar } from "@/components/library-filter-bar";
import {
  filterToRules,
  isFilterEmpty,
  rulesToFilter,
  type LibraryFilter,
} from "@/lib/library-filter";
import { usePageTitle } from "@/lib/use-page-title";
import { usePermissions } from "@/lib/permissions";
import { buildHomeRows, newCollectionRow, rowsToPrefs } from "@/lib/home-rows";
import { useUiPrefs } from "@/lib/ui-prefs";
import {
  PREF_TO_SORT,
  SORT_DIRECTIONS,
  SORT_PREF_LABELS,
  orderParam,
  useWallSortPref,
  type WallSortPref,
} from "@/lib/wall-sort";

const PAGE_SIZE = 60;

/** ⋯ 菜单项：与单库页那份逐字相同，两处菜单不该长得不一样。 */
const MENU_ITEM_CLASS =
  "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
  "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
  "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";

/**
 * 合集的默认档——「不给 sort」时服务端按什么排，以及它在控件上叫什么、
 * 自然方向是哪边。
 *
 * 三种合集三个答案：名单驱动是用户拖出来的**自定顺序**（自然方向 = 名单序）；
 * 系列合集是按上映顺序（服务端存的是 release_date_asc）；其余规则驱动的按
 * 合集存的 sort。控件上把它叫成它真正的名字，而不是一个笼统的「默认」——
 * 值自己会说话，标签才能省（与单库页「按标题」/「按时间」同一条理由）。
 */
function defaultSortOf(collection: Collection | null): {
  label: string;
  /** 与这一档等价的可选档（不重复摆一遍）；null = 没有等价档 */
  equivalent: Exclude<WallSortPref, "default"> | null;
  naturalAsc: boolean;
  asc: string;
  desc: string;
} {
  if (!collection || !collection.rule_driven) {
    return {
      label: "自定顺序",
      equivalent: null,
      naturalAsc: true,
      asc: "正序",
      desc: "倒序",
    };
  }
  if (collection.kind === "series" || collection.sort === "release_date_asc") {
    // 方向的人话沿用「按上映时间」那档（旧→新 / 新→旧），只是自然方向反过来
    return {
      label: "按上映顺序",
      equivalent: "release_date",
      ...SORT_DIRECTIONS.release_date,
      naturalAsc: true,
    };
  }
  const key = (
    collection.sort in PREF_TO_SORT ? collection.sort : "title"
  ) as Exclude<WallSortPref, "default">;
  return {
    label: SORT_PREF_LABELS[key],
    equivalent: key,
    ...SORT_DIRECTIONS[PREF_TO_SORT[key]],
  };
}

/**
 * 合集详情页（docs/design/library-filtering.md 4.4）。
 *
 * 页面只比海报墙多一样东西：**规则条**——把这个合集"为什么收了这些片"直接
 * 画出来，而不是藏在一个「编辑」弹窗后面。合集是存好的筛选，规则条与库页上的
 * 已选条件行是同一种东西、同一套语言（维内「或」、维间「且」），用户不需要
 * 学第二遍。
 *
 * 名单驱动的合集没有规则条，取而代之的是一句实话：它不会自己长。这两种形态的
 * 差别在几个月后才显形，页面上必须一眼可辨。
 *
 * 浏览能力与单库页、收藏页对齐（用户反馈 2026-09-13）：
 *   - **排序**：同一颗控件、同一套档位（lib/wall-sort.ts），默认档是合集自己的序
 *     （见 defaultSortOf），偏好按合集各记各的——在「诺兰作品」里选的「按评分」
 *     不该把系列合集的上映顺序也排乱；
 *   - **图床浏览**：顶栏那颗键切换海报墙 / 瀑布流，同一套墙与灯箱（video-gallery），
 *     数据换成 /collections/{id}/gallery——与海报墙传同一个 sort，两种形态永远是
 *     同一份名单。形态与看图偏好（分组、密度）与别处共用一份 localStorage。
 */
export function LibraryCollectionDetailView({
  libraryId,
  collectionId,
}: {
  /** 所属库；跨库合集为 null——它的每一格各归各的库 */
  libraryId: number | null;
  collectionId: number;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const prompt = usePrompt();
  const { canManageLibraries } = usePermissions();
  const [collection, setCollection] = useState<Collection | null>(null);
  // null = 第一页还没回来：这时既不画空墙也不说"一部都没有"，那是一句还没成立的话
  const [items, setItems] = useState<LibraryItem[] | null>(null);
  const [facets, setFacets] = useState<LibraryFacets | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [series, setSeries] = useState<CollectionSeries | null>(null);
  // 「整理顺序」面板拿到的名单：一定是自定顺序那一份（见 openOrdering）
  const [ordering, setOrdering] = useState<LibraryItem[] | null>(null);
  // 「改条件」模式：条件本身就是这个合集的定义，改它要能看见现在筛出多少部，
  // 所以直接复用库页那条筛选条——用户不用学第二套控件
  const [editing, setEditing] = useState<LibraryFilter | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareInitial, setShareInitial] = useState<ShareView | null>(null);

  usePageTitle(collection?.name);

  // —— 排序 —— //
  // 偏好按合集各记各的：默认档因合集而异（自定顺序 / 上映顺序 / 合集存的序），
  // 全站共用一个键的话，在别的合集里选的「按评分」会把系列合集的上映顺序也排乱
  const [
    { pref: sortPref, reversed: sortReversed },
    setSortPref,
    toggleSortReversed,
    sortReady,
  ] = useWallSortPref(`movieclaw.collection.wall-sort:${collectionId}`);
  const defaultSort = defaultSortOf(collection);
  const direction =
    sortPref === "default"
      ? defaultSort
      : SORT_DIRECTIONS[PREF_TO_SORT[sortPref]];
  const sortAscending = direction.naturalAsc !== sortReversed;
  // 两种形态的请求都带同一份：默认档不带 sort（服务端按合集自己的序），不反转不带 order
  const sortParams = useMemo<CollectionSortParams>(
    () =>
      sortPref === "default"
        ? {
            order: sortReversed
              ? defaultSort.naturalAsc
                ? "desc"
                : "asc"
              : undefined,
          }
        : {
            sort: PREF_TO_SORT[sortPref],
            order: orderParam(PREF_TO_SORT[sortPref], sortReversed),
          },
    [sortPref, sortReversed, defaultSort.naturalAsc],
  );
  // ref 版：翻页 / 重取的回调每轮读最新值，不必为换排序重建回调链
  const sortRef = useRef(sortParams);
  sortRef.current = sortParams;
  // 这份排序的指纹：两面墙的窗口都按它取；空串 = 合集自己的序
  const sortKey = `${sortPref === "default" ? "" : sortPref}${sortReversed ? ":rev" : ""}`;
  // 与默认档等价的那一档不重复摆：规则合集存的是「按标题」时，列表里再来一个「按标题」
  // 只会让人以为两者有什么区别
  const sortOptions = useMemo<readonly (readonly [WallSortPref, string])[]>(
    () => [
      ["default", defaultSort.label],
      ...(Object.entries(SORT_PREF_LABELS) as [WallSortPref, string][]).filter(
        ([key]) => key !== defaultSort.equivalent,
      ),
    ],
    [defaultSort.label, defaultSort.equivalent],
  );

  useEffect(() => {
    let alive = true;
    getCollection(collectionId)
      .then((row) => alive && setCollection(row))
      .catch(() => alive && setError("这个合集不存在，或者你看不到它。"));
    return () => {
      alive = false;
    };
  }, [collectionId]);

  /** 第一页重取并整体替换：换排序、改条件、整理顺序之后都走这里。 */
  const reloadItems = useCallback(async () => {
    const rows = await listCollectionItems(collectionId, {
      limit: PAGE_SIZE,
      ...sortRef.current,
    });
    setItems(rows);
    setHasMore(rows.length === PAGE_SIZE);
  }, [collectionId]);

  // 第一页：偏好还没从 storage 读出来时先按兵不动——这一帧的排序是默认值，照它
  // 拉一遍再按真正的偏好重拉，墙会闪一下。换了排序同样从第一页重来
  useEffect(() => {
    if (!sortReady) return;
    let alive = true;
    listCollectionItems(collectionId, { limit: PAGE_SIZE, ...sortRef.current })
      .then((rows) => {
        if (!alive) return;
        setItems(rows);
        setHasMore(rows.length === PAGE_SIZE);
      })
      .catch(() => alive && setHasMore(false));
    return () => {
      alive = false;
    };
    // sortKey 变了 sortRef 就变了：它是这一份请求真正的依赖
  }, [collectionId, sortReady, sortKey]);

  // 缺片补齐：**只在打开系列合集的详情页时**才发这一次请求（懒加载）。
  // 刮削期不拉——那是白白给扫描加负担，用户从没点开的系列一个请求都不该花
  useEffect(() => {
    if (collection?.kind !== "series") return;
    let alive = true;
    getCollectionSeries(collectionId)
      .then((data) => alive && setSeries(data))
      .catch(() => alive && setSeries(null));
    return () => {
      alive = false;
    };
  }, [collectionId, collection?.kind]);

  // 规则里的取值要翻成中文名（类型 id → 「动画」），标签来自库的 facet：
  // 与库页筛选条上显示的是同一份，不另起一套翻译
  useEffect(() => {
    if (!collection?.rules?.length || libraryId === null) return;
    let alive = true;
    getLibraryFacets(libraryId, rulesToFilter(collection.rules), "all")
      .then((data) => alive && setFacets(data))
      .catch(() => alive && setFacets(null));
    return () => {
      alive = false;
    };
  }, [libraryId, collection?.rules]);

  /** 合集挂在库下面时每一格都落回本库；跨库合集按每一格自己的库落地 */
  const ownLibraryId = useCallback(
    (item: LibraryItem) => libraryId ?? item.library_id ?? 0,
    [libraryId],
  );

  const rows = useMemo(() => items ?? [], [items]);
  const loadMore = useCallback(async () => {
    const page = await listCollectionItems(collectionId, {
      limit: PAGE_SIZE,
      offset: rows.length,
      ...sortRef.current,
    });
    setItems((prev) => [...(prev ?? []), ...page]);
    setHasMore(page.length === PAGE_SIZE);
  }, [collectionId, rows.length]);

  // —— 图床浏览模式 —— //
  const [galleryPreferred, setGalleryMode] = useVideoGalleryMode();
  const [galleryGrouped, setGalleryGrouped] = useVideoGalleryGrouped();
  const [density, setDensity] = usePhotoWallDensity();
  const [galleryGroups, setGalleryGroups] = useState<LibraryGalleryGroup[]>([]);
  const [galleryHasMore, setGalleryHasMore] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  // 已请求到的作品数（按页长推进，不按拿到的组数——没图的作品也占一组）
  const galleryLoaded = useRef(0);
  const galleryLoading = useRef(false);
  // 成员变过几次（改条件、整理顺序）：图廊窗口跟着作废，下次进图廊重拉
  const [membersEpoch, setMembersEpoch] = useState(0);
  const galleryEntries = useMemo(
    () => flattenGallery(galleryGroups),
    [galleryGroups],
  );
  // 合集一部都没有时不给切换键：两种形态都是空页，多一颗键只会让人以为点了没反应
  const gallery = galleryPreferred && rows.length > 0;

  const loadMoreGallery = useCallback(() => {
    if (galleryLoading.current) return;
    galleryLoading.current = true;
    const offset = galleryLoaded.current;
    listCollectionGallery(collectionId, {
      limit: GALLERY_PAGE_SIZE,
      offset,
      ...sortRef.current,
    })
      .then((page) => {
        galleryLoaded.current = offset + page.length;
        setGalleryGroups((current) =>
          dedupeGalleryGroups([...current, ...page]),
        );
        setGalleryHasMore(page.length >= GALLERY_PAGE_SIZE);
      })
      .catch(() => setGalleryHasMore(false))
      .finally(() => {
        galleryLoading.current = false;
      });
  }, [collectionId]);

  // 这份图廊窗口是按哪个（合集、排序、成员版本）取的；切回海报墙不清空——
  // 窗口留着，再切回来不用白拉一遍
  const galleryWindowKey = useRef<string | null>(null);
  useEffect(() => {
    if (!galleryPreferred || !sortReady) return;
    const key = `${collectionId}:${sortKey}:${membersEpoch}`;
    if (galleryWindowKey.current === key) return;
    galleryWindowKey.current = key;
    galleryLoaded.current = 0;
    setGalleryGroups([]);
    setGalleryHasMore(false);
    loadMoreGallery();
  }, [
    galleryPreferred,
    sortReady,
    sortKey,
    membersEpoch,
    collectionId,
    loadMoreGallery,
  ]);

  /**
   * 灯箱里点心：与收藏页、单库页同一处理——收藏态挂在分组上，灯箱的心与墙上
   * 那几张瓦片的角标读同一份，翻一次全都跟着变。先翻本地再落库，失败翻回来。
   */
  const toggleGalleryFavorite = useCallback(
    async (mediaItemId: number, next: boolean) => {
      const patch = (value: boolean) =>
        setGalleryGroups((current) =>
          current.map((g) =>
            g.media_item_id === mediaItemId ? { ...g, is_favorite: value } : g,
          ),
        );
      patch(next);
      try {
        const marks = await setPlaybackMarks(
          { media_item_id: mediaItemId },
          { favorite: next },
        );
        patch(marks.is_favorite);
      } catch (e) {
        patch(!next);
        toast.error(e instanceof Error ? e.message : "收藏失败，请稍后重试");
      }
    },
    [toast],
  );

  /** 成员或顺序变了：海报墙第一页重取，图廊窗口作废。 */
  const membersChanged = useCallback(() => {
    reloadItems().catch(() => undefined);
    setMembersEpoch((n) => n + 1);
    getCollection(collectionId)
      .then(setCollection)
      .catch(() => undefined);
  }, [collectionId, reloadItems]);

  /**
   * 打开「整理顺序」：面板里的名单必须是**自定顺序**那一份。墙上正按评分排着时
   * 把这份名单交给面板，用户不拖一下就点保存，评分序就被写成了他的自定顺序——
   * 一个看着没做什么的操作把顺序改了。所以临时排序下另取一次默认序。
   */
  const openOrdering = useCallback(() => {
    if (!sortKey) {
      setOrdering(rows);
      return;
    }
    listCollectionItems(collectionId, { limit: PAGE_SIZE })
      .then(setOrdering)
      .catch((err) =>
        toast.error(err instanceof Error ? err.message : "取不到名单"),
      );
  }, [collectionId, rows, sortKey, toast]);

  const rename = useCallback(async () => {
    if (!collection) return;
    const name = (
      await prompt({ title: "合集名", initialValue: collection.name })
    )?.trim();
    if (!name || name === collection.name) return;
    try {
      setCollection(await updateCollection(collection.id, { name }));
      toast.success("已改名");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "改名失败");
    }
  }, [collection, prompt, toast]);

  // 同一颗按钮两种归宿：自建的真删，自动生成的落墓碑（真删了下次扫描
  // 又会长回来，用户会觉得"删不掉"）。归宿由后端按 builtin 推导，前端只是
  // 把话说对——文案说"删除"而实际藏起来，比藏起来本身更让人迷惑
  // 首页行清单存在界面偏好里（成员各存各的）：这个合集有没有一行、那一行藏没藏
  const { prefs, savePrefs } = useUiPrefs();
  const homeRow = collection
    ? prefs.home.rows.find((row) => row.collection_id === collection.id)
    : undefined;
  const onHome = Boolean(homeRow && !homeRow.hidden);
  const toggleOnHome = useCallback(async () => {
    if (!collection) return;
    try {
      let rows;
      if (homeRow) {
        rows = prefs.home.rows.map((row) =>
          row.collection_id === collection.id
            ? { ...row, hidden: onHome }
            : row,
        );
      } else {
        // 与自定义页同一条路：先按当前的库与合集合并出整份清单，再把新行追加在末尾。
        // 直接往存的清单里塞一条，在从没存过清单的人那里会排到首页最前面；
        // newCollectionRow 还会把合集自己的排序收窄到首页支持的档
        const [libraries, collections] = await Promise.all([
          listLibraries(),
          listCollections(),
        ]);
        rows = rowsToPrefs([
          ...buildHomeRows(prefs.home, libraries, collections),
          newCollectionRow(collection),
        ]);
      }
      await savePrefs({ ...prefs, home: { rows } });
      toast.success(onHome ? "已从首页移除" : "已显示在首页");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }, [collection, homeRow, onHome, prefs, savePrefs, toast]);

  const auto = collection ? collection.kind !== "user" : false;
  const remove = useCallback(async () => {
    if (!collection) return;
    const automatic = collection.kind !== "user";
    const ok = await confirm({
      title: automatic
        ? `隐藏「${collection.name}」？`
        : `删除合集「${collection.name}」？`,
      // 这句一定要说：合集从来不拥有作品，删它不会少一部片。不说的话，
      // 用户会因为怕删掉影片而不敢清理合集
      description: automatic
        ? "自动生成的合集会一直重新出现，所以这里是把它藏起来：影片一部都不会少，想找回来在媒体库设置里打开「显示已隐藏的合集」。"
        : "只删掉这层视图，里面的影片一部都不会少。",
      tone: automatic ? undefined : "danger",
      // 按钮上写它真的会做什么。默认那个「确定」在这种两种归宿的对话框里
      // 最容易让人按错——用户以为自己在删，实际是藏（反过来更糟）
      confirmLabel: automatic ? "隐藏" : "删除",
    });
    if (!ok) return;
    try {
      await deleteCollection(collection.id);
      toast.success(automatic ? "已隐藏" : "已删除");
      window.history.back();
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : automatic
            ? "隐藏失败"
            : "删除失败",
      );
    }
  }, [collection, confirm, toast]);

  const saveRules = useCallback(async () => {
    if (!collection || editing === null) return;
    try {
      setCollection(
        await updateCollection(collection.id, {
          rules: filterToRules(editing),
        }),
      );
      setEditing(null);
      // 条件变了成员就变了：把这一页重取，别让用户对着旧名单猜
      await reloadItems();
      setMembersEpoch((n) => n + 1);
      toast.success("条件已保存");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }, [collection, editing, reloadItems, toast]);

  const unhide = useCallback(async () => {
    if (!collection) return;
    try {
      setCollection(await updateCollection(collection.id, { hidden: false }));
      toast.success("已恢复显示");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "恢复失败");
    }
  }, [collection, toast]);

  const applyToLibrary = useCallback(async () => {
    if (!collection) return;
    const ok = await confirm({
      title: "把这组条件设为库的收藏范围？",
      description:
        "以后订阅与自动入库会按这组条件挑库。只有类型和地区会被用上，其余条件用不到。",
    });
    if (!ok) return;
    try {
      if (libraryId === null) return;
      await applyCollectionToLibrary(collection.id, libraryId);
      toast.success("已设为该库的收藏范围");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "设置失败");
    }
  }, [collection, confirm, libraryId, toast]);

  if (error) {
    return (
      <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
        <PageNav
          title="合集"
          fallback={{
            label: "媒体库",
            href: (libraryId === null
              ? "/library/collections"
              : `/library/${libraryId}`) as Route,
          }}
        />
        <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">
          {error}
        </p>
      </div>
    );
  }

  // 系列缺片只在**上映顺序**下插进墙里：缺片是按上映日归并到它该在的位置上的，
  // 墙按评分、按片长排着时那个位置不存在——硬插只会插错地方。换了排序就退回
  // 普通海报墙，页头的「已有 N / 共 M」照样在，缺哪几部切回默认序一眼就看到
  const showSeries =
    !sortKey &&
    series?.available &&
    series.parts.some((part) => part.media_item_id === null);

  // 页面自己出滚动容器：外壳的 main 不滚动（与收藏页、单库页同一约定），
  // 少了这一层，海报墙超出一屏就滑不动
  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <PageNav
        title={collection?.name ?? "合集"}
        fallback={{
          label: "媒体库",
          href: (libraryId === null
            ? "/library/collections"
            : `/library/${libraryId}`) as Route,
        }}
        actions={
          // 收进 ⋯，与单库页一致：顶栏那几个位子是 36px 的圆钮，塞中文标签会
          // 挤成竖排。内置合集不可改，那颗键干脆不出现
          collection ? (
            <>
              {/* 与收藏页顶栏同一颗键、同一副图标：画的是**点过去会变成的那面墙** */}
              {rows.length > 0 && (
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
              )}
              <DropdownMenu.Root>
                <DropdownMenu.Trigger asChild>
                  <button
                    type="button"
                    aria-label="更多操作"
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
                    {/* 看图的两个偏好（按作品分组 / 瀑布流密度）：只在图廊里有意义 */}
                    {gallery && (
                      <>
                        <WallPrefItems
                          grouped={galleryGrouped}
                          onGroupedChange={setGalleryGrouped}
                          density={density}
                          onDensityChange={setDensity}
                          itemClass={MENU_ITEM_CLASS}
                        />
                        <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
                      </>
                    )}
                    <DropdownMenu.Item
                      onSelect={rename}
                      className={MENU_ITEM_CLASS}
                    >
                      改名
                    </DropdownMenu.Item>
                    {/* 分享整个合集：链接对外可看，成员每次访问现算——
                        规则驱动的合集会自己长，朋友明天打开就多了几部 */}
                    {canManageLibraries && (
                      <DropdownMenu.Item
                        onSelect={() => {
                          getCollectionShare(collectionId)
                            .then((existing) => {
                              setShareInitial(existing);
                              setShareOpen(true);
                            })
                            .catch((err) =>
                              toast.error(
                                err instanceof Error
                                  ? err.message
                                  : "打不开分享",
                              ),
                            );
                        }}
                        className={MENU_ITEM_CLASS}
                      >
                        分享…
                      </DropdownMenu.Item>
                    )}
                    {/* 规则就是这个合集的定义，改它是最要紧的一件事——此前只读，
                        看得见改不了（F4 把它补上）*/}
                    {libraryId !== null &&
                      collection.editable &&
                      collection.rule_driven && (
                        <DropdownMenu.Item
                          onSelect={() =>
                            setEditing(rulesToFilter(collection.rules))
                          }
                          className={MENU_ITEM_CLASS}
                        >
                          改条件…
                        </DropdownMenu.Item>
                      )}
                    {/* 手动合集才谈得上"顺序"：规则驱动的成员是求值出来的，
                        它的先后由 sort 决定，拖不动也不该拖 */}
                    {collection.editable && !collection.rule_driven && (
                      <DropdownMenu.Item
                        onSelect={openOrdering}
                        className={MENU_ITEM_CLASS}
                      >
                        整理顺序…
                      </DropdownMenu.Item>
                    )}
                    {libraryId !== null &&
                      canManageLibraries &&
                      collection.editable &&
                      collection.rule_driven && (
                        <DropdownMenu.Item
                          onSelect={applyToLibrary}
                          className={MENU_ITEM_CLASS}
                        >
                          设为本库的收藏范围
                        </DropdownMenu.Item>
                      )}
                    {/* 「显示在首页」：把这个合集加成媒体库首页的一行（与 Plex 的 Pin to Home
                        一致），写的是与自定义页同一份偏好；再点一次是隐藏那一行，不删 */}
                    <DropdownMenu.Item
                      onSelect={toggleOnHome}
                      className={MENU_ITEM_CLASS}
                    >
                      {onHome ? "从首页移除" : "显示在首页"}
                    </DropdownMenu.Item>
                    <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
                    {collection.hidden ? (
                      <DropdownMenu.Item
                        onSelect={unhide}
                        className={MENU_ITEM_CLASS}
                      >
                        恢复显示
                      </DropdownMenu.Item>
                    ) : (
                      <DropdownMenu.Item
                        onSelect={remove}
                        className={MENU_ITEM_CLASS}
                      >
                        {auto ? "隐藏这个合集" : "删除合集"}
                      </DropdownMenu.Item>
                    )}
                  </DropdownMenu.Content>
                </DropdownMenu.Portal>
              </DropdownMenu.Root>
            </>
          ) : undefined
        }
      />

      <div className="px-6 pt-2 max-md:px-4">
        <h1 className="text-title font-semibold text-[var(--text-strong)]">
          {collection?.name ?? " "}
        </h1>
        <p className="mt-1 text-ui text-[var(--text-muted)]">
          {/* 系列合集说「已有 6 / 共 8」：这个产品能回答"你缺哪几部"，
              而这句话就是入口。上游档案没拉到时退回普通计数，不编数字 */}
          {collection
            ? series?.available && series.total > 0
              ? `已有 ${series.owned_count} / 共 ${series.total} 部`
              : `${collection.item_count} 部`
            : ""}
          {collection?.visibility === "private" && (
            <span className="ml-2">· 只有我可见</span>
          )}
          {collection?.hidden && <span className="ml-2">· 已隐藏</span>}
        </p>
        {collection && editing === null && (
          <RuleRow collection={collection} facets={facets} />
        )}
        {/* 排序：与单库页、收藏页同一颗控件、同一套档位，默认档叫合集自己的序。
            「按什么排」看墙是看不出来的，所以当前值挂在外面，不收进 ⋯ 菜单。
            改条件时收起——那一行已经被筛选条占了，且改完条件成员会整个换掉 */}
        {collection && editing === null && rows.length > 0 && (
          <div className="mt-3 flex items-center">
            <WallSortControl
              value={sortPref}
              options={sortOptions}
              onChange={setSortPref}
              disabled={!sortReady}
              direction={{
                ascending: sortAscending,
                label: direction[sortAscending ? "asc" : "desc"],
                onToggle: toggleSortReversed,
              }}
            />
          </div>
        )}
        {collection && editing !== null && libraryId !== null && (
          <div className="mt-3">
            <LibraryFilterBar
              libraryId={libraryId}
              filter={editing}
              onFilterChange={setEditing}
            />
            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={() => void saveRules()}
                disabled={isFilterEmpty(editing)}
                className="h-8 rounded-lg bg-white/10 px-3 text-ui font-medium text-white transition hover:bg-white/20 disabled:opacity-40"
              >
                保存条件
              </button>
              <button
                type="button"
                onClick={() => setEditing(null)}
                className="h-8 rounded-lg px-3 text-ui text-white/70 transition hover:bg-white/10 hover:text-white"
              >
                取消
              </button>
              {/* 一个条件都不剩 = 收录整库，那不是用户想要的合集，也不该
                  让他一不小心存成那样 */}
              {isFilterEmpty(editing) && (
                <span className="text-sub text-[var(--text-faint)]">
                  至少留一个条件，否则这个合集会收录整库
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {collection && canManageLibraries && (
        <ShareDialog
          open={shareOpen}
          onClose={() => setShareOpen(false)}
          collectionId={collection.id}
          title={collection.name}
          year={null}
          posterUrl={collection.covers[0]?.url ?? null}
          seasonSummary={`${collection.item_count} 部${
            collection.rule_driven ? " · 会自动收录新片" : ""
          }`}
          initialShare={shareInitial}
        />
      )}

      {ordering && collection && (
        <CollectionOrderPanel
          collectionId={collection.id}
          items={ordering}
          onClose={() => setOrdering(null)}
          // 顺序/成员变了就把这一页重取：详情页的名单与服务端必须是同一份
          onSaved={membersChanged}
        />
      )}

      <div className="mt-6 max-md:mt-4">
        {items === null ? null : gallery ? (
          <div className="px-6 max-md:px-4">
            <VideoGalleryWall
              groups={galleryGroups}
              density={density}
              grouped={galleryGrouped}
              onOpen={setLightboxIndex}
            />
            <WallLoadMore
              hasMore={galleryHasMore}
              loaded={galleryLoaded.current}
              start={0}
              total={collection?.item_count ?? galleryLoaded.current}
              onReach={loadMoreGallery}
              rootMargin={GALLERY_LOAD_MARGIN}
            />
          </div>
        ) : showSeries ? (
          // 系列缺片：缺的那几部不另起一块，直接按上映顺序画进墙里（见 SeriesWall）
          <div className="px-6 max-md:px-4">
            <SeriesWall
              items={rows}
              parts={series.parts}
              complete={!hasMore}
              libraryIdOf={ownLibraryId}
            />
            <WallLoadMore
              hasMore={hasMore}
              loaded={rows.length}
              start={0}
              total={collection?.item_count ?? rows.length}
              onReach={loadMore}
            />
          </div>
        ) : rows.length === 0 ? (
          <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">
            这个合集现在一部都没有。
          </p>
        ) : (
          <div className="px-6 max-md:px-4">
            {/* 合集挂在库下面，每一格都落回本库的条目详情 */}
            <PosterWall items={rows} libraryIdOf={ownLibraryId} wide={false} />
            <WallLoadMore
              hasMore={hasMore}
              loaded={rows.length}
              start={0}
              total={collection?.item_count ?? rows.length}
              onReach={loadMore}
            />
          </div>
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

/**
 * 系列合集的海报墙：库里有的与缺的按上映顺序排在**同一面墙**上
 * （docs/design/library-series-collections.md 6.5）。
 *
 * **缺片补齐才是系列合集真正的价值。** 只做归类的话，用户装个 Emby 也有；
 * 能告诉他"缺哪几部、点一下就去补"的，只有这个产品。
 *
 * 缺的那部不另起一块"还缺 N 部"清单，而是画在它该在的位置上：《哈利·波特》
 * 第 3 部缺了，就在第 2 部与第 4 部之间放一张压暗的海报——缺的是哪一部、前后
 * 是什么一眼就懂；清单只能让用户自己去对年份。
 *
 * 订阅走全站那一份订阅弹窗（海报卡片自带的「订阅影片」动作），不就地一键订上：
 * 选库、规则组、下载落点都在弹窗里，缺片补订不该是绕开这些的旁路。
 *
 * 不用虚拟化的 PosterWall：一个系列就几部到二十几部，普通网格足够；虚拟化墙的
 * 排版与位置锚点都假定每一格是库存条目，硬把缺片塞成假条目只会污染它们。
 * 缺片等合集整页加载完才插（``complete``）——还没加载到的库存片可能排在它前面。
 */
function SeriesWall({
  items,
  parts,
  complete,
  libraryIdOf,
}: {
  items: LibraryItem[];
  parts: SeriesPart[];
  complete: boolean;
  libraryIdOf: (item: LibraryItem) => number;
}) {
  const { subscriptionOf } = useSubscribeEntry();
  const missing = complete
    ? parts.filter((part) => part.media_item_id === null)
    : [];
  return (
    <div data-testid="series-wall" className={WALL_GRID_POSTER}>
      {interleaveByRelease(items, missing).map((cell) =>
        "part" in cell ? (
          <MissingPartCell
            key={`missing:${cell.part.tmdb_id}`}
            part={cell.part}
            tracked={
              cell.part.subscribed ||
              Boolean(
                subscriptionOf({
                  id: String(cell.part.tmdb_id),
                  type: "movie",
                }),
              )
            }
          />
        ) : (
          <InventoryCell
            key={cell.item.media_item_id}
            item={cell.item}
            libraryId={libraryIdOf(cell.item)}
          />
        ),
      )}
    </div>
  );
}

type SeriesCell = { item: LibraryItem } | { part: SeriesPart };

/** 两列都已按上映正序：归并成一列。没有日期的排最后，与后端 parts 的排序同一口径。 */
function interleaveByRelease(
  items: LibraryItem[],
  missing: SeriesPart[],
): SeriesCell[] {
  const dateOf = (value: string | null) => value ?? "9999-12-31";
  const cells: SeriesCell[] = [];
  let next = 0;
  for (const item of items) {
    while (
      next < missing.length &&
      dateOf(missing[next].release_date) < dateOf(item.release_date)
    ) {
      cells.push({ part: missing[next++] });
    }
    cells.push({ item });
  }
  for (; next < missing.length; next++) cells.push({ part: missing[next] });
  return cells;
}

/**
 * 缺片格：与发现页同一张海报卡，只是**压暗**——一眼看出"这部还不在我的库里"。
 *
 * 点海报与发现页一样进 TMDB 详情；悬停（触屏是首次点按展开）浮出「订阅影片」，
 * 走全站的订阅弹窗。已经在追的副行写「追踪中」，卡片自己也会把订阅键换成
 * 「管理订阅」，不会让人再订一遍。
 */
function MissingPartCell({
  part,
  tracked,
}: {
  part: SeriesPart;
  tracked: boolean;
}) {
  // 点海报走发现页同一条 TMDB 详情路径，所以要给完整的 MediaItem；列表拿不到的
  // 字段（类型、简介）留空，进详情后由详情接口回填
  const visual: MediaItem = {
    titleRef: `tmdb:movie:${part.tmdb_id}`,
    id: String(part.tmdb_id),
    source: "tmdb",
    type: "movie",
    title: part.title,
    originalTitle: part.title,
    // 0 = 上映日待定：卡片对假值年份不渲染
    year: part.release_date ? Number(part.release_date.slice(0, 4)) : 0,
    rating: 0,
    genres: [],
    badges: [],
    overview: "",
    posterUrl: imageUrl(part.poster_url, "poster-card"),
    extent: tracked ? "追踪中" : "未入库",
  };
  return (
    // 只压暗海报图，不压暗片名与悬停层：文字照样读得清，订阅键照样醒目。
    // 悬停时提亮一些，让人看清要订的是哪张海报
    <div
      data-testid="series-missing-part"
      className="[&_img]:opacity-40 [&_img]:transition-opacity [&_img]:duration-300 hover:[&_img]:opacity-75"
    >
      <PosterCard item={visual} action="subscribe" />
    </div>
  );
}

/** 维度名 → 标签，与库页筛选条同一份顺序与叫法。 */
const DIMS = [
  { key: "genres", label: "类型" },
  { key: "decades", label: "年代" },
  { key: "countries", label: "地区" },
  { key: "watch", label: "观看" },
] as const;

/**
 * 规则条：合集"为什么收了这些片"。
 *
 * 只读——改条件是 F4 的事。但即使只读也必须画出来：看不见规则的智能合集，
 * 数字变了用户只会怀疑软件坏了。
 */
function RuleRow({
  collection,
  facets,
}: {
  collection: Collection;
  facets: LibraryFacets | null;
}) {
  if (collection.kind === "series") {
    // 系列合集的规则是 series_key，翻不成"类型/年代/地区"那套话。硬套的话
    // 这里会显示"收录本库全部作品"——一句彻头彻尾的假话
    return (
      <p className="mt-3 text-sub text-[var(--text-faint)]">
        {/* 不说"收录全部作品"：合集里只有库里有的那几部（上面写着「已有 N / 共 M」），
            也不说"自动收录"：缺片画在墙上之后，这四个字会被读成"会自动去下载" */}
        作品系列 · 按上映顺序排列，以后入库的续作会自动归进来
      </p>
    );
  }
  if (!collection.rule_driven) {
    return (
      <p className="mt-3 text-sub text-[var(--text-faint)]">
        固定名单 · 不会自动收录新片
      </p>
    );
  }
  const filter = rulesToFilter(collection.rules);
  // 查不到展示名就给省略号：规则里存的是 TMDB id 与国家码，界面上冒出「16」
  // 「JP」比空着更糟。查不到只意味着 facet 还在路上，到了自然补上
  const labelOf = (
    pool: { value: string; label: string }[] | undefined,
    value: string,
  ) => pool?.find((row) => row.value === value)?.label ?? "…";
  const groups = DIMS.map(({ key, label }) => {
    const values =
      key === "watch"
        ? filter.watch
          ? [
              {
                value: filter.watch,
                label: labelOf(facets?.watch, filter.watch),
              },
            ]
          : []
        : (((filter[key] ?? []) as (string | number)[]) ?? []).map((raw) => {
            const value = String(raw);
            const pool =
              key === "genres"
                ? facets?.genres
                : key === "countries"
                  ? facets?.countries
                  : facets?.decades;
            return { value, label: labelOf(pool, value) };
          });
    return { label, values };
  }).filter((group) => group.values.length > 0);

  if (groups.length === 0) {
    return (
      <p className="mt-3 text-sub text-[var(--text-faint)]">
        自动收录 · 收录本库全部作品
      </p>
    );
  }

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <span className="text-caption text-[var(--text-faint)]">自动收录</span>
      {groups.map((group, index) => (
        <div key={group.label} className="flex items-center gap-2">
          {/* 维度之间是「且」，维度内是「或」——与库页筛选条同一套语言 */}
          {index > 0 && (
            <span className="text-caption tracking-wide text-white/30">且</span>
          )}
          <span className="glass-row flex h-7 !w-auto items-center gap-1.5 rounded-lg !bg-[var(--glass-fill-active)] !px-2 py-0">
            <span className="rounded bg-black/25 px-1.5 py-0.5 text-caption text-white/40">
              {group.label}
            </span>
            {group.values.map((value, i) => (
              <span key={value.value} className="flex items-center gap-1.5">
                {i > 0 && (
                  <span className="text-caption text-white/30">或</span>
                )}
                <span className="text-caption font-semibold text-white">
                  {value.label}
                </span>
              </span>
            ))}
          </span>
        </div>
      ))}
    </div>
  );
}
