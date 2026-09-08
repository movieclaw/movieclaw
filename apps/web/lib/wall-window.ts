/**
 * 瀑布流的可视窗口计算（photo-wall / video-gallery 两面墙共用）。
 *
 * 两面墙都是「先算坐标再上墙」：每块瓦片的 x/y/宽/高在渲染前就由 layoutMasonry
 * 定好了，段的高度也显式写在容器上。因此只挂视口附近那几块（虚拟化）不需要
 * 任何测量，也不会影响滚动条与滚动位置——只要能算出「这一段里哪几块落在
 * 视口附近」。这里就是那段算术，与 React 无关，单独放出来是为了能直接测。
 *
 * 关键前提：**placements 的 y 是非降序的**。layoutMasonry 每次都往最矮的一列
 * 放，而"最矮列的高度"只增不减，所以后放的瓦片 y 不会比先放的小。二分因此成立。
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
