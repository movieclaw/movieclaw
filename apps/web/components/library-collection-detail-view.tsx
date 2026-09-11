"use client";

import { useCallback, useEffect, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";

import { CollectionOrderPanel } from "@/components/collection-order-panel";
import { ShareDialog } from "@/components/share-dialog";
import { useConfirm, usePrompt, useToast } from "@/components/feedback";
import { MoreIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { PosterCard } from "@/components/poster-card";
import { InventoryCell, PosterWall, WALL_GRID_POSTER } from "@/components/poster-wall";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { WallLoadMore } from "@/components/wall-chrome";
import {
  applyCollectionToLibrary,
  deleteCollection,
  getCollection,
  getCollectionSeries,
  listCollectionItems,
  updateCollection,
  type Collection,
  type CollectionSeries,
  type SeriesPart,
} from "@/lib/api/collections";
import { getCollectionShare, type ShareView } from "@/lib/api/shares";
import { getLibraryFacets, type LibraryFacets, type LibraryItem } from "@/lib/api/libraries";
import { imageUrl } from "@/lib/image-proxy";
import type { MediaItem } from "@/lib/media-types";
import { LibraryFilterBar } from "@/components/library-filter-bar";
import { filterToRules, isFilterEmpty, rulesToFilter, type LibraryFilter } from "@/lib/library-filter";
import { usePageTitle } from "@/lib/use-page-title";
import { usePermissions } from "@/lib/permissions";

const PAGE_SIZE = 60;

/** ⋯ 菜单项：与单库页那份逐字相同，两处菜单不该长得不一样。 */
const MENU_ITEM_CLASS =
  "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
  "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
  "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";

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
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [facets, setFacets] = useState<LibraryFacets | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [series, setSeries] = useState<CollectionSeries | null>(null);
  const [ordering, setOrdering] = useState(false);
  // 「改条件」模式：条件本身就是这个合集的定义，改它要能看见现在筛出多少部，
  // 所以直接复用库页那条筛选条——用户不用学第二套控件
  const [editing, setEditing] = useState<LibraryFilter | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareInitial, setShareInitial] = useState<ShareView | null>(null);

  usePageTitle(collection?.name);

  useEffect(() => {
    let alive = true;
    getCollection(collectionId)
      .then((row) => alive && setCollection(row))
      .catch(() => alive && setError("这个合集不存在，或者你看不到它。"));
    listCollectionItems(collectionId, { limit: PAGE_SIZE })
      .then((rows) => {
        if (!alive) return;
        setItems(rows);
        setHasMore(rows.length === PAGE_SIZE);
      })
      .catch(() => alive && setHasMore(false));
    return () => {
      alive = false;
    };
  }, [collectionId]);

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

  const loadMore = useCallback(async () => {
    const rows = await listCollectionItems(collectionId, {
      limit: PAGE_SIZE,
      offset: items.length,
    });
    setItems((prev) => [...prev, ...rows]);
    setHasMore(rows.length === PAGE_SIZE);
  }, [collectionId, items.length]);

  const rename = useCallback(async () => {
    if (!collection) return;
    const name = (await prompt({ title: "合集名", initialValue: collection.name }))?.trim();
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
  const auto = collection ? collection.kind !== "user" : false;
  const remove = useCallback(async () => {
    if (!collection) return;
    const automatic = collection.kind !== "user";
    const ok = await confirm({
      title: automatic ? `隐藏「${collection.name}」？` : `删除合集「${collection.name}」？`,
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
      toast.error(err instanceof Error ? err.message : automatic ? "隐藏失败" : "删除失败");
    }
  }, [collection, confirm, toast]);

  const saveRules = useCallback(async () => {
    if (!collection || editing === null) return;
    try {
      setCollection(await updateCollection(collection.id, { rules: filterToRules(editing) }));
      setEditing(null);
      // 条件变了成员就变了：把这一页重取，别让用户对着旧名单猜
      const rows = await listCollectionItems(collectionId, { limit: PAGE_SIZE });
      setItems(rows);
      setHasMore(rows.length === PAGE_SIZE);
      toast.success("条件已保存");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }, [collection, collectionId, editing, toast]);

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
        <PageNav title="合集" fallback={{
          label: "媒体库",
          href: (libraryId === null ? "/library/collections" : `/library/${libraryId}`) as Route,
        }} />
        <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">{error}</p>
      </div>
    );
  }

  // 页面自己出滚动容器：外壳的 main 不滚动（与收藏页、单库页同一约定），
  // 少了这一层，海报墙超出一屏就滑不动
  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <PageNav
        title={collection?.name ?? "合集"}
        fallback={{
          label: "媒体库",
          href: (libraryId === null ? "/library/collections" : `/library/${libraryId}`) as Route,
        }}
        actions={
          // 收进 ⋯，与单库页一致：顶栏那几个位子是 36px 的圆钮，塞中文标签会
          // 挤成竖排。内置合集不可改，那颗键干脆不出现
          collection ? (
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
                  <DropdownMenu.Item onSelect={rename} className={MENU_ITEM_CLASS}>
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
                            toast.error(err instanceof Error ? err.message : "打不开分享"),
                          );
                      }}
                      className={MENU_ITEM_CLASS}
                    >
                      分享…
                    </DropdownMenu.Item>
                  )}
                  {/* 规则就是这个合集的定义，改它是最要紧的一件事——此前只读，
                      看得见改不了（F4 把它补上）*/}
                  {libraryId !== null && collection.editable && collection.rule_driven && (
                    <DropdownMenu.Item
                      onSelect={() => setEditing(rulesToFilter(collection.rules))}
                      className={MENU_ITEM_CLASS}
                    >
                      改条件…
                    </DropdownMenu.Item>
                  )}
                  {/* 手动合集才谈得上"顺序"：规则驱动的成员是求值出来的，
                      它的先后由 sort 决定，拖不动也不该拖 */}
                  {collection.editable && !collection.rule_driven && (
                    <DropdownMenu.Item
                      onSelect={() => setOrdering(true)}
                      className={MENU_ITEM_CLASS}
                    >
                      整理顺序…
                    </DropdownMenu.Item>
                  )}
                  {libraryId !== null &&
                    canManageLibraries &&
                    collection.editable &&
                    collection.rule_driven && (
                      <DropdownMenu.Item onSelect={applyToLibrary} className={MENU_ITEM_CLASS}>
                        设为本库的收藏范围
                      </DropdownMenu.Item>
                    )}
                  <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
                  {collection.hidden ? (
                    <DropdownMenu.Item onSelect={unhide} className={MENU_ITEM_CLASS}>
                      恢复显示
                    </DropdownMenu.Item>
                  ) : (
                    <DropdownMenu.Item onSelect={remove} className={MENU_ITEM_CLASS}>
                      {auto ? "隐藏这个合集" : "删除合集"}
                    </DropdownMenu.Item>
                  )}
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
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
          {collection?.visibility === "private" && <span className="ml-2">· 只有我可见</span>}
          {collection?.hidden && <span className="ml-2">· 已隐藏</span>}
        </p>
        {collection && editing === null && <RuleRow collection={collection} facets={facets} />}
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
          items={items}
          onClose={() => setOrdering(false)}
          onSaved={() => {
            // 顺序/成员变了就把这一页重取：详情页的名单与服务端必须是同一份
            listCollectionItems(collectionId, { limit: PAGE_SIZE })
              .then((rows) => {
                setItems(rows);
                setHasMore(rows.length === PAGE_SIZE);
              })
              .catch(() => undefined);
            getCollection(collectionId)
              .then(setCollection)
              .catch(() => undefined);
          }}
        />
      )}

      <div className="mt-6 max-md:mt-4">
        {series?.available && series.parts.some((part) => part.media_item_id === null) ? (
          // 系列缺片：缺的那几部不另起一块，直接按上映顺序画进墙里（见 SeriesWall）
          <div className="px-6 max-md:px-4">
            <SeriesWall
              items={items}
              parts={series.parts}
              complete={!hasMore}
              libraryIdOf={ownLibraryId}
            />
            <WallLoadMore
              hasMore={hasMore}
              loaded={items.length}
              start={0}
              total={collection?.item_count ?? items.length}
              onReach={loadMore}
            />
          </div>
        ) : items.length === 0 ? (
          <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">
            这个合集现在一部都没有。
          </p>
        ) : (
          <div className="px-6 max-md:px-4">
            {/* 合集挂在库下面，每一格都落回本库的条目详情 */}
            <PosterWall items={items} libraryIdOf={ownLibraryId} wide={false} />
            <WallLoadMore
              hasMore={hasMore}
              loaded={items.length}
              start={0}
              total={collection?.item_count ?? items.length}
              onReach={loadMore}
            />
          </div>
        )}
      </div>
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
  const missing = complete ? parts.filter((part) => part.media_item_id === null) : [];
  return (
    <div data-testid="series-wall" className={WALL_GRID_POSTER}>
      {interleaveByRelease(items, missing).map((cell) =>
        "part" in cell ? (
          <MissingPartCell
            key={`missing:${cell.part.tmdb_id}`}
            part={cell.part}
            tracked={
              cell.part.subscribed ||
              Boolean(subscriptionOf({ id: String(cell.part.tmdb_id), type: "movie" }))
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
function interleaveByRelease(items: LibraryItem[], missing: SeriesPart[]): SeriesCell[] {
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
function MissingPartCell({ part, tracked }: { part: SeriesPart; tracked: boolean }) {
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
  const labelOf = (pool: { value: string; label: string }[] | undefined, value: string) =>
    pool?.find((row) => row.value === value)?.label ?? "…";
  const groups = DIMS.map(({ key, label }) => {
    const values =
      key === "watch"
        ? filter.watch
          ? [{ value: filter.watch, label: labelOf(facets?.watch, filter.watch) }]
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
      <p className="mt-3 text-sub text-[var(--text-faint)]">自动收录 · 收录本库全部作品</p>
    );
  }

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <span className="text-caption text-[var(--text-faint)]">自动收录</span>
      {groups.map((group, index) => (
        <div key={group.label} className="flex items-center gap-2">
          {/* 维度之间是「且」，维度内是「或」——与库页筛选条同一套语言 */}
          {index > 0 && <span className="text-caption tracking-wide text-white/30">且</span>}
          <span className="glass-row flex h-7 !w-auto items-center gap-1.5 rounded-lg !bg-[var(--glass-fill-active)] !px-2 py-0">
            <span className="rounded bg-black/25 px-1.5 py-0.5 text-caption text-white/40">
              {group.label}
            </span>
            {group.values.map((value, i) => (
              <span key={value.value} className="flex items-center gap-1.5">
                {i > 0 && <span className="text-caption text-white/30">或</span>}
                <span className="text-caption font-semibold text-white">{value.label}</span>
              </span>
            ))}
          </span>
        </div>
      ))}
    </div>
  );
}
