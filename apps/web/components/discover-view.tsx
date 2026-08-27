"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { Route } from "next";

import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  GlobeIcon,
  PlusIcon,
  StarIcon,
} from "@/components/icons";
import { MediaRow } from "@/components/media-row";
import { DiscoverRegionFooter } from "@/components/discover-region-footer";
import { DiscoveryFilterControl } from "@/components/discovery-filter-dialog";
import { FilteredDiscoveryView } from "@/components/filtered-discovery-view";
import { PosterImage } from "@/components/poster-image";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import {
  browseDiscoveryCollection,
  collectionHref,
  collectionToRow,
  fetchDiscoveryPage,
  type DiscoveryPageData,
  type DiscoveryPageSection,
} from "@/lib/api/discover";
import { HttpError } from "@/lib/http";
import {
  discoveryFilterCount,
  discoveryFiltersKey,
  discoveryFiltersQuery,
  EMPTY_DISCOVERY_FILTERS,
  type DiscoveryFilters,
} from "@/lib/discovery-filters";
import { useMediaDetail } from "@/lib/media-detail";
import { usePageChrome } from "@/lib/page-chrome";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";
import { useIsMobile } from "@/lib/use-media-query";
import { useTapGuard } from "@/lib/use-tap-guard";
import type {
  MediaItem,
  MediaRowData,
  MediaSource,
  MediaType,
} from "@/lib/media-types";

/**
 * 发现页（发现电影 / 发现剧集）：Netflix 式「Hero 大横幅 + 分类横滚行」。
 *
 * 页面纵向结构：
 *   1. HeroBanner —— 精选影片轮播大横幅（宽幅剧照 + 渐变蒙版 + 标题区 + 操作按钮）
 *   2. 若干 MediaRow —— 「今日热榜（排名变体）/ 热门 / 高分 / …」横滚海报行
 *
 * 渐进加载：先读取 Web 展示清单，再把每个 collectionRef 原样交给领域接口。
 * 页面只知道一个片单应画成 Hero、排名行还是普通行，不再拼装 provider 专属
 * 端点。展示清单与片单预览都做模块级内存缓存，视角切换后可以即时恢复。
 */
const pageCache = new Map<string, DiscoveryPageData>();
const collectionCache = new Map<string, MediaRowData>();

/** 加载失败信息：除文案外带上后端错误码与引导提示，驱动引导式错误态。 */
interface DiscoverErrorInfo {
  message: string;
  /** 后端统一错误码（如 UPSTREAM_UNREACHABLE = 网络级不可达） */
  code?: string;
  /** 后端给的下一步操作提示（如去网络设置配代理） */
  hint?: string;
}

/** 从任意异常提取结构化错误信息（HttpError 携带后端信封的 code/details）。 */
function toErrorInfo(err: unknown): DiscoverErrorInfo {
  if (err instanceof HttpError) {
    const payload = err.details as
      | { code?: string; details?: { service?: string; hint?: string }[] }
      | undefined;
    return {
      message: err.message,
      code: payload?.code,
      hint: payload?.details?.[0]?.hint,
    };
  }
  return { message: (err as Error)?.message || "加载失败，请稍后重试" };
}

export function DiscoverView({
  mediaType,
  source,
  filters,
  currentYear,
}: {
  mediaType: MediaType;
  source: MediaSource;
  filters: DiscoveryFilters;
  /** 服务端确定年份，避免跨年瞬间 SSR 与浏览器水合生成不同选项。 */
  currentYear: number;
}) {
  const router = useRouter();
  const cacheKey = `${mediaType}:${source}`;
  const filterKey = discoveryFiltersKey(filters);
  const filtering = source === "tmdb" && discoveryFilterCount(filters) > 0;
  const scrollRef = useScrollRestoration(`discover:${cacheKey}:${filterKey}`);
  const [page, setPage] = useState<DiscoveryPageData | null>(() => pageCache.get(cacheKey) ?? null);
  // Hero 三态：undefined=加载中（出骨架）、[]=无或失败（收起）、有值=轮播
  const [hero, setHero] = useState<MediaItem[] | undefined>();
  // 每行三态：undefined=加载中（出骨架）、"error"=失败（收起）、有值=渲染
  const [rowsByRef, setRowsByRef] = useState<Record<string, MediaRowData | "error">>({});
  const [error, setError] = useState<DiscoverErrorInfo | null>(null);
  // 重试计数器：点「重试」时 +1，触发 effect 重新拉取
  const [reloadKey, setReloadKey] = useState(0);

  // 页脚切换院线地区后：清掉片单缓存并重拉——「正在热映/即将上映」的内容
  // 随地区而变，留着旧缓存用户会以为切换没生效
  const reloadAfterRegionChange = useCallback(() => {
    pageCache.clear();
    collectionCache.clear();
    setRowsByRef({});
    setHero(undefined);
    setReloadKey((k) => k + 1);
  }, []);

  useEffect(() => {
    if (filtering) return;
    const controller = new AbortController();
    setError(null);
    setHero(undefined);

    const cacheKeyFor = (section: DiscoveryPageSection) =>
      `${section.collectionRef}:${section.previewLimit}`;

    // 已缓存的片单直接进入初始状态，未缓存的保持 undefined（骨架）等待逐项到达。
    const seedRows = (sections: DiscoveryPageSection[]) => {
      const seeded: Record<string, MediaRowData | "error"> = {};
      for (const section of sections) {
        const cached = collectionCache.get(cacheKeyFor(section));
        if (cached) seeded[section.collectionRef] = cached;
      }
      setRowsByRef(seeded);
      return seeded;
    };

    // 展示清单就绪后逐个读取片单：先到先渲染；Hero 失败只收起自身，普通
    // 分区全部失败且没有缓存时，整页进入带网络引导的错误态。
    const loadPage = (pageData: DiscoveryPageData) => {
      const heroSection = pageData.sections.find((section) => section.presentation === "hero");
      const rowSections = pageData.sections.filter((section) => section.presentation !== "hero");
      const seeded = seedRows(rowSections);

      if (heroSection) {
        const cached = collectionCache.get(cacheKeyFor(heroSection));
        if (cached) {
          setHero(cached.items);
        } else {
          browseDiscoveryCollection(heroSection.collectionRef, heroSection.previewLimit, {
            signal: controller.signal,
          })
            .then((collection) => {
              const row = collectionToRow(collection);
              collectionCache.set(cacheKeyFor(heroSection), row);
              if (!controller.signal.aborted) setHero(row.items);
            })
            .catch(() => {
              if (!controller.signal.aborted) setHero([]);
            });
        }
      } else {
        setHero([]);
      }

      const pending = rowSections.filter((section) => !seeded[section.collectionRef]);
      let failed = 0;
      let firstError: DiscoverErrorInfo | null = null;
      for (const section of pending) {
        browseDiscoveryCollection(section.collectionRef, section.previewLimit, {
          signal: controller.signal,
        })
          .then((collection) => {
            const row = collectionToRow(collection);
            collectionCache.set(cacheKeyFor(section), row);
            if (!controller.signal.aborted) {
              setRowsByRef((prev) => ({ ...prev, [section.collectionRef]: row }));
            }
          })
          .catch((err: unknown) => {
            if (controller.signal.aborted) return;
            firstError ??= toErrorInfo(err);
            failed += 1;
            setRowsByRef((prev) => ({ ...prev, [section.collectionRef]: "error" }));
            if (failed === pending.length && pending.length === rowSections.length) {
              setError(firstError);
            }
          });
      }
    };

    const cachedPage = pageCache.get(cacheKey);
    setPage(cachedPage ?? null);
    if (cachedPage) {
      loadPage(cachedPage);
    } else {
      fetchDiscoveryPage(mediaType, source, { signal: controller.signal })
        .then((data) => {
          pageCache.set(cacheKey, data);
          if (controller.signal.aborted) return;
          setPage(data);
          loadPage(data);
        })
        .catch((err: unknown) => {
          if (!controller.signal.aborted) setError(toErrorInfo(err));
        });
    }
    return () => controller.abort();
  }, [cacheKey, filtering, mediaType, reloadKey, source]);

  const switchSource = useCallback(
    (nextSource: MediaSource) => {
      if (nextSource === source) return;
      router.push(`/discover/${mediaType}?source=${nextSource}` as Route);
    },
    [mediaType, router, source],
  );

  const applyFilters = useCallback(
    (nextFilters: DiscoveryFilters) => {
      const query = discoveryFiltersQuery(nextFilters);
      router.push(`/discover/${mediaType}${query ? `?${query}` : ""}` as Route);
    },
    [mediaType, router],
  );

  const controls = useMemo(
    () => (
      <div className="flex items-center gap-2">
        {source === "tmdb" && (
          <DiscoveryFilterControl
            mediaType={mediaType}
            filters={filters}
            currentYear={currentYear}
            onApply={applyFilters}
          />
        )}
        <SourceSwitcher value={source} onChange={switchSource} />
      </div>
    ),
    [applyFilters, currentYear, filters, mediaType, source, switchSource],
  );

  // 发现页是侧栏一级入口，没有 PageNav，数据源切换若自己吸一条顶栏，窄屏上
  // 就会摞在全局顶栏底下变成两排 header。移动端改为挂进全局顶栏那一行
  // （字标与搜索之间本来就空着），桌面端维持原来的吸顶工具栏不变。
  const chrome = usePageChrome();
  const isMobile = useIsMobile();
  const setTopBarActions = chrome?.setTopBarActions;
  useEffect(() => {
    if (!isMobile || !setTopBarActions) return;
    return setTopBarActions(controls);
  }, [controls, isMobile, setTopBarActions]);

  const toolbar = isMobile ? null : (
    <div className="sticky top-0 z-20 flex items-center justify-end px-6 pb-3 pt-7">
      {controls}
    </div>
  );

  if (filtering) {
    return (
      <div
        ref={scrollRef}
        data-scroll-root
        className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10 max-md:pt-4"
      >
        {toolbar}
        <FilteredDiscoveryView
          key={filterKey}
          mediaType={mediaType}
          filters={filters}
          onClear={() => applyFilters(EMPTY_DISCOVERY_FILTERS)}
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-1 flex-col max-md:pt-4">
        {toolbar}
        <DiscoverError error={error} onRetry={() => setReloadKey((k) => k + 1)} />
      </div>
    );
  }
  if (!page) {
    return (
      <div className="flex flex-1 flex-col max-md:pt-4">
        {toolbar}
        <DiscoverSkeleton />
      </div>
    );
  }
  return (
    <div
      ref={scrollRef}
      className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10 max-md:pt-4"
    >
      {toolbar}
      {/* Hero 区：展示清单声明 Hero 时先占位，数据到达后换成轮播。 */}
      {page.sections.some((section) => section.presentation === "hero") && hero === undefined && (
        <div className="px-6 max-md:px-4">
          <HeroSkeleton />
        </div>
      )}
      {hero && hero.length > 0 && (
        <div className="px-6 max-md:px-4">
          <HeroBanner items={hero} />
        </div>
      )}
      <div className="mt-8 space-y-8">
        {page.sections.filter((section) => section.presentation !== "hero").map((section) => {
          const row = rowsByRef[section.collectionRef];
          // 失败或条目太少（空 items）的行整行收起
          if (row === "error") return null;
          if (row && row.items.length === 0) return null;
          if (!row) return <RowSkeleton key={section.collectionRef} stub={section} />;
          const href = section.supportsFullListing
            ? collectionHref(section.collectionRef)
            : undefined;
          const moreHref = href as Route | undefined;
          return (
            <MediaRow
              key={row.id}
              row={moreHref ? { ...row, items: row.items.slice(0, 10) } : row}
              moreHref={moreHref}
            />
          );
        })}
      </div>
      {/* 院线地区就地设置：只在有院线榜单的电影 TMDB 视角出现 */}
      {mediaType === "movie" && source === "tmdb" && (
        <DiscoverRegionFooter onChanged={reloadAfterRegionChange} />
      )}
    </div>
  );
}

/** 数据源视角切换：两个视角分别缓存，来回切换不会重复请求。 */
function SourceSwitcher({
  value,
  onChange,
}: {
  value: MediaSource;
  onChange: (source: MediaSource) => void;
}) {
  return (
      <div className="flex shrink-0 rounded-full border border-white/10 bg-black/35 p-1 backdrop-blur-xl">
        {(["tmdb", "douban"] as const).map((source) => (
          <button
            key={source}
            type="button"
            onClick={() => onChange(source)}
            className={`rounded-full px-4 py-1.5 text-sub font-semibold transition ${
              value === source
                ? "bg-white/15 text-white shadow-sm"
                : "text-[var(--text-muted)] hover:text-white"
            }`}
          >
            {source === "tmdb" ? "TMDB" : "豆瓣"}
          </button>
        ))}
      </div>
  );
}

/** 布局到达前的整页骨架（Hero 大块 + 两行海报）；布局是毫秒级的，一闪而过。 */
function DiscoverSkeleton() {
  return (
    <div className="flex-1 overflow-hidden px-6 pb-10 max-md:px-4" aria-busy="true" aria-label="发现页加载中">
      <HeroSkeleton />
      {[0, 1].map((row) => (
        <div key={row} className="mt-10">
          <div className="h-4 w-28 animate-pulse rounded bg-white/[0.08]" />
          <div className="mt-4 flex gap-4 overflow-hidden">
            <RowItemsSkeleton />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Hero 大横幅的占位块（与真实 Hero 同尺寸，数据到达后原位替换不跳版）。 */
function HeroSkeleton() {
  return (
    <div className="h-[46vh] min-h-[320px] animate-pulse rounded-2xl bg-white/[0.05] ring-1 ring-white/10 max-md:h-[38vh] max-md:min-h-[230px]" />
  );
}

/**
 * 单行加载骨架：行标题实显（来自布局），海报区闪烁占位。
 * 标题栏与横滚区的留白复刻 MediaRow 的布局，数据到达后原位替换不跳版；
 * 这一行行「亮着名字等数据」的骨架就是页面的分区加载进度。
 */
function RowSkeleton({ stub }: { stub: { title: string } }) {
  return (
    <section aria-busy="true" aria-label={`「${stub.title}」加载中`}>
      <div className="mb-3 px-6 max-md:mb-2 max-md:px-4">
        <h3 className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
          {stub.title}
        </h3>
      </div>
      <div className="flex gap-4 overflow-hidden px-6 pb-1 pt-1 max-md:gap-3 max-md:px-4">
        <RowItemsSkeleton />
      </div>
    </section>
  );
}

/** 一排海报占位卡，与真实海报行同一规格。 */
function RowItemsSkeleton() {
  return (
    <>
      {Array.from({ length: 8 }, (_, i) => (
        <div
          key={i}
          className="aspect-[2/3] w-[152px] shrink-0 animate-pulse rounded-2xl bg-white/[0.05] max-md:w-[126px] xl:w-[164px]"
        />
      ))}
    </>
  );
}

/** 加载失败态：展示后端的中文错误信息（如未配置 TMDB Key 的引导）并提供重试。
 *
 * 网络级不可达（UPSTREAM_UNREACHABLE，含熔断快速失败）渲染引导式错误：
 * 说明原因 + 后端 hint + 「前往网络设置」按钮，替代干等骨架屏。
 */
function DiscoverError({ error, onRetry }: { error: DiscoverErrorInfo; onRetry: () => void }) {
  const unreachable = error.code === "UPSTREAM_UNREACHABLE";
  return (
    <div className="flex flex-1 items-center justify-center px-6">
      <div className="max-w-md rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] p-8 text-center backdrop-blur-xl">
        {unreachable && (
          <span className="icon-chip mx-auto mb-4 flex size-11 !rounded-2xl">
            <GlobeIcon className="size-5" />
          </span>
        )}
        <p className="text-body-lg font-semibold text-[var(--text)]">
          {unreachable ? "无法连接数据源" : "发现页加载失败"}
        </p>
        <p className="mt-2 break-all text-ui leading-6 text-[var(--text-muted)]">{error.message}</p>
        {unreachable && error.hint && (
          <p className="mt-2 text-sub leading-6 text-[var(--text-faint)]">{error.hint}</p>
        )}
        <div className="mt-5 flex items-center justify-center gap-3">
          {unreachable && (
            <Link
              href={"/settings/network" as Route}
              className="btn-accent flex h-9 items-center rounded-full px-5 text-ui font-semibold"
            >
              前往网络设置
            </Link>
          )}
          <button
            type="button"
            onClick={onRetry}
            className={`${unreachable ? "btn-glass" : "btn-accent"} h-9 rounded-full px-5 text-ui font-semibold`}
          >
            重试
          </button>
        </div>
      </div>
    </div>
  );
}

/** Hero 轮播间隔（毫秒） */
const HERO_INTERVAL = 8000;

/**
 * Hero 大横幅：精选影片自动轮播。
 * 所有帧常驻 DOM 叠放，靠 opacity 交叉淡入淡出（避免切换时图片重新加载闪白）；
 * 文字区跟随当前帧一起淡入。手动切换（桌面悬停浮现的两侧箭头 / 触屏横向
 * 滑动 / 右下角圆点）都会重置自动轮播计时。
 * 图片按需装载：帧壳常驻，但 w1280 大图只有轮到（当前帧/下一帧）才写入 src，
 * 首屏不必一次下载解码全部 6 张；已展示过的帧保持已加载，交叉淡出不闪白。
 */
function HeroBanner({ items }: { items: MediaItem[] }) {
  const [index, setIndex] = useState(0);
  // 触屏滑动切换的起点（无悬停设备不显示箭头，滑动是唯一的大面积切换手势）
  const touchStart = useRef<{ x: number; y: number } | null>(null);

  const switchSlide = (offset: number) => {
    setIndex((current) => (current + offset + items.length) % items.length);
  };

  useEffect(() => {
    if (items.length <= 1) return;
    const timer = setInterval(() => {
      // 后台标签页不推进轮播：推进了也没人看，白白解码下一张大图并重渲染
      if (document.hidden) return;
      setIndex((i) => (i + 1) % items.length);
    }, HERO_INTERVAL);
    return () => clearInterval(timer);
    // index 作为依赖：手动切换后重置轮播计时，避免刚点完就被自动切走
  }, [items.length, index]);

  const next = (index + 1) % items.length;
  return (
    <div
      className="group relative h-[46vh] min-h-[320px] w-full overflow-hidden rounded-2xl shadow-[0_24px_70px_-18px_rgba(0,0,0,0.62)] ring-1 ring-white/10 max-md:h-[38vh] max-md:min-h-[230px]"
      onTouchStart={(e) => {
        const t = e.touches[0];
        touchStart.current = { x: t.clientX, y: t.clientY };
      }}
      onTouchEnd={(e) => {
        const start = touchStart.current;
        touchStart.current = null;
        if (!start || items.length <= 1) return;
        const t = e.changedTouches[0];
        const dx = t.clientX - start.x;
        const dy = t.clientY - start.y;
        // 水平位移够大且明显横向才算切换手势：纵向滚动起手、普通点按
        // （tapGuard 负责进详情）都不受影响
        if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.5) {
          switchSlide(dx < 0 ? 1 : -1);
        }
      }}
    >
      {items.map((item, i) => (
        <HeroSlide key={item.id} item={item} active={i === index} preload={i === index || i === next} />
      ))}

      {/* 手动切换按钮：桌面悬停浮现。无悬停设备直接不渲染出来——常显会压住
          标题文字（截图实锤），"点一下再浮现"又与整块点击进详情冲突，触屏
          的切换交给横向滑动与右下圆点。 */}
      {items.length > 1 && (
        <>
          <button
            type="button"
            aria-label="切换到上一部"
            onClick={() => switchSlide(-1)}
            className="absolute left-3 top-1/2 z-10 flex size-11 -translate-y-1/2 items-center justify-center rounded-full bg-black/35 text-white/80 opacity-0 ring-1 ring-white/15 backdrop-blur-sm transition hover:bg-black/55 hover:text-white group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 [@media(hover:none)]:hidden"
          >
            <ChevronLeftIcon className="size-6" />
          </button>
          <button
            type="button"
            aria-label="切换到下一部"
            onClick={() => switchSlide(1)}
            className="absolute right-3 top-1/2 z-10 flex size-11 -translate-y-1/2 items-center justify-center rounded-full bg-black/35 text-white/80 opacity-0 ring-1 ring-white/15 backdrop-blur-sm transition hover:bg-black/55 hover:text-white group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 [@media(hover:none)]:hidden"
          >
            <ChevronRightIcon className="size-6" />
          </button>
        </>
      )}

      {/* 轮播指示点 */}
      {items.length > 1 && (
        <div className="absolute bottom-4 right-5 z-10 flex gap-1.5">
          {items.map((item, i) => (
            <button
              key={item.id}
              type="button"
              aria-label={`切换到《${item.title}》`}
              onClick={() => setIndex(i)}
              className={`h-1.5 rounded-full transition-all duration-300 ${
                i === index ? "w-5 bg-white/85" : "w-1.5 bg-white/30 hover:bg-white/55"
              }`}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function HeroSlide({
  item,
  active,
  preload,
}: {
  item: MediaItem;
  active: boolean;
  /** 是否该装载大图（当前帧或下一帧）；一旦装载过就保持，淡出时不闪白 */
  preload: boolean;
}) {
  const { open } = useMediaDetail();
  const { canSubscribe, open: openSubscribe, subscriptionOf } = useSubscribeEntry();
  // 该影片是否已有订阅（数据来自 SubscribeEntryProvider 的全站订阅列表，Hero 自身不发请求）
  const existingSub = subscriptionOf(item);
  // 「粘性」装载：轮到过一次就永久保留 src（浏览器已缓存，重复挂载无成本）
  const [revealed, setRevealed] = useState(preload);
  useEffect(() => {
    if (preload) setRevealed(true);
  }, [preload]);
  // 整块 Hero 就是进详情的入口（与海报卡片「点海报进详情」一致，不再另设「更多信息」键）。
  // Hero 占满首屏，手机上「向下滑看海报墙」几乎必然从这块起手，所以点击要过一遍误触判定。
  const tapGuard = useTapGuard(() => open(item));
  const subscribeTapGuard = useTapGuard(() => void openSubscribe(item));
  return (
    <div
      aria-hidden={!active}
      // 非当前帧虽然透明但仍占满同一块区域，必须关掉命中测试，否则点击可能落到它身上
      role={active ? "button" : undefined}
      tabIndex={active ? 0 : -1}
      aria-label={`查看《${item.title}》详情`}
      {...tapGuard}
      onKeyDown={(e) => {
        // 只认落在 Hero 自身上的回车/空格；内部订阅键的按键由它自己处理，
        // 否则一次回车会同时触发订阅弹层和详情页
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open(item);
        }
      }}
      className={`absolute inset-0 transition-opacity duration-700 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] ${
        active ? "z-[1] cursor-pointer opacity-100" : "pointer-events-none z-0 opacity-0"
      }`}
    >
      {/* 宽幅剧照 + 双层渐变蒙版：左侧压暗保文字可读，底部渐隐融入页面 */}
      <PosterImage
        src={revealed ? item.backdropUrl : undefined}
        alt={`${item.title} 剧照`}
        className={`absolute inset-0 size-full object-cover object-top transition-transform duration-[9000ms] ease-linear ${
          active ? "scale-[1.06]" : "scale-100"
        }`}
      />
      <div className="absolute inset-0 bg-gradient-to-r from-[rgba(7,9,14,0.88)] via-[rgba(7,9,14,0.42)] to-transparent" />
      <div className="absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-[rgba(7,9,14,0.72)] to-transparent max-md:h-3/4 max-md:from-[rgba(7,9,14,0.9)]" />

      {/* 文字与操作区：随当前帧轻微上移淡入 */}
      <div
        className={`absolute inset-0 flex max-w-xl flex-col justify-end p-7 transition-all delay-150 duration-500 ease-out max-md:p-4 sm:p-9 ${
          active ? "translate-y-0 opacity-100" : "translate-y-3 opacity-0"
        }`}
      >
        <p className="text-caption font-semibold uppercase tracking-[0.22em] text-[var(--accent-2)]">
          今日精选 · {item.type === "movie" ? "电影" : "剧集"}
        </p>
        <h2 className="text-on-image mt-2 text-[34px] font-bold leading-[1.1] tracking-[-0.02em] text-white max-md:mt-1 max-md:text-[23px] sm:text-[40px]">
          {item.title}
        </h2>
        <p className="text-on-image mt-1 truncate text-ui text-white/55 max-md:text-caption">{item.originalTitle}</p>

        {/* 元信息行：评分 / 年份 / 类型 / 规模 / 质量徽章（空字段不占位） */}
        <div className="tnum mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sub text-white/80">
          {item.rating > 0 && (
            <span className="flex items-center gap-1 font-semibold text-white">
              <StarIcon className="size-3.5 text-[var(--warn)]" />
              {item.rating.toFixed(1)}
            </span>
          )}
          <span>{item.year}</span>
          {item.genres.length > 0 && <span>{item.genres.join(" / ")}</span>}
          {item.extent && <span>{item.extent}</span>}
          {item.libraryStatus && (
            <span className="flex items-center gap-1.5 text-emerald-300/90">
              <span className="size-1.5 rounded-full bg-[var(--ok)]" />
              在库
            </span>
          )}
          {item.badges.length > 0 && (
            <span className="flex gap-1.5">
              {item.badges.map((b) => (
                <span
                  key={b}
                  className="rounded border border-white/25 px-1.5 py-px text-micro font-semibold tracking-wide text-white/85"
                >
                  {b}
                </span>
              ))}
            </span>
          )}
        </div>

        <p className="text-on-image mt-3 line-clamp-2 max-w-lg text-ui leading-6 text-white/75 max-md:mt-2 max-md:text-sub max-md:leading-5 sm:line-clamp-3">
          {item.overview}
        </p>

        {/* 订阅入口：文案/图标/行为与海报卡片完全一致（已订阅则切成状态键，点击进订阅管理）。
            它嵌在「整块进详情」的点击区里，所以点击必须 stopPropagation，
            否则订阅弹层弹出的同时详情页也会被打开。 */}
        <div className="mt-5 flex flex-wrap items-center gap-3 max-md:mt-3.5 max-md:gap-2">
          {canSubscribe && <button
            type="button"
            aria-label={existingSub ? `管理《${item.title}》的订阅` : `订阅影片《${item.title}》`}
            onPointerDown={subscribeTapGuard.onPointerDown}
            onPointerUp={subscribeTapGuard.onPointerUp}
            onPointerCancel={subscribeTapGuard.onPointerCancel}
            onClick={(e) => {
              e.stopPropagation();
              subscribeTapGuard.onClick(e);
            }}
            className={
              existingSub
                ? "flex h-10 items-center gap-2 rounded-full bg-white/[0.18] px-5 text-ui font-semibold text-white/90 backdrop-blur-md transition-colors hover:bg-white/[0.26]"
                : "btn-accent flex h-10 items-center gap-2 rounded-full px-5 text-ui font-semibold"
            }
          >
            {existingSub ? (
              <>
                <CheckIcon className="size-4 text-[var(--ok)]" />
                已订阅
              </>
            ) : (
              <>
                <PlusIcon className="size-4" />
                订阅影片
              </>
            )}
          </button>}
        </div>
      </div>
    </div>
  );
}
