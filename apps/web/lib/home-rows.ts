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

/**
 * 库行 / 合集行可选的排序档。取值与海报墙的 WallSort 同名（含首页独有的 random）。
 *
 * 方向**不再**合并在取值里：每档有自己的自然方向（见 SORT_PRESETS.naturalAsc），
 * 行上另存一个 `reversed`，与海报墙的 `WallSortState.reversed` 同一语义。
 * 初稿把方向烧进取值（`release_date_asc`），理由是「用户选的是『最近上映』这个
 * 名字，不是『上映时间 + 倒序』」；后来推翻——用户明确要每个指标都能自己定正倒序，
 * 而后端三个取数接口本来就收 `order`，前端没理由替他把这个选项藏起来。
 * 老偏好里存的 `release_date_asc` 在读取时归一成 `release_date` + 反转，不需要迁移。
 */
export type HomeRowSort =
  "added_at" | "release_date" | "last_played" | "rating" | "random" | "title";

/** 方向的人话：请求里的 `order` 取值。 */
export type HomeRowOrder = "asc" | "desc";

/** 「我的收藏」行的排序档。未看优先是它的默认（见 playback_favorites 的 unwatched_first）。 */
export type FavoritesSort =
  "unwatched_first" | "favorited_at" | "rating" | "title";

/** 服务端存的一行（settings.schemas.HomeRowPref）；除 id 外全部可空，空即默认。 */
export interface HomeRowPref {
  id: string;
  sort?: string | null;
  /** 方向；空 = 该档的自然方向。只在反转时才存（与海报墙 orderParam 的约定一致） */
  order?: HomeRowOrder | null;
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
  | {
      id: "favorites";
      kind: "favorites";
      hidden: boolean;
      sort: FavoritesSort;
      /** 是否反转了这一档的自然方向；「未看优先」没有方向，恒为 false */
      reversed: boolean;
    }
  | { id: "libraries"; kind: "libraries"; hidden: boolean }
  | {
      id: string;
      kind: "library";
      hidden: boolean;
      sort: HomeRowSort;
      /** 是否反转了这一档的自然方向（random 没有方向，恒为 false） */
      reversed: boolean;
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
      /** 是否反转了这一档的自然方向（random 没有方向，恒为 false） */
      reversed: boolean;
      /** 用户起的名字；空 = 跟随合集自己的名字（合集改名后这一行自动跟着变） */
      name: string;
      collection: HomeCollectionLike;
    };

/**
 * 一档排序的方向档案：自然方向是不是升序，以及两个方向各自的人话。
 * `null` = 这一档没有方向（随机、未看优先），控件不画方向开关。
 *
 * 方向文案与海报墙的 `SORT_DIRECTIONS`（lib/wall-sort.ts）逐字相同——同一档在墙上
 * 和首页得说同一句话。没有直接 import 那张表：本模块要保持零依赖给 node --test
 * 直接跑，而 wall-sort.ts 带着 React 钩子。改文案请两处一起改。
 */
export interface SortDirection {
  naturalAsc: boolean;
  /** 升序时的人话，如「旧→新」「A→Z」 */
  asc: string;
  /** 降序时的人话，如「新→旧」「Z→A」 */
  desc: string;
}

/**
 * 排序预设：一档 = 指标 + 两个方向各自的推荐名 + 方向档案 + 一句规则。
 * 自定义页的排序菜单里每个有方向的指标列两条（`short(false)` / `short(true)`，如
 * 「最近添加」「最早添加」），点一条同时定了档位和方向；推荐名按（指标, 方向）算，
 * 所以「添加时间 + 反转」推荐的是「最早添加的电影」。
 */
export const SORT_PRESETS: Record<
  HomeRowSort,
  {
    /** 推荐的行名：按方向给 */
    name: (library: string, reversed: boolean) => string;
    /** 不带库名的短标签：按方向给 */
    short: (reversed: boolean) => string;
    direction: SortDirection | null;
    hint: string;
  }
> = {
  added_at: {
    name: (l, r) => (r ? `最早添加的${l}` : `最近添加的${l}`),
    short: (r) => (r ? "最早添加" : "最近添加"),
    direction: { naturalAsc: false, asc: "旧→新", desc: "新→旧" },
    hint: "入库时间",
  },
  release_date: {
    name: (l, r) => (r ? `最早上映的${l}` : `最近上映的${l}`),
    short: (r) => (r ? "最早上映" : "最近上映"),
    direction: { naturalAsc: false, asc: "旧→新", desc: "新→旧" },
    hint: "上映时间",
  },
  last_played: {
    // 反转 = 上次播放离现在最远的在前：「很久没看的」比「最早观看的」更像人话
    name: (l, r) => (r ? `很久没看的${l}` : `最近观看的${l}`),
    short: (r) => (r ? "很久没看" : "最近观看"),
    direction: { naturalAsc: false, asc: "远→近", desc: "近→远" },
    hint: "我播放过的，按上次播放时间",
  },
  rating: {
    name: (l, r) => (r ? `评分最低的${l}` : `评分最高的${l}`),
    short: (r) => (r ? "评分最低" : "评分最高"),
    direction: { naturalAsc: false, asc: "低→高", desc: "高→低" },
    hint: "评分",
  },
  random: {
    name: (l) => `随便看看 · ${l}`,
    short: () => "随便看看",
    direction: null,
    hint: "每天换一批",
  },
  title: {
    name: (l, r) => (r ? `${l} Z–A` : `${l} A–Z`),
    short: (r) => (r ? "Z–A" : "A–Z"),
    direction: { naturalAsc: true, asc: "A→Z", desc: "Z→A" },
    hint: "片名",
  },
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
        "last_played",
        "rating",
        "random",
        "title",
      ];
  }
}

/** 合集行的预设：与库行同一组六个，标签不带库名。 */
export const COLLECTION_SORTS: HomeRowSort[] = [
  "added_at",
  "release_date",
  "last_played",
  "rating",
  "random",
  "title",
];

export const FAVORITES_SORT_PRESETS: Record<
  FavoritesSort,
  {
    /** 展示名：按方向给（无方向的档只有一种叫法） */
    name: (reversed: boolean) => string;
    direction: SortDirection | null;
    hint: string;
  }
> = {
  unwatched_first: {
    name: () => "未看优先",
    direction: null,
    hint: "没看完的在前，再按收藏时间",
  },
  favorited_at: {
    name: (r) => (r ? "最早收藏" : "最近收藏"),
    direction: { naturalAsc: false, asc: "旧→新", desc: "新→旧" },
    hint: "收藏时间",
  },
  rating: {
    name: (r) => (r ? "评分最低" : "评分最高"),
    direction: { naturalAsc: false, asc: "低→高", desc: "高→低" },
    hint: "评分",
  },
  title: {
    name: (r) => (r ? "片名 Z–A" : "片名 A–Z"),
    direction: { naturalAsc: true, asc: "A→Z", desc: "Z→A" },
    hint: "片名",
  },
};

const FAVORITES_SORTS = new Set<string>(Object.keys(FAVORITES_SORT_PRESETS));

/**
 * 把存下来的（sort, order）认成（档位, 是否反转）。
 *
 * - 认不出的档回落到 `allowed[0]`；
 * - 老偏好与合集表里的 `release_date_asc`（方向烧在取值里的那个年代）归一成
 *   `release_date` + 反转，这样不需要迁移，老数据读出来与从前的行为逐字一样；
 * - `order` 与该档自然方向相同时视作没反转（写回时也不会再存它）；
 * - 没有方向的档（random）永远不反转。
 */
function asRowSort(
  value: string | null | undefined,
  order: HomeRowOrder | null | undefined,
  allowed: HomeRowSort[],
): { sort: HomeRowSort; reversed: boolean } {
  if (value === "release_date_asc" && allowed.includes("release_date")) {
    return { sort: "release_date", reversed: true };
  }
  const sort = allowed.includes(value as HomeRowSort)
    ? (value as HomeRowSort)
    : allowed[0];
  return { sort, reversed: isReversed(SORT_PRESETS[sort].direction, order) };
}

function isReversed(
  direction: SortDirection | null,
  order: HomeRowOrder | null | undefined,
): boolean {
  if (!direction || !order) return false;
  return (order === "asc") !== direction.naturalAsc;
}

/**
 * 一档排序 + 是否反转 → 请求里该带的 `order`。与自然方向一致就不带（服务端按自然
 * 方向排，与加方向之前逐字相同），反转了才带——与海报墙的 `orderParam` 同一条规矩，
 * 也是偏好里 `order` 字段的写法。
 */
export function orderParamFor(
  direction: SortDirection | null,
  reversed: boolean,
): HomeRowOrder | undefined {
  if (!direction || !reversed) return undefined;
  return direction.naturalAsc ? "desc" : "asc";
}

/**
 * 这一行显示的名字：用户起的优先，空则跟随默认——库行按排序推荐，合集行用合集名。
 *
 * 合集行的名字原本是硬跟着合集走的（「一个东西一个名字」），后来改成可自定义：
 * 首页上这一行叫什么是**呈现**的事，与合集自身的身份是两回事。同一个合集出现在
 * 首页可以叫「今晚看点轻松的」，合集页里它仍叫原名，两边互不影响。
 */
export function rowTitle(row: HomeRow): string {
  switch (row.kind) {
    case "up-next":
      return "接下来继续";
    case "favorites":
      return "我的收藏";
    case "libraries":
      return "我的媒体库";
    case "library":
      return (
        row.name || SORT_PRESETS[row.sort].name(row.library.name, row.reversed)
      );
    case "collection":
      return row.name || row.collection.name;
  }
}

/** 自定义页里每行的小字：来源 · 排序 · 只看没看过的。行名是用户起的，来源和排序是它的真身。 */
export function rowMeta(row: HomeRow): string {
  switch (row.kind) {
    case "up-next":
      return "内置 · 我正在看的";
    case "favorites":
      return `内置 · ${FAVORITES_SORT_PRESETS[row.sort].name(row.reversed)}`;
    case "libraries":
      return "内置 · 管理页的库顺序";
    case "library":
      return [
        `${row.library.name}库`,
        SORT_PRESETS[row.sort].short(row.reversed),
        row.unwatched ? "只看没看过的" : null,
      ]
        .filter(Boolean)
        .join(" · ");
    case "collection":
      return `合集 · ${SORT_PRESETS[row.sort].short(row.reversed)}`;
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
      reversed: false,
    },
    { id: "libraries", kind: "libraries", hidden: false },
    ...libraries
      .filter((library) => !library.exclude_from_home)
      .map((library): HomeRow => ({
        id: `lib:${library.id}`,
        kind: "library",
        hidden: false,
        sort: "added_at",
        reversed: false,
        unwatched: false,
        name: "",
        library,
        builtin: true,
      })),
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
    return {
      id: "favorites",
      kind: "favorites",
      hidden,
      sort,
      reversed: isReversed(FAVORITES_SORT_PRESETS[sort].direction, pref.order),
    };
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
    // 没存排序时沿用合集自己的序（含它的方向）；存了就以存的为准，方向也是
    const { sort, reversed } =
      pref.sort != null
        ? asRowSort(pref.sort, pref.order, COLLECTION_SORTS)
        : asRowSort(collection.sort, null, COLLECTION_SORTS);
    return {
      id: pref.id,
      kind: "collection",
      hidden,
      sort,
      reversed,
      name: (pref.name ?? "").trim(),
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
  const { sort, reversed } = asRowSort(
    pref.sort,
    pref.order,
    sortPresetsFor(library.kind),
  );
  return {
    id: pref.id,
    kind: "library",
    hidden: pref.hidden === true,
    sort,
    reversed,
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
      case "favorites": {
        if (row.sort !== "unwatched_first") base.sort = row.sort;
        const order = orderParamFor(
          FAVORITES_SORT_PRESETS[row.sort].direction,
          row.reversed,
        );
        if (order) base.order = order;
        return base;
      }
      case "library": {
        if (!row.builtin) base.library_id = row.library.id;
        base.sort = row.sort;
        const order = orderParamFor(
          SORT_PRESETS[row.sort].direction,
          row.reversed,
        );
        if (order) base.order = order;
        if (row.unwatched) base.unwatched = true;
        if (row.name) base.name = row.name;
        return base;
      }
      case "collection": {
        base.collection_id = row.collection.id;
        base.sort = row.sort;
        const order = orderParamFor(
          SORT_PRESETS[row.sort].direction,
          row.reversed,
        );
        if (order) base.order = order;
        if (row.name) base.name = row.name;
        return base;
      }
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
    reversed: false,
    unwatched: false,
    name: "",
    library,
    builtin: false,
  };
}

/** 新建一条合集行（排序取合集自己的默认，名字留空跟随合集名）。 */
export function newCollectionRow(
  collection: HomeCollectionLike,
  id = newRowId(),
): HomeRow {
  return {
    id,
    kind: "collection",
    hidden: false,
    ...asRowSort(collection.sort, null, COLLECTION_SORTS),
    name: "",
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
