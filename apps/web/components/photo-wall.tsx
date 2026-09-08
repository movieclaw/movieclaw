"use client";

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";

import { PosterImage } from "@/components/poster-image";
import type { LibraryItem } from "@/lib/api/libraries";
import { imageUrl, type ImageVariant } from "@/lib/image-proxy";
import { tileWindowRange } from "@/lib/wall-window";

/**
 * 图片库的瀑布流墙（docs/design/library-photo-kind.md 3.2）。
 *
 * 与海报墙的区别只有一件事：混合长宽比。海报墙的格子比例是"一库一种"（2:3 或
 * 16:9），CSS grid 就能排；照片的比例每张不同，得自己算位置：
 *
 *   - **比例在渲染前已知**（`primary_aspect` 由扫描入账时读的原图尺寸得来），
 *     每张的高度 = 列宽 / 比例，放进当前最短的一列——Pinterest 的原版算法。
 *     不等图片加载，布局零抖动，也就能用 content-visibility 做虚拟化；
 *   - **极端比例夹到 [0.5, 2]**：全景图与长截图不能撑爆一列，超出的部分
 *     object-cover 裁掉，角标提示「全景 / 长图」，全屏时看完整原图；
 *   - **按月分组**：照片库的第一心智是"什么时候拍的"，整库一条瀑布流会把顺序
 *     打散；每个月一段标题 + 一面墙，段内再走最短列。月份来自条目的
 *     `release_date`（EXIF 拍摄日），与服务端的月份索引同一口径；
 *   - 瓦片绝对定位 + transform，密度切换 / 窗口变宽时重排走 CSS 过渡；
 *   - **只挂视口附近的瓦片**（虚拟化，见 useTileWindow）：几千张照片的库里，
 *     整墙上墙意味着几千个 DOM 节点、几千张解码位图，滑到后面手机就撑不住了。
 *     位置在渲染前已经算好，段的高度是显式的，虚拟化不需要任何测量。
 *
 * 数据仍是分页追加的（与海报墙同一套 loadMore），布局对已加载部分是最终的：
 * 后追加的条目只会落在同月末尾或新的月份段里，前面的瓦片不动。
 */

export type PhotoWallDensity = "compact" | "standard" | "loose";

/**
 * 三档密度各自的目标列宽、最少列数与间距。
 *
 * 只按目标列宽算列数在窄窗口上会失效：600px 以下三档都落到"至少两列"，点了
 * 没有任何变化；桌面上也只差一列，看不出来。所以每档还各自定最少列数与间距：
 * 紧凑在手机上也是三列、间距 6px，宽松在手机上是单列大图、间距 18px——
 * 任何宽度下三档都是三种明显不同的画面。
 */
export interface DensitySpec {
  /** 目标列宽（px）：列数 = floor((容器宽 + 间距) / (目标列宽 + 间距)) */
  column: number;
  minColumns: number;
  gap: number;
  /** 瓦片取哪个规格的图：列宽 ≤230 CSS px 用 480px 的 photo-tile 派生图（2x 屏够用），
   *  宽松密度列宽更大，直接用 720px 的缩略图本体 */
  variant: ImageVariant | undefined;
}
export const DENSITY: Record<PhotoWallDensity, DensitySpec> = {
  // 间距是「一面墙」与「一堆卡片」的分界：留白一宽，视线就被格线切碎，
  // 沉浸感没了。三档各自砍掉约一半（用户反馈 2026-09-07），仍保持
  // 紧凑 < 标准 < 宽松的梯度
  compact: { column: 150, minColumns: 3, gap: 3, variant: "photo-tile" },
  standard: { column: 230, minColumns: 2, gap: 6, variant: "photo-tile" },
  loose: { column: 340, minColumns: 1, gap: 10, variant: undefined },
};
const GAP = 12;
const MIN_ASPECT = 0.5;
const MAX_ASPECT = 2;

const DENSITY_STORAGE_KEY = "movieclaw.photo-wall.density";

/** 读写密度偏好：只是浏览器内的便利设置，读不到就用标准 */
export function usePhotoWallDensity(): [PhotoWallDensity, (next: PhotoWallDensity) => void] {
  const [density, setDensity] = useState<PhotoWallDensity>("standard");
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(DENSITY_STORAGE_KEY);
      if (stored === "compact" || stored === "standard" || stored === "loose") setDensity(stored);
    } catch {
      /* 隐私模式等拿不到 storage：保持默认 */
    }
  }, []);
  const update = useCallback((next: PhotoWallDensity) => {
    setDensity(next);
    try {
      window.localStorage.setItem(DENSITY_STORAGE_KEY, next);
    } catch {
      /* 同上 */
    }
  }, []);
  return [density, update];
}

/** 条目的月份档名（与服务端 build_library_index 的 release_date 分档同口径） */
export function photoMonthOf(item: Pick<LibraryItem, "release_date">): string {
  return item.release_date ? item.release_date.slice(0, 7) : "未知";
}

export function formatPhotoMonth(month: string): string {
  if (month === "未知") return "日期未知";
  const [year, mon] = month.split("-");
  return `${year} 年 ${Number(mon)} 月`;
}

interface Placement {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** 最短列放置：返回每张的位置与整面墙的高度。 */
export function layoutMasonry(
  aspects: readonly number[],
  containerWidth: number,
  targetColumnWidth: number,
  gap = GAP,
  minColumns = 2,
): { placements: Placement[]; height: number; columns: number } {
  const columns = Math.max(
    minColumns,
    Math.floor((containerWidth + gap) / (targetColumnWidth + gap)),
  );
  const columnWidth = (containerWidth - gap * (columns - 1)) / columns;
  const heights = new Array<number>(columns).fill(0);
  const placements = aspects.map((raw) => {
    const aspect = Math.min(MAX_ASPECT, Math.max(MIN_ASPECT, raw || 1));
    const height = columnWidth / aspect;
    let column = 0;
    for (let i = 1; i < columns; i += 1) {
      if (heights[i] < heights[column] - 0.5) column = i;
    }
    const placement = {
      x: column * (columnWidth + gap),
      y: heights[column],
      width: columnWidth,
      height,
    };
    heights[column] += height + gap;
    return placement;
  });
  const height = aspects.length === 0 ? 0 : Math.max(...heights) - gap;
  return { placements, height, columns };
}

/**
 * 稀疏月份（照片数少于列数）的排版：一行等高、限高。
 *
 * 最短列算法对 1–2 张的月份会退化成"一根孤柱"：一张 1:2 的长截图在 238px 的列里
 * 就是 475px 高，右边几列全空，首屏看起来像没内容。零星截图、单张转存在真实
 * 相册里很常见（没有 EXIF 的图按修改时间归到当月，往往就是一两张）。这种月份
 * 改成一行等高：高度取「填满整行所需」与「上限」的较小者，每张按比例给宽度，
 * 既不撑高也不撑爆。上限 = 目标列宽 × 1.2，与相邻月份的瀑布流视觉体量接近。
 */
export function layoutSparseRow(
  aspects: readonly number[],
  containerWidth: number,
  targetColumnWidth: number,
  gap = GAP,
): { placements: Placement[]; height: number } {
  const ratios = aspects.map((raw) => Math.min(MAX_ASPECT, Math.max(MIN_ASPECT, raw || 1)));
  const sum = ratios.reduce((a, b) => a + b, 0);
  const fitHeight = (containerWidth - gap * (ratios.length - 1)) / Math.max(sum, 0.01);
  const height = Math.min(targetColumnWidth * 1.2, fitHeight);
  let x = 0;
  const placements = ratios.map((ratio) => {
    const placement = { x, y: 0, width: ratio * height, height };
    x += ratio * height + gap;
    return placement;
  });
  return { placements, height: ratios.length === 0 ? 0 : height };
}

/* —— 瀑布流的虚拟化 ——
 *
 * 几千张照片的库里，「整墙都上墙」在手机上是撑不住的：一张瓦片是一个 <button>
 * ＋ 一个 <img> ＋两层浮层，5000 张就是三万多个 DOM 节点；滑过的图还全留着
 * 解码位图（实测滑 40 屏后 366MB 的解码内存）。而人只看当前这一屏。
 *
 * 这面墙做虚拟化几乎不用付代价：位置在渲染前就由 layoutMasonry 算好了，段的
 * 高度是显式写在 style 上的，挂不挂瓦片都不影响滚动条与滚动位置。区间算术
 * 在 lib/wall-window.ts（可单测），这里只负责「什么时候重算」。
 */

/** 视口上下各多挂这么多屏：快速滑动时不至于露出空档，图也有提前量去取 */
const OVERSCAN_SCREENS = 1.5;

/**
 * 一面墙上所有段共用的滚动广播。
 *
 * 每段各自监听 scroll 的话，几十段就是几十个监听器与几十次 rAF。这里合成一个：
 * 用**捕获阶段**监听 window 的 scroll——库页真正滚动的是内部容器，scroll 事件
 * 不冒泡，只有捕获收得到；这样也不必把滚动容器一路传进每一段。
 *
 * 广播时附带「这一帧滑了多少像素」。远处的段据此决定这次要不要真去量
 * （见 useTileWindow 的 budget）：十年的相册有两百多个月份段，每段每帧都读一次
 * 布局，光这一项就要吃掉三成帧预算。滑动距离是唯一能让段与视口的相对位置
 * 发生变化的东西，所以按它计费既省得准、又不会漏——一次跳转（回到上次位置、
 * 时间刻度跳月）会一次性把所有预算吃光，段立刻重新量。
 */
const wallWatchers = new Set<(movedPx: number) => void>();
let wallFrame = 0;
let lastScrollTop: number | null = null;
/** 本帧滑过的距离；量不准（resize、换了滚动容器）时给 Infinity＝所有段都重新量 */
let movedPx = Number.POSITIVE_INFINITY;

function onWallScroll(event: Event) {
  const target = event.target;
  const top =
    target instanceof Element
      ? target.scrollTop
      : (document.scrollingElement?.scrollTop ?? null);
  if (top === null) movedPx = Number.POSITIVE_INFINITY;
  else {
    movedPx = lastScrollTop === null ? Number.POSITIVE_INFINITY : Math.abs(top - lastScrollTop);
    lastScrollTop = top;
  }
  scheduleWallFrame();
}
function onWallResize() {
  // 视口尺寸变了，带子的边界跟着变，谁都不能再吃预算
  movedPx = Number.POSITIVE_INFINITY;
  lastScrollTop = null;
  scheduleWallFrame();
}
function scheduleWallFrame() {
  if (wallFrame) return;
  wallFrame = requestAnimationFrame(() => {
    wallFrame = 0;
    const moved = movedPx;
    movedPx = 0;
    for (const watcher of wallWatchers) watcher(moved);
  });
}
function subscribeWall(watcher: (movedPx: number) => void): () => void {
  if (wallWatchers.size === 0) {
    window.addEventListener("scroll", onWallScroll, { capture: true, passive: true });
    window.addEventListener("resize", onWallResize, { passive: true });
  }
  wallWatchers.add(watcher);
  return () => {
    wallWatchers.delete(watcher);
    if (wallWatchers.size > 0) return;
    window.removeEventListener("scroll", onWallScroll, { capture: true });
    window.removeEventListener("resize", onWallResize);
    if (wallFrame) cancelAnimationFrame(wallFrame);
    wallFrame = 0;
    lastScrollTop = null;
  };
}

/**
 * 立刻让墙上所有段重新量一次虚拟化窗口（同步，不等下一帧）。
 *
 * 给「代码自己改了 scrollTop」的场合用：scroll 事件要到下一帧才来，中间那一帧
 * 挂着的还是旧位置附近的瓦片，看起来就是闪一下空墙。库页向上补页时会把墙已
 * 加载的部分整体往下推、再把 scrollTop 加回去（见 library-detail-view 的前置
 * 加载补偿），补完调一次这里，这一帧就已经是新位置该挂的那几块。
 */
export function remeasureWalls(): void {
  // 复制一份再遍历：量测里会 setState，React 可能顺手让某个段卸载退订
  for (const watcher of [...wallWatchers]) watcher(Number.POSITIVE_INFINITY);
}

/**
 * 本段当前该挂哪一段瓦片：返回 [起, 止) 的下标区间。
 *
 * 一次量测 ＝ 一次 getBoundingClientRect（读段容器本身，不读瓦片里的 <img>
 * ——那个读法在 content-visibility 的格子里每读一次就逼一次全量布局）＋两次
 * 二分。区间没变就不 setState，因此绝大多数帧里整棵树一次重渲染都没有。
 *
 * 远处的段按**距离预算**跳过量测：量完一次就记下「离带子还有多远」，之后每帧
 * 扣掉滑过的距离，扣光了才重新量。这不是近似——滑动是段与视口相对位置变化的
 * 唯一来源，所以离带子 8000px 的段在页面又滑了 8000px 之前，不可能需要改窗口。
 * 十年的相册有两百多个月份段，每段每帧都读一次布局要吃掉三成帧预算（实测
 * 五万张时滚动主线程占用 57%），按预算跳过之后回到 35%，且不引入任何延迟。
 */
export function useTileWindow(
  containerRef: RefObject<HTMLElement | null>,
  placements: readonly Placement[],
): readonly [number, number] {
  const [range, setRange] = useState<readonly [number, number]>([0, 0]);
  // 当前区间也存一份在 ref 里，就为了「没变就一次 setState 都不发」。
  // 用 setRange(current => current) 是不够的：即便返回同一个值，React 也可能
  // 先把这个组件重渲一遍再决定跳过。一面墙上两百多段、每秒 60 帧，那是每秒
  // 上万次白跑的组件渲染——五万张时滚动的主线程占用有一半出在这里。
  const rangeRef = useRef(range);
  // 二分只能定位「y 不小于某值的第一块」，而跨在带子上沿的那块 y 更小。
  // 往回退一整块最高瓦片的高度，保证它也在区间里
  const tallest = useMemo(
    () => placements.reduce((max, p) => (p.height > max ? p.height : max), 0),
    [placements],
  );

  // layout effect：首帧就把窗口量出来，否则会先画一帧空段再补上瓦片，
  // 看起来像闪了一下（段容器只在容器宽度量到之后才渲染，不会跑在服务端）
  useLayoutEffect(() => {
    const commit = (next: readonly [number, number]) => {
      if (rangeRef.current[0] === next[0] && rangeRef.current[1] === next[1]) return;
      rangeRef.current = next;
      setRange(next);
    };
    // 还可以再让页面滑多少像素才需要重新量（见上）
    let budget = 0;
    const measure = (movedPx = Number.POSITIVE_INFINITY) => {
      budget -= movedPx;
      if (budget > 0) return;
      const el = containerRef.current;
      if (!el || placements.length === 0) {
        commit([0, 0]);
        return;
      }
      // 段顶相对视口的位置：负值表示段顶已经滑到视口上方
      const rect = el.getBoundingClientRect();
      const overscan = window.innerHeight * OVERSCAN_SCREENS;
      commit(tileWindowRange(placements, rect.top, window.innerHeight, overscan, tallest));
      // 本段离「视口 ± 提前量」这条带子还有多远：在带子里就是 0（下一帧照常量），
      // 在带子外就是下一次量测之前可以放心滑过的距离
      budget = Math.max(0, rect.top - (window.innerHeight + overscan), -overscan - rect.bottom);
    };
    measure();
    return subscribeWall(measure);
  }, [containerRef, placements, tallest]);

  return range;
}

/**
 * 悬浮时间刻度（Google Photos 式）：覆在墙的右缘、不占宽度，平时几乎不可见，
 * 滚动中或把鼠标移到右缘时浮现。
 *
 * 之前是一条常驻的月份列表放在墙旁边，占 56px 宽还一直亮着，浏览照片时是个
 * 干扰。这里换成零宽的占位列 + sticky 的刻度条向左负边距叠在墙上：墙用满整个
 * 宽度，刻度只在需要时出现。刻度按每月张数**按比例**分布（张数多的月份占的
 * 刻度段长），年份标签立在该年第一个月处；悬停某段浮出「2026 年 8 月 · 16 张」
 * 气泡，点击跳到该月第一张；当前所在月份常亮。
 */
export function PhotoTimelineScrubber({
  index,
  active,
  scrollElement,
  onJump,
}: {
  index: readonly { initial: string; count: number; offset: number }[];
  active: string | null;
  /** 真实滚动容器：滚动时刻度浮现，停下 1.4 秒后隐去 */
  scrollElement: HTMLElement | null;
  onJump: (offset: number) => void;
}) {
  const [scrolling, setScrolling] = useState(false);
  const [hover, setHover] = useState<string | null>(null);
  useEffect(() => {
    if (!scrollElement) return;
    let timer = 0;
    const onScroll = () => {
      setScrolling(true);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setScrolling(false), 1400);
    };
    scrollElement.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      scrollElement.removeEventListener("scroll", onScroll);
      window.clearTimeout(timer);
    };
  }, [scrollElement]);

  const total = useMemo(() => index.reduce((sum, entry) => sum + entry.count, 0), [index]);
  const segments = useMemo(() => {
    let acc = 0;
    return index.map((entry) => {
      const year = entry.initial === "未知" ? null : entry.initial.slice(0, 4);
      const start = acc / Math.max(total, 1);
      acc += entry.count;
      return { ...entry, year, start, size: entry.count / Math.max(total, 1) };
    });
  }, [index, total]);
  if (index.length === 0) return null;
  const shown = scrolling || hover !== null;
  const hovered = hover ? segments.find((s) => s.initial === hover) : undefined;

  return (
    // 零宽占位列：墙不为它让路；刻度条向左负边距叠在墙的右缘（-ml-11 = 自身
    // 宽 36px + 与墙之间的 8px 列间距，右缘恰与墙的右缘对齐）
    <div className="relative w-0 shrink-0 self-stretch max-md:hidden">
      <nav
        aria-label="按月份跳转"
        onPointerEnter={() => setHover((h) => h ?? "")}
        onPointerLeave={() => setHover(null)}
        // 浮现时带一层向右加深的暗色渐变垫底：刻度与年份叠在亮色照片上也读得清
        className={`sticky top-24 -ml-11 flex h-[calc(100dvh-160px)] w-9 flex-col justify-stretch select-none rounded-l-lg bg-gradient-to-r from-transparent via-[rgba(6,8,14,0.55)] to-[rgba(6,8,14,0.85)] transition-opacity duration-300 ${
          shown ? "opacity-100" : "opacity-0 hover:opacity-100"
        }`}
      >
        {segments.map((seg) => {
          const isActive = seg.initial === active;
          const isHover = seg.initial === hover;
          return (
            <button
              key={seg.initial}
              type="button"
              aria-label={`${formatPhotoMonth(seg.initial)} · ${seg.count} 张`}
              onPointerEnter={() => setHover(seg.initial)}
              onClick={() => onJump(seg.offset)}
              className="group/tick relative flex min-h-[10px] items-start justify-end pr-2"
              style={{ flexGrow: Math.max(seg.size, 0.02), flexBasis: 0 }}
            >
              {/* 年份标签：立在该年第一个月处 */}
              {seg.year && (segments.find((s) => s.year === seg.year) === seg) && (
                <span className="tnum pointer-events-none absolute right-2 top-0 -translate-y-1/2 text-[10px] font-semibold leading-none text-white/85 [text-shadow:0_1px_2px_rgba(0,0,0,0.8)]">
                  {seg.year}
                </span>
              )}
              <span
                className={`mt-2 h-px transition-all ${
                  isActive || isHover
                    ? "w-4 bg-white"
                    : "w-2 bg-white/55 group-hover/tick:bg-white/85"
                }`}
              />
            </button>
          );
        })}
        {/* 气泡：悬停段的月份与张数，浮在刻度条左侧 */}
        {hovered && (
          <span
            className="tnum pointer-events-none absolute right-10 whitespace-nowrap rounded-full bg-[rgba(16,18,26,0.92)] px-2.5 py-1 text-caption font-semibold text-white shadow-[0_6px_18px_rgba(0,0,0,0.5)] ring-1 ring-white/[0.12]"
            style={{ top: `calc(${(hovered.start + hovered.size / 2) * 100}% - 12px)` }}
          >
            {formatPhotoMonth(hovered.initial)} · {hovered.count} 张
          </span>
        )}
      </nav>
    </div>
  );
}

/** 墙上的一条：条目本身 ＋ 它在整份已加载列表里的下标（灯箱按下标翻页） */
interface WallEntry {
  item: LibraryItem;
  index: number;
}

/** 两轮的月份内容是不是同一批（逐个引用相等即可，不比内容） */
function sameEntries(a: readonly WallEntry[], b: readonly WallEntry[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i].item !== b[i].item || a[i].index !== b[i].index) return false;
  }
  return true;
}

/** 极端比例的角标：夹紧后被裁的那类 */
function extremeLabel(aspect: number): string | null {
  if (aspect > MAX_ASPECT) return "全景";
  if (aspect < MIN_ASPECT) return "长图";
  return null;
}

function formatDate(iso: string | null): string | null {
  if (!iso) return null;
  const [y, m, d] = iso.split("-");
  return `${y}-${m}-${d}`;
}

export function PhotoWall({
  items,
  density,
  grouped = true,
  monthCounts,
  onOpen,
  workingLabelOf,
}: {
  /** 已加载的条目，按内容时间倒序（服务端 sort=release_date 的顺序） */
  items: LibraryItem[];
  density: PhotoWallDensity;
  /**
   * 是否按月分段（默认分）。选了「最近添加」排序时整墙不再按拍摄时间有序，
   * 同一个月的照片散落在各处，分段只会切出一堆重复的月份标题——那一档走
   * 不分段的一条瀑布流。
   */
  grouped?: boolean;
  /** 月份索引给出的全库每月张数（未加载的月份也能显示总数）；缺省只按已加载数 */
  monthCounts?: ReadonlyMap<string, number>;
  /** 点击某张：传的是它在 items 里的下标（灯箱按同一列表翻页） */
  onOpen: (index: number) => void;
  workingLabelOf?: (item: LibraryItem) => string | undefined;
}) {
  // 上一轮每个月的条目数组。内容没变的月份要沿用上一轮那个数组，理由见下
  const previousMonths = useRef(new Map<string, WallEntry[]>());
  // 按月切段。服务端按内容时间倒序时同月天然连续；但库页首轮可能先按默认的
  // 标题序拉一页再切到时间序，那一瞬间同月不连续——按月归并（而不是按连续段切）
  // 保证每个月只有一段、section 的 key 唯一，否则 React 会留下重复的瓦片
  const groups = useMemo(() => {
    const byMonth = new Map<string, WallEntry[]>();
    if (grouped) {
      for (let index = 0; index < items.length; index += 1) {
        const month = photoMonthOf(items[index]);
        const bucket = byMonth.get(month);
        if (bucket) bucket.push({ item: items[index], index });
        else byMonth.set(month, [{ item: items[index], index }]);
      }
    } else {
      byMonth.set(
        "",
        items.map((item, index) => ({ item, index })),
      );
    }
    // 逐月与上一轮比对，内容一样就把上一轮的数组原样交回去。
    //
    // 为什么值得：灯箱里点一次收藏、滚到底追加一页，动的都只是一个月，但库页
    // 交下来的 items 是整份换了新引用的。不比对的话每个月都会拿到一个新数组
    // ——每一段都白重渲一遍、白算一遍最短列。五万张、两百多段时这一下要 200ms，
    // 人是感觉得到的。比对本身只是逐个引用相等，比重排便宜两个数量级。
    const reused = new Map<string, WallEntry[]>();
    for (const [month, rows] of byMonth) {
      const previous = previousMonths.current.get(month);
      reused.set(month, previous && sameEntries(previous, rows) ? previous : rows);
    }
    previousMonths.current = reused;
    return Array.from(reused, ([month, entries]) => ({ month, entries }));
  }, [items, grouped]);

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

  const spec = DENSITY[density];

  return (
    <div ref={containerRef} className="min-w-0">
      {width > 0 &&
        groups.map((group) => (
          <PhotoMonthSection
            key={group.month}
            month={group.month}
            total={monthCounts?.get(group.month)}
            entries={group.entries}
            width={width}
            spec={spec}
            onOpen={onOpen}
            workingLabelOf={workingLabelOf}
          />
        ))}
    </div>
  );
}

const PhotoMonthSection = memo(function PhotoMonthSection({
  month,
  total,
  entries,
  width,
  spec,
  onOpen,
  workingLabelOf,
}: {
  month: string;
  total?: number;
  /** 本月的条目及其在整份已加载列表里的下标（灯箱按下标翻页） */
  entries: { item: LibraryItem; index: number }[];
  width: number;
  spec: DensitySpec;
  onOpen: (index: number) => void;
  workingLabelOf?: (item: LibraryItem) => string | undefined;
}) {
  const layout = useMemo(() => {
    const aspects = entries.map(({ item }) => item.primary_aspect);
    const masonry = layoutMasonry(aspects, width, spec.column, spec.gap, spec.minColumns);
    // 照片数少于列数：瀑布流会退化成孤柱，改一行等高（见 layoutSparseRow）
    return aspects.length < masonry.columns
      ? layoutSparseRow(aspects, width, spec.column, spec.gap)
      : masonry;
  }, [entries, width, spec]);
  const count = total ?? entries.length;
  const tilesRef = useRef<HTMLDivElement>(null);
  const [from, to] = useTileWindow(tilesRef, layout.placements);
  // 挂在窗口里的那几块。整段的高度写在容器上，没挂的部分照样占着位置
  const visible = useMemo(() => entries.slice(from, to), [entries, from, to]);
  return (
    // data-wall-initial：月份段的首部锚点，海报墙的滚动联动据此点亮索引条上的月份。
    // 不分段时（month 为空）既没有标题也没有锚点，就是一整面墙
    <section data-wall-initial={month || undefined} className="mb-8 last:mb-0">
      {month !== "" && (
        <div className="mb-3 flex items-baseline gap-2.5">
          <h3 className="text-on-image text-body-lg font-semibold text-white/85">
            {formatPhotoMonth(month)}
          </h3>
          <span className="tnum text-caption text-[var(--text-faint)]">{count} 张</span>
        </div>
      )}
      <div ref={tilesRef} className="relative" style={{ height: layout.height }}>
        {visible.map(({ item, index }, i) => {
          const placement = layout.placements[from + i];
          return (
            <PhotoTile
              key={item.media_item_id}
              item={item}
              index={index}
              x={placement.x}
              y={placement.y}
              width={placement.width}
              height={placement.height}
              variant={spec.variant}
              onOpen={onOpen}
              workingLabel={workingLabelOf?.(item)}
            />
          );
        })}
      </div>
    </section>
  );
});

/**
 * 一张瓦片。
 *
 * 位置拆成四个数字、点击给稳定的 ``onOpen`` ＋ 自己的下标，都是为了让 ``memo``
 * 真的生效（与图廊的瓦片同一套口径）：传 placement 对象（每次重排都是新引用）
 * 或内联箭头（每次渲染都是新函数）会让父组件的任何一次重渲都穿透到每一块瓦片
 * ——库页光是滚动联动与后台轮询就会重渲好几十次。
 */
const PhotoTile = memo(function PhotoTile({
  item,
  index,
  x,
  y,
  width,
  height,
  variant,
  onOpen,
  workingLabel,
}: {
  item: LibraryItem;
  /** 本瓦片在整份已加载列表里的下标：灯箱按同一列表翻页 */
  index: number;
  x: number;
  y: number;
  width: number;
  height: number;
  variant: ImageVariant | undefined;
  onOpen: (index: number) => void;
  workingLabel?: string;
}) {
  const badge = extremeLabel(item.primary_aspect);
  const date = formatDate(item.release_date);
  const size = item.resolutions[0] ?? null;
  const dead = item.file_count > 0 && item.missing_count >= item.file_count;
  return (
    // 绝对定位 + transform：重排走 CSS 过渡。
    // 这里不再用 content-visibility:auto——墙已经只挂视口附近的瓦片（useTileWindow），
    // 挂上来的本来就要画；而它带来的两个副作用是实打实的：被跳过的子树里
    // <img> 的懒加载与解码完成通知都会失灵（详见 poster-image.tsx 的长注释），
    // 只能靠同步解码兜底，滑动时每张图都在主线程上解码
    <button
      type="button"
      // 位置锚点：会话内的滚动恢复（lib/use-scroll-restoration.ts）与跨会话的
      // 「回到上次位置」（lib/library-wall-recall.ts）都按它认这一屏是哪几张
      data-library-item-id={item.media_item_id}
      aria-label={`查看 ${item.title}`}
      onClick={() => onOpen(index)}
      className="group/tile absolute left-0 top-0 block overflow-hidden rounded-xl bg-[#141824] text-left shadow-[0_8px_22px_rgba(0,0,0,0.35)] ring-1 ring-white/[0.07] transition-[transform,width,height,box-shadow] duration-300 ease-out hover:z-[2] hover:shadow-[0_18px_44px_rgba(0,0,0,0.6)] hover:ring-white/25 focus-visible:z-[2] focus-visible:ring-2 focus-visible:ring-[var(--accent)] motion-reduce:transition-none"
      style={{
        transform: `translate(${Math.round(x)}px, ${Math.round(y)}px)`,
        width: Math.round(width),
        height: Math.round(height),
      }}
    >
      {/* 渐进式加载第一级：列表自带的 16px 微缩图铺底并模糊，缩略图到达前先看到
          照片的大致颜色；缩略图加载完成后盖在上面（缩放 1.1 让模糊边缘不露底） */}
      {item.poster_blur && (
        <span
          aria-hidden="true"
          className="absolute inset-0 scale-110 bg-cover bg-center blur-md"
          style={{ backgroundImage: `url(${item.poster_blur})` }}
        />
      )}
      {/* 挂上来的瓦片必然在视口附近（虚拟化的窗口就是按这个切的），直接取图，
          不必再让 PosterImage 自己逐张探测 —— 那次探测每张要读一次
          getBoundingClientRect，几千张就是几千次强制布局 */}
      <PosterImage
        src={imageUrl(item.poster_url, variant)}
        alt={item.title}
        preload
        className={`absolute inset-0 size-full object-cover transition-transform duration-500 ease-out group-hover/tile:scale-[1.04] motion-reduce:transition-none ${
          dead ? "opacity-50 grayscale" : ""
        }`}
      />
      {badge && (
        <span className="absolute left-2 top-2 rounded-md bg-black/70 px-1.5 py-0.5 text-micro font-bold tracking-wide text-[var(--accent)]">
          {badge}
        </span>
      )}
      {/* 悬停信息层：文件名、拍摄日期、原图尺寸。不用 backdrop-blur（几百张瓦片叠加会拖慢滚动） */}
      <span className="pointer-events-none absolute inset-0 flex flex-col justify-end bg-gradient-to-t from-[rgba(6,8,14,0.85)] via-[rgba(6,8,14,0.25)] to-transparent p-2.5 opacity-0 transition-opacity duration-200 group-hover/tile:opacity-100 group-focus-visible/tile:opacity-100">
        <span className="truncate text-caption font-semibold text-white">{item.title}</span>
        <span className="tnum truncate text-micro text-white/70">
          {[date, size].filter(Boolean).join(" · ")}
        </span>
      </span>
      {workingLabel && (
        <span className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center gap-1.5 bg-[rgba(7,12,20,0.92)] px-2 py-1.5 text-micro font-medium text-[var(--info)]">
          <span className="size-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-[var(--info)]/30 border-t-[var(--info)]" />
          <span className="truncate">{workingLabel}</span>
        </span>
      )}
      {dead && (
        <span className="pointer-events-none absolute inset-x-0 bottom-0 bg-[rgba(7,12,20,0.85)] px-2 py-1 text-micro text-[var(--text-muted)]">
          文件已缺失
        </span>
      )}
    </button>
  );
});
