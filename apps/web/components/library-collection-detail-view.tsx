"use client";

import { useCallback, useEffect, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";

import { CollectionOrderPanel } from "@/components/collection-order-panel";
import { useConfirm, usePrompt, useToast } from "@/components/feedback";
import { MoreIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { PosterWall } from "@/components/poster-wall";
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
import { createSubscription } from "@/lib/api/subscriptions";
import { getLibraryFacets, type LibraryFacets, type LibraryItem } from "@/lib/api/libraries";
import { rulesToFilter } from "@/lib/library-filter";
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
  libraryId: number;
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
    if (!collection?.rules?.length) return;
    let alive = true;
    getLibraryFacets(libraryId, rulesToFilter(collection.rules), "all")
      .then((data) => alive && setFacets(data))
      .catch(() => alive && setFacets(null));
    return () => {
      alive = false;
    };
  }, [libraryId, collection?.rules]);

  /** 合集挂在库下面：每一格都落回本库 */
  const ownLibraryId = useCallback(() => libraryId, [libraryId]);

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
      await applyCollectionToLibrary(collection.id, libraryId);
      toast.success("已设为该库的收藏范围");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "设置失败");
    }
  }, [collection, confirm, libraryId, toast]);

  if (error) {
    return (
      <>
        <PageNav title="合集" fallback={{ label: "媒体库", href: `/library/${libraryId}` as Route }} />
        <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">{error}</p>
      </>
    );
  }

  return (
    <>
      <PageNav
        title={collection?.name ?? "合集"}
        fallback={{ label: "媒体库", href: `/library/${libraryId}` as Route }}
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
                  {canManageLibraries && collection.editable && collection.rule_driven && (
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
        {collection && <RuleRow collection={collection} facets={facets} />}
      </div>

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

      {series && series.available && series.total > series.owned_count && (
        <MissingParts
          series={series}
          libraryId={libraryId}
          onSubscribed={(tmdbId) =>
            setSeries((prev) =>
              prev
                ? {
                    ...prev,
                    parts: prev.parts.map((part) =>
                      part.tmdb_id === tmdbId ? { ...part, subscribed: true } : part,
                    ),
                  }
                : prev,
            )
          }
        />
      )}

      <div className="mt-6 max-md:mt-4">
        {items.length === 0 ? (
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
    </>
  );
}

/**
 * 缺片补齐（docs/design/library-series-collections.md 6.5）。
 *
 * **这一块才是系列合集真正的价值。** 只做归类的话，用户装个 Emby 也有；
 * 能告诉他"缺哪两部、点一下就去补"的，只有这个产品。
 *
 * 订阅是现成能力：``title_ref = "tmdb:movie:{id}"``，而 parts 正好给这个 id。
 * 所以这里不需要新的下游链路，只是把两个已有的东西接起来——与剧集的
 * 「补齐缺集」是同一个心智，用户不用学新东西。
 *
 * 分寸：只在详情页出现，**不上卡片**（6.5.2）。已经在追的显示「追踪中」而不是
 * 一颗还能再点一次的按钮。
 */
function MissingParts({
  series,
  libraryId,
  onSubscribed,
}: {
  series: CollectionSeries;
  libraryId: number;
  onSubscribed: (tmdbId: number) => void;
}) {
  const toast = useToast();
  const [pending, setPending] = useState<number | null>(null);
  const missing = series.parts.filter((part) => part.media_item_id === null);
  if (missing.length === 0) return null;

  const subscribe = async (part: SeriesPart) => {
    setPending(part.tmdb_id);
    try {
      await createSubscription({
        title_ref: `tmdb:movie:${part.tmdb_id}`,
        library_id: libraryId,
      });
      onSubscribed(part.tmdb_id);
      toast.success(`已订阅《${part.title}》`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "订阅失败");
    } finally {
      setPending(null);
    }
  };

  return (
    <section className="mt-6 px-6 max-md:mt-4 max-md:px-4">
      <h2 className="text-ui font-medium text-[var(--text-strong)]">
        还缺 {missing.length} 部
      </h2>
      <div className="mt-3 flex flex-wrap gap-3">
        {missing.map((part) => (
          <div
            key={part.tmdb_id}
            className="glass-row flex !w-auto items-center gap-3 rounded-xl !px-3 py-2"
          >
            <div className="min-w-0">
              <p className="truncate text-ui text-[var(--text-strong)]">{part.title}</p>
              <p className="text-caption text-[var(--text-faint)]">
                {part.release_date?.slice(0, 4) ?? "待定"}
              </p>
            </div>
            {part.subscribed ? (
              <span className="shrink-0 text-caption text-[var(--text-faint)]">追踪中</span>
            ) : (
              <button
                type="button"
                onClick={() => void subscribe(part)}
                disabled={pending === part.tmdb_id}
                className="shrink-0 rounded-lg bg-white/10 px-2.5 py-1 text-caption font-medium text-white transition hover:bg-white/20 disabled:opacity-40"
              >
                {pending === part.tmdb_id ? "订阅中…" : "订阅"}
              </button>
            )}
          </div>
        ))}
      </div>
    </section>
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
        作品系列 · 自动收录这个系列的全部作品，按上映顺序排列
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
