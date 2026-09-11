"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";
import Link from "next/link";

import { useConfirm, useToast } from "@/components/feedback";
import {
  chapterImagesConfirm,
  refreshLibraryConfirm,
  rereadLibraryNfoConfirm,
  scanLibraryConfirm,
} from "@/lib/library-confirm";
import { chapterJobLabel } from "@/lib/library-manage";
import {
  LockIcon,
  MoreIcon,
  XIcon,
} from "@/components/icons";
import {
  FilterEmptyState,
  LibraryFilterBar,
  WallSortControl,
} from "@/components/library-filter-bar";
import { LibraryCollectionsView } from "@/components/library-collections-view";
import { SaveAsCollectionDialog } from "@/components/save-as-collection-dialog";
import { listCollections, type Collection } from "@/lib/api/collections";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { usePageTitle } from "@/lib/use-page-title";
import { useIsMobile } from "@/lib/use-media-query";
import { LibraryFormDialog } from "@/components/library-form-dialog";
import { LIBRARY_KIND_META } from "@/components/library-kind-meta";
import { LibraryOrganizeDialog } from "@/components/library-organize-dialog";
import { PhotoLightbox } from "@/components/photo-lightbox";
import {
  PhotoTimelineScrubber,
  PhotoWall,
  remeasureWalls,
  usePhotoWallDensity,
  type PhotoWallDensity,
} from "@/components/photo-wall";
import { WallLoadMore, WallLoadPrev, WallRecallPill } from "@/components/wall-chrome";
import { InventoryCell, PosterWall, WALL_GRID_WIDE } from "@/components/poster-wall";
import {
  GALLERY_LOAD_MARGIN,
  GALLERY_PAGE_SIZE,
  dedupeGalleryGroups,
  VideoGalleryLightbox,
  VideoGalleryWall,
  WallPrefItems,
  flattenGallery,
  useVideoGalleryGrouped,
  useVideoGalleryMode,
} from "@/components/video-gallery";
import {
  type ChapterJobProgress,
  type LibraryCapabilities,
  type LibraryGalleryGroup,
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
  listLibraryGallery,
  listLibraryItemIndex,
  type LibraryFilter,
  type WatchFilter,
  filterKey,
  filterQuery,
  isFilterEmpty,
  listLibraryItems,
  type LibraryIndexEntry,
  type LibraryItemOrder,
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
  startLibraryChapterImages,
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
import { setPlaybackMarks } from "@/lib/api/playback";
import { HttpError } from "@/lib/http";
import { formatBytes } from "@/lib/format";
import { activeWallInitialAtViewport, wallInitialAtOffset } from "@/lib/library-wall-index";
import { activeInitialAt } from "@/lib/wall-window";
import {
  firstVisibleAnchorId,
  isReentryAfterAbsence,
  wallRecallScope,
} from "@/lib/library-wall-recall";
import { useWallRecall } from "@/lib/use-wall-recall";
import { formatRelativeTime } from "@/lib/time";
import { cachedImageUrl } from "@/lib/image-proxy";
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
 * 两个分区：
 * 1. 库存（library_file 台账聚合）：已在磁盘上的作品，格下标注集数/规格/大小；
 * 2. 待识别：扫描认不出身份的文件，按条目目录成组，点候选或填 TMDB ID 整组认领。
 *
 * 不再有「追踪中」分区（订阅了、文件还没落地的片）：它不受筛选与搜索约束，永远
 * 钉在墙顶，搜什么都先看到一排无关的片。订阅进度去订阅页看；系列合集里缺的那几部
 * 另有「追踪中」标注。
 */

/** 海报墙每次向服务端要的格数（首屏一批，滚到底再追加一批）。 */
/** 未识别分区一次拉取的上限：它不分页，超出的去待处理清单看 */
const PROVISIONAL_LIMIT = 200;
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

/**
 * 墙的排序偏好：`default` = 按库的形态给的常驻序（影视库拼音序、其他库与图片库
 * 按内容时间倒序），`added_at` = 最近添加优先。
 *
 * 为什么常驻序不是「最近添加」：首次建库/批量导入时全库的入账时间挤在同一
 * 分钟里，倒序出来其实是目录遍历顺序，等于没排；而按标题能分出 A-Z 档、按
 * 内容时间能分出月份档，右侧的跳转轨道靠的就是这个。所以「最近添加」是持续
 * 往库里添内容的人**自己选**的一档，不做默认。
 */
type WallSortPref =
  | "default"
  | "added_at"
  | "release_date"
  | "rating"
  | "runtime"
  | "size"
  | "last_played";

/** 偏好 → 服务端排序键。`default` 由库的形态决定（影视库拼音序、其他库按时间）。 */
const PREF_TO_SORT: Record<Exclude<WallSortPref, "default">, LibraryItemSort> = {
  added_at: "added_at",
  release_date: "release_date",
  rating: "rating",
  runtime: "runtime",
  size: "size",
  last_played: "last_played",
};

/**
 * 每档的自然方向与方向的人话（2026-09-11 起排序可切换正倒序）。
 *
 * 不反转时请求不带 order，服务端按自然方向排——与加方向之前逐字相同。方向写成
 * 人话（「短→长」）而不是只画箭头：↑ 到底是"从小到大"还是"大的在上"，光看箭头
 * 要想一下。补探序是扫描临时接管的，控件那几分钟本来就是灰的，方向无意义
 */
const SORT_DIRECTIONS: Record<LibraryItemSort, { naturalAsc: boolean; asc: string; desc: string }> = {
  title: { naturalAsc: true, asc: "A→Z", desc: "Z→A" },
  added_at: { naturalAsc: false, asc: "旧→新", desc: "新→旧" },
  release_date: { naturalAsc: false, asc: "旧→新", desc: "新→旧" },
  probing: { naturalAsc: true, asc: "A→Z", desc: "Z→A" },
  rating: { naturalAsc: false, asc: "低→高", desc: "高→低" },
  runtime: { naturalAsc: true, asc: "短→长", desc: "长→短" },
  size: { naturalAsc: false, asc: "小→大", desc: "大→小" },
  last_played: { naturalAsc: false, asc: "远→近", desc: "近→远" },
};

/**
 * 排序档位与展示值。
 *
 * 去掉了「排序」这个前缀标签——前提是让值自述：默认档不叫「默认」，而是
 * 按库的形态叫「按标题」/「按时间」（本来就有 defaultSortLabel 这套叫法）。
 * 标签能删的前提是值自己会说话，不是硬删。
 *
 * 图廊（图床浏览）只吃服务端的 title / added_at 两档，所以那个形态下只给两档
 * ——把点了不生效的档摆出来，比少几档更伤。
 */
function sortOptions(
  defaultLabel: string,
  gallery: boolean,
  timeline: boolean,
): readonly (readonly [WallSortPref, string])[] {
  const base = [
    ["default", defaultLabel],
    // 一次导入的内容入账时间都挤在一起，所以这一档对"陆续往库里添东西"才有意义
    ["added_at", "最近添加"],
  ] as [WallSortPref, string][];
  if (gallery) return base;
  return [
    ...base,
    // 影视库补上「按上映时间」：老片→新片 / 新片→老片 是正倒序最用得上的一档。
    // 其他库的默认档本来就是按时间，不重复摆
    ...(timeline ? [] : ([["release_date", "按上映时间"]] as [WallSortPref, string][])),
    ["rating", "按评分"],
    ["runtime", "按片长"],
    ["size", "按体积"],
    ["last_played", "最近观看"],
  ];
}

/**
 * 从地址栏读筛选条件。
 *
 * **URL 是筛选态的唯一事实源**，筛选不进 localStorage——上次筛的条件在下次
 * 打开时还在，是本类产品最经典的困惑来源（"我的片怎么少了一半"）。
 * 排序是偏好该记，筛选是意图不该记（docs/design/library-filtering.md 3.4）。
 * 读 window.location 而不是 useSearchParams，与本文件既有惯例一致。
 */
function readFilterFromUrl(): LibraryFilter {
  if (typeof window === "undefined") return {};
  const params = new URLSearchParams(window.location.search);
  const list = (key: string) => (params.get(key) ?? "").split(",").filter(Boolean);
  const watch = params.get("w");
  const rating = params.get("rating_gte");
  const hdr = params.get("hdr");
  return {
    genres: list("g")
      .map(Number)
      .filter((n) => Number.isFinite(n)),
    countries: list("c"),
    decades: list("d"),
    watch: watch ? (watch as WatchFilter) : null,
    ratingGte: rating !== null && Number.isFinite(Number(rating)) ? Number(rating) : null,
    runtimes: list("rt"),
    languages: list("lang"),
    resolutions: list("res"),
    hdr: hdr === null ? null : hdr === "true",
    stock: list("stock"),
  };
}

/** 把筛选条件写回地址栏（replaceState：筛选不该在浏览器历史里堆一串条目）。 */
function writeFilterToUrl(filter: LibraryFilter): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  for (const key of ["g", "c", "d", "w", "rating_gte", "rt", "lang", "res", "hdr", "stock"])
    params.delete(key);
  filterQuery(filter, params);
  const rest = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
}

/** 库内的两个视图：作品（海报墙）/ 合集（纵向网格）。视图态同样只认地址栏。 */
type LibraryView = "items" | "collections";

function readViewFromUrl(): LibraryView {
  if (typeof window === "undefined") return "items";
  return new URLSearchParams(window.location.search).get("view") === "collections"
    ? "collections"
    : "items";
}

/** 视图切换写回地址栏（同筛选：replaceState，不在历史里堆条目）。 */
function writeViewToUrl(view: LibraryView): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  if (view === "collections") params.set("view", "collections");
  else params.delete("view");
  const rest = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
}

/**
 * 哪些排序要请求索引：按标题（A-Z 索引条）与按内容时间（其他库 / 图片库的月份时间线）。
 *
 * 评分档不再请求：影视库按评分、按上映时间都不显示侧边索引条（2026-09-11 试过画成
 * 「9+ / 8+ / 未评分」「2010s」分档后撤掉）——只有几档、档名又宽，一条宽索引条挤掉
 * 海报列宽，却只省下几屏滚动
 */
const INDEXED_SORTS: Partial<Record<LibraryItemSort, "title" | "release_date">> = {
  title: "title",
  probing: "title",
  release_date: "release_date",
};
const WALL_SORT_STORAGE_KEY = "movieclaw.library.wall-sort";

/** 能从 storage 里认回来的排序偏好；其余一律当默认序（防老版本或手改出来的脏值） */
const WALL_SORT_PREFS: readonly WallSortPref[] = [
  "default",
  "added_at",
  "release_date",
  "rating",
  "runtime",
  "size",
  "last_played",
];

/** 排序偏好：选的哪一档，以及是否反转了这一档的自然方向。 */
interface WallSortState {
  pref: WallSortPref;
  reversed: boolean;
}

/**
 * 读写排序偏好（含方向）。第四个返回值是「读完 storage 了没有」：首帧一律先给默认值
 * （服务端渲染没有 localStorage），排序相关的副作用必须等它为真再动手——
 * 否则从详情页返回的那一帧会先按默认序把窗口重拉一遍，把人甩回墙首。
 *
 * 存成 `rating` / `rating:rev`。此前只认回「最近添加」一档：选了按评分、按片长，
 * 刷新一下就退回默认序——排序是偏好，该记全。换档时方向回到新档的自然方向：
 * 从「片长 长→短」换到「评分」，用户要的是"高分在前"，不是继承一个反向
 */
function useWallSortPref(): [WallSortState, (next: WallSortPref) => void, () => void, boolean] {
  const [state, setState] = useState<WallSortState>({ pref: "default", reversed: false });
  const [ready, setReady] = useState(false);
  const stateRef = useRef(state);
  stateRef.current = state;
  useEffect(() => {
    try {
      const [pref, flag] = (window.localStorage.getItem(WALL_SORT_STORAGE_KEY) ?? "").split(":");
      if (WALL_SORT_PREFS.includes(pref as WallSortPref)) {
        setState({ pref: pref as WallSortPref, reversed: flag === "rev" });
      }
    } catch {
      /* 隐私模式等拿不到 storage：保持默认序 */
    }
    setReady(true);
  }, []);
  const persist = useCallback((next: WallSortState) => {
    setState(next);
    try {
      window.localStorage.setItem(
        WALL_SORT_STORAGE_KEY,
        next.reversed ? `${next.pref}:rev` : next.pref,
      );
    } catch {
      /* 同上 */
    }
  }, []);
  const update = useCallback((pref: WallSortPref) => persist({ pref, reversed: false }), [persist]);
  const toggleReversed = useCallback(
    () => persist({ ...stateRef.current, reversed: !stateRef.current.reversed }),
    [persist],
  );
  return [state, update, toggleReversed, ready];
}

export function LibraryDetailView({ libraryId }: { libraryId: number }) {
  const initialSnapshot = getLibraryDetailSnapshot(libraryId);
  // 本次是不是「重新进入」：首帧没有会话快照 = 冷启动 / 刷新 / 换了库进来的；
  // 或者本次页面加载期间挂过很久后台（iOS PWA 恢复应用不重新加载页面，只能
  // 靠这条认），且那段后台发生在离开这面墙之后。必须在首帧定格——本组件挂载
  // 后自己就会写快照，之后每次 render 都读得到它。重新进入时不自动回位，
  // 改由底部胶囊来问（两者同时来会变成「指着脚下那一格」的废话）
  const freshEntry = useRef(
    initialSnapshot === undefined || isReentryAfterAbsence(wallRecallScope(libraryId)),
  );
  const { canManageLibraries } = usePermissions();
  const { activeJobs } = useJobs();
  // 影视库 / 其他库的图床浏览模式（video-gallery.tsx）：海报墙换成每部作品的
  // 海报 / 剧照 / 章节图瀑布流，点开灯箱能直接播放或进详情。偏好记在浏览器里；
  // 图片库本身就是相册墙，这个开关对它没有意义
  const [galleryPreferred, setGalleryMode] = useVideoGalleryMode();
  // 两种墙的锚点属性不同：海报墙一格一个条目（data-library-item-id），图廊一部
  // 作品十几张图，得按瓦片认（data-gallery-tile-id）。两者不会同时在 DOM 里
  const restoreScrollRef = useScrollRestoration(`library:${libraryId}`, {
    anchorAttribute: galleryPreferred ? "data-gallery-tile-id" : "data-library-item-id",
    restore: !freshEntry.current,
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
  const toast = useToast();
  // 筛选态：首帧就从 URL 读，这样分享出去的链接一进来就是筛好的
  const [filter, setFilter] = useState<LibraryFilter>(() => readFilterFromUrl());
  // 同 wallSort：放 ref 供 reload/翻页每轮读最新值，不必为改条件重建回调链
  const wallFilter = useRef<LibraryFilter>(filter);
  wallFilter.current = filter;
  const filtering = !isFilterEmpty(filter);
  // 本库的合集：chip 行与「合集」视图共用这一份，不各拉各的
  const [collections, setCollections] = useState<Collection[]>([]);
  // 「显示已隐藏的合集」：自动生成的合集删不掉、只能藏，藏了必须找得回来。
  // 不进 URL、不落盘——它是一次性的"我来找找刚才藏的那个"，不是长期偏好
  const [showHiddenCollections, setShowHiddenCollections] = useState(false);
  const [libraryView, setLibraryView] = useState<LibraryView>(() => readViewFromUrl());
  const [savingCollection, setSavingCollection] = useState(false);
  const isMobile = useIsMobile();
  // 带筛选进来时不吃会话快照：快照是未筛选那面墙的窗口，拿它铺首帧会先闪
  // 一屏不该出现的内容，随即被 reload 的结果整片替换
  const snapshot = filtering ? undefined : initialSnapshot;
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(snapshot?.libraries ?? null);
  const [items, setItems] = useState<LibraryItem[]>(snapshot?.items ?? []);
  // 影视库里认不出、按文件名临时挂着的条目：不进主墙，单独一段展示（其他库恒空）
  const [provisional, setProvisional] = useState<LibraryItem[]>([]);
  // 服务端还有没有下一页；滚动加载的哨兵据此决定是否继续观察
  const [wallHasMore, setWallHasMore] = useState(snapshot?.wallHasMore ?? false);
  // A-Z 索引条的分档（按标题排序时才有意义）
  const [wallIndex, setWallIndex] = useState<LibraryIndexEntry[]>(snapshot?.wallIndex ?? []);
  // 当前窗口在整份排序里的起点：0 = 从头开始；点字母跳转后是该档的 offset。
  // 轮询要按这个起点重拉，否则每 3 秒把用户拽回墙首
  const [wallStart, setWallStart] = useState(snapshot?.wallStart ?? 0);
  // 与分页窗口起点分离：wallStart 只决定服务端从哪里取；活动字母要跟随
  // 用户在已加载窗口里的真实滚动位置。
  const [activeWallInitial, setActiveWallInitial] = useState<string | null>(null);
  const [unidentified, setUnidentified] = useState<UnidentifiedGroup[]>(snapshot?.unidentified ?? []);
  const [review, setReview] = useState<ReviewGroup[]>(snapshot?.review ?? []);
  const [ignored, setIgnored] = useState<UnidentifiedGroup[]>(snapshot?.ignored ?? []);
  const [missing, setMissing] = useState<MissingItem[]>(snapshot?.missing ?? []);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<MediaLibrary | null>(null);
  // 整理文件名对话框的目标库；null = 关闭
  const [organizeTarget, setOrganizeTarget] = useState<MediaLibrary | null>(null);
  // 整库元数据刷新进度（进行中每 3 秒轮询，结束自动刷新库存）
  const [metaRefresh, setMetaRefresh] = useState<MetadataRefreshProgress | null>(
    snapshot?.metaRefresh ?? null,
  );
  // 删除/转移等详情页操作会把旧窗口标记为过期。先保留它供滚动锚点落脚，
  // 后台对账成功后再清除标记；请求失败时下次返回仍会重试。
  const [snapshotStale, setSnapshotStale] = useState(snapshot?.stale ?? false);
  // 待处理抽屉：从哪个入口点进来就落在哪个 tab；null = 关闭
  const [issueTab, setIssueTab] = useState<IssueTab | null>(null);
  // 管理页「待处理」跳过来带 ?pending=1：首轮数据到齐后自动打开抽屉。
  // 读 location 而不是 useSearchParams（全站惯例），读完即抹掉参数，刷新不再弹
  const pendingRequested = useRef(false);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("pending") !== "1") return;
    pendingRequested.current = true;
    params.delete("pending");
    const rest = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
  }, []);

  // 图床浏览模式已加载的窗口（用法见下面「图床浏览模式」一段）。与海报墙的
  // 窗口一样随快照恢复，声明放在这里是因为下面的快照组装要读它们
  const [galleryGroups, setGalleryGroups] = useState<LibraryGalleryGroup[]>(
    snapshot?.galleryGroups ?? [],
  );
  const [galleryHasMore, setGalleryHasMore] = useState(snapshot?.galleryHasMore ?? false);
  // 当前图廊窗口在整份排序里的起点（0 = 墙首；「回到上次位置」跳过来后不为 0）。
  // 与海报墙的 wallOffset 同一个意思，图廊的分页口径也是条目数
  const galleryStart = useRef(snapshot?.galleryStart ?? 0);
  // 已请求到第几个条目（绝对位置，按页长推进，不按拿到的组数——没图的条目也占一组）
  const galleryLoaded = useRef(snapshot?.galleryLoaded ?? 0);

  // 轮询乱序守卫：扫描期间后端响应时间抖动大，上一轮的慢响应可能晚于
  // 下一轮到达，不作废就会用旧快照覆盖新状态（进度回跳、胶囊闪烁）
  const reloadSeq = useRef(0);
  // 海报墙已加载的格数：轮询按这个数重拉第一页，用户滚到第几屏就刷新到第几屏
  // ——否则每轮轮询都把墙缩回首屏，正在看的位置被抽走
  const wallLoaded = useRef(snapshot?.wallLoaded ?? WALL_PAGE_SIZE);
  // 当前排序（扫描补探阶段切到「待补探优先」）。放 ref 而不进依赖：reload
  // 每轮都读最新值，不必为切排序重建回调链
  const wallSort = useRef<LibraryItemSort>(snapshot?.wallSort ?? "title");
  // 排序方向（同 wallSort 放 ref）：undefined = 该档的自然方向，请求里不带 order
  const wallOrder = useRef<LibraryItemOrder | undefined>(snapshot?.wallOrder);
  // 当前窗口起点的 ref 版：reload 每轮读它，不进依赖（同 wallSort）
  const wallOffset = useRef(snapshot?.wallOffset ?? 0);
  const snapshotRef = useRef<LibraryDetailSnapshot | null>(null);
  snapshotRef.current = libraries
    ? {
        libraries,
        items,
        wallHasMore,
        wallIndex,
        wallStart,
        unidentified,
        review,
        ignored,
        missing,
        metaRefresh,
        wallLoaded: wallLoaded.current,
        wallSort: wallSort.current,
        wallOrder: wallOrder.current,
        wallOffset: wallOffset.current,
        galleryGroups,
        galleryHasMore,
        galleryStart: galleryStart.current,
        galleryLoaded: galleryLoaded.current,
        stale: snapshotStale,
      }
    : null;

  // 布局提交后就更新快照，路由切换的下一棵树可以在首帧直接读取它；不能只在
  // 卸载 cleanup 中写入，因为新路由的首次 render 可能早于被动 effect 的 cleanup。
  useLayoutEffect(() => {
    if (snapshotRef.current) setLibraryDetailSnapshot(libraryId, snapshotRef.current);
  }, [
    galleryGroups,
    galleryHasMore,
    ignored,
    items,
    libraries,
    libraryId,
    metaRefresh,
    missing,
    review,
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
          order: wallOrder.current,
          filter: wallFilter.current,
          limit: Math.min(WALL_API_PAGE_SIZE, wanted - page * WALL_API_PAGE_SIZE),
          offset,
        });
      },
    );
    Promise.all([
      listLibraries(),
      // 超管不在这个库的浏览范围内时海报墙接口按 404 拒绝（管理视图不渲染墙），
      // 不是拉取失败，别点亮顶部的重试提示条
      Promise.all(itemPages)
        .then((pages) => pages.flat())
        .catch((e) => {
          if (e instanceof HttpError && e.status === 404) return [] as LibraryItem[];
          throw e;
        }),
      // 临时条目通常是几个到几十个，一次拉全、按入账时间倒序；不参与主墙分页与索引
      listLibraryItems(libraryId, {
        identity: "provisional",
        sort: "added_at",
        limit: PROVISIONAL_LIMIT,
      }).catch(() => [] as LibraryItem[]),
      // 跳转索引与当前排序、当前筛选同口径——三者读的是同一份有序名单。
      // 只有三种排序分得出有意义的档：首字母 / 月份 / 评分档。其余（最近添加、
      // 片长、体积、最近观看）轨道本来就不显示，索引这一趟请求也省了
      INDEXED_SORTS[wallSort.current] === undefined
        ? Promise.resolve([] as LibraryIndexEntry[])
        : listLibraryItemIndex(
            libraryId,
            INDEXED_SORTS[wallSort.current]!,
            wallFilter.current,
            wallOrder.current,
          ).catch(() => []),
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
    ])
      .then(([libs, libraryItems, provisionalItems, index, unknown, reviewGroups, ignoredGroups, missingItems]) => {
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
        setProvisional((prev) => reconcileList(prev, provisionalItems, (i) => i.media_item_id));
        wallLoaded.current = Math.max(WALL_PAGE_SIZE, libraryItems.length);
        // 拿满这一页就假定后面还有；真到底时下一次追加会拿到空数组并收尾
        setWallHasMore(libraryItems.length >= wanted);
        setWallIndex((prev) => keepIfEqual(prev, index));
        if (unknown !== null) setUnidentified((prev) => keepIfEqual(prev, unknown));
        if (reviewGroups !== null) setReview((prev) => keepIfEqual(prev, reviewGroups));
        if (ignoredGroups !== null) setIgnored((prev) => keepIfEqual(prev, ignoredGroups));
        if (missingItems !== null) setMissing((prev) => keepIfEqual(prev, missingItems));
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
    // 挂载即对账，有会话快照也不例外：快照只负责首帧先把上次的分页窗口画
    // 出来（滚动位置有落脚点），内容必须以服务端为准。曾经有快照就跳过重拉、
    // 只等 30 秒空闲轮询——线上因此出过事：建库后马上点进去（空）、退出、
    // 导入完成再点进来，看到的还是那份空快照，停留不满 30 秒永远看不到新内容。
    // 重拉不会缩墙：wallLoaded/wallOffset 都从快照恢复，拉的是同一个窗口，
    // reconcileList 又按 id 复用旧引用，滚动锚点不会丢
    reload();
  }, [reload]);

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
    listLibraryItems(libraryId, {
      sort: wallSort.current,
      order: wallOrder.current,
      filter: wallFilter.current,
      limit: WALL_PAGE_SIZE,
      offset,
    })
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

  /* —— 向上补页 ——
     跳字母 / 回到上次位置会把整个分页窗口换成从该处开始的一页，窗口起点
     （wallStart）因此不为 0：上方明明还有作品，往上滑却是一堵墙，用户以为
     "跳过去就只能往下看了"（用户反馈 2026-09-08）。这里补上反方向的分页——
     墙顶的哨兵提前 600px 触发，把上一页接到已加载内容前面。

     接上去之后墙会整体长高、已加载的内容被往下推，所以补完要按长高的量把
     scrollTop 加回去，眼下这一屏才纹丝不动。 */
  const loadingPrev = useRef(false);
  // 前置加载前滚动容器的高度；null = 本次 items 变化不是前置加载，不必补偿
  const prependFrom = useRef<number | null>(null);
  const loadPrev = useCallback(() => {
    if (loadingPrev.current) return;
    const until = wallOffset.current;
    if (until <= 0) return; // 已经到墙首，上方没有东西可补
    loadingPrev.current = true;
    // 同 loadMore：作废在途的轮询，否则它拿的是旧窗口的那一页，晚一步回来
    // 会把刚补上的上文整段抹掉
    const requestSeq = ++reloadSeq.current;
    const from = Math.max(0, until - WALL_PAGE_SIZE);
    listLibraryItems(libraryId, {
      sort: wallSort.current,
      order: wallOrder.current,
      filter: wallFilter.current,
      limit: until - from,
      offset: from,
    })
      .then((prev) => {
        if (requestSeq !== reloadSeq.current) return;
        // 一部都没拿到（这一段刚好被删空）：窗口起点保持不动就此打住，
        // 否则起点一路往前挪、哨兵每次都重新观察，会把这一段空区间反复请求
        if (prev.length === 0) return;
        // 起点按**实拿到的条数**回推，而不是按请求的条数：拿少了（并发删除）
        // 时窗口起点仍与已加载内容对得上，索引条与「上次位置」才不会整体错位
        const start = until - prev.length;
        wallOffset.current = start;
        wallLoaded.current += prev.length;
        prependFrom.current = scrollElement?.scrollHeight ?? null;
        setWallStart(start);
        // 与轮询/追加交叠时同一条目可能拿到两次，按 id 去重再接
        setItems((current) => {
          const seen = new Set(current.map((i) => i.media_item_id));
          return [...prev.filter((i) => !seen.has(i.media_item_id)), ...current];
        });
      })
      // 失败就先不补：哨兵还在墙顶，用户下次滑离再滑回来会重新触发
      .catch(() => {})
      .finally(() => {
        loadingPrev.current = false;
      });
  }, [libraryId, scrollElement]);

  // 前置加载的滚动补偿：墙长高了多少就把 scrollTop 加回多少。放 layout effect
  // 里在绘制前完成；补完立刻让虚拟化窗口重量一次——滚动事件要到下一帧才来，
  // 不补这一下会闪一帧空墙
  useLayoutEffect(() => {
    const before = prependFrom.current;
    prependFrom.current = null;
    if (before === null || !scrollElement) return;
    const grown = scrollElement.scrollHeight - before;
    if (grown <= 0) return;
    scrollElement.scrollTop += grown;
    remeasureWalls();
  }, [items, scrollElement]);

  // 海报墙顶部的锚：跳字母后滚回墙首，否则用户停在原来的滚动位置上，
  // 看到的是新一批的中间，像是"点了没反应"
  const wallTop = useRef<HTMLDivElement>(null);
  /** 筛选条的锚点：改条件之后滚到它，条件行才不会被推出视口 */
  const filterBarTop = useRef<HTMLDivElement>(null);
  const wallGrid = useRef<HTMLDivElement>(null);
  /**
   * 跳到某个首字母档：换掉整个窗口（而不是继续往后追加），此后向下照常滚动
   * 加载，向上由墙顶哨兵把上文补回来（loadPrev）。
   * offset=0 即回到墙首，索引条的「全部」走的也是这条路。
   */
  /**
   * 改筛选条件。
   *
   * 条件变了，旧窗口的 offset 指向的是另一份名单，一律回到墙首重取；
   * 索引条与计数也都在 reload 里，跟着一起换。ref 先于 state 更新，
   * 好让这一轮 reload 立刻读到新条件（state 要到下一帧才生效）。
   */
  /** 拉本库的合集。空合集后端已经滤掉了——点进去空无一物的合集是纯粹的死路。 */
  const reloadCollections = useCallback(() => {
    listCollections({ libraryId, includeHidden: showHiddenCollections })
      .then(setCollections)
      // 拿不到就当没有合集：chip 行与视图切换一起不出现，墙照常能用
      .catch(() => setCollections([]));
  }, [libraryId, showHiddenCollections]);

  const switchView = useCallback((next: LibraryView) => {
    setLibraryView(next);
    writeViewToUrl(next);
  }, []);

  const applyFilter = useCallback(
    (next: LibraryFilter) => {
      wallFilter.current = next;
      setFilter(next);
      writeFilterToUrl(next);
      wallOffset.current = 0;
      wallLoaded.current = WALL_PAGE_SIZE;
      setWallStart(0);
      setActiveWallInitial(null);
      void reload();
      // 滚到**筛选条**而不是墙顶：滚到墙顶会把筛选条连同已选条件一起推到视口
      // 上方，在窄屏上正好钻到浮在顶部的那排导航键底下，看着像坏了。而条件行
      // 的职责恰恰是"改完之后仍然看得见自己筛了什么"
      (filterBarTop.current ?? wallTop.current)?.scrollIntoView({
        block: "start",
        behavior: "instant",
      });
    },
    [reload],
  );

  const jumpTo = useCallback(
    (offset: number) => {
      // 跳转期间两头的哨兵都挡住，别让旧窗口的追加/补页插进来
      loadingMore.current = true;
      loadingPrev.current = true;
      // 作废在途的轮询：它拿的是旧窗口的那一页，晚一步回来就把用户刚跳到的
      // 位置又拽回去（表现为"点了字母，一秒后自己跳回墙首"）
      reloadSeq.current += 1;
      wallOffset.current = offset;
      listLibraryItems(libraryId, {
      sort: wallSort.current,
      order: wallOrder.current,
      filter: wallFilter.current,
      limit: WALL_PAGE_SIZE,
      offset,
    })
        .then((page) => {
          wallLoaded.current = Math.max(WALL_PAGE_SIZE, page.length);
          setWallStart(offset);
          setItems(page);
          setWallHasMore(page.length >= WALL_PAGE_SIZE);
          // 瞬时而不是平滑：墙上的内容已经整段换掉，平滑滚过去的是一堆不存在
          // 的旧内容；更要紧的是落地后墙顶哨兵会立刻补上一页，那一下的滚动
          // 补偿会打断还在跑的平滑动画，把人停在半路上
          wallTop.current?.scrollIntoView({ block: "start", behavior: "instant" });
        })
        .catch(() => {})
        .finally(() => {
          loadingMore.current = false;
          loadingPrev.current = false;
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

  // ?pending=1 的落地：库与四份清单在同一轮 reload 里一起就位（见 reload），
  // 所以 library 一出现 pendingTab 就是准的
  useEffect(() => {
    if (!pendingRequested.current || !library || !canManageLibraries) return;
    pendingRequested.current = false;
    setIssueTab(pendingTab);
  }, [library, pendingTab, canManageLibraries]);

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
  // 其他库（本地内容、无结构）：墙按内容时间倒序（家庭录像按拍摄日期最自然），
  // 没有拼音字母档
  const timeline = Boolean(library && !library.capabilities.scraped);
  // 图片库：条目只看不播，墙是按月分组的瀑布流，点击开灯箱而不是进详情页
  // （docs/design/library-photo-kind.md 3.2）
  const photoWall = Boolean(library && !library.capabilities.playable);
  // 图片库不拉合集：照片没有类型/评分/地区这些事实，合集也就无从谈起
  useEffect(() => {
    if (photoWall) return;
    reloadCollections();
  }, [photoWall, reloadCollections]);
  const [photoDensity, setPhotoDensity] = usePhotoWallDensity();
  // 「最近添加」是用户在 ⋯ 菜单里选的一档，三面墙（海报墙 / 相册墙 / 图廊）
  // 共用。补探阶段的临时排序压过它：那几分钟墙上要回答的是"在处理哪几部"
  const [
    { pref: wallSortPref, reversed: wallSortReversed },
    setWallSortPref,
    toggleWallSortReversed,
    wallSortReady,
  ] = useWallSortPref();
  const recentFirst = wallSortPref === "added_at" && !probing;
  // 当前真正生效的服务端排序键：补探阶段临时接管；其次是用户选的档；默认档按库的形态。
  // 图片库只认「最近添加」：偏好是全站共用的一个键，别的库里选的「按评分」
  // 不该把相册的按月瀑布流排乱（图片库也不摆排序控件）
  const effectiveSort: LibraryItemSort = probing
    ? "probing"
    : wallSortPref !== "default" && !(photoWall && wallSortPref !== "added_at")
      ? PREF_TO_SORT[wallSortPref]
      : timeline
        ? "release_date"
        : "title";
  // 这一次是不是升序：该档自然方向，用户反转过就倒过来（补探序与相册墙不理会方向）
  const sortAscending =
    SORT_DIRECTIONS[effectiveSort].naturalAsc !==
    (wallSortReversed && effectiveSort !== "probing" && !photoWall);
  // 图廊按同一个键取页（后端默认标题序，只有选了最近添加才带参数）
  const gallerySort = recentFirst ? ("added_at" as const) : undefined;
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  // 月份索引给出全库每月张数，墙上的月份标题据此显示总数而不是已加载数
  const photoMonthCounts = useMemo(
    () => (photoWall ? new Map(wallIndex.map((entry) => [entry.initial, entry.count])) : undefined),
    [photoWall, wallIndex],
  );
  // 灯箱翻到最后一张时：已加载列表被跳转替换过也无妨，loadMore 按当前窗口追加
  const closeLightbox = useCallback(() => setLightboxIndex(null), []);
  // 图床浏览模式的开关在文件上方（滚动恢复要先知道用哪个锚点属性）
  // 按作品分段，还是整库的图混成一条瀑布流（⋯ 菜单里切换）
  const [galleryGrouped, setGalleryGrouped] = useVideoGalleryGrouped();
  const gallery = galleryPreferred && Boolean(library?.capabilities.playable);
  const galleryLoading = useRef(false);
  const galleryEntries = useMemo(() => flattenGallery(galleryGroups), [galleryGroups]);
  const loadMoreGallery = useCallback(() => {
    if (galleryLoading.current) return;
    galleryLoading.current = true;
    const offset = galleryLoaded.current;
    listLibraryGallery(libraryId, {
      limit: GALLERY_PAGE_SIZE,
      offset,
      sort: gallerySort,
      filter: wallFilter.current,
    })
      .then((page) => {
        galleryLoaded.current = offset + page.length;
        setGalleryGroups((current) => dedupeGalleryGroups([...current, ...page]));
        setGalleryHasMore(page.length >= GALLERY_PAGE_SIZE);
      })
      .catch(() => setGalleryHasMore(false))
      .finally(() => {
        galleryLoading.current = false;
      });
  }, [libraryId, gallerySort]);
  /**
   * 按已加载的页数重拉整个图廊窗口：从快照恢复出来的窗口是离开这一屏时的旧
   * 数据（比如在详情页点了心，返回时角标要跟着变），拿到结果整体替换。
   * 不缩窗口、不动滚动位置；失败就留着旧窗口，下次返回再对账。
   */
  const refreshGallery = useCallback(() => {
    const start = galleryStart.current;
    const loaded = galleryLoaded.current - start;
    if (loaded <= 0 || galleryLoading.current) return;
    galleryLoading.current = true; // 对账期间别让滚动哨兵同时追加下一页
    Promise.all(
      Array.from({ length: Math.ceil(loaded / GALLERY_PAGE_SIZE) }, (_, page) =>
        listLibraryGallery(libraryId, {
          filter: wallFilter.current,
          limit: GALLERY_PAGE_SIZE,
          offset: start + page * GALLERY_PAGE_SIZE,
          sort: gallerySort,
        }),
      ),
    )
      .then((pages) => {
        galleryLoaded.current = start + pages.reduce((sum, page) => sum + page.length, 0);
        setGalleryGroups(dedupeGalleryGroups(pages.flat()));
        setGalleryHasMore((pages.at(-1)?.length ?? 0) >= GALLERY_PAGE_SIZE);
      })
      .catch(() => undefined)
      .finally(() => {
        galleryLoading.current = false;
      });
  }, [libraryId, gallerySort]);
  /**
   * 图廊跳到整份排序里的某个位置：与海报墙的 jumpTo 是同一件事——换掉整个
   * 窗口（而不是从头追加到那里），此后照常向下滚动加载。图廊的分页口径也是
   * 条目数，两面墙的 offset 通用，「回到上次位置」因此两种形态都能跳。
   */
  const jumpGalleryTo = useCallback(
    (offset: number) => {
      galleryLoading.current = true; // 跳转期间挡住滚动哨兵与对账，别让旧窗口的页插进来
      galleryStart.current = offset;
      listLibraryGallery(libraryId, {
      limit: GALLERY_PAGE_SIZE,
      offset,
      sort: gallerySort,
      filter: wallFilter.current,
    })
        .then((page) => {
          galleryLoaded.current = offset + page.length;
          setGalleryGroups(dedupeGalleryGroups(page));
          setGalleryHasMore(page.length >= GALLERY_PAGE_SIZE);
          wallTop.current?.scrollIntoView({ block: "start", behavior: "smooth" });
        })
        .catch(() => {})
        .finally(() => {
          galleryLoading.current = false;
        });
    },
    [libraryId, gallerySort],
  );
  /**
   * 图廊里点心：收藏 / 取消收藏整部作品（与详情页那颗心同一落点）。
   *
   * 收藏态随图廊一起下发、挂在分组上，所以这里改的是 galleryGroups——灯箱的
   * 心与墙上那几张瓦片的角标读同一份，翻一次全都跟着变。先翻本地再落库，
   * 失败翻回来并提示。
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
  // 当前这份图廊窗口属于哪个库：快照带回来的窗口一进来就算数
  const galleryWindowLibrary = useRef<number | null>(
    snapshot?.galleryGroups.length ? libraryId : null,
  );
  // 当前这份窗口是按哪个排序取的。null = 快照带回来的窗口（与偏好同一份
  // localStorage，排序不会中途变），按现在的排序对账即可，不必重拉
  const galleryWindowSort = useRef<typeof gallerySort | null>(null);
  /**
   * 切进图廊：第一次（或换库、换排序）从第一页拉起；从条目详情页返回时窗口已经
   * 由快照恢复，**不能**清空重拉——只补第一页的话容器矮到装不下离开时的滚动
   * 位置，滚动恢复会等到超时后放弃，人被甩回墙首（用户反馈 2026-09-07）。
   *
   * 也不再于切回海报墙时清空：这份窗口本来就随快照留在会话里，清了只是让下次
   * 切回图廊白拉一遍。
   */
  useEffect(() => {
    // 偏好还没从 storage 读出来时先按兵不动：这一帧的排序是默认值，
    // 照它对账会把窗口按错的顺序重排一遍（见 useWallSortPref）
    if (!gallery || !wallSortReady) return;
    const sortChanged =
      galleryWindowSort.current !== null && galleryWindowSort.current !== gallerySort;
    galleryWindowSort.current = gallerySort;
    if (!sortChanged && galleryWindowLibrary.current === libraryId) {
      refreshGallery();
      return;
    }
    galleryWindowLibrary.current = libraryId;
    galleryStart.current = 0;
    galleryLoaded.current = 0;
    setGalleryGroups([]);
    setGalleryHasMore(false);
    loadMoreGallery();
  }, [gallery, libraryId, gallerySort, wallSortReady, loadMoreGallery, refreshGallery]);

  /* —— 「回到上次浏览的位置」（lib/library-wall-recall.ts）——
     大库滑到第几十屏是常态，关掉页面第二天再进来又从墙首开始。这里在底部弹
     一枚胶囊问一句：要跳回去点它，不要就继续滑——滑够一屏胶囊自己让位，
     从那一刻起记的是新位置。会话内从详情页返回不弹（滚动恢复已经自动回位）。 */
  // 墙的形态决定 offset 的口径：图廊按标题序分页，其他库的海报墙按内容时间序，
  // 两者的「第 300 个」不是同一部作品，形态对不上就不提示（见 WallRecall.view）
  // 排序也是形态的一部分：同一个 offset 在标题序与最近添加序里指向的不是
  // 同一部作品，换了排序就当作没有记录（后缀只加在非默认序上，免得让老记录失效）
  // 每一档、每个方向各记各的：同一个 offset 在「按评分 高→低」与「低→高」里指向两头。
  // 默认档（标题序 / 其他库的时间序）不加后缀，老记录不失效；「最近添加」沿用 :added
  const defaultSort =
    effectiveSort === "title" ||
    effectiveSort === "probing" ||
    (timeline && effectiveSort === "release_date");
  const wallView =
    (gallery ? "gallery" : timeline ? "wall:time" : "wall:title") +
    (recentFirst ? ":added" : gallery || defaultSort ? "" : `:${effectiveSort}`) +
    (!gallery && sortAscending !== SORT_DIRECTIONS[effectiveSort].naturalAsc ? ":rev" : "");
  // 条目 id → 它在整份排序里的绝对位置。滚动时按首个可见格反查（图廊按瓦片
  // 所属的作品），DOM 上只挂 id，不必给每一格再算一遍下标
  const offsetById = useMemo(
    () => new Map(items.map((item, index) => [String(item.media_item_id), wallStart + index])),
    [items, wallStart],
  );
  // galleryStart 只在窗口被整体换掉时变，与 galleryGroups 同一时刻，不必进依赖
  const galleryOffsetById = useMemo(
    () =>
      new Map(
        galleryGroups.map((group, index) => [
          String(group.media_item_id),
          galleryStart.current + index,
        ]),
      ),
    [galleryGroups],
  );
  const wallOffsetAt = useCallback(() => {
    const wall = wallTop.current;
    if (!wall || !scrollElement) return null;
    const id = firstVisibleAnchorId(
      wall,
      gallery ? "data-gallery-item-id" : "data-library-item-id",
      scrollElement.getBoundingClientRect().top,
    );
    // 反查不到就不记：未识别分区的格子也挂着条目 id，但它不在主排序里，
    // 拿它的位置去跳会跳到一部毫不相干的作品上
    return id === null ? null : ((gallery ? galleryOffsetById : offsetById).get(id) ?? null);
  }, [gallery, galleryOffsetById, offsetById, scrollElement]);
  // 久别回归时把这一屏复位：页面没重新加载，人还停在离开时的位置上，不回到
  // 顶部的话胶囊指着的就是脚下这一格。瞬时归零而不是平滑滚动——这是「重新
  // 进入」，不是一次导航，几万像素的平滑动画只会让人以为页面失控了
  const resetWallToTop = useCallback(() => {
    scrollElement?.scrollTo({ top: 0, behavior: "instant" });
  }, [scrollElement]);
  const { recallOffset, dismissRecall } = useWallRecall({
    scope: wallRecallScope(
      libraryId,
      // 指纹与「当前墙是不是等于某个合集」用的是同一份规范化键（filterKey），
      // 两处口径分叉的话，合集 chip 会亮在一面并不属于它的墙上
      filterKey(filter),
    ),
    view: wallView,
    scroller: scrollElement,
    // 补探阶段的排序是临时的（几分钟后自动落回拼音序），那期间不记也不提示；
    // 偏好没读出来之前也不记，那一帧的 wallView 还是默认序的
    enabled:
      !probing && wallSortReady && (gallery ? galleryGroups.length > 0 : items.length > 0),
    offer: freshEntry.current,
    offsetAt: wallOffsetAt,
    onReenter: resetWallToTop,
  });
  // 记录可能指向已经不存在的位置（库被清空、大批删除），跳过去只会是一面空墙
  const recallable =
    recallOffset !== null && recallOffset < (library?.stats.item_count ?? 0) ? recallOffset : null;
  const wideCards = Boolean(library && library.capabilities.default_aspect > 1);
  // 其他库的主图两种形态并存：刮削器放好 -poster 的是 2:3 竖版海报，只有 -thumb /
  // 抓帧的是横版缩略图。竖横混在一个网格里对不齐，按主图比例切成两区，各自用
  // 合适的列宽；只有一种形态时仍是普通的一面墙（docs/design/library-other-kind.md 4.3）
  const wallGroups = useMemo(() => {
    if (!timeline) return null;
    return {
      posters: items.filter((item) => item.primary_aspect < 1),
      thumbs: items.filter((item) => item.primary_aspect >= 1),
    };
  }, [items, timeline]);
  const splitWall = Boolean(
    wallGroups && wallGroups.posters.length > 0 && wallGroups.thumbs.length > 0,
  );
  // 单区时列宽跟着实际形态走：其他库里全是海报就用电影库的窄列
  const wideWall = wallGroups ? wallGroups.posters.length === 0 : wideCards;
  // 这一格正被后台处理时的文案（整库刷新阶段 / 单条目任务 / 扫描补探）。
  // 必须是稳定引用：相册墙把它一路传到每个月份段，每次渲染换一个新函数就会
  // 击穿下游所有 memo——库页光是滚动联动与后台轮询就会重渲几十次
  const workingLabelOf = useCallback(
    (item: LibraryItem) =>
      refreshPhaseById.get(item.media_item_id) ??
      jobPhaseById.get(item.media_item_id) ??
      (probing && item.probe_pending_count > 0 ? "正在读取规格" : undefined),
    [refreshPhaseById, jobPhaseById, probing],
  );
  /** 单库页的墙：每一格都落回本库 */
  const ownLibraryId = useCallback(() => libraryId, [libraryId]);

  /* —— 拼音索引条的当前字母 ——
     两面墙两条路：图片库的相册墙按月分段，段是 <section>、永远挂着，仍按
     DOM 锚点求；海报墙虚拟化之后视口外的格子根本不在 DOM 里，锚点无从谈起，
     改成按算好的行位置求（PosterWall 把行位置交出来）。后者反而更省——每帧
     不必再查 DOM。 */
  const wallGeometry = useRef<{ rowTops: readonly number[]; columns: number } | null>(null);
  const onWallGeometry = useCallback(
    (geometry: { rowTops: readonly number[]; columns: number } | null) => {
      wallGeometry.current = geometry;
    },
    [],
  );
  // 各字母首部条目在**本窗口内**的下标（整份排序的 offset 减去窗口起点）
  const wallAnchors = useMemo(
    () =>
      wallIndex
        .map((entry) => ({ initial: entry.initial, index: entry.offset - wallStart }))
        .filter((anchor) => anchor.index >= 0),
    [wallIndex, wallStart],
  );
  useEffect(() => {
    const fallback = wallInitialAtOffset(wallIndex, wallStart);
    setActiveWallInitial(fallback);
    if (!scrollElement || probing) return;

    let frame = 0;
    const update = () => {
      frame = 0;
      const grid = wallGrid.current;
      if (!grid) return;
      const viewportTop = scrollElement.getBoundingClientRect().top + 1;
      const geometry = wallGeometry.current;
      const next = photoWall
        ? activeWallInitialAtViewport(
            Array.from(
              grid.querySelectorAll<HTMLElement>("[data-wall-initial]"),
              (marker) => ({
                initial: marker.dataset.wallInitial ?? "",
                top: marker.getBoundingClientRect().top,
              }),
            ).filter((marker) => marker.initial !== ""),
            viewportTop,
            fallback,
          )
        : geometry
          ? activeInitialAt(
              wallAnchors,
              geometry.rowTops,
              geometry.columns,
              viewportTop - grid.getBoundingClientRect().top,
              fallback,
            )
          : fallback;
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
  }, [items.length, photoWall, probing, scrollElement, wallAnchors, wallIndex, wallStart]);

  // 排序切换是**服务端**的事（墙是分页的，本地排只能排到已加载的那几屏）：
  // 阶段一变、或用户在 ⋯ 菜单里换了排序，就换排序键重拉第一页
  useEffect(() => {
    // 同图廊：偏好没读出来之前不动排序，否则从详情页返回的那一帧会先按
    // 默认序把窗口重拉一遍，人被甩回墙首
    if (!wallSortReady) return;
    // 只有反转了自然方向才带 order：不反转时与加方向之前的请求逐字相同
    const nextOrder: LibraryItemOrder | undefined =
      sortAscending === SORT_DIRECTIONS[effectiveSort].naturalAsc
        ? undefined
        : sortAscending
          ? "asc"
          : "desc";
    if (wallSort.current === effectiveSort && wallOrder.current === nextOrder) return;
    wallSort.current = effectiveSort;
    wallOrder.current = nextOrder;
    wallLoaded.current = WALL_PAGE_SIZE;
    // 换了排序或方向，之前跳到的字母位置就没意义了，窗口回到墙首
    wallOffset.current = 0;
    setWallStart(0);
    reload();
  }, [effectiveSort, sortAscending, wallSortReady, reload]);

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

  // 图床浏览模式的开关：长在顶栏、与 ⋯ 菜单并排。只有能播的库（影视库 /
  // 其他库）才有——图片库本身就是相册墙。切换时顺手关掉灯箱：两种模式的
  // 灯箱翻的不是同一份列表，下标不能沿用
  /**
   * 图床浏览能不能用。合集视图里不能——那面墙根本不在，点了什么也不会发生
   *（没有事实就不摆控件）。
   *
   * 入口在 ⋯ 菜单里，不在顶栏：顶栏右上角只留搜索与 ⋯ 两颗。图床是"偶尔换个
   * 看法"，不是常用动作，为它常驻一颗键，代价是吸顶标题少一半地方
   *（实测 50.5px → 102px 的差别）。
   */
  const galleryAvailable = library.capabilities.playable && libraryView === "items";
  // 按评分排序或用评分筛选时，评分就是用户正在比的东西：墙上每格常显评分。
  // 平时浏览不印（见 InventoryCell 的 ratingText）
  const showRating = wallSortPref === "rating" || filter.ratingGte != null;

  /**
   * 作品 / 合集 切换。窄屏上挂进 PageNav 顶栏、搜索键左侧（与发现页把 TMDB /
   * 豆瓣 切换挂进全局顶栏同一个位置，外观也照抄 discover-view 的
   * SourceSwitcher）：它在正文里要独占一整行，而 390px 的屏幕上那一行很贵。
   * 桌面端仍留在正文，那儿不缺这一行，tab 紧挨着内容也更符合 Plex 的心智。
   *
   * 顶栏里不带合集数：☰ + 返回 + 切换 + 搜索 + ⋯ 已经把 390px 用满，再多
   * 一个数字就把整行挤出屏幕。数量在切到合集视图后一眼就能看到。
   *
   * 一个合集都没有时整个控件不出现——没有事实就不摆控件。
   */
  const viewSwitch = collections.length > 0 && (
    <div
      role="tablist"
      aria-label="库内视图"
      className="flex shrink-0 rounded-full border border-white/10 bg-black/35 p-1 backdrop-blur-xl"
    >
      {(
        [
          ["items", "作品"],
          ["collections", "合集"],
        ] as const
      ).map(([value, label]) => (
        <button
          key={value}
          type="button"
          role="tab"
          aria-selected={libraryView === value}
          onClick={() => switchView(value)}
          className={`rounded-full px-4 py-1.5 text-sub font-semibold transition ${
            libraryView === value
              ? "bg-white/15 text-white shadow-sm"
              : "text-[var(--text-muted)] hover:text-white"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );

  // 库操作全部收进 ⋯ 菜单，顶栏只留这一个入口；运行状态看头部下方的胶囊。
  // 清空观看记录不在这里——那是跨库的个人数据，入口在首页「最近观看」的 ⋯ 里。
  //
  // 排序提到墙控件行之后，这个菜单不再"恒在"：非管理员在海报墙形态下已经
  // 一项可调的都没有，那就别渲染——点开一片空白比没有这颗键更糟。
  const hasMenuItems = canManageLibraries || photoWall || gallery || galleryAvailable;
  const actionsMenu = hasMenuItems && (
    <LibraryActionsMenu
      canManage={canManageLibraries}
      density={photoWall || gallery ? photoDensity : undefined}
      onDensityChange={photoWall || gallery ? setPhotoDensity : undefined}
      grouped={gallery ? galleryGrouped : undefined}
      onGroupedChange={gallery ? setGalleryGrouped : undefined}
      galleryMode={galleryAvailable || gallery ? gallery : undefined}
      onGalleryModeChange={
        galleryAvailable || gallery
          ? (next: boolean) => {
              setLightboxIndex(null);
              setGalleryMode(next);
            }
          : undefined
      }
      // 「显示已隐藏的合集」只在合集视图里给：在海报墙上它没有任何意义
      showHiddenCollections={libraryView === "collections" ? showHiddenCollections : undefined}
      onShowHiddenCollectionsChange={
        libraryView === "collections" ? setShowHiddenCollections : undefined
      }
      // 排序是三面墙共用的偏好，普通成员也能选；补探那几分钟排序被临时接管，
      // 菜单如实置灰而不是假装可选

      // 默认那一档在各面墙上叫法不同：图廊恒按标题序（与海报墙共用名单，
      // 见 build_library_gallery），其他库与图片库的海报墙按内容时间倒序
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
      capabilities={library.capabilities}
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
        const caps = library.capabilities;
        const ask = !caps.scraped && caps.playable ? rereadLibraryNfoConfirm : refreshLibraryConfirm;
        void confirm(ask(library.name)).then((ok) => {
          if (ok) void kick(startLibraryMetadataRefresh(libraryId));
        });
      }}
      pendingCount={pendingCount}
      onOpenPending={() => setIssueTab(pendingTab)}
      chapterJob={library.chapter_job}
      onChapterImages={
        library.extract_chapter_images
          ? () => {
              setNotice(null);
              void confirm(chapterImagesConfirm(library.name)).then(({ ok, checked }) => {
                if (ok) {
                  startLibraryChapterImages(libraryId, { force: checked })
                    .then(() =>
                      toast.success(
                        checked
                          ? "已开始重新生成章节，可在任务中心查看进度"
                          : "已开始生成章节，可在任务中心查看进度",
                      ),
                    )
                    .catch((e) => toast.error((e as Error).message));
                }
              });
            }
          : undefined
      }
      onEdit={() => setEditing(library)}
    />
  );

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      {/* 顶栏：返回媒体库 + 吸顶库名 + 库操作 ⋯（无 pt——顶边与侧栏卡片顶边齐平） */}
      <PageNav
        title={library.name}
        fallback={navFallback}
        actions={actionsMenu || undefined}
        toolbar={(isMobile && !photoWall && viewSwitch) || undefined}
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
          {!library.viewer_access ? (
            <span className="flex shrink-0 items-center gap-1 rounded-full border border-[var(--warn)]/35 bg-[var(--warn)]/[0.12] px-2 py-0.5 text-caption font-semibold text-[var(--warn)]">
              <LockIcon className="size-3" />
              仅管理
            </span>
          ) : null}
        </div>
        <p className="text-on-image mt-1.5 truncate text-ui text-[var(--text-muted)] max-md:text-sub">
          {meta.label}库 · {stats.item_count} {library.kind === "photo" ? "张" : "部作品"} ·{" "}
          {stats.file_count} 个文件 · {formatBytes(stats.total_size_bytes)}
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

      {/* —— 管理视图：超管不在本库的浏览范围内（docs/design/library-access.md 2.4）。
          设置 / 扫描 / 待处理 / 回收站照常（都在上方头部与 ⋯ 菜单里），海报墙、
          未识别分区一概不渲染——内容对当前身份就是不存在 —— */}
      {!library.viewer_access ? (
        <div className="mt-16 flex flex-col items-center gap-3 px-6 text-center">
          <LockIcon className="size-9 text-white/[0.28]" />
          <p className="text-ui leading-7 text-[var(--text-muted)]">
            内容已隐藏：你不在这个库的可见范围内。
            <br />
            设置、扫描与待处理仍可使用；把自己加入可见范围即可浏览。
          </p>
          {canManageLibraries && (
            <button
              type="button"
              onClick={() => setEditing(library)}
              className="btn-glass mt-1 h-9 px-4 text-ui font-medium"
            >
              把我加入可见范围
            </button>
          )}
        </div>
      ) : (
      <>
      {/* —— 作品 / 合集：库内的两个视图（Plex 的 tab 模型）。
          一个合集都没有时这一行不出现——没有事实就不摆控件 —— */}
      {!photoWall && !isMobile && collections.length > 0 && (
        <div
          role="tablist"
          aria-label="库内视图"
          className="mt-5 flex items-center gap-1 px-6"
        >
          {(
            [
              ["items", "作品"],
              ["collections", "合集"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={libraryView === value}
              onClick={() => switchView(value)}
              className={`h-8 rounded-full px-3 text-ui transition ${
                libraryView === value
                  ? "bg-white/[0.14] font-medium text-white"
                  : "text-white/55 hover:bg-white/[0.08] hover:text-white"
              }`}
            >
              {label}
              {value === "collections" && (
                <span className="ml-1.5 font-mono text-caption tabular-nums text-white/40">
                  {collections.length}
                </span>
              )}
            </button>
          ))}
        </div>
      )}

      {/* —— 合集视图：纵向网格，一次看全（横滚只会掩盖数量）—— */}
      {libraryView === "collections" ? (
        <div className="mt-4">
          <LibraryCollectionsView collections={collections} libraryId={libraryId} />
        </div>
      ) : (
      <>
      {/* —— 库存海报墙 —— */}
      {items.length === 0 && provisional.length === 0 && filtering ? (
        // 筛空了不给空墙——给一条真能救回内容的出路（铁律 2）。库本身就是空的
        // 是另一回事，走下面那个分支
        <FilterEmptyState
          libraryId={libraryId}
          filter={filter}
          onFilterChange={applyFilter}
        />
      ) : items.length === 0 && provisional.length === 0 ? (
        <p className="mt-16 text-center text-ui leading-7 text-[var(--text-muted)]">
          这个库还没有内容。
          <br />
          {canManageLibraries
            ? "点右上角菜单里的「扫描库」把已有影片识别入库；订阅内容下载完成后也会自动进来。"
            : "订阅内容下载并入库后会显示在这里。"}
        </p>
      ) : (
        <>
          {/* 筛选条：静止态只有一个「筛选」按钮，点开才有四个维度；
              条件一生效，条件本身就顶替控件出现在下面那行（且与面板开合无关）。
              图片库不给筛选——照片没有类型/评分/地区这些事实，摆上去就是
              永远返回 0 的死控件（docs/design/library-filtering.md 3.5） */}
          {/* 外面这层 div 只做滚动锚点：scroll-mt 让开顶栏那 52px（PageNav 是
              sticky 的无底浮层），不留这一截，scrollIntoView 会把筛选条正好
              塞到导航键底下，控件与文字糊成一团 */}
          {!photoWall && (
            <div
              ref={filterBarTop}
              className="scroll-mt-[52px] max-md:scroll-mt-[calc(52px+var(--safe-top))]"
            >
            <LibraryFilterBar
              libraryId={libraryId}
              filter={filter}
              onFilterChange={applyFilter}
              collections={collections}
              onSaveAsCollection={() => setSavingCollection(true)}
              sortControl={
                <WallSortControl
                  value={wallSortPref}
                  options={sortOptions(!gallery && timeline ? "按时间" : "按标题", gallery, timeline)}
                  onChange={setWallSortPref}
                  disabled={probing}
                  // 图廊只吃服务端标题 / 最近添加两档的默认方向，不给方向切换
                  direction={
                    gallery
                      ? undefined
                      : {
                          ascending: sortAscending,
                          label: SORT_DIRECTIONS[effectiveSort][sortAscending ? "asc" : "desc"],
                          onToggle: toggleWallSortReversed,
                        }
                  }
                />
              }
              className="mt-5 px-6 max-md:mt-4 max-md:px-4"
            />
            </div>
          )}
          <div ref={wallTop} className="mt-6 max-md:mt-4">
            {/* 索引条与内容列并排：条固定在视口右侧（sticky），列照常滚。索引条
                有固定高度，加载哨兵与未识别分区必须放进同一列里——否则卡片少时
                这一行被索引条撑高，分区会被推到一大段空白之下 */}
            <div className="flex items-start gap-2 px-6 max-md:gap-1 max-md:px-4">
              {/* overflow-anchor:none：向上补页后墙会长高，浏览器自带的滚动锚定
                  会跟着自己补一次 scrollTop，与我们按长高量做的补偿叠加就是跳两下
                  （何况 Safari 根本没有滚动锚定）。这一段的位置全部自己算 */}
              <div className="min-w-0 flex-1 [overflow-anchor:none]">
                {/* 墙顶还有上文时向上补页：跳字母/回到上次位置之后仍然能往上滑 */}
                {!gallery && <WallLoadPrev start={wallStart} onReach={loadPrev} />}
                {gallery ? (
                  <VideoGalleryWall
                    groups={galleryGroups}
                    density={photoDensity}
                    grouped={galleryGrouped}
                    onOpen={setLightboxIndex}
                  />
                ) : photoWall ? (
                  <div ref={wallGrid}>
                    <PhotoWall
                      items={items}
                      density={photoDensity}
                      // 「最近添加」序里同一个月的照片不再连续，按月分段会把顺序
                      // 打散成一堆重复的月份标题——那一档就是一条不分段的瀑布流
                      grouped={!recentFirst}
                      monthCounts={photoMonthCounts}
                      onOpen={setLightboxIndex}
                      workingLabelOf={workingLabelOf}
                    />
                  </div>
                ) : splitWall && wallGroups ? (
                  <>
                    {/* 标题不带数字：分区是在已加载的分页上切的，数字会随滚动加载变 */}
                    <h3 className="text-on-image mb-4 text-body-lg font-semibold text-white/85">
                      海报
                    </h3>
                    <div ref={wallGrid}>
                      <PosterWall
                        items={wallGroups.posters}
                        libraryIdOf={ownLibraryId}
                        wide={false}
                        workingLabelOf={workingLabelOf}
                        showRating={showRating}
                      />
                    </div>
                    <h3 className="text-on-image mb-4 mt-8 text-body-lg font-semibold text-white/85">
                      缩略图
                    </h3>
                    <PosterWall
                      items={wallGroups.thumbs}
                      libraryIdOf={ownLibraryId}
                      wide
                      workingLabelOf={workingLabelOf}
                      showRating={showRating}
                    />
                  </>
                ) : (
                  <div ref={wallGrid}>
                    <PosterWall
                      items={items}
                      libraryIdOf={ownLibraryId}
                      wide={wideWall}
                      workingLabelOf={workingLabelOf}
                      onGeometry={onWallGeometry}
                      showRating={showRating}
                    />
                  </div>
                )}
                {/* 图廊与海报墙各自分页，哨兵按当前模式接线（图廊按作品数计） */}
                <WallLoadMore
                  hasMore={gallery ? galleryHasMore : wallHasMore}
                  loaded={gallery ? galleryLoaded.current - galleryStart.current : items.length}
                  start={gallery ? galleryStart.current : wallStart}
                  total={library.stats.item_count}
                  onReach={gallery ? loadMoreGallery : loadMore}
                  rootMargin={gallery ? GALLERY_LOAD_MARGIN : undefined}
                />

                {/* —— 未识别分区（只有影视库会有）：认不出的文件按文件名/目录名
                    临时挂着，可直接播放、记进度；和正式条目分开摆——2:3 海报与
                    16:9 抓帧混排、又混进拼音序里，正片的墙会被打散。认领后并入主墙 —— */}
                {!gallery && provisional.length > 0 && (
                  <section
                    data-wall="provisional"
                    aria-labelledby="provisional-title"
                    className="mt-6 max-md:mt-4"
                  >
                    <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
                      <div>
                        <h3
                          id="provisional-title"
                          className="text-on-image text-body-lg font-semibold text-white/85"
                        >
                          未识别 {provisional.length}
                        </h3>
                        <p className="text-on-image mt-1 text-caption text-[var(--text-muted)]">
                          按文件名展示，可以直接播放；认领身份后会并入上方的正式条目。
                        </p>
                      </div>
                      {canManageLibraries && (
                        <button
                          type="button"
                          onClick={() => setIssueTab("unidentified")}
                          className="btn-glass h-7 shrink-0 px-2.5 text-caption font-medium"
                        >
                          去待处理认领
                        </button>
                      )}
                    </div>
                    <div className={`mt-4 ${WALL_GRID_WIDE}`}>
                      {provisional.map((item) => (
                        <InventoryCell
                          key={item.media_item_id}
                          item={item}
                          libraryId={libraryId}
                          workingLabel={
                            probing && item.probe_pending_count > 0 ? "正在读取规格" : undefined
                          }
                        />
                      ))}
                    </div>
                  </section>
                )}
              </div>
              {/* 补探阶段排序不是拼音序，字母跳转会跳错位置——那几分钟里收起来；
                  其他库按内容时间排，同理没有字母档；「最近添加」序两种档都没有 */}
              {/* 只有按标题排序才有侧边索引条：补探序、最近添加、其他库的时间序没有字母档；
                  按评分 / 按上映时间只有几档、档名又宽，画出来挤海报却省不了几屏（见 INDEXED_SORTS） */}
              {effectiveSort === "title" && !gallery && (
                <WallIndexBar
                  index={wallIndex}
                  active={activeWallInitial}
                  onJump={jumpTo}
                  reversed={!sortAscending}
                />
              )}
              {photoWall && !recentFirst && (
                <PhotoTimelineScrubber
                  index={wallIndex}
                  active={activeWallInitial}
                  scrollElement={scrollElement}
                  onJump={jumpTo}
                />
              )}
            </div>
          </div>
          {gallery && lightboxIndex !== null && galleryEntries[lightboxIndex] && (
            <VideoGalleryLightbox
              entries={galleryEntries}
              index={lightboxIndex}
              hasMore={galleryHasMore}
              onIndexChange={setLightboxIndex}
              onReachEnd={loadMoreGallery}
              onToggleFavorite={toggleGalleryFavorite}
              onClose={closeLightbox}
            />
          )}
          {photoWall && lightboxIndex !== null && items[lightboxIndex] && (
            <PhotoLightbox
              libraryId={libraryId}
              items={items}
              index={lightboxIndex}
              hasMore={wallHasMore}
              onIndexChange={setLightboxIndex}
              onReachEnd={loadMore}
              onClose={closeLightbox}
            />
          )}
        </>
      )}

      </>
      )}

      </>
      )}

      {/* 上次滑到哪：进来时问一句要不要跳回去，不理它、往下滑一屏就自己消失 */}
      {recallable !== null && (
        <WallRecallPill
          onJump={() => {
            dismissRecall();
            if (gallery) jumpGalleryTo(recallable);
            else jumpTo(recallable);
          }}
          onDismiss={dismissRecall}
        />
      )}

      {/* 筛完存为合集：同一份条件的第二个时态（第三个是库的收藏范围）。
          新建之后立刻刷 chip 行——用户刚存的合集要马上看得见 */}
      <SaveAsCollectionDialog
        open={savingCollection}
        libraryId={libraryId}
        filter={filter}
        onClose={() => setSavingCollection(false)}
        onCreated={reloadCollections}
      />

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
  /** 媒体库管理权限：没有时菜单只剩图片库的相册墙密度 */
  canManage: boolean;
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
  /** 库的能力位：没有命名能力不给「整理文件名」，没有刮削链不给「刷新元数据」 */
  capabilities: LibraryCapabilities;
  onOpenPending: () => void;
  onOrganize: () => void;
  onToggleMetaRefresh: () => void;
  /** 整库生成章节（是否重做已有的在确认弹窗里勾选）；库关了开关时不传 */
  onChapterImages?: () => void;
  /** 章节作业排队/进行中：菜单项置灰并如实写状态 */
  chapterJob?: ChapterJobProgress | null;
  onEdit: () => void;
  /** 图片库：相册墙的密度（个人偏好，与管理权无关）；不传不渲染这一组 */
  density?: PhotoWallDensity;
  onDensityChange?: (next: PhotoWallDensity) => void;
  /** 图床浏览的开关：当前在不在图床模式；不传不渲染这一项（图片库、合集视图都没有） */
  galleryMode?: boolean;
  onGalleryModeChange?: (next: boolean) => void;
  /** 图床浏览模式：是否按作品分段；不传不渲染这一项（海报墙与图片库都没有分组一说） */
  grouped?: boolean;
  onGroupedChange?: (next: boolean) => void;
  /** 合集视图：要不要把藏起来的合集翻出来。不传不渲染这一项（只有合集视图有）。
   *  这是「隐藏」的回头路——没有它，那颗按钮就是单向黑洞 */
  showHiddenCollections?: boolean;
  onShowHiddenCollectionsChange?: (next: boolean) => void;
  /** 墙的排序（个人偏好，三面墙共用） */
  /** 补探阶段排序被临时接管，这一组置灰 */
  /** 默认那一档叫什么：影视库是「按标题」，其他库与图片库是「按时间」 */
}

function LibraryActionsMenu({
  canManage,
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
  capabilities,
  onOpenPending,
  onOrganize,
  onToggleMetaRefresh,
  onChapterImages,
  chapterJob,
  onEdit,
  density,
  onDensityChange,
  galleryMode,
  onGalleryModeChange,
  grouped,
  onGroupedChange,
  showHiddenCollections,
  onShowHiddenCollectionsChange,
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
          className="menu-surface z-50 min-w-[11rem] p-1"
        >
          {/* 待处理清单的**常驻**入口。头部胶囊是条件渲染的（有待办才出现），
              光靠它意味着清单一空抽屉就整个不可达——「已忽略」更是从来没有
              自己的胶囊，把待识别全忽略掉之后就再也回不去了。这里恒在，
              计数为 0 时也不隐藏：归档随时可查，空清单本身也是有效答案 */}
          {canManage && (
          <>
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
          {capabilities.naming && (
            <DropdownMenu.Item
              onSelect={onOrganize}
              disabled={busy && !organizing}
              className={itemClass}
            >
              {organizing
                ? `整理中…${organizePercent === null ? "" : ` ${organizePercent}%`}`
                : "整理文件名"}
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item onSelect={onToggleMetaRefresh} className={itemClass}>
            {refreshingMeta
              ? `停止刷新${metaProgress === null ? "" : ` ${metaProgress}`}`
              : capabilities.scraped
                ? "刷新元数据"
                : capabilities.playable
                  ? "重新读取 NFO 与封面"
                  : "重新生成封面"}
          </DropdownMenu.Item>
          {onChapterImages && (
            <DropdownMenu.Item
              onSelect={onChapterImages}
              disabled={busy || Boolean(chapterJob)}
              className={itemClass}
            >
              {chapterJobLabel(chapterJob)}
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item onSelect={onEdit} disabled={busy} className={itemClass}>
            编辑库
          </DropdownMenu.Item>
          </>
          )}
          {/* 浏览偏好收在菜单里，不占墙上的位置，选完即生效并记住。
              整组共用上面这一条分隔线，别各挂一条挤成几道 */}
          {canManage && <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />}
          {/* 与「全部收藏」页的图廊菜单是同一组（见 video-gallery.tsx）：
              图片库只传密度，图床浏览模式两项都传。排序不在这儿了——它总有
              一个当前值可显示，埋进菜单用户就看不到自己正按什么排，已经提到
              墙控件行上（docs/design/library-filtering.md 5.1.1） */}
          {/* 图床浏览：从顶栏收到这儿来，右上角只留搜索与 ⋯ 两颗。
              文案写的是**点下去会变成的那面墙**，与原来那颗图标键同一口径 */}
          {onGalleryModeChange && (
            <DropdownMenu.Item
              onSelect={() => onGalleryModeChange(!galleryMode)}
              className={itemClass}
            >
              {galleryMode ? "回到海报墙" : "图床浏览"}
            </DropdownMenu.Item>
          )}
          {/* 隐藏的回头路。自动生成的合集删不掉、只能藏，藏了必须找得回来 */}
          {onShowHiddenCollectionsChange && (
            <DropdownMenu.Item
              onSelect={() => onShowHiddenCollectionsChange(!showHiddenCollections)}
              className={itemClass}
            >
              {showHiddenCollections ? "不显示已隐藏的合集" : "显示已隐藏的合集"}
            </DropdownMenu.Item>
          )}
          <WallPrefItems
            grouped={grouped}
            onGroupedChange={onGroupedChange}
            density={density}
            onDensityChange={onDensityChange}
            itemClass={itemClass}
          />
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
 * 该档起点——墙是分页的，"跳到 S"意味着重新取一页，而不是在已加载的几屏里找；
 * 跳过去之后往上滑，S 上面的内容由墙顶哨兵按需补回来（见 WallLoadPrev）。
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
  reversed,
}: {
  index: LibraryIndexEntry[];
  /** 当前视口所在档位；由海报墙滚动锚点实时更新。 */
  active: string | null;
  onJump: (offset: number) => void;
  /** 墙是倒序（Z→A）时字母表跟着倒过来：往下滑墙与往下滑索引条是同一个方向 */
  reversed: boolean;
}) {
  const byInitial = useMemo(() => new Map(index.map((e) => [e.initial, e])), [index]);
  const slots = useMemo(() => (reversed ? [...WALL_INITIALS].reverse() : WALL_INITIALS), [reversed]);
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
    const idx = Math.min(slots.length - 1, Math.max(0, Math.floor(ratio * slots.length)));
    return slots[idx];
  }, [slots]);

  if (index.length === 0) return null;

  const previewEntry = preview ? byInitial.get(preview) : undefined;
  const previewIdx = preview ? slots.indexOf(preview) : -1;

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
      {slots.map((initial) => {
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
          style={{ top: `${((previewIdx + 0.5) / slots.length) * 100}%` }}
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
        {/* 头部：tab + 关闭。padding-top 叠 --safe-top：面板 top-0 贴的是屏幕物理顶边，
            iOS 独立 App（black-translucent + viewport-fit=cover）里状态栏正压在这一行上，
            不让位就会与 tab 胶囊糊在一起（见 globals.css 的安全区说明） */}
        <div className="flex items-center justify-between gap-3 border-b border-white/[0.08] px-5 py-3.5 [padding-top:calc(0.875rem+var(--safe-top))]">
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
          className="menu-surface z-[60] min-w-[9rem] p-1"
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
