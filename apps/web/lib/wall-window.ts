/**
 * 三面墙的几何与可视窗口计算（相册瀑布流 / 图廊 / 海报墙共用）。
 *
 * 三面墙都是「先算坐标再上墙」：每一格的 x/y/宽/高在渲染前就定好，容器高度也
 * 显式写出来。因此只挂视口附近那几格（虚拟化）不需要任何测量，也不会影响
 * 滚动条与滚动位置——只要能算出「这一段里哪几格落在视口附近」。这里就是那段
 * 算术，与 React 无关，单独放出来是为了能直接测。
 *
 * 关键前提：**placements 的 y 是非降序的**。瀑布流每次都往最矮的一列放，而
 * "最矮列的高度"只增不减；海报墙是按行铺的，y 更是逐行递增。二分因此成立。
 */

export interface TilePlacement {
  y: number;
  height: number;
}

/** 第一个 y ≥ 目标值的下标（要求 placements 的 y 非降序） */
export function lowerBoundY(placements: readonly TilePlacement[], y: number): number {
  let lo = 0;
  let hi = placements.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (placements[mid].y < y) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * 本段该挂哪一段瓦片，返回 [起, 止) 的下标区间。
 *
 * @param sectionTop 段顶相对视口顶边的位置（负值 = 段顶已滑到视口上方）
 * @param viewportHeight 视口高度
 * @param overscan 视口上下各多留的距离，快速滑动时不至于露出空档
 * @param tallest 本段最高的一块瓦片的高度
 *
 * 上沿要多退一整块 ``tallest``：二分只能定位「y 不小于某值的第一块」，而正好
 * 跨在带子上沿的那块 y 比带子上沿更小，不退就会把它漏掉——表现为滚动时
 * 视口顶部缺一格。高度参差（全景 / 长图）时这个漏洞尤其明显。
 */
export function tileWindowRange(
  placements: readonly TilePlacement[],
  sectionTop: number,
  viewportHeight: number,
  overscan: number,
  tallest: number,
): readonly [number, number] {
  if (placements.length === 0) return [0, 0];
  const from = lowerBoundY(placements, -sectionTop - overscan - tallest);
  const to = lowerBoundY(placements, -sectionTop + viewportHeight + overscan);
  return [from, to];
}

/* —— 海报墙的网格几何 ——
 *
 * 海报墙原先是 CSS grid（`repeat(auto-fill, minmax(148px, 1fr))` + `auto` 行高）。
 * 要虚拟化就得知道「第 n 行的 y」，而 CSS 的 `auto` 行高只有浏览器自己知道；
 * 换成 `grid-auto-rows: 定值` 又会抹掉「这一行里有格子带『文件缺失』提示、整行
 * 跟着高一点」的现有行为。所以改成自己算坐标——列数与列宽照搬 auto-fill 的
 * 口径，行高逐行算：同一行里最高的那格说了算，行内各格仍然等高、片名一条线。
 */

export interface GridPlacement extends TilePlacement {
  x: number;
  width: number;
}

export interface PosterGridSpec {
  /** 目标最小列宽，对应 CSS 里 minmax(148px, 1fr) 的那个 148 */
  minColumn: number;
  gapX: number;
  gapY: number;
  /**
   * 一格的基准高度：海报框 + 下方的标题与元信息。
   *
   * 只有它需要实测——标题区是文字，高度随字号档位走，写死 px 既是魔法数字也
   * 扛不住断点切换。列宽变了它也要重量（海报框高 = 列宽 ÷ 比例）。
   */
  cellHeight: number;
}

/** 列数：与 CSS `repeat(auto-fill, minmax(minColumn, 1fr))` 同一口径 */
export function posterGridColumns(containerWidth: number, spec: PosterGridSpec): number {
  return Math.max(1, Math.floor((containerWidth + spec.gapX) / (spec.minColumn + spec.gapX)));
}

/**
 * 把 count 个格子按行铺开。
 *
 * @param extraOf 第 i 格额外需要的高度（「N 个文件缺失」那一行）。同一行取最大值
 *                ——CSS grid 的 auto 行高就是这个语义，这样改前改后一模一样。
 */
export function layoutPosterGrid(
  count: number,
  containerWidth: number,
  spec: PosterGridSpec,
  extraOf?: (index: number) => number,
): { placements: GridPlacement[]; height: number; columns: number; rowTops: number[] } {
  const columns = posterGridColumns(containerWidth, spec);
  const width = (containerWidth - spec.gapX * (columns - 1)) / columns;
  const placements: GridPlacement[] = [];
  const rowTops: number[] = [];
  let y = 0;
  for (let start = 0; start < count; start += columns) {
    rowTops.push(y);
    const end = Math.min(start + columns, count);
    let extra = 0;
    if (extraOf) {
      for (let i = start; i < end; i += 1) extra = Math.max(extra, extraOf(i));
    }
    const height = spec.cellHeight + extra;
    for (let i = start; i < end; i += 1) {
      placements.push({ x: (i - start) * (width + spec.gapX), y, width, height });
    }
    y += height + spec.gapY;
  }
  return { placements, height: count === 0 ? 0 : y - spec.gapY, columns, rowTops };
}

/**
 * 拼音索引条的当前字母：按算好的行位置求，不再依赖挂在格子上的 DOM 锚点。
 *
 * 虚拟化之后视口外的格子根本不在 DOM 里，锚点自然也就没了；而每一格在第几行、
 * y 是多少本来就是算出来的，直接算反而更省——每帧不必再查 DOM。
 *
 * @param anchors 各字母首部条目在**本窗口内**的下标（整份排序的 offset 减去窗口起点）
 * @param scrollTop 墙顶相对滚动容器可视区顶边的偏移取负；也就是"已经滑过去多少"
 */
export function activeInitialAt(
  anchors: readonly { initial: string; index: number }[],
  rowTops: readonly number[],
  columns: number,
  scrolled: number,
  fallback: string | null,
): string | null {
  let active = fallback;
  for (const anchor of anchors) {
    const top = rowTops[Math.floor(anchor.index / columns)];
    if (top === undefined || top > scrolled) break;
    active = anchor.initial;
  }
  return active;
}
