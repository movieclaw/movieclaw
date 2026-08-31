"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ArtworkPickerDialog } from "@/components/artwork-picker-dialog";
import { CastRow } from "@/components/cast-row";
import { MediaTrackRows } from "@/components/media-track-rows";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { HScroller } from "@/components/h-scroller";
import {
  ArrowLeftIcon,
  CheckIcon,
  ChevronRightIcon,
  FolderIcon,
  MoreIcon,
  PlayIcon,
  TrashIcon,
} from "@/components/icons";
import { useConfirm, useToast } from "@/components/feedback";
import { Modal } from "@/components/modal";
import { PosterImage } from "@/components/poster-image";
import { playHref, rememberPlayerReturnPath } from "@/lib/player/play-links";
import { ReidentifyDialog } from "@/components/reidentify-dialog";
import { Tooltip } from "@/components/tooltip";
import {
  type ItemDeleteResult,
  type LibraryEpisode,
  type LibraryItemDetail,
  type LibraryItemFile,
  type MediaLibrary,
  type SeasonEpisodes,
  type TransferPreview,
  type TransferStatus,
  deleteLibraryFile,
  deleteLibraryItem,
  getItemEpisodes,
  getLibrary,
  getLibraryItemDetail,
  getLibraryItemTransferStatus,
  listLibraries,
  previewItemTransfer,
  purgeLibraryFile,
  refreshItemMetadata,
  restoreLibraryFile,
  setItemScrapeLibrary,
  transferLibraryItem,
} from "@/lib/api/libraries";
import {
  type PlaybackUnit,
  type PlaybackWatchState,
  fetchResumeState,
} from "@/lib/api/playback";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import type { MediaType } from "@/lib/media-types";
import { getDiscoveryReturnPath } from "@/lib/discovery-return-path";
import { formatBytes, formatRuntimeMinutes, formatVideoResolution } from "@/lib/format";
import { formatClock } from "@/lib/player/timeline";
import { USER_LOWEST_SOURCE, mediaSourceDisplayLabel } from "@/lib/media-source-annotation";
import { useDoubanAppHref } from "@/lib/douban-app-link";
import { useBackdrop } from "@/lib/backdrop";
import { resolveRequestUrl } from "@/lib/http";
import { cachedImageUrl } from "@/lib/image-proxy";
import { languageLabel } from "@/lib/language-labels";
import { invalidateLibraryDetailSnapshot } from "@/lib/library-detail-snapshot";
import { refreshItemConfirm } from "@/lib/library-confirm";
import { usePermissions } from "@/lib/permissions";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { usePageTitle } from "@/lib/use-page-title";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/** 剧集详情页当前选中的分集上下文，供 Hero 与分集区共享同一份数据。 */
interface SelectedEpisodeContext {
  seasonNumber: number;
  episode: LibraryEpisode;
  files: LibraryItemFile[];
}

/**
 * 媒体库条目详情页（/library/[id]/item/[mediaItemId]）——与发现页详情
 * （MediaDetailView，纯 TMDB 实时数据）职责不同：这里回答的是
 * 「**我拥有的这份拷贝**是什么」，全部信息来自本地刮削成果与文件本体：
 *
 *   1. 沉浸背景（全站背景临时换成本片剧照）优先条目目录里的 fanart（本地美术图接口），其次 TMDB 剧照；
 *   2. 简介 / 风格 / 片长 / 演职员来自条目目录的 NFO（TMM/Emby 刮削产物）；
 *   3. 片源规格来自 ffprobe 对文件本体的探测；基础信息展示当前文件的音轨 / 字幕，
 *      底部文件区按物理文件折叠尺寸 / 视频 / 码率，剧集只展示当前选中集；
 *   4. 底部文件区默认只列原始文件名，悬浮看物理路径——识别错了
 *      用户要能立刻知道"这是哪个文件"，同时不让技术信息压过影片本身；
 *   5. 条目级操作：重新识别（识别器升级后的翻案通道）与删除（唯一会
 *      真删磁盘的入口，整个刮削目录一起清，二次确认）。文件行与分集
 *      文件卡上另有单文件删除（多版本洗掉一个 / 删某集重下，同样二次
 *      确认；最后一个文件升级为整条目删除）。
 */
export function LibraryItemDetailView({
  libraryId,
  mediaItemId,
  returnTo,
  fromRecent = false,
  initialSeason,
  initialEpisode,
}: {
  libraryId: number;
  mediaItemId: number;
  returnTo?: string;
  /** 从媒体库首页最近观看进入：左上角返回首页，而不是条目所在的单库库存页。 */
  fromRecent?: boolean;
  initialSeason?: number;
  initialEpisode?: number;
}) {
  const { canManageLibraries } = usePermissions();
  // 洗版入口（quality-upgrade.md §13.3/§13.5）：有订阅并入既有订阅，无订阅走
  // 订阅弹层的洗版变体（库存季预填、建完自动接一轮洗版）
  const { canSubscribe, subscriptionOf, open: openSubscribe } = useSubscribeEntry();
  const router = useRouter();
  const discoveryReturnPath = getDiscoveryReturnPath(returnTo);
  const confirm = useConfirm();
  const toast = useToast();
  const [detail, setDetail] = useState<LibraryItemDetail | null>(null);
  const [library, setLibrary] = useState<MediaLibrary | null>(null);
  const [failed, setFailed] = useState(false);
  // 「修正识别结果」弹窗：重走识别链出结论 → 用户拍板 → 才落库。
  // 拍板后不立刻重拉详情——文件全改挂走时本页会 404 翻成兜底态、把弹窗
  // 连同"✓ 已改挂为《X》"的回执一起卸掉，分裂成多组时更是没法接着处理
  // 剩下的组。改成记一个脏标记，关窗时再刷新
  // 播放键是 button 而不是 <Link>，Next 不会自动预取 /play 的路由包与 RSC
  // 载荷；条目一加载完成就预取，点播放时整整省掉一跳（§6.10 起播链路）。
  // 预取电影形态的地址即可：剧集地址只是多一段路径，JS 包完全相同。
  useEffect(() => {
    if (detail) router.prefetch(playHref(detail.media_item_id) as Route);
  }, [router, detail]);

  const [reidentifyOpen, setReidentifyOpen] = useState(false);
  const [reidentifyDirty, setReidentifyDirty] = useState(false);
  // 元数据刷新的失败提示（原先与重识别共用一条横幅）
  const [refreshError, setRefreshError] = useState<string | null>(null);
  // 元数据刷新：**进行中的状态以服务端 detail.scraping 为准**（离开页面、
  // 刷新浏览器、换设备打开都能接着看到）；这个本地态只覆盖"点击到接口
  // 返回"这一小段，避免按钮闪一下没反应
  const [kicking, setKicking] = useState(false);
  // 「更换图片」弹层（手动选海报/背景，选后加锁）
  const [artworkOpen, setArtworkOpen] = useState(false);
  // 「刮削归属」弹层（决定这条目按哪个库的语言/选图设置刮）
  const [scrapeLibraryOpen, setScrapeLibraryOpen] = useState(false);
  // 删除确认弹窗
  const [deleteOpen, setDeleteOpen] = useState(false);
  // 单文件删除确认弹窗（非 null 即打开；多版本洗版 / 删某集重下的入口）
  const [deleteFileTarget, setDeleteFileTarget] = useState<LibraryItemFile | null>(null);
  // 转移到其他库的弹窗（选库 → 预览 → 执行 → 进度 → 结论）
  const [transferOpen, setTransferOpen] = useState(false);
  // 剧集详情的 Hero 与分集区必须指向同一集；电影保持为 null。
  const [selectedSeriesEpisode, setSelectedSeriesEpisode] =
    useState<SelectedEpisodeContext | null>(null);
  // 顶部文件选择器由父级控制，分辨率才能与音轨、字幕同步切换。
  const [selectedTrackFileId, setSelectedTrackFileId] = useState<number | null>(null);
  // 当前播放单元的观看记录（上次看到哪 / 是否看完）。播放按钮据此在
  // 「播放 / 继续观看 / 重新播放」之间切换——null 表示还没问到，按钮先按
  // 「播放」渲染，不为一次可能是全零的查询留骨架。
  const [watched, setWatched] = useState<PlaybackWatchState | null>(null);

  /**
   * 播放入口指向的播放单元（media_item + 季集三元组，与播放器同一套约定）。
   * 电影用 (0, 0) 哨兵；剧集跟随分集区当前选中的那一集，选集变了续播点也要跟着变。
   */
  const playUnit = useMemo<PlaybackUnit | null>(() => {
    if (!detail) return null;
    if (detail.kind === "movie") {
      return { media_item_id: detail.media_item_id, season_number: 0, episode_number: 0 };
    }
    if (!selectedSeriesEpisode) return null;
    return {
      media_item_id: detail.media_item_id,
      season_number: selectedSeriesEpisode.seasonNumber,
      episode_number: selectedSeriesEpisode.episode.episode_number,
    };
  }, [detail, selectedSeriesEpisode]);

  // 单元三元组的字符串形式：detail 每次轮询都是新对象，直接依赖 playUnit
  // 会让刮削期间每两秒重查一次续播点。
  const playUnitKey = playUnit
    ? `${playUnit.media_item_id}/${playUnit.season_number}/${playUnit.episode_number}`
    : null;
  useEffect(() => {
    if (!playUnit) {
      setWatched(null);
      return;
    }
    let cancelled = false;
    setWatched(null);
    fetchResumeState(playUnit)
      .then((state) => {
        if (!cancelled) setWatched(state);
      })
      // 查不到续播点不影响起播，按钮退回「播放」即可，不打扰用户
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // playUnit 是对象字面量，按内容（三元组）比较
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playUnitKey]);

  const handleTransferFinished = useCallback(
    (targetLibraryId: number) => {
      invalidateLibraryDetailSnapshot(libraryId);
      invalidateLibraryDetailSnapshot(targetLibraryId);
    },
    [libraryId],
  );

  const reload = useCallback(() => {
    setFailed(false);
    Promise.all([
      getLibraryItemDetail(libraryId, mediaItemId),
      getLibrary(libraryId).catch(() => null),
    ])
      .then(([data, lib]) => {
        setDetail(data);
        setLibrary(lib);
      })
      .catch(() => setFailed(true));
  }, [libraryId, mediaItemId]);

  useEffect(() => {
    setDetail(null);
    setRefreshError(null);
    setSelectedSeriesEpisode(null);
    setSelectedTrackFileId(null);
    reload();
  }, [reload]);

  // 沉浸背景：进入本页把全站背景临时换成该片剧照（侧栏、外壳留白一起透出，
  // 不再只铺详情卡片的局部），离开即恢复用户配置的背景——见 lib/backdrop.tsx
  // 的 setOverrideBackdrop。没有横幅剧照时退回海报，覆盖层自己会铺满作氛围色。
  const { setOverrideBackdrop } = useBackdrop();
  const immersiveUrl = detail ? imageUrl(detail.backdrop_url ?? detail.poster_url) : "";
  useEffect(() => {
    if (!immersiveUrl) return;
    setOverrideBackdrop(immersiveUrl);
    return () => setOverrideBackdrop(null);
  }, [immersiveUrl, setOverrideBackdrop]);

  // 待回收行的恢复 / 立即清理（library-file-recycle.md §7）。
  // 恢复是可逆动作直接执行；清理真删磁盘，做种保护形态额外讲清断种风险
  const restoreTrashedFile = useCallback(
    async (file: LibraryItemFile) => {
      try {
        await restoreLibraryFile(libraryId, mediaItemId, file.id);
        toast.success(`「${file.file_name}」已恢复为在位版本`);
        reload();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "恢复失败，请稍后重试");
      }
    },
    [libraryId, mediaItemId, reload, toast],
  );
  const purgeTrashedFile = useCallback(
    async (file: LibraryItemFile) => {
      const seeding = !file.purge_after;
      const ok = await confirm({
        title: `立即清理「${file.file_name}」？`,
        description: seeding
          ? "该文件处于做种保护，可能仍被下载器做种。清理会从磁盘删除文件并可能中断做种任务（PT 站请留意保种要求），此操作不可恢复。"
          : "将立即从回收站删除该文件，不再等待保留期，此操作不可恢复。",
        confirmLabel: "立即清理",
        cancelLabel: "先不",
        tone: "danger",
      });
      if (!ok) return;
      try {
        await purgeLibraryFile(libraryId, mediaItemId, file.id);
        toast.success(`「${file.file_name}」已清理`);
        reload();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "清理失败，请稍后重试");
      }
    },
    [confirm, libraryId, mediaItemId, reload, toast],
  );

  usePageTitle(detail?.title);
  // 与发现详情页保持同一外链行为：移动端优先唤起豆瓣 App，桌面回落网页。
  const doubanAppHref = useDoubanAppHref(detail?.douban_id);

  // 刮削进行中就轮询到它结束——状态在服务端（detail.scraping），所以
  // **无论刷新是从这个页面发起的、还是别处发起后你才打开这一页**，都会
  // 看到"正在刷新"并在结束时自动呈现新档案/新图；离开页面也不影响后台跑完
  const scrapingNow = Boolean(detail?.scraping) || kicking;
  useVisiblePolling(
    () => {
      getLibraryItemDetail(libraryId, mediaItemId)
        .then(setDetail)
        .catch(() => {});
    },
    detail?.scraping ? 2000 : null,
  );

  // 兜底态（加载中/失败）的顶栏：条目标题未知，末项留空——渲染 PageNav 是为了
  // 向外壳登记「本页自带顶栏」，否则移动端全局顶栏（☰ + logo）会先显示再消失，
  // 顶部闪一下；同时转圈期间就有返回键可点。加载态与正文共用同一兜底目标。
  const navFallback = discoveryReturnPath
    ? { label: "发现详情", href: discoveryReturnPath }
    : fromRecent
      ? { label: "媒体库", href: "/library" as Route }
      : { label: library?.name ?? "库存", href: `/library/${libraryId}` as Route };

  if (failed) {
    return (
      // ambient-fallback：同 MediaDetailView——本页豁免全局蒙版，兜底态没有沉浸
      // 背景可铺，文案会压在用户壁纸上，自己带一层底才读得清
      <div className="ambient-fallback flex h-full flex-col">
        <PageNav title="" fallback={navFallback} />
        <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
          <p className="text-body-lg font-semibold text-[var(--text)]">未能加载该条目</p>
          <p className="max-w-sm text-ui leading-6 text-[var(--text-muted)]">
            条目可能已被删除或重新识别为其他作品，请返回后查看。
          </p>
          <Link
            href={navFallback.href}
            replace
            className="btn-glass flex items-center gap-2 px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            <ArrowLeftIcon className="size-4" />
            返回{navFallback.label}
          </Link>
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="ambient-fallback flex h-full flex-col">
        <PageNav title="" fallback={navFallback} />
        <div className="flex flex-1 items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在读取本地刮削信息…
        </div>
      </div>
    );
  }

  const isMovie = detail.kind === "movie";
  const meta = detail.local_meta;
  // 片长：NFO 的 runtime 优先，其次任意文件的实测时长
  const runtimeMinutes =
    meta?.runtime_minutes ??
    (() => {
      const probed = detail.files.find((f) => f.duration_seconds)?.duration_seconds;
      return probed ? Math.round(probed / 60) : null;
    })();
  const trackFiles = isMovie
    ? detail.files
    : (selectedSeriesEpisode?.files ?? []);
  // 音轨/规格只认在位文件：缺失与待回收的版本都不该被当作可播内容展示
  const availableTrackFiles = trackFiles.filter((file) => file.state === "in_place");
  const selectedTrackFile =
    availableTrackFiles.find((file) => file.id === selectedTrackFileId) ??
    availableTrackFiles[0] ??
    null;
  // 电影和剧集共用同一套基础信息层级：标题行只保留年份、片长、分辨率、HDR；
  // 帧率、编码、文件大小等技术细节留在文件区。电影概览全部在位版本，
  // 剧集只显示当前集当前文件的画面规格，并随文件选择器同步变化。
  const resolutionSources = isMovie
    ? detail.files.filter((file) => file.state === "in_place")
    : selectedTrackFile
      ? [selectedTrackFile]
      : [];
  const itemResolutions = [
    ...new Set(
      resolutionSources
        .map((file) => file.resolution)
        .filter((resolution): resolution is string => Boolean(resolution)),
    ),
  ]
    .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }))
    .map(formatVideoResolution);
  const itemHdrFormats = [
    ...new Set(
      resolutionSources.map((file) => file.hdr).filter((hdr): hdr is string => Boolean(hdr)),
    ),
  ].sort((a, b) => {
    const priority = ["Dolby Vision", "HDR10+", "HDR10", "HLG", "HDR"];
    const aIndex = priority.indexOf(a);
    const bIndex = priority.indexOf(b);
    return (aIndex < 0 ? priority.length : aIndex) -
      (bIndex < 0 ? priority.length : bIndex);
  });
  const itemFacts = [
    detail.year ? String(detail.year) : null,
    runtimeMinutes != null ? formatRuntimeMinutes(runtimeMinutes) : null,
  ].filter((fact): fact is string => Boolean(fact));
  const directorCast =
    meta
      ? meta.director_credits?.length > 0
        ? meta.director_credits.map((director) => ({
            name: director.name,
            credit: "导演",
            avatarUrl: director.thumb_url ? cachedImageUrl(director.thumb_url) : null,
            tmdbPersonId: director.tmdb_person_id,
          }))
        : [...new Set(meta.directors)].map((name) => ({ name, credit: "导演" }))
      : [];
  const itemPlot = isMovie
    ? meta?.plot
    : (selectedSeriesEpisode?.episode.overview ?? meta?.plot);

  const runMetadataRefresh = async () => {
    // 重操作先确认：单条目刷新是 force 语义（图片覆盖重下），说清再动手
    if (!(await confirm(refreshItemConfirm(detail?.title ?? "此条目")))) return;
    setKicking(true);
    try {
      await refreshItemMetadata(libraryId, mediaItemId);
      // 立刻重拉一次详情：服务端的 scraping 标志会接管后续状态展示，
      // 前端不再盲等固定秒数（那种写法一旦离开页面状态就丢了）
      reload();
      // 后台任务在响应发出后才起跑，上面那次 reload 可能抢在 scraping
      // 标志立起之前拉到 false——稍后再拉一次兜底，否则界面毫无动静、
      // 轮询也不会启动，用户会以为没点上
      setTimeout(reload, 1500);
    } catch (err) {
      setRefreshError(err instanceof Error ? err.message : "元数据刷新失败，请稍后重试");
    } finally {
      setKicking(false);
    }
  };

  return (
    // rounded-2xl + overflow 裁切：顶部剧照渐变到纯黑内容板，方角
    // 会与全站"浮起圆角卡片"的形状语言冲突——按侧栏同规格圆角收尾。
    // max-md:rounded-none：这套圆角只在桌面成立——桌面外壳有 p-3.5 的留白，
    // 圆角落在留白里、背景大图从四周透出，才是一张"浮起的卡片"。窄屏是通栏
    // 满屏布局，没有留白，圆角直接压在屏幕边上：顶栏的吸顶雾层被这层
    // overflow 一裁，就成了贴在屏幕顶上的一块圆角色块（手机上肉眼可见的
    // 两个缺角），底边同理被 Home 指示条切掉。手机上一律方角、真通栏。
    <div className="detail-ambient scroll-thin scroll-safe relative isolate h-full overflow-y-auto rounded-2xl max-md:rounded-none">
      {/* 没有任何 Hero 图层：全站背景此刻就是本片剧照（沉浸覆盖 + 本页豁免
          全局蒙版，见 app-shell 的 isHome），大图直出、零边界；.detail-ambient
          在滚动容器上铺「透明 → 纯黑」的渐变板托住下方内容（见 globals.css）。
          顶栏首屏只有返回键与操作入口浮在剧照上。 */}
      <PageNav
        title={detail.title}
        fallback={navFallback}
        actions={
          canManageLibraries || (canSubscribe && detail.tmdb_id > 0) ? (
            <ItemActionsMenu
              canManage={canManageLibraries}
              scraping={scrapingNow}
              searchHref={`/search?q=${encodeURIComponent(detail.title)}` as Route}
              onReidentify={() => setReidentifyOpen(true)}
              onRefreshMetadata={runMetadataRefresh}
              onChangeArtwork={() => setArtworkOpen(true)}
              scrapeLibraryName={detail.scrape_library_name}
              onChangeScrapeLibrary={() => setScrapeLibraryOpen(true)}
              onTransfer={() => setTransferOpen(true)}
              onDelete={() => setDeleteOpen(true)}
              // 未识别条目（tmdb_id=0）没有订阅锚点，不给洗版入口
              onUpgrade={
                canSubscribe && detail.tmdb_id > 0
                  ? () => {
                      const existing = subscriptionOf({
                        id: String(detail.tmdb_id),
                        type: detail.kind,
                      });
                      if (existing) {
                        // 一部影片只有一个订阅：并入既有订阅，跳详情直接开弹层
                        router.push(
                          `/subscriptions/${existing.id}?upgrade-run=1` as Route,
                        );
                        return;
                      }
                      void openSubscribe(
                        {
                          id: String(detail.tmdb_id),
                          title: detail.title,
                          rating: 0,
                          posterUrl: detail.poster_url ?? "",
                          type: detail.kind,
                          year: detail.year ?? undefined,
                        },
                        { upgradeIntent: true },
                      );
                    }
                  : undefined
              }
            />
          ) : null
        }
      />

      {/* 氛围留白：这一段什么都不放，让剧照完整呼吸 */}
      <div className="h-[30vh] min-h-[180px] max-md:h-[22vh] max-md:min-h-[120px]" />

      {/* 内容层：-mt-28/pt-28 与 .detail-ambient 的渐变起点对齐——渐变从标题
          上方开始压暗，音轨附近已接近纯黑，下面保持全黑。 */}
      <div className="relative z-10 -mt-28 pb-12 pt-28">
      {/* —— 头部信息区 —— */}
      <div className="relative z-10 px-12 pt-6 max-md:px-4 max-md:pt-3">
        <div className="min-w-0 max-w-5xl pb-1">
          <h1 className="text-on-image text-[42px] font-bold leading-[1.1] tracking-[-0.02em] text-white max-md:text-[28px]">
            {detail.title}
          </h1>
          {!isMovie && selectedSeriesEpisode && (
            <p className="text-on-image mt-2 text-body text-white/65 max-md:mt-1.5 max-md:text-ui">
              {`第 ${selectedSeriesEpisode.seasonNumber} 季 第 ${selectedSeriesEpisode.episode.episode_number} 集${
                selectedSeriesEpisode.episode.name
                  ? ` - ${selectedSeriesEpisode.episode.name}`
                  : ""
              }`}
            </p>
          )}
          {(itemFacts.length > 0 ||
            itemResolutions.length > 0 ||
            itemHdrFormats.length > 0) && (
            <div className="tnum mt-3.5 flex flex-wrap items-center gap-x-3 gap-y-2 text-ui text-white/80 max-md:mt-2 max-md:gap-x-2 max-md:text-sub">
              {itemFacts.map((fact, index) => (
                <span key={fact} className="flex items-center gap-3 max-md:gap-2">
                  {index > 0 && <span aria-hidden="true">·</span>}
                  <span>{fact}</span>
                </span>
              ))}
              {itemResolutions.length > 0 && (
                <span className="flex items-center gap-3 max-md:gap-2">
                  {itemFacts.length > 0 && <span aria-hidden="true">·</span>}
                  <span>{itemResolutions.join(" / ")}</span>
                </span>
              )}
              {itemHdrFormats.length > 0 && (
                <span className="flex items-center gap-3 max-md:gap-2">
                  {(itemFacts.length > 0 || itemResolutions.length > 0) && (
                    <span aria-hidden="true">·</span>
                  )}
                  <span>{itemHdrFormats.join(" / ")}</span>
                </span>
              )}
            </div>
          )}
          {meta && meta.genres.length > 0 && (
            <p className="text-on-image mt-3 text-ui leading-6 text-white/72 max-md:mt-2 max-md:text-sub">
              {meta.genres.join(" · ")}
            </p>
          )}
          <MediaTrackRows
            files={trackFiles}
            selectedFileId={selectedTrackFile?.id ?? null}
            onSelectedFileIdChange={setSelectedTrackFileId}
            onChanged={reload}
          />

          {/* 播放入口：只在真有在位文件时出现。缺集/待回收的版本给一个点了
              必然报错的按钮，比不给更糟。剧集跟随分集区当前选中的那一集。

              位置刻意排在音轨 / 字幕之后：这一屏的动线是「先挑版本与轨道，
              再决定开播」，播放键作为这段选择的落点，而不是把它插在标题与
              轨道之间、把一次连续的阅读切成两截。 */}
          {availableTrackFiles.length > 0 && (isMovie || selectedSeriesEpisode) && (
            <PlayAction
              watched={watched}
              onPlay={() => {
                // 退出播放要回到用户离开的这一屏（含季集查询参数）。导航
                // 状态不进播放页地址（§6.10 地址就是分享凭证），走
                // sessionStorage。读 location 而不是 useSearchParams()：
                // 后者会把整页拖进「必须包 Suspense」的预渲染约束。
                rememberPlayerReturnPath(window.location.pathname + window.location.search);
                router.push(
                  playHref(detail.media_item_id, {
                    season:
                      !isMovie && selectedSeriesEpisode
                        ? selectedSeriesEpisode.seasonNumber
                        : undefined,
                    episode:
                      !isMovie && selectedSeriesEpisode
                        ? selectedSeriesEpisode.episode.episode_number
                        : undefined,
                  }) as Route,
                );
              }}
            />
          )}

          {/* 刮削进行中的状态条：与库页的整库刷新面板同一套语言（阶段文案
              也同源），状态在服务端——离开页面/刷新浏览器回来照样看得到 */}
          {scrapingNow && (
            <div className="mt-4 flex max-w-2xl items-center gap-2 rounded-xl border border-[var(--info)]/25 bg-[var(--info)]/[0.07] px-4 py-2.5 text-sub font-medium text-[var(--info)]">
              <span className="size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-[var(--info)]/30 border-t-[var(--info)]" />
              <span className="truncate">
                正在刷新元数据
                {detail.scraping_phase ? ` · ${detail.scraping_phase}` : ""}
              </span>
              <span className="ml-auto shrink-0 text-caption font-normal text-white/45">
                完成后自动更新本页
              </span>
            </div>
          )}

          {/* 元数据刷新的失败提示（识别相关的结论都在「修正识别结果」弹窗里给） */}
          {refreshError && (
            <div className="mt-4 max-w-2xl rounded-xl border border-white/[0.1] bg-[rgba(14,16,22,0.6)] px-4 py-3 text-sub leading-6 text-[#ff9f9f] backdrop-blur-md">
              {refreshError}
            </div>
          )}
        </div>
      </div>

      {/* 简介承接标题、类型与介质轨信息；电影与剧集保持同一阅读路径。 */}
      {itemPlot && (
        <div className="mt-4 px-12 max-md:px-4">
          <ExpandablePlot text={itemPlot} />
        </div>
      )}

      <div className="mt-9 space-y-8 px-12 max-md:mt-6 max-md:space-y-6 max-md:px-4">
        {/* —— 剧集分集区：季选择 + 分集横滚卡 + 选中集的简介/规格/文件 —— */}
        {!isMovie && detail.seasons.length > 0 && (
          <SeasonEpisodesSection
            libraryId={libraryId}
            detail={detail}
            initialSeason={initialSeason}
            initialEpisode={initialEpisode}
            onEpisodeChange={setSelectedSeriesEpisode}
          />
        )}

        {/* —— 演职员：与发现页条目详情共用同一个组件。头像来自 NFO 的 <thumb>，
            NFO 没写的按姓名回填库内档案的 profile_path（见后端 _fill_actor_thumbs）；
            TMDB 本就没有照片的人渲染姓名首字占位 —— */}
        {meta && (
          <CastRow
            cast={[
              // 导演与 Jellyfin People 同源；旧条目没有人物关系时才退回姓名占位。
              ...directorCast,
              ...meta.actors.map((actor) => ({
                name: actor.name,
                role: actor.role,
                avatarUrl: actor.thumb_url ? cachedImageUrl(actor.thumb_url) : null,
                tmdbPersonId: actor.tmdb_person_id,
              })),
            ]}
          />
        )}

        {/* 文件区固定收尾：电影列全部版本；剧集只列当前选中集的全部版本。
            两者共用同一套弱化、默认折叠样式，未入库集自然不渲染。 */}
        {(() => {
          const files = isMovie ? detail.files : (selectedSeriesEpisode?.files ?? []);
          if (files.length === 0) return null;
          return (
            <FileSection
              files={files}
              title="文件"
              onDeleteFile={canManageLibraries ? setDeleteFileTarget : undefined}
              onRestoreFile={canManageLibraries ? restoreTrashedFile : undefined}
              onPurgeFile={canManageLibraries ? purgeTrashedFile : undefined}
            />
          );
        })()}

        {/* 外部词条像友情链接一样固定在文件区之后，继续使用原有的新窗口跳转；
            豆瓣在移动端沿用 App 唤起逻辑。 */}
        {(detail.tmdb_id > 0 || detail.imdb_id || detail.douban_id) && (
          <nav
            aria-label="外部词条"
            className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-white/[0.04] pt-4 text-caption"
          >
            <span className="text-[var(--text-faint)]">相关链接</span>
            {detail.tmdb_id > 0 && (
              <SourceLink
                href={`https://www.themoviedb.org/${detail.kind}/${detail.tmdb_id}`}
                label="TMDB"
              />
            )}
            {detail.imdb_id && (
              <SourceLink
                href={`https://www.imdb.com/title/${detail.imdb_id}/`}
                label="IMDb"
              />
            )}
            {detail.douban_id && (
              <SourceLink
                href={
                  doubanAppHref ??
                  `https://movie.douban.com/subject/${detail.douban_id}/`
                }
                label="豆瓣"
              />
            )}
          </nav>
        )}
      </div>
      </div>

      {canManageLibraries && <DeleteDialog
        open={deleteOpen}
        detail={detail}
        onClose={() => setDeleteOpen(false)}
        onDeleted={() => {
          invalidateLibraryDetailSnapshot(libraryId);
          router.replace(`/library/${libraryId}` as Route);
        }}
        libraryId={libraryId}
      />}

      {canManageLibraries && <DeleteFileDialog
        file={deleteFileTarget}
        detail={detail}
        libraryId={libraryId}
        onClose={() => setDeleteFileTarget(null)}
        onDeleted={(deletedItem) => {
          setDeleteFileTarget(null);
          invalidateLibraryDetailSnapshot(libraryId);
          if (deletedItem) {
            router.replace(`/library/${libraryId}` as Route);
          }
          else reload();
        }}
      />}

      {canManageLibraries && <TransferDialog
        open={transferOpen}
        detail={detail}
        libraryId={libraryId}
        sourceLibraryName={library?.name ?? null}
        onClose={() => setTransferOpen(false)}
        onFinished={handleTransferFinished}
        onTransferred={(targetLibraryId) => {
          router.replace(`/library/${targetLibraryId}/item/${mediaItemId}` as Route);
        }}
      />}

      {canManageLibraries && <ReidentifyDialog
        open={reidentifyOpen}
        libraryId={libraryId}
        mediaItemId={mediaItemId}
        onClose={() => {
          setReidentifyOpen(false);
          // 条目可能已经不在了（文件全改挂走 / 全标为非独立作品）：重拉
          // 失败会落到本页的兜底态，引导用户回库存页
          if (reidentifyDirty) {
            setReidentifyDirty(false);
            reload();
          }
        }}
        onApplied={() => {
          invalidateLibraryDetailSnapshot(libraryId);
          setReidentifyDirty(true);
        }}
      />}

      {canManageLibraries && <ArtworkPickerDialog
        open={artworkOpen}
        libraryId={libraryId}
        mediaItemId={mediaItemId}
        onClose={() => setArtworkOpen(false)}
        onChanged={reload}
      />}

      {canManageLibraries && (
        <ScrapeLibraryDialog
          open={scrapeLibraryOpen}
          libraryId={libraryId}
          mediaItemId={mediaItemId}
          kind={detail.kind}
          current={detail.scrape_library_id}
          onClose={() => setScrapeLibraryOpen(false)}
          onChanged={reload}
        />
      )}
    </div>
  );
}

/**
 * 播放入口（主行动按钮 + 续播进度）。
 *
 * 三态一个按钮，而不是并排铺三个入口：
 *   - 从未播过 → 「播放」
 *   - 播到一半 → 「继续观看」，右侧补一条细进度条与「看到 xx:xx · 剩余 xx」
 *   - 已看完   → 「重新播放」，右侧换成一枚对勾与观看次数
 *
 * 主流媒体产品（Netflix / Apple TV / Disney+）在详情页都是这么处理的：
 * 一个页面只保留一个「现在就看」的落点，进度信息挂在按钮旁边做解释，
 * 而不是让用户先在「播放」和「继续播放」两个按钮之间做一次选择题。
 *
 * 尺寸也按这套规格给：整页标题 42px、正文栏宽 max-w-4xl，原来 px-6/py-2.5
 * 的小胶囊放在这样的版面里明显不像主行动按钮，因此抬到 h-12 + text-body，
 * 窄屏改为整行铺满（拇指区最容易命中的形状）。
 */
function PlayAction({
  watched,
  onPlay,
}: {
  watched: PlaybackWatchState | null;
  onPlay: () => void;
}) {
  const positionMs = watched?.position_ms ?? 0;
  const durationMs = watched?.duration_ms ?? null;
  const finished = watched?.played ?? false;
  // 「能续播」以服务端结论为准：已看完的重播从头开始（播放器同一套判定），
  // 所以看完之后不再展示续播点，否则点进去的位置和文案对不上。
  const resumable = !finished && positionMs > 0;
  // 进度条至少留 2%：真按比例画，刚开头几分钟的记录在条上是看不见的一根线。
  const percent =
    resumable && durationMs && durationMs > 0
      ? Math.min(100, Math.max(2, Math.round((positionMs / durationMs) * 100)))
      : null;
  const remainingMinutes =
    resumable && durationMs && durationMs > positionMs
      ? Math.round((durationMs - positionMs) / 60000)
      : null;
  const label = finished ? "重新播放" : resumable ? "继续观看" : "播放";
  const progressText = resumable
    ? [
        `看到 ${formatClock(positionMs)}`,
        remainingMinutes && remainingMinutes >= 1
          ? `剩余 ${formatRuntimeMinutes(remainingMinutes)}`
          : durationMs
            ? "即将看完"
            : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : null;

  return (
    <div className="mt-5 flex max-w-4xl flex-wrap items-center gap-x-5 gap-y-3 max-md:mt-4">
      <button
        type="button"
        onClick={onPlay}
        aria-label={progressText ? `${label}，${progressText}` : label}
        className="inline-flex h-12 shrink-0 items-center justify-center gap-2.5 rounded-full bg-white px-8 text-body font-semibold text-black shadow-[0_10px_30px_rgba(0,0,0,0.35)] transition duration-200 hover:bg-white/90 active:scale-[0.985] max-md:h-11 max-md:w-full max-md:px-6"
      >
        <PlayIcon className="size-5" />
        {label}
      </button>

      {progressText && (
        <div className="min-w-0 max-md:w-full">
          <div
            className="h-1 w-44 overflow-hidden rounded-full bg-white/[0.18] max-md:w-full"
            role="progressbar"
            aria-valuenow={percent ?? 0}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="观看进度"
          >
            <div className="h-full rounded-full bg-white/85" style={{ width: `${percent ?? 0}%` }} />
          </div>
          <p className="tnum mt-1.5 text-caption text-white/60">{progressText}</p>
        </div>
      )}

      {finished && (
        <p className="flex items-center gap-1.5 text-caption text-white/55">
          <CheckIcon className="size-3.5 text-emerald-300/90" />
          已看完
          {watched && watched.play_count > 1 ? ` · 看过 ${watched.play_count} 次` : ""}
        </p>
      )}
    </div>
  );
}

/**
 * 条目操作 ⋯ 菜单：修正识别结果 / 刷新元数据 / 更换图片 / 转移 / 删除。
 *
 * 这几个都是低频且不可逆（改身份锚、重下全套图、搬目录、删文件）的操作，
 * 摆成常驻大按钮既压着正文，又把「误点」的成本摊在最显眼的位置。收进顶栏
 * 右上角的 ⋯ 后，页面主区只剩内容；跑起来之后的状态仍由正文里的进度条
 * 完整交代（与媒体库页「操作进 ⋯、状态看正文」的分工一致）。
 *
 * 「转移到其他库」与「修正识别结果」是**两种不同的错**的补救，菜单里紧挨着
 * 摆：前者是"片子认对了、库放错了"（韩剧进了大陆剧库），后者是"片子认错了"。
 *
 * 「修正识别结果」带省略号是有意的——它开的是一个拍板面板（重跑识别链、
 * 摆出结论让人选），不是点下去就改身份的一次性动作。
 */
function ItemActionsMenu({
  canManage,
  scraping,
  searchHref,
  onReidentify,
  onRefreshMetadata,
  onChangeArtwork,
  scrapeLibraryName,
  onChangeScrapeLibrary,
  onTransfer,
  onDelete,
  onUpgrade,
}: {
  /** 媒体库管理权限：识别/刮削/图片/转移/删除这些条目管理项按它显隐 */
  canManage: boolean;
  scraping: boolean;
  /** 站点资源搜索直达（预填片名）：手动补版本/换版本的入口 */
  searchHref: Route;
  onReidentify: () => void;
  onRefreshMetadata: () => void;
  onChangeArtwork: () => void;
  /** 当前刮削归属库名；null=无归属（跟全局设置） */
  scrapeLibraryName: string | null;
  onChangeScrapeLibrary: () => void;
  onTransfer: () => void;
  onDelete: () => void;
  /** 洗版入口（quality-upgrade.md §13.5）；无订阅权限或条目未识别时不传 */
  onUpgrade?: () => void;
}) {
  const router = useRouter();
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  const running = scraping;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="更多操作"
          className={`${PAGE_NAV_BUTTON_CLASS} relative data-[state=open]:bg-black/55 data-[state=open]:text-white`}
        >
          <MoreIcon className="size-[18px] max-md:size-[22px]" />
          {/* 任务在跑时给触发键点一个小点：菜单收起后也知道后台还在忙 */}
          {running && (
            <span className="absolute right-1 top-1 size-1.5 animate-pulse rounded-full bg-[var(--info)]" />
          )}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[11rem] p-1"
        >
          {canManage && (
            <DropdownMenu.Item
              onSelect={() => router.push(searchHref)}
              className={itemClass}
            >
              搜索资源
            </DropdownMenu.Item>
          )}
          {onUpgrade && (
            <DropdownMenu.Item onSelect={onUpgrade} className={itemClass}>
              洗版…
            </DropdownMenu.Item>
          )}
          {canManage && (
            <>
              <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
              <DropdownMenu.Item onSelect={onReidentify} className={itemClass}>
                修正识别结果…
              </DropdownMenu.Item>
              <DropdownMenu.Item
                onSelect={onRefreshMetadata}
                disabled={scraping}
                className={itemClass}
              >
                {scraping ? "正在刷新元数据…" : "刷新元数据"}
              </DropdownMenu.Item>
              <DropdownMenu.Item onSelect={onChangeArtwork} className={itemClass}>
                更换图片…
              </DropdownMenu.Item>
              {/* 刮削归属：一条目只有一份档案与一张海报，按哪个库的语言/选图
                  设置刮由它决定。菜单里带出当前值——不摆出来用户无从解释
                  "为什么这部片没跟我的动漫库设置"（设计文档 §14.5） */}
              <DropdownMenu.Item onSelect={onChangeScrapeLibrary} className={itemClass}>
                刮削归属：{scrapeLibraryName ?? "跟随全局"}…
              </DropdownMenu.Item>
              <DropdownMenu.Item onSelect={onTransfer} className={itemClass}>
                转移到其他库…
              </DropdownMenu.Item>
              <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
              <DropdownMenu.Item
                onSelect={onDelete}
                className={`${itemClass} !text-[#ff9f9f] data-[highlighted]:!bg-[rgba(255,90,90,0.16)] data-[highlighted]:!text-[#ffb4b4]`}
              >
                删除影片
              </DropdownMenu.Item>
            </>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/* ------------------------------------------------------------------------ */
/* 展示格式化：ffprobe 原始值 → 用户认知的规格语言                              */
/* ------------------------------------------------------------------------ */

/** 图片地址：本地美术图是 API 相对路径（补 base），TMDB 图床走缓存代理。 */
function imageUrl(url: string | null): string {
  if (!url) return "";
  return /^https?:\/\//i.test(url) ? cachedImageUrl(url) : resolveRequestUrl(url);
}

const VIDEO_CODEC_LABELS: Record<string, string> = {
  hevc: "HEVC",
  h264: "H.264",
  h265: "HEVC",
  av1: "AV1",
  vc1: "VC-1",
  mpeg2video: "MPEG-2",
  vp9: "VP9",
};

function videoCodecLabel(codec: string | null): string | null {
  if (!codec) return null;
  return VIDEO_CODEC_LABELS[codec.toLowerCase()] ?? codec.toUpperCase();
}

/** ffprobe 小数帧率保留行业常用精度，如 23.976 / 29.97 / 59.94 fps。 */
function frameRateLabel(frameRate: number | null): string | null {
  if (frameRate == null || frameRate <= 0) return null;
  return `${Math.round(frameRate * 1000) / 1000} fps`;
}

/* ------------------------------------------------------------------------ */
/* 子组件                                                                     */
/* ------------------------------------------------------------------------ */

/**
 * 电影与分集简介共用的四行摘要。只有真实发生溢出时才出现展开入口；剧集
 * 切换分集会先恢复折叠，再按新文案重新测量，避免沿用上一集的展开状态。
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
        className={`text-on-image text-body-lg leading-7 text-white/78 ${
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

/** 外部信息源链接：新窗口打开站点词条，样式与「在 TMDB 打开核对」保持一致。 */
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
 * 季号 → 展示名（0 是特别篇，TMDB 的 specials 惯例）。整季一个文件都没有的
 * 季标"未入库"：季选择器里元数据的季和实有的季混在一起，不标出来用户会以为
 * 自己本地存了那么多季。原生 select 收起时显示的就是选中项的文本，所以这个
 * 后缀在展开和收起两种状态下都看得到。
 */
function seasonLabel(season: number, owned: boolean): string {
  const name = season === 0 ? "特别篇" : `第 ${season} 季`;
  return owned ? name : `${name} · 未入库`;
}

/**
 * 剧集分集区（播放器式）：季选择器 + 分集横滚缩略图卡（剧照 + 集名，
 * 缺集置灰）。本区只负责选择，当前集的简介与轨道回到 Hero，物理文件回到
 * 页面底部的统一折叠文件区。分集信息本地刮削优先，TMDB 分季兜底。
 */
function SeasonEpisodesSection({
  libraryId,
  detail,
  initialSeason,
  initialEpisode,
  onEpisodeChange,
}: {
  libraryId: number;
  detail: LibraryItemDetail;
  initialSeason?: number;
  initialEpisode?: number;
  onEpisodeChange?: (selection: SelectedEpisodeContext | null) => void;
}) {
  const seasons = detail.seasons;
  // 季选择器列的是「元数据的季 ∪ 库里实有的季」，本地没有的季也在里面（看得到
  // 缺口）。所以要单独算出哪些季真在库——口径与后端 owned_seasons 一致：台账
  // 文件的季号集合。
  const ownedSeasons = useMemo(
    () => new Set(detail.files.map((f) => f.season_number)),
    [detail.files],
  );
  // 默认落在第一个在库的季，而不是季号最小的那一季：只存了第 5、6 季的剧，
  // 打开详情页就停在满屏置灰的第 1 季，第一眼像"我的片子没了"
  const defaultSeason = seasons.find((s) => ownedSeasons.has(s)) ?? seasons[0];
  // 最近观看入口只有在目标季仍有在位文件时才采纳；记录过期则回退普通详情。
  const requestedSeason =
    initialSeason != null && ownedSeasons.has(initialSeason) ? initialSeason : undefined;
  const [season, setSeason] = useState(() => requestedSeason ?? defaultSeason);
  const [data, setData] = useState<SeasonEpisodes | null>(null);
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const initialEpisodeScrolled = useRef(false);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setFailed(false);
    getItemEpisodes(libraryId, detail.media_item_id, season)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        // 最近观看入口优先选中目标集；目标已不在库时安全回退到第一集在库内容。
        const requested =
          season === requestedSeason && initialEpisode != null
            ? result.episodes.find(
                (episode) => episode.episode_number === initialEpisode && episode.owned,
              )
            : undefined;
        const first = requested ?? result.episodes.find((e) => e.owned) ?? result.episodes[0];
        setSelected(first?.episode_number ?? null);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
    // detail.file_count：单文件删除后详情重拉，分集的 owned/file_ids 也要
    // 跟着刷新（用文件数而不是整个 detail 做依赖——刮削轮询也会换 detail
    // 对象，不该每轮都重拉分集）
  }, [
    libraryId,
    detail.media_item_id,
    detail.file_count,
    season,
    requestedSeason,
    initialEpisode,
  ]);

  // 分集数据异步到达后，把最近观看对应的集卡横向滚到中间；只执行一次，
  // 后续用户手动换季/选集不抢滚动位置。
  useEffect(() => {
    if (
      initialEpisodeScrolled.current ||
      season !== requestedSeason ||
      selected == null ||
      selected !== initialEpisode
    ) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      sectionRef.current
        ?.querySelector<HTMLElement>(`[data-episode-number="${selected}"]`)
        ?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
      initialEpisodeScrolled.current = true;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [initialEpisode, requestedSeason, season, selected]);

  const filesById = useMemo(
    () => new Map(detail.files.map((f) => [f.id, f])),
    [detail.files],
  );
  const current =
    data?.season_number === season
      ? (data.episodes.find((e) => e.episode_number === selected) ?? null)
      : null;
  const currentFiles = useMemo(
    () =>
      (current?.file_ids ?? [])
        .map((id) => filesById.get(id))
        .filter((f): f is LibraryItemFile => Boolean(f)),
    [current, filesById],
  );
  const ownedCount = data ? data.episodes.filter((e) => e.owned).length : 0;

  // Hero 的简介、文件、分辨率和轨道必须与分集区保持同一选择；数据加载或
  // 换季期间先清空，避免短暂显示上一季上一集的信息。
  useEffect(() => {
    onEpisodeChange?.(
      current
        ? {
            seasonNumber: season,
            episode: current,
            files: currentFiles,
          }
        : null,
    );
  }, [current, currentFiles, onEpisodeChange, season]);

  return (
    <section ref={sectionRef}>
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
          分集
        </h2>
        {seasons.length > 1 ? (
          <select
            value={season}
            onChange={(e) => setSeason(Number(e.target.value))}
            className="rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-sub text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
          >
            {seasons.map((s) => (
              <option key={s} value={s}>
                {seasonLabel(s, ownedSeasons.has(s))}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-sub text-[var(--text-muted)]">
            {seasonLabel(season, ownedSeasons.has(season))}
          </span>
        )}
        {data && (
          <span className="tnum text-sub text-[var(--text-faint)]">
            在库 {ownedCount} / {data.episodes.length} 集
          </span>
        )}
      </div>

      {failed && (
        <p className="text-sub text-[var(--text-muted)]">分集信息加载失败，请稍后重试。</p>
      )}
      {!data && !failed && (
        <div className="flex items-center gap-2.5 py-6 text-sub text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在读取分集信息…
        </div>
      )}

      {/* 分集横滚卡：16:9 剧照 + "N. 集名"；缺集置灰，选中亮环。
          走 HScroller 以复用发现页海报行的左右翻页钮，避免用户看不出这行可横滑。 */}
      {data && (
        <HScroller className="-mx-1 gap-3 px-1 pb-1 pt-1">
          {data.episodes.map((episode) => (
            <EpisodeCard
              key={episode.episode_number}
              episode={episode}
              selected={episode.episode_number === selected}
              onSelect={() => setSelected(episode.episode_number)}
            />
          ))}
        </HScroller>
      )}
    </section>
  );
}

/** 分集横滚卡：16:9 剧照缩略图 + 集号集名；缺集置灰。

    观看状态与首页「最近观看」卡同一套视觉语言（同一张 playback_state 表，
    Jellyfin 客户端里看的进度也在内）：看了一半底部细进度条，看完右上角
    绿色对勾——用户扫一眼分集行就知道追到哪了。 */
function EpisodeCard({
  episode,
  selected,
  onSelect,
}: {
  episode: LibraryEpisode;
  selected: boolean;
  onSelect: () => void;
}) {
  // 看完 = 满条绿；看一半 = 百分比蓝；有记录但算不出百分比（无时长）给
  // 一根 60% 透明的整条兜底——三种形态与 RecentWatchCard 逐一对应
  const progress = episode.played ? 100 : episode.progress_percent;
  return (
    <button
      type="button"
      data-episode-number={episode.episode_number}
      onClick={onSelect}
      aria-pressed={selected}
      className={`w-[200px] shrink-0 rounded-xl text-left outline-none transition ${
        episode.owned ? "" : "opacity-45 saturate-50"
      }`}
    >
      <div
        className={`relative aspect-video overflow-hidden rounded-xl bg-[#141824] transition ${
          selected
            ? "ring-2 ring-white/85 shadow-[0_10px_30px_rgba(0,0,0,0.5)]"
            : "ring-1 ring-white/[0.08] hover:ring-white/35"
        }`}
      >
        <PosterImage
          src={imageUrl(episode.still_url)}
          alt={`第 ${episode.episode_number} 集剧照`}
          className="size-full object-cover"
          fallback={
            <span className="tnum flex size-full items-center justify-center text-[22px] font-bold text-white/20">
              {episode.episode_number}
            </span>
          }
        />
        {/* 右上角状态位：缺集与已看对勾同排（看过之后文件丢了两者会同时出现） */}
        {(!episode.owned || episode.played) && (
          <div className="pointer-events-none absolute right-1.5 top-1.5 flex items-center gap-1">
            {!episode.owned && (
              <span className="rounded bg-black/60 px-1.5 py-px text-micro font-semibold text-[var(--warn)]">
                缺
              </span>
            )}
            {episode.played && (
              <span
                aria-label="已看完"
                className="flex size-5 items-center justify-center rounded-full bg-[var(--ok)] text-[#07120c] shadow-lg"
              >
                <CheckIcon className="size-3 stroke-[2.5]" />
              </span>
            )}
          </div>
        )}
        {progress != null ? (
          <div className="pointer-events-none absolute inset-x-1.5 bottom-1.5 h-[3px] overflow-hidden rounded-full bg-white/25">
            <div
              className={`h-full rounded-full ${episode.played ? "bg-[var(--ok)]" : "bg-[var(--accent-2)]"}`}
              style={{ width: `${progress}%` }}
            />
          </div>
        ) : (
          episode.position_ms > 0 && (
            <div className="pointer-events-none absolute inset-x-1.5 bottom-1.5 h-[3px] rounded-full bg-[var(--accent-2)]/60" />
          )
        )}
      </div>
      <p
        className={`tnum mt-1.5 truncate text-sub ${
          selected ? "font-semibold text-white" : "text-[var(--text)]"
        }`}
      >
        {episode.episode_number}. {episode.name ?? `第 ${episode.episode_number} 集`}
      </p>
    </button>
  );
}

/** 从完整文件路径提取保存目录，同时兼容 NAS/POSIX 与 Windows 分隔符。 */
function fileDirectory(filePath: string): string {
  const normalized = filePath.replace(/[\\/]+$/, "");
  const separatorIndex = Math.max(
    normalized.lastIndexOf("/"),
    normalized.lastIndexOf("\\"),
  );
  if (separatorIndex < 0) return "—";
  if (separatorIndex === 0) return normalized.slice(0, 1);
  return normalized.slice(0, separatorIndex);
}

/**
 * 文件区：详情正文的最后一块。电影与剧集当前集完全共用一套弱化样式，
 * 默认只露出文件名，点击后按物理文件展开尺寸、视频与码率。
 */
function FileSection({
  files,
  title,
  onDeleteFile,
  onRestoreFile,
  onPurgeFile,
}: {
  files: LibraryItemFile[];
  title: string;
  onDeleteFile?: (file: LibraryItemFile) => void;
  /** 待回收行的恢复/立即清理（library-file-recycle.md §7）；未传则只读展示 */
  onRestoreFile?: (file: LibraryItemFile) => void;
  onPurgeFile?: (file: LibraryItemFile) => void;
}) {
  return (
    <section>
      <h2 className="mb-3 text-ui font-medium tracking-[-0.01em] text-[var(--text-muted)]">
        {title}{" "}
        <span className="tnum text-caption font-normal text-[var(--text-faint)]">
          {files.length}
        </span>
      </h2>
      <div className="overflow-hidden rounded-xl border border-white/[0.04] bg-white/[0.015]">
        {files.map((file) => (
          <FileRow
            key={file.id}
            file={file}
            onDelete={onDeleteFile ? () => onDeleteFile(file) : undefined}
            onRestore={onRestoreFile ? () => onRestoreFile(file) : undefined}
            onPurge={onPurgeFile ? () => onPurgeFile(file) : undefined}
          />
        ))}
      </div>
    </section>
  );
}

/** 待回收行的倒计时文案：直读 purge_after，展示精度与清理周期无关。 */
function purgeCountdown(purgeAfter: string | null): string {
  if (!purgeAfter) return "做种保护中，不自动清理";
  const ms = new Date(purgeAfter).getTime() - Date.now();
  if (ms <= 0) return "即将自动清理";
  const days = Math.floor(ms / 86_400_000);
  const hours = Math.floor((ms % 86_400_000) / 3_600_000);
  if (days > 0) return `预计 ${days} 天 ${hours} 小时后自动清理`;
  if (hours > 0) return `预计 ${hours} 小时后自动清理`;
  return "预计 1 小时内自动清理";
}

function FileRow({
  file,
  onDelete,
  onRestore,
  onPurge,
}: {
  file: LibraryItemFile;
  onDelete?: () => void;
  onRestore?: () => void;
  onPurge?: () => void;
}) {
  // 每个物理文件独立展开，多版本无需再维护额外的版本切换状态。
  const [expanded, setExpanded] = useState(false);
  const pictureInfo = [
    file.resolution ? formatVideoResolution(file.resolution) : null,
    file.hdr,
  ]
    .filter((value): value is string => Boolean(value))
    .join(" · ");
  const codec = videoCodecLabel(file.video_codec);
  const frameRate = frameRateLabel(file.frame_rate);
  const bitRate = file.bit_rate
    ? `${(file.bit_rate / 1_000_000).toFixed(1)} Mbps`
    : "尚未探测";
  const detailsId = `file-details-${file.id}`;

  return (
    <div className="border-b border-white/[0.035] last:border-b-0">
      <div className="group/filerow flex items-center gap-2 px-4 py-2.5 transition-colors hover:bg-white/[0.025]">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={() => setExpanded((value) => !value)}
          className="flex min-w-0 flex-1 items-center gap-2.5 text-left text-[var(--text-muted)] outline-none transition-colors hover:text-[var(--text)] focus-visible:text-[var(--text)]"
        >
          <ChevronRightIcon
            className={`size-3.5 shrink-0 transition-transform ${expanded ? "rotate-90" : ""}`}
          />
          <Tooltip
            content={
              <span className="tnum break-all font-mono text-caption leading-5">
                {file.file_path}
              </span>
            }
            maxWidth={520}
          >
            <span
              className={`min-w-0 truncate text-sub ${
                file.state === "trashed" ? "text-[var(--text-faint)] line-through" : ""
              }`}
            >
              {file.file_name}
            </span>
          </Tooltip>
        </button>
        {file.missing && (
          <span className="shrink-0 rounded border border-[var(--warn)]/30 px-1.5 py-px text-micro text-[var(--warn)]/80">
            文件缺失
          </span>
        )}
        {file.state === "trashed" && (
          <span className="shrink-0 rounded border border-white/15 px-1.5 py-px text-micro text-[var(--text-faint)]">
            待回收
          </span>
        )}
        {file.state === "trashed" && onRestore && (
          <button
            type="button"
            onClick={onRestore}
            className="shrink-0 rounded-md px-2 py-1 text-caption font-medium text-[var(--text-muted)] transition hover:bg-white/[0.06] hover:text-white"
          >
            恢复
          </button>
        )}
        {file.state === "trashed" && onPurge && (
          <button
            type="button"
            onClick={onPurge}
            className="shrink-0 rounded-md px-2 py-1 text-caption font-medium text-[var(--text-faint)] transition hover:bg-white/[0.06] hover:text-[#ff9f9f]"
          >
            立即清理
          </button>
        )}
        {file.state !== "trashed" && onDelete && (
          <button
            type="button"
            aria-label="删除此文件"
            title="删除此文件"
            onClick={onDelete}
            className="touch-reveal shrink-0 rounded-md p-1.5 text-[var(--text-faint)] opacity-0 transition group-hover/filerow:opacity-100 hover:bg-white/[0.05] hover:text-[#ff9f9f]"
          >
            <TrashIcon className="size-4" />
          </button>
        )}
      </div>
      {file.state === "trashed" && (
        <p className="-mt-1 px-10 pb-2 text-caption leading-5 text-[var(--text-faint)] max-md:px-4">
          {purgeCountdown(file.purge_after)}
          {file.trash_note ? ` · ${file.trash_note}` : ""}
        </p>
      )}
      {expanded && (
        <div
          id={detailsId}
          className="border-t border-white/[0.035] bg-black/[0.08] px-10 py-4 max-md:px-4"
        >
          <dl className="grid max-w-2xl grid-cols-[72px_minmax(0,1fr)] gap-x-5 gap-y-3 text-sub max-md:grid-cols-[64px_minmax(0,1fr)]">
            <dt className="text-[var(--text-faint)]">保存目录</dt>
            <dd className="min-w-0 break-all font-mono text-caption leading-5 text-[var(--text-muted)]">
              {fileDirectory(file.file_path)}
            </dd>
            <dt className="text-[var(--text-faint)]">入库时间</dt>
            <dd className="tnum text-[var(--text-muted)]">
              {formatDateTime(file.added_at)}
              <span className="ml-2 text-[var(--text-faint)]">
                · {formatRelativeTime(file.added_at)}
              </span>
            </dd>
            <dt className="text-[var(--text-faint)]">文件尺寸</dt>
            <dd className="tnum text-[var(--text-muted)]">{formatBytes(file.size_bytes)}</dd>
            <dt className="text-[var(--text-faint)]">片源</dt>
            <dd className="text-[var(--text-muted)]">
              {mediaSourceDisplayLabel(file.media_source) || "未识别"}
              {file.media_source_manual && file.media_source !== USER_LOWEST_SOURCE && (
                <span className="ml-1.5 text-[var(--text-faint)]">（人工标注）</span>
              )}
            </dd>
            <dt className="text-[var(--text-faint)]">画面规格</dt>
            <dd className="tnum text-[var(--text-muted)]">
              {pictureInfo || "未能探测（文件不可达或尚未扫描）"}
            </dd>
            <dt className="text-[var(--text-faint)]">视频编码</dt>
            <dd className="tnum text-[var(--text-muted)]">{codec || "尚未探测"}</dd>
            <dt className="text-[var(--text-faint)]">色深</dt>
            <dd className="tnum text-[var(--text-muted)]">
              {file.bit_depth ? `${file.bit_depth}-bit` : "尚未探测"}
            </dd>
            <dt className="text-[var(--text-faint)]">帧率</dt>
            <dd className="tnum text-[var(--text-muted)]">{frameRate || "尚未探测"}</dd>
            <dt className="text-[var(--text-faint)]">色彩空间</dt>
            <dd className="tnum text-[var(--text-muted)]">
              {file.color_space || "尚未探测"}
            </dd>
            <dt className="text-[var(--text-faint)]">视频码率</dt>
            <dd className="tnum text-[var(--text-muted)]">{bitRate}</dd>
          </dl>
        </div>
      )}
    </div>
  );
}

/**
 * 转移到其他库的弹窗：选库 → 预览 → 确认 → 进度 → 结论，四步走完一条路。
 *
 * 存在的理由：入库选库要么靠收藏范围自动路由、要么靠订阅时手选，两条路
 * 都会错——最典型的是韩剧被判进了「大陆华语剧」。此前对这种错分没有任何
 * 补救手段，只能手工挪目录再两边重扫。
 *
 * 界面上必须讲清的三件事（都在预览步骤里）：
 * 1. **搬的是磁盘目录**，不是"从列表里挪一下"——把源目录与目标目录并排列出；
 * 2. **跨盘要复制**：目标与源不在同一块盘时是完整复制而非改名，耗时按体积
 *    走，且会断开与做种目录的硬链接（磁盘占用翻倍）——这条必须显式警示；
 * 3. **目标已有同名目录就不干**：宁可让用户自己决断，绝不覆盖或合并。
 */
function TransferDialog({
  open,
  detail,
  libraryId,
  sourceLibraryName,
  onFinished,
  onClose,
  onTransferred,
}: {
  open: boolean;
  detail: LibraryItemDetail;
  libraryId: number;
  sourceLibraryName: string | null;
  onFinished: (targetLibraryId: number) => void;
  onClose: () => void;
  onTransferred: (targetLibraryId: number) => void;
}) {
  // 可选目标库：同类型、非当前库（类型不同是"认错了片子"，该走重新识别）
  const [candidates, setCandidates] = useState<MediaLibrary[] | null>(null);
  const [targetId, setTargetId] = useState<number | null>(null);
  const [preview, setPreview] = useState<TransferPreview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 进行中/已完成的转移状态（后台任务，轮询同一个接口一路走到结论）
  const [status, setStatus] = useState<TransferStatus | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setTargetId(null);
    setPreview(null);
    setStatus(null);
    setError(null);
    listLibraries(detail.kind)
      .then((libs) => setCandidates(libs.filter((l) => l.id !== libraryId)))
      .catch(() => setError("读取媒体库列表失败，请稍后重试"));
  }, [open, detail.kind, libraryId]);

  // 选中目标库就立刻算预览：用户要先看清"搬到哪、搬多少"才谈得上确认
  useEffect(() => {
    if (!open || targetId == null) return;
    let alive = true;
    setLoadingPreview(true);
    setPreview(null);
    setError(null);
    previewItemTransfer(libraryId, detail.media_item_id, targetId)
      .then((data) => alive && setPreview(data))
      .catch((err) => alive && setError(err instanceof Error ? err.message : "预览失败"))
      .finally(() => alive && setLoadingPreview(false));
    return () => {
      alive = false;
    };
  }, [open, targetId, libraryId, detail.media_item_id]);

  // 转移进行中：每秒拉一次进度，跑完自动切到结论页（不用 useVisiblePolling——
  // 这是用户正盯着看的短任务，页面切后台也该继续走完，回来直接看到结果）
  const running = status?.running ?? false;
  useEffect(() => {
    if (!open || !running) return;
    const timer = setInterval(() => {
      getLibraryItemTransferStatus(libraryId)
        .then((next) => {
          setStatus(next);
          if (!next.running && next.errors.length === 0 && next.target_library_id != null) {
            onFinished(next.target_library_id);
          }
        })
        .catch(() => {});
    }, 1000);
    return () => clearInterval(timer);
  }, [open, running, libraryId, onFinished]);

  const run = async () => {
    if (targetId == null) return;
    setStarting(true);
    setError(null);
    try {
      await transferLibraryItem(libraryId, detail.media_item_id, targetId);
      const next = await getLibraryItemTransferStatus(libraryId);
      setStatus(next);
      if (!next.running && next.errors.length === 0 && next.target_library_id != null) {
        onFinished(next.target_library_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "转移失败，请稍后重试");
    } finally {
      setStarting(false);
    }
  };

  const busy = starting || running;
  const finished = status != null && !status.running;
  const canRun =
    preview != null && preview.blocked.length === 0 && preview.moves.length > 0 && !busy;

  return (
    <Modal
      open={open}
      onClose={busy ? () => {} : onClose}
      label="转移到其他媒体库"
      width="2xl"
      panelClassName="flex max-h-[80vh] flex-col"
    >
      <div className="scroll-thin flex-1 overflow-y-auto p-6">
        {finished ? (
          <TransferResult status={status!} />
        ) : (
          <>
            <h3 className="flex items-center gap-2 text-title-sm font-semibold text-[var(--text)]">
              <FolderIcon className="size-4.5 text-[var(--accent-2)]" />
              把「{detail.title}」转移到其他媒体库
            </h3>
            <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">
              分错库时用它补救（例如韩剧被判进了「大陆华语剧」）。
              <span className="text-white/80">磁盘上的整个条目目录</span>
              （视频、NFO、海报、字幕）会连同库存记录一起搬到目标库；
              目录名原样保留，需要规范化请到目标库运行「整理文件名」。
            </p>

            {/* —— 第一步：选目标库 —— */}
            <p className="mt-5 text-caption font-semibold uppercase tracking-[0.16em] text-[var(--text-faint)]">
              转移到
            </p>
            {candidates == null ? (
              <p className="mt-2 text-sub text-[var(--text-muted)]">正在读取媒体库…</p>
            ) : candidates.length === 0 ? (
              <p className="mt-2 text-sub leading-6 text-[#ffd08a]">
                没有其他{detail.kind === "movie" ? "电影" : "剧集"}
                库可选——请先在「媒体库」页新建一个，再回来转移。
              </p>
            ) : (
              <div className="mt-2 space-y-1.5">
                {candidates.map((lib) => (
                  <button
                    key={lib.id}
                    type="button"
                    disabled={busy}
                    onClick={() => setTargetId(lib.id)}
                    className={`flex w-full items-center gap-3 rounded-xl border px-3.5 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-50 ${
                      targetId === lib.id
                        ? "border-[var(--accent-2)]/60 bg-[var(--accent-2)]/[0.1]"
                        : "border-white/[0.08] bg-white/[0.03] hover:bg-white/[0.06]"
                    }`}
                  >
                    <span
                      className={`size-3.5 shrink-0 rounded-full border-2 ${
                        targetId === lib.id
                          ? "border-[var(--accent-2)] bg-[var(--accent-2)]"
                          : "border-white/25"
                      }`}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block text-ui font-medium text-[var(--text)]">
                        {lib.name}
                        {lib.is_default && (
                          <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                            默认库
                          </span>
                        )}
                      </span>
                      <span className="block truncate font-mono text-caption text-white/45">
                        {lib.primary_root ?? "未配置根路径"}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}

            {/* —— 第二步：预览「将要发生什么」 —— */}
            {loadingPreview && (
              <p className="mt-4 flex items-center gap-2 text-sub text-[var(--text-muted)]">
                <span className="size-3.5 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
                正在计算转移计划…
              </p>
            )}
            {preview && <TransferPlanPreview preview={preview} sourceName={sourceLibraryName} />}
            {error && <p className="mt-3 text-sub leading-6 text-[#ff9f9f]">{error}</p>}
          </>
        )}
      </div>

      {/* —— 底栏：进行中只留进度，不给"取消"（搬到一半停不下来）—— */}
      <div className="flex items-center gap-3 border-t border-white/[0.07] px-6 py-4">
        {running && (
          <span className="flex min-w-0 flex-1 items-center gap-2 text-sub text-[var(--info)]">
            <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-[var(--info)]/30 border-t-[var(--info)]" />
            <span className="truncate">
              正在转移
              {status && status.total > 0 ? ` · ${status.processed}/${status.total}` : ""}
              ；跨盘转移需要完整复制文件，请勿关闭页面以外的操作
            </span>
          </span>
        )}
        <div className="ml-auto flex gap-3">
          {finished ? (
            <>
              <button
                type="button"
                onClick={onClose}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
              >
                留在本页
              </button>
              {status!.errors.length === 0 && status!.target_library_id != null && (
                <button
                  type="button"
                  onClick={() => onTransferred(status!.target_library_id!)}
                  className="btn-accent rounded-full px-5 py-2 text-ui font-semibold"
                >
                  去目标库查看
                </button>
              )}
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)] disabled:opacity-40"
              >
                取消
              </button>
              <button
                type="button"
                onClick={run}
                disabled={!canRun}
                className="btn-accent flex items-center gap-2 rounded-full px-5 py-2 text-ui font-semibold disabled:cursor-not-allowed disabled:opacity-40"
              >
                {starting && (
                  <span className="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                )}
                确认转移
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}

/** 转移计划预览：目标位置、体积、跨盘警示、阻断原因与跳过说明。 */
function TransferPlanPreview({
  preview,
  sourceName,
}: {
  preview: TransferPreview;
  sourceName: string | null;
}) {
  return (
    <div className="mt-4 space-y-3">
      {preview.blocked.length > 0 && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/[0.08] px-4 py-3 text-sub leading-6 text-[#ffb4b4]">
          {preview.blocked.map((b) => (
            <p key={b}>{b}</p>
          ))}
        </div>
      )}

      {preview.cross_device && preview.moves.length > 0 && (
        <div className="rounded-xl border border-[#ffd08a]/30 bg-[#ffd08a]/[0.08] px-4 py-3 text-sub leading-6 text-[#ffd08a]">
          目标库与当前位置<span className="font-semibold">不在同一块盘</span>
          ：文件需要完整复制（{formatBytes(preview.total_bytes)}），耗时取决于体积与盘速；
          复制会产生新文件，与下载器做种目录的硬链接关系将断开，两边各占一份空间。
        </div>
      )}

      {preview.moves.length > 0 && (
        <div className="rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
          <p className="text-caption text-[var(--text-faint)]">
            {preview.moves.length} 个路径 · {formatBytes(preview.total_bytes)}
            {preview.missing_count > 0 && ` · ${preview.missing_count} 个缺失记录随迁`}
          </p>
          <div className="scroll-thin mt-2 max-h-44 space-y-2 overflow-y-auto">
            {preview.moves.map((m) => (
              <div key={m.source_path} className="tnum font-mono text-caption leading-5">
                <p className="break-all text-white/45">
                  {sourceName ? `${sourceName} · ` : ""}
                  {m.source_path}
                </p>
                <p className="break-all text-white/80">
                  ↳ {preview.target_library_name} · {m.target_path}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {preview.skips.length > 0 && (
        <div className="rounded-xl border border-white/[0.08] bg-white/[0.02] p-3.5 text-caption leading-5 text-white/60">
          {preview.skips.map((s) => (
            <p key={s.file_path} className="break-all py-0.5">
              <span className="font-mono">{s.file_path}</span>：{s.reason}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/** 转移结论页：搬了哪些路径、随迁多少台账、订阅是否跟着改挂。 */
function TransferResult({ status }: { status: TransferStatus }) {
  const failed = status.errors.length > 0;
  return (
    <>
      <h3 className="text-title-sm font-semibold text-[var(--text)]">
        {failed ? "部分转移完成" : `「${status.title}」已转移到「${status.target_library_name}」`}
      </h3>
      <div className="scroll-thin mt-4 max-h-56 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
        {status.moved_paths.map((p) => (
          <p
            key={p}
            className="tnum break-all py-0.5 font-mono text-caption leading-5 text-white/70"
          >
            {p}
          </p>
        ))}
        {status.moved_paths.length === 0 && (
          <p className="text-sub text-[var(--text-muted)]">没有搬运任何磁盘路径</p>
        )}
      </div>
      {failed && (
        <div className="mt-3 space-y-1 text-sub leading-5 text-[#ff9f9f]">
          {status.errors.map((e) => (
            <p key={e}>{e}</p>
          ))}
        </div>
      )}
      <p className="mt-3 text-sub leading-6 text-[var(--text-muted)]">
        已随迁 {status.files_relocated} 条库存记录（{formatBytes(status.bytes_moved)}）
        {status.removed_dirs > 0 && `，清理空目录 ${status.removed_dirs} 个`}。
        {status.subscription_moved && "该片的订阅已一并改挂到目标库，后续剧集直接入新库。"}
      </p>
    </>
  );
}

/**
 * 删除确认弹窗：全站唯一真删磁盘的操作，把后果一次性讲透——
 * 列出将被清掉的目录、文件数与体积，勾选确认后才放开红色按钮；
 * 完成后展示实际删除的路径清单，让用户对"删了什么"有完整凭据。
 */
function DeleteDialog({
  open,
  detail,
  libraryId,
  onClose,
  onDeleted,
}: {
  open: boolean;
  detail: LibraryItemDetail;
  libraryId: number;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ItemDeleteResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setResult(null);
      setError(null);
    }
  }, [open]);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await deleteLibraryItem(libraryId, detail.media_item_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} label="删除影片" width="lg">
      <div className="p-6">
        {result ? (
          <>
            <h3 className="text-title-sm font-semibold text-[var(--text)]">
              {result.errors.length > 0 ? "部分删除完成" : "已从磁盘彻底删除"}
            </h3>
            <div className="scroll-thin mt-4 max-h-56 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              {result.removed_paths.map((p) => (
                <p key={p} className="tnum break-all py-0.5 font-mono text-caption leading-5 text-white/70">
                  {p}
                </p>
              ))}
              {result.removed_paths.length === 0 && (
                <p className="text-sub text-[var(--text-muted)]">没有删除任何磁盘路径</p>
              )}
            </div>
            {result.errors.length > 0 && (
              <div className="mt-3 space-y-1 text-sub leading-5 text-[#ff9f9f]">
                {result.errors.map((e) => (
                  <p key={e}>{e}</p>
                ))}
              </div>
            )}
            <p className="mt-3 text-sub text-[var(--text-muted)]">
              已清理 {result.rows_deleted} 条台账，释放 {formatBytes(result.freed_bytes)}。
            </p>
            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={onDeleted}
                className="btn-accent rounded-full px-5 py-2 text-ui font-semibold"
              >
                回到库存页
              </button>
            </div>
          </>
        ) : (
          <>
            <h3 className="flex items-center gap-2 text-title-sm font-semibold text-[var(--text)]">
              <TrashIcon className="size-4.5 text-[#ff9f9f]" />
              删除「{detail.title}」
            </h3>
            <p className="mt-3 text-ui leading-6 text-white/80">
              这不是「从列表移除」——将把下列目录从磁盘
              <span className="font-semibold text-[#ff9f9f]">彻底删除</span>
              ，包括视频文件、NFO、海报、字幕等全部刮削产物，共{" "}
              <span className="tnum font-semibold">{detail.file_count}</span> 个媒体文件、
              <span className="tnum font-semibold"> {formatBytes(detail.total_size_bytes)}</span>
              。此操作不可恢复。
            </p>
            <div className="scroll-thin mt-4 max-h-40 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              {(detail.entry_dirs.length > 0
                ? detail.entry_dirs
                : detail.files.map((f) => f.file_path)
              ).map((p) => (
                <p key={p} className="tnum flex items-start gap-1.5 break-all py-0.5 font-mono text-caption leading-5 text-white/70">
                  <FolderIcon className="mt-0.5 size-3.5 shrink-0 text-white/40" />
                  {p}
                </p>
              ))}
            </div>
            <label className="mt-4 flex cursor-pointer items-start gap-2.5 text-sub leading-6 text-white/80">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
                className="mt-1 size-4 accent-[#ff6b6b]"
              />
              我已明白：以上目录及其中全部文件将被永久删除，无法恢复。
            </label>
            {error && <p className="mt-3 text-sub text-[#ff9f9f]">{error}</p>}
            <div className="mt-5 flex justify-end gap-3">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={run}
                disabled={!confirmed || busy}
                className="flex items-center gap-2 rounded-full bg-[var(--danger-solid)] px-5 py-2 text-ui font-semibold text-white transition hover:bg-[var(--danger-solid-hover)] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy && (
                  <span className="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                )}
                {busy ? "正在删除…" : "彻底删除"}
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

/**
 * 单文件删除确认弹窗：条目删除的文件级姊妹（多版本洗掉一个 / 删某集重下）。
 * 与 DeleteDialog 同一套三段式：讲透后果 → 勾选确认 → 红色按钮；
 * 完成后展示实际删除的路径清单。两条必须讲清的规则：
 * 1. 最后一个文件会升级为整条目删除（后端行为，不留只剩 NFO/海报的空目录）；
 * 2. 该单元有订阅盯着时，删掉最后一份拷贝订阅会自动重新下载补齐。
 */
function DeleteFileDialog({
  file,
  detail,
  libraryId,
  onClose,
  onDeleted,
}: {
  file: LibraryItemFile | null;
  detail: LibraryItemDetail;
  libraryId: number;
  onClose: () => void;
  onDeleted: (deletedItem: boolean) => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ItemDeleteResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 条目在本库只剩这一行台账（含 missing 行）→ 后端会升级为整条目删除
  const isLast = detail.files.length === 1;

  useEffect(() => {
    if (file) {
      setConfirmed(false);
      setResult(null);
      setError(null);
    }
  }, [file]);

  if (!file) return null;

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await deleteLibraryFile(libraryId, detail.media_item_id, file.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={busy ? () => {} : onClose} label="删除文件" width="lg">
      <div className="p-6">
        {result ? (
          <>
            <h3 className="text-title-sm font-semibold text-[var(--text)]">
              {result.errors.length > 0 ? "删除失败" : "已从磁盘删除"}
            </h3>
            <div className="scroll-thin mt-4 max-h-56 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              {result.removed_paths.map((p) => (
                <p key={p} className="tnum break-all py-0.5 font-mono text-caption leading-5 text-white/70">
                  {p}
                </p>
              ))}
              {result.removed_paths.length === 0 && (
                <p className="text-sub text-[var(--text-muted)]">
                  没有删除任何磁盘路径{file.missing ? "（文件本就缺失，仅清除了台账记录）" : ""}
                </p>
              )}
            </div>
            {result.errors.length > 0 && (
              <div className="mt-3 space-y-1 text-sub leading-5 text-[#ff9f9f]">
                {result.errors.map((e) => (
                  <p key={e}>{e}</p>
                ))}
              </div>
            )}
            <p className="mt-3 text-sub text-[var(--text-muted)]">
              已清理 {result.rows_deleted} 条台账，释放 {formatBytes(result.freed_bytes)}。
            </p>
            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={() => onDeleted(isLast && result.errors.length === 0)}
                className="btn-accent rounded-full px-5 py-2 text-ui font-semibold"
              >
                完成
              </button>
            </div>
          </>
        ) : (
          <>
            <h3 className="flex items-center gap-2 text-title-sm font-semibold text-[var(--text)]">
              <TrashIcon className="size-4.5 text-[#ff9f9f]" />
              删除文件
            </h3>
            <p className="mt-3 text-ui leading-6 text-white/80">
              {file.missing ? (
                <>该文件在磁盘上已缺失，删除只会清掉这条台账记录。</>
              ) : (
                <>
                  将把下列文件从磁盘
                  <span className="font-semibold text-[#ff9f9f]">彻底删除</span>
                  ，同名的 NFO/字幕/图片附属文件一并清除。此操作不可恢复。
                </>
              )}
            </p>
            <div className="mt-4 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              <p className="text-ui text-[var(--text)]">
                {detail.kind !== "movie" &&
                  (file.episode_number > 0 || file.season_number > 0) && (
                    <span className="tnum mr-2 text-sub font-semibold text-[var(--accent-2)]">
                      S{String(file.season_number).padStart(2, "0")}E
                      {String(file.episode_number).padStart(2, "0")}
                    </span>
                  )}
                {file.file_name}
                <span className="tnum ml-2 text-sub text-[var(--text-muted)]">
                  {formatBytes(file.size_bytes)}
                </span>
              </p>
              <p className="tnum mt-1 break-all font-mono text-caption leading-5 text-white/50">
                {file.file_path}
              </p>
            </div>
            {isLast && (
              <p className="mt-3 rounded-xl border border-[var(--warn)]/30 bg-[var(--warn)]/[0.06] px-3.5 py-2.5 text-sub leading-6 text-[var(--warn)]">
                这是「{detail.title}」在本库的最后一个文件——删除将升级为整条目删除，
                整个刮削目录（含 NFO/海报）一并清除，条目将从库存消失。
              </p>
            )}
            <p className="mt-3 text-caption leading-5 text-[var(--text-faint)]">
              若该作品有订阅且删除后此单元不再有其他拷贝，订阅会将其视为缺失并自动重新下载。
            </p>
            <label className="mt-4 flex cursor-pointer items-start gap-2.5 text-sub leading-6 text-white/80">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
                className="mt-1 size-4 accent-[#ff6b6b]"
              />
              我已明白：{isLast ? "整个条目目录及其中全部文件" : "该文件及其同名附属文件"}
              将被永久删除，无法恢复。
            </label>
            {error && <p className="mt-3 text-sub text-[#ff9f9f]">{error}</p>}
            <div className="mt-5 flex justify-end gap-3">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={run}
                disabled={!confirmed || busy}
                className="flex items-center gap-2 rounded-full bg-[var(--danger-solid)] px-5 py-2 text-ui font-semibold text-white transition hover:bg-[var(--danger-solid-hover)] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy && (
                  <span className="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                )}
                {busy ? "正在删除…" : "彻底删除"}
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}


/**
 * 刮削归属库弹层（docs/design/scrape-customization.md §14.5）。
 *
 * 元数据与图片的产物挂在**全局条目**上——一部片一份档案、一张海报——所以
 * 语言与选图这类设置没法像命名模板那样"每个库各来一套"。归属库就是"这条
 * 条目按谁的口味刮"的唯一答案；同一条目的文件散在两个库时，这里显示的就是
 * 谁赢了，也是用户唯一能翻案的地方。
 *
 * 「跟随自动判定」= 清空归属，由系统按"在位文件所属库 → 订阅目标库"重新推断。
 * 改完不自动重刮：重刮要重下全套图，是用户该自己按的按钮（菜单里的刷新元数据）。
 */
function ScrapeLibraryDialog({
  open,
  libraryId,
  mediaItemId,
  kind,
  current,
  onClose,
  onChanged,
}: {
  open: boolean;
  libraryId: number;
  mediaItemId: number;
  kind: MediaType;
  current: number | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    // 只列同类型的库：电影库的刮削口味套到剧集上没有意义，后端也会拒
    listLibraries(kind)
      .then(setLibraries)
      .catch(() => setLibraries([]));
  }, [open, kind]);

  const apply = async (target: number | null) => {
    setBusy(true);
    try {
      const result = await setItemScrapeLibrary(libraryId, mediaItemId, target);
      toast.success(
        result.scrape_library_id === null
          ? "已恢复自动判定；刷新元数据后按推断出的库重刮"
          : "刮削归属已更新；刷新元数据后按该库的设置重刮",
      );
      onChanged();
      onClose();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "修改失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} label="刮削归属" width="lg">
      <div className="p-6">
        <h3 className="text-title-sm font-semibold text-[var(--text)]">刮削归属</h3>
        <p className="mt-2 text-sub leading-relaxed text-[var(--text-muted)]">
          这部影片的语言、分级与选图按下面这个媒体库的刮削设置来。一部片只有一份档案与
          一张海报，所以文件即使散在多个库，也只能有一个库说了算。
        </p>
        <div className="mt-4 space-y-2">
          {libraries.map((library) => (
            <button
              key={library.id}
              type="button"
              disabled={busy}
              onClick={() => apply(library.id)}
              className={`flex w-full items-center justify-between gap-3 rounded-xl border px-3.5 py-2.5 text-left transition-colors disabled:opacity-50 ${
                current === library.id
                  ? "border-[var(--accent-2)] bg-[var(--accent-soft)]"
                  : "border-white/[0.08] bg-white/[0.04] hover:bg-white/[0.07]"
              }`}
            >
              <span className="min-w-0">
                <span className="block truncate text-ui font-medium">{library.name}</span>
                <span className="mt-0.5 block truncate text-caption text-[var(--text-faint)]">
                  {library.root_paths[0] ?? "未配置根路径"}
                </span>
              </span>
              {current === library.id && (
                <span className="shrink-0 text-caption text-[var(--accent)]">当前</span>
              )}
            </button>
          ))}
          <button
            type="button"
            disabled={busy}
            onClick={() => apply(null)}
            className="flex w-full items-center justify-between gap-3 rounded-xl border border-dashed border-white/[0.15] px-3.5 py-2.5 text-left text-[var(--text-muted)] transition-colors hover:text-[var(--text)] disabled:opacity-50"
          >
            <span className="min-w-0">
              <span className="block text-ui font-medium">跟随自动判定</span>
              <span className="mt-0.5 block text-caption text-[var(--text-faint)]">
                按在位文件所属的库推断；都没有则跟随全局刮削设置
              </span>
            </span>
          </button>
        </div>
      </div>
    </Modal>
  );
}
