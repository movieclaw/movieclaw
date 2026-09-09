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
  // —— 二级·找片（作品是什么样的）——
  ratingGte?: number | null;
  /** 片长档：lte60 / 60to90 / 90to120 / gt120 */
  runtimes?: string[];
  /** 原始语言码 */
  languages?: string[];
  // —— 二级·查库（文件是什么规格）——
  resolutions?: string[];
  /** true=只看 HDR / false=只看 SDR / null=都看 */
  hdr?: boolean | null;
  /** 库存状态：missing=有文件失联 / unscraped=没刮到档案 */
  stock?: string[];
}

/** 筛选条件 → 查询串片段。三个接口（items / facets / item-index）共用它，
 *  口径分叉在前端这一侧同样不可能发生。 */
export function filterQuery(filter: LibraryFilter | undefined, query: URLSearchParams): void {
  if (!filter) return;
  if (filter.genres?.length) query.set("g", filter.genres.join(","));
  if (filter.countries?.length) query.set("c", filter.countries.join(","));
  if (filter.decades?.length) query.set("d", filter.decades.join(","));
  if (filter.watch) query.set("w", filter.watch);
  if (filter.ratingGte !== undefined && filter.ratingGte !== null)
    query.set("rating_gte", String(filter.ratingGte));
  if (filter.runtimes?.length) query.set("rt", filter.runtimes.join(","));
  if (filter.languages?.length) query.set("lang", filter.languages.join(","));
  if (filter.resolutions?.length) query.set("res", filter.resolutions.join(","));
  if (filter.hdr !== undefined && filter.hdr !== null) query.set("hdr", String(filter.hdr));
  if (filter.stock?.length) query.set("stock", filter.stock.join(","));
}

/** 筛选是否为空——为空时一切按"未筛选"走（不改 URL、不加指纹、不显示条件行）。 */
export function isFilterEmpty(filter: LibraryFilter | undefined): boolean {
  if (!filter) return true;
  return (
    !filter.genres?.length &&
    !filter.countries?.length &&
    !filter.decades?.length &&
    !filter.watch &&
    (filter.ratingGte === undefined || filter.ratingGte === null) &&
    !filter.runtimes?.length &&
    !filter.languages?.length &&
    !filter.resolutions?.length &&
    (filter.hdr === undefined || filter.hdr === null) &&
    !filter.stock?.length
  );
}


/** 已选条件的条数（「筛选」按钮上的角标）。 */
export function filterCount(filter: LibraryFilter | undefined): number {
  if (!filter) return 0;
  return (
    (filter.genres?.length ?? 0) +
    (filter.countries?.length ?? 0) +
    (filter.decades?.length ?? 0) +
    (filter.watch ? 1 : 0) +
    (filter.ratingGte !== undefined && filter.ratingGte !== null ? 1 : 0) +
    (filter.runtimes?.length ?? 0) +
    (filter.languages?.length ?? 0) +
    (filter.resolutions?.length ?? 0) +
    (filter.hdr !== undefined && filter.hdr !== null ? 1 : 0) +
    (filter.stock?.length ?? 0)
  );
}

/**
 * 合集规则的一条（与后端 ``services/library/collections.py`` 的 ``rules_to_filter``
 * 同一套字段名，也与 ``library.match_rules`` 同构）。
 */
export interface FilterRule {
  field: string;
  op: "any_of";
  values: (string | number | boolean)[];
}

/**
 * 筛选条件 → 合集规则。「筛完存为合集」是一次纯粹的形状转换，不是另一套语义。
 *
 * 字段名必须与后端那份一一对上：对不上的字段后端会**保守忽略**（宁可少收窄也
 * 不误收窄），表现就是"存下来的合集比刚才筛出来的多"——这类偏差没有报错，
 * 只能靠两边共用同一份字段名来杜绝。
 */
export function filterToRules(filter: LibraryFilter | undefined): FilterRule[] {
  if (!filter) return [];
  const rules: FilterRule[] = [];
  const push = (field: string, values: (string | number | boolean)[]) => {
    if (values.length) rules.push({ field, op: "any_of", values });
  };
  push("genres", filter.genres ?? []);
  push("origin_countries", filter.countries ?? []);
  push("decades", filter.decades ?? []);
  push("watch", filter.watch ? [filter.watch] : []);
  push(
    "rating_gte",
    filter.ratingGte !== undefined && filter.ratingGte !== null ? [filter.ratingGte] : [],
  );
  push("runtimes", filter.runtimes ?? []);
  push("languages", filter.languages ?? []);
  push("resolutions", filter.resolutions ?? []);
  push("hdr", filter.hdr !== undefined && filter.hdr !== null ? [filter.hdr] : []);
  push("stock", filter.stock ?? []);
  return rules;
}

/** 合集规则 → 筛选条件（合集详情页要把规则画成可改的条件 chip）。 */
export function rulesToFilter(rules: FilterRule[] | undefined): LibraryFilter {
  const filter: LibraryFilter = {};
  for (const rule of rules ?? []) {
    const values = rule?.values ?? [];
    switch (rule?.field) {
      case "genres":
        filter.genres = values.map((v) => Number(v)).filter((v) => Number.isFinite(v));
        break;
      case "origin_countries":
      case "countries":
        filter.countries = values.map((v) => String(v).toUpperCase());
        break;
      case "decades":
        filter.decades = values.map((v) => String(v));
        break;
      case "watch":
        filter.watch = values.length ? (String(values[0]) as WatchFilter) : null;
        break;
      case "rating_gte":
        filter.ratingGte = values.length ? Number(values[0]) : null;
        break;
      case "runtimes":
        filter.runtimes = values.map((v) => String(v));
        break;
      case "languages":
        filter.languages = values.map((v) => String(v).toLowerCase());
        break;
      case "resolutions":
        filter.resolutions = values.map((v) => String(v));
        break;
      case "hdr":
        filter.hdr = values.length ? Boolean(values[0]) : null;
        break;
      case "stock":
        filter.stock = values.map((v) => String(v));
        break;
      // 未知字段保守忽略：与后端同一条降级策略
    }
  }
  return filter;
}

/**
 * 条件的规范化键：同一组条件无论勾选顺序都得到同一个串，空条件为空串。
 *
 * 两处在用，而且必须是同一份：
 *   - 墙位置记忆的指纹（每种筛选各记各的位置）；
 *   - 判断「当前这面墙是不是正好等于某个合集」——合集 chip 的选中态就是这么
 *     **推导**出来的，不存状态。用户改动任意一条，键不再相等，标记自然消失，
 *     不需要谁去记得清掉它。
 */
export function filterKey(filter: LibraryFilter | undefined): string {
  if (isFilterEmpty(filter)) return "";
  const parts: Record<string, string[] | string | null> = {
    g: (filter?.genres ?? []).map(String),
    c: filter?.countries ?? [],
    d: filter?.decades ?? [],
    w: filter?.watch ?? null,
    r: filter?.ratingGte != null ? String(filter.ratingGte) : null,
    rt: filter?.runtimes ?? [],
    lang: filter?.languages ?? [],
    res: filter?.resolutions ?? [],
    hdr: filter?.hdr == null ? null : String(filter.hdr),
    st: filter?.stock ?? [],
  };
  const pieces: string[] = [];
  for (const key of Object.keys(parts).sort()) {
    const value = parts[key];
    if (!value || (Array.isArray(value) && value.length === 0)) continue;
    pieces.push(`${key}${Array.isArray(value) ? [...value].sort().join("_") : value}`);
  }
  return pieces.join(".");
}
