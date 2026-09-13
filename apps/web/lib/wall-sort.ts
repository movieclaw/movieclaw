"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { LibraryItemSort } from "@/lib/api/libraries";

/**
 * 海报墙的排序偏好——单库页、「全部收藏」页、合集详情页共用的那一份。
 *
 * 三面墙的排序**能力对齐**指的是：同一档在三处叫同一个名、按同一个方向语义、
 * 记法相同（`rating` / `rating:rev`）。所以档位、方向表与读写偏好的钩子只在这里
 * 写一次；每面墙只决定两件事——默认档叫什么（单库页按库的形态叫「按标题」/
 * 「按时间」，收藏页叫「最近收藏」，合集页叫「自定顺序」/ 合集自己的序）和
 * 偏好记在哪个键下（单库页全站一个键，收藏页一个键，合集按 id 各记各的）。
 *
 * 排序是偏好该记，筛选是意图不该记（docs/design/library-filtering.md 3.4）。
 */
export type WallSortPref =
  | "default"
  | "title"
  | "added_at"
  | "release_date"
  | "rating"
  | "runtime"
  | "size"
  | "last_played";

/** 偏好 → 服务端排序键。`default` 由各面墙自己决定（单库页按库的形态，合集页是合集自己的序）；
 *  `probing` 是扫描临时接管的序，不是用户能选的档。 */
export const PREF_TO_SORT: Record<
  Exclude<WallSortPref, "default">,
  Exclude<LibraryItemSort, "probing">
> = {
  title: "title",
  added_at: "added_at",
  release_date: "release_date",
  rating: "rating",
  runtime: "runtime",
  size: "size",
  last_played: "last_played",
};

/** 非默认档的展示名，各面墙拼自己的选项列表时取用（与筛选条的 SORT_LABELS 同一套叫法）。 */
export const SORT_PREF_LABELS: Record<Exclude<WallSortPref, "default">, string> = {
  title: "按标题",
  // 一次导入的内容入账时间都挤在一起，所以这一档对"陆续往库里添东西"才有意义
  added_at: "最近添加",
  release_date: "按上映时间",
  rating: "按评分",
  runtime: "按片长",
  size: "按体积",
  last_played: "最近观看",
};

/**
 * 每档的自然方向与方向的人话（2026-09-11 起排序可切换正倒序）。
 *
 * 不反转时请求不带 order，服务端按自然方向排——与加方向之前逐字相同。方向写成
 * 人话（「短→长」）而不是只画箭头：↑ 到底是"从小到大"还是"大的在上"，光看箭头
 * 要想一下。补探序是扫描临时接管的，控件那几分钟本来就是灰的，方向无意义
 */
export const SORT_DIRECTIONS: Record<
  LibraryItemSort,
  { naturalAsc: boolean; asc: string; desc: string }
> = {
  title: { naturalAsc: true, asc: "A→Z", desc: "Z→A" },
  added_at: { naturalAsc: false, asc: "旧→新", desc: "新→旧" },
  release_date: { naturalAsc: false, asc: "旧→新", desc: "新→旧" },
  probing: { naturalAsc: true, asc: "A→Z", desc: "Z→A" },
  rating: { naturalAsc: false, asc: "低→高", desc: "高→低" },
  runtime: { naturalAsc: true, asc: "短→长", desc: "长→短" },
  size: { naturalAsc: false, asc: "小→大", desc: "大→小" },
  last_played: { naturalAsc: false, asc: "远→近", desc: "近→远" },
};

/** 单库页的偏好键：全站共用一个（在哪个库里选的「按评分」，换个库还是按评分）。 */
export const WALL_SORT_STORAGE_KEY = "movieclaw.library.wall-sort";

/** 能从 storage 里认回来的排序偏好；其余一律当默认序（防老版本或手改出来的脏值） */
const WALL_SORT_PREFS: readonly WallSortPref[] = [
  "default",
  "title",
  "added_at",
  "release_date",
  "rating",
  "runtime",
  "size",
  "last_played",
];

/** 排序偏好：选的哪一档，以及是否反转了这一档的自然方向。 */
export interface WallSortState {
  pref: WallSortPref;
  reversed: boolean;
}

/**
 * 读写排序偏好（含方向）。第四个返回值是「读完 storage 了没有」：首帧一律先给默认值
 * （服务端渲染没有 localStorage），排序相关的副作用必须等它为真再动手——
 * 否则从详情页返回的那一帧会先按默认序把窗口重拉一遍，把人甩回墙首。
 *
 * 存成 `rating` / `rating:rev`。此前只认回「最近添加」一档：选了按评分、按片长，
 * 刷新一下就退回默认序——排序是偏好，该记全。换档时方向回到新档的自然方向：
 * 从「片长 长→短」换到「评分」，用户要的是"高分在前"，不是继承一个反向
 */
export function useWallSortPref(
  storageKey: string = WALL_SORT_STORAGE_KEY,
): [WallSortState, (next: WallSortPref) => void, () => void, boolean] {
  const [state, setState] = useState<WallSortState>({ pref: "default", reversed: false });
  // 读完的是哪个键：合集页的键随合集 id 变，换合集的那一帧手上还是上一个合集的
  // 偏好，「读完了没有」必须按键算，否则会先按错的排序拉一页再重拉
  const [readyFor, setReadyFor] = useState<string | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  useEffect(() => {
    let next: WallSortState = { pref: "default", reversed: false };
    try {
      const [pref, flag] = (window.localStorage.getItem(storageKey) ?? "").split(":");
      if (WALL_SORT_PREFS.includes(pref as WallSortPref)) {
        next = { pref: pref as WallSortPref, reversed: flag === "rev" };
      }
    } catch {
      /* 隐私模式等拿不到 storage：保持默认序 */
    }
    setState(next);
    setReadyFor(storageKey);
  }, [storageKey]);
  const ready = readyFor === storageKey;
  const persist = useCallback(
    (next: WallSortState) => {
      setState(next);
      try {
        window.localStorage.setItem(storageKey, next.reversed ? `${next.pref}:rev` : next.pref);
      } catch {
        /* 同上 */
      }
    },
    [storageKey],
  );
  const update = useCallback((pref: WallSortPref) => persist({ pref, reversed: false }), [persist]);
  const toggleReversed = useCallback(
    () => persist({ ...stateRef.current, reversed: !stateRef.current.reversed }),
    [persist],
  );
  return [state, update, toggleReversed, ready];
}

/**
 * 一档排序 + 方向 → 请求里该带什么 `order`：与自然方向一致就不带（服务端按自然
 * 方向排，与加方向之前逐字相同），反转了才带。三面墙都按这一条算，不各自判。
 */
export function orderParam(sort: LibraryItemSort, reversed: boolean): "asc" | "desc" | undefined {
  if (!reversed) return undefined;
  return SORT_DIRECTIONS[sort].naturalAsc ? "desc" : "asc";
}
