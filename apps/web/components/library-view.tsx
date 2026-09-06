"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { ContentEmptyState } from "@/components/content-empty-state";
import { HScroller } from "@/components/h-scroller";
import { LIBRARY_KIND_META } from "@/components/library-kind-meta";
import {
  FilmIcon,
  GearIcon,
  PlusIcon,
} from "@/components/icons";
import { MediaRow } from "@/components/media-row";
import type { PosterCardAction } from "@/components/poster-card";
import { RecentWatchRow } from "@/components/recent-watch-row";
import {
  type LibraryItem,
  type MediaLibrary,
  listLibraries,
  listLibraryItems,
  SCAN_PHASE_LABELS,
} from "@/lib/api/libraries";
import { listRecentWatch, type RecentWatchItem } from "@/lib/api/playback";
import type { Subscription } from "@/lib/api/subscriptions";
import { publicEnv } from "@/lib/env";
import { formatBytes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import { libraryInventoryAction } from "@/lib/library-inventory-summary";
import type { MediaItem } from "@/lib/media-types";
import { usePermissions } from "@/lib/permissions";
import { buildRecentAdditionOverlay } from "@/lib/recent-addition";
import { formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/** 每个库「最近添加」行的格数（也是本页向服务端要的条目数上限）。 */
const RECENT_COUNT = 20;

/**
 * 库存条目的悬浮操作与本卡 hover 的完整度文案同源：季或集有一项未齐就
 * 「补齐缺集」；当前已知季集全部在库则「自动续订」，等待未来出现的新季。
 * 已在“我的订阅”中的条目由 PosterCardVisual 统一隐藏操作，不再显示
 * 没有决策价值的“已订阅”按钮。
 */
export function libraryCardAction(item: LibraryItem): PosterCardAction {
  return libraryInventoryAction(item.kind, item.inventory_summary);
}

/**
 * 订阅的实际归属库：显式指定优先，否则该类型的默认库。
 * 与后端 resolve_for_subscription 同一语义，库页与单库页共用。
 */
export function effectiveLibraryId(
  sub: Subscription,
  libraries: MediaLibrary[],
): number | null {
  if (sub.library_id != null) return sub.library_id;
  return libraries.find((l) => l.kind === sub.media.kind && l.is_default)?.id ?? null;
}

/** 媒体库首页摘要：只聚合接口随库返回的预计算快照，不触发额外请求。 */
export function libraryStatsSummary(libraries: MediaLibrary[] | null): string {
  if (libraries === null) return "正在汇总媒体库统计…";
  if (libraries.length === 0) return "还没有媒体库，创建后会在这里显示库存统计";
  const movieCount = libraries
    .filter((library) => library.kind === "movie")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const tvCount = libraries
    .filter((library) => library.kind === "tv")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const videoCount = libraries
    .filter((library) => library.kind === "video")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const totalSizeBytes = libraries.reduce(
    (total, library) => total + library.stats.total_size_bytes,
    0,
  );
  const videoPart = videoCount > 0 ? ` · ${videoCount} 个其他视频` : "";
  return `${libraries.length} 个媒体库 · ${movieCount} 部电影 · ${tvCount} 部剧集${videoPart} · 共占用 ${formatBytes(totalSizeBytes)} 存储空间`;
}

/**
 * 媒体库页（/library）：全部库的 Emby 风格卡片横排——**只做浏览入口**。
 *
 * 每张卡是一个库：封面用库内作品的海报做「货架」展示（最多 4 张站立海报
 * 带底部倒影，纯前端 CSS 合成、零后端开销），叠库名/类型/统计；
 * 点击进入单库海报墙（/library/[id]）。库的增删改/设默认/扫描/排序全部在
 * 管理页（/library/manage）完成，见 docs/design/library-manage.md——卡片上只留
 * 预告"马上会看到新内容"的信息：扫描进度环与「入库中」徽标。
 *
 * 数据源是 library_file 台账的**真实库存**（L3 起）：入库管线与存量扫描
 * 落账的文件聚合，不再用订阅占位。
 */
export function LibraryView() {
  const { canManageLibraries } = usePermissions();
  const scrollRef = useScrollRestoration("library");
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [itemsByLibrary, setItemsByLibrary] = useState<Map<number, LibraryItem[]>>(
    new Map(),
  );
  const [recentWatch, setRecentWatch] = useState<RecentWatchItem[] | null>(null);
  const [failed, setFailed] = useState(false);

  // 轮询乱序守卫：扫描期间后端响应时间抖动大，上一轮的慢响应可能晚于
  // 下一轮到达，不作废就会用旧快照覆盖新状态（进度回跳、卡片状态闪烁）
  const reloadSeq = useRef(0);
  // 上一轮已拉过条目时的库列表快照：库状态（扫描/整理/入账数/刷新进度）
  // 没有任何变化，说明库存也不会变，封面拼图不必再逐库全量拉一遍条目
  // ——否则空闲时每 30 秒也要打出 1 + N 个请求
  const lastLibsSnapshot = useRef<string | null>(null);
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    Promise.all([
      listLibraries(),
      // 最近观看失败不拖垮媒体库首页；保留旧数据，下一轮轮询自动重试。
      listRecentWatch(RECENT_COUNT).catch(() => null),
    ])
      .then(async ([libs, latestWatch]) => {
        if (seq !== reloadSeq.current) return;
        setFailed(false);
        if (latestWatch !== null) setRecentWatch(latestWatch);
        else setRecentWatch((previous) => previous ?? []);
        const snapshot = JSON.stringify(libs);
        // 内容没变就复用旧引用，跳过整页卡片的无谓重渲染
        setLibraries((prev) => (prev && JSON.stringify(prev) === snapshot ? prev : libs));
        if (snapshot === lastLibsSnapshot.current) return;
        // 本页每个库只用到最近入账的 20 部（「最近添加」行 + 卡片封面拼图取前 4），
        // 排序与截断都交给服务端——早先在这里拉整库再本地切片，一个几千部的库
        // 光这一个请求就是几百 KB、后端还要把全库台账聚合一遍
        const entries = await Promise.all(
          libs.map(
            async (lib) =>
              [
                lib.id,
                // 不在自己浏览范围内的库（超管把自己摘掉了）只有管理权：
                // 卡片带锁、不拉条目——拉了也是 404
                lib.viewer_access
                  ? await listLibraryItems(lib.id, { sort: "added_at", limit: RECENT_COUNT }).catch(
                      () => [],
                    )
                  : [],
              ] as const,
          ),
        );
        if (seq !== reloadSeq.current) return;
        lastLibsSnapshot.current = snapshot;
        setItemsByLibrary(new Map(entries));
      })
      // 瞬时失败不清已有数据：failed 只决定提示条，卡片继续用上一份快照，
      // 下一轮轮询成功即自动恢复（整页错误屏只留给一次都没加载成功的情况）
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // 有库在扫描/整理时轮询刷新，任务完成即看到最新库存与文件名
  const busyAny = (libraries ?? []).some((l) => l.scanning || l.organizing);
  // 元数据刷新单独一档：它以分钟计，而本页每轮 reload 还要把每个库的条目
  // 列表拉一遍，用扫描那档 3 秒会打出上百次无谓请求；5 秒足够让进度环和
  // "到哪部了"看着在动（完整阶段列表在单库页的面板里）
  const refreshingAny = (libraries ?? []).some((l) => l.metadata_refresh?.refreshing);
  // 有文件写入中暂缓入账（拷贝/下载进行时 watchdog 已发现，等补扫落定）：
  // 中速轮询让「入库中」徽标与随后的库存变化自动呈现；完全空闲时低频兜底
  // ——后台自发的扫描（实时监控/定时对账）页面开着不动也能感知到
  const importingAny = (libraries ?? []).some(
    (l) => !l.scanning && !l.organizing && (l.last_scan?.deferred ?? 0) > 0,
  );
  // busy 刚结束后保持快轮询一小段再降速：监控去抖触发的连环扫描之间隔着
  // 几秒空档，一采样到空档就降去 30 秒档的话，下一轮扫描的开始要很久
  // 才被发现，卡片状态看起来就是"时隐时现"
  const [recentlyBusy, setRecentlyBusy] = useState(false);
  useEffect(() => {
    if (busyAny) {
      setRecentlyBusy(true);
      return;
    }
    if (!recentlyBusy) return;
    const timer = setTimeout(() => setRecentlyBusy(false), 12_000);
    return () => clearTimeout(timer);
  }, [busyAny, recentlyBusy]);
  // 页面隐藏时暂停轮询、恢复可见立即补一次（useVisiblePolling）
  useVisiblePolling(
    reload,
    busyAny || recentlyBusy ? 3000 : refreshingAny ? 5000 : importingAny ? 10_000 : 30_000,
  );

  // 每个非空库一行「最近添加」：服务端已按最近入账倒序给到前 20，复用发现页的
  // 横滚海报行。这里只呈现入库上下文，订阅/补齐操作留在单库页，避免 hover
  // 被“已订阅”等与最近添加无关的状态占据。
  // 卡片区只放当前身份能浏览的库：超管把自己摘出浏览范围的库（「仅管理」）
  // 对首页来说就是不存在，它只在管理页出现（带锁标）
  const visibleLibraries = useMemo(
    () => (libraries ?? []).filter((library) => library.viewer_access),
    [libraries],
  );

  const recentRows = useMemo(
    () =>
      (libraries ?? [])
        // 勾了「从首页排除」的库不上首页（库卡片仍在，进库内看照常）；
        // 不在自己浏览范围内的库更不上——内容对当前身份就是不存在
        .filter((library) => !library.exclude_from_home && library.viewer_access)
        .map((library) => {
          const recent = itemsByLibrary.get(library.id) ?? [];
          // 已在库的条目点击进**媒体库条目详情**（本地刮削信息 + 片源规格 +
          // 条目操作），与单库页库存格同一目标，不再跳发现页的 TMDB 详情
          const hrefs = new Map(
            recent.map((it) => [
              libraryItemKey(it),
              `/library/${library.id}/item/${it.media_item_id}` as Route,
            ]),
          );
          return {
            library,
            items: recent.map(libraryItemToMediaItem),
            hrefOf: (m: MediaItem) => hrefs.get(m.id),
          };
        })
        .filter((row) => row.items.length > 0),
    [libraries, itemsByLibrary],
  );

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      {/* 页头：标题 + 统计，右侧是页面级操作「管理媒体库」（SaaS 惯例：页面
          动作放标题行右端；分区标题行只留分区自己的东西） */}
      <div className="flex items-start justify-between gap-4 px-6 pt-7 max-md:px-4 max-md:pt-4">
        <div className="min-w-0">
          <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
            媒体库
          </h2>
          <p className="text-on-image mt-1.5 text-ui text-[var(--text-muted)] max-md:mt-1 max-md:line-clamp-2 max-md:text-sub">
            {failed && libraries === null
              ? "暂时无法获取媒体库统计，正在自动重试"
              : libraryStatsSummary(libraries === null ? null : visibleLibraries)}
          </p>
        </div>
        {canManageLibraries && (
          <Link
            href={"/library/manage" as Route}
            className="btn-glass mt-1 h-8 shrink-0 gap-1.5 px-3 text-sub font-medium max-md:mt-0"
          >
            <GearIcon className="size-4" />
            管理媒体库
          </Link>
        )}
      </div>

      {libraries === null && !failed && (
        <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载媒体库…
        </div>
      )}

      {/* 只有一次都没加载成功过才整页报错；已有数据在手时，瞬时失败只挂
          提示条（stale-while-error），卡片照常展示上一份快照 */}
      {failed && libraries === null && (
        <div className="mt-16 flex flex-col items-center gap-3 text-center">
          <p className="text-ui text-[var(--text-muted)]">媒体库加载失败</p>
          <button
            type="button"
            onClick={reload}
            className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            重试
          </button>
        </div>
      )}

      {failed && libraries !== null && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {/* 当前账号跨可见库聚合的播放状态；空列表时组件整段隐藏。 */}
      {(!failed || libraries !== null) && <RecentWatchRow items={recentWatch} />}

      {libraries !== null && libraries.length === 0 && (
        <ContentEmptyState
          variant="library"
          title={canManageLibraries ? "为收藏准备一个家" : "还没有可浏览的媒体库"}
          description={
            canManageLibraries
              ? "创建电影库或剧集库，选好根目录后，订阅完成的内容会自动整理到这里。"
              : "当前账号暂时没有可浏览的媒体库，请联系管理员分配媒体库权限。"
          }
          action={
            canManageLibraries ? (
              <Link
                href={"/library/manage?create=1" as Route}
                className="btn-accent flex items-center gap-1 rounded-full py-2 pl-3 pr-4 text-ui font-semibold"
              >
                <PlusIcon className="size-4" />
                创建第一个媒体库
              </Link>
            ) : undefined
          }
        />
      )}

      {/* 库卡片横排：库多了不换行堆高，改为一行横滚（与下方「最近添加」
          同一交互），首屏始终保住「最近观看 → 我的媒体库 → 最近添加」的层次。 */}
      {libraries !== null && visibleLibraries.length > 0 && (
        <section className="mt-8 max-md:mt-6" aria-labelledby="my-libraries-title">
          <div className="flex items-center justify-between gap-4 px-6 max-md:px-4">
            <h3
              id="my-libraries-title"
              className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]"
            >
              我的媒体库
            </h3>
          </div>
          <HScroller className="mt-3 gap-5 px-6 pb-1 pt-1 max-md:gap-3.5 max-md:px-4">
            {visibleLibraries.map((library) => (
              <div
                key={library.id}
                data-library-card={library.id}
                className="w-[268px] shrink-0 rounded-2xl max-md:w-[230px]"
              >
                <LibraryCard library={library} items={itemsByLibrary.get(library.id) ?? []} />
              </div>
            ))}
          </HScroller>
        </section>
      )}

      {/* —— 最近添加：Emby 首页式分区，每个非空库一行横滚海报 —— */}
      {recentRows.length > 0 && (
        <div className="mt-10 space-y-8">
          {recentRows.map(({ library, items, hrefOf }) => (
            <MediaRow
              key={library.id}
              row={{
                id: `library-recent-${library.id}`,
                title: `最近添加的${library.name}`,
                items,
              }}
              moreHref={`/library/${library.id}` as Route}
              moreLabel="查看全部"
              cardAction="none"
              cardHref={hrefOf}
              cardRevealInfoOnTouch
            />
          ))}
        </div>
      )}

    </div>
  );
}

/**
 * 库存条目 → 发现页海报卡的数据形态。点击走 /media/{type}/{tmdb_id} 详情
 * （与单库页库存格同一目标）。卡片底部只留片名与年份；本批季集范围和入库
 * 时间进入 hover，不能拿累计库存季集数冒充新增内容。海报不打清晰度徽章。
 */
/** 海报卡的 id：TMDB 条目用 tmdb_id（订阅状态按它对齐），本地条目没有外部 id，
 *  用带前缀的条目 id 占位——只用来当 Map 键与 React key，不会被当成 TMDB id 请求。 */
function libraryItemKey(item: LibraryItem): string {
  return item.tmdb_id != null ? String(item.tmdb_id) : `local:${item.media_item_id}`;
}

function libraryItemToMediaItem(item: LibraryItem): MediaItem {
  const overlayDetails = buildRecentAdditionOverlay(
    item.kind,
    item.recent_addition,
    item.added_at ? `${formatRelativeTime(item.added_at)}入库` : null,
  );
  return {
    id: libraryItemKey(item),
    source: "tmdb",
    // 其他库条目没有发现页类型；卡片只当本地内容展示，不给订阅入口
    type: item.kind === "video" ? "movie" : item.kind,
    aspect: item.primary_aspect,
    title: item.title,
    originalTitle: "",
    year: item.year ?? 0,
    rating: 0,
    genres: [],
    extent: "",
    badges: [],
    overview: "",
    overlayDetails,
    // 海报可能是本地刮削资产的相对路径（/images/assets/...），也可能是
    // TMDB 图床绝对地址——统一经 imageUrl 解析（补 API base / 走缓存代理）。
    // 取 poster-card 派生图而非原图：格子实测渲染 150~170 CSS px，328px 的
    // 预设覆盖 2x 屏绰绰有余，而原图是 500px 宽的刮削资产——一屏 60 格直出
    // 原图要 4.9 MB，取派生图只要 1.7 MB（实测单张 82KB → 29KB）
    posterUrl: imageUrl(item.poster_url, "poster-card"),
  };
}

/* —— 库卡片：海报货架封面 + 库名/徽标/计数，Emby「我的媒体」磁贴风 —— */

function LibraryCard({ library, items }: { library: MediaLibrary; items: LibraryItem[] }) {
  const meta = LIBRARY_KIND_META[library.kind];
  // 封面海报取最近入库的 4 部（items 已是服务端按最近入账排好的那批）
  const posters = items
    .map((s) => s.poster_url)
    .filter((u): u is string => Boolean(u))
    .slice(0, 4);
  // 扫描/整理/元数据刷新进行中：封面归进度环，其余状态徽标一律让位。
  // 三种长任务在卡片上同一套呈现——用户不该因为"哪种任务"而看不到进度
  const refreshingMeta = Boolean(library.metadata_refresh?.refreshing);
  const busy = library.scanning || library.organizing || refreshingMeta;
  // 写入中暂缓入账的文件数（watchdog 已发现、等拷贝/下载落定后自动补扫入库）
  const importing = busy ? 0 : (library.last_scan?.deferred ?? 0);

  return (
    <div className="group/lib relative">
      <Link
        href={`/library/${library.id}` as Route}
        scroll={false}
        aria-label={`打开「${library.name}」`}
        className="block overflow-hidden rounded-2xl ring-1 ring-white/10 outline-none transition duration-300 hover:ring-white/35 focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
      >
        <div className="relative aspect-[21/10] bg-[#0a0c12]">
          <LibraryCover libraryId={library.id} posters={posters} Icon={meta.Icon} />
          {/* 状态徽标叠在封面左下的倒影暗区：那块本就没有信息、又足够暗
              压得住字；标题行因此永远只有库名，长库名不会被徽标挤没。
              扫描/整理时封面归进度环，徽标让位（否则隔着蒙版透出来像脏渲染） */}
          {!busy && importing > 0 && (
            <div className="absolute inset-x-2.5 bottom-2 flex flex-wrap items-center gap-1.5">
              <span className="flex items-center gap-1.5 rounded-full border border-[var(--info)]/35 bg-black/55 px-2 py-0.5 text-micro font-semibold text-[var(--info)] backdrop-blur-md">
                <span className="size-1.5 animate-pulse rounded-full bg-[var(--info)]" />
                {importing} 个新文件入库中
              </span>
            </div>
          )}
          {busy && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-1 bg-black/55 backdrop-blur-[2px]">
              <ScanProgressRing
                progress={
                  library.scanning
                    ? library.scan_progress
                    : library.organizing
                      ? library.organize_progress
                      : library.metadata_refresh
                }
              />
              {/* 进度环只有百分比，说不清在干什么，补一行状态词。扫描内部
                  分阶段（盘点/入账/补图），阶段变了这里必须跟着变——否则
                  文件扫完后还要下几分钟图片，环停在 100% 配一句"扫描中"，
                  看起来就是卡死了 */}
              <span className="text-caption font-semibold text-white/85">
                {library.scanning
                  ? (SCAN_PHASE_LABELS[library.scan_progress?.phase ?? "ingesting"] ?? "扫描中")
                  : library.organizing
                    ? "整理中"
                    : "刷新元数据"}
              </span>
              {/* 刷新是全量重刷、以分钟计，多给一行"到哪部了"（并发若干路
                  时取第一部即可，完整列表在单库页的面板里） */}
              {refreshingMeta && library.metadata_refresh?.active?.[0] && (
                <span className="max-w-[86%] truncate text-micro text-white/60">
                  {library.metadata_refresh.active[0].title} ·{" "}
                  {library.metadata_refresh.active[0].phase}
                </span>
              )}
            </div>
          )}
        </div>
      </Link>

      {/* 库名：Emby 式放在封面下方居中，只与「默认」共处一行 */}
      <div className="mt-2.5 flex items-center justify-center gap-2 px-2">
        <h3 className="truncate text-body-lg font-semibold text-white">{library.name}</h3>
        {library.is_default && (
          <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.1] px-2 py-0.5 text-micro font-semibold text-white/80">
            默认
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * 封面「氛围光货架」：首张海报重模糊后铺满做氛围光晕（每个库有自己的
 * 色调），最多 4 张海报立体站排，底部倒影直接落在氛围暗底上表达
 * 「反光地面」；0 张=类型图标底纹。卡片 21/10 比例，海报占约 2/3。
 */
/** 扫描进度环：有分母画百分比，刚起步（进度未知）转圈占位。 */
/** 扫描/整理/元数据刷新共用的进度环（三者的进度都是 已处理/总数）。 */
function ScanProgressRing({ progress }: { progress: { processed: number; total: number } | null }) {
  const pct =
    progress && progress.total > 0
      ? Math.min(100, Math.round((progress.processed / progress.total) * 100))
      : null;
  const R = 26;
  const C = 2 * Math.PI * R;
  return (
    <div className="relative size-[72px]">
      <svg
        viewBox="0 0 64 64"
        className={`size-full -rotate-90 ${pct === null ? "animate-spin" : ""}`}
      >
        <circle cx="32" cy="32" r={R} fill="none" stroke="rgba(255,255,255,0.2)" strokeWidth="5" />
        <circle
          cx="32"
          cy="32"
          r={R}
          fill="none"
          stroke="white"
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={pct === null ? C * 0.75 : C * (1 - pct / 100)}
          className="transition-[stroke-dashoffset] duration-500 ease-out"
        />
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-ui font-semibold text-white">
        {pct === null ? "…" : `${pct}%`}
      </span>
    </div>
  );
}

function LibraryCover({
  libraryId,
  posters,
  Icon,
}: {
  libraryId: number;
  posters: string[];
  Icon: typeof FilmIcon;
}) {
  // 服务端渲染的「氛围光货架」拼贴（与 Jellyfin 兼容层给播放器的是同一张图）：
  // 一次 <img> 请求替代 9+ 张图的客户端合成，ETag 协商缓存，渲染显著更快。
  // 拼贴尚未生成/加载失败时回退到原客户端 CSS 货架（素材同源，观感一致）。
  const [collageFailed, setCollageFailed] = useState(false);
  if (posters.length === 0) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-[#1c2230] to-[#10131c]">
        <Icon className="size-12 text-white/[0.13]" />
      </div>
    );
  }
  if (!collageFailed) {
    return (
      <div className="absolute inset-0 overflow-hidden">
        <img
          src={`${publicEnv.apiBaseUrl}/libraries/${libraryId}/cover`}
          alt=""
          loading="lazy"
          className="absolute inset-0 size-full object-cover transition duration-300 group-hover/lib:scale-[1.02]"
          onError={() => setCollageFailed(true)}
        />
        {/* 悬停扫光沿用：一道斜向柔光掠过底部倒影区 */}
        <div className="pointer-events-none absolute -left-[45%] bottom-0 h-[25%] w-[45%] -skew-x-12 bg-gradient-to-r from-transparent via-white/[0.14] to-transparent transition-transform duration-700 ease-out group-hover/lib:translate-x-[350%]" />
      </div>
    );
  }
  return (
    <div className="absolute inset-0 overflow-hidden">
      {/* 氛围光：首图放大重模糊 + 提饱和，再整体压暗保证前景对比度 */}
      <img
        src={imageUrl(posters[0])}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        className="absolute inset-0 size-full scale-150 object-cover opacity-70 blur-3xl saturate-150"
      />
      <div className="absolute inset-0 bg-[#080a10]/50" />
      {/* 灯箱底光：首图模糊后以 screen 混合从底边向上发光，颜色天然
          取自海报主色；再叠一个中性地面光斑，像射灯打在舞台地面上 */}
      <img
        src={imageUrl(posters[0])}
        alt=""
        aria-hidden
        loading="lazy"
        referrerPolicy="no-referrer"
        className="absolute inset-x-0 bottom-0 h-1/2 w-full object-cover opacity-55 blur-3xl saturate-150 mix-blend-screen [mask-image:linear-gradient(to_top,black,transparent)]"
      />
      <div className="absolute inset-x-[8%] bottom-0 h-[28%] [background:radial-gradient(60%_100%_at_50%_100%,rgba(255,255,255,0.09),transparent_70%)]" />
      {/* 海报排：立在玻璃搁板上，悬停整排轻微上浮 */}
      <div className="absolute inset-x-0 top-[4.5%] flex justify-center gap-[2%] px-[2%]">
        {posters.map((url, i) => (
          <div
            key={i}
            className="w-[22.5%] shrink-0 transition duration-300 group-hover/lib:-translate-y-1"
          >
            <img
              src={imageUrl(url)}
              alt=""
              loading="lazy"
              referrerPolicy="no-referrer"
              className="aspect-[2/3] w-full rounded-[4px] object-cover shadow-[0_6px_18px_rgba(0,0,0,0.5)] ring-1 ring-white/20"
            />
            {/* 倒影：翻转副本贴着底边，向下快速渐隐。注意 mask 在元素本地
                坐标系生效、会跟着 scaleY(-1) 一起翻转，所以这里写 to top，
                翻转后在屏幕上才是「贴近海报处最实、向下淡出」 */}
            <img
              src={imageUrl(url)}
              alt=""
              aria-hidden
              loading="lazy"
              referrerPolicy="no-referrer"
              className="mt-[2px] aspect-[2/3] w-full -scale-y-100 rounded-[4px] object-cover opacity-55 blur-[1px] [mask-image:linear-gradient(to_top,rgba(0,0,0,0.7),transparent_26%)]"
            />
          </div>
        ))}
      </div>
      {/* 悬停扫光：一道斜向柔光从左扫到右掠过倒影区（transform 过渡
          实现单次扫过，移出卡片后自动滑回原位待命） */}
      <div className="pointer-events-none absolute -left-[45%] bottom-0 h-[25%] w-[45%] -skew-x-12 bg-gradient-to-r from-transparent via-white/[0.14] to-transparent transition-transform duration-700 ease-out group-hover/lib:translate-x-[350%]" />
    </div>
  );
}
