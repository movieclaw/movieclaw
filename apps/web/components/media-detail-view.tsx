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
import { BrandLoader } from "@/components/brand-loader";
import { CastRow } from "@/components/cast-row";
import { DetailBackdropSlideshow } from "@/components/detail-backdrop-slideshow";
import { HScroller } from "@/components/h-scroller";
import { Modal } from "@/components/modal";
import { NetflixBackButton } from "@/components/netflix/back-button";
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
import { upgradedTmdbOriginalUrl } from "@/lib/image-proxy";
import { getMediaSeed } from "@/lib/media-detail";
import { useTapGuard } from "@/lib/use-tap-guard";
import { usePageTitle } from "@/lib/use-page-title";
import { useTheme } from "@/lib/ui-prefs";
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
 *      页面本身不画 Hero 图层：大图直出、零边界，侧栏与外壳留白一起透出。
 *      顶栏首屏只有一颗返回键浮在剧照上，没有横幅剧照时退回海报作氛围图。
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
  // 退回海报，覆盖层自己会铺满作氛围色。豆瓣来源不换背景：只有小尺寸海报、
  // 没有高清横幅剧照，铺成全屏背景是一片糊图——宁可保持原背景，也不为沉浸降质
  // （页内手机 Hero 与背景分开取源，豆瓣在手机上仍有 Hero，见下）。
  const { setOverrideBackdrop } = useBackdrop();
  // 沉浸背景只走高清：TMDB 的 original 地址是确定性的（w1280 同图换尺寸段，
  // 见 upgradedTmdbOriginalUrl），进入页面即刻推导并预加载，加载**并解码**完成
  // 才显示——不存在「先低清后高清」的换图过程，也就没有换图带来的突兀/闪烁。
  // 低清 w1280 只作兜底：非 TMDB 图（无更高档位，地址原样返回）或高清加载
  // 失败时才显示。没有横幅剧照时退回海报。
  const fallbackBackdrop = item?.backdropUrl || item?.posterUrl || "";
  const hdUrl =
    source === "douban"
      ? undefined
      : (detail?.backdropOriginalUrl ??
        (fallbackBackdrop ? upgradedTmdbOriginalUrl(fallbackBackdrop) : undefined));
  const [hdState, setHdState] = useState<"pending" | "ok" | "failed">("pending");
  useEffect(() => {
    if (!hdUrl) {
      setHdState("failed"); // 没有高清档：直接用兜底图
      return;
    }
    setHdState("pending");
    let cancelled = false;
    const img = new Image();
    const settle = () => {
      img
        .decode()
        .catch(() => {})
        .then(() => {
          if (!cancelled) setHdState("ok");
        });
    };
    img.onload = settle;
    img.onerror = () => {
      if (!cancelled) setHdState("failed");
    };
    img.src = hdUrl;
    return () => {
      cancelled = true;
    };
  }, [hdUrl]);
  // 等待高清期间保持黑场（宁黑勿糊）：只有确认无高清档或高清加载失败，
  // 才把低清图作为兜底显示出来。豆瓣不换背景（没有高清横幅，铺满全屏是糊图）。
  const immersiveUrl =
    source === "douban"
      ? ""
      : hdState === "ok"
        ? hdUrl
        : hdState === "failed"
          ? fallbackBackdrop
          : "";
  useEffect(() => {
    if (!immersiveUrl) return;
    setOverrideBackdrop(immersiveUrl);
    return () => setOverrideBackdrop(null);
  }, [immersiveUrl, setOverrideBackdrop]);
  // 豆瓣外链的移动端 App 直跳：无悬停设备把「豆瓣」外链换成官方分发地址，
  // 装了豆瓣 App 直接拉起进词条页（桌面/未命中时为 null，回落网页地址）
  const doubanAppHref = useDoubanAppHref(source === "douban" ? id : null);

  // Netflix 桌面主题的详情页定制层。银玻璃的 PageNav（圆角玻璃返回键 + 吸顶
  // 雾）在这里退役——它是银玻璃的控件语言，浮在仿 Netflix 顶栏左端既突兀、
  // 又被 z-40 的固定顶栏整个盖住点不到；Netflix 的返回语言是一颗裸的白色
  // chevron（见 NetflixBackButton）。移动端仍保留 PageNav：它要向外壳登记
  // 「本页自带顶栏」并充当返回入口（见 app-shell）。
  // 这些 hook 必须无条件调用（短路写法会触发 rules-of-hooks）。
  const themeId = useTheme().id;
  const isMobile = useIsMobile();
  const isNf = themeId === "netflix";
  const isNfDesktop = isNf && !isMobile;
  const hidePageNav = isNfDesktop;

  // 滚动退场：详情页下滚时剧照不是被机械地推出屏幕，而是随滚动进度渐暗 +
  // 模糊（Netflix 海报墙的观感）。进度写到根节点 CSS 变量 --nf-hero-recede
  // （0→1），沉浸覆盖层（globals.css 的 html.nf-hero-live .backdrop-override）
  // 用它驱动 filter——滚动过程零 React 重渲染。rAF 合帧：一次滚动会派发多次
  // scroll 事件，只保留最后一帧的写入。Netflix 主题桌面与移动都启用（移动端
  // 的纵向渐隐同样挂在这个标记类上，见 globals.css 的移动端档）；卸载 / 换片
  // 重建时把变量与标记类清干净，别污染其他页面。
  const scrollRef = useRef<HTMLDivElement>(null);
  const hasItem = Boolean(item);
  useEffect(() => {
    if (!isNf) return;
    const root = document.documentElement;
    root.classList.add("nf-hero-live");
    const el = scrollRef.current;
    if (!el) {
      // 兜底态（数据未到）没有滚动容器：只挂标记类，清理时照常摘除
      return () => {
        root.classList.remove("nf-hero-live");
        root.style.removeProperty("--nf-hero-recede");
      };
    }
    let frame = 0;
    const sync = () => {
      frame = 0;
      // 归一化尺度取首屏的 75%：缓出的区间更长，渐暗/模糊的「过程感」更足；
      // 变量注册了 <number> 类型并挂了过渡，滚轮大幅甩动时也是缓动跟随
      const range = Math.max(320, el.clientHeight * 0.75);
      const progress = Math.min(1, Math.max(0, el.scrollTop / range));
      root.style.setProperty("--nf-hero-recede", progress.toFixed(3));
    };
    const onScroll = () => {
      if (!frame) frame = window.requestAnimationFrame(sync);
    };
    sync();
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      if (frame) window.cancelAnimationFrame(frame);
      root.classList.remove("nf-hero-live");
      root.style.removeProperty("--nf-hero-recede");
    };
  }, [isNf, hasItem]);

  // 兜底态也必须渲染 PageNav——它向外壳登记「本页自带顶栏」，否则移动端的
  // 全局顶栏（☰ + logo）会在数据到达前先显示、随后又消失，顶部闪一下；
  // 顺带让用户在转圈期间就有返回键可点。
  // 无 seed 且详情尚未到达：直达链接的加载态（或失败兜底）。
  if (!item) {
    return (
      <div className="flex h-full flex-col">
        {/* 当前页标题未知，留空——只为立起返回键并认领顶栏 */}
        {!hidePageNav && <PageNav title="" fallback={navFallback} />}
        {isNfDesktop && <NetflixBackButton onBack={back} />}
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
      ref={scrollRef}
      className="detail-ambient scroll-thin scroll-safe relative isolate h-full overflow-y-auto rounded-2xl max-md:rounded-none"
    >
      {/* 没有任何 Hero 图层：全站背景此刻就是本片剧照（沉浸覆盖 + 本页豁免
          全局蒙版，见 app-shell 的 isHome），大图直出、零边界；.detail-ambient
          在滚动容器上铺「透明 → 纯黑」的渐变板托住下方内容（见 globals.css，
          Netflix 主题另有左侧渐变遮罩护住标题区）。 */}
      {!hidePageNav && <PageNav title={item.title} fallback={navFallback} />}
      {isNfDesktop && <NetflixBackButton onBack={back} />}
      {/* 背景轮换：Netflix 桌面且剧照多于一张时，按序叠变（见组件说明）。
          首帧传主 backdrop 原图——与覆盖层当前显示的是同一张照片，轮换层
          淡入接管时没有构图/内容跳变。 */}
      {isNfDesktop && detail && detail.backdrops.length > 1 && (
        <DetailBackdropSlideshow
          images={detail.backdrops}
          initialUrl={detail.backdropOriginalUrl ?? detail.backdrops[0]?.fullUrl}
        />
      )}

      {/* 氛围留白：这一段什么都不放，让剧照完整呼吸。高度由 --detail-hero-h
          驱动（与 globals.css 的渐变起点同源，各主题自行取值）。 */}
      <div className="h-[var(--detail-hero-h)] min-h-[var(--detail-hero-min-h)]" />

      {/* 内容层：-mt-28/pt-28 与 .detail-ambient 的渐变起点对齐——渐变从标题
          上方开始压暗，基础信息附近已接近纯黑，下面保持全黑。detail-content
          是 Netflix 主题的标题上移钩子（globals.css 把它的 pt 收小，标题
          借左侧遮罩直接落在剧照上）。 */}
      <div className="detail-content relative z-10 -mt-28 pb-12 pt-28">
      {/* 前导簇（标题 → 元信息 → 操作 → 简介）：Netflix 主题下高度固定
          （globals.css 的 .detail-lead min-height），演职员表从横幅正下方一条
          固定线开始，不随简介长短上下漂移——简介短时下方留黑色空档。 */}
      <div className="detail-lead">
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
      </div>

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
          <TrailerCard key={video.key} video={video} title={title} onPlay={() => setPlaying(video)} />
        ))}
      </HScroller>

      {playing && (
        <TrailerPlayer video={playing} title={title} onClose={() => setPlaying(null)} />
      )}
    </section>
  );
}

/** 预告片卡：横滚行内的可点卡，套 useTapGuard——滑动/刹车手势派发的
 *  click 会被拦下，避免滑一下就弹出播放层（同 PosterCard 约定）。 */
function TrailerCard({
  video,
  title,
  onPlay,
}: {
  video: MediaVideo;
  title: string;
  onPlay: () => void;
}) {
  const tapGuard = useTapGuard(onPlay);
  return (
    <button
      type="button"
      {...tapGuard}
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
          // overscroll-x-contain：滑到行的尽头后不把剩余动量甩给外层纵向滚动
          // （同 HScroller 的处理，触屏上「滑到头带动整页跳一下」即由此而来）
          className="scroll-none -mx-1 flex gap-3 overflow-x-auto overscroll-x-contain px-1 pb-1 pt-1"
        >
          {active.images.map((img, i) => (
            <PhotoCard
              key={img.previewUrl}
              img={img}
              index={i}
              label={active.label}
              landscape={active.id === "backdrops"}
              title={title}
              onOpen={() => setLightboxIndex(i)}
            />
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
      // 触屏隐藏箭头（同 HScroller）：本就只靠 hover 显形，触屏没有 hover
      // 会变成「看不见但能点」的隐形命中区，吃掉图片边缘的点击
      className={`surface-raised !absolute top-1/2 z-10 flex size-9 -translate-y-1/2 items-center justify-center !rounded-full text-[var(--text)] transition-all duration-200 hover:scale-110 [@media(hover:none)]:hidden ${
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

/** 剧照墙的单张卡：横滚行内可点（开灯箱），套 useTapGuard 防滑动误触。 */
function PhotoCard({
  img,
  index,
  label,
  landscape,
  title,
  onOpen,
}: {
  img: { previewUrl: string };
  index: number;
  label: string;
  landscape: boolean;
  title: string;
  onOpen: () => void;
}) {
  const tapGuard = useTapGuard(onOpen);
  return (
    <button
      type="button"
      {...tapGuard}
      aria-label={`查看${label}第 ${index + 1} 张`}
      className={`shrink-0 overflow-hidden rounded-xl bg-[#141824] ring-1 ring-white/[0.08] transition-all duration-300 ease-out hover:-translate-y-1 hover:shadow-[0_16px_40px_rgba(0,0,0,0.55)] hover:ring-white/30 ${
        landscape ? "aspect-video h-[148px] max-md:h-[104px]" : "aspect-[2/3] h-[148px] max-md:h-[126px]"
      }`}
    >
      <PosterImage
        src={img.previewUrl}
        alt={`${title} ${label}`}
        className="size-full object-cover transition-transform duration-500 ease-out hover:scale-[1.05]"
      />
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
          <BrandLoader className="size-5" />
          正在加载详情…
        </div>
      )}
    </div>
  );
}
