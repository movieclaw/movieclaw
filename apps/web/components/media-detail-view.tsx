"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { Route } from "next";

import {
  ArrowLeftIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  FolderIcon,
  PhotoIcon,
  BellIcon,
  PlayIcon,
  SearchIcon,
  StarIcon,
  XIcon,
} from "@/components/icons";
import { CastRow } from "@/components/cast-row";
import { HScroller } from "@/components/h-scroller";
import { Modal } from "@/components/modal";
import { PageNav } from "@/components/page-nav";
import { ImageLightbox, type LightboxAction } from "@/components/image-lightbox";
import { MediaRow } from "@/components/media-row";
import { PosterImage } from "@/components/poster-image";
import { SubscribeDialog, type SubscribeTarget } from "@/components/subscribe-dialog";
import {
  fetchDiscoveredTitleDetails,
  titleRef,
  type MediaDetailData,
  type MediaImage,
  type MediaVideo,
} from "@/lib/api/discover";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { useBackNavigation } from "@/lib/back-navigation";
import { useBackdrop } from "@/lib/backdrop";
import { buildDiscoveryReturnPath } from "@/lib/discovery-return-path";
import { useDoubanAppHref } from "@/lib/douban-app-link";
import { getMediaSeed } from "@/lib/media-detail";
import { usePageTitle } from "@/lib/use-page-title";
import { useIsMobile } from "@/lib/use-media-query";
import { usePermissions } from "@/lib/permissions";
import type { MediaSource, MediaType } from "@/lib/media-types";
import {
  subscriptionProgressNote,
  subscriptionStatusMeta,
} from "@/lib/subscription-ui";

/**
 * 影片详情页：点击任意海报后，主内容区整体切换为该影片的详情。
 *
 * 骨架与媒体库条目详情页（LibraryItemDetailView）保持一致——同样是「影片详情」，
 * 用户不该因为这部片在不在库里就换一套阅读结构。两页的差别只在**信息来源**：
 * 那边是本地刮削成果与文件本体（我拥有的这份拷贝是什么），这边是 TMDB / 豆瓣
 * 的在线信息（这部片本身是什么），因此这边多出剧照墙与相似推荐，那边多出
 * 片源规格与文件区。
 *
 * 页面纵向结构：
 *   1. 沉浸背景 —— 进入本页把全站背景临时换成该片剧照（setOverrideBackdrop），
 *      桌面上页面本身不画 Hero 图层：大图直出、零边界，侧栏与外壳留白一起透出；
 *      手机上竖屏放不下横版剧照，改由页内 Hero 呈现（与媒体库条目详情页同一套，
 *      见下方 mobileHeroSrc）。没有横幅剧照时退回海报作氛围图。
 *   2. 氛围留白 + 渐变内容层 —— 渐变从标题上方开始压暗，并在基础信息之后落成纯黑。
 *   3. 头部信息区 —— 标题 / 核心元信息 / 地区、语言与类型 / 上映日期 / 订阅操作，
 *      已订阅的影片额外显示订阅状态与追更进度。
 *   4. 剧情简介 —— 四行折叠，只有真实溢出才显示展开入口。
 *   5. 演职员 —— 导演 / 主创与演员合并为同一条人物横滚条。
 *   6. 预告片 —— YouTube 预告与花絮，点开在弹层里内嵌播放。
 *   7. 剧照与海报 —— Apple TV+ 式横滚图片条，胶囊标签切换类型，点图开灯箱。
 *   8. 系列电影 —— 展示完整系列。
 *   9. 相似推荐 —— 复用 MediaRow，点击可继续跳详情。
 *  10. 相关链接 —— 与媒体库详情一致，外部词条统一弱化到底部。
 *
 * 数据分两段呈现：点卡片时已有的列表字段（标题/海报/简介）立即渲染，
 * 地区、语言、上映日期、演职员、系列电影与相似推荐从稳定 titleRef 对应的详情接口
 * 异步补齐（同时回填片长/季数）。
 */
export function MediaDetailView({
  type,
  id,
  source = "tmdb",
}: {
  type?: MediaType;
  id: string;
  source?: MediaSource;
}) {
  const { canSearch } = usePermissions();
  const [detail, setDetail] = useState<MediaDetailData | null>(null);
  // 详情拉取失败状态：仅在无 seed（硬刷新/分享直达）时才需要整页兜底
  const [loadFailed, setLoadFailed] = useState(false);
  // 该条目的订阅：与海报卡片同一份全站订阅状态（subscribe-entry 收口），
  // 订阅/取消后 refresh 一次，详情页与所有卡片同步更新
  const {
    canSubscribe,
    subscriptionOf,
    refresh: refreshSubscriptions,
  } = useSubscribeEntry();
  const sub = subscriptionOf({ id, source, type: type ?? "movie" });
  // 订阅弹层的打开参数；null = 关闭
  const [subscribeTarget, setSubscribeTarget] = useState<SubscribeTarget | null>(null);
  // 站内点卡片跳转时预存的列表字段（标题/海报/简介），用于首屏零白屏；
  // 硬刷新 / 分享链接直达时为空，此时全靠 Discover 详情接口拉取。
  const listItem = getMediaSeed(source, id);
  const fallbackType = listItem?.type ?? type ?? "movie";
  const navFallback = {
    label: fallbackType === "tv" ? "发现剧集" : "发现电影",
    href: `/discover/${fallbackType}` as Route,
  };
  const back = useBackNavigation(navFallback.href);
  // 站内跳转优先沿用列表响应给出的稳定引用；硬刷新没有 seed 时，详情 URL
  // 本身已携带等价的来源/类型/ID，再据此恢复引用。
  const reference = listItem?.titleRef ?? titleRef(source, type ?? "movie", id);

  useEffect(() => {
    setDetail(null);
    setLoadFailed(false);
    const controller = new AbortController();
    fetchDiscoveredTitleDetails(reference, {
      signal: controller.signal,
    })
      .then((data) => {
        if (!controller.signal.aborted) setDetail(data);
      })
      .catch(() => {
        // 有 seed 时详情拉取失败不打断页面：列表字段仍可完整展示；
        // 无 seed（直达）时则没有任何可渲染内容，标记失败以显示兜底。
        if (!controller.signal.aborted) setLoadFailed(true);
      });
    return () => controller.abort();
  }, [reference]);

  // 详情接口回填过 extent（片长/季数）等字段，未返回前先用列表字段渲染
  const item = detail?.item ?? listItem;
  usePageTitle(item?.title);

  // 沉浸背景：进入本页把全站背景临时换成该片剧照（侧栏、外壳留白一起透出，
  // 不再只铺详情卡片的局部），离开即恢复用户配置的背景——与媒体库条目详情页
  // 同一条链路（见 lib/backdrop.tsx 的 setOverrideBackdrop）。没有横幅剧照时
  // 退回海报，覆盖层自己会铺满作氛围色。豆瓣条目同样换：它的图确实比 TMDB 小，
  // 但手机上页面看到的是下面的页内 Hero、全站背景被黑底整个挡住，桌面上一张
  // 偏软的剧照也好过「这部片的页面配着另一部片的壁纸」的断裂感。
  const { setOverrideBackdrop } = useBackdrop();
  const isMobile = useIsMobile();
  const immersiveUrl = item?.backdropUrl || item?.posterUrl || "";
  // 手机也换全站背景，但页面本身不靠它显示：横版剧照铺满又高又窄的整屏只能按高度放大、
  // 从正中裁一条竖条，所以手机上看到的剧照是页内 Hero（mobileHeroSrc），滚动容器铺黑把
  // 全站背景整个挡住。仍然要换，是因为侧栏的液态玻璃折射的就是全站背景
  // （useBackdrop().backdrop）：不换的话展开侧栏透出的是用户自己的壁纸，与页面上的剧照
  // 断成两截。两处是同一个 URL，浏览器只下载一次
  useEffect(() => {
    if (!immersiveUrl) return;
    setOverrideBackdrop(immersiveUrl);
    return () => setOverrideBackdrop(null);
  }, [immersiveUrl, setOverrideBackdrop]);
  // 手机 Hero 用剧照，与桌面同一张：海报在外面的海报墙上已经看过了，进详情页要的是另一张
  // 画面。没有剧照时才退回海报（豆瓣榜单条目、极少数没有横幅图的 TMDB 条目）
  const mobileHeroSrc = immersiveUrl;
  // Hero 的框比剧照高：宽度撑满、高约 1.15 倍宽（封顶 62svh，390px 宽的屏上约 448px）。
  // 横版剧照按高度铺满、上下不裁，左右裁掉两边、居中留下人物主体——比按宽度塞下整张
  // （只有 219px 高）多一倍画面，又不像铺满整屏那样只剩中间一条
  const mobileHeroHeight = "min(115vw, 62svh)";
  const showMobileHero = isMobile && mobileHeroSrc !== "";

  // 豆瓣外链的移动端 App 直跳：无悬停设备把「豆瓣」外链换成官方分发地址，
  // 装了豆瓣 App 直接拉起进词条页（桌面/未命中时为 null，回落网页地址）
  const doubanAppHref = useDoubanAppHref(source === "douban" ? id : null);

  // 兜底态也必须渲染 PageNav——它向外壳登记「本页自带顶栏」，否则移动端的
  // 全局顶栏（☰ + logo）会在数据到达前先显示、随后又消失，顶部闪一下；
  // 顺带让用户在转圈期间就有返回键可点。
  // 无 seed 且详情尚未到达：直达链接的加载态（或失败兜底）。
  if (!item) {
    return (
      <div className="flex h-full flex-col">
        {/* 当前页标题未知，留空——只为立起返回键并认领顶栏 */}
        <PageNav title="" fallback={navFallback} />
        <DetailFallback failed={loadFailed} onBack={back} />
      </div>
    );
  }

  const info = detail?.info;
  const collection = detail?.collection;
  const related = detail?.related ?? [];
  const libraryLinks = detail?.libraryLinks ?? [];
  // 发现详情路由是媒体库页可安全返回的唯一来源；不从标题或媒体库数据反推条目。
  const libraryReturnTo = buildDiscoveryReturnPath(source, type ?? item.type, id);

  const isMovie = item.type === "movie";
  // 电影入库即完成：再摆「订阅追踪 / 搜索资源」等于邀请用户重下一遍已有的片子，
  // 隐藏后由上方的「在库」信息条接手（点库名直达条目，想看就去看）。
  // 剧集不适用同一条规则——在库不等于收齐，缺集与未来新季仍要靠订阅追更或
  // 手动找资源，所以剧集在库时两个按钮照常显示；口径与海报卡的
  // libraryInventoryAction（电影 none、剧集 follow/backfill）一致。
  const ownedMovie = isMovie && libraryLinks.length > 0;
  // 已订阅的在库电影仍保留状态键：它是「管理 / 取消订阅」入口，不是再订一次的号召。
  const showSubscribeButton = canSubscribe && (Boolean(sub) || !ownedMovie);
  const showSearchButton = canSearch && !ownedMovie;
  const directorCast =
    info?.directorCredits.length
      ? info.directorCredits.map((director) => ({
          ...director,
          credit: isMovie ? "导演" : "主创",
        }))
      : [...new Set(info?.directors ?? [])].map((name) => ({
          name,
          credit: isMovie ? "导演" : "主创",
        }));
  const people = info ? [...directorCast, ...info.cast] : [];

  /** 打开订阅弹层：稳定引用原样交给订阅 API，页面不再拆解来源与外部 ID。 */
  const openSubscribe = () =>
    setSubscribeTarget({
      titleRef: reference,
      kind: item.type,
      title: item.title,
      year: item.year || undefined,
    });

  return (
    // rounded-2xl + overflow 裁切：顶部剧照渐变到纯黑内容板，方角会与
    // 全站「浮起圆角卡片」的形状语言冲突——按侧栏同规格圆角收尾。
    // max-md:rounded-none：圆角只在桌面成立（外壳 p-3.5 的留白托着卡片）；
    // 窄屏通栏满屏，圆角会直接压在屏幕边上，把吸顶顶栏裁成一块贴在屏幕顶上的
    // 圆角色块——与 library-item-detail-view 同一处理。
    <div
      className={`detail-ambient scroll-thin scroll-safe relative isolate h-full overflow-y-auto rounded-2xl max-md:rounded-none ${
        showMobileHero ? "detail-ambient--hero" : ""
      }`}
    >
      {/* 桌面没有任何 Hero 图层：全站背景此刻就是本片剧照（沉浸覆盖 + 本页豁免
          全局蒙版，见 app-shell 的 isHome），大图直出、零边界；.detail-ambient
          在滚动容器上铺「透明 → 纯黑」的渐变板托住下方内容（见 globals.css）。
          顶栏首屏只有一颗返回键浮在剧照上。 */}
      <PageNav title={item.title} fallback={navFallback} />

      {/* 手机 Hero（剧照）：宽度撑满，从状态栏底下起铺（绝对定位在滚动内容顶端，PageNav
          的返回键与吸顶雾层浮在它上面），随内容一起滚走，不固定在背景上。顶部一抹暗让状态栏
          与返回键落在亮图上也读得清；底部从中段开始压暗，到底边落成纯黑，与下方黑底无缝接上 */}
      {showMobileHero && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 z-0 overflow-hidden"
          style={{ height: mobileHeroHeight }}
        >
          <img src={mobileHeroSrc} alt="" decoding="async" className="size-full object-cover object-center" />
          <div className="absolute inset-x-0 top-0 h-28 bg-gradient-to-b from-black/45 to-transparent" />
          <div className="absolute inset-x-0 bottom-0 h-[55%] bg-gradient-to-b from-transparent via-black/55 to-black" />
        </div>
      )}

      {/* 氛围留白：这一段什么都不放，让剧照完整呼吸。手机有 Hero 时 = Hero 高度减去吸顶
          顶栏的占位（52px + 安全区）与片名压进图里的那一截（150px），片名与信息落在剧照
          底部的渐变上；视口很矮时减到负数就不留白 */}
      <div
        className={showMobileHero ? undefined : "h-[30vh] min-h-[180px] max-md:h-[22vh] max-md:min-h-[120px]"}
        style={
          showMobileHero
            ? { height: `max(0px, calc(${mobileHeroHeight} - 52px - var(--safe-top) - 150px))` }
            : undefined
        }
      />

      {/* 内容层：-mt-28/pt-28 与 .detail-ambient 的渐变起点对齐——渐变从标题
          上方开始压暗，基础信息附近已接近纯黑，下面保持全黑。 */}
      <div className="relative z-10 -mt-28 pb-12 pt-28">
      {/* —— 3. 头部信息区 —— */}
      <div className="relative z-10 px-12 pt-6 max-md:px-4 max-md:pt-3">
        <div className="min-w-0 max-w-5xl pb-1">
          {/* break-words：未识别条目的标题就是文件名（Some.Movie.2023.2160p…），
              整串无空格，不允许断词就会横向撑开整页 */}
          <h1 className="text-on-image break-words text-[42px] font-bold leading-[1.1] tracking-[-0.02em] text-white max-md:text-[28px]">
            {item.title}
          </h1>

          {/* 元信息行与媒体库详情保持同一层级：评分 / 年份 / 片长或季数 / 质量。 */}
          <div className="tnum mt-3.5 flex flex-wrap items-center gap-x-3 gap-y-2 text-ui text-white/80 max-md:mt-2 max-md:gap-x-2 max-md:text-sub">
            {/* 评分 0 = 数据源暂无评分（新片/小众条目），不能照原样渲染成「0.0」
                ——那读起来是「烂到 0 分」。海报卡片与条目详情页都是这个口径 */}
            {item.rating > 0 && (
              <span className="flex items-center gap-1.5">
                <StarIcon className="size-4 text-[var(--warn)]" />
                <span className="text-title-sm font-bold text-white">{item.rating.toFixed(1)}</span>
              </span>
            )}
            {!!item.year && (
              <span className="flex items-center gap-3 max-md:gap-2">
                {item.rating > 0 && <span aria-hidden="true">·</span>}
                <span>{item.year}</span>
              </span>
            )}
            {item.extent && (
              <span className="flex items-center gap-3 max-md:gap-2">
                {(item.rating > 0 || !!item.year) && <span aria-hidden="true">·</span>}
                <span>{item.extent}</span>
              </span>
            )}
            {item.badges.length > 0 && (
              <span className="flex items-center gap-3 max-md:gap-2">
                {(item.rating > 0 || !!item.year || !!item.extent) && (
                  <span aria-hidden="true">·</span>
                )}
                <span>{item.badges.join(" / ")}</span>
              </span>
            )}
          </div>
          {(info?.country || info?.language || item.genres.length > 0) && (
            <p className="text-on-image mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-ui leading-6 text-white/72 max-md:mt-2 max-md:text-sub">
              {info && (info.country || info.language) && (
                <span className="whitespace-nowrap text-white/65">
                  {[info.country, info.language].filter(Boolean).join(" · ")}
                </span>
              )}
              {item.genres.length > 0 && (
                <span className="flex min-w-0 items-start gap-3">
                  {info && (info.country || info.language) && (
                    <span aria-hidden="true" className="shrink-0 text-white/25">
                      ｜
                    </span>
                  )}
                  <span>{item.genres.join(" · ")}</span>
                </span>
              )}
            </p>
          )}
          {info?.released && (
            <p className="tnum text-on-image mt-1.5 text-sub leading-6 text-white/55">
              {isMovie ? "上映日期" : "首播日期"} · {info.released}
            </p>
          )}

          {/* 在库是详情状态，不与外部词条链接混在一起；每个链接保持后端给出的库顺序。 */}
          {libraryLinks.length > 0 && (
            <div className="mt-4 max-w-full max-md:mt-3">
              <div className="inline-flex max-w-full flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-emerald-300/20 bg-emerald-400/[0.08] px-3.5 py-2.5 text-sub text-emerald-100/90 max-md:px-3">
                <span className="flex shrink-0 items-center gap-1.5 font-medium text-emerald-200">
                  <FolderIcon className="size-4" />
                  在库
                </span>
                {libraryLinks.map((libraryLink) => (
                  <Link
                    key={`${libraryLink.libraryId}:${libraryLink.mediaItemId}`}
                    href={`/library/${libraryLink.libraryId}/item/${libraryLink.mediaItemId}?returnTo=${encodeURIComponent(libraryReturnTo)}` as Route}
                    className="min-w-0 max-w-full break-words font-medium text-emerald-100 underline decoration-emerald-200/45 underline-offset-4 transition-colors hover:text-white hover:decoration-emerald-100"
                  >
                    {libraryLink.libraryName}
                  </Link>
                ))}
              </div>
            </div>
          )}

          {/* 操作区：已订阅的影片主按钮变为状态展示（点击进入管理弹层可取消订阅）。
              在库电影会把两个按钮都收掉，此时整行无内容就不渲染，免得留一段空白。 */}
          {(showSubscribeButton || showSearchButton || sub) && (
            <div className="mt-5 flex flex-wrap items-center gap-3 max-md:mt-3.5 max-md:gap-2">
              {showSubscribeButton && (sub ? (
                <button
                  type="button"
                  onClick={openSubscribe}
                  className="btn-glass flex h-10 items-center gap-2 bg-white/10 px-5 text-ui font-medium backdrop-blur-md transition hover:bg-white/15"
                >
                  <CheckIcon
                    className="size-4"
                    style={{ color: subscriptionStatusMeta[sub.status].color }}
                  />
                  已订阅 · {subscriptionStatusMeta[sub.status].label}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={openSubscribe}
                  className="btn-accent flex h-10 items-center gap-2 rounded-full px-5 text-ui font-semibold"
                >
                  <BellIcon className="size-4" />
                  订阅追踪
                </button>
              ))}
              {/* 搜索资源：不订阅、只想手动找种子下一次的直达口（此前只能回 ⌘K 重打片名） */}
              {showSearchButton && <Link
                href={`/search?q=${encodeURIComponent(item.title)}` as Route}
                className="btn-glass flex h-10 items-center gap-2 bg-white/10 px-5 text-ui font-medium backdrop-blur-md transition hover:bg-white/15"
              >
                <SearchIcon className="size-4" />
                搜索资源
              </Link>}
              {sub && (
                <span className="text-on-image flex items-center gap-1.5 text-sub text-[var(--text-muted)]">
                  <span
                    className="size-1.5 rounded-full"
                    style={{ backgroundColor: subscriptionStatusMeta[sub.status].color }}
                  />
                  {subscriptionProgressNote(sub)}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 订阅弹层：prepare → 季选择/自动续订/规则组 → 创建；已订阅时为管理态 */}
      <SubscribeDialog
        target={subscribeTarget}
        onClose={() => setSubscribeTarget(null)}
        onChanged={refreshSubscriptions}
      />

      {/* 简介承接标题与基础信息；四行确实溢出时才提供展开入口。 */}
      {item.overview && (
        <div className="mt-4 px-12 max-md:px-4">
          <ExpandablePlot text={item.overview} />
        </div>
      )}

      {/* —— 5. 演职员 —— */}
      {people.length > 0 && (
        <div className="mt-9 px-12 max-md:mt-6 max-md:px-4">
          {/* 导演 / 主创放在演员之前，共用同一条人物横滚；演员头像仍来自数据源 credits。 */}
          <CastRow cast={people} personHrefPrefix="/discover/people" />
        </div>
      )}

      {/* —— 6. 预告片：紧邻剧照，把「动态素材 + 静态素材」并成一段观感区 —— */}
      {detail && detail.videos.length > 0 && (
        <div className="mt-9 px-12 max-md:mt-6 max-md:px-4">
          <TrailerRow title={item.title} videos={detail.videos} />
        </div>
      )}

      {/* —— 7. 剧照与海报 —— */}
      {detail && (detail.backdrops.length > 0 || detail.posters.length > 0) && (
        <div className="mt-9 px-12 max-md:mt-6 max-md:px-4">
          <PhotoWall
            title={item.title}
            backdrops={detail.backdrops}
            posters={detail.posters}
          />
        </div>
      )}

      {/* —— 8. 系列电影：使用 TMDB collection 的完整作品顺序，不混入相似推荐。 —— */}
      {collection && collection.items.length > 1 && (
        <div className="mt-9">
          <MediaRow
            row={{
              id: `collection-${collection.id}`,
              title: collection.name,
              items: collection.items,
            }}
            insetClassName="px-12 max-md:px-4"
          />
        </div>
      )}

      {/* —— 9. 相似推荐 —— */}
      {related.length > 0 && (
        <div className="mt-9">
          <MediaRow
            row={{ id: `related-${item.id}`, title: "相似推荐", items: related }}
            insetClassName="px-12 max-md:px-4"
          />
        </div>
      )}

      {/* —— 10. 相关链接：与媒体库详情一致，固定在正文所有内容之后。 —— */}
      {(source === "tmdb" || info?.sourceUrl) && (
        <nav
          aria-label="外部词条"
          className="mx-12 mt-9 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-white/[0.04] pt-4 text-caption max-md:mx-4"
        >
          <span className="text-[var(--text-faint)]">相关链接</span>
          {source === "tmdb" && (
            <SourceLink
              href={`https://www.themoviedb.org/${item.type}/${item.id}`}
              label="TMDB"
            />
          )}
          {info?.sourceUrl && (
            <SourceLink href={doubanAppHref ?? info.sourceUrl} label="豆瓣" />
          )}
        </nav>
      )}
      </div>
    </div>
  );
}

/**
 * 与媒体库详情一致的四行简介。只有真实发生溢出时才显示展开入口；详情数据
 * 异步回填或页内切换作品时恢复折叠并重新测量，避免沿用上一部影片的状态。
 */
function ExpandablePlot({ text }: { text: string }) {
  const paragraphRef = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [hasOverflow, setHasOverflow] = useState(false);

  useEffect(() => {
    setExpanded(false);
    setHasOverflow(false);
  }, [text]);

  useEffect(() => {
    if (expanded) return;
    const paragraph = paragraphRef.current;
    if (!paragraph) return;

    const measure = () => {
      setHasOverflow(paragraph.scrollHeight > paragraph.clientHeight + 1);
    };
    const frame = window.requestAnimationFrame(measure);
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(paragraph);
    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [expanded, text]);

  return (
    <div className="max-w-3xl">
      <p
        ref={paragraphRef}
        className={`selectable text-on-image text-body-lg leading-7 text-white/78 ${
          expanded ? "" : "line-clamp-4"
        }`}
      >
        {text}
      </p>
      {(hasOverflow || expanded) && (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
          className="mt-1.5 inline-flex items-center gap-1 text-sub font-medium text-white/55 transition hover:text-white/85 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-2)]"
        >
          {expanded ? "收起" : "展开全文"}
          <ChevronRightIcon
            className={`size-3.5 transition-transform ${expanded ? "-rotate-90" : "rotate-90"}`}
          />
        </button>
      )}
    </div>
  );
}

/** 外部信息源链接：固定在正文底部，与媒体库详情使用同一弱化样式。 */
function SourceLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-caption text-[var(--text-muted)] underline decoration-white/20 underline-offset-2 transition hover:text-white/80"
    >
      {label} ↗
    </a>
  );
}

/**
 * 预告片横滚条 + 内嵌播放弹层。
 *
 * 这里有一处绕不开的现实：TMDB 只给 YouTube 的视频 key，**没有可直接播放的
 * 视频流**，所以「播放」这一步必须由浏览器直连 YouTube——本产品服务端配的
 * 代理只作用于服务端自己的请求，帮不到浏览器。为此把两件事拆开处理：
 *   - 封面图是普通图片，经 /images/proxy 由服务端回源缓存，因此**卡片一定能显示**；
 *   - 播放用 youtube-nocookie 内嵌（点开才创建 iframe，不预加载也不落跟踪 cookie），
 *     并在打开时探测浏览器能否直连 YouTube 图床；连不上就直接换成说明文案，
 *     而不是留给用户一个永远转圈的黑框。
 */
function TrailerRow({ title, videos }: { title: string; videos: MediaVideo[] }) {
  const [playing, setPlaying] = useState<MediaVideo | null>(null);

  return (
    <section>
      <h2 className="text-on-image mb-3 text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
        预告片
      </h2>

      <HScroller className="-mx-1 gap-3 px-1 pb-1 pt-1">
        {videos.map((video) => (
          <button
            key={video.key}
            type="button"
            onClick={() => setPlaying(video)}
            className="group/trailer w-[264px] shrink-0 text-left max-md:w-[208px]"
          >
            <div className="relative aspect-video overflow-hidden rounded-xl bg-[#141824] ring-1 ring-white/[0.08] transition-all duration-300 ease-out group-hover/trailer:-translate-y-1 group-hover/trailer:shadow-[0_16px_40px_rgba(0,0,0,0.55)] group-hover/trailer:ring-white/30">
              {/* YouTube 封面是 4:3（上下带黑边），object-cover 裁进 16:9 恰好只剩画面 */}
              <PosterImage
                src={video.thumbnailUrl}
                alt={`${title} ${video.kind}`}
                className="size-full object-cover transition-transform duration-500 ease-out group-hover/trailer:scale-[1.05]"
              />
              <span className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/25 transition-colors group-hover/trailer:bg-black/10">
                <span className="flex size-11 items-center justify-center rounded-full bg-black/55 text-white ring-1 ring-white/25 backdrop-blur-sm transition-transform duration-300 group-hover/trailer:scale-110">
                  <PlayIcon className="ml-0.5 size-5" />
                </span>
              </span>
              <span className="pointer-events-none absolute left-2 top-2 rounded-full bg-black/60 px-2 py-0.5 text-caption font-medium text-white/85 backdrop-blur-sm">
                {video.kind}
              </span>
            </div>
            <p className="mt-2 truncate text-sub text-[var(--text-muted)] transition-colors group-hover/trailer:text-[var(--text)]">
              {video.name}
            </p>
          </button>
        ))}
      </HScroller>

      {playing && (
        <TrailerPlayer video={playing} title={title} onClose={() => setPlaying(null)} />
      )}
    </section>
  );
}

/** 浏览器能否直连 YouTube：加载一张 YouTube 图床的小图作探针，超时按不可达算。 */
type YoutubeReach = "checking" | "ok" | "blocked";

function useYoutubeReachable(videoKey: string): YoutubeReach {
  const [reach, setReach] = useState<YoutubeReach>("checking");

  useEffect(() => {
    setReach("checking");
    // 探针刻意不走 /images/proxy：要测的正是「浏览器自己」的可达性，
    // 走了代理就变成在测服务端，结论会反过来骗人。
    const probe = new Image();
    const timer = window.setTimeout(() => setReach("blocked"), 6000);
    const settle = (result: YoutubeReach) => () => {
      window.clearTimeout(timer);
      setReach(result);
    };
    probe.onload = settle("ok");
    probe.onerror = settle("blocked");
    probe.src = `https://i.ytimg.com/vi/${videoKey}/default.jpg`;
    return () => {
      window.clearTimeout(timer);
      probe.onload = null;
      probe.onerror = null;
    };
  }, [videoKey]);

  return reach;
}

function TrailerPlayer({
  video,
  title,
  onClose,
}: {
  video: MediaVideo;
  title: string;
  onClose: () => void;
}) {
  const reach = useYoutubeReachable(video.key);

  return (
    // !max-w-4xl 压过 width="full" 的 max-w-none：播放器要够大但不必铺满桌面视口；
    // Modal 自带的 max-md:!max-w-none 声明在后，窄屏仍是满宽底部抽屉。
    <Modal open onClose={onClose} label={`${title} ${video.kind}`} width="full" panelClassName="!max-w-4xl">
      <div className="flex items-start gap-3 px-4 py-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-ui font-medium text-[var(--text)]">{video.name}</p>
          <p className="mt-0.5 text-caption text-[var(--text-muted)]">
            {title} · {video.kind}
          </p>
        </div>
        <button
          type="button"
          aria-label="关闭"
          onClick={onClose}
          className="btn-glass -mr-1 flex size-8 shrink-0 items-center justify-center !rounded-full"
        >
          <XIcon className="size-4" />
        </button>
      </div>

      <div className="aspect-video w-full bg-black">
        {reach === "blocked" ? (
          <div className="flex size-full flex-col items-center justify-center gap-3 px-6 text-center">
            <p className="text-ui font-medium text-[var(--text)]">当前浏览器无法直连 YouTube</p>
            {/* 整段写成单个字符串字面量：JSX 会把源码换行折成空格，中文句子里
                会平白多出一个空格。 */}
            <p className="max-w-md text-sub leading-6 text-[var(--text-muted)]">
              {
                "预告片由 YouTube 提供，播放需要浏览器本机能访问它。服务端在「设置 → 网络」配的代理只作用于服务端自己抓数据，不经过播放器；给浏览器挂上代理后即可正常播放。"
              }
            </p>
            <a
              href={video.watchUrl}
              target="_blank"
              rel="noreferrer"
              className="btn-glass flex h-9 items-center gap-2 px-4 text-sub font-medium text-[var(--text)]"
            >
              在 YouTube 打开 ↗
            </a>
          </div>
        ) : (
          // 点开才创建 iframe：详情页不为没人看的预告片预连 YouTube。
          // nocookie 域 + rel=0（相关视频只限本频道），autoplay 对齐「点了就播」的预期。
          <iframe
            key={video.key}
            src={`${video.embedUrl}?autoplay=1&rel=0`}
            title={`${title} ${video.kind}`}
            allow="accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture"
            allowFullScreen
            referrerPolicy="strict-origin-when-cross-origin"
            className="size-full border-0"
          />
        )}
      </div>
    </Modal>
  );
}

/**
 * 剧照与海报（Apple TV+ 式图片横滚条 + IMDb 式类型切换）：
 *   - 「剧照 / 海报」胶囊标签切换（无图的类型不渲染标签）；
 *   - 剧照 16:9、海报 2:3，等高排成一行横滚，隐藏滚动条，
 *     hover 时两侧浮现翻页钮（与发现页海报行同一套交互语言）；
 *   - 点任意缩略图 → 全屏灯箱看原图（复用 ImageLightbox：←→ 切换 + 缩略图条）。
 */
function PhotoWall({
  title,
  backdrops,
  posters,
}: {
  title: string;
  backdrops: MediaImage[];
  posters: MediaImage[];
}) {
  const tabs = [
    { id: "backdrops" as const, label: "剧照", images: backdrops },
    { id: "posters" as const, label: "海报", images: posters },
  ].filter((t) => t.images.length > 0);
  const [activeId, setActiveId] = useState(tabs[0].id);
  // 灯箱：记录打开时的图片下标；null = 关闭
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  const active = tabs.find((t) => t.id === activeId) ?? tabs[0];
  const { uploadBackdrop } = useBackdrop();

  // 「设为背景」（仅剧照：16:9 宽幅才适合铺满视口；海报是 2:3 竖图不提供）。
  // 完全复用外观设置的上传链路：拉取 TMDB 原图（图床允许跨域）→ 压缩成 2560px
  // JPEG → POST /appearance/backdrops 入库并生效 → 全站背景与外观设置图库同步更新。
  const setAsBackdrop: LightboxAction | undefined =
    active.id === "backdrops"
      ? {
          label: "设为背景",
          busyLabel: "正在下载并设置…",
          doneLabel: "已设为背景",
          icon: <PhotoIcon className="size-3.5" />,
          run: async (i: number) => {
            const image = active.images[i];
            let blob: Blob;
            try {
              // cache:no-store 绕过 HTTP 缓存：灯箱 <img> 已用 no-cors 模式加载过
              // 这张图，缓存里是不带 CORS 头的响应（CDN Vary: Origin），直接 fetch
              // 会命中污染缓存被判跨域失败——必须强制重新请求
              const resp = await fetch(image.fullUrl, { cache: "no-store" });
              if (!resp.ok) throw new Error();
              blob = await resp.blob();
            } catch {
              throw new Error("下载剧照原图失败，请检查网络后重试");
            }
            await uploadBackdrop(
              new File([blob], "backdrop.jpg", { type: blob.type || "image/jpeg" }),
            );
          },
        }
      : undefined;

  const scrollerRef = useRef<HTMLDivElement>(null);
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(false);
  const edgeFrame = useRef(0);

  /** 与 HScroller 同款的边缘检测：到达两端时隐藏对应方向的翻页钮。
   *  测量合并进 rAF：scroll 回调里直接读布局会强制同步布局（见 h-scroller.tsx） */
  const updateEdges = useCallback(() => {
    if (edgeFrame.current) return;
    edgeFrame.current = window.requestAnimationFrame(() => {
      edgeFrame.current = 0;
      const el = scrollerRef.current;
      if (!el) return;
      setCanLeft(el.scrollLeft > 1);
      setCanRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
    });
  }, []);

  useEffect(() => {
    updateEdges();
    window.addEventListener("resize", updateEdges);
    return () => {
      window.removeEventListener("resize", updateEdges);
      window.cancelAnimationFrame(edgeFrame.current);
    };
  }, [updateEdges, activeId]);

  const page = (dir: -1 | 1) => {
    const el = scrollerRef.current;
    el?.scrollBy({ left: dir * el.clientWidth * 0.85, behavior: "smooth" });
  };

  const switchTab = (id: typeof activeId) => {
    setActiveId(id);
    // 切换类型回到行首，避免带着上一类的滚动位置看新列表
    scrollerRef.current?.scrollTo({ left: 0 });
  };

  return (
    <section className="group/photos">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
          剧照与海报
        </h2>
        {tabs.length > 1 && (
          <div className="flex gap-1.5">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                type="button"
                aria-pressed={tab.id === activeId}
                onClick={() => switchTab(tab.id)}
                className={`tnum rounded-full px-3 py-1 text-sub font-medium transition-colors ${
                  tab.id === activeId
                    ? "bg-white/[0.14] text-white"
                    : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
                }`}
              >
                {tab.label} {tab.images.length}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="relative">
        <div
          ref={scrollerRef}
          onScroll={updateEdges}
          className="scroll-none -mx-1 flex gap-3 overflow-x-auto px-1 pb-1 pt-1"
        >
          {active.images.map((img, i) => (
            <button
              key={img.previewUrl}
              type="button"
              aria-label={`查看${active.label}第 ${i + 1} 张`}
              onClick={() => setLightboxIndex(i)}
              className={`shrink-0 overflow-hidden rounded-xl bg-[#141824] ring-1 ring-white/[0.08] transition-all duration-300 ease-out hover:-translate-y-1 hover:shadow-[0_16px_40px_rgba(0,0,0,0.55)] hover:ring-white/30 ${
                active.id === "backdrops" ? "aspect-video h-[148px] max-md:h-[104px]" : "aspect-[2/3] h-[148px] max-md:h-[126px]"
              }`}
            >
              <PosterImage
                src={img.previewUrl}
                alt={`${title} ${active.label}`}
                className="size-full object-cover transition-transform duration-500 ease-out hover:scale-[1.05]"
              />
            </button>
          ))}
        </div>

        <PhotoArrow dir={-1} visible={canLeft} onClick={() => page(-1)} />
        <PhotoArrow dir={1} visible={canRight} onClick={() => page(1)} />
      </div>

      {lightboxIndex !== null && (
        <ImageLightbox
          images={active.images.map((img) => img.fullUrl)}
          initialIndex={lightboxIndex}
          title={`${title} · ${active.label}`}
          action={setAsBackdrop}
          thumbAspect={active.id === "backdrops" ? "landscape" : "portrait"}
          onClose={() => setLightboxIndex(null)}
        />
      )}
    </section>
  );
}

/** 图片条的翻页钮：!absolute 同 MediaRow —— surface-raised 自带 relative 会盖掉 absolute */
function PhotoArrow({
  dir,
  visible,
  onClick,
}: {
  dir: -1 | 1;
  visible: boolean;
  onClick: () => void;
}) {
  const Icon = dir === -1 ? ChevronLeftIcon : ChevronRightIcon;
  return (
    <button
      type="button"
      aria-label={dir === -1 ? "向左滚动" : "向右滚动"}
      onClick={onClick}
      className={`surface-raised !absolute top-1/2 z-10 flex size-9 -translate-y-1/2 items-center justify-center !rounded-full text-[var(--text)] transition-all duration-200 hover:scale-110 ${
        dir === -1 ? "left-2" : "right-2"
      } ${
        visible
          ? "pointer-events-auto opacity-0 group-hover/photos:opacity-100"
          : "pointer-events-none opacity-0"
      }`}
    >
      <Icon className="size-4" />
    </button>
  );
}

/**
 * 直达详情页（硬刷新 / 分享链接）时的整页兜底：
 * 站内跳转有 seed 可秒开，直达则先转圈等接口；接口失败给出错误 + 返回入口。
 */
function DetailFallback({ failed, onBack }: { failed: boolean; onBack: () => void }) {
  return (
    // ambient-fallback：本页豁免了全局蒙版（isHome），而此刻还没有沉浸背景可铺，
    // 文案会直接压在用户配的壁纸上——亮壁纸下就读不出来了，兜底态自己带一层底。
    // flex-1 而非 h-full：调用处在其上方叠了一条 PageNav（flex 纵向布局）。
    <div className="ambient-fallback flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
      {failed ? (
        <>
          <p className="text-body-lg font-semibold text-[var(--text)]">未能加载该影片详情</p>
          <p className="max-w-sm text-ui leading-6 text-[var(--text-muted)]">
            资源可能已下线，或网络暂时不可达。请返回后重试。
          </p>
          <button
            type="button"
            onClick={onBack}
            className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            <ArrowLeftIcon className="size-4" />
            返回
          </button>
        </>
      ) : (
        <div className="flex items-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载详情…
        </div>
      )}
    </div>
  );
}
