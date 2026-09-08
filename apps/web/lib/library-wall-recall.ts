/**
 * 媒体库墙的「上次浏览到哪」——跨会话的位置记忆（配合底部胶囊）。
 *
 * 与 lib/use-scroll-restoration.ts 的分工，两者互不覆盖：
 *   - 滚动恢复只活在**同一次浏览会话**里（内存 Map）：点进详情页再返回，
 *     自动回到原处，不问用户；
 *   - 这里记的是**跨会话**的位置（localStorage）：关掉标签页、明天再打开，
 *     底部弹一枚胶囊问「要不要回到上次的位置」。跳不跳由用户决定，所以
 *     不还原像素，只存一个「首个可见条目在整份排序里的绝对位置」，跳转走
 *     墙自己的分页跳转（与 A-Z 索引条同一条路：换一个从该位置起的窗口）。
 *
 * 为什么存绝对位置而不是 scrollTop：墙是分页的，几千部的库离开时可能已经
 * 加载了十几屏。按像素恢复要把这十几屏原样重拉一遍（十几个请求），按位置
 * 跳转只需一页——跳过去之后上方的内容由墙顶哨兵按需往上补（与索引条跳字母
 * 同一条路），往上滑照常有内容。
 *
 * 纯逻辑模块（存储可注入），`node --test` 直接跑。
 */

export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 一面墙的位置记录。 */
export interface WallRecall {
  /** 上次首个可见条目在整份排序里的绝对位置（0 = 墙首） */
  offset: number;
  /**
   * 记录时那面墙的形态：海报墙 / 图廊、以及排序口径。形态一换，同一个 offset
   * 指向的就不是同一部作品了（比如按时间排的其他库与按标题排的图廊），
   * 对不上就当作没有记录，宁可不提示也不要把人送到莫名其妙的位置。
   */
  view: string;
  updatedAt: number;
}

/** 全部墙的记录合存一个键：一库一个键会在 localStorage 里越积越多且无人清理。 */
const STORAGE_KEY = "movieclaw.wall-recall";
/** 最多记住多少面墙（超出按最久未更新淘汰） */
const MAX_ENTRIES = 20;
/**
 * 过期时长：两周前看到哪里，今天已经不是「上次」了，弹胶囊只剩打扰。
 * 库的内容也在变，太旧的绝对位置多半已经指向别的作品。
 */
export const RECALL_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;
/**
 * 浅于这个位置不提示：一两屏的距离用户自己滑更快，胶囊只是碍事。
 * 24 ≈ 桌面海报墙两行多、手机三行多。
 */
export const RECALL_MIN_OFFSET = 24;

function storageOrNull(storage?: KeyValueStorage): KeyValueStorage | null {
  if (storage) return storage;
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null; // 隐私模式 / 禁用站点数据：当作没有记录，功能静默降级
  }
}

function readAll(store: KeyValueStorage): Record<string, WallRecall> {
  try {
    const raw = store.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    const rows: Record<string, WallRecall> = {};
    for (const [scope, value] of Object.entries(parsed as Record<string, unknown>)) {
      const row = value as Partial<WallRecall> | null;
      if (!row || typeof row.offset !== "number" || typeof row.view !== "string") continue;
      rows[scope] = {
        offset: Math.max(0, Math.trunc(row.offset)),
        view: row.view,
        updatedAt: typeof row.updatedAt === "number" ? row.updatedAt : 0,
      };
    }
    return rows;
  } catch {
    return {}; // 存的内容坏了就当没记过，下一次滚动会重新写入
  }
}

function writeAll(store: KeyValueStorage, rows: Record<string, WallRecall>) {
  // 超限时淘汰最久未更新的那几面墙
  const entries = Object.entries(rows).sort((a, b) => b[1].updatedAt - a[1].updatedAt);
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(entries.slice(0, MAX_ENTRIES))));
  } catch {
    /* 配额满 / 隐私模式：位置记不住而已，不影响浏览 */
  }
}

/** 一面墙的记录键：单库页是 `library:12`。 */
export function wallRecallScope(libraryId: number): string {
  return `library:${libraryId}`;
}

/**
 * 读上次的位置。形态对不上、太浅、过期、没记过一律回 null——调用方据此
 * 决定要不要弹胶囊，拿到 null 就当作「这次是从头看」。
 */
export function readWallRecall(
  scope: string,
  view: string,
  storage?: KeyValueStorage,
  now: number = Date.now(),
): WallRecall | null {
  const store = storageOrNull(storage);
  if (!store) return null;
  const saved = readAll(store)[scope];
  if (!saved || saved.view !== view) return null;
  if (saved.offset < RECALL_MIN_OFFSET) return null;
  if (now - saved.updatedAt > RECALL_MAX_AGE_MS) return null;
  return saved;
}

/** 记下当前位置（同一面墙覆盖上一条）。 */
export function writeWallRecall(
  scope: string,
  view: string,
  offset: number,
  storage?: KeyValueStorage,
  now: number = Date.now(),
) {
  const store = storageOrNull(storage);
  if (!store) return;
  const rows = readAll(store);
  rows[scope] = { offset: Math.max(0, Math.trunc(offset)), view, updatedAt: now };
  writeAll(store, rows);
}

/* —— 长时间挂后台 —— */

/**
 * 挂后台超过这么久再回到前台，就把这一屏当成「重新进入」。
 *
 * 为什么需要这条规则：iOS 的 PWA 恢复应用时**不会重新加载页面**——把 App
 * 划掉再打开，多半是把原来那张页面原样恢复出来，JS 上下文没死、组件没重挂，
 * 于是"重新进入"这件事在代码里从来没发生过，胶囊也就永远等不到（用户反馈
 * 2026-09-08，iOS PWA 实测）。改成按「离开了多久」判断：隔夜回来算重新进入，
 * 几分钟内的来回切换仍然无感。
 */
export const LONG_ABSENCE_MS = 30 * 60 * 1000;

/** 切到后台的时刻；0 = 当前在前台 */
let hiddenAt = 0;
/** 最近一次「久别回归」的时刻；0 = 本次页面加载还没发生过 */
let returnedAt = 0;
const returnListeners = new Set<() => void>();

function handleVisibilityChange() {
  if (document.visibilityState === "hidden") {
    hiddenAt = Date.now();
    return;
  }
  if (hiddenAt === 0) return;
  const away = Date.now() - hiddenAt;
  hiddenAt = 0;
  if (away < LONG_ABSENCE_MS) return;
  returnedAt = Date.now();
  // 复制一份再遍历：回调里可能顺手退订
  for (const listener of [...returnListeners]) listener();
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", handleVisibilityChange);
}

/** 订阅「挂后台很久之后回到前台」。返回退订函数。 */
export function onReturnFromLongAbsence(listener: () => void): () => void {
  returnListeners.add(listener);
  return () => {
    returnListeners.delete(listener);
  };
}

/**
 * 久别回归是否发生在「离开这面墙之后」——是的话这次进入算重新进入。
 *
 * 判据是两个时刻的先后：回到前台的时刻 vs 上次在这面墙上滚动的时刻。用户
 * 只要在这面墙上滚一下，记录时间就会超过它，胶囊不会反复弹。
 */
export function isReentry(returnMoment: number, savedUpdatedAt: number | undefined): boolean {
  return returnMoment > 0 && returnMoment > (savedUpdatedAt ?? 0);
}

/** 上面那条判据的实际调用口（读模块状态与本地记录）。 */
export function isReentryAfterAbsence(scope: string, storage?: KeyValueStorage): boolean {
  if (returnedAt === 0) return false;
  const store = storageOrNull(storage);
  return isReentry(returnedAt, store ? readAll(store)[scope]?.updatedAt : undefined);
}

/**
 * 找出滚动容器里第一个还露在视口内的锚点。
 *
 * 锚点按 DOM 顺序传入（海报墙一格一个条目，图廊一张图一个瓦片，瓦片带的是
 * 所属作品）。命中即返回，不必量完整面墙——用户在第几屏，就只多读几屏的
 * 矩形。`viewportTop` 是滚动容器可视区的顶边（不是页面顶边）。
 */
export function firstVisibleAnchorId(
  container: Element,
  attribute: string,
  viewportTop: number,
): string | null {
  for (const node of container.querySelectorAll(`[${attribute}]`)) {
    if (node.getBoundingClientRect().bottom > viewportTop) {
      return node.getAttribute(attribute);
    }
  }
  return null;
}
