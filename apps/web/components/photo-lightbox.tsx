"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  ExpandIcon,
  InfoIcon,
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
 *   - 缩放：滚轮 / 双击放大到 5×，放大后拖拽平移，`0` 复位；
 *   - 翻页：←→ 与两侧按钮（手机上靠滑动，按钮隐去），触屏左右滑；翻到已加载列表末尾且服务端还有下一页
 *     时向外要一页（onReachEnd），拿到后继续翻；
 *   - 缩略条只渲染当前位置前后各 30 张：万张库不铺满 DOM；
 *   - 信息面板（`i`）：文件名、拍摄日期、原图尺寸、大小、格式、路径，按需从
 *     条目详情接口拉，同一张只拉一次；
 *   - 操作：下载原图（同一原图路由加 download 参数）。
 *
 * Portal 到 body，与 ImageLightbox 同一层叠约定。
 */
const STRIP_WINDOW = 30;
const MAX_ZOOM = 5;

function formatDate(iso: string | null): string {
  return iso ?? "—";
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
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  // 屏幕适配图 / 原图各自的就绪态；原图只在放大后才开始加载
  const [screenReady, setScreenReady] = useState(false);
  const [wantOriginal, setWantOriginal] = useState(false);
  const [originalReady, setOriginalReady] = useState(false);
  const [broken, setBroken] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [detail, setDetail] = useState<LibraryItemDetail | null>(null);
  const detailCache = useRef(new Map<number, LibraryItemDetail>());
  const drag = useRef<{ x: number; y: number; pointerId: number } | null>(null);
  const swipe = useRef<number | null>(null);
  // 翻到末尾要下一页的闸门：一页没回来之前不重复要
  const waitingMore = useRef(false);

  const thumbUrl = item ? imageUrl(item.poster_url) : "";
  const fileId = item?.primary_file_id ?? null;
  const screenUrl = fileId != null ? libraryFileOriginalUrl(fileId, { size: "screen" }) : "";
  const fullUrl = fileId != null ? libraryFileOriginalUrl(fileId) : "";
  const fullReady = screenReady || originalReady;

  const resetZoom = useCallback(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  // 换图：复位缩放与加载态
  useEffect(() => {
    resetZoom();
    setScreenReady(false);
    setWantOriginal(false);
    setOriginalReady(false);
    setBroken(false);
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

  // 键盘：Esc 关闭，←/→ 翻页，0 复位缩放，i 信息面板
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
      else if (e.key === "0") resetZoom();
      else if (e.key === "i" || e.key === "I") setInfoOpen((open) => !open);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, resetZoom, step]);

  // 锁住身后页面的滚动
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

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

  // 缩放幅度随滚动量走：鼠标滚轮一格（约 100）≈ 1.2×，触控板的细碎事件各自
  // 只走一点点，一次大幅滚动最多翻倍——比固定步长更跟手
  const onWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const factor = Math.min(2, Math.max(0.5, Math.exp(-e.deltaY * 0.002)));
    setZoom((current) => {
      const next = Math.min(MAX_ZOOM, Math.max(1, current * factor));
      if (next === 1) setPan({ x: 0, y: 0 });
      return next;
    });
  }, []);

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
          {zoom > 1 && (
            <span className="tnum mr-1 rounded-full bg-white/[0.1] px-2 py-0.5 text-caption text-white/70">
              {Math.round(zoom * 100)}%
            </span>
          )}
          <button
            type="button"
            title="适应屏幕 (0)"
            aria-label="适应屏幕"
            onClick={resetZoom}
            className="rounded-full p-2 text-white/70 transition-colors hover:bg-white/[0.12] hover:text-white"
          >
            <ExpandIcon className="size-[18px]" />
          </button>
          {fullUrl && (
            <a
              href={libraryFileOriginalUrl(item.primary_file_id as number, { download: true })}
              title="下载原图"
              aria-label="下载原图"
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

      <div className="flex min-h-0 flex-1">
        {/* 舞台：点空白关闭（未缩放时），滚轮缩放，放大后拖拽 */}
        <div
          className={`relative flex min-h-0 min-w-0 flex-1 touch-none items-center justify-center overflow-hidden px-14 max-md:px-2 ${
            zoom > 1 ? (drag.current ? "cursor-grabbing" : "cursor-grab") : "cursor-zoom-in"
          }`}
          onClick={(e) => {
            if (e.target === e.currentTarget && zoom === 1) onClose();
          }}
          onWheel={onWheel}
          onDoubleClick={(e) => {
            if (e.target === e.currentTarget) return;
            if (zoom > 1) resetZoom();
            else setZoom(2.5);
          }}
          onPointerDown={(e) => {
            if (zoom === 1 || e.target === e.currentTarget) return;
            drag.current = { x: e.clientX - pan.x, y: e.clientY - pan.y, pointerId: e.pointerId };
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!drag.current) return;
            setPan({ x: e.clientX - drag.current.x, y: e.clientY - drag.current.y });
          }}
          onPointerUp={() => {
            drag.current = null;
          }}
          onPointerCancel={() => {
            drag.current = null;
          }}
          onTouchStart={(e) => {
            if (zoom === 1 && e.touches.length === 1) swipe.current = e.touches[0].clientX;
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
                transition: drag.current ? "none" : "transform 120ms ease-out",
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
          {!broken && fullUrl && (!fullReady || (wantOriginal && !originalReady)) && (
            <span className="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full bg-black/50 px-2.5 py-0.5 text-micro tracking-wide text-white/50">
              {fullReady ? "正在加载原图" : "正在加载"}
            </span>
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

        {/* 信息面板 */}
        {infoOpen && (
          <aside className="w-[300px] shrink-0 overflow-y-auto border-l border-white/[0.08] bg-[rgba(16,18,26,0.85)] px-5 py-4 max-md:hidden">
            <h3 className="mb-3 text-caption font-semibold uppercase tracking-[0.06em] text-[var(--text-faint)]">
              拍摄信息
            </h3>
            <dl className="grid grid-cols-[72px_1fr] gap-x-3 gap-y-2 text-sub">
              {infoRows.map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="text-[var(--text-faint)]">{label}</dt>
                  <dd className="tnum m-0 break-all text-white/90">{value}</dd>
                </div>
              ))}
            </dl>
            {infoOpen && detail === null && (
              <p className="mt-3 text-caption text-[var(--text-faint)]">正在读取文件信息…</p>
            )}
          </aside>
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
