"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";
import Link from "next/link";

import { useConfirm } from "@/components/feedback";
import { refreshLibraryConfirm, scanLibraryConfirm } from "@/lib/library-confirm";
import { MoreIcon, XIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { usePageTitle } from "@/lib/use-page-title";
import {
  LIBRARY_KIND_META,
  LibraryFormDialog,
  effectiveLibraryId,
  libraryCardAction,
} from "@/components/library-view";
import { LibraryOrganizeDialog } from "@/components/library-organize-dialog";
import { PosterCardVisual, type PosterVisualItem } from "@/components/poster-card";
import {
  type LibraryItem,
  type MediaLibrary,
  type MissingItem,
  type ReviewGroup,
  type MetadataRefreshProgress,
  type UnidentifiedGroup,
  assignLibraryFilesToTitle,
  clearMissingLibraryRecords,
  getMetadataRefreshProgress,
  ignoreAllUnidentifiedLibraryFiles,
  ignoreUnidentifiedLibraryFile,
  listIgnoredLibraryFiles,
  listLibraryIdentityReviewCases,
  listLibraries,
  listLibraryItemIds,
  listLibraryItemIndex,
  listLibraryItems,
  type LibraryIndexEntry,
  type LibraryItemSort,
  listMissingLibraryFiles,
  listUnidentifiedLibraryFiles,
  redownloadMissing,
  resolveLibraryIdentityReview,
  restoreIgnoredLibraryFiles,
  SCAN_PHASE_HINTS,
  SCAN_PHASE_LABELS,
  type ScanPhase,
  type ScanProgress,
  startLibraryMetadataRefresh,
  startLibraryScan,
  stopLibraryMetadataRefresh,
  stopLibraryScan,
} from "@/lib/api/libraries";
import {
  ClaimConfirmPanel,
  ClaimSearchPanel,
  type ClaimSeed,
  searchSeedFromLabel,
} from "@/components/claim-panels";
import { listSubscriptions, type Subscription } from "@/lib/api/subscriptions";
import { formatBytes } from "@/lib/format";
import { formatLibraryInventorySummary } from "@/lib/library-inventory-summary";
import { activeWallInitialAtViewport, wallInitialAtOffset } from "@/lib/library-wall-index";
import { formatRelativeTime } from "@/lib/time";
import { cachedImageUrl, imageUrl } from "@/lib/image-proxy";
import { keepIfEqual, reconcileList } from "@/lib/poll-reconcile";
import { usePermissions } from "@/lib/permissions";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { useJobs } from "@/lib/jobs";
import {
  getLibraryDetailSnapshot,
  setLibraryDetailSnapshot,
  type LibraryDetailSnapshot,
} from "@/lib/library-detail-snapshot";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";
import {
  subscriptionProgressNote,
  subscriptionStatusMeta,
} from "@/lib/subscription-ui";

/**
 * 长任务状态胶囊的文案：阶段名 + 分子分母 + 一句"在等什么"。
 *
 * 分子分母**只在分母已知时**显示：分母为 0 是"还不知道有多少"（如盘点
 * 阶段），硬写成 0/0 比不写更误导。阶段换了分母就跟着换，因此不会出现
 * 进度走满后界面还停在同一句话上干等的情况。
 */
function busyText(progress: ScanProgress | null): string {
  if (!progress) return "正在处理…"; // 理论上取不到（接口保证同生同灭），保底不留白
  const counter = progress.total > 0 ? ` ${progress.processed}/${progress.total}` : "";
  return `${SCAN_PHASE_LABELS[progress.phase]}${counter} · ${SCAN_PHASE_HINTS[progress.phase]}`;
}

/**
 * 单库页（/library/[id]）：库头部 + **真实库存**海报墙（Emby 进库后的浏览视图）。
 *
 * 三个分区：
 * 1. 追踪中：入库目标是本库、但还没有文件落地的订阅——自动化正在进行的部分，
 *    时效性最强，置顶展示（格子样式仍弱化，点进订阅详情）；
 * 2. 库存（library_file 台账聚合）：已在磁盘上的作品，格下标注集数/规格/大小；
 * 3. 待识别：扫描认不出身份的文件，按条目目录成组，点候选或填 TMDB ID 整组认领。
 */
/** 海报墙每次向服务端要的格数（首屏一批，滚到底再追加一批）。 */
const WALL_PAGE_SIZE = 60;
/** 后端单次分页的硬上限；轮询已加载窗口时按此上限分块请求。 */
const WALL_API_PAGE_SIZE = 200;

/**
 * 列表拉取失败折成 ``null``（而不是空数组）。
 *
 * 「拉不到」和「没有」是两回事，待办清单尤其不能混：折成空数组的话，一次
 * 500 / 超时 / 权限不足会让胶囊与计数静默归零，页面上找不到任何异常痕迹，
 * 用户只会以为待办都处理完了。调用方拿到 null 就保留上一份快照并点亮顶部
 * 的重试提示条——展示旧数据总好过展示假数据。
 */
function keepOnError<T>(rows: Promise<T[]>): Promise<T[] | null> {
  return rows.catch(() => null);
}

export function LibraryDetailView({ libraryId }: { libraryId: number }) {
  const initialSnapshot = getLibraryDetailSnapshot(libraryId);
  const restoredFromSnapshot = useRef(initialSnapshot !== undefined);
  const { canManageLibraries } = usePermissions();
  const { activeJobs } = useJobs();
  const restoreScrollRef = useScrollRestoration(`library:${libraryId}`, {
    anchorAttribute: "data-library-item-id",
  });
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);
  // 滚动位置恢复与字母索引联动共用同一个真实滚动容器，合并 callback ref
  // 避免两套监听器各自猜测 window/document。
  const scrollRef = useCallback(
    (node: HTMLDivElement | null) => {
      restoreScrollRef(node);
      setScrollElement(node);
    },
    [restoreScrollRef],
  );
  const confirm = useConfirm();
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(initialSnapshot?.libraries ?? null);
  const [items, setItems] = useState<LibraryItem[]>(initialSnapshot?.items ?? []);
  // 服务端还有没有下一页；滚动加载的哨兵据此决定是否继续观察
  const [wallHasMore, setWallHasMore] = useState(initialSnapshot?.wallHasMore ?? false);
  // 本库全部条目 id：海报墙分页后这份名单仍要完整——「追踪中」要靠它把
  // 已入库的订阅剔掉，而已入库的那部可能在第 5 页上，光看当前页会误判
  const [ownedIds, setOwnedIds] = useState<Set<number>>(
    () => initialSnapshot?.ownedIds ?? new Set(),
  );
  // A-Z 索引条的分档（按标题排序时才有意义）
  const [wallIndex, setWallIndex] = useState<LibraryIndexEntry[]>(initialSnapshot?.wallIndex ?? []);
  // 当前窗口在整份排序里的起点：0 = 从头开始；点字母跳转后是该档的 offset。
  // 轮询要按这个起点重拉，否则每 3 秒把用户拽回墙首
  const [wallStart, setWallStart] = useState(initialSnapshot?.wallStart ?? 0);
  // 与分页窗口起点分离：wallStart 只决定服务端从哪里取；活动字母要跟随
  // 用户在已加载窗口里的真实滚动位置。
  const [activeWallInitial, setActiveWallInitial] = useState<string | null>(null);
  const [unidentified, setUnidentified] = useState<UnidentifiedGroup[]>(initialSnapshot?.unidentified ?? []);
  const [review, setReview] = useState<ReviewGroup[]>(initialSnapshot?.review ?? []);
  const [ignored, setIgnored] = useState<UnidentifiedGroup[]>(initialSnapshot?.ignored ?? []);
  const [missing, setMissing] = useState<MissingItem[]>(initialSnapshot?.missing ?? []);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>(initialSnapshot?.subscriptions ?? []);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<MediaLibrary | null>(null);
  // 整理文件名对话框的目标库；null = 关闭
  const [organizeTarget, setOrganizeTarget] = useState<MediaLibrary | null>(null);
  // 整库元数据刷新进度（进行中每 3 秒轮询，结束自动刷新库存）
  const [metaRefresh, setMetaRefresh] = useState<MetadataRefreshProgress | null>(
    initialSnapshot?.metaRefresh ?? null,
  );
  // 删除/转移等详情页操作会把旧窗口标记为过期。先保留它供滚动锚点落脚，
  // 后台对账成功后再清除标记；请求失败时下次返回仍会重试。
  const [snapshotStale, setSnapshotStale] = useState(initialSnapshot?.stale ?? false);
  // 待处理抽屉：从哪个入口点进来就落在哪个 tab；null = 关闭
  const [issueTab, setIssueTab] = useState<IssueTab | null>(null);

  // 轮询乱序守卫：扫描期间后端响应时间抖动大，上一轮的慢响应可能晚于
  // 下一轮到达，不作废就会用旧快照覆盖新状态（进度回跳、胶囊闪烁）
  const reloadSeq = useRef(0);
  // 海报墙已加载的格数：轮询按这个数重拉第一页，用户滚到第几屏就刷新到第几屏
  // ——否则每轮轮询都把墙缩回首屏，正在看的位置被抽走
  const wallLoaded = useRef(initialSnapshot?.wallLoaded ?? WALL_PAGE_SIZE);
  // 当前排序（扫描补探阶段切到「待补探优先」）。放 ref 而不进依赖：reload
  // 每轮都读最新值，不必为切排序重建回调链
  const wallSort = useRef<LibraryItemSort>(initialSnapshot?.wallSort ?? "title");
  // 当前窗口起点的 ref 版：reload 每轮读它，不进依赖（同 wallSort）
  const wallOffset = useRef(initialSnapshot?.wallOffset ?? 0);
  const snapshotRef = useRef<LibraryDetailSnapshot | null>(null);
  snapshotRef.current = libraries
    ? {
        libraries,
        items,
        wallHasMore,
        ownedIds,
        wallIndex,
        wallStart,
        unidentified,
        review,
        ignored,
        missing,
        subscriptions,
        metaRefresh,
        wallLoaded: wallLoaded.current,
        wallSort: wallSort.current,
        wallOffset: wallOffset.current,
        stale: snapshotStale,
      }
    : null;

  // 布局提交后就更新快照，路由切换的下一棵树可以在首帧直接读取它；不能只在
  // 卸载 cleanup 中写入，因为新路由的首次 render 可能早于被动 effect 的 cleanup。
  useLayoutEffect(() => {
    if (snapshotRef.current) setLibraryDetailSnapshot(libraryId, snapshotRef.current);
  }, [
    ignored,
    items,
    libraries,
    libraryId,
    metaRefresh,
    missing,
    ownedIds,
    review,
    subscriptions,
    snapshotStale,
    unidentified,
    wallHasMore,
    wallIndex,
    wallStart,
  ]);

  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    const wanted = wallLoaded.current;
    const from = wallOffset.current;
    const itemPages = Array.from(
      { length: Math.ceil(wanted / WALL_API_PAGE_SIZE) },
      (_, page) => {
        const offset = from + page * WALL_API_PAGE_SIZE;
        return listLibraryItems(libraryId, {
          sort: wallSort.current,
          limit: Math.min(WALL_API_PAGE_SIZE, wanted - page * WALL_API_PAGE_SIZE),
          offset,
        });
      },
    );
    Promise.all([
      listLibraries(),
      Promise.all(itemPages).then((pages) => pages.flat()),
      listLibraryItemIds(libraryId).catch(() => []),
      listLibraryItemIndex(libraryId).catch(() => []),
      canManageLibraries
        ? keepOnError(listUnidentifiedLibraryFiles(libraryId))
        : Promise.resolve([]),
      canManageLibraries
        ? keepOnError(listLibraryIdentityReviewCases(libraryId))
        : Promise.resolve([]),
      canManageLibraries
        ? keepOnError(listIgnoredLibraryFiles(libraryId))
        : Promise.resolve([]),
      canManageLibraries
        ? keepOnError(listMissingLibraryFiles(libraryId))
        : Promise.resolve([]),
      listSubscriptions().catch(() => []),
    ])
      .then(([libs, libraryItems, ids, index, unknown, reviewGroups, ignoredGroups, missingItems, subs]) => {
        if (seq !== reloadSeq.current) return;
        setSnapshotStale(false);
        // 四张待办清单只要有一张没拿到，就保留上一份快照并点亮顶部提示条。
        // 把失败折成空数组等于对用户说"没有待办了"：胶囊消失、⋯ 菜单的计数
        // 归零，一个 500/超时/权限不足看起来和"全处理完了"一模一样
        setFailed(
          [unknown, reviewGroups, ignoredGroups, missingItems].some((rows) => rows === null),
        );
        // 轮询快照内容没变时复用旧引用：库存墙逐条目复用（配合 InventoryCell
        // 的 memo，只有真正变化的格子重渲染），其余列表整体复用。否则扫描期间
        // 每 3 秒就把几百个格子全部重画一遍，表现为周期性卡顿
        setLibraries((prev) => (prev ? keepIfEqual(prev, libs) : libs));
        setItems((prev) => reconcileList(prev, libraryItems, (i) => i.media_item_id));
        wallLoaded.current = Math.max(WALL_PAGE_SIZE, libraryItems.length);
        // 拿满这一页就假定后面还有；真到底时下一次追加会拿到空数组并收尾
        setWallHasMore(libraryItems.length >= wanted);
        // 内容没变就复用旧集合，别让「追踪中」每轮轮询白白重算重画
        setOwnedIds((prev) =>
          ids.length === prev.size && ids.every((i) => prev.has(i)) ? prev : new Set(ids),
        );
        setWallIndex((prev) => keepIfEqual(prev, index));
        if (unknown !== null) setUnidentified((prev) => keepIfEqual(prev, unknown));
        if (reviewGroups !== null) setReview((prev) => keepIfEqual(prev, reviewGroups));
        if (ignoredGroups !== null) setIgnored((prev) => keepIfEqual(prev, ignoredGroups));
        if (missingItems !== null) setMissing((prev) => keepIfEqual(prev, missingItems));
        setSubscriptions((prev) => keepIfEqual(prev, subs));
        // 整库刷新可能是别处（首页卡片/其他设备）发起的：库列表响应里带着
        // 状态，据此补种进度面板——否则只有挂载时那一次探测，之后发起的
        // 刷新这个页面永远看不见。已有进行中的状态时不覆盖（专用轮询更新鲜）
        const remote = libs.find((l) => l.id === libraryId)?.metadata_refresh;
        if (remote?.refreshing) setMetaRefresh((prev) => (prev?.refreshing ? prev : remote));
      })
      // 瞬时失败（网络抖动/后端忙）不清已有数据：failed 只决定顶部提示条，
      // 页面继续用上一份快照展示，下一轮轮询成功即自动恢复
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, [canManageLibraries, libraryId]);

  useEffect(() => {
    // 从详情页返回时，已加载分页窗口就在会话快照中。立刻重拉会先把墙缩成
    // 首批 60 条，深处的滚动目标又会失去高度；之后仍由可见页轮询负责对账。
    if (!restoredFromSnapshot.current || snapshotStale) {
      restoredFromSnapshot.current = true;
      reload();
    }
  }, [reload, snapshotStale]);

  // 海报墙滚到底时向服务端追加一页。并发闸门用 ref：哨兵在快速滚动中会
  // 连续触发几次，不挡住就是同一页被拉好几遍
  const loadingMore = useRef(false);
  const loadMore = useCallback(() => {
    if (loadingMore.current) return;
    loadingMore.current = true;
    // 分页请求开始后作废正在路上的轮询响应；否则旧轮询可能先/后返回，
    // 用较短的窗口覆盖刚追加的页面，把用户滚动到的内容又截回首批。
    const requestSeq = ++reloadSeq.current;
    const offset = wallOffset.current + wallLoaded.current;
    listLibraryItems(libraryId, { sort: wallSort.current, limit: WALL_PAGE_SIZE, offset })
      .then((next) => {
        if (requestSeq !== reloadSeq.current) return;
        wallLoaded.current += next.length;
        // 追加与轮询可能交叠着回来，同一条目被拿到两次——按 id 去重再拼
        setItems((current) => {
          const seen = new Set(current.map((i) => i.media_item_id));
          return [...current, ...next.filter((i) => !seen.has(i.media_item_id))];
        });
        setWallHasMore(next.length >= WALL_PAGE_SIZE);
      })
      .catch(() => {
        if (requestSeq === reloadSeq.current) setWallHasMore(false);
      })
      .finally(() => {
        loadingMore.current = false;
      });
  }, [libraryId]);

  // 海报墙顶部的锚：跳字母后滚回墙首，否则用户停在原来的滚动位置上，
  // 看到的是新一批的中间，像是"点了没反应"
  const wallTop = useRef<HTMLDivElement>(null);
  const wallGrid = useRef<HTMLDivElement>(null);
  /**
   * 跳到某个首字母档：换掉整个窗口（而不是继续往后追加），此后照常向下滚动加载。
   * offset=0 即回到墙首，索引条的「全部」走的也是这条路。
   */
  const jumpTo = useCallback(
    (offset: number) => {
      loadingMore.current = true; // 跳转期间挡住哨兵，别让旧窗口的追加插进来
      // 作废在途的轮询：它拿的是旧窗口的那一页，晚一步回来就把用户刚跳到的
      // 位置又拽回去（表现为"点了字母，一秒后自己跳回墙首"）
      reloadSeq.current += 1;
      wallOffset.current = offset;
      listLibraryItems(libraryId, { sort: "title", limit: WALL_PAGE_SIZE, offset })
        .then((page) => {
          wallLoaded.current = Math.max(WALL_PAGE_SIZE, page.length);
          setWallStart(offset);
          setItems(page);
          setWallHasMore(page.length >= WALL_PAGE_SIZE);
          wallTop.current?.scrollIntoView({ block: "start", behavior: "smooth" });
        })
        .catch(() => {})
        .finally(() => {
          loadingMore.current = false;
        });
    },
    [libraryId],
  );

  // 进入页面先探一次元数据刷新状态（可能是别的入口/上次会话发起的）
  useEffect(() => {
    if (!canManageLibraries) {
      setMetaRefresh(null);
      return;
    }
    getMetadataRefreshProgress(libraryId)
      .then(setMetaRefresh)
      .catch(() => {});
  }, [canManageLibraries, libraryId]);

  // 刷新进行中每 2 秒轮询状态，结束自动重拉库存（海报/档案已更新）。
  // 2 秒是"阶段文字跟得上"与"别把接口打太密"的折中：单部片的一个阶段
  // 常在数秒内走完，轮询再慢就只能看到最后一个阶段
  const refreshingMeta = Boolean(metaRefresh?.refreshing);
  useVisiblePolling(
    () => {
      getMetadataRefreshProgress(libraryId)
        .then((p) => {
          // 阶段没推进时复用旧引用，别让 2 秒一次的轮询白白重渲染页面
          setMetaRefresh((prev) => keepIfEqual(prev, p));
          if (!p.refreshing) reload();
        })
        // 瞬时失败保留旧状态、下一轮继续：这里一旦清空，refreshingMeta 变
        // false 会把本轮询连根停掉，后台还在跑的刷新从此在界面上失踪
        // （曾是线上实况）。刷新真结束时成功响应会带 refreshing=false 收尾
        .catch(() => {});
    },
    canManageLibraries && refreshingMeta ? 2000 : null,
  );

  const library = libraries?.find((l) => l.id === libraryId) ?? null;
  usePageTitle(library?.name);

  // 扫描/整理期间轮询，结束自动展示最新库存与文件名
  const busy = Boolean(library?.scanning || library?.organizing);
  // 写入中暂缓入账的文件数（watchdog 已发现、等拷贝/下载落定后自动补扫入库）
  const importing = busy ? 0 : (library?.last_scan?.deferred ?? 0);
  // busy 刚结束后保持快轮询一小段再降速：监控去抖触发的连环扫描之间隔着
  // 几秒空档，一采样到空档就降去 30 秒档的话，下一轮扫描的开始要很久
  // 才被发现，状态看起来就是"时隐时现"
  const [recentlyBusy, setRecentlyBusy] = useState(false);
  useEffect(() => {
    if (busy) {
      setRecentlyBusy(true);
      return;
    }
    if (!recentlyBusy) return;
    const timer = setTimeout(() => setRecentlyBusy(false), 12_000);
    return () => clearTimeout(timer);
  }, [busy, recentlyBusy]);
  // 忙时快轮询；有文件入库中 / 元数据刷新中中速跟进（刷新会边跑边换海报，
  // 让墙上的图逐步更新）；空闲低频兜底——后台自发的扫描（实时监控/定时
  // 对账）页面开着不动也能感知到。页面隐藏时暂停、恢复可见立即补一次
  useVisiblePolling(
    reload,
    busy || recentlyBusy ? 3000 : importing > 0 || refreshingMeta ? 10_000 : 30_000,
  );

  // 待识别的文件总数（清单按条目目录分组，一组可能是一部剧的几十集）
  const unidentifiedFiles = unidentified.reduce((n, g) => n + g.file_count, 0);
  // ⋯ 菜单上的待处理件数：数的是**要拍几次板**，不是涉及多少文件——
  // 一部剧几十集聚成一组、认领一次就完事，按文件数报会把 3 件活说成 80 件
  const pendingCount = missing.length + unidentified.length + review.length;
  // 从菜单进抽屉时的落点：按抽屉自身的 tab 顺序取第一个有内容的，都空了
  // 落在「待识别」（最常用的那张，且空态文案本身就是有效答复）
  const pendingTab: IssueTab =
    missing.length > 0
      ? "missing"
      : unidentified.length > 0
        ? "unidentified"
        : review.length > 0
          ? "review"
          : ignored.length > 0
            ? "ignored"
            : "unidentified";

  // 整库刷新中"正在处理哪几部、各在什么阶段"：海报墙据此点亮对应的格，
  // 用户不必在进度面板与墙之间对片名
  const refreshPhaseById = useMemo(
    () => new Map((metaRefresh?.active ?? []).map((a) => [a.media_item_id, a.phase])),
    [metaRefresh],
  );
  // 统一 Job 资源索引让 CLI / Agent 发起的任务也能直接点亮海报格，不依赖
  // 详情组件是否已经挂载。一个任务关联剧集条目时只显示一枚聚合状态。
  const jobPhaseById = useMemo(() => {
    const mapped = new Map<number, string>();
    for (const job of activeJobs) {
      const resource = job.resources.find((item) => item.resource_type === "media_item");
      if (!resource) continue;
      const mediaId = Number(resource.resource_id);
      if (!Number.isFinite(mediaId) || mapped.has(mediaId)) continue;
      const prefix = job.status === "blocked" ? "需要处理" : "后台任务";
      mapped.set(mediaId, `${prefix} · ${job.progress.message}`);
    }
    return mapped;
  }, [activeJobs]);
  const libraryJobs = useMemo(
    () =>
      activeJobs.filter((job) =>
        job.resources.some(
          (resource) =>
            resource.resource_type === "library" && resource.resource_id === String(libraryId),
        ),
      ),
    [activeJobs, libraryId],
  );

  // 扫描的补探阶段：把「还有文件没读出规格」的条目排到墙前面并点亮——
  // 头部胶囊只有一个总数（22/25），用户看不出具体是哪几部还在处理。
  // 仅在补探阶段生效：平时按标题排，阶段结束墙自动落回原序
  const probing = Boolean(
    library?.scanning && library.scan_progress?.phase === "probing",
  );
  const initialByOffset = useMemo(
    () => new Map(wallIndex.map((entry) => [entry.offset, entry.initial])),
    [wallIndex],
  );

  // 用户滚动时，用每个字母首部影片的 DOM 锚点更新活动字母。滚动事件用
  // requestAnimationFrame 合帧；每帧最多读取 27 个锚点，与已加载影片数无关。
  useEffect(() => {
    const fallback = wallInitialAtOffset(wallIndex, wallStart);
    setActiveWallInitial(fallback);
    if (!scrollElement || probing) return;

    let frame = 0;
    const update = () => {
      frame = 0;
      const grid = wallGrid.current;
      if (!grid) return;
      const markers = Array.from(
        grid.querySelectorAll<HTMLElement>("[data-wall-initial]"),
        (marker) => ({
          initial: marker.dataset.wallInitial ?? "",
          top: marker.getBoundingClientRect().top,
        }),
      ).filter((marker) => marker.initial !== "");
      const viewportTop = scrollElement.getBoundingClientRect().top + 1;
      const next = activeWallInitialAtViewport(markers, viewportTop, fallback);
      setActiveWallInitial((current) => (current === next ? current : next));
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };

    scrollElement.addEventListener("scroll", schedule, { passive: true });
    schedule();
    return () => {
      scrollElement.removeEventListener("scroll", schedule);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [items.length, probing, scrollElement, wallIndex, wallStart]);

  // 排序切换是**服务端**的事（墙是分页的，本地排只能排到已加载的那几屏）：
  // 阶段一变就换排序键重拉第一页，回到首屏看的就是正在处理的那几部
  useEffect(() => {
    const next: LibraryItemSort = probing ? "probing" : "title";
    if (wallSort.current === next) return;
    wallSort.current = next;
    wallLoaded.current = WALL_PAGE_SIZE;
    // 换了排序，之前跳到的字母位置就没意义了，窗口回到墙首
    wallOffset.current = 0;
    setWallStart(0);
    reload();
  }, [probing, reload]);

  // 追踪中：目标是本库、且尚未在库存中出现的订阅
  const pending = useMemo(() => {
    if (!libraries || !library) return [];
    return subscriptions.filter(
      (s) =>
        effectiveLibraryId(s, libraries) === library.id &&
        !ownedIds.has(s.media.media_item_id ?? -1) &&
        s.progress.imported === 0,
    );
  }, [libraries, library, subscriptions, ownedIds]);

  // 只有一次都没加载成功过才整页报错；已有数据在手时，瞬时失败只在页内
  // 挂提示条（stale-while-error）——为一次网络抖动把整面海报墙换成错误屏，
  // 用户看到的就是"页面闪没了"
  // 兜底态（加载中/失败/不存在）也渲染 PageNav（库名未知，末项留空）：向外壳
  // 登记「本页自带顶栏」，否则移动端全局顶栏（☰ + logo）会先显示再消失、顶部闪一下。
  const navFallback = { label: "媒体库", href: "/library" as Route };

  if (failed && libraries === null) {
    return (
      <div className="flex-1">
        <PageNav title="" fallback={navFallback} />
        <CenteredNote>
          <p className="text-ui text-[var(--text-muted)]">媒体库加载失败</p>
          <button
            type="button"
            onClick={reload}
            className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            重试
          </button>
        </CenteredNote>
      </div>
    );
  }

  if (libraries === null) {
    return (
      <div className="flex-1">
        <PageNav title="" fallback={navFallback} />
        <CenteredNote>
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          <p className="text-ui text-[var(--text-muted)]">正在加载媒体库…</p>
        </CenteredNote>
      </div>
    );
  }

  if (library === null) {
    return (
      <div className="flex-1">
        <PageNav title="" fallback={navFallback} />
        <CenteredNote>
          <p className="text-ui text-[var(--text-muted)]">这个媒体库不存在（可能已被删除）</p>
          <Link
            href={"/library" as Route}
            replace
            className="btn-glass px-4 py-2 text-ui font-medium"
          >
            返回媒体库
          </Link>
        </CenteredNote>
      </div>
    );
  }

  const meta = LIBRARY_KIND_META[library.kind];
  const { stats } = library;

  // 库操作全部收进 ⋯ 菜单，顶栏只留这一个入口；运行状态看头部下方的胶囊
  const actionsMenu = canManageLibraries ? (
    <LibraryActionsMenu
      scanning={Boolean(library.scanning)}
      scanPhase={library.scan_progress?.phase ?? null}
      scanPercent={
        library.scan_progress && library.scan_progress.total > 0
          ? Math.min(
              100,
              Math.round(
                (library.scan_progress.processed / library.scan_progress.total) * 100,
              ),
            )
          : null
      }
      onToggleScan={() => {
        setNotice(null);
        if (library.scanning) {
          void stopLibraryScan(library.id)
            .then(() => reload())
            .catch((e) => setNotice((e as Error).message));
          return;
        }
        // 重操作先确认（停止不确认：停止本身就是在纠正）
        void confirm(scanLibraryConfirm(library.name)).then((ok) => {
          if (!ok) return;
          void startLibraryScan(library.id)
            .then(() => reload())
            .catch((e) => setNotice((e as Error).message));
        });
      }}
      organizing={Boolean(library.organizing)}
      organizePercent={
        library.organize_progress && library.organize_progress.total > 0
          ? Math.min(
              100,
              Math.round(
                (library.organize_progress.processed / library.organize_progress.total) *
                  100,
              ),
            )
          : null
      }
      refreshingMeta={refreshingMeta}
      metaProgress={
        metaRefresh && metaRefresh.total > 0
          ? `${metaRefresh.processed}/${metaRefresh.total}`
          : null
      }
      busy={busy}
      onOrganize={() => {
        setNotice(null);
        setOrganizeTarget(library);
      }}
      onToggleMetaRefresh={() => {
        setNotice(null);
        const kick = (action: Promise<unknown>) =>
          action
            .then(() =>
              getMetadataRefreshProgress(libraryId)
                .then(setMetaRefresh)
                .catch(() => {}),
            )
            .catch((e) => setNotice((e as Error).message));
        if (refreshingMeta) {
          void kick(stopLibraryMetadataRefresh(libraryId));
          return;
        }
        void confirm(refreshLibraryConfirm(library.name)).then((ok) => {
          if (ok) void kick(startLibraryMetadataRefresh(libraryId));
        });
      }}
      pendingCount={pendingCount}
      onOpenPending={() => setIssueTab(pendingTab)}
      onEdit={() => setEditing(library)}
    />
  ) : null;

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      {/* 顶栏：返回媒体库 + 吸顶库名 + 库操作 ⋯（无 pt——顶边与侧栏卡片顶边齐平） */}
      <PageNav
        title={library.name}
        fallback={navFallback}
        actions={actionsMenu}
      />
      {/* —— 库头部 —— */}
      <div className="px-6 max-md:px-4">
        <div className="flex items-center gap-2.5">
          <h2 className="text-on-image truncate text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[20px]">
            {library.name}
          </h2>
          {library.is_default && (
            <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.12] px-2 py-0.5 text-caption font-semibold text-white/90">
              默认
            </span>
          )}
        </div>
        <p className="text-on-image mt-1.5 truncate text-ui text-[var(--text-muted)] max-md:text-sub">
          {meta.label}库 · {stats.item_count} 部作品 · {stats.file_count} 个文件 ·{" "}
          {formatBytes(stats.total_size_bytes)}
        </p>
        {/* 这一行是**那一次扫描的成绩单**，每个数都是历史（last_scan 只在扫描
            收尾时覆写，见 _last_scan_view）。所以说「未识别 N」——它是当轮
            的战果，不是现在还剩多少活。当前待办一律看下方胶囊与 ⋯ 菜单里的
            「待处理」，那两处读的是实时清单。曾经这里写「待识别 N」，扫完
            之后哪怕全处理干净了也永远停在那个数，与消失的胶囊自相矛盾 */}
        {library.last_scan && !busy && (
          <p className="mt-1 text-sub text-white/45">
            最近扫描 {formatRelativeTime(library.last_scan.finished_at)}
            {library.last_scan.cancelled ? "（手动停止，未扫完）" : ""} · 新入账{" "}
            {library.last_scan.scanned}（识别 {library.last_scan.identified} / 未识别{" "}
            {library.last_scan.unidentified}）
            {library.last_scan.retried > 0
              ? ` · 重试识别 ${library.last_scan.retried} 个待识别文件`
              : ""}
            {library.last_scan.marked_missing > 0
              ? ` · 标记丢失 ${library.last_scan.marked_missing}`
              : ""}
            {library.last_scan.cleared_missing > 0
              ? ` · 清理丢失记录 ${library.last_scan.cleared_missing}`
              : ""}
            {library.last_scan.deferred > 0
              ? ` · ${library.last_scan.deferred} 个写入中暂缓（稍后自动补扫）`
              : ""}
            {library.last_scan.errors.length > 0
              ? ` · ${library.last_scan.errors[0]}`
              : ""}
          </p>
        )}
        {/* —— 健康状态胶囊：待办收进抽屉，海报墙保持干净 —— */}
        {canManageLibraries && (missing.length > 0 ||
          unidentified.length > 0 ||
          review.length > 0 ||
          importing > 0 ||
          libraryJobs.length > 0 ||
          refreshingMeta ||
          busy) && (
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            {busy && (
              <span className="flex items-center gap-1.5 rounded-full border border-[var(--info)]/35 bg-[var(--info)]/[0.12] px-3 py-1 text-sub font-semibold text-[var(--info)]">
                <span className="size-3 animate-spin rounded-full border-[1.5px] border-[var(--info)]/30 border-t-[var(--info)]" />
                {busyText(
                  library.scanning ? library.scan_progress : library.organize_progress,
                )}
              </span>
            )}
            {/* 刷新元数据的按钮已收进 ⋯ 菜单；它是全量重刷、耗时长，
                进度另用下方的面板完整展示（到哪部了、在做什么） */}
            {importing > 0 && (
              <span className="flex items-center gap-1.5 rounded-full border border-[var(--info)]/35 bg-[var(--info)]/[0.12] px-3 py-1 text-sub font-semibold text-[var(--info)]">
                <span className="size-1.5 animate-pulse rounded-full bg-[var(--info)]" />
                已发现 {importing} 个新文件 · 写入完成后自动入库
              </span>
            )}
            {libraryJobs.length > 0 && (
              <span className="flex items-center gap-1.5 rounded-full border border-[var(--info)]/35 bg-[var(--info)]/[0.12] px-3 py-1 text-sub font-semibold text-[var(--info)]">
                <span className="size-1.5 animate-pulse rounded-full bg-[var(--info)]" />
                {libraryJobs.length} 个后台任务正在处理库内影片
              </span>
            )}
            {missing.length > 0 && (
              <button
                type="button"
                onClick={() => setIssueTab("missing")}
                className="flex items-center gap-1.5 rounded-full border border-white/[0.14] bg-white/[0.06] px-3 py-1 text-sub font-semibold text-white/75 transition hover:bg-white/[0.12] hover:text-white"
              >
                <span className="size-1.5 rounded-full bg-white/40" />
                {missing.length} 个条目缺失
              </button>
            )}
            {/* 待识别与待复核都是「识别环节没搞定、等人工看一眼」，同属 --warn。
                两者的区别由文案承担：紫色曾经暗示它们是两类不同的东西，而紫在
                订阅首页表示「等着就行」，同一个色在两页说反话。 */}
            {unidentified.length > 0 && (
              <button
                type="button"
                onClick={() => setIssueTab("unidentified")}
                className="flex items-center gap-1.5 rounded-full border border-[var(--warn)]/35 bg-[var(--warn)]/[0.12] px-3 py-1 text-sub font-semibold text-[var(--warn)] transition hover:bg-[var(--warn)]/[0.22]"
              >
                <span className="size-1.5 rounded-full bg-[var(--warn)]" />
                {unidentifiedFiles} 个文件待识别
                {unidentified.length > 1 ? ` · ${unidentified.length} 组` : ""}
              </button>
            )}
            {review.length > 0 && (
              <button
                type="button"
                onClick={() => setIssueTab("review")}
                className="flex items-center gap-1.5 rounded-full border border-[var(--warn)]/35 bg-[var(--warn)]/[0.12] px-3 py-1 text-sub font-semibold text-[var(--warn)] transition hover:bg-[var(--warn)]/[0.22]"
              >
                <span className="size-1.5 rounded-full bg-[var(--warn)]" />
                {review.length} 个条目待复核身份
              </button>
            )}
          </div>
        )}
        {/* —— 整库刷新进度：全量重刷每部都要重下图，慢，状态给全 —— */}
        {canManageLibraries && refreshingMeta && metaRefresh && (
          <MetadataRefreshPanel
            state={metaRefresh}
            onStop={() => {
              setNotice(null);
              void stopLibraryMetadataRefresh(libraryId)
                .then(() =>
                  getMetadataRefreshProgress(libraryId)
                    .then(setMetaRefresh)
                    .catch(() => {}),
                )
                .catch((e) => setNotice((e as Error).message));
            }}
          />
        )}
        {failed && (
          <p className="mt-3 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3.5 py-2 text-sub text-amber-200">
            与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
          </p>
        )}
        {notice && (
          <p className="mt-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2 text-sub text-red-200">
            {notice}
          </p>
        )}
      </div>

      {/* —— 待处理抽屉：缺失 / 待识别 / 身份复核 / 已忽略，从胶囊或 ⋯ 菜单进入 —— */}
      {canManageLibraries && (
        <IssueDrawer
          open={issueTab}
          onClose={() => setIssueTab(null)}
          onSwitchTab={setIssueTab}
          libraryId={libraryId}
          missing={missing}
          unidentified={unidentified}
          review={review}
          ignored={ignored}
          movie={library.kind === "movie"}
          onChanged={reload}
        />
      )}

      {/* —— 追踪中（订阅已指向本库、文件未落地）：自动化正在进行的部分，置顶优先 —— */}
      {pending.length > 0 && (
        <div className="mt-6 px-6 max-md:mt-4 max-md:px-4">
          <h3 className="text-on-image text-body-lg font-semibold text-white/85">
            追踪中
            <span className="ml-2 text-sub font-normal text-[var(--text-faint)]">
              已订阅、资源到位后自动入库
            </span>
          </h3>
          <div className="mt-4 grid gap-x-4 gap-y-7 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]">
            {pending.map((sub) => (
              <PendingCell key={sub.id} sub={sub} />
            ))}
          </div>
        </div>
      )}

      {/* —— 库存海报墙（追踪中置顶时补「已入库」标题，两片海报墙不致连读）—— */}
      {items.length === 0 && unidentified.length === 0 ? (
        <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">
          这个库还没有内容。
          <br />
          {canManageLibraries
            ? "点右上角菜单里的「扫描库」把已有影片识别入库；订阅内容下载完成后也会自动进来。"
            : "订阅内容下载并入库后会显示在这里。"}
        </p>
      ) : (
        <>
          {pending.length > 0 && (
            <h3 className="text-on-image mt-10 px-6 text-body-lg font-semibold text-white/85 max-md:mt-7 max-md:px-4">
              已入库
            </h3>
          )}
          <div ref={wallTop} className={pending.length > 0 ? "mt-4" : "mt-6 max-md:mt-4"}>
            {/* 索引条与墙并排：条固定在视口右侧（sticky），墙照常滚 */}
            <div className="flex items-start gap-2 px-6 max-md:gap-1 max-md:px-4">
              <div
                ref={wallGrid}
                className="grid flex-1 gap-x-4 gap-y-7 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]"
              >
                {items.map((item, index) => (
                  <InventoryCell
                    key={item.media_item_id}
                    item={item}
                    libraryId={libraryId}
                    wallInitial={initialByOffset.get(wallStart + index)}
                    workingLabel={
                      refreshPhaseById.get(item.media_item_id) ??
                      jobPhaseById.get(item.media_item_id) ??
                      (probing && item.probe_pending_count > 0 ? "正在读取规格" : undefined)
                    }
                  />
                ))}
              </div>
              {/* 补探阶段排序不是拼音序，字母跳转会跳错位置——那几分钟里收起来 */}
              {!probing && (
                <WallIndexBar index={wallIndex} active={activeWallInitial} onJump={jumpTo} />
              )}
            </div>
          </div>
          <WallLoadMore
            hasMore={wallHasMore}
            loaded={items.length}
            start={wallStart}
            total={library.stats.item_count}
            onReach={loadMore}
          />
        </>
      )}

      {canManageLibraries && (
        <>
          <LibraryFormDialog
            state={editing}
            onClose={() => setEditing(null)}
            onSaved={() => {
              setEditing(null);
              reload();
            }}
          />
          <LibraryOrganizeDialog
            library={organizeTarget}
            onClose={() => setOrganizeTarget(null)}
            onChanged={reload}
          />
        </>
      )}
    </div>
  );
}

/* —— 库操作折叠菜单：待处理 / 扫描库 / 整理文件名 / 刷新元数据 / 编辑库 全收进 ⋯，头部不再摆一排按钮。
 *  三个长任务在菜单里就能看进度并原地停止，运行状态另由头部胶囊常驻呈现。 */

interface LibraryActionsMenuProps {
  scanning: boolean;
  /** 扫描类任务的当前阶段；没在跑为 null——决定停止入口给不给 */
  scanPhase: ScanPhase | null;
  /** 扫描进度百分比；无进度信息时为 null */
  scanPercent: number | null;
  onToggleScan: () => void;
  organizing: boolean;
  /** 整理进度百分比；无进度信息时为 null */
  organizePercent: number | null;
  refreshingMeta: boolean;
  /** 元数据刷新进度文案 "已处理/总数"；无进度信息时为 null */
  metaProgress: string | null;
  /** 扫描或整理进行中：编辑库须锁定（任务正按当前根路径读写台账） */
  busy: boolean;
  /** 待处理总件数（缺失 + 待识别 + 身份复核）；0 也照常给入口，见下方说明 */
  pendingCount: number;
  onOpenPending: () => void;
  onOrganize: () => void;
  onToggleMetaRefresh: () => void;
  onEdit: () => void;
}

function LibraryActionsMenu({
  scanning,
  scanPhase,
  scanPercent,
  onToggleScan,
  organizing,
  organizePercent,
  refreshingMeta,
  metaProgress,
  busy,
  pendingCount,
  onOpenPending,
  onOrganize,
  onToggleMetaRefresh,
  onEdit,
}: LibraryActionsMenuProps) {
  // 与站点配置一致用 Radix DropdownMenu：Portal 到 body + 碰撞检测，
  // 不会被头部容器裁切；开合/外部点击/键盘导航全交给 Radix。
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  const running = scanning || organizing || refreshingMeta;
  // 重识别占的是同一把库级锁，但它在改身份锚、不接受中途停止（后端会
  // 拒绝）。菜单据此把入口置灰并如实标出在做什么，不给按了没反应的按钮
  const stoppable = scanning && scanPhase !== "reidentifying";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="更多操作"
          // 长在顶栏里，与返回键并排：共用顶栏控件形状，不再是页面里的胶囊按钮
          className={`${PAGE_NAV_BUTTON_CLASS} relative data-[state=open]:bg-black/55 data-[state=open]:text-white`}
        >
          <MoreIcon className="size-[18px] max-md:size-[22px]" />
          {/* 收起的长任务在跑：触发按钮点一个小点，不至于被菜单藏住 */}
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
          className="menu-surface z-50 min-w-[11rem] !rounded-xl p-1"
        >
          {/* 待处理清单的**常驻**入口。头部胶囊是条件渲染的（有待办才出现），
              光靠它意味着清单一空抽屉就整个不可达——「已忽略」更是从来没有
              自己的胶囊，把待识别全忽略掉之后就再也回不去了。这里恒在，
              计数为 0 时也不隐藏：归档随时可查，空清单本身也是有效答案 */}
          <DropdownMenu.Item onSelect={onOpenPending} className={itemClass}>
            待处理{pendingCount > 0 ? ` ${pendingCount}` : ""}
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
          {/* 扫描中切换为「停止扫描」（增量幂等：已入账的保留，剩余下次继续） */}
          <DropdownMenu.Item
            onSelect={onToggleScan}
            disabled={(busy && !scanning) || (scanning && !stoppable)}
            className={itemClass}
          >
            {!scanning
              ? "扫描库"
              : stoppable
                ? `停止扫描${scanPercent === null ? "" : ` ${scanPercent}%`}`
                : `${SCAN_PHASE_LABELS[scanPhase ?? "ingesting"]}…`}
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onOrganize}
            disabled={busy && !organizing}
            className={itemClass}
          >
            {organizing
              ? `整理中…${organizePercent === null ? "" : ` ${organizePercent}%`}`
              : "整理文件名"}
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onToggleMetaRefresh} className={itemClass}>
            {refreshingMeta
              ? `停止刷新${metaProgress === null ? "" : ` ${metaProgress}`}`
              : "刷新元数据"}
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onEdit} disabled={busy} className={itemClass}>
            编辑库
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/**
 * 整库元数据刷新的进度面板。
 *
 * 全量重刷（每部片都重拉档案 + 重下图片 + 覆盖媒体目录）在大库上要跑很久，
 * 只给一个百分比等于让用户干等：这里把**到哪几部了、每部在做什么**都摊开，
 * 外加失败计数和停止入口。并发 3 路，所以 active 是列表。
 */
function MetadataRefreshPanel({
  state,
  onStop,
}: {
  state: MetadataRefreshProgress;
  onStop: () => void;
}) {
  const percent =
    state.total > 0 ? Math.min(100, Math.round((state.processed / state.total) * 100)) : 0;
  return (
    <div className="mt-3 rounded-xl border border-[var(--info)]/25 bg-[var(--info)]/[0.07] px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-sub font-semibold text-[var(--info)]">
          <span className="size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-[var(--info)]/30 border-t-[var(--info)]" />
          <span className="truncate">
            {state.stopping ? "正在停止刷新" : "正在刷新元数据"}
            {state.total > 0 ? ` ${state.processed}/${state.total}` : ""}
            {state.failed > 0 ? ` · 失败 ${state.failed}` : ""}
          </span>
        </div>
        <button
          type="button"
          onClick={onStop}
          disabled={state.stopping}
          className="shrink-0 text-sub font-medium text-white/70 transition hover:text-white disabled:opacity-50"
        >
          {state.stopping ? "收尾中…" : "停止"}
        </button>
      </div>
      {/* 进度条：全量刷新常以分钟计，一条能看出"在动"的进度很重要 */}
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/[0.08]">
        <div
          className="h-full rounded-full bg-[var(--info)] transition-[width] duration-500"
          style={{ width: `${percent}%` }}
        />
      </div>
      {state.active.length > 0 && (
        <div className="mt-2.5 space-y-1">
          {state.active.map((a) => (
            <p key={a.media_item_id} className="flex gap-2 text-sub text-white/70">
              <span className="min-w-0 flex-1 truncate">{a.title}</span>
              <span className="shrink-0 text-white/45">{a.phase}</span>
            </p>
          ))}
        </div>
      )}
      <p className="mt-2 text-caption text-white/40">
        全量重刷：重拉 TMDB 档案、按当前尺寸重下图片、覆盖媒体目录镜像；
        你手动选定的图不受影响
      </p>
    </div>
  );
}

/** 库存格：真实拥有的作品。点击进**媒体库条目详情**（本地刮削信息 +
 *  片源规格 + 条目操作），不再复用发现页的 TMDB 详情；格下标注库存概况。
 *
 *  memo 化 + reload 的逐条目引用复用（见 lib/poll-reconcile.ts）：轮询快照
 *  里没变化的条目沿用旧对象，这里比对通过就整格跳过——大库轮询时只有真正
 *  变化的格子会重渲染。 */
const InventoryCell = memo(function InventoryCell({
  item,
  libraryId,
  wallInitial,
  workingLabel,
}: {
  item: LibraryItem;
  libraryId: number;
  /** 该格是否为某个拼音首字母档的第一部影片；滚动联动只标记这些锚点。 */
  wallInitial?: string;
  /** 这一格正被后台处理（整库刷新的阶段 / 扫描补探）时的文案；不在处理为 undefined */
  workingLabel?: string;
}) {
  const inventoryLabel =
    item.kind === "tv" && item.inventory_summary
      ? formatLibraryInventorySummary(item.inventory_summary)
      : null;
  const visual: PosterVisualItem = {
    id: String(item.tmdb_id),
    source: "tmdb",
    type: item.kind,
    title: item.title,
    year: item.year ?? undefined,
    rating: 0,
    overlayDetails: inventoryLabel ? { primary: inventoryLabel } : undefined,
    // 海报可能是本地刮削资产的相对路径（断网可用），也可能是 TMDB 图床地址。
    // 与首页海报墙同样取 poster-card 派生图：海报墙是全站最大的一张图片网格，
    // 直出原图等于每屏多拉三倍字节（见 library-view.tsx 同名字段的注释）
    posterUrl: imageUrl(item.poster_url, "poster-card"),
  };
  // 文件全部缺失的"死条目"：海报置灰，一眼与在位内容区分
  const dead = item.file_count > 0 && item.missing_count >= item.file_count;
  // 卡片下方只保留片名与年份；缺失是需要常显的异常，作为唯一例外单独点灯。
  const abnormalLabel = dead
    ? "文件已全部缺失"
    : item.missing_count > 0
      ? `${item.missing_count} 个文件缺失`
      : null;
  return (
    // content-visibility：视口外的格子跳过布局与绘制，大库海报墙的滚动/更新
    // 成本只与可见格数相关；intrinsic-size 占住尺寸，滚动条不跳
    <div
      data-library-item-id={item.media_item_id}
      data-wall-initial={wallInitial}
      className="[contain-intrinsic-size:auto_270px] [content-visibility:auto]"
    >
      {/* 后台正在处理的那一格自己点亮：进度面板/胶囊列的是总数或片名，
          海报墙上也要能一眼看到"正在弄这部"，否则用户得在两处之间对片名 */}
      <div className="relative">
        <div className={dead ? "opacity-50 grayscale" : undefined}>
          <PosterCardVisual
            item={visual}
            href={`/library/${libraryId}/item/${item.media_item_id}` as Route}
            action={libraryCardAction(item)}
            revealInfoOnTouch
          />
        </div>
        {workingLabel && (
          <>
            <span className="pointer-events-none absolute inset-0 rounded-xl ring-2 ring-[var(--info)] ring-offset-0" />
            {/* 不用 backdrop-blur：海报墙每格一个模糊合成层会放大滚动时的 GPU 压力，底色加实即可 */}
            <span className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center gap-1.5 rounded-b-xl bg-[rgba(7,12,20,0.92)] px-2 py-1.5 text-micro font-medium text-[var(--info)]">
              <span className="size-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-[var(--info)]/30 border-t-[var(--info)]" />
              <span className="truncate">{workingLabel}</span>
            </span>
          </>
        )}
      </div>
      {abnormalLabel && (
        <p className="text-on-image mt-1.5 flex items-center gap-1.5 truncate text-caption text-[var(--text-muted)]">
          <span
            className={`size-1.5 shrink-0 rounded-full ${dead ? "bg-white/30" : "bg-[var(--warn)]"}`}
          />
          <span className="truncate">{abnormalLabel}</span>
        </p>
      )}
    </div>
  );
});

/**
 * 海报墙底部的滚动加载哨兵。
 *
 * 提前 600px 触发下一页，正常滚动速度下新一批在滚到底之前就已到位，看不出
 * 分页。已加载数变化后重新观察：一页塞不满视口时哨兵仍在屏内，重新观察会
 * 立刻再触发一次，直到填满或到底——否则墙会停在半屏、再也不加载。
 */
function WallLoadMore({
  hasMore,
  loaded,
  start,
  total,
  onReach,
}: {
  hasMore: boolean;
  loaded: number;
  /** 当前窗口在整份排序里的起点（跳字母后不为 0），决定进度文案的口径 */
  start: number;
  total: number;
  onReach: () => void;
}) {
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const target = sentinel.current;
    if (!target || !hasMore) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) onReach();
      },
      { rootMargin: "600px 0px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, loaded, onReach]);

  return (
    <div
      ref={sentinel}
      className="mt-8 flex h-10 items-center justify-center text-sub text-[var(--text-muted)]"
      aria-live="polite"
    >
      {hasMore
        ? `加载中…（第 ${start + 1}–${start + loaded} 部 / 共 ${Math.max(total, start + loaded)}）`
        : null}
    </div>
  );
}

/** A-Z 索引条的完整档位（与后端 sort_key.INITIALS 同序）：# 收尾。 */
const WALL_INITIALS = [...Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i)), "#"];

/** 索引条整高的上限（px）：27 档 × 18px。视口更矮时按视口等比压缩每格。 */
const WALL_INDEX_MAX_HEIGHT = WALL_INITIALS.length * 18;
/** 索引条最多占视口高度的比例：留出上下呼吸空间，也保证吸附居中有余量 */
const WALL_INDEX_HEIGHT = `min(${WALL_INDEX_MAX_HEIGHT}px, 72dvh)`;

/**
 * 海报墙右侧的 A-Z 快速定位条。
 *
 * 中文按拼音首字母分档（后端算好，见 sort_key 模块）。选中一档把窗口整体换到
 * 该档起点——墙是分页的，"跳到 S"意味着重新取一页，而不是在已加载的几屏里找。
 * 空档（该字母下没有作品）保留位置但不可选：字母位置固定，肌肉记忆才成立。
 *
 * 两个关键设计（都为触屏体验）：
 * 1. **高度自适应 + 吸附居中**：整条高度压到视口内（每格 flex 等分），sticky 的
 *    top 取"视口高减条高的一半"——滚动时恒定悬在视口右侧垂直居中处。原先固定
 *    486px + 吸顶，在手机上比屏幕还高，底部字母永远够不着。
 * 2. **滑动选中**：不再逐字母摆按钮，整条用 Pointer 事件统一接管（touch-none
 *    阻掉页面滚动、setPointerCapture 保证滑出条外不丢）：按下/滑动时按指尖纵向
 *    位置换算档位并浮出气泡预览，**松手才跳转**——跳转要重新取一页，滑动过程中
 *    连发请求既浪费也让墙抖动。鼠标点击等价于"按下即松手"，行为不变。
 */
function WallIndexBar({
  index,
  active,
  onJump,
}: {
  index: LibraryIndexEntry[];
  /** 当前视口所在档位；由海报墙滚动锚点实时更新。 */
  active: string | null;
  onJump: (offset: number) => void;
}) {
  const byInitial = useMemo(() => new Map(index.map((e) => [e.initial, e])), [index]);
  const barRef = useRef<HTMLDivElement>(null);
  // 正在滑选的字母（气泡预览 + 高亮）；null = 没在按压
  const [preview, setPreview] = useState<string | null>(null);
  // 是否处于按压/滑动中：pointermove 在鼠标悬停时也会触发，得靠它区分
  const dragging = useRef(false);

  /** 指尖纵坐标 → 档位字母：格子是 flex 等分的，按比例换算即可 */
  const letterAtY = useCallback((clientY: number) => {
    const rect = barRef.current?.getBoundingClientRect();
    if (!rect || rect.height === 0) return null;
    const ratio = (clientY - rect.top) / rect.height;
    const idx = Math.min(
      WALL_INITIALS.length - 1,
      Math.max(0, Math.floor(ratio * WALL_INITIALS.length)),
    );
    return WALL_INITIALS[idx];
  }, []);

  if (index.length === 0) return null;

  const previewEntry = preview ? byInitial.get(preview) : undefined;
  const previewIdx = preview ? WALL_INITIALS.indexOf(preview) : -1;

  return (
    <div
      ref={barRef}
      role="navigation"
      aria-label="按首字母跳转"
      className="sticky flex shrink-0 touch-none select-none flex-col items-stretch text-micro font-semibold leading-none text-[var(--text-faint)]"
      style={{
        height: WALL_INDEX_HEIGHT,
        // 吸附点 = 视口垂直居中；视口过矮时保底离顶 16px
        top: `max(16px, calc((100dvh - ${WALL_INDEX_HEIGHT}) / 2))`,
      }}
      onPointerDown={(e) => {
        // 阻掉按下的默认行为（文字选择/图片拖拽），并捕获指针：
        // 手指滑出条外仍持续收到 move/up，选择不中断
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        dragging.current = true;
        setPreview(letterAtY(e.clientY));
      }}
      onPointerMove={(e) => {
        if (dragging.current) setPreview(letterAtY(e.clientY));
      }}
      onPointerUp={(e) => {
        if (!dragging.current) return;
        dragging.current = false;
        const entry = byInitial.get(letterAtY(e.clientY) ?? "");
        if (entry) onJump(entry.offset);
        setPreview(null);
      }}
      onPointerCancel={() => {
        dragging.current = false;
        setPreview(null);
      }}
    >
      {WALL_INITIALS.map((initial) => {
        const entry = byInitial.get(initial);
        return (
          <span
            key={initial}
            title={entry ? `${initial} · ${entry.count} 部` : undefined}
            className={`flex min-h-0 w-[18px] flex-1 items-center justify-center rounded transition-colors max-md:w-5 ${
              !entry
                ? "text-white/15"
                : initial === preview
                  ? "bg-[var(--accent)]/40 text-white"
                  : initial === active
                    ? "bg-[var(--accent)]/25 text-white"
                    : "text-white/55 hover:bg-white/10 hover:text-white"
            }`}
          >
            {initial}
          </span>
        );
      })}
      {/* 滑选气泡：跟着指尖所在档位浮在条左侧，大字预览 + 该档作品数。
          指尖压着的字母本身被手指挡住，没有它滑选等于盲选 */}
      {preview && previewIdx >= 0 && (
        <div
          className="pointer-events-none absolute right-full mr-3 flex -translate-y-1/2 items-center gap-2 rounded-xl border border-white/[0.14] bg-[rgba(16,18,26,0.92)] px-3.5 py-2 shadow-[0_8px_28px_rgba(0,0,0,0.5)]"
          style={{ top: `${((previewIdx + 0.5) / WALL_INITIALS.length) * 100}%` }}
        >
          <span className="text-title-lg font-bold text-white">{preview}</span>
          <span className="whitespace-nowrap text-caption text-[var(--text-muted)]">
            {previewEntry ? `${previewEntry.count} 部` : "无作品"}
          </span>
        </div>
      )}
    </div>
  );
}

/** 追踪中格：订阅状态行沿用订阅页语言，点击进订阅详情。 */
function PendingCell({ sub }: { sub: Subscription }) {
  const meta = subscriptionStatusMeta[sub.status];
  const visual: PosterVisualItem = {
    id: String(sub.media.tmdb_id),
    source: "tmdb",
    title: sub.media.title,
    year: sub.media.year ?? undefined,
    rating: 0,
    posterUrl: sub.media.poster_url ? cachedImageUrl(sub.media.poster_url) : "",
  };
  return (
    // content-visibility 与库存格同款：追踪中的订阅一多（批量订阅季播剧），
    // 视口外的格子同样不该参与布局与绘制
    <div className="opacity-80 transition [contain-intrinsic-size:auto_270px] [content-visibility:auto] hover:opacity-100">
      {/* 已是订阅产物，悬浮层不再给「订阅影片」重复入口 */}
      <PosterCardVisual item={visual} href={`/subscriptions/${sub.id}` as Route} action="none" />
      <p className="text-on-image mt-1.5 flex items-center gap-1.5 truncate text-caption text-[var(--text-muted)]">
        <span
          className="size-1.5 shrink-0 rounded-full"
          style={{ backgroundColor: meta.color }}
        />
        <span className="truncate">
          {meta.label} · {subscriptionProgressNote(sub)}
        </span>
      </p>
    </div>
  );
}

/* —— 待处理抽屉：缺失 / 待识别 / 身份复核 / 已忽略，右侧滑出，海报墙不再被待办铺满 ——
   「已忽略」不是待办，是**已处理**的归档：默认不打扰，只在有内容时露出 tab，
   给识别器变强之后反悔的机会。 */

type IssueTab = "missing" | "unidentified" | "review" | "ignored";

function IssueDrawer({
  open,
  onClose,
  onSwitchTab,
  libraryId,
  missing,
  unidentified,
  review,
  ignored,
  movie,
  onChanged,
}: {
  open: IssueTab | null;
  onClose: () => void;
  onSwitchTab: (tab: IssueTab) => void;
  libraryId: number;
  missing: MissingItem[];
  unidentified: UnidentifiedGroup[];
  review: ReviewGroup[];
  ignored: UnidentifiedGroup[];
  movie: boolean;
  onChanged: () => void;
}) {
  const confirm = useConfirm();
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  // 打开/切 tab 时清空过滤词；Escape 关闭
  useEffect(() => {
    setQuery("");
  }, [open]);
  useEffect(() => {
    if (open === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (open === null || typeof document === "undefined") return null;

  const keyword = query.trim().toLowerCase();
  const missingShown = keyword
    ? missing.filter((m) => m.title.toLowerCase().includes(keyword))
    : missing;
  const unidentifiedShown = keyword
    ? unidentified.filter(
        (g) =>
          g.label.toLowerCase().includes(keyword) ||
          g.files.some((f) => f.file_path.toLowerCase().includes(keyword)),
      )
    : unidentified;
  const reviewShown = keyword
    ? review.filter(
        (g) =>
          g.label.toLowerCase().includes(keyword) ||
          g.current.title.toLowerCase().includes(keyword) ||
          g.suggestion.title.toLowerCase().includes(keyword),
      )
    : review;
  const ignoredShown = keyword
    ? ignored.filter(
        (g) =>
          g.label.toLowerCase().includes(keyword) ||
          g.files.some((f) => f.file_path.toLowerCase().includes(keyword)),
      )
    : ignored;
  const missingFileTotal = missing.reduce((n, item) => n + item.files.length, 0);
  const unidentifiedFileTotal = unidentified.reduce((n, g) => n + g.file_count, 0);
  const ignoredFileTotal = ignored.reduce((n, g) => n + g.file_count, 0);
  // 「放错库了」的文件数：认领解决不了（TMDB 的 movie/tv 是两套 id 空间），
  // 得挪文件或删库重建，单独给一条修复引导
  const kindMismatchFiles = unidentified.reduce(
    (n, g) => (g.code === "kind_mismatch" ? n + g.file_count : n),
    0,
  );

  const tabClass = (active: boolean) =>
    `rounded-full px-3.5 py-1.5 text-sub font-semibold transition ${
      active ? "bg-white/[0.14] text-white" : "text-[var(--text-muted)] hover:text-white"
    }`;

  return createPortal(
    // bottom 越出视口 --vp-overshoot：遮罩与右侧全高面板铺到屏幕物理底边
    // （iOS 独立 App 的视口矮一截，见 globals.css）；列表内容用加大的 pb 留在视口内
    <div
      className="fixed inset-0 z-50 [bottom:calc(-1*var(--vp-overshoot))]"
      role="dialog"
      aria-modal="true"
      aria-label="待处理"
    >
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-black/50 backdrop-blur-[2px]"
      />
      <div className="absolute right-0 top-0 flex h-full w-full max-w-[600px] flex-col border-l border-white/10 bg-[rgba(16,18,26,0.94)] shadow-[-24px_0_70px_rgba(0,0,0,0.55)] backdrop-blur-2xl max-md:max-w-none">
        {/* 头部：tab + 关闭 */}
        <div className="flex items-center justify-between gap-3 border-b border-white/[0.08] px-5 py-3.5">
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => onSwitchTab("missing")}
              className={tabClass(open === "missing")}
            >
              缺失 {missing.length > 0 ? missing.length : ""}
            </button>
            <button
              type="button"
              onClick={() => onSwitchTab("unidentified")}
              className={tabClass(open === "unidentified")}
            >
              待识别 {unidentifiedFileTotal > 0 ? unidentifiedFileTotal : ""}
            </button>
            <button
              type="button"
              onClick={() => onSwitchTab("review")}
              className={tabClass(open === "review")}
            >
              身份复核 {review.length > 0 ? review.length : ""}
            </button>
            {/* 已忽略是归档不是待办：没有内容就不占位，除非正停在这个 tab 上 */}
            {(ignoredFileTotal > 0 || open === "ignored") && (
              <button
                type="button"
                onClick={() => onSwitchTab("ignored")}
                className={tabClass(open === "ignored")}
              >
                已忽略 {ignoredFileTotal > 0 ? ignoredFileTotal : ""}
              </button>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭抽屉"
            className="btn-glass flex size-8 items-center justify-center !rounded-full"
          >
            <XIcon className="size-4" />
          </button>
        </div>

        {/* 工具行：过滤 + 批量操作 */}
        <div className="flex items-center gap-2.5 border-b border-white/[0.08] px-5 py-3">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={open === "missing" ? "按片名过滤…" : "按文件名过滤…"}
            className="min-w-0 flex-1 rounded-lg border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-sub text-[var(--text)] outline-none placeholder:text-white/30 focus:border-[var(--accent)]/60"
          />
          {open === "missing" && missing.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                if (
                  !(await confirm({
                    title: `清理全部 ${missingFileTotal} 条缺失记录？`,
                    description: "只删台账，不动磁盘。",
                    confirmLabel: "全部清理",
                    tone: "danger",
                  }))
                )
                  return;
                setBusy(true);
                void clearMissingLibraryRecords(libraryId)
                  .then(onChanged)
                  .catch(() => {})
                  .finally(() => setBusy(false));
              }}
              className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium disabled:opacity-50"
            >
              全部清理
            </button>
          )}
          {open === "unidentified" && unidentified.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                if (
                  !(await confirm({
                    title: `忽略全部 ${unidentifiedFileTotal} 个待识别文件？`,
                    description: "之后扫描不再过问它们（不动磁盘；可在「已忽略」里恢复）。",
                    confirmLabel: "全部忽略",
                    tone: "danger",
                  }))
                )
                  return;
                setBusy(true);
                void ignoreAllUnidentifiedLibraryFiles(libraryId)
                  .then(onChanged)
                  .catch(() => {})
                  .finally(() => setBusy(false));
              }}
              className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium disabled:opacity-50"
            >
              全部忽略
            </button>
          )}
        </div>

        {/* 说明行 */}
        <p className="px-5 pt-3 text-caption leading-5 text-[var(--text-muted)]">
          {open === "missing"
            ? "文件已不在磁盘；「重新下载」交给订阅管线补回，「清理记录」只删台账（都不动磁盘）；文件回归会自动恢复。经常自己删片子的话，可在库设置里打开「扫描后自动清理丢失记录」，以后扫完即对齐。"
            : open === "unidentified"
              ? "同一目录的文件聚成一组，认领一次整组生效；点候选可先核对海报简介再确认。状态标签悬停看完整原因。"
              : open === "review"
                ? "识别器升级后重新核对了这些条目，新结论与现有身份不一致。身份没有被自动改动——由你拍板：采纳新结论改挂条目，或维持现状。拍板后不再提醒。"
                : "这些文件你选择过「忽略」，之后每次扫描都会直接跳过，不再占用待识别清单（磁盘文件一直都在）。识别器在持续变强，当初认不出的现在未必认不出——「恢复」即可让它重新参与识别。"}
        </p>

        {/* 「放错库了」修复引导：认领解决不了这一类，不给引导用户会在认领里打转 */}
        {open === "unidentified" && kindMismatchFiles > 0 && (
          <p className="mx-5 mt-3 rounded-lg bg-[var(--warn)]/[0.1] px-3 py-2 text-caption leading-5 text-[var(--warn)]">
            有 {kindMismatchFiles} 个文件的实际类型与本库不符（
            {movie ? "剧集文件在电影库" : "电影文件在剧集库"}
            ），认领无法解决。个别文件放错了：把文件移到对应类型的库即可；整库类型建错了：
            删除本库并以正确类型重建（删库不会动磁盘文件），重新扫描即可恢复。
          </p>
        )}

        {/* 列表区：独立滚动。面板底边已铺到物理底边，滚动到底时最后一行
            要停在安全区与视口截差之上，不被 Home 指示条压住 */}
        <div className="scroll-thin min-h-0 flex-1 space-y-2 overflow-y-auto px-5 pt-4 pb-[calc(1rem+var(--safe-bottom)+var(--vp-overshoot))]">
          {open === "missing" &&
            missingShown.map((item) => (
              <MissingRow
                key={item.media_item_id}
                libraryId={libraryId}
                item={item}
                onChanged={onChanged}
              />
            ))}
          {open === "unidentified" &&
            unidentifiedShown.map((group) => (
              <UnidentifiedGroupRow
                key={group.key}
                group={group}
                movie={movie}
                onChanged={onChanged}
              />
            ))}
          {open === "review" &&
            reviewShown.map((group) => (
              <ReviewGroupRow key={group.key} group={group} onChanged={onChanged} />
            ))}
          {open === "ignored" &&
            ignoredShown.map((group) => (
              <IgnoredGroupRow key={group.key} group={group} onChanged={onChanged} />
            ))}
          {((open === "missing" && missingShown.length === 0) ||
            (open === "unidentified" && unidentifiedShown.length === 0) ||
            (open === "review" && reviewShown.length === 0) ||
            (open === "ignored" && ignoredShown.length === 0)) && (
            <p className="mt-12 text-center text-ui text-[var(--text-muted)]">
              {keyword
                ? "没有匹配的条目"
                : open === "ignored"
                  ? "没有忽略过的文件"
                  : "没有需要处理的了 🎉"}
            </p>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function MissingRow({
  libraryId,
  item,
  onChanged,
}: {
  libraryId: number;
  item: MissingItem;
  onChanged: () => void;
}) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const act = (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    void fn()
      .then(onChanged)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  // 剧集：按季聚合缺失集数做摘要；电影：单文件
  const summary =
    item.kind === "tv"
      ? Array.from(
          item.files.reduce((m, f) => {
            m.set(f.season_number, (m.get(f.season_number) ?? 0) + 1);
            return m;
          }, new Map<number, number>()),
        )
          .sort(([a], [b]) => a - b)
          .map(([s, n]) => (s === 0 ? `特别篇 ${n} 集` : `第 ${s} 季缺 ${n} 集`))
          .join("、")
      : `${item.files.length} 个文件`;

  return (
    <div className="rounded-xl bg-white/[0.03] px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="min-w-0 flex-1 truncate text-ui font-medium text-white/85">
          {item.title}
          {item.year ? ` (${item.year})` : ""}
          <span className="ml-2 text-sub font-normal text-[var(--text-muted)]">{summary}</span>
        </span>
        {done ? (
          <span className="text-sub text-[var(--ok)]">{done}</span>
        ) : (
          <span className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                act(() =>
                  redownloadMissing(libraryId, item.media_item_id).then(({ requeued }) => {
                    setDone(`已交给订阅管线（${requeued} 个工单排队）`);
                  }),
                )
              }
              className="btn-accent rounded-full px-3 py-1.5 text-sub font-semibold disabled:opacity-50"
            >
              重新下载
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                const ok = await confirm({
                  title: `清理「${item.title}」的 ${item.files.length} 条缺失记录？`,
                  description: item.subscription_id
                    ? "该条目有正在追踪的订阅，只清记录的话订阅可能把它重新下回来。（只删台账，不动磁盘）"
                    : "只删台账，不动磁盘。",
                  confirmLabel: "清理记录",
                  tone: "danger",
                });
                if (!ok) return;
                act(() => clearMissingLibraryRecords(libraryId, item.media_item_id));
              }}
              className="btn-glass px-3 py-1.5 text-sub font-medium disabled:opacity-50"
            >
              清理记录
            </button>
          </span>
        )}
      </div>
      {item.subscription_id && !done && (
        <p className="mt-1 text-caption text-[var(--warn)]/80">该条目有订阅在追踪</p>
      )}
      {error && <p className="mt-1 text-caption text-red-300">{error}</p>}
    </div>
  );
}

/* —— 待识别组：整组认领或整组忽略 ——
   一部剧几十集是同一件事，逐集处理既刷屏又折磨人。认领一律走「详情确认
   面板」：点候选/搜索结果先看海报、简介、季数再确认——只看名字不足以在
   同名双版本（正片 46 集 vs 送审版 51 集）之间下判断。候选全不对时走
   TMDB 搜索（按片名检索、支持直接粘 ID），不再让用户手抄 TMDB ID。
   TMDB 真没有条目（no_match）也不是死路：卡片上写明「扫描会自动重试」
   并引导去 TMDB 社区补录——补录收录后回来搜索认领即可。 */

/* 失败分类 → 标签：整句原因塞进 title，清单上只留一个能扫读的短标签。
   配色只分两级——**黄色专供"系统故障、重扫可自愈"**（TMDB 不可达），
   它有确定解法、不需要用户动脑；其余是正常待办不是错误，用中性色，
   否则满屏黄字等于没有信号。 */
const UNIDENTIFIED_BADGE: Record<
  string,
  { label: (g: UnidentifiedGroup) => string; tone: "warn" | "muted" }
> = {
  tmdb_unreachable: { label: () => "TMDB 不可达", tone: "warn" },
  // 库类型选错也是"有确定解法但需要动手"的一类：认领单个文件没用，得换库
  kind_mismatch: { label: () => "放错库了", tone: "warn" },
  ambiguous: { label: (g) => `${g.candidates.length} 个候选待定`, tone: "muted" },
  no_match: { label: () => "TMDB 无匹配", tone: "muted" },
  unparsable: { label: () => "认不出片名", tone: "muted" },
};

/** 组卡片的次要动作（忽略 / 查看文件）：收进 ⋯，不跟主动作抢视线。 */
function GroupMoreMenu({
  busy,
  expanded,
  canExpand,
  onIgnore,
  onToggleFiles,
}: {
  busy: boolean;
  expanded: boolean;
  canExpand: boolean;
  onIgnore: () => void;
  onToggleFiles: () => void;
}) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="更多操作"
          // 常显但压暗：纯 hover 显示的控件在触屏上根本点不到（NAS 常用平板访问）
          className="btn-glass shrink-0 !rounded-full px-1.5 py-1 opacity-45 transition hover:opacity-100 group-hover/card:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
        >
          <MoreIcon className="size-4" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          // Escape 只关菜单：Radix 在 document 上处理，不拦住就会继续冒泡到
          // 抽屉挂在 window 的监听，把整个抽屉一起关掉（菜单照常关闭——
          // stopPropagation 不影响 Radix 自己的 dismiss）
          onEscapeKeyDown={(e) => e.stopPropagation()}
          className="menu-surface z-[60] min-w-[9rem] !rounded-xl p-1"
        >
          {canExpand && (
            <DropdownMenu.Item onSelect={onToggleFiles} className={itemClass}>
              {expanded ? "收起文件" : "查看文件"}
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item onSelect={onIgnore} disabled={busy} className={itemClass}>
            忽略
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function StatusBadge({ group }: { group: UnidentifiedGroup }) {
  // 旧数据没有 code：退回按有无候选粗分，不至于空着
  const meta =
    (group.code && UNIDENTIFIED_BADGE[group.code]) ??
    (group.candidates.length > 0
      ? UNIDENTIFIED_BADGE.ambiguous
      : UNIDENTIFIED_BADGE.no_match);
  return (
    <span
      title={group.reason ?? undefined}
      className={`shrink-0 rounded-md px-1.5 py-0.5 text-caption ${
        meta.tone === "warn"
          ? "bg-[var(--warn)]/[0.16] text-[var(--warn)]"
          : "bg-white/[0.07] text-[var(--text-muted)]"
      }`}
    >
      {meta.label(group)}
    </span>
  );
}

function UnidentifiedGroupRow({
  group,
  movie,
  onChanged,
}: {
  group: UnidentifiedGroup;
  movie: boolean;
  onChanged: () => void;
}) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  // 卡片内联面板：确认（点候选/搜索结果后看详情再定）或搜索；同时只开一个
  const [panel, setPanel] = useState<
    { view: "confirm"; seed: ClaimSeed } | { view: "search" } | null
  >(null);

  const fileIds = group.files.map((f) => f.id);
  const act = (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    void fn()
      .then(onChanged)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  const selectedId = panel?.view === "confirm" ? panel.seed.tmdbId : null;
  const unreachable = group.code === "tmdb_unreachable";
  const searchOpen = panel?.view === "search";

  const ignoreGroup = async () => {
    if (
      group.file_count > 1 &&
      !(await confirm({
        title: `忽略「${group.label}」的全部 ${group.file_count} 个文件？`,
        description:
          "之后扫描不再过问（不动磁盘；可在「已忽略」里恢复）。适合自录、花絮这类 TMDB 本就不会有条目的内容——只是暂时认不出的建议先搜索认领。",
        confirmLabel: "全部忽略",
        tone: "danger",
      }))
    )
      return;
    act(() => Promise.all(fileIds.map((id) => ignoreUnidentifiedLibraryFile(id))));
  };

  return (
    <div className="group/card rounded-xl bg-white/[0.03] px-3.5 py-2.5">
      {/* 头行一行说完"是谁、什么状态、多大、能做什么"：整句原因收进标签的
          title，次要动作收进 ⋯ 菜单——组多起来时这一行就是全部扫读内容 */}
      <div className="flex items-center gap-2">
        <span
          className="min-w-0 flex-1 truncate text-ui font-medium text-white/85"
          title={`${group.label}\n${group.key}`}
        >
          {group.label}
        </span>
        <StatusBadge group={group} />
        <span className="shrink-0 text-caption text-[var(--text-faint)]">
          {group.file_count} 个 · {formatBytes(group.total_size_bytes)}
        </span>
        {/* TMDB 不可达有确定解法，直接给按钮；其余给搜索入口 */}
        {unreachable ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => act(() => startLibraryScan(group.library_id))}
            className="btn-glass shrink-0 px-2.5 py-1 text-sub font-medium disabled:opacity-40"
          >
            重新扫描
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => setPanel((p) => (p?.view === "search" ? null : { view: "search" }))}
            className={`shrink-0 rounded-full px-2.5 py-1 text-sub font-medium transition disabled:opacity-40 ${
              searchOpen ? "bg-white/[0.14] text-white" : "btn-glass"
            }`}
          >
            搜索
          </button>
        )}
        <GroupMoreMenu
          busy={busy}
          expanded={expanded}
          canExpand={group.file_count > 1}
          onIgnore={() => void ignoreGroup()}
          onToggleFiles={() => setExpanded((v) => !v)}
        />
      </div>

      {/* no_match 的两条真实出口写在卡片上：条目常常是晚几周才被社区补上
          （待识别行每次扫描本就自动重试），等不及可以自己去补录。不写清楚，
          用户会以为搜不到就只剩忽略一条路 */}
      {group.code === "no_match" && (
        <p className="mt-1 text-caption leading-relaxed text-[var(--text-faint)]">
          每次扫描会自动重试识别。TMDB 是社区维护的数据库，确认缺这个条目可以
          <a
            href={`https://www.themoviedb.org/${movie ? "movie" : "tv"}/new`}
            target="_blank"
            rel="noreferrer"
            className="underline decoration-white/20 underline-offset-2 transition hover:text-white/80"
          >
            去 TMDB 补录 ↗
          </a>
          ，收录后回来搜索认领；自录、花絮这类本就不会有条目的内容用「忽略」即可。
        </p>
      )}

      {/* 候选：机器给出的可能匹配。点击先进详情确认面板，不直接认领 */}
      {group.candidates.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {group.candidates.map((c) => (
            <button
              key={c.tmdb_id}
              type="button"
              disabled={busy}
              onClick={() =>
                setPanel(
                  selectedId === c.tmdb_id
                    ? null
                    : {
                        view: "confirm",
                        seed: {
                          tmdbId: c.tmdb_id,
                          title: c.title,
                          year: c.year,
                          episodeCount: c.episode_count,
                          reasons: c.reasons,
                        },
                      },
                )
              }
              className={`rounded-lg border px-2.5 py-1 text-sub transition disabled:opacity-40 ${
                selectedId === c.tmdb_id
                  ? "border-[var(--accent)] bg-[var(--accent)]/[0.28] text-white"
                  : "border-[var(--accent)]/40 bg-[var(--accent)]/[0.12] text-white/90 hover:bg-[var(--accent)]/[0.24]"
              }`}
            >
              {c.title}
              {c.year ? ` (${c.year})` : ""}
              {c.episode_count ? (
                <span className="ml-1.5 text-caption text-[var(--text-muted)]">
                  {c.episode_count} 集
                </span>
              ) : null}
            </button>
          ))}
        </div>
      )}

      {error && <p className="mt-1.5 text-caption text-red-300">{error}</p>}

      {panel?.view === "search" && (
        <ClaimSearchPanel
          movie={movie}
          initialQuery={searchSeedFromLabel(group.label)}
          onPick={(seed) => setPanel({ view: "confirm", seed })}
        />
      )}
      {panel?.view === "confirm" && (
        <ClaimConfirmPanel
          key={panel.seed.tmdbId}
          seed={panel.seed}
          movie={movie}
          fileCount={group.file_count}
          busy={busy}
          onConfirm={() =>
            act(() =>
              assignLibraryFilesToTitle(
                fileIds,
                `tmdb:${movie ? "movie" : "tv"}:${panel.seed.tmdbId}`,
              ),
            )
          }
          onCancel={() => setPanel(null)}
        />
      )}

      {expanded && (
        <ul className="mt-2 space-y-1 border-t border-white/[0.06] pt-2">
          {group.files.map((f) => (
            <li
              key={f.id}
              className="truncate font-mono text-caption text-[var(--text-faint)]"
              title={f.file_path}
            >
              {f.file_path}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* —— 身份复核组行：现身份 vs 建议身份并排对比，采纳或维持一键拍板 —— */

function ReviewGroupRow({ group, onChanged }: { group: ReviewGroup; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = (accept: boolean) => {
    setBusy(true);
    setError(null);
    void resolveLibraryIdentityReview(
      group.file_ids,
      accept ? "accept_suggestion" : "keep_current",
    )
      .then(onChanged)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  const side = (info: ReviewGroup["current"], tone: "current" | "suggestion") => (
    <div className="flex min-w-0 flex-1 items-center gap-2.5">
      {info.poster_url ? (
        <img
          src={cachedImageUrl(info.poster_url)}
          alt=""
          loading="lazy"
          decoding="async"
          className="h-[54px] w-9 shrink-0 rounded-md object-cover"
        />
      ) : (
        <div className="h-[54px] w-9 shrink-0 rounded-md bg-white/[0.06]" />
      )}
      <div className="min-w-0">
        <p className="text-micro font-semibold uppercase tracking-wide text-[var(--text-faint)]">
          {tone === "current" ? "现身份" : "新识别结论"}
        </p>
        <p
          className={`truncate text-ui font-medium ${
            tone === "suggestion" ? "text-[#c4b5fd]" : "text-white/85"
          }`}
          title={info.title}
        >
          {info.title}
          {info.year ? ` (${info.year})` : ""}
        </p>
        {info.tmdb_id != null && (
          <p className="text-caption text-[var(--text-faint)]">tmdb {info.tmdb_id}</p>
        )}
      </div>
    </div>
  );

  return (
    <div className="rounded-xl bg-white/[0.03] px-3.5 py-3">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-ui font-medium text-white/85" title={group.key}>
          {group.label}
        </span>
        <span className="shrink-0 text-caption text-[var(--text-faint)]">
          {group.file_count} 个文件 · {formatBytes(group.total_size_bytes)}
        </span>
      </div>

      {/* 两个身份并排：看清是谁 vs 该是谁 */}
      <div className="mt-2.5 flex items-center gap-3">
        {side(group.current, "current")}
        <span className="shrink-0 text-body-lg text-[var(--text-faint)]">→</span>
        {side(group.suggestion, "suggestion")}
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => act(true)}
          className="rounded-full border border-[#c4b5fd]/45 bg-[#c4b5fd]/[0.14] px-3.5 py-1.5 text-sub font-semibold text-[#c4b5fd] transition hover:bg-[#c4b5fd]/[0.26] disabled:opacity-40"
        >
          采纳新结论
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => act(false)}
          className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-40"
        >
          维持现状
        </button>
        {error && <span className="text-caption text-red-300">{error}</span>}
      </div>
    </div>
  );
}

/* —— 已忽略组：只有一个动作「恢复」——归档视图不该再摆认领/搜索那一套 —— */

function IgnoredGroupRow({
  group,
  onChanged,
}: {
  group: UnidentifiedGroup;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-xl bg-white/[0.02] px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span
          className="min-w-0 flex-1 truncate text-ui text-white/70"
          title={group.key}
        >
          {group.label}
        </span>
        <span className="shrink-0 text-caption text-[var(--text-faint)]">
          {group.file_count} 个文件 · {formatBytes(group.total_size_bytes)}
        </span>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            setError(null);
            void restoreIgnoredLibraryFiles(group.files.map((f) => f.id))
              .then(onChanged)
              .catch((e) => setError((e as Error).message))
              .finally(() => setBusy(false));
          }}
          className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium disabled:opacity-40"
        >
          恢复
        </button>
        {group.file_count > 1 && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-caption text-[var(--text-muted)] transition hover:text-white/80"
          >
            {expanded ? "收起文件" : "查看文件"}
          </button>
        )}
      </div>
      {error && <p className="mt-1 text-caption text-red-300">{error}</p>}
      {expanded && (
        <ul className="mt-2 space-y-1 border-t border-white/[0.06] pt-2">
          {group.files.map((f) => (
            <li
              key={f.id}
              className="truncate font-mono text-caption text-[var(--text-faint)]"
              title={f.file_path}
            >
              {f.file_path}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CenteredNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-16 flex flex-col items-center gap-3 text-center">{children}</div>
  );
}
