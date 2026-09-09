/**
 * 海报墙筛选条件的纯逻辑（docs/design/library-filtering.md 3.1/3.4）。
 *
 * 单独成模块而不是塞进 lib/api/libraries.ts：那个文件带 `@/` 路径别名，
 * node --test 解析不了，逻辑就测不到。与 lib/discovery-filters.ts 同一惯例。
 */

/** 观看状态筛选：未看 / 在看 / 已看完是一个划分，收藏与它们正交（单选）。 */
export type WatchFilter = "unwatched" | "watching" | "played" | "favorite";

/**
 * 海报墙的筛选条件（docs/design/library-filtering.md 3.1）。
 *
 * 维内 OR、维间 AND。**筛选态的唯一事实源是 URL**，不进 localStorage——
 * 上次筛的条件在下次打开时还在，是本类产品最经典的困惑来源
 * （"我的片怎么少了一半"）。排序是偏好该记，筛选是意图不该记。
 */
export interface LibraryFilter {
  /** 类型：TMDB genre id（语言无关） */
  genres?: number[];
  /** 地区：ISO 3166-1 二字码 */
  countries?: string[];
  /** 年代档：2020s / 2010s / 2000s / 1990s / earlier */
  decades?: string[];
  watch?: WatchFilter | null;
}

/** 筛选条件 → 查询串片段。三个接口（items / facets / item-index）共用它，
 *  口径分叉在前端这一侧同样不可能发生。 */
export function filterQuery(filter: LibraryFilter | undefined, query: URLSearchParams): void {
  if (!filter) return;
  if (filter.genres?.length) query.set("g", filter.genres.join(","));
  if (filter.countries?.length) query.set("c", filter.countries.join(","));
  if (filter.decades?.length) query.set("d", filter.decades.join(","));
  if (filter.watch) query.set("w", filter.watch);
}

/** 筛选是否为空——为空时一切按"未筛选"走（不改 URL、不加指纹、不显示条件行）。 */
export function isFilterEmpty(filter: LibraryFilter | undefined): boolean {
  if (!filter) return true;
  return (
    !filter.genres?.length &&
    !filter.countries?.length &&
    !filter.decades?.length &&
    !filter.watch
  );
}
