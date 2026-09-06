"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  InfoIcon,
  MinusIcon,
  PlusIcon,
  XIcon,
} from "@/components/icons";
import {
  type LibraryItem,
  type LibraryItemDetail,
  getLibraryItemDetail,
  libraryFileOriginalUrl,
} from "@/lib/api/libraries";
import { formatBytes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";

/**
 * 图片库的全屏灯箱（docs/design/library-photo-kind.md 3.3）。
 *
 * 与搜索页的 ImageLightbox（一组外链 URL 的浏览器）是两种数据模型：这里翻的是
 * **分页加载的条目列表**，每张有缩略图与原图两级、有台账信息，所以单独成组件，
 * 不往通用灯箱里塞分支：
 *   - 渐进三级：先显示墙上的缩略图（模糊放大）→ 长边 2048 的屏幕适配图
 *     （几百 KB，服务端按原图惰性派生并缓存）→ 只有放大到 1:1 时才拉几 MB 的
 *     原图。相邻两张预加载的也是屏幕适配图；下载永远给原图；
 *   - 缩放：滚轮 / 触控板捏合以鼠标位置为锚，双击 / 双击屏幕在该点放大到 2.5×，
 *     两指捏合以两指中点为锚，最大 5×；放大后拖拽平移；舞台底部一组
 *     「− 比例 +」控件（桌面悬停显现、触屏常显，放大中常显），比例以「适应
 *     屏幕」为 100%，点比例复位；键盘 +/- 缩放、`0` 复位。
 *     手势一律走 Pointer Events 自己判定（双击、捏合都不靠浏览器事件）：
 *     iOS 不派发 dblclick，舞台又必须 touch-action:none 挡住系统的整页缩放，
 *     交给浏览器的话手机上放大缩小就全无反应；
 *   - 翻页：←→ 与两侧按钮（手机上靠滑动，按钮隐去），触屏左右滑；翻到已加载
 *     列表末尾且服务端还有下一页时向外要一页（onReachEnd），拿到后继续翻；
 *   - 缩略条只渲染当前位置前后各 30 张：万张库不铺满 DOM；
 *   - 信息面板（`i`）：文件名、拍摄日期、原图尺寸、大小、格式、路径，按需从
 *     条目详情接口拉，同一张只拉一次。外观照播放器的诊断面板——一块压在画面
 *     左上角的半透明黑，桌面与手机同一套（手机上横向铺满），不再是桌面独有
 *     的侧栏；
 *   - 操作：下载原图（同一原图路由加 download 参数；iOS 桌面应用里改走系统
 *     分享面板，见 downloadOriginal）。
 *
 * Portal 到 body，与 ImageLightbox 同一层叠约定。
 */
const STRIP_WINDOW = 30;
const MIN_ZOOM = 1;
const MAX_ZOOM = 5;
/** 放大 / 缩小按钮与 +/- 键每次的倍率 */
const ZOOM_STEP = 1.5;
/** 双击 / 双击屏幕放大到的倍率 */
const DOUBLE_TAP_ZOOM = 2.5;
/** 两次点按间隔与位移在此以内算双击（触屏没有 dblclick，自己判） */
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_SLOP = 30;
/** 按下到抬起位移超过它就不算点按（是拖拽或滑动） */
const TAP_SLOP = 10;

interface Point {
  x: number;
  y: number;
}

/** 缩放态：倍率 + 平移，一起更新才能保证锚点不漂 */
interface View {
  zoom: number;
  pan: Point;
}

const FIT_VIEW: View = { zoom: MIN_ZOOM, pan: { x: 0, y: 0 } };

function clampZoom(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/**
 * 以舞台上的某点为锚缩放：锚点底下的那块画面在缩放前后停在原地。
 *
 * 图片元素居中于舞台、transform-origin 在自身中心，`anchor` 取相对舞台中心的
 * 坐标；锚点对应的画面坐标是 (anchor - pan) / zoom，要它不动，新的平移就是
 * anchor - (anchor - pan) × (新倍率 / 旧倍率)。缩回 1× 时一并归零平移，
 * 否则「适应屏幕」会停在偏离中心的位置。
 */
function zoomAt(anchor: Point, view: View, nextZoom: number): View {
  const zoom = clampZoom(nextZoom);
  if (zoom === MIN_ZOOM) return FIT_VIEW;
  const ratio = zoom / view.zoom;
  return {
    zoom,
    pan: {
      x: anchor.x - (anchor.x - view.pan.x) * ratio,
      y: anchor.y - (anchor.y - view.pan.y) * ratio,
    },
  };
}

function distance(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function midpoint(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

function formatDate(iso: string | null): string {
  return iso ?? "—";
}

/** iOS「添加到主屏幕」的独立 App 形态（navigator.standalone 是 iOS 独有属性） */
function isIosStandalone(): boolean {
  return (navigator as { standalone?: boolean }).standalone === true;
}

export function PhotoLightbox({
  libraryId,
  items,
  index,
  hasMore,
  onIndexChange,
  onReachEnd,
  onClose,
}: {
  libraryId: number;
  /** 已加载的条目（与墙同一列表、同一顺序） */
  items: LibraryItem[];
  index: number;
  /** 服务端还有下一页：翻到末尾时向外要 */
  hasMore: boolean;
  onIndexChange: (index: number) => void;
  onReachEnd: () => void;
  onClose: () => void;
}) {
  const item = items[index];
  const [view, setView] = useState<View>(FIT_VIEW);
  const { zoom, pan } = view;
  // 屏幕适配图 / 原图各自的就绪态；原图只在放大后才开始加载
  const [screenReady, setScreenReady] = useState(false);
  const [wantOriginal, setWantOriginal] = useState(false);
  const [originalReady, setOriginalReady] = useState(false);
  const [broken, setBroken] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [detail, setDetail] = useState<LibraryItemDetail | null>(null);
  /** iOS 桌面应用里下载的进度 / 失败提示；其它环境走浏览器原生下载，恒为 null */
  const [downloadNote, setDownloadNote] = useState<string | null>(null);
  const detailCache = useRef(new Map<number, LibraryItemDetail>());
  const stageRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // —— 手势状态（都是 ref：手势中每帧更新，不该触发重渲染）——
  /** 当前按在舞台上的所有指针（鼠标 / 手指），捏合靠它凑齐两根手指 */
  const pointers = useRef(new Map<number, Point>());
  /** 拖拽平移：指针位置减去平移量的偏移 */
  const drag = useRef<Point | null>(null);
  /** 捏合：起手时的两指距离与倍率、上一帧的两指中点（中点移动也跟着平移） */
  const pinch = useRef<{ dist: number; zoom: number; mid: Point } | null>(null);
  /** 本次按下的起点：抬起时判断是不是「原地点按」 */
  const tapStart = useRef<{ id: number; t: number; x: number; y: number } | null>(null);
  /** 上一次点按：与本次凑成双击 */
  const lastTap = useRef<{ t: number; x: number; y: number } | null>(null);
  const swipe = useRef<number | null>(null);
  // 翻到末尾要下一页的闸门：一页没回来之前不重复要
  const waitingMore = useRef(false);

  const thumbUrl = item ? imageUrl(item.poster_url) : "";
  const fileId = item?.primary_file_id ?? null;
  const screenUrl = fileId != null ? libraryFileOriginalUrl(fileId, { size: "screen" }) : "";
  const fullUrl = fileId != null ? libraryFileOriginalUrl(fileId) : "";
  const downloadUrl = fileId != null ? libraryFileOriginalUrl(fileId, { download: true }) : "";
  const fullReady = screenReady || originalReady;

  const resetZoom = useCallback(() => setView(FIT_VIEW), []);
  /** 按钮与键盘的缩放：以舞台中心为锚 */
  const zoomBy = useCallback(
    (factor: number) => setView((current) => zoomAt({ x: 0, y: 0 }, current, current.zoom * factor)),
    [],
  );

  /** 视口坐标 → 相对舞台中心的坐标（zoomAt 的锚点约定） */
  const stageAnchor = useCallback((clientX: number, clientY: number): Point => {
    const rect = stageRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: clientX - rect.left - rect.width / 2, y: clientY - rect.top - rect.height / 2 };
  }, []);

  // 换图：复位缩放与加载态
  useEffect(() => {
    resetZoom();
    setScreenReady(false);
    setWantOriginal(false);
    setOriginalReady(false);
    setBroken(false);
    setDownloadNote(null);
  }, [index, resetZoom]);

  // 放大到超过屏幕适配图的分辨率时才拉原图（第三级）
  useEffect(() => {
    if (zoom > 1) setWantOriginal(true);
  }, [zoom]);

  // 列表变长了（要的下一页到了）：闸门放开
  useEffect(() => {
    waitingMore.current = false;
  }, [items.length]);

  const step = useCallback(
    (delta: number) => {
      const next = index + delta;
      if (next < 0) return;
      if (next >= items.length) {
        if (hasMore && !waitingMore.current) {
          waitingMore.current = true;
          onReachEnd();
        }
        return;
      }
      onIndexChange(next);
    },
    [hasMore, index, items.length, onIndexChange, onReachEnd],
  );

  // 快翻到末尾前提前要下一页，翻到最后一张时通常已经到了
  useEffect(() => {
    if (hasMore && index >= items.length - 5 && !waitingMore.current) {
      waitingMore.current = true;
      onReachEnd();
    }
  }, [hasMore, index, items.length, onReachEnd]);

  // 键盘：Esc 关闭，←/→ 翻页，+/- 缩放，0 复位缩放，i 信息面板
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
      else if (e.key === "+" || e.key === "=") zoomBy(ZOOM_STEP);
      else if (e.key === "-") zoomBy(1 / ZOOM_STEP);
      else if (e.key === "0") resetZoom();
      else if (e.key === "i" || e.key === "I") setInfoOpen((open) => !open);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, resetZoom, step, zoomBy]);

  // 锁住身后页面的滚动
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  // 滚轮 / 触控板捏合缩放，以鼠标位置为锚。
  // 必须用原生监听并声明 passive:false：React 把 wheel 挂成 passive 监听，
  // 合成事件里 preventDefault 无效（控制台报 "Unable to preventDefault inside
  // passive event listener"），触控板捏合会被浏览器拿去缩放整页。
  // 缩放幅度随滚动量走：鼠标滚轮一格（约 100）≈ 1.2×，触控板的细碎事件各自
  // 只走一点点，一次大幅滚动最多翻倍——比固定步长更跟手。
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (e: WheelEvent) => {
      // 信息面板自己要滚动，不当缩放
      if (panelRef.current?.contains(e.target as Node)) return;
      e.preventDefault();
      const factor = Math.min(2, Math.max(0.5, Math.exp(-e.deltaY * 0.002)));
      const anchor = stageAnchor(e.clientX, e.clientY);
      setView((current) => zoomAt(anchor, current, current.zoom * factor));
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, [stageAnchor]);

  // 预加载相邻两张的屏幕适配图：翻过去不用再等（不预拉原图，几 MB 一张太贵）
  useEffect(() => {
    for (const neighbor of [index - 1, index + 1]) {
      const neighborFile = items[neighbor]?.primary_file_id;
      if (neighborFile != null) {
        const img = new Image();
        img.src = libraryFileOriginalUrl(neighborFile, { size: "screen" });
      }
    }
  }, [index, items]);

  // 信息面板打开时按需拉条目详情（文件路径、大小、格式都在文件行上）
  useEffect(() => {
    if (!infoOpen || !item) return;
    const cached = detailCache.current.get(item.media_item_id);
    if (cached) {
      setDetail(cached);
      return;
    }
    setDetail(null);
    let cancelled = false;
    getLibraryItemDetail(libraryId, item.media_item_id)
      .then((data) => {
        detailCache.current.set(item.media_item_id, data);
        if (!cancelled) setDetail(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [infoOpen, item, libraryId]);

  // —— 舞台手势：拖拽平移、两指捏合、双击 / 双击屏幕 ——
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // 翻页键、缩放控件上的按下交给按钮自己：舞台一旦捕获指针，click 会被
    // 重定向到舞台，按钮就点不响了
    if ((e.target as Element).closest("button")) return;
    const point = { x: e.clientX, y: e.clientY };
    pointers.current.set(e.pointerId, point);
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { dist: distance(a, b), zoom, mid: midpoint(a, b) };
      drag.current = null;
      tapStart.current = null;
      swipe.current = null;
      return;
    }
    if (pointers.current.size > 2) return;
    tapStart.current = { id: e.pointerId, t: e.timeStamp, ...point };
    if (zoom > 1) {
      drag.current = { x: e.clientX - pan.x, y: e.clientY - pan.y };
      e.currentTarget.setPointerCapture(e.pointerId);
    }
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const active = pinch.current;
    if (active && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()];
      const mid = midpoint(a, b);
      const nextZoom = (active.zoom * distance(a, b)) / active.dist;
      const anchor = stageAnchor(mid.x, mid.y);
      const shift = { x: mid.x - active.mid.x, y: mid.y - active.mid.y };
      active.mid = mid;
      setView((current) => {
        const next = zoomAt(anchor, current, nextZoom);
        if (next.zoom === MIN_ZOOM) return next;
        // 两指整体移动时画面跟着走（捏合的同时也能平移）
        return { zoom: next.zoom, pan: { x: next.pan.x + shift.x, y: next.pan.y + shift.y } };
      });
      return;
    }
    if (drag.current) {
      const offset = drag.current;
      setView((current) => ({ ...current, pan: { x: e.clientX - offset.x, y: e.clientY - offset.y } }));
    }
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
    drag.current = null;
    if (pointers.current.size < 2) pinch.current = null;
    // 双击判定：本次是原地点按，且与上一次点按足够近、足够快。
    // 鼠标也走这条（不用 dblclick）：Android Chrome 双击会同时派发 dblclick，
    // 两条路各放大一次就抵消了，统一只认这一条
    const start = tapStart.current;
    tapStart.current = null;
    if (!start || start.id !== e.pointerId || pointers.current.size > 0) return;
    if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > TAP_SLOP) return;
    const previous = lastTap.current;
    const tap = { t: e.timeStamp, x: e.clientX, y: e.clientY };
    if (
      previous &&
      tap.t - previous.t < DOUBLE_TAP_MS &&
      Math.hypot(tap.x - previous.x, tap.y - previous.y) < DOUBLE_TAP_SLOP
    ) {
      lastTap.current = null;
      const anchor = stageAnchor(tap.x, tap.y);
      setView((current) =>
        current.zoom > 1 ? FIT_VIEW : zoomAt(anchor, current, DOUBLE_TAP_ZOOM),
      );
      return;
    }
    lastTap.current = tap;
  };

  const onPointerCancel = (e: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
    drag.current = null;
    pinch.current = null;
    tapStart.current = null;
  };

  /**
   * 下载原图。浏览器里就是普通的 `<a download>`；iOS 桌面应用（PWA）例外：
   * 独立容器没有标签页，附件响应会把整个 App 视图导航到下载页、没有返回键，
   * 产品页面就此丢失。改走系统分享面板——拉到原图后交给 iOS，用户在面板里
   * 「存储图像」到相册或存到文件，App 留在原地。分享面板要求在用户手势的
   * 有效期内调用（WebKit 给几秒），局域网内几 MB 的原图来得及；超时或环境
   * 不支持就退到新窗口打开，仍不覆盖当前页。
   */
  const downloadOriginal = async (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (!isIosStandalone() || !item) return;
    e.preventDefault();
    const name = detail?.files.find((f) => f.id === fileId)?.file_name ?? item.title;
    setDownloadNote("正在准备下载…");
    try {
      const response = await fetch(fullUrl);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const file = new File([blob], name, { type: blob.type });
      if (navigator.canShare?.({ files: [file] })) {
        await navigator.share({ files: [file] });
      } else {
        window.open(downloadUrl, "_blank");
      }
      setDownloadNote(null);
    } catch (err) {
      // 用户在分享面板里点了取消不是错误
      if (err instanceof DOMException && err.name === "AbortError") {
        setDownloadNote(null);
        return;
      }
      console.warn("下载原图失败：", err);
      setDownloadNote("下载失败，请在 Safari 中打开本站后下载");
      window.setTimeout(() => setDownloadNote(null), 4000);
    }
  };

  const stripRange = useMemo(() => {
    const start = Math.max(0, index - STRIP_WINDOW);
    const end = Math.min(items.length, index + STRIP_WINDOW + 1);
    return { start, end };
  }, [index, items.length]);
  const activeThumb = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    activeThumb.current?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
  }, [index]);

  if (!item) return null;

  const primaryFile = detail?.files.find((f) => f.id === item.primary_file_id) ?? detail?.files[0];
  const infoRows: [string, string][] = [
    ["文件名", primaryFile?.file_name ?? item.title],
    ["拍摄日期", formatDate(item.release_date)],
    ["尺寸", primaryFile?.resolution ?? item.resolutions[0] ?? "—"],
    ["大小", formatBytes(primaryFile?.size_bytes ?? item.total_size_bytes)],
    ["格式", primaryFile?.container ? primaryFile.container.toUpperCase() : "—"],
    ["路径", primaryFile?.file_path ?? "—"],
  ];
  const gesturing = drag.current !== null || pinch.current !== null;
  const loadingNote =
    !broken && fullUrl && (!fullReady || (wantOriginal && !originalReady))
      ? fullReady
        ? "正在加载原图"
        : "正在加载"
      : null;
  const stageNote = downloadNote ?? loadingNote;
  // 停止冒泡：信息面板上的按下 / 滑动 / 点击都不是舞台手势
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`查看图片：${item.title}`}
      className="fixed inset-0 z-[70] flex flex-col bg-[rgba(4,5,9,0.94)] backdrop-blur-md [bottom:calc(-1*var(--vp-overshoot))]"
    >
      {/* 顶栏：计数 + 文件名 + 工具 */}
      <div className="flex shrink-0 items-center gap-3 px-4 py-2.5 text-white/85 [padding-top:calc(0.625rem+var(--safe-top))] max-md:gap-2 max-md:px-3">
        <span className="tnum shrink-0 rounded-full bg-white/[0.1] px-2.5 py-0.5 text-sub">
          {index + 1} / {hasMore ? `${items.length}+` : items.length}
        </span>
        <p className="min-w-0 flex-1 truncate text-center text-ui text-white/70">{item.title}</p>
        <div className="flex shrink-0 items-center gap-1">
          {downloadUrl && (
            <a
              href={downloadUrl}
              download
              title="下载原图"
              aria-label="下载原图"
              onClick={(e) => void downloadOriginal(e)}
              className="rounded-full p-2 text-white/70 transition-colors hover:bg-white/[0.12] hover:text-white"
            >
              <DownloadIcon className="size-[18px]" />
            </a>
          )}
          <button
            type="button"
            title="拍摄信息 (I)"
            aria-label="拍摄信息"
            aria-pressed={infoOpen}
            onClick={() => setInfoOpen((open) => !open)}
            className={`rounded-full p-2 transition-colors hover:bg-white/[0.12] hover:text-white ${
              infoOpen ? "bg-white/[0.12] text-white" : "text-white/70"
            }`}
          >
            <InfoIcon className="size-[18px]" />
          </button>
          <button
            type="button"
            aria-label="关闭 (Esc)"
            onClick={onClose}
            className="rounded-full p-2 text-white/70 transition-colors hover:bg-white/[0.12] hover:text-white"
          >
            <XIcon className="size-5" />
          </button>
        </div>
      </div>

      {/* 舞台：点空白关闭（未缩放时），滚轮 / 捏合 / 双击缩放，放大后拖拽。
          touch-none：系统的整页捏合与双击缩放交给自己判定的手势 */}
      <div
        ref={stageRef}
        className={`group relative flex min-h-0 min-w-0 flex-1 touch-none items-center justify-center overflow-hidden px-14 max-md:px-2 ${
          zoom > 1 ? (drag.current ? "cursor-grabbing" : "cursor-grab") : "cursor-zoom-in"
        }`}
        onClick={(e) => {
          if (e.target === e.currentTarget && zoom === 1) onClose();
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onTouchStart={(e) => {
          // 第二根手指落下即取消滑动翻页：这是捏合
          swipe.current = zoom === 1 && e.touches.length === 1 ? e.touches[0].clientX : null;
        }}
        onTouchEnd={(e) => {
          if (swipe.current === null) return;
          const dx = e.changedTouches[0].clientX - swipe.current;
          swipe.current = null;
          if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
        }}
      >
        {broken ? (
          <div className="rounded-2xl border border-white/[0.12] bg-white/[0.04] px-8 py-10 text-center text-ui text-white/60">
            原图加载失败
            <span className="mt-1 block text-caption text-white/40">
              文件可能已被移动或删除，重新扫描后会更新
            </span>
          </div>
        ) : (
          <div
            className="relative max-h-full max-w-full select-none"
            style={{
              transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              transition: gesturing ? "none" : "transform 120ms ease-out",
            }}
          >
            {/* 三级渐进：缩略图垫底（模糊）→ 屏幕适配图盖上 → 放大后原图再盖上 */}
            {thumbUrl && (
              <img
                src={thumbUrl}
                alt=""
                aria-hidden="true"
                draggable={false}
                className={`max-h-[calc(100dvh-140px)] max-w-full rounded-lg object-contain shadow-[0_24px_80px_rgba(0,0,0,0.8)] transition-[filter] duration-300 ${
                  fullReady ? "invisible absolute inset-0" : "blur-[6px] brightness-90"
                }`}
              />
            )}
            {screenUrl && (
              <img
                key={screenUrl}
                src={screenUrl}
                alt={item.title}
                data-stage="screen"
                draggable={false}
                onLoad={() => setScreenReady(true)}
                onError={() => {
                  // 派生失败（格式 Pillow 不认等）：直接退到原图
                  setWantOriginal(true);
                }}
                className={`max-h-[calc(100dvh-140px)] max-w-full rounded-lg object-contain shadow-[0_24px_80px_rgba(0,0,0,0.8)] ${
                  screenReady && !originalReady ? "" : "absolute inset-0 opacity-0"
                }`}
              />
            )}
            {fullUrl && wantOriginal && (
              <img
                key={fullUrl}
                src={fullUrl}
                alt={item.title}
                data-stage="original"
                draggable={false}
                onLoad={() => setOriginalReady(true)}
                onError={() => setBroken(true)}
                className={`max-h-[calc(100dvh-140px)] max-w-full rounded-lg object-contain shadow-[0_24px_80px_rgba(0,0,0,0.8)] ${
                  originalReady ? "" : "absolute inset-0 opacity-0"
                }`}
              />
            )}
          </div>
        )}

        {/* 缩放控件：桌面悬停显现、触屏常显，放大中常显。比例以适应屏幕为 100% */}
        {!broken && (
          <div
            className={`absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-0.5 rounded-full bg-black/70 p-1 text-white/85 transition-opacity ${
              zoom > 1
                ? "opacity-100"
                : "opacity-0 focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
            }`}
          >
            <button
              type="button"
              title="缩小 (-)"
              aria-label="缩小"
              disabled={zoom <= MIN_ZOOM}
              onClick={() => zoomBy(1 / ZOOM_STEP)}
              className="grid size-7 place-items-center rounded-full transition-colors hover:bg-white/[0.12] hover:text-white disabled:opacity-35 disabled:hover:bg-transparent"
            >
              <MinusIcon className="size-4" />
            </button>
            <button
              type="button"
              title="适应屏幕 (0)"
              aria-label={`当前 ${Math.round(zoom * 100)}%，点击适应屏幕`}
              onClick={resetZoom}
              className="tnum min-w-[52px] rounded-full px-1.5 py-1 text-caption transition-colors hover:bg-white/[0.12] hover:text-white"
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              type="button"
              title="放大 (+)"
              aria-label="放大"
              disabled={zoom >= MAX_ZOOM}
              onClick={() => zoomBy(ZOOM_STEP)}
              className="grid size-7 place-items-center rounded-full transition-colors hover:bg-white/[0.12] hover:text-white disabled:opacity-35 disabled:hover:bg-transparent"
            >
              <PlusIcon className="size-4" />
            </button>
          </div>
        )}
        {stageNote && (
          <span className="pointer-events-none absolute bottom-14 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-full bg-black/50 px-2.5 py-0.5 text-micro tracking-wide text-white/50">
            {stageNote}
          </span>
        )}

        {/* 信息面板：照播放器诊断面板的做法压在画面左上角，一块半透明的黑。
            桌面固定 300px；手机上横向铺满、限高到舞台一半，超出滚动。
            top 不必叠 --safe-top：舞台在顶栏之下，顶栏已经让过状态栏 */}
        {infoOpen && (
          <div
            ref={panelRef}
            role="region"
            aria-label="拍摄信息"
            onPointerDown={stop}
            onTouchStart={stop}
            onTouchEnd={stop}
            onClick={stop}
            className="absolute left-[max(0.75rem,var(--safe-left))] top-3 z-10 max-h-[calc(100%-4.5rem)] w-[300px] cursor-auto overflow-y-auto overscroll-contain rounded-[14px] bg-black/70 px-3.5 py-2.5 text-[11.5px] leading-relaxed max-md:right-[max(0.75rem,var(--safe-right))] max-md:w-auto max-md:max-h-[50%]"
          >
            <div className="mb-1.5 flex items-center justify-between">
              <h3 className="text-[12px] font-semibold text-white/90">拍摄信息</h3>
              <button
                type="button"
                onClick={() => setInfoOpen(false)}
                aria-label="关闭拍摄信息"
                className="-mr-1 grid size-6 place-items-center rounded-full text-white/50 transition-colors hover:bg-white/10 hover:text-white"
              >
                <XIcon className="size-3.5" />
              </button>
            </div>
            <dl className="grid grid-cols-[56px_1fr] gap-x-2.5 gap-y-1">
              {infoRows.map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="text-white/50">{label}</dt>
                  <dd className="tnum m-0 break-all text-white/90">{value}</dd>
                </div>
              ))}
            </dl>
            {detail === null && <p className="mt-1.5 text-white/50">正在读取文件信息…</p>}
          </div>
        )}

        {index > 0 && (
          <button
            type="button"
            aria-label="上一张 (←)"
            onClick={(e) => {
              e.stopPropagation();
              step(-1);
            }}
            className="absolute left-3 top-1/2 -translate-y-1/2 rounded-full bg-white/[0.08] p-2.5 text-white/80 backdrop-blur transition-colors hover:bg-white/[0.18] hover:text-white max-md:hidden"
          >
            <ChevronLeftIcon className="size-6" />
          </button>
        )}
        {(index < items.length - 1 || hasMore) && (
          <button
            type="button"
            aria-label="下一张 (→)"
            onClick={(e) => {
              e.stopPropagation();
              step(1);
            }}
            className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full bg-white/[0.08] p-2.5 text-white/80 backdrop-blur transition-colors hover:bg-white/[0.18] hover:text-white max-md:hidden"
          >
            <ChevronRightIcon className="size-6" />
          </button>
        )}
      </div>

      {/* 底部缩略条：只渲染当前位置前后各 30 张 */}
      <div className="scroll-none shrink-0 overflow-x-auto px-4 py-2.5 [padding-bottom:calc(0.625rem+var(--safe-bottom)+var(--vp-overshoot))] max-md:px-3">
        <div className="mx-auto flex w-max gap-1.5">
          {items.slice(stripRange.start, stripRange.end).map((entry, offset) => {
            const i = stripRange.start + offset;
            const active = i === index;
            return (
              <button
                key={entry.media_item_id}
                ref={active ? activeThumb : undefined}
                type="button"
                aria-label={`查看 ${entry.title}`}
                aria-current={active}
                onClick={() => onIndexChange(i)}
                className={`h-12 shrink-0 overflow-hidden rounded-md transition ${
                  active ? "ring-2 ring-[var(--accent)]" : "opacity-45 hover:opacity-90"
                }`}
                style={{ width: Math.round(48 * Math.min(2, Math.max(0.5, entry.primary_aspect))) }}
              >
                <img
                  src={imageUrl(entry.poster_url)}
                  alt=""
                  loading="lazy"
                  className="h-full w-full bg-white/[0.05] object-cover"
                />
              </button>
            );
          })}
        </div>
      </div>
    </div>,
    document.body,
  );
}
