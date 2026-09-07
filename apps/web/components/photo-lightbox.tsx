"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DownloadIcon, InfoIcon, XIcon } from "@/components/icons";
import { ZoomLightbox, type ZoomLightboxSlide } from "@/components/zoom-lightbox";
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
 * **分页加载的条目列表**，每张有缩略图、屏幕适配图与原图三级、有台账信息。
 * 舞台交互（缩放、手势、翻页、缩略条）全在 ZoomLightbox 里，与影视库图廊的
 * 灯箱共用；本组件只负责图片库特有的两件事：
 *   - 三级地址：墙上的缩略图 → 长边 2048 的屏幕适配图（几百 KB，服务端按原图
 *     惰性派生并缓存）→ 只有放大到 1:1 时才拉几 MB 的原图；下载永远给原图；
 *   - 信息面板（`i`）：文件名、拍摄日期、原图尺寸、大小、格式、路径，按需从
 *     条目详情接口拉，同一张只拉一次。外观照播放器的诊断面板——一块压在画面
 *     左上角的半透明黑，桌面与手机同一套（手机上横向铺满）；
 *   - 下载原图（同一原图路由加 download 参数；iOS 桌面应用里改走系统分享
 *     面板，见 downloadOriginal）。
 */

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
  const [infoOpen, setInfoOpen] = useState(false);
  const [detail, setDetail] = useState<LibraryItemDetail | null>(null);
  /** iOS 桌面应用里下载的进度 / 失败提示；其它环境走浏览器原生下载，恒为 null */
  const [downloadNote, setDownloadNote] = useState<string | null>(null);
  const detailCache = useRef(new Map<number, LibraryItemDetail>());

  const slides = useMemo<ZoomLightboxSlide[]>(
    () =>
      items.map((entry) => {
        const fileId = entry.primary_file_id;
        return {
          key: entry.media_item_id,
          title: entry.title,
          thumbUrl: imageUrl(entry.poster_url),
          screenUrl: fileId != null ? libraryFileOriginalUrl(fileId, { size: "screen" }) : "",
          fullUrl: fileId != null ? libraryFileOriginalUrl(fileId) : undefined,
          aspect: entry.primary_aspect,
        };
      }),
    [items],
  );

  const fileId = item?.primary_file_id ?? null;
  const fullUrl = fileId != null ? libraryFileOriginalUrl(fileId) : "";
  const downloadUrl = fileId != null ? libraryFileOriginalUrl(fileId, { download: true }) : "";

  // 换图：清掉上一张的下载提示
  useEffect(() => {
    setDownloadNote(null);
  }, [index]);

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

  // 快捷键 i：信息面板
  const onKey = useCallback((key: string) => {
    if (key !== "i" && key !== "I") return false;
    setInfoOpen((open) => !open);
    return true;
  }, []);

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
  // 停止冒泡：信息面板上的按下 / 滑动 / 点击都不是舞台手势
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  return (
    <ZoomLightbox
      label={`查看图片：${item.title}`}
      slides={slides}
      index={index}
      hasMore={hasMore}
      onIndexChange={onIndexChange}
      onReachEnd={onReachEnd}
      onClose={onClose}
      note={downloadNote}
      onKey={onKey}
      actions={
        <>
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
        </>
      }
      overlay={
        // 信息面板：照播放器诊断面板的做法压在画面左上角，一块半透明的黑。
        // 桌面固定 300px；手机上横向铺满、限高到舞台一半，超出滚动。
        // 舞台现在铺满整个对话框（控件浮在它上面），所以要自己让开顶栏那一条，
        // 否则面板会压住左上角的计数；限高也要留出底栏（面板开着时控件常显）
        infoOpen ? (
          <div
            data-lightbox-panel
            role="region"
            aria-label="拍摄信息"
            onPointerDown={stop}
            onTouchStart={stop}
            onTouchEnd={stop}
            onClick={stop}
            className="absolute left-[max(0.75rem,var(--safe-left))] top-[calc(3.75rem+var(--safe-top))] z-10 max-h-[calc(100%-12rem)] w-[300px] cursor-auto overflow-y-auto overscroll-contain rounded-[14px] bg-black/70 px-3.5 py-2.5 text-[11.5px] leading-relaxed max-md:right-[max(0.75rem,var(--safe-right))] max-md:w-auto max-md:max-h-[50%]"
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
        ) : null
      }
    />
  );
}
