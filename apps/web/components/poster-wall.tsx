"use client";

import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";

import { useTileWindow } from "@/components/photo-wall";
import { PosterCardVisual, type PosterVisualItem } from "@/components/poster-card";
import { libraryCardAction } from "@/components/library-view";
import type { LibraryItem } from "@/lib/api/libraries";
import { cardVariantFor, imageUrl } from "@/lib/image-proxy";
import { formatLibraryInventorySummary } from "@/lib/library-inventory-summary";
import {
  layoutPosterGrid,
  type GridPlacement,
  type PosterGridSpec,
} from "@/lib/wall-window";

/**
 * 影视库 / 其他库的海报墙。
 *
 * 原先是一张 CSS grid（`repeat(auto-fill, minmax(148px, 1fr))`），已加载的条目
 * **全部**上墙，靠 `content-visibility:auto` 跳过视口外格子的布局与绘制。问题是
 * 它只省绘制：节点、`<img>`、解码位图一个都不少。实测（模拟 4× 降速的手机、
 * 414×896）：
 *
 *   1000 格  节点 11462  滚动 54.2fps  丢帧 0.10  主线程 0.39
 *   2000 格  节点 22906  滚动 51.6fps  丢帧 0.14  主线程 0.64
 *   5000 格  节点 57241  滚动 34.2fps  丢帧 0.43  主线程 0.94  ← 帧预算吃光了
 *
 * 所以改成只挂视口附近的格子，与相册瀑布流、图廊同一套（`useTileWindow`：
 * 共享滚动广播 + 距离预算）。要虚拟化就得知道「第 n 行的 y」，而 CSS 的 `auto`
 * 行高只有浏览器知道，于是坐标改成自己算（`lib/wall-window.ts` 的
 * `layoutPosterGrid`，带单测）。列数与列宽照搬 auto-fill 的口径，行高逐行算——
 * 一行里有格子带「文件缺失」提示就整行高一点，与 CSS `auto` 行高同语义，
 * **观感与改之前完全一致**。
 *
 * 唯一需要实测的是「一格的文字区有多高」（标题 + 元信息）：那是文字，高度随
 * 字号档位走，写死 px 既是魔法数字也扛不住断点切换。用两枚隐藏的探针格量
 * （一枚普通、一枚带缺失提示），ResizeObserver 兜住断点 / 缩放 / 字号变化。
 */

/** 墙的两种列宽：竖版海报（电影库）与横版缩略图（其他库 / 未识别区）。 */
export const WALL_GRID_POSTER =
  "grid gap-x-4 gap-y-7 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]";
export const WALL_GRID_WIDE =
  "grid gap-x-4 gap-y-7 [grid-template-columns:repeat(auto-fill,minmax(220px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:[grid-template-columns:repeat(auto-fill,minmax(160px,1fr))]";

/**
 * 上面那两个 class 的数值口径，给自己算坐标的墙用。
 *
 * 两边必须一致：class 决定「改之前长什么样」，这里决定「改之后长什么样」。
 * 移动端断点（max-md，768px 以下）的列宽与间距都更小，与 class 里一一对应。
 */
const GRID_SPEC: Record<"poster" | "wide", { desktop: Omit<PosterGridSpec, "cellHeight">; mobile: Omit<PosterGridSpec, "cellHeight"> }> = {
  poster: {
    desktop: { minColumn: 148, gapX: 16, gapY: 28 },
    mobile: { minColumn: 140, gapX: 12, gapY: 20 },
  },
  wide: {
    desktop: { minColumn: 220, gapX: 16, gapY: 28 },
    mobile: { minColumn: 160, gapX: 12, gapY: 20 },
  },
};
/** Tailwind 的 md 断点：以下走 max-md 那套列宽与间距 */
const MOBILE_MAX = 768;

/** 当前是不是 max-md 档（与 CSS 媒体查询同一口径，不看容器宽） */
function useIsMobileWall(): boolean {
  const [mobile, setMobile] = useState(false);
  useLayoutEffect(() => {
    const query = window.matchMedia(`(max-width: ${MOBILE_MAX - 0.02}px)`);
    const sync = () => setMobile(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);
  return mobile;
}

/** 一格的框比例：调用方钉死了就照它，否则按本格主图形态自选（竖 2:3 / 横 16:9） */
function frameAspectOf(item: LibraryItem, forced: number | undefined): number {
  return forced ?? (item.primary_aspect >= 1 ? 16 / 9 : 2 / 3);
}

/** 这一格要不要在海报下方多一行「文件缺失」提示 */
function hasAbnormalLabel(item: LibraryItem): boolean {
  return item.missing_count > 0;
}


/** 库存格：真实拥有的作品。点击进**媒体库条目详情**（本地刮削信息 +
 *  片源规格 + 条目操作），不再复用发现页的 TMDB 详情；格下标注库存概况。
 *
 *  memo 化 + reload 的逐条目引用复用（见 lib/poll-reconcile.ts）：轮询快照
 *  里没变化的条目沿用旧对象，这里比对通过就整格跳过——大库轮询时只有真正
 *  变化的格子会重渲染。「全部收藏」页复用同一格（跨库的收藏各带自己的落点库）。 */
export const InventoryCell = memo(function InventoryCell({
  item,
  libraryId,
  workingLabel,
  frameAspect,
  measuring = false,
  showRating = false,
}: {
  item: LibraryItem;
  libraryId: number;
  /** 这一格正被后台处理（整库刷新的阶段 / 扫描补探）时的文案；不在处理为 undefined */
  workingLabel?: string;
  /**
   * 强制锁定框比例。单库页的墙已按主图比例切成竖横两区，每区内比例天然一致，
   * 不传即按本格主图自选（竖 2:3 / 横 16:9）；而「全部收藏」是跨库按时间排的
   * 一面墙，不能为了对齐去打散收藏顺序，只能由调用方把整面墙钉死在一个比例上。
   */
  frameAspect?: number;
  /**
   * 探针格：只为量高度而渲染的那两枚（见 PosterWall）。不挂位置锚点——滚动恢复与
   * 「回到上次位置」都按 data-library-item-id 认这一屏是哪几部，探针混进去会把
   * 它们指到一部并不在墙上的作品。
   */
  measuring?: boolean;
  /**
   * 在年份后面常显评分。只在用户正按评分排序 / 用评分筛选时开：那时评分就是他
   * 正在比的东西，不印出来只能一部部点进去看。平时浏览不印（见 ratingText）
   */
  showRating?: boolean;
}) {
  const inventoryLabel =
    item.kind === "tv" && item.inventory_summary
      ? formatLibraryInventorySummary(item.inventory_summary)
      : null;
  // 评分：浏览墙默认不印在海报上——与 Netflix 同一个判断，每格多一个数字整面墙
  // 就吵，还会让人只盯着分数挑片；评分是对某一部起了兴趣之后才要的信息。
  // 所以它只出现在「多看一眼」的地方：悬停信息层，以及按评分排序/筛选时的副行
  const ratingText =
    item.rating != null && item.rating > 0 ? `★ ${item.rating.toFixed(1)}` : null;
  const visual: PosterVisualItem = {
    // 本地条目没有 TMDB id：占位 id 只做 key，不会被当成外部 id 请求
    id: item.tmdb_id != null ? String(item.tmdb_id) : `local:${item.media_item_id}`,
    source: "tmdb",
    type: item.kind === "video" || item.kind === "photo" ? undefined : item.kind,
    title: item.title,
    year: item.year ?? undefined,
    rating: 0,
    // 框比例按分区锁死（竖版 2:3 / 横版 16:9），同一分区里每格等高、片名一条线；
    // 主图真实比例另传，和框不一致的（4:3 封面、1.5 的横版海报）模糊铺底居中完整显示，
    // 不按各自真实比例撑格——那样一行里 1.5 与 1.78 的封面高度不一，片名参差。
    // 调用方给了 frameAspect 就一切照它来（竖横混排的墙，见上面参数注释）
    aspect: frameAspect ?? (item.primary_aspect >= 1 ? 16 / 9 : 2 / 3),
    imageAspect: item.primary_aspect,
    overlayDetails: inventoryLabel ? { primary: inventoryLabel } : undefined,
    // 副行已经常显评分时，悬停层就别再印一遍
    overlayMeta: showRating ? undefined : (ratingText ?? undefined),
    extent: showRating ? (ratingText ?? undefined) : undefined,
    favorite: item.is_favorite,
    // 海报可能是本地刮削资产的相对路径（断网可用），也可能是 TMDB 图床地址。
    // 与首页海报墙同样取派生图（竖版 poster-card / 横版 landscape-card）：海报墙
    // 是全站最大的一张图片网格，直出原图等于每屏多拉三倍字节（见 library-view.tsx
    // 同名字段的注释）
    posterUrl: imageUrl(item.poster_url, cardVariantFor(item.primary_aspect)),
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
    // 视口外的格子根本不挂（PosterWall 的虚拟化窗口），因此不再需要
    // content-visibility 去跳过绘制——它只能省绘制，省不掉节点与解码位图。
    //
    // 但 contain:paint 要单独留下：content-visibility:auto 一直隐含着它，卡片
    // 那层 shadow-[0_10px_28px] 因此被裁在格子边界上。去掉之后投影会漫到相邻
    // 格子上——实测整墙 6~9% 的像素跟着变。那是既有观感的一部分，这次只做性能，
    // 不顺手改画面（要放开投影是另一件事，得单独看效果）
    <div
      // 位置锚点：会话内的滚动恢复（lib/use-scroll-restoration.ts）与跨会话的
      // 「回到上次位置」（lib/library-wall-recall.ts）都按它认这一屏是哪几部
      data-library-item-id={measuring ? undefined : item.media_item_id}
      style={{ contain: "paint" }}
    >
      {/* 后台正在处理的那一格自己点亮：进度面板/胶囊列的是总数或片名，
          海报墙上也要能一眼看到"正在弄这部"，否则用户得在两处之间对片名 */}
      <div className="relative">
        <div className={dead ? "opacity-50 grayscale" : undefined}>
          {/* 触屏的「首点展开信息层」只给剧集的库存概况：电影的信息层只剩一行评分，
              为它让每部电影都多点一下才进得了详情，不值——触屏看评分去详情页 */}
          <PosterCardVisual
            item={visual}
            href={`/library/${libraryId}/item/${item.media_item_id}` as Route}
            action={libraryCardAction(item)}
            revealInfoOnTouch={Boolean(inventoryLabel)}
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
 * 探针：两枚只为量高度而渲染的格子（一枚普通、一枚带缺失提示）。
 *
 * 量的是「文字区高」与「缺失提示那一行有多高」——两者都与列宽、与海报比例无关
 * （标题和元信息各一行、都 truncate），所以量一次就能给整面墙用，列宽变了也不必
 * 重量。用 `visibility:hidden` 而不是 `display:none`：后者不参与布局，量不出高度。
 * 探针不给海报地址，渲染的是占位底，不会多发一个图片请求。
 */
const PROBE_WIDTH = 148;

function useCellMetrics(sample: LibraryItem | undefined, frameAspect: number | undefined) {
  const [metrics, setMetrics] = useState<{ caption: number; abnormal: number } | null>(null);
  const normalRef = useRef<HTMLDivElement>(null);
  const abnormalRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const normal = normalRef.current;
    const abnormal = abnormalRef.current;
    if (!normal || !abnormal) return;
    const measure = () => {
      // 海报框高是算得出来的（探针宽固定、比例已知），差额就是文字区
      const posterHeight = PROBE_WIDTH / frameAspectOf(sample!, frameAspect);
      // 用 getBoundingClientRect 而不是 offsetHeight：后者取整，而海报框高是小数，
      // 一相减就把零点几像素的误差带进每一行，几百行下来墙高会差出一屏
      const caption = normal.getBoundingClientRect().height - posterHeight;
      const extra = abnormal.getBoundingClientRect().height - normal.getBoundingClientRect().height;
      if (caption <= 0) return; // 还没排上版
      setMetrics((current) =>
        current && Math.abs(current.caption - caption) < 0.5 && Math.abs(current.abnormal - extra) < 0.5
          ? current
          : { caption, abnormal: Math.max(0, extra) },
      );
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    // 断点切换 / 浏览器缩放 / 用户改字号都会让文字区变高，探针一直看着
    const observer = new ResizeObserver(measure);
    observer.observe(normal);
    observer.observe(abnormal);
    return () => observer.disconnect();
  }, [sample, frameAspect]);

  const probes = sample ? (
    <div
      aria-hidden="true"
      className="pointer-events-none invisible absolute left-0 top-0"
      style={{ width: PROBE_WIDTH }}
    >
      <div ref={normalRef}>
        <InventoryCell
          item={{ ...sample, poster_url: null, missing_count: 0 }}
          libraryId={0}
          frameAspect={frameAspect}
          measuring
        />
      </div>
      <div ref={abnormalRef}>
        <InventoryCell
          item={{ ...sample, poster_url: null, missing_count: 1, file_count: 2 }}
          libraryId={0}
          frameAspect={frameAspect}
          measuring
        />
      </div>
    </div>
  ) : null;

  return { metrics, probes };
}

export function PosterWall<T extends LibraryItem>({
  items,
  libraryIdOf,
  wide,
  frameAspect,
  workingLabelOf,
  onGeometry,
  showRating = false,
}: {
  /** 已加载的条目（服务端给的顺序） */
  items: T[];
  /** 这一格点进哪个库的条目详情：单库页是常量，「全部收藏」是跨库的逐条目 */
  libraryIdOf: (item: T) => number;
  /** 用宽列（其他库的 16:9 抓帧）还是窄列（竖版海报） */
  wide: boolean;
  /** 把整面墙的框比例钉死（「全部收藏」是跨库一面墙，不能按各自主图形态排） */
  frameAspect?: number;
  workingLabelOf?: (item: T) => string | undefined;
  /**
   * 把算好的行位置交出去。拼音索引条要据此求当前字母——虚拟化之后视口外的
   * 格子不在 DOM 里，靠锚点元素反查已经行不通了；而"当前字母"要按**滚动容器**
   * 的顶边算，那个容器只有库页知道，所以计算留在调用方
   */
  onGeometry?: (geometry: { rowTops: readonly number[]; columns: number } | null) => void;
  /** 在年份后面常显评分（按评分排序 / 筛选时由调用方打开，见 InventoryCell） */
  showRating?: boolean;
}) {
  // 容器宽度：ResizeObserver 驱动重排；首帧用 layout effect 量一次，避免闪一下空墙
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? el.clientWidth);
      setWidth((current) => (current === next ? current : next));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const { metrics, probes } = useCellMetrics(items[0], frameAspect);
  // 断点按**视口**宽判，与 Tailwind 的 max-md 同一口径——容器宽还要减掉页面
  // 左右留白，拿它去比会在 768 附近判错一档，列宽与间距整套对不上
  const mobile = useIsMobileWall();
  const base = GRID_SPEC[wide ? "wide" : "poster"][mobile ? "mobile" : "desktop"];

  const layout = useMemo(() => {
    if (width === 0 || !metrics) return null;
    const columns = Math.max(
      1,
      Math.floor((width + base.gapX) / (base.minColumn + base.gapX)),
    );
    const cellWidth = (width - base.gapX * (columns - 1)) / columns;
    // 一格的高 = 海报框（列宽 ÷ 比例，纯算术）+ 文字区（实测）。混排的墙里
    // 竖横两种比例的格子高度不同，靠 extraOf 把行内最高的那个算进行高
    const heightOf = (item: LibraryItem) =>
      cellWidth / frameAspectOf(item, frameAspect) + metrics.caption;
    let shortest = Infinity;
    for (const item of items) shortest = Math.min(shortest, heightOf(item));
    return {
      ...layoutPosterGrid(
        items.length,
        width,
        { ...base, cellHeight: shortest },
        (i) =>
          heightOf(items[i]) - shortest + (hasAbnormalLabel(items[i]) ? metrics.abnormal : 0),
      ),
      cellWidth,
    };
  }, [items, width, base, metrics, frameAspect]);

  const wallRef = useRef<HTMLDivElement>(null);
  const placements = useMemo<readonly GridPlacement[]>(() => layout?.placements ?? [], [layout]);
  const [from, to] = useTileWindow(wallRef, placements);
  const visible = useMemo(() => items.slice(from, to), [items, from, to]);

  const reportRef = useRef(onGeometry);
  reportRef.current = onGeometry;
  useEffect(() => {
    reportRef.current?.(layout ? { rowTops: layout.rowTops, columns: layout.columns } : null);
  }, [layout]);

  return (
    <div ref={containerRef} className="relative min-w-0">
      {probes}
      {layout && (
        <div ref={wallRef} className="relative" style={{ height: layout.height }}>
          {visible.map((item, i) => {
            const placement = placements[from + i];
            return (
              <PositionedCell
                key={item.media_item_id}
                item={item}
                libraryId={libraryIdOf(item)}
                frameAspect={frameAspect}
                workingLabel={workingLabelOf?.(item)}
                showRating={showRating}
                x={placement.x}
                y={placement.y}
                width={placement.width}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * 定位壳：位置拆成三个数字传，`memo` 才拦得住。
 *
 * 高度不写死——格子的自然高度就是它该有的高度，写死反而会在文字区实测值与
 * 真实排版差一两像素时把内容裁掉。行距由算出来的 y 保证。
 */
const PositionedCell = memo(function PositionedCell({
  item,
  libraryId,
  frameAspect,
  workingLabel,
  showRating,
  x,
  y,
  width,
}: {
  item: LibraryItem;
  libraryId: number;
  frameAspect?: number;
  workingLabel?: string;
  showRating?: boolean;
  x: number;
  y: number;
  width: number;
}) {
  return (
    <div
      className="absolute"
      // 用 left/top 而不是 transform 定位：transform 会给格子造一个合成层，
      // 里面的文字改走灰度抗锯齿，字形像素与改前不一样（几何完全相同，肉眼
      // 看是"字重变了一点点"）。相册瀑布流用 transform 是因为它的重排要走
      // CSS 过渡，这面墙没有那回事。
      //
      // 位置与宽度都不取整：CSS grid 的列宽是小数（834px 视口下是 184.5），
      // 取整会让海报高多出 0.75px、标题整体下移一行像素
      style={{ left: x, top: y, width }}
    >
      <InventoryCell
        item={item}
        libraryId={libraryId}
        frameAspect={frameAspect}
        workingLabel={workingLabel}
        showRating={showRating}
      />
    </div>
  );
});
