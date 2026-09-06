"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { PosterImage } from "@/components/poster-image";
import type { LibraryItem } from "@/lib/api/libraries";
import { imageUrl } from "@/lib/image-proxy";

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
 *   - 瓦片绝对定位 + transform，密度切换 / 窗口变宽时重排走 CSS 过渡。
 *
 * 数据仍是分页追加的（与海报墙同一套 loadMore），布局对已加载部分是最终的：
 * 后追加的条目只会落在同月末尾或新的月份段里，前面的瓦片不动。
 */

export type PhotoWallDensity = "compact" | "standard" | "loose";

/** 目标列宽（px）：列数 = floor((容器宽 + 间距) / (目标列宽 + 间距))，至少两列 */
const TARGET_COLUMN_WIDTH: Record<PhotoWallDensity, number> = {
  compact: 170,
  standard: 230,
  loose: 310,
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
): { placements: Placement[]; height: number; columns: number } {
  const columns = Math.max(2, Math.floor((containerWidth + gap) / (targetColumnWidth + gap)));
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

/**
 * 月份跳转轨道：与海报墙的 A-Z 索引条同一位置、同一交互（点档名跳到该档
 * 第一格），档名换成月份、按年分组。月份数量不定，不像 26 个字母那样等分，
 * 所以是可滚动的列表而不是按比例换算的滑条。
 */
export function PhotoMonthIndex({
  index,
  active,
  onJump,
}: {
  index: readonly { initial: string; count: number; offset: number }[];
  active: string | null;
  onJump: (offset: number) => void;
}) {
  const years = useMemo(() => {
    const out: { year: string; months: { initial: string; count: number; offset: number }[] }[] = [];
    for (const entry of index) {
      const year = entry.initial === "未知" ? "未知" : entry.initial.slice(0, 4);
      const last = out[out.length - 1];
      if (last && last.year === year) last.months.push(entry);
      else out.push({ year, months: [entry] });
    }
    return out;
  }, [index]);
  if (index.length === 0) return null;
  return (
    <nav
      aria-label="按月份跳转"
      // top-20：让开页头右上角悬浮的「⋯」操作按钮（滚动后它仍固定在那一角）
      className="scroll-none sticky top-20 max-h-[calc(100dvh-96px)] w-14 shrink-0 select-none overflow-y-auto text-micro leading-none max-md:hidden"
    >
      {years.map((group) => (
        <div key={group.year} className="mb-2">
          <div className="tnum px-1.5 pb-1 pt-1 font-semibold text-[var(--text-faint)]">
            {group.year}
          </div>
          {group.months.map((entry) => {
            const label = entry.initial === "未知" ? "未知" : `${Number(entry.initial.slice(5))} 月`;
            const isActive = entry.initial === active;
            return (
              <button
                key={entry.initial}
                type="button"
                title={`${formatPhotoMonth(entry.initial)} · ${entry.count} 张`}
                onClick={() => onJump(entry.offset)}
                className={`tnum block w-full rounded-md px-1.5 py-1 text-left transition-colors ${
                  isActive
                    ? "bg-white/[0.14] font-semibold text-white"
                    : "text-[var(--text-muted)] hover:bg-white/[0.08] hover:text-white"
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
      ))}
    </nav>
  );
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
  monthCounts,
  onOpen,
  workingLabelOf,
}: {
  /** 已加载的条目，按内容时间倒序（服务端 sort=release_date 的顺序） */
  items: LibraryItem[];
  density: PhotoWallDensity;
  /** 月份索引给出的全库每月张数（未加载的月份也能显示总数）；缺省只按已加载数 */
  monthCounts?: ReadonlyMap<string, number>;
  /** 点击某张：传的是它在 items 里的下标（灯箱按同一列表翻页） */
  onOpen: (index: number) => void;
  workingLabelOf?: (item: LibraryItem) => string | undefined;
}) {
  // 按月切段。服务端按内容时间倒序时同月天然连续；但库页首轮可能先按默认的
  // 标题序拉一页再切到时间序，那一瞬间同月不连续——按月归并（而不是按连续段切）
  // 保证每个月只有一段、section 的 key 唯一，否则 React 会留下重复的瓦片
  const groups = useMemo(() => {
    const byMonth = new Map<string, { item: LibraryItem; index: number }[]>();
    items.forEach((item, index) => {
      const month = photoMonthOf(item);
      const bucket = byMonth.get(month);
      if (bucket) bucket.push({ item, index });
      else byMonth.set(month, [{ item, index }]);
    });
    return Array.from(byMonth, ([month, entries]) => ({ month, entries }));
  }, [items]);

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

  const targetColumnWidth = TARGET_COLUMN_WIDTH[density];

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
            targetColumnWidth={targetColumnWidth}
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
  targetColumnWidth,
  onOpen,
  workingLabelOf,
}: {
  month: string;
  total?: number;
  /** 本月的条目及其在整份已加载列表里的下标（灯箱按下标翻页） */
  entries: { item: LibraryItem; index: number }[];
  width: number;
  targetColumnWidth: number;
  onOpen: (index: number) => void;
  workingLabelOf?: (item: LibraryItem) => string | undefined;
}) {
  const layout = useMemo(() => {
    const aspects = entries.map(({ item }) => item.primary_aspect);
    const masonry = layoutMasonry(aspects, width, targetColumnWidth);
    // 照片数少于列数：瀑布流会退化成孤柱，改一行等高（见 layoutSparseRow）
    return aspects.length < masonry.columns
      ? layoutSparseRow(aspects, width, targetColumnWidth)
      : masonry;
  }, [entries, width, targetColumnWidth]);
  const count = total ?? entries.length;
  return (
    // data-wall-initial：月份段的首部锚点，海报墙的滚动联动据此点亮索引条上的月份
    <section data-wall-initial={month} className="mb-8 last:mb-0">
      <div className="mb-3 flex items-baseline gap-2.5">
        <h3 className="text-on-image text-body-lg font-semibold text-white/85">
          {formatPhotoMonth(month)}
        </h3>
        <span className="tnum text-caption text-[var(--text-faint)]">{count} 张</span>
      </div>
      <div className="relative" style={{ height: layout.height }}>
        {entries.map(({ item, index }, i) => (
          <PhotoTile
            key={item.media_item_id}
            item={item}
            placement={layout.placements[i]}
            onOpen={() => onOpen(index)}
            workingLabel={workingLabelOf?.(item)}
          />
        ))}
      </div>
    </section>
  );
});

const PhotoTile = memo(function PhotoTile({
  item,
  placement,
  onOpen,
  workingLabel,
}: {
  item: LibraryItem;
  placement: Placement;
  onOpen: () => void;
  workingLabel?: string;
}) {
  const badge = extremeLabel(item.primary_aspect);
  const date = formatDate(item.release_date);
  const size = item.resolutions[0] ?? null;
  const dead = item.file_count > 0 && item.missing_count >= item.file_count;
  return (
    // 绝对定位 + transform：重排走 CSS 过渡；content-visibility:auto 让视口外的
    // 瓦片跳过绘制——尺寸是显式的，不需要 intrinsic-size 占位
    <button
      type="button"
      aria-label={`查看 ${item.title}`}
      onClick={onOpen}
      className="group/tile absolute left-0 top-0 block overflow-hidden rounded-xl bg-[#141824] text-left shadow-[0_8px_22px_rgba(0,0,0,0.35)] ring-1 ring-white/[0.07] transition-[transform,width,height,box-shadow] duration-300 ease-out [content-visibility:auto] hover:z-[2] hover:shadow-[0_18px_44px_rgba(0,0,0,0.6)] hover:ring-white/25 focus-visible:z-[2] focus-visible:ring-2 focus-visible:ring-[var(--accent)] motion-reduce:transition-none"
      style={{
        transform: `translate(${Math.round(placement.x)}px, ${Math.round(placement.y)}px)`,
        width: Math.round(placement.width),
        height: Math.round(placement.height),
      }}
    >
      <PosterImage
        src={imageUrl(item.poster_url)}
        alt={item.title}
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
