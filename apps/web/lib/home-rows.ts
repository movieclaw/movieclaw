/**
 * 媒体库首页的「行清单」：合并规则、排序预设与命名推荐。
 *
 * 首页 = 每个成员一份有序的行清单；每一行 = 来源 × 排序 × 名字
 * （docs/design/library-home-perspective.md）。清单存在 `ui.preferences.home.rows`
 * （超管写全局配置域、成员写自己的 `member.ui_prefs`）。
 *
 * 与 lib/sidebar-nav.ts 同一条约定：**存下来的是提示，不是契约**。
 *   - 存过的行按存的顺序在前；
 *   - 没存过的内置行、以及每个可见库的默认行，按出厂顺序补在后面——
 *     新版本加的内置行、新建的库，在存过清单的老用户那里也一定看得到；
 *   - 指向已删库 / 不可见合集的行直接忽略，不需要迁移，也不需要清孤儿；
 *   - 空清单 = 出厂布局，「恢复默认」就是存一个空列表。
 *
 * 本模块刻意不 import 任何组件或 `@/` 别名：合并规则是这个功能里唯一会出事的
 * 地方，保持无依赖才能用 node --test 直接单测（见 test/home-rows.test.mjs）。
 */

/** 库行 / 合集行可选的排序档。取值与海报墙的 WallSort 同名（含首页独有的 random），
 *  方向合并在取值里（release_date_asc），不单独存方向。 */
export type HomeRowSort =
  | "added_at"
  | "release_date"
  | "release_date_asc"
  | "last_played"
  | "rating"
  | "random"
  | "title";

/** 「我的收藏」行的排序档。未看优先是它的默认（见 playback_favorites 的 unwatched_first）。 */
export type FavoritesSort =
  | "unwatched_first"
  | "favorited_at"
  | "rating"
  | "title";

/** 服务端存的一行（settings.schemas.HomeRowPref）；除 id 外全部可空，空即默认。 */
export interface HomeRowPref {
  id: string;
  sort?: string | null;
  name?: string | null;
  unwatched?: boolean | null;
  hidden?: boolean | null;
  library_id?: number | null;
  collection_id?: number | null;
}

export interface HomeUiPrefs {
  rows: HomeRowPref[];
}

export type HomeLibraryKind = "movie" | "tv" | "video" | "photo";

/** 合并只用到库的这几个字段（真实的 MediaLibrary 还带统计、扫描状态等）。 */
export interface HomeLibraryLike {
  id: number;
  name: string;
  kind: HomeLibraryKind;
  /** 当前身份能否浏览；超管「仅管理」的库为 false，不上首页 */
  viewer_access: boolean;
  /** 管理页勾了「从首页排除」：不给默认行，用户自己加的行仍尊重 */
  exclude_from_home: boolean;
}

/** 合并只用到合集的这几个字段；传进来的应当已经是当前身份可见的合集。 */
export interface HomeCollectionLike {
  id: number;
  name: string;
  /** 所属库；null = 跨库合集（落点走 /library/c/{id}） */
  library_id: number | null;
  /** 合集自己的默认排序（WallSort 取值），新加合集行时作为初始排序 */
  sort: string;
}

/** 合并后的一行：来源已解析、排序与名字已落到具体值，首页与自定义页直接消费。 */
export type HomeRow =
  | { id: "up-next"; kind: "up-next"; hidden: boolean }
  | { id: "favorites"; kind: "favorites"; hidden: boolean; sort: FavoritesSort }
  | { id: "libraries"; kind: "libraries"; hidden: boolean }
  | {
      id: string;
      kind: "library";
      hidden: boolean;
      sort: HomeRowSort;
      unwatched: boolean;
      /** 用户起的名字；空 = 跟随推荐（rowTitle 会按排序算） */
      name: string;
      library: HomeLibraryLike;
      /** 每库一条的默认行（lib:<id>）：能藏、能改，不能删 */
      builtin: boolean;
    }
  | {
      id: string;
      kind: "collection";
      hidden: boolean;
      sort: HomeRowSort;
      collection: HomeCollectionLike;
    };

/** 排序预设：一个取值 = 一个推荐名 + 一句规则。展开区的单选就是这张表。
 *  `short` 是不带库名的短标签，合集行的单选与自定义页的小字用它。 */
export const SORT_PRESETS: Record<
  HomeRowSort,
  { name: (library: string) => string; short: string; hint: string }
> = {
  added_at: {
    name: (l) => `最近添加的${l}`,
    short: "最近添加",
    hint: "入库时间，新的在前",
  },
  release_date: {
    name: (l) => `最近上映的${l}`,
    short: "最近上映",
    hint: "上映时间，新的在前",
  },
  release_date_asc: {
    name: (l) => `最早上映的${l}`,
    short: "最早上映",
    hint: "上映时间，老的在前",
  },
  last_played: {
    name: (l) => `最近观看的${l}`,
    short: "最近观看",
    hint: "我最近播放过的",
  },
  rating: {
    name: (l) => `评分最高的${l}`,
    short: "评分最高",
    hint: "评分，高的在前",
  },
  random: {
    name: (l) => `随便看看 · ${l}`,
    short: "随便看看",
    hint: "每天换一批",
  },
  title: { name: (l) => `${l} A–Z`, short: "A–Z", hint: "片名" },
};

/** 排序预设按库的 kind 裁剪：评分、上映对家庭录像与照片没有意义，列出来只会选到一行空的。 */
export function sortPresetsFor(kind: HomeLibraryKind): HomeRowSort[] {
  switch (kind) {
    case "photo":
      return ["added_at", "title", "random"];
    case "video":
      return ["added_at", "last_played", "title", "random"];
    default:
      return [
        "added_at",
        "release_date",
        "release_date_asc",
        "last_played",
        "rating",
        "random",
        "title",
      ];
  }
}

/** 合集行的预设：与库行同一组七个，标签不带库名。 */
export const COLLECTION_SORTS: HomeRowSort[] = [
  "added_at",
  "release_date",
  "release_date_asc",
  "last_played",
  "rating",
  "random",
  "title",
];

export const FAVORITES_SORT_PRESETS: Record<
  FavoritesSort,
  { name: string; hint: string }
> = {
  unwatched_first: { name: "未看优先", hint: "没看完的在前，再按收藏时间" },
  favorited_at: { name: "最近收藏", hint: "收藏时间" },
  rating: { name: "评分最高", hint: "评分" },
  title: { name: "片名 A–Z", hint: "片名" },
};

const FAVORITES_SORTS = new Set<string>(Object.keys(FAVORITES_SORT_PRESETS));

function asRowSort(
  value: string | null | undefined,
  allowed: HomeRowSort[],
): HomeRowSort {
  return allowed.includes(value as HomeRowSort)
    ? (value as HomeRowSort)
    : allowed[0];
}

/** 这一行显示的名字：用户起的优先，空则按排序推荐；合集行跟合集名走。 */
export function rowTitle(row: HomeRow): string {
  switch (row.kind) {
    case "up-next":
      return "接下来继续";
    case "favorites":
      return "我的收藏";
    case "libraries":
      return "我的媒体库";
    case "library":
      return row.name || SORT_PRESETS[row.sort].name(row.library.name);
    case "collection":
      return row.collection.name;
  }
}

/** 自定义页里每行的小字：来源 · 排序 · 只看没看过的。行名是用户起的，来源和排序是它的真身。 */
export function rowMeta(row: HomeRow): string {
  switch (row.kind) {
    case "up-next":
      return "内置 · 我正在看的";
    case "favorites":
      return `内置 · ${FAVORITES_SORT_PRESETS[row.sort].name}`;
    case "libraries":
      return "内置 · 管理页的库顺序";
    case "library":
      return [
        `${row.library.name}库`,
        SORT_PRESETS[row.sort].short,
        row.unwatched ? "只看没看过的" : null,
      ]
        .filter(Boolean)
        .join(" · ");
    case "collection":
      return `合集 · ${SORT_PRESETS[row.sort].short}`;
  }
}

/** 新加的行用随机 id：`row:` + 6 位 base36，与库 id、合集 id 都无关，删了库也不会撞。 */
export function newRowId(random: () => number = Math.random): string {
  let slug = "";
  while (slug.length < 6) slug += Math.floor(random() * 36).toString(36);
  return `row:${slug.slice(0, 6)}`;
}

/** 出厂布局：接下来继续 → 我的收藏 → 我的媒体库 → 每个库一行「最近添加」。 */
function defaultRows(libraries: HomeLibraryLike[]): HomeRow[] {
  return [
    { id: "up-next", kind: "up-next", hidden: false },
    {
      id: "favorites",
      kind: "favorites",
      hidden: false,
      sort: "unwatched_first",
    },
    { id: "libraries", kind: "libraries", hidden: false },
    ...libraries
      .filter((library) => !library.exclude_from_home)
      .map(
        (library): HomeRow => ({
          id: `lib:${library.id}`,
          kind: "library",
          hidden: false,
          sort: "added_at",
          unwatched: false,
          name: "",
          library,
          builtin: true,
        }),
      ),
  ];
}

/**
 * 把存下来的清单与当前可见的库、合集合并成首页要渲染的行。
 *
 * `libraries` 传全部库即可（这里按 viewer_access 过滤）；`collections` 传当前身份
 * 可见的合集。认不出的 id、指向不可见来源的行、重复的 id 都静默丢弃。
 */
export function buildHomeRows(
  prefs: HomeUiPrefs | null | undefined,
  libraries: HomeLibraryLike[],
  collections: HomeCollectionLike[],
): HomeRow[] {
  const visible = libraries.filter((library) => library.viewer_access);
  const saved = Array.isArray(prefs?.rows) ? prefs.rows : [];
  const defaults = defaultRows(visible);
  if (saved.length === 0) return defaults;

  const libById = new Map(visible.map((library) => [library.id, library]));
  const colById = new Map(
    collections.map((collection) => [collection.id, collection]),
  );
  const seen = new Set<string>();
  const rows: HomeRow[] = [];

  for (const pref of saved) {
    if (!pref || typeof pref.id !== "string" || seen.has(pref.id)) continue;
    const row = resolveRow(pref, libById, colById);
    if (!row) continue;
    seen.add(pref.id);
    rows.push(row);
  }

  // 没存过的内置行追加在末尾（版本升级新增的入口不能消失）
  for (const row of defaults) {
    if (row.kind === "library" || seen.has(row.id)) continue;
    seen.add(row.id);
    rows.push(row);
  }
  // 没存过的库（新建的、或存清单之后才可见的）补一条默认行，插在最后一条库行之后
  // ——放在队尾会落到合集行后面，"新库的最近添加"混在合集里不像是首页的默认行
  const missing = defaults.filter(
    (row) => row.kind === "library" && !seen.has(row.id),
  );
  if (missing.length > 0) {
    // 一条库行都没有时插在「我的媒体库」之后（出厂布局里库行就跟在它后面）
    let at = rows.length;
    const lastLibrary = rows.map((row) => row.kind).lastIndexOf("library");
    const librariesRow = rows.findIndex((row) => row.kind === "libraries");
    if (lastLibrary >= 0) at = lastLibrary + 1;
    else if (librariesRow >= 0) at = librariesRow + 1;
    rows.splice(at, 0, ...missing);
  }
  return rows;
}

function resolveRow(
  pref: HomeRowPref,
  libById: Map<number, HomeLibraryLike>,
  colById: Map<number, HomeCollectionLike>,
): HomeRow | null {
  const hidden = pref.hidden === true;
  if (pref.id === "up-next") return { id: "up-next", kind: "up-next", hidden };
  if (pref.id === "libraries")
    return { id: "libraries", kind: "libraries", hidden };
  if (pref.id === "favorites") {
    const sort = FAVORITES_SORTS.has(pref.sort ?? "")
      ? (pref.sort as FavoritesSort)
      : "unwatched_first";
    return { id: "favorites", kind: "favorites", hidden, sort };
  }
  if (pref.id.startsWith("lib:")) {
    const library = libById.get(Number(pref.id.slice(4)));
    // 管理员勾了「从首页排除」的库：默认行不出现，存过也一样（管理员的决定优先）；
    // 用户自己加的 row: 仍尊重
    if (!library || library.exclude_from_home) return null;
    return libraryRow(pref, library, true);
  }
  if (!pref.id.startsWith("row:")) return null;
  if (pref.collection_id != null) {
    const collection = colById.get(pref.collection_id);
    if (!collection) return null;
    return {
      id: pref.id,
      kind: "collection",
      hidden,
      sort: asRowSort(pref.sort ?? collection.sort, COLLECTION_SORTS),
      collection,
    };
  }
  if (pref.library_id != null) {
    const library = libById.get(pref.library_id);
    if (!library) return null;
    return libraryRow(pref, library, false);
  }
  return null;
}

function libraryRow(
  pref: HomeRowPref,
  library: HomeLibraryLike,
  builtin: boolean,
): HomeRow {
  const sort = asRowSort(pref.sort, sortPresetsFor(library.kind));
  return {
    id: pref.id,
    kind: "library",
    hidden: pref.hidden === true,
    sort,
    // 「最近观看」只要播过的，与「只看没看过的」互斥：两者同时为真会得到一行按 id
    // 排的没播过的片；以排序为准，开关作废
    unwatched: pref.unwatched === true && sort !== "last_played",
    name: (pref.name ?? "").trim(),
    library,
    builtin,
  };
}

/** 反向：把合并后的行写回可存的形状。只存与默认不同的字段，空即默认。 */
export function rowsToPrefs(rows: HomeRow[]): HomeRowPref[] {
  return rows.map((row): HomeRowPref => {
    const base: HomeRowPref = { id: row.id };
    if (row.hidden) base.hidden = true;
    switch (row.kind) {
      case "favorites":
        if (row.sort !== "unwatched_first") base.sort = row.sort;
        return base;
      case "library":
        if (!row.builtin) base.library_id = row.library.id;
        base.sort = row.sort;
        if (row.unwatched) base.unwatched = true;
        if (row.name) base.name = row.name;
        return base;
      case "collection":
        base.collection_id = row.collection.id;
        base.sort = row.sort;
        return base;
      default:
        return base;
    }
  });
}

/** 新建一条库行（排序取「最近添加」，名字留空跟随推荐）。 */
export function newLibraryRow(
  library: HomeLibraryLike,
  id = newRowId(),
): HomeRow {
  return {
    id,
    kind: "library",
    hidden: false,
    sort: "added_at",
    unwatched: false,
    name: "",
    library,
    builtin: false,
  };
}

/** 新建一条合集行（排序取合集自己的默认）。 */
export function newCollectionRow(
  collection: HomeCollectionLike,
  id = newRowId(),
): HomeRow {
  return {
    id,
    kind: "collection",
    hidden: false,
    sort: asRowSort(collection.sort, COLLECTION_SORTS),
    collection,
  };
}

/** 把第 from 行挪到第 to 位（拖拽落点）；越界或没动原样返回。 */
export function moveRowTo(
  rows: HomeRow[],
  from: number,
  to: number,
): HomeRow[] {
  if (
    from === to ||
    from < 0 ||
    to < 0 ||
    from >= rows.length ||
    to >= rows.length
  )
    return rows;
  const next = rows.slice();
  const [row] = next.splice(from, 1);
  next.splice(to, 0, row);
  return next;
}
