"use client";

import { createContext, memo, useContext, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import type { Route } from "next";

import { useToast } from "@/components/feedback";
import { ImageLightbox } from "@/components/image-lightbox";
import { LayersIcon, ListIcon, PhotoIcon, XIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { PosterImage } from "@/components/poster-image";
import { Tooltip } from "@/components/tooltip";
import type { SearchScope } from "@/lib/categories";
import { platformLabel } from "@/lib/platforms";
import {
  getTorrentSearchHistoryResults,
  streamSearchTorrents,
  type SiteSearchStatus,
  type TorrentAttrs,
  type TorrentHit,
} from "@/lib/api/search";
import {
  DownloadTargetDialog,
  readRememberedTarget,
  submitRememberedTarget,
  type DownloadTargetRequest,
} from "@/components/download-target-dialog";
import {
  downloadSelectedTorrentForSubscription,
  getSubscription,
} from "@/lib/api/subscriptions";
import { cachedImageUrl } from "@/lib/image-proxy";
import { usePermissions } from "@/lib/permissions";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/**
 * 搜索结果页（主内容区）——消费 SSE 流式搜索，结果渐进渲染。
 *
 * 数据流：streamSearchTorrents 逐事件回调，快的站点先出结果先上屏，不被最慢的
 * 站点拖住。页头状态行右侧有站点状态聚合 chip（点开看逐站命中/耗时/失败原因），
 * 流式过程中的等待感由页头进度条与骨架屏承载。
 *
 * 筛选区采用「频率分层」的三层结构（Airbnb 式抽屉方案）：
 * 1. **常驻工具栏**（统一胶囊语言，随结果复杂度自适应收缩）：排序胶囊
 *    （SortDropdown，键+方向合一）+ 分辨率 chips（命中前 3 平铺）+ 年份/季/
 *    压制组单维度下拉（FacetDropdown，可选值 ≥2 才出现）+ 纯图标视图切换
 *    + 带角标的「筛选」按钮；
 * 2. **筛选弹层**：站点/年份/季/集/片源/编码/HDR/音频/字幕/压制组按组分区，
 *    chips 多选（组内=或，组间=且），底部实时显示命中数；服务重度组合场景；
 * 3. **已应用条件回显行**：弹层里激活的条件以可摘除 chip 回显在结果上方，
 *    用户不用打开弹层就知道列表被什么约束着。
 *
 * 筛选与排序都是纯前端操作，不重新发起搜索；新搜索重置筛选、保留排序偏好。
 * 流式期间新到的结果实时并入当前筛选/排序视图。
 */
export interface SearchQuery {
  keyword: string;
  /** 搜索范围（标签换算而来）：展示名 + 分类/站点组合 */
  scope: SearchScope;
  /**
   * 快照预览：非空时不发起实时搜索，改为加载该条历史留存的结果快照
   * （页顶提示条标注快照年龄，可一键切回实时搜索）。
   */
  snapshotId?: number;
}

export interface SearchResultsProps {
  query: SearchQuery;
  /** 发起实时搜索（快照提示条的「重新搜索」按钮）；不传则不渲染该按钮。 */
  onResearch?: (keyword: string, scope: SearchScope) => void;
  /** 手动选种模式（URL 的 for_sub 参数）：每条资源多一颗「投给订阅」按钮，
   *  点击直接投给该订阅（跳过规则组过滤，身份匹配照常）。 */
  grabForSubscriptionId?: number | null;
}

/** 手动选种上下文：SearchResults 顶层拉取订阅标题后灌入，行组件按需消费。 */
const GrabContext = createContext<{ id: number; title: string } | null>(null);

/**
 * 整页搜索阶段：connecting=流尚未建立（start 事件未到），streaming=站点结果陆续
 * 到达中，done=全部站点已返回，error=流本身失败（网络/鉴权——单站失败不算）。
 */
type Phase = "connecting" | "streaming" | "done" | "error";

/** 单个站点的实时进度（由 start / site_result / site_error 事件逐步填充）。 */
interface SiteProgress {
  site_id: string;
  site_name: string;
  phase: "searching" | "ok" | "error";
  count: number;
  error: string | null;
  elapsed_ms: number | null;
}

/** 耗时展示：统一到秒（一位小数），站点状态弹层用。 */
function formatElapsed(ms: number): string {
  return `${(ms / 1000).toFixed(1)} 秒`;
}

/** 影视类型的展示文案（值与后端 TorrentAttrs.media_type 一致）。 */
const MEDIA_TYPE_LABEL: Record<string, string> = {
  movie: "电影",
  tv: "剧集",
};

/* —— 排序 —— */

type SortKey =
  | "seeders"
  | "time"
  | "size"
  | "snatched"
  | "complete"
  | "quality"
  | "free";

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "seeders", label: "做种数" },
  { key: "time", label: "发布时间" },
  { key: "size", label: "体积" },
  { key: "snatched", label: "完成数" },
];

/**
 * 智能排序键：基于解析属性（attrs）的复合排序，按当前结果集的类型构成
 * 动态出现在菜单里（见 SearchResults 的 smartSortKeys）：剧集为主时给
 * 「全集优先」，电影为主时给「画质优先」，「免费优先」常驻。
 * 语义都是「优先」，选中即降序（升序无意义但仍可手动切换）。
 */
const SMART_SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "complete", label: "全集优先" },
  { key: "quality", label: "画质优先" },
  { key: "free", label: "免费优先" },
];

interface SortState {
  key: SortKey;
  dir: "desc" | "asc";
}

/** 从地址栏的 sort=key:dir 参数还原排序态，非法/缺失时回落到默认（做种数降序）。 */
function readSortFromUrl(): SortState {
  const fallback: SortState = { key: "seeders", dir: "desc" };
  if (typeof window === "undefined") return fallback;
  const [key, dir] = (new URLSearchParams(window.location.search).get("sort") ?? "").split(":");
  if (
    [...SORT_OPTIONS, ...SMART_SORT_OPTIONS].some((o) => o.key === key) &&
    (dir === "asc" || dir === "desc")
  ) {
    return { key: key as SortKey, dir };
  }
  return fallback;
}

/** 结果展示视图：group=按作品分组（默认），list=平铺种子列表，poster=图览。 */
type ResultView = "group" | "list" | "poster";

/**
 * 视图初值：图览预设/地址栏 poster 参数优先，其次地址栏 view=list（平铺），
 * 默认分组。与 readSortFromUrl 同理，本组件仅在客户端渲染，可安全读 window。
 */
function initialView(posterPreset: boolean): ResultView {
  if (posterPreset) return "poster";
  if (
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).get("view") === "list"
  ) {
    return "list";
  }
  return "group";
}

/**
 * 剧集完整度层级（越大越完整）：4=明确标注全集（"全N集"/COMPLETE 必置
 * complete=true，见 movieclaw_enrich.inference），3=整季包（有季无集，与
 * matcher 的整季 pack 判定同口径），2=多集区间，1=单集，0=无季集信息
 * （电影或解析失败）。同层内再比季数（多季合集靠前）、做种数，见 sortVector。
 */
function completenessTier(a: TorrentAttrs | null): number {
  if (!a) return 0;
  if (a.complete === true) return 4;
  if (a.seasons.length > 0 && a.episodes.length === 0) return 3;
  if (a.episodes.length > 1) return 2;
  if (a.episodes.length === 1) return 1;
  return 0;
}

/** 片源档位（画质排序第二级）：碟源 > WEB > 电视录制 > DVD；键是 enrich 归一化值。 */
const SOURCE_RANK: Record<string, number> = {
  "UHD Blu-ray": 7,
  "Blu-ray": 6,
  "WEB-DL": 5,
  BDRip: 4,
  WEBRip: 3,
  "HD-DVD": 3,
  HDTV: 2,
  HDTVRip: 2,
  HDRip: 2,
  TVRip: 1,
  DVDRip: 1,
  DVD: 1,
};

/**
 * 排序向量：逐级字典序比较（前级相等才看后级）。常规键单元素；智能键是
 * 「主指标 → 次指标 → 做种数决胜」，保证同档资源仍按热度稳定排列，且缺失
 * 解析属性的结果自然垫底、不干扰排序。
 */
function sortVector(hit: TorrentHit, key: SortKey): number[] {
  const a = hit.attrs;
  switch (key) {
    case "time":
      // 无发布时间的排最后（升降序都垫底更符合直觉，用 -Infinity 简化处理）
      return [hit.upload_time ? Date.parse(hit.upload_time) : -Infinity];
    case "size":
      return [hit.size_bytes];
    case "snatched":
      return [hit.snatched];
    case "complete":
      return [completenessTier(a), a?.seasons.length ?? 0, hit.seeders];
    case "quality":
      // 分辨率数值（"2160p"→2160）为主，片源档位次之（同源 Remux 加半档）
      return [
        a?.resolution ? parseInt(a.resolution, 10) || 0 : 0,
        (a?.media_source ? (SOURCE_RANK[a.media_source] ?? 0) : 0) + (a?.remux ? 0.5 : 0),
        hit.seeders,
      ];
    case "free":
      // 免费 > 折扣 > 原价；同档「明确无 H&R 考核」者优先（null=站点不提供，不加分）
      return [
        hit.free || hit.download_volume_factor === 0 ? 2 : hit.download_volume_factor < 1 ? 1 : 0,
        hit.hit_and_run === false ? 1 : 0,
        hit.seeders,
      ];
    default:
      return [hit.seeders];
  }
}

/* —— 筛选状态 —— */

interface Filters {
  resolution: Set<string>; // 常驻 chips
  // 以下维度收在筛选弹层里，全部多选（组内=或）
  site: Set<string>;
  year: Set<number>;
  season: Set<number>;
  episode: Set<number>;
  source: Set<string>; // media_source 值 + 特殊值 "Remux"
  platform: Set<string>; // 流媒体平台规范值（netflix/iqiyi…）
  codec: Set<string>;
  hdr: Set<string>;
  audio: Set<string>;
  subtitle: Set<string>;
  group: Set<string>;
}

function emptyFilters(): Filters {
  return {
    resolution: new Set(),
    site: new Set(),
    year: new Set(),
    season: new Set(),
    episode: new Set(),
    source: new Set(),
    platform: new Set(),
    codec: new Set(),
    hdr: new Set(),
    audio: new Set(),
    subtitle: new Set(),
    group: new Set(),
  };
}

/** 弹层内维度的键（回显行与角标计数只统计这些——类型/分辨率在工具栏上自明）。 */
const SHEET_KEYS = [
  "site", "year", "season", "episode", "source", "platform", "codec", "hdr", "audio",
  "subtitle", "group",
] as const;
type SheetKey = (typeof SHEET_KEYS)[number];

// 语言值来自 movieclaw_enrich.lang_decl（BCP 47）；zh 是"中字"类泛称声明，
// 刻意不猜简繁。未收录的语言码直接展示原值
const SUBTITLE_LANGUAGE_LABELS: Record<string, string> = {
  zh: "中文字幕（未标简繁）",
  "zh-Hans": "简体中文字幕",
  "zh-Hant": "繁体中文字幕",
  en: "英文字幕",
  ja: "日文字幕",
  ko: "韩文字幕",
  yue: "粤语字幕",
};

function subtitleLanguageLabel(language: string): string {
  return SUBTITLE_LANGUAGE_LABELS[language] ?? language;
}

// —— 结果行紧凑徽标：多语言聚合成单徽标（"字幕 简·繁·英"），全称留给筛选弹层
const SUB_LANG_SHORT: Record<string, string> = {
  "zh-Hans": "简",
  "zh-Hant": "繁",
  zh: "中",
  en: "英",
  ja: "日",
  ko: "韩",
  yue: "粤",
};
const AUDIO_LANG_SHORT: Record<string, string> = {
  cmn: "国",
  yue: "粤",
  en: "英",
  ja: "日",
  ko: "韩",
};

/** "字幕 简·繁·英［·硬］"；仅泛称时显示"中字"；无声明返回 null。 */
function compactSubtitleBadge(attrs: TorrentAttrs): string | null {
  let langs = attrs.subtitle_languages ?? [];
  if (!langs.length) return null;
  // 有具体简繁时泛称 zh 被蕴含，不重复展示
  if (langs.some((v) => v.startsWith("zh-"))) langs = langs.filter((v) => v !== "zh");
  const hard = (attrs.subtitle_carriers ?? []).includes("hardcoded") ? "·硬" : "";
  if (langs.length === 1 && langs[0] === "zh") return `中字${hard}`;
  return `字幕 ${langs.map((v) => SUB_LANG_SHORT[v] ?? v).join("·")}${hard}`;
}

/** "音轨 国·粤"；无声明返回 null。 */
function compactAudioBadge(attrs: TorrentAttrs): string | null {
  const langs = attrs.audio_languages ?? [];
  if (!langs.length) return null;
  return `音轨 ${langs.map((v) => AUDIO_LANG_SHORT[v] ?? v).join("·")}`;
}

function sheetSelectionCount(f: Filters): number {
  return SHEET_KEYS.reduce((sum, key) => sum + f[key].size, 0);
}

function hasActiveFilters(f: Filters): boolean {
  return f.resolution.size > 0 || sheetSelectionCount(f) > 0;
}

/** 筛选维度标识：与 Filters 的键一一对应（分面计数按维度做自排除时引用）。 */
type FilterDim = keyof Filters;

/** 作品分组里「未识别」桶的聚合键（解析不出片名的种子都归这里，按原始名平铺）。 */
const ENTITY_UNPARSED = "__unparsed__";

/**
 * 作品聚合键：解析主名（中文优先）定作品；电影按年份区分（沙丘 1984 vs 2021），
 * 剧集不按年拆（同一部剧的各季跨年）。别名归并（沙丘2 / Dune: Part Two 各自成组）
 * 留给 TMDB 回填后用 tmdb_id 解决。
 */
function entityKeyOf(hit: TorrentHit): string {
  const title = hit.attrs?.titles_zh?.[0] ?? hit.attrs?.titles_en?.[0];
  if (!title) return ENTITY_UNPARSED;
  const type = hit.attrs?.media_type ?? "?";
  if (type === "tv") return `tv|${title}`;
  return `${type}|${title}|${hit.attrs?.year ?? "?"}`;
}

/**
 * 逐维度的通过判定（维度未激活即通过）。过滤与分面计数共用这一份逻辑：
 * matchesFilters 要求全部维度通过；计数时则需要知道某条结果具体挂在哪个维度上
 * （见 aggregateFacets 的自排除计数），拆成表驱动两边才不会出现口径不一致。
 */
const FILTER_DIMENSIONS: { dim: FilterDim; pass: (hit: TorrentHit, f: Filters) => boolean }[] = [
  {
    dim: "resolution",
    pass: (hit, f) =>
      !f.resolution.size || (!!hit.attrs?.resolution && f.resolution.has(hit.attrs.resolution)),
  },
  { dim: "site", pass: (hit, f) => !f.site.size || f.site.has(hit.site_id) },
  {
    dim: "year",
    pass: (hit, f) => !f.year.size || (hit.attrs?.year != null && f.year.has(hit.attrs.year)),
  },
  {
    dim: "season",
    pass: (hit, f) => !f.season.size || (hit.attrs?.seasons ?? []).some((s) => f.season.has(s)),
  },
  {
    dim: "episode",
    // 全集包（complete）视为包含任意一集——它没逐集列出但语义上都有
    pass: (hit, f) =>
      !f.episode.size ||
      (hit.attrs?.episodes ?? []).some((e) => f.episode.has(e)) ||
      hit.attrs?.complete === true,
  },
  {
    dim: "source",
    pass: (hit, f) => {
      if (!f.source.size) return true;
      const a = hit.attrs;
      const bySource = a?.media_source ? f.source.has(a.media_source) : false;
      const byRemux = f.source.has("Remux") && a?.remux === true;
      return bySource || byRemux;
    },
  },
  {
    dim: "platform",
    pass: (hit, f) =>
      !f.platform.size || (hit.attrs?.platforms ?? []).some((v) => f.platform.has(v)),
  },
  {
    dim: "codec",
    pass: (hit, f) =>
      !f.codec.size || (!!hit.attrs?.video_codec && f.codec.has(hit.attrs.video_codec)),
  },
  { dim: "hdr", pass: (hit, f) => !f.hdr.size || (hit.attrs?.hdr ?? []).some((v) => f.hdr.has(v)) },
  {
    dim: "audio",
    pass: (hit, f) => !f.audio.size || (hit.attrs?.audio ?? []).some((v) => f.audio.has(v)),
  },
  {
    dim: "subtitle",
    // BCP 47 前缀语义与订阅选种规则一致：勾 "zh" 命中 zh/zh-Hans/zh-Hant，
    // 勾 "zh-Hans" 只命中简体（泛称"中字"资源不算简体）
    pass: (hit, f) =>
      !f.subtitle.size ||
      (hit.attrs?.subtitle_languages ?? []).some((v) =>
        f.subtitle.has(v) ||
        [...f.subtitle].some((sel) => v.startsWith(`${sel}-`)),
      ),
  },
  {
    dim: "group",
    pass: (hit, f) =>
      !f.group.size || (!!hit.attrs?.release_group && f.group.has(hit.attrs.release_group)),
  },
];

function matchesFilters(hit: TorrentHit, f: Filters): boolean {
  return FILTER_DIMENSIONS.every((d) => d.pass(hit, f));
}

/* —— 聚合：从当前结果集统计各维度可选值与计数 —— */

interface FacetValue {
  value: string;
  count: number;
}

/** 数值型枚举维度（季/集/年份）的可选值与命中计数。 */
interface NumericFacetValue {
  value: number;
  count: number;
}

interface Facets {
  resolution: FacetValue[];
  years: NumericFacetValue[]; // 降序
  seasons: NumericFacetValue[]; // 升序，观测到多少列多少
  episodes: NumericFacetValue[];
  source: FacetValue[];
  platform: FacetValue[];
  codec: FacetValue[];
  hdr: FacetValue[];
  audio: FacetValue[];
  subtitles: FacetValue[];
  groups: FacetValue[];
  /** 站点维度按 site_id 计数（chip 列表来自站点搜索状态，这里只供数字）。 */
  sites: Map<string, number>;
}

/** 各维度的值→命中数映射（一次遍历的原始计数，排序/截断在 aggregateFacets 里做）。 */
interface FacetMaps {
  resolution: Map<string, number>;
  source: Map<string, number>;
  platform: Map<string, number>;
  codec: Map<string, number>;
  hdr: Map<string, number>;
  audio: Map<string, number>;
  subtitles: Map<string, number>;
  groups: Map<string, number>;
  sites: Map<string, number>;
  years: Map<number, number>;
  seasons: Map<number, number>;
  episodes: Map<number, number>;
}

/**
 * 遍历结果集统计各维度计数。filters 为 null 时统计全量（决定 chip 的取值范围与
 * 展示顺序）；传入 filters 时做「自排除」计数——某条结果只有在通过了*其他所有*
 * 维度的筛选后，才计入某维度的数字。组内多选是"或"，所以本维度自身不设限，
 * 这样每个标签上的数字始终等于「选中它之后会看到的条数」。
 * 实现上按未通过的维度数分流：全通过 → 计入所有维度；只挂在一个维度 →
 * 只计入该维度（选中该维度里对应的值就能救回它）；挂两个以上 → 不计入任何维度。
 */
function collectFacetMaps(items: TorrentHit[], filters: Filters | null): FacetMaps {
  const maps: FacetMaps = {
    resolution: new Map(),
    source: new Map(),
    platform: new Map(),
    codec: new Map(),
    hdr: new Map(),
    audio: new Map(),
    subtitles: new Map(),
    groups: new Map(),
    sites: new Map(),
    years: new Map(),
    seasons: new Map(),
    episodes: new Map(),
  };
  const bump = (map: Map<string, number>, key: string) =>
    map.set(key, (map.get(key) ?? 0) + 1);
  const bumpNum = (map: Map<number, number>, key: number) =>
    map.set(key, (map.get(key) ?? 0) + 1);

  for (const hit of items) {
    // only 非空 = 这条结果只挂在该维度上，仅计入它；全通过时保持 null（计入所有维度）
    let only: FilterDim | null = null;
    if (filters) {
      // 手写循环而非 filter()：这里在每条结果上执行，千条结果 × 每批流式
      // 重算,避免每条都分配一个临时数组;失败超过 1 个维度即可提前退出
      let failedCount = 0;
      for (const d of FILTER_DIMENSIONS) {
        if (d.pass(hit, filters)) continue;
        failedCount += 1;
        if (failedCount > 1) break;
        only = d.dim;
      }
      if (failedCount > 1) continue;
      if (failedCount === 0) only = null;
    }
    const want = (dim: FilterDim) => only === null || only === dim;

    if (want("site")) bump(maps.sites, hit.site_id);
    const a = hit.attrs;
    if (!a) continue;
    if (want("year") && a.year !== null) bumpNum(maps.years, a.year);
    // 季/集：一个种子可能覆盖多季/多集（S01-S05 / E01-E06），逐值计数
    if (want("season")) for (const s of a.seasons) bumpNum(maps.seasons, s);
    if (want("episode")) for (const e of a.episodes) bumpNum(maps.episodes, e);
    if (want("resolution") && a.resolution) bump(maps.resolution, a.resolution);
    if (want("source")) {
      if (a.media_source) bump(maps.source, a.media_source);
      if (a.remux) bump(maps.source, "Remux");
    }
    if (want("platform")) for (const v of a.platforms ?? []) bump(maps.platform, v);
    if (want("codec") && a.video_codec) bump(maps.codec, a.video_codec);
    if (want("hdr")) for (const v of a.hdr) bump(maps.hdr, v);
    if (want("audio")) for (const v of a.audio) bump(maps.audio, v);
    if (want("subtitle")) {
      {
        // 分面计数与前缀筛选语义对齐："中文（不限简繁）"的计数要涵盖
        // 声明了 zh-Hans/zh-Hant 的资源（每条资源对每个分面键至多计一次）
        const langs = a.subtitle_languages ?? [];
        const keys = new Set(langs);
        if (langs.some((v) => v.startsWith("zh-"))) keys.add("zh");
        for (const v of keys) bump(maps.subtitles, v);
      }
    }
    if (want("group") && a.release_group) bump(maps.groups, a.release_group);
  }
  return maps;
}

function aggregateFacets(items: TorrentHit[], filters: Filters): Facets {
  // 全量计数定「有哪些 chip、按什么顺序排」——筛选变化时 chip 不增删、不换位，
  // 已选中的值也不会因为计数归零而消失（否则没法取消勾选）；数字才跟着筛选走。
  const full = collectFacetMaps(items, null);
  const counts = hasActiveFilters(filters) ? collectFacetMaps(items, filters) : full;

  const toSorted = (key: keyof FacetMaps & string, cap: number): FacetValue[] =>
    [...(full[key] as Map<string, number>).entries()]
      .sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
      .slice(0, cap)
      .map(([value]) => ({
        value,
        count: (counts[key] as Map<string, number>).get(value) ?? 0,
      }));
  const toNumeric = (key: "years" | "seasons" | "episodes", order: 1 | -1): NumericFacetValue[] =>
    [...full[key].entries()]
      .sort((x, y) => (x[0] - y[0]) * order)
      .map(([value]) => ({ value, count: counts[key].get(value) ?? 0 }));

  return {
    // 分辨率是唯一的常驻 chips 维度，上限收紧防止挤爆一行；其余在弹层里，可放宽
    resolution: toSorted("resolution", 8),
    years: toNumeric("years", -1),
    seasons: toNumeric("seasons", 1),
    episodes: toNumeric("episodes", 1),
    source: toSorted("source", 50),
    platform: toSorted("platform", 50),
    codec: toSorted("codec", 50),
    hdr: toSorted("hdr", 50),
    audio: toSorted("audio", 50),
    subtitles: toSorted("subtitles", 50),
    groups: toSorted("groups", 50),
    sites: counts.sites,
  };
}

/* —— 作品聚合：分组视图的组头元数据 —— */

interface EntityGroup {
  key: string;
  /** 中文主名 / 外文主名：组头展示用，组内首个观测到的非空值 */
  nameZh: string | null;
  nameEn: string | null;
  year: number | null;
  mediaType: string | null;
  /** 题材轴（动漫/纪录片…），组内首个观测值 */
  contentType: string | null;
}

/**
 * 从全量结果收集各作品的组头元数据（键 → 名称/年份/类型）。
 * 组内字段取首个非空观测值：同组种子的解析结果一致性很高，缺失的由后来者补齐。
 */
function collectEntities(items: TorrentHit[]): Map<string, EntityGroup> {
  const groups = new Map<string, EntityGroup>();
  for (const hit of items) {
    const key = entityKeyOf(hit);
    if (key === ENTITY_UNPARSED) continue;
    const a = hit.attrs;
    const g = groups.get(key);
    if (g) {
      g.nameZh ??= a?.titles_zh?.[0] ?? null;
      g.nameEn ??= a?.titles_en?.[0] ?? null;
      g.year ??= a?.year ?? null;
      g.contentType ??= a?.content_type ?? null;
    } else {
      groups.set(key, {
        key,
        nameZh: a?.titles_zh?.[0] ?? null,
        nameEn: a?.titles_en?.[0] ?? null,
        year: a?.year ?? null,
        mediaType: a?.media_type ?? null,
        contentType: a?.content_type ?? null,
      });
    }
  }
  return groups;
}

export function SearchResults({ query, onResearch, grabForSubscriptionId }: SearchResultsProps) {
  const scrollRef = useScrollRestoration(
    `search:torrent:${query.keyword}:${query.scope.label ?? "all"}:${query.scope.categories.join(",")}:${query.scope.siteIds.join(",")}:${query.snapshotId ?? "live"}`,
  );
  const [phase, setPhase] = useState<Phase>("connecting");
  // 手动选种模式：拉一次订阅标题供横幅与按钮提示；订阅不存在则静默退出该模式
  const [grabTarget, setGrabTarget] = useState<{ id: number; title: string } | null>(null);
  useEffect(() => {
    if (!grabForSubscriptionId) {
      setGrabTarget(null);
      return;
    }
    let cancelled = false;
    getSubscription(grabForSubscriptionId)
      .then((d) => {
        if (!cancelled) setGrabTarget({ id: d.id, title: d.media.title });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [grabForSubscriptionId]);
  const [fatalError, setFatalError] = useState<string | null>(null);
  // 结果按 site_result 事件到达顺序累加——快站先上屏，排序视图实时并入新结果
  const [items, setItems] = useState<TorrentHit[]>([]);
  const [siteProgress, setSiteProgress] = useState<SiteProgress[]>([]);
  // 整次搜索的总耗时（done 事件 / 快照回放），站点状态弹层的汇总行展示
  const [totalElapsedMs, setTotalElapsedMs] = useState<number | null>(null);
  // 快照预览态：非空 = 当前展示的是历史快照（值为快照生成时间，供提示条换算年龄）
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  // 排序偏好跨搜索保留（改排序不代表想对下一次搜索用别的排序）。
  // 初值优先取地址栏的 sort 参数（刷新 / 分享链接直达时恢复），否则默认做种数降序。
  // 本子树仅在登录确认后于客户端首次渲染（见 AuthGate 未登录返回 null），
  // 故可安全读取 window，不会有 SSR 水合不一致。
  const [sort, setSort] = useState<SortState>(readSortFromUrl);
  // 结果视图三选一：分组（默认）/ 列表（平铺种子）/ 图览。图览初值跟随本次搜索
  // 的范围——scope.posterMode 已由 parseSearchQuery 从地址栏 poster 参数解出，
  // 刷新时经这里自然恢复；平铺偏好走地址栏 view=list（见下方 URL 同步）。
  // 工具栏右侧的分段可随时切换，只影响展示，不写回任何设置。
  const [view, setView] = useState<ResultView>(() => initialView(query.scope.posterMode));

  // 分页（各站独立分页，后端 page 参数早已支持）：page = 已加载到第几页；
  // exhausted = 上一次「加载更多」全站都没有新结果，按钮就此收起
  const [page, setPage] = useState(1);
  const [loadingMore, setLoadingMore] = useState(false);
  const [exhausted, setExhausted] = useState(false);
  // 补充流（单站重试 / 加载更多）的中止句柄：新搜索发起时一并中止
  const auxAbortsRef = useRef<Set<AbortController>>(new Set());

  // 视图态（排序 / 展示视图）实时同步进地址栏：让「点一下就变」的操作也能刷新保留、分享还原。
  // 用原生 replaceState 而非 router.replace——只改地址不触发 Next 路由/useSearchParams 更新，
  // 因此不会误判为新搜索而重新发起流式请求；默认值（做种降序 / 分组视图）不写入，保持地址简洁。
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (sort.key === "seeders" && sort.dir === "desc") params.delete("sort");
    else params.set("sort", `${sort.key}:${sort.dir}`);
    if (view === "poster") params.set("poster", "1");
    else params.delete("poster");
    if (view === "list") params.set("view", "list");
    else params.delete("view");
    const qs = params.toString();
    window.history.replaceState(
      window.history.state,
      "",
      qs ? `${window.location.pathname}?${qs}` : window.location.pathname,
    );
  }, [sort, view]);

  // 查询（关键词/范围）变化即重新发起流式搜索；用 AbortController 中断上一条流，避免竞态。
  // 新搜索同时重置筛选条件——旧结果的筛选对新结果没有意义。
  // 依赖用 query 对象本身：app-shell 每次提交都会构造新对象，成员深比较无必要。
  useEffect(() => {
    const controller = new AbortController();
    setPhase("connecting");
    setFatalError(null);
    setItems([]);
    setSiteProgress([]);
    setTotalElapsedMs(null);
    setSnapshotAt(null);
    setFilters(emptyFilters());
    setPage(1);
    setLoadingMore(false);
    setExhausted(false);
    // 上一轮搜索的补充流（单站重试/加载更多）随主流一起作废
    for (const aux of auxAbortsRef.current) aux.abort();
    auxAbortsRef.current.clear();
    // 图览跟随新搜索的范围预设；列表/分组是跨搜索保留的用户偏好（同排序），
    // 从图览预设离开时回到默认的分组视图
    setView((prev) =>
      query.scope.posterMode ? "poster" : prev === "poster" ? "group" : prev,
    );

    // 快照预览：不打扰任何站点，直接加载历史留存的结果集一次性上屏。
    // 站点状态（含逐站耗时/失败原因）从快照回放，过程面板/进度条自然不出现。
    if (query.snapshotId != null) {
      getTorrentSearchHistoryResults(query.snapshotId, { signal: controller.signal })
        .then((snap) => {
          setItems(snap.items);
          setSiteProgress(
            snap.sites.map((s) => ({
              site_id: s.site_id,
              site_name: s.site_name,
              phase: s.error ? "error" : "ok",
              count: s.count,
              error: s.error,
              elapsed_ms: s.elapsed_ms ?? null,
            })),
          );
          setTotalElapsedMs(snap.elapsed_ms ?? null);
          setSnapshotAt(snap.snapshot_at);
          setPhase("done");
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return;
          setPhase("error");
          setFatalError(
            err instanceof Error ? err.message : "快照加载失败，请稍后重试",
          );
        });
      return () => controller.abort();
    }

    // 用最新事件更新单个站点的进度
    const patchSite = (siteId: string, patch: Partial<SiteProgress>) =>
      setSiteProgress((prev) =>
        prev.map((s) => (s.site_id === siteId ? { ...s, ...patch } : s)),
      );

    streamSearchTorrents(
      { keyword: query.keyword, scope: query.scope },
      (event) => {
        switch (event.type) {
          case "start":
            setPhase("streaming");
            setSiteProgress(
              event.data.sites.map((s) => ({
                ...s,
                phase: "searching",
                count: 0,
                error: null,
                elapsed_ms: null,
              })),
            );
            break;
          case "site_start":
            break; // 逐站进度由 start 事件建好的 siteProgress 承载，无需额外处理
          case "site_result": {
            const d = event.data;
            setItems((prev) => [...prev, ...d.items]);
            patchSite(d.site_id, { phase: "ok", count: d.count, elapsed_ms: d.elapsed_ms });
            break;
          }
          case "site_error": {
            const d = event.data;
            patchSite(d.site_id, { phase: "error", error: d.error, elapsed_ms: d.elapsed_ms });
            break;
          }
          case "done":
            setTotalElapsedMs(event.data.elapsed_ms);
            setPhase("done");
            break;
        }
      },
      { signal: controller.signal },
    ).catch((err: unknown) => {
      if (controller.signal.aborted) return; // 被新搜索中断，忽略
      setPhase("error");
      setFatalError(err instanceof Error ? err.message : "搜索失败，请稍后重试");
    });
    return () => controller.abort();
  }, [query]);

  /**
   * 单站重试（站点状态弹层的「重试」）：清掉该站旧结果，仅对它重发一次流。
   * 只搜当前第 1 页——重试的典型场景是修完 Cookie 回来验证，不必追平
   * 「加载更多」的页深。结果就地并入当前筛选/排序视图。
   */
  const retrySite = (siteId: string) => {
    setItems((prev) => prev.filter((h) => h.site_id !== siteId));
    setSiteProgress((prev) =>
      prev.map((s) =>
        s.site_id === siteId
          ? { ...s, phase: "searching", count: 0, error: null, elapsed_ms: null }
          : s,
      ),
    );
    const controller = new AbortController();
    auxAbortsRef.current.add(controller);
    streamSearchTorrents(
      { keyword: query.keyword, scope: { ...query.scope, siteIds: [siteId] } },
      (event) => {
        if (event.type === "site_result") {
          const d = event.data;
          setItems((prev) => [...prev, ...d.items]);
          setSiteProgress((prev) =>
            prev.map((s) =>
              s.site_id === d.site_id
                ? { ...s, phase: "ok", count: d.count, error: null, elapsed_ms: d.elapsed_ms }
                : s,
            ),
          );
        } else if (event.type === "site_error") {
          const d = event.data;
          setSiteProgress((prev) =>
            prev.map((s) =>
              s.site_id === d.site_id
                ? { ...s, phase: "error", error: d.error, elapsed_ms: d.elapsed_ms }
                : s,
            ),
          );
        }
      },
      { signal: controller.signal },
    )
      .catch(() => {
        if (controller.signal.aborted) return;
        setSiteProgress((prev) =>
          prev.map((s) =>
            s.site_id === siteId && s.phase === "searching"
              ? { ...s, phase: "error", error: "重试失败，请稍后再试" }
              : s,
          ),
        );
      })
      .finally(() => auxAbortsRef.current.delete(controller));
  };

  /**
   * 加载更多（各站独立分页）：以 page+1 重发一次全范围流，新结果去重后追加。
   * 全部站点都没有带来新条目 → 视为到底，按钮收起。
   */
  const loadMore = () => {
    if (loadingMore || exhausted) return;
    const next = page + 1;
    setLoadingMore(true);
    const controller = new AbortController();
    auxAbortsRef.current.add(controller);
    let added = 0;
    streamSearchTorrents(
      { keyword: query.keyword, scope: query.scope, page: next },
      (event) => {
        if (event.type !== "site_result") return;
        const d = event.data;
        setItems((prev) => {
          // 站点忽略 page 参数时会原样重发第一页，按 (站点, 种子) 去重兜住
          const seen = new Set(prev.map((h) => `${h.site_id}/${h.torrent_id}`));
          const fresh = d.items.filter((h) => !seen.has(`${h.site_id}/${h.torrent_id}`));
          added += fresh.length;
          return fresh.length > 0 ? [...prev, ...fresh] : prev;
        });
      },
      { signal: controller.signal },
    )
      .then(() => {
        if (controller.signal.aborted) return;
        setPage(next);
        if (added === 0) setExhausted(true);
      })
      .catch(() => undefined)
      .finally(() => {
        auxAbortsRef.current.delete(controller);
        setLoadingMore(false);
      });
  };

  const facets = useMemo(() => aggregateFacets(items, filters), [items, filters]);
  const entities = useMemo(() => collectEntities(items), [items]);
  const filtered = useMemo(
    () => items.filter((hit) => matchesFilters(hit, filters)),
    [items, filters],
  );
  const sorted = useMemo(() => {
    const dir = sort.dir === "asc" ? 1 : -1;
    // 先逐条算好排序向量再排序：比较函数里现算会在 N·logN 次比较中反复
    // 分配数组（千条结果约数万次），流式进结果期间每批都要全量重排，很可感
    const decorated = filtered.map((hit) => ({ hit, vector: sortVector(hit, sort.key) }));
    decorated.sort((x, y) => {
      for (let i = 0; i < x.vector.length; i++) {
        if (x.vector[i] !== y.vector[i]) return (x.vector[i] - y.vector[i]) * dir;
      }
      return 0;
    });
    return decorated.map((d) => d.hit);
  }, [filtered, sort]);

  // 智能排序键的动态注入：按全量结果（而非筛选后）的类型构成决定——
  // 剧集过半给「全集优先」，电影过半给「画质优先」，「免费优先」常驻。
  // 用全量口径可避免筛选把菜单项筛没导致选中项凭空消失。
  const smartSortKeys = useMemo(() => {
    let tv = 0;
    let movie = 0;
    for (const h of items) {
      if (h.attrs?.media_type === "tv") tv++;
      else if (h.attrs?.media_type === "movie") movie++;
    }
    const keys: SortKey[] = [];
    const typed = tv + movie;
    if (typed > 0 && tv >= typed / 2) keys.push("complete");
    if (typed > 0 && movie > typed / 2) keys.push("quality");
    keys.push("free");
    return keys;
  }, [items]);
  const filtering = hasActiveFilters(filters);

  // 已完成站点的状态视图：筛选弹层的「站点」组与条件回显只认已出结果的站点
  const settledStatuses: SiteSearchStatus[] = useMemo(
    () =>
      siteProgress
        .filter((s) => s.phase !== "searching")
        .map((s) => ({
          site_id: s.site_id,
          site_name: s.site_name,
          count: s.count,
          error: s.error,
          elapsed_ms: s.elapsed_ms,
        })),
    [siteProgress],
  );
  const settledCount = settledStatuses.length;
  const streaming = phase === "connecting" || phase === "streaming";

  return (
    <GrabContext.Provider value={grabTarget}>
    <div className="relative flex h-full flex-col">
      {/* 手动选种横幅：从订阅详情页跳来时说明当前模式与退出方式 */}
      {grabTarget && (
        <div className="mx-6 mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-[#6aa7ff]/30 bg-[#6aa7ff]/12 px-4 py-2.5 text-sub text-[#b9d4ff] backdrop-blur-sm max-md:mx-4">
          <span className="min-w-0">
            正在为《{grabTarget.title}》手动选种——点资源上的「投给订阅」直接下载并计入该订阅
            （跳过规则组限制）
          </span>
          <Link
            href={`/subscriptions/${grabTarget.id}` as Route}
            className="ml-auto shrink-0 font-medium text-white/85 hover:underline"
          >
            返回订阅 ›
          </Link>
        </div>
      )}
      {/* 顶部（常驻两行 + 按需一行）：
          状态行 = 搜索词/计数（左）+ 快照药丸/站点状态聚合 chip（右）
          工具栏 = 类型分段/分辨率（左）+ 筛选/排序/视图（右）
          条件回显行只在筛选激活时出现；流式期间再加一条 2px 进度线 */}
      {/* pt-4：上方还有 /search 页的垂直选项卡行（影视 | 站点资源），间距略收 */}
      <header className="relative z-20 shrink-0 px-6 pb-3 pt-4 max-md:px-4 max-md:pt-3">
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
          <h1 className="text-on-image text-title-lg font-semibold tracking-[-0.01em] text-white">
            {/* 浏览模式（无关键词）标题落在动作上，具体范围由右边的分类药丸交代 */}
            {query.keyword ? `“${query.keyword}”` : "最新资源"}
          </h1>
          {query.scope.label && (
            <span className="rounded-full bg-black/30 px-2.5 py-0.5 text-caption text-[var(--accent)] backdrop-blur-sm">
              {query.scope.label}
            </span>
          )}
          {phase === "streaming" && (
            <span className="text-on-image flex items-center gap-1.5 text-sub text-[rgba(243,245,249,0.75)]">
              <span className="size-1.5 animate-pulse rounded-full bg-[var(--accent)]" />
              已找到 {items.length} 条
            </span>
          )}
          {phase === "done" && (
            <span className="text-on-image text-sub text-[rgba(243,245,249,0.75)]">
              {filtering
                ? `筛选后 ${filtered.length} / 共 ${items.length} 条`
                : `共 ${items.length} 条结果`}
            </span>
          )}

          {/* 右侧状态组：快照年龄 + 重搜、站点状态聚合（点开看逐站详情） */}
          <div className="ml-auto flex items-center gap-2">
            {snapshotAt && phase === "done" && (
              <>
                <span
                  title="这是历史留存的结果快照，站点数据（做种数/促销/链接）可能已变化"
                  className="flex items-center gap-1.5 rounded-full border border-[#6aa7ff]/30 bg-[#6aa7ff]/12 px-2.5 py-1 text-caption text-[#b9d4ff] backdrop-blur-sm"
                >
                  <svg
                    viewBox="0 0 24 24"
                    className="size-[13px] shrink-0"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.8}
                    strokeLinecap="round"
                    aria-hidden="true"
                  >
                    <circle cx="12" cy="12" r="8.5" />
                    <path d="M12 7.5V12l3 2" />
                  </svg>
                  {formatRelativeTime(snapshotAt)}的快照
                </span>
                {onResearch && (
                  <button
                    type="button"
                    onClick={() => onResearch(query.keyword, query.scope)}
                    className="btn-accent rounded-full px-2.5 py-1 text-caption font-medium"
                  >
                    重新搜索
                  </button>
                )}
              </>
            )}
            <SiteStatusSummary
              sites={siteProgress}
              streaming={streaming}
              onRetrySite={snapshotAt ? undefined : retrySite}
              totalElapsedMs={totalElapsedMs}
            />
          </div>
        </div>
        {phase === "done" && siteProgress.length === 0 && !snapshotAt && (
          <p className="text-on-image mt-2 text-sub text-[rgba(243,245,249,0.7)]">
            当前没有「已启用且验证通过」的站点，请先在设置里配置站点。
          </p>
        )}
        {siteProgress.length > 0 && (
          <>
            <FilterToolbar
              sites={settledStatuses}
              facets={facets}
              filters={filters}
              onChange={setFilters}
              sort={sort}
              smartSortKeys={smartSortKeys}
              onSortChange={setSort}
              resultCount={filtered.length}
              view={view}
              onViewChange={setView}
            />
            <AppliedChips
              sites={settledStatuses}
              filters={filters}
              onChange={setFilters}
            />
          </>
        )}
        {streaming && (
          <SearchProgressBar settled={settledCount} total={siteProgress.length} />
        )}
      </header>

      {/* 主体：结果随 site_result 事件渐进出现，首批结果到达前保持骨架屏 */}
      <div
        ref={scrollRef}
        className="scroll-thin scroll-safe relative z-0 min-h-0 flex-1 overflow-y-auto px-6 pb-6 max-md:px-4"
      >
        {streaming && items.length === 0 && (
          <SkeletonList siteCount={siteProgress.length} />
        )}
        {phase === "error" && (
          <EmptyHint title="搜索出错" hint={fatalError ?? "搜索失败，请稍后重试"} />
        )}
        {sorted.length > 0 ? (
          view === "poster" ? (
            <PosterResults hits={sorted} />
          ) : view === "group" && entities.size >= 2 ? (
            // 分组视图且识别出至少两部作品：按作品分组（组头 = 片名中英文 + 年份）
            <GroupedResults hits={sorted} entities={entities} />
          ) : (
            // 列表视图；分组视图下单作品/全未识别时也退回平铺
            <ul className="space-y-2">
              {sorted.map((hit) => (
                <TorrentRow
                  key={`${hit.site_id}:${hit.torrent_id}`}
                  hit={hit}
                  showRawTitles={view === "list"}
                />
              ))}
            </ul>
          )
        ) : items.length ? (
          <EmptyHint
            title="没有符合筛选条件的结果"
            hint="放宽或清除顶部的筛选条件试试。"
          />
        ) : phase === "done" ? (
          <EmptyHint
            title="没有找到匹配的资源"
            hint={
              query.keyword
                ? "换个关键词，或检查已配置站点是否验证通过。"
                : "换个分类，或检查已配置站点是否验证通过。"
            }
          />
        ) : null}

        {/* 加载更多：各站独立分页取下一页；快照回放没有"更深的页"可取，不出现 */}
        {phase === "done" && !snapshotAt && items.length > 0 && !exhausted && (
          <div className="mt-4 flex justify-center">
            <button
              type="button"
              disabled={loadingMore}
              onClick={loadMore}
              className="btn-glass px-5 py-2 text-sub font-medium disabled:opacity-50"
            >
              {loadingMore ? "正在加载…" : `加载更多（第 ${page + 1} 页）`}
            </button>
          </div>
        )}
        {phase === "done" && exhausted && items.length > 0 && (
          <p className="mt-4 text-center text-caption text-[var(--text-faint)]">
            已加载全部 {page} 页结果
          </p>
        )}
      </div>

    </div>
    </GrabContext.Provider>
  );
}

/* —— 通用小件 —— */

const CHIP_ACTIVE_CLS =
  "border-white/[0.2] bg-white/[0.14] text-[var(--text)] shadow-[inset_0_1px_0_rgba(255,255,255,0.1)]";
const CHIP_IDLE_CLS =
  "border-white/[0.08] bg-white/[0.035] text-[var(--text-muted)] hover:border-white/[0.15] hover:bg-white/[0.07] hover:text-[var(--text)]";
const SEGMENT_ACTIVE_CLS =
  "bg-white/[0.14] font-medium text-[var(--text)] shadow-[inset_0_1px_0_rgba(255,255,255,0.1)]";
const SEGMENT_IDLE_CLS =
  "text-[var(--text-muted)] hover:bg-white/[0.05] hover:text-[var(--text)]";

/** 可切换的筛选 chip（带命中计数）。 */
function FacetChip({
  label,
  count,
  active,
  onToggle,
}: {
  label: string;
  count: number;
  active: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={active}
      className={`h-7 rounded-full border px-2.5 text-caption transition-all ${active ? CHIP_ACTIVE_CLS : CHIP_IDLE_CLS}`}
    >
      {label}
      <span className="tnum ml-1 opacity-60">{count}</span>
    </button>
  );
}

/** 在 Set 型筛选维度上切换一个值（不可变更新）。 */
function toggleIn<T>(set: Set<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

/* —— 作品分组视图 —— */

/** 组内默认展示的版本数上限；超出折叠成「展开其余 N 个版本」。 */
const GROUP_ROW_CAP = 4;

/**
 * 按作品分组的结果视图（识别出 ≥2 部作品时替代平铺，见调用处开关）。
 *
 * 分组顺序 = 按当前排序遍历结果时各作品的「首次出现」序——排序取做种数降序时，
 * 头部组自然是含最热种子的作品，全局排序的语义不丢；组内行序同理天然有序。
 * 未识别桶（解析不出片名）固定沉底，组内按原始种子名平铺。
 * 组头可点击折叠；组内超过 GROUP_ROW_CAP 条时余量收起，按需展开。
 */
function GroupedResults({
  hits,
  entities,
}: {
  hits: TorrentHit[];
  entities: Map<string, EntityGroup>;
}) {
  // 折叠/展开状态按组键记忆：筛选与排序变化不清空；新搜索时组件整体重挂载自然重置
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [uncapped, setUncapped] = useState<Set<string>>(() => new Set());

  // 分桶按 hits 缓存：折叠/展开等本地状态变化不必重新遍历全量结果
  const buckets = useMemo(() => {
    const buckets: { key: string; rows: TorrentHit[] }[] = [];
    const indexOf = new Map<string, number>();
    for (const hit of hits) {
      const key = entityKeyOf(hit);
      const i = indexOf.get(key);
      if (i === undefined) {
        indexOf.set(key, buckets.length);
        buckets.push({ key, rows: [hit] });
      } else {
        buckets[i].rows.push(hit);
      }
    }
    const unparsedAt = indexOf.get(ENTITY_UNPARSED);
    if (unparsedAt !== undefined && unparsedAt !== buckets.length - 1) {
      const [u] = buckets.splice(unparsedAt, 1);
      buckets.push(u);
    }
    return buckets;
  }, [hits]);

  return (
    <div className="space-y-4">
      {buckets.map(({ key, rows }) => {
        const meta = entities.get(key);
        const open = !collapsed.has(key);
        const showAll = uncapped.has(key) || rows.length <= GROUP_ROW_CAP;
        const shown = showAll ? rows : rows.slice(0, GROUP_ROW_CAP);
        return (
          <section key={key}>
            <GroupHeader
              meta={meta ?? null}
              rows={rows}
              open={open}
              onToggle={() => setCollapsed((prev) => toggleIn(prev, key))}
            />
            {open && (
              <>
                <ul className="mt-2 space-y-2 pl-4">
                  {shown.map((hit) => (
                    <TorrentRow key={`${hit.site_id}:${hit.torrent_id}`} hit={hit} grouped />
                  ))}
                </ul>
                {!showAll && (
                  <button
                    type="button"
                    onClick={() => setUncapped((prev) => toggleIn(prev, key))}
                    className="ml-4 mt-2 px-1 py-0.5 text-sub text-[var(--accent-2)] transition-colors hover:text-[var(--text)]"
                  >
                    展开其余 {rows.length - GROUP_ROW_CAP} 个版本 ▾
                  </button>
                )}
              </>
            )}
          </section>
        );
      })}
    </div>
  );
}

/** 组头：片名中英文 + 年份/类型/题材 + 组内聚合徽标（版本数、最高分辨率、免费数）。 */
function GroupHeader({
  meta,
  rows,
  open,
  onToggle,
}: {
  meta: EntityGroup | null;
  rows: TorrentHit[];
  open: boolean;
  onToggle: () => void;
}) {
  const unparsed = meta === null;
  const freeCount = rows.filter((h) => h.free || h.download_volume_factor === 0).length;
  const topRes = maxResolution(rows);
  // 组内存在明确标注全集的资源（"全N集"/COMPLETE）——不切排序也能一眼看到
  // 哪部剧能一包带走；季包不算（连载中的整季包不等于全集）
  const hasCompletePack = rows.some((h) => h.attrs?.complete === true);
  const info = unparsed
    ? "按原始名展示"
    : [
        meta.year,
        meta.mediaType && MEDIA_TYPE_LABEL[meta.mediaType],
        meta.contentType && CONTENT_TYPE_LABEL[meta.contentType],
      ]
        .filter(Boolean)
        .join(" · ");
  const header = (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="flex w-full items-center gap-3 rounded-2xl border border-white/[0.09] bg-white/[0.045] px-4 py-2.5 text-left backdrop-blur-xl transition-colors hover:border-white/[0.16] hover:bg-white/[0.07]"
    >
      <span
        className={`shrink-0 text-micro text-[var(--text-faint)] transition-transform ${open ? "rotate-90" : ""}`}
      >
        ▶
      </span>
      <span className="min-w-0 flex-1 truncate">
        <span className="text-body font-semibold text-[var(--text)]">
          {unparsed ? "未识别" : (meta.nameZh ?? meta.nameEn)}
        </span>
        {!unparsed && meta.nameZh && meta.nameEn && (
          <span className="ml-2 text-sub text-[var(--text-muted)]">{meta.nameEn}</span>
        )}
        {info && (
          <span className="tnum ml-2 text-sub text-[var(--text-faint)]">{info}</span>
        )}
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        <span className="tnum rounded-md bg-white/[0.05] px-1.5 py-0.5 text-micro text-[var(--text-muted)]">
          {rows.length} 个版本
        </span>
        {topRes && (
          <span className="rounded-md bg-white/[0.05] px-1.5 py-0.5 text-micro text-[var(--accent-2)]">
            最高 {topRes}
          </span>
        )}
        {hasCompletePack && <span className={COMPLETE_BADGE_CLS}>全集包</span>}
        {freeCount > 0 && (
          <span className="tnum rounded-md bg-[var(--ok)]/15 px-1.5 py-0.5 text-micro font-semibold text-[#79d193]">
            {freeCount} 个免费
          </span>
        )}
      </span>
    </button>
  );
  // 悬停组头直接浮出 PT 站原始标题（主标题 + 中文副标题），不做任何加工——
  // 组头是解析结果，原始串在这里对照；取组内当前排序的第一条为代表
  const sample = rows[0];
  if (unparsed || !sample || (!sample.title && !sample.subtitle)) return header;
  return (
    <Tooltip
      placement="top-start"
      content={
        <div className="space-y-1">
          {sample.title && <p>{sample.title}</p>}
          {sample.subtitle && <p className="text-[var(--text-muted)]">{sample.subtitle}</p>}
        </div>
      }
    >
      {header}
    </Tooltip>
  );
}

/** 组内最高分辨率（按数字位比较，2160p > 1080p > 720p），无解析值返回 null。 */
function maxResolution(rows: TorrentHit[]): string | null {
  let best: string | null = null;
  let bestNum = -1;
  for (const hit of rows) {
    const res = hit.attrs?.resolution;
    if (!res) continue;
    const num = parseInt(res, 10);
    if (Number.isFinite(num) && num > bestNum) {
      bestNum = num;
      best = res;
    }
  }
  return best;
}

/* —— 第一层：常驻工具栏 —— */

function FilterToolbar({
  sites,
  facets,
  filters,
  onChange,
  sort,
  smartSortKeys,
  onSortChange,
  resultCount,
  view,
  onViewChange,
}: {
  sites: SiteSearchStatus[];
  facets: Facets;
  filters: Filters;
  onChange: (f: Filters) => void;
  sort: SortState;
  smartSortKeys: SortKey[];
  onSortChange: (s: SortState) => void;
  resultCount: number;
  view: ResultView;
  onViewChange: (v: ResultView) => void;
}) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const sheetRef = useRef<HTMLDivElement>(null);
  useDismiss(sheetRef, () => setSheetOpen(false), sheetOpen);
  const badge = sheetSelectionCount(filters);
  // 当前选中的智能键即使不在注入列表里（URL 还原/上次搜索的偏好残留）也要
  // 保留其选项，否则 select 会显示空白
  const smartOptions = SMART_SORT_OPTIONS.filter(
    (o) => smartSortKeys.includes(o.key) || o.key === sort.key,
  );

  return (
    <div className="mt-3 flex flex-wrap items-center gap-3 rounded-2xl border border-white/[0.07] bg-black/[0.14] p-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] backdrop-blur-xl">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
        {/* 排序：高频操作，置于工具栏最前（键与方向合并成一颗胶囊） */}
        <SortDropdown sort={sort} smartOptions={smartOptions} onChange={onSortChange} />

        {/* 分辨率：最高频的筛选维度，直接平铺 chips；只放命中最多的前 3 个
            （聚合已按计数降序），长尾分辨率进「筛选」弹层 */}
        {facets.resolution.slice(0, 3).map(({ value, count }, i) => (
        <span key={value} className={i >= 2 ? "max-md:hidden" : undefined}>
        <FacetChip
          label={value}
          count={count}
          active={filters.resolution.has(value)}
          onToggle={() =>
            onChange({ ...filters, resolution: toggleIn(filters.resolution, value) })
          }
        />
        </span>
        ))}

        {/* 高频维度前置成单维度下拉（按重要性排：年份/季/压制组——年份直接
            区分同名不同作；站点属于来源信息，离下载决策远，留在「筛选」弹层）。
            只有可选值 ≥2（即真有区分度）才出现：搜具体片名时维度往往只剩单值，
            下拉毫无决策价值，隐去让工具栏随结果复杂度自适应收缩。
            全维度组合仍走右侧「筛选」弹层，两边共享同一份筛选状态 */}
        <div className="flex items-center gap-2 max-md:hidden">
        {facets.years.length >= 2 && (
          <FacetDropdown
            label="年份"
            options={facets.years.map((y) => ({
              value: y.value,
              label: String(y.value),
              count: y.count,
            }))}
            active={filters.year}
            onToggle={(v) => onChange({ ...filters, year: toggleIn(filters.year, v) })}
          />
        )}
        {facets.seasons.length >= 2 && (
          <FacetDropdown
            label="季"
            options={facets.seasons.map((s) => ({
              value: s.value,
              label: `第${s.value}季`,
              count: s.count,
            }))}
            active={filters.season}
            onToggle={(v) => onChange({ ...filters, season: toggleIn(filters.season, v) })}
          />
        )}
        {facets.groups.length >= 2 && (
          <FacetDropdown
            label="压制组"
            options={facets.groups.map((g) => ({
              value: g.value,
              label: g.value,
              count: g.count,
            }))}
            active={filters.group}
            onToggle={(v) => onChange({ ...filters, group: toggleIn(filters.group, v) })}
          />
        )}
        </div>
      </div>

      {/* 右侧：视图切换（分组/列表/图览）+ 筛选弹层入口（最后） */}
      <div className="relative ml-auto flex flex-wrap items-center justify-end gap-1.5">
        {/* 视图分段切换：只切换当次搜索的展示方式，图览的长期默认值在自定义分类里
            设置。纯图标 + tooltip：设置一次就不常动的偏好，不配占文字标签的宽度 */}
        <div className="flex items-center rounded-full border border-white/[0.08] bg-black/[0.16] p-0.5">
          {(
            [
              { v: "group", label: "分组", Icon: LayersIcon, hint: "分组：按作品聚合，组内是各版本" },
              { v: "list", label: "列表", Icon: ListIcon, hint: "列表：平铺展示每条种子" },
              { v: "poster", label: "图览", Icon: PhotoIcon, hint: "图览：带海报的结果以图墙展示" },
            ] as const
          ).map(({ v, label, Icon, hint }) => (
            <button
              key={v}
              type="button"
              aria-pressed={view === v}
              aria-label={label}
              title={hint}
              onClick={() => onViewChange(v)}
              className={`grid h-6 w-8 place-items-center rounded-full transition-all ${view === v ? SEGMENT_ACTIVE_CLS : SEGMENT_IDLE_CLS}`}
            >
              <Icon className="size-3.5" />
            </button>
          ))}
        </div>

        {/* 触发器与弹层同容器：useDismiss 以此为界判定「点外关闭」 */}
        <div ref={sheetRef} className="relative">
          <button
            type="button"
            onClick={() => setSheetOpen((v) => !v)}
            aria-expanded={sheetOpen}
            className={`flex h-7 items-center gap-1.5 rounded-full border px-3 text-caption transition-all ${
              badge > 0 ? CHIP_ACTIVE_CLS : CHIP_IDLE_CLS
            }`}
          >
            筛选
            {badge > 0 && (
              <span className="tnum rounded-full bg-[var(--accent)]/25 px-1.5 text-micro">
                {badge}
              </span>
            )}
            <span className={`text-micro opacity-70 transition-transform ${sheetOpen ? "rotate-180" : ""}`}>▾</span>
          </button>

          {sheetOpen && (
            <FilterSheet
              sites={sites}
              facets={facets}
              filters={filters}
              onChange={onChange}
              resultCount={resultCount}
              onClose={() => setSheetOpen(false)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/* —— 第二层：筛选弹层 —— */

/**
 * 浮层退出交互：Esc 或点击 ref 区域（浮层+触发器）以外即关闭。
 * 不用全屏遮罩层实现"点外关闭"——工具栏容器带 backdrop-filter，是 fixed
 * 定位的包含块，遮罩会被困在容器内（只盖住工具栏、盖不住结果区，点结果区
 * 关不掉弹层，与 Modal 基座踩过的坑同源）；文档级 pointerdown 判定不受
 * 定位上下文影响，且点外关闭时点击还能落到目标元素上（少点一次）。
 */
function useDismiss(
  ref: React.RefObject<HTMLElement | null>,
  onClose: () => void,
  active = true,
) {
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
    };
  }, [ref, onClose, active]);
}

/**
 * 单维度筛选下拉（工具栏高频维度前置用）：点开小面板勾选即生效、点空白/Esc
 * 即收。多数筛选只碰 1-2 个维度，前置后不必进全维度的「筛选」大弹层——
 * 大弹层被收窄为重度组合场景的入口，二者共享同一份筛选状态。
 */
function FacetDropdown<T extends string | number>({
  label,
  options,
  active,
  onToggle,
}: {
  label: string;
  options: { value: T; label: string; count: number }[];
  active: Set<T>;
  onToggle: (value: T) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  useDismiss(rootRef, () => setOpen(false), open);
  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex h-7 items-center gap-1.5 rounded-full border px-3 text-caption transition-all ${
          active.size > 0 ? CHIP_ACTIVE_CLS : CHIP_IDLE_CLS
        }`}
      >
        {label}
        {active.size > 0 && (
          <span className="tnum rounded-full bg-[var(--accent)]/25 px-1.5 text-micro">
            {active.size}
          </span>
        )}
        <span className={`text-micro opacity-70 transition-transform ${open ? "rotate-180" : ""}`}>
          ▾
        </span>
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-2 max-h-[46dvh] w-max max-w-[420px] overflow-y-auto rounded-2xl border border-white/[0.12] bg-[rgba(14,16,22,0.96)] p-3 shadow-2xl backdrop-blur-2xl max-md:max-w-[calc(100vw-2rem)]">
          <div className="flex flex-wrap gap-1.5">
            {options.map((o) => (
              <FacetChip
                key={o.value}
                label={o.label}
                count={o.count}
                active={active.has(o.value)}
                onToggle={() => onToggle(o.value)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * 排序胶囊：把「排序键 select + 独立方向按钮」合并成一个下拉。
 * 菜单里点新键 = 选中并重置为降序（所有键的直觉首选方向）；点当前键 = 翻转
 * 方向（表头排序惯例，菜单底部有文字提示）。胶囊标签实时显示「键名 + 箭头」。
 * 常规/智能两组，智能组按结果集类型动态注入（见 smartSortKeys）。
 */
function SortDropdown({
  sort,
  smartOptions,
  onChange,
}: {
  sort: SortState;
  smartOptions: { key: SortKey; label: string }[];
  onChange: (s: SortState) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  useDismiss(rootRef, () => setOpen(false), open);
  const arrow = sort.dir === "desc" ? "↓" : "↑";
  const label =
    [...SORT_OPTIONS, ...SMART_SORT_OPTIONS].find((o) => o.key === sort.key)?.label ?? sort.key;
  const pick = (key: SortKey) => {
    onChange(
      key === sort.key
        ? { key, dir: sort.dir === "desc" ? "asc" : "desc" }
        : { key, dir: "desc" },
    );
    setOpen(false);
  };
  const item = (o: { key: SortKey; label: string }) => (
    <button
      key={o.key}
      type="button"
      onClick={() => pick(o.key)}
      className={`flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-caption transition-colors ${
        o.key === sort.key
          ? "bg-white/[0.09] text-[var(--text)]"
          : "text-[var(--text-muted)] hover:bg-white/[0.06] hover:text-[var(--text)]"
      }`}
    >
      {o.label}
      {o.key === sort.key && <span className="text-sub">{arrow}</span>}
    </button>
  );
  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`排序：${label}${sort.dir === "desc" ? "降序" : "升序"}`}
        className={`flex h-7 items-center gap-1.5 rounded-full border px-3 text-caption transition-all ${CHIP_IDLE_CLS}`}
      >
        {label}
        <span className="text-sub">{arrow}</span>
        <span className={`text-micro opacity-70 transition-transform ${open ? "rotate-180" : ""}`}>
          ▾
        </span>
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-2 w-44 rounded-2xl border border-white/[0.12] bg-[rgba(14,16,22,0.96)] p-1.5 shadow-2xl backdrop-blur-2xl">
          <p className="px-2.5 pb-1 pt-1.5 text-micro text-[var(--text-faint)]">常规</p>
          {SORT_OPTIONS.map(item)}
          {smartOptions.length > 0 && (
            <>
              <p className="px-2.5 pb-1 pt-2 text-micro text-[var(--text-faint)]">智能</p>
              {smartOptions.map(item)}
            </>
          )}
          <p className="border-t border-white/[0.08] px-2.5 pb-1 pt-2 text-micro text-[var(--text-faint)]">
            点击当前项可切换升降序
          </p>
        </div>
      )}
    </div>
  );
}

function SheetGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-1.5 text-caption font-medium text-[var(--text-faint)]">{title}</p>
      <div className="flex flex-wrap gap-1.5">{children}</div>
    </div>
  );
}

function FilterSheet({
  sites,
  facets,
  filters,
  onChange,
  resultCount,
  onClose,
}: {
  sites: SiteSearchStatus[];
  facets: Facets;
  filters: Filters;
  onChange: (f: Filters) => void;
  resultCount: number;
  onClose: () => void;
}) {
  const toggle = (key: SheetKey, value: string | number) =>
    onChange({ ...filters, [key]: toggleIn(filters[key] as Set<string | number>, value) });

  const okSites = sites.filter((s) => !s.error);

  return (
    <>
      {/* 点外/Esc 关闭由 FilterToolbar 的 useDismiss 承担（触发器与弹层同容器） */}
      {/* 宽度收着点：右侧浮层少遮结果列表，让「选择即生效」的变化被看见 */}
      <div className="absolute right-0 top-full z-30 mt-2 max-h-[60dvh] w-[480px] max-w-[82vw] overflow-y-auto rounded-2xl border border-white/[0.12] bg-[rgba(14,16,22,0.96)] p-4 shadow-2xl backdrop-blur-2xl">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-ui font-medium text-[var(--text)]">筛选结果</p>
            <p className="mt-0.5 text-caption text-[var(--text-faint)]">可多选，分类之间组合生效</p>
          </div>
          <button type="button" onClick={onClose} className={`grid size-7 place-items-center rounded-full border text-body ${CHIP_IDLE_CLS}`} aria-label="关闭筛选">
            ×
          </button>
        </div>
        <div className="space-y-3.5">
          {okSites.length > 0 && (
            <SheetGroup title="站点">
              {okSites.map((s) => (
                <FacetChip
                  key={s.site_id}
                  label={s.site_name}
                  // 计数走分面统计而非站点原始结果数：跟随其他维度的筛选联动
                  count={facets.sites.get(s.site_id) ?? 0}
                  active={filters.site.has(s.site_id)}
                  onToggle={() => toggle("site", s.site_id)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.years.length > 0 && (
            <SheetGroup title="年份">
              {facets.years.map((y) => (
                <FacetChip
                  key={y.value}
                  label={String(y.value)}
                  count={y.count}
                  active={filters.year.has(y.value)}
                  onToggle={() => toggle("year", y.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.seasons.length > 0 && (
            <SheetGroup title="季">
              {facets.seasons.map((s) => (
                <FacetChip
                  key={s.value}
                  label={`第${s.value}季`}
                  count={s.count}
                  active={filters.season.has(s.value)}
                  onToggle={() => toggle("season", s.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.episodes.length > 0 && (
            <SheetGroup title="集">
              {facets.episodes.map((ep) => (
                <FacetChip
                  key={ep.value}
                  label={`第${ep.value}集`}
                  count={ep.count}
                  active={filters.episode.has(ep.value)}
                  onToggle={() => toggle("episode", ep.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.source.length > 0 && (
            <SheetGroup title="片源">
              {facets.source.map((v) => (
                <FacetChip
                  key={v.value}
                  label={v.value}
                  count={v.count}
                  active={filters.source.has(v.value)}
                  onToggle={() => toggle("source", v.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.platform.length > 0 && (
            <SheetGroup title="流媒体平台">
              {facets.platform.map((v) => (
                <FacetChip
                  key={v.value}
                  label={platformLabel(v.value)}
                  count={v.count}
                  active={filters.platform.has(v.value)}
                  onToggle={() => toggle("platform", v.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.codec.length > 0 && (
            <SheetGroup title="视频编码">
              {facets.codec.map((v) => (
                <FacetChip
                  key={v.value}
                  label={v.value}
                  count={v.count}
                  active={filters.codec.has(v.value)}
                  onToggle={() => toggle("codec", v.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.hdr.length > 0 && (
            <SheetGroup title="HDR">
              {facets.hdr.map((v) => (
                <FacetChip
                  key={v.value}
                  label={v.value}
                  count={v.count}
                  active={filters.hdr.has(v.value)}
                  onToggle={() => toggle("hdr", v.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.audio.length > 0 && (
            <SheetGroup title="音频">
              {facets.audio.map((v) => (
                <FacetChip
                  key={v.value}
                  label={v.value}
                  count={v.count}
                  active={filters.audio.has(v.value)}
                  onToggle={() => toggle("audio", v.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.subtitles.length > 0 && (
            <SheetGroup title="字幕">
              {facets.subtitles.map((v) => (
                <FacetChip
                  key={v.value}
                  label={subtitleLanguageLabel(v.value)}
                  count={v.count}
                  active={filters.subtitle.has(v.value)}
                  onToggle={() => toggle("subtitle", v.value)}
                />
              ))}
            </SheetGroup>
          )}
          {facets.groups.length > 0 && (
            <SheetGroup title="压制组">
              {facets.groups.map((v) => (
                <FacetChip
                  key={v.value}
                  label={v.value}
                  count={v.count}
                  active={filters.group.has(v.value)}
                  onToggle={() => toggle("group", v.value)}
                />
              ))}
            </SheetGroup>
          )}
        </div>

        <div className="mt-4 flex items-center justify-between border-t border-white/[0.08] pt-3">
          <button
            type="button"
            onClick={() => onChange(emptyFilters())}
            className="btn-glass h-8 px-3 text-caption text-[var(--text-muted)]"
          >
            清除全部
          </button>
          <button
            type="button"
            onClick={onClose}
            className="btn-accent h-8 rounded-full px-4 text-caption font-medium"
          >
            查看 {resultCount} 条结果
          </button>
        </div>
      </div>
    </>
  );
}

/* —— 第三层：已应用条件回显行 —— */

function AppliedChips({
  sites,
  filters,
  onChange,
}: {
  sites: SiteSearchStatus[];
  filters: Filters;
  onChange: (f: Filters) => void;
}) {
  if (sheetSelectionCount(filters) === 0) return null;

  const siteName = (id: string) =>
    sites.find((s) => s.site_id === id)?.site_name ?? id;
  const labelOf = (key: SheetKey, value: string | number): string => {
    if (key === "site") return siteName(String(value));
    if (key === "season") return `第${value}季`;
    if (key === "episode") return `第${value}集`;
    if (key === "subtitle") return subtitleLanguageLabel(String(value));
    return String(value);
  };

  const chips = SHEET_KEYS.flatMap((key) =>
    [...filters[key]].map((value) => ({ key, value })),
  );

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5 px-1">
      <span className="text-caption text-[var(--text-faint)]">生效条件</span>
      {chips.map(({ key, value }) => (
        <button
          key={`${key}:${value}`}
          type="button"
          onClick={() =>
            onChange({ ...filters, [key]: toggleIn(filters[key] as Set<string | number>, value) })
          }
          className={`group flex h-6 items-center gap-1 rounded-full border px-2 text-caption transition-all hover:bg-white/[0.2] ${CHIP_ACTIVE_CLS}`}
        >
          {labelOf(key, value)}
          <span className="opacity-60 transition-opacity group-hover:opacity-100">×</span>
        </button>
      ))}
      <button
        type="button"
        onClick={() => onChange(emptyFilters())}
        className="ml-1 text-caption text-[var(--text-faint)] transition-colors hover:text-[var(--text)]"
      >
        清除全部
      </button>
    </div>
  );
}

/* —— 站点状态：状态行右侧的聚合 chip + 逐站详情弹层 —— */

/**
 * 把逐站进度压缩成一枚聚合 chip（原来独占一行的逐站 chips 太占页头高度）：
 * 搜索中显示「x/N 站点」带脉冲点，完成后显示「N 站点（· M 失败）」，
 * 状态点颜色 = 整体健康度（搜索中琥珀脉冲 / 全部成功绿 / 有失败红）。
 *
 * 点击弹出逐站详情，每站一行三要素齐全：**状态**（色点+文字）、**命中条数**、
 * **耗时**——成功失败都有耗时，十几秒后才失败的一眼可辨是超时，秒失败的多半
 * 是认证/解析问题。底部汇总行给出整次搜索的总耗时；快照回放时数据同样齐全。
 */
function SiteStatusSummary({
  sites,
  streaming,
  totalElapsedMs,
  onRetrySite,
}: {
  sites: SiteProgress[];
  streaming: boolean;
  totalElapsedMs: number | null;
  /** 单站重试（失败行的行动出口）；不传则失败行只读 */
  onRetrySite?: (siteId: string) => void;
}) {
  const [open, setOpen] = useState(false);

  if (sites.length === 0) return null;

  const settled = sites.filter((s) => s.phase !== "searching").length;
  const failed = sites.filter((s) => s.phase === "error").length;
  const dotCls = streaming
    ? "animate-pulse bg-[var(--accent)]"
    : failed > 0
      ? "bg-[#ff6b6b]"
      : "bg-[var(--ok)]";

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title="查看各站点的搜索详情"
        className="flex items-center gap-1.5 rounded-full border border-white/[0.12] bg-white/[0.05] px-2.5 py-1 text-caption text-[var(--text-muted)] backdrop-blur-sm transition-colors hover:border-white/[0.22] hover:text-[var(--text)]"
      >
        <span className={`size-1.5 rounded-full ${dotCls}`} />
        {streaming ? (
          <span className="tnum">{settled}/{sites.length} 站点</span>
        ) : (
          <>
            <span className="tnum">{sites.length} 站点</span>
            {failed > 0 && (
              <span className="tnum text-[#ff9a9a]">· {failed} 失败</span>
            )}
          </>
        )}
        <span className="text-micro opacity-70">▾</span>
      </button>

      {open && (
        <>
          {/* 点击空白处关闭（与筛选弹层同款交互） */}
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-30 mt-2 w-[320px] max-w-[82vw] rounded-2xl border border-white/[0.12] bg-[rgba(14,16,22,0.94)] p-2 shadow-2xl backdrop-blur-2xl">
            <ul className="flex flex-col">
              {sites.map((s) => (
                <li key={s.site_id} className="rounded-lg px-2 py-1.5">
                  <div className="flex items-center gap-2">
                    <span
                      className={`size-1.5 shrink-0 rounded-full ${
                        s.phase === "searching"
                          ? "animate-pulse bg-[var(--accent)]"
                          : s.phase === "error"
                            ? "bg-[#ff6b6b]"
                            : "bg-[var(--ok)]"
                      }`}
                    />
                    <span className="min-w-0 flex-1 truncate text-sub text-[var(--text)]">
                      {s.site_name}
                    </span>
                    {/* 右侧固定：状态 + 条数/耗时。失败也带耗时——超时一眼可辨 */}
                    {s.phase === "searching" && (
                      <span className="shrink-0 text-caption text-[var(--text-faint)]">
                        搜索中…
                      </span>
                    )}
                    {s.phase === "ok" && (
                      <span className="tnum shrink-0 text-caption text-[var(--text-muted)]">
                        {s.count} 条
                        {s.elapsed_ms !== null && (
                          <span className="text-[var(--text-faint)]">
                            {" "}· {formatElapsed(s.elapsed_ms)}
                          </span>
                        )}
                      </span>
                    )}
                    {s.phase === "error" && (
                      <span className="tnum shrink-0 text-caption text-[#ff9a9a]">
                        失败
                        {s.elapsed_ms !== null && (
                          <span className="opacity-75"> · {formatElapsed(s.elapsed_ms)}</span>
                        )}
                      </span>
                    )}
                  </div>
                  {/* 失败原因独立成行，不再和站名挤在一行里被截断到没法读 */}
                  {s.phase === "error" && s.error && (
                    <p
                      title={s.error}
                      className="mt-0.5 line-clamp-2 pl-3.5 text-caption leading-4 text-[#ff9a9a]/80"
                    >
                      {s.error}
                    </p>
                  )}
                  {/* 失败行的行动出口：就地重试 / 去站点设置修凭据——此前只能
                      读完错误自己记住站名再去设置里翻（订阅体检早有 fix 跳转，
                      这里拉齐同一标准） */}
                  {s.phase === "error" && (
                    <div className="mt-1 flex gap-2 pl-3.5">
                      {onRetrySite && (
                        <button
                          type="button"
                          onClick={() => onRetrySite(s.site_id)}
                          className="rounded-md bg-white/[0.08] px-2 py-0.5 text-caption font-medium text-white/80 transition hover:bg-white/[0.15]"
                        >
                          重试该站
                        </button>
                      )}
                      <Link
                        href={"/settings/sites" as Route}
                        className="rounded-md bg-white/[0.08] px-2 py-0.5 text-caption font-medium text-white/80 transition hover:bg-white/[0.15]"
                      >
                        去站点设置 ›
                      </Link>
                    </div>
                  )}
                </li>
              ))}
            </ul>
            {/* 汇总行：整次搜索的总耗时（≈ 最慢站点耗时）；快照回放同样有值 */}
            {totalElapsedMs !== null && (
              <div className="mt-1 border-t border-white/[0.08] px-2 pb-0.5 pt-1.5 text-caption text-[var(--text-faint)]">
                总耗时 {formatElapsed(totalElapsedMs)}
                <span className="opacity-70">（以最慢的站点为准）</span>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/* —— 图览模式：海报墙 + 无图结果回退列表 —— */

/**
 * 图览模式的结果主体：带海报的结果渲染成海报网格，没有海报的（多数站点
 * 不返回 poster_url）仍按普通列表排在其后——两种视图共享同一套筛选与排序。
 * 全部结果都无海报时不渲染空网格，整页自然退化为普通列表。
 */
function PosterResults({ hits }: { hits: TorrentHit[] }) {
  // 流式期间每批结果都重渲染本组件，分桶结果按 hits 缓存
  const { withPoster, withoutPoster } = useMemo(() => {
    const withPoster: TorrentHit[] = [];
    const withoutPoster: TorrentHit[] = [];
    for (const h of hits) (h.poster_url ? withPoster : withoutPoster).push(h);
    return { withPoster, withoutPoster };
  }, [hits]);
  return (
    <div className="space-y-5">
      {withPoster.length > 0 && (
        <ul className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3">
          {withPoster.map((hit) => (
            <TorrentPosterCard key={`${hit.site_id}:${hit.torrent_id}`} hit={hit} />
          ))}
        </ul>
      )}
      {withoutPoster.length > 0 && (
        <div>
          {withPoster.length > 0 && (
            <p className="text-on-image mb-2 text-caption text-[rgba(243,245,249,0.7)]">
              以下 {withoutPoster.length} 条结果没有海报，按列表展示
            </p>
          )}
          <ul className="space-y-2">
            {withoutPoster.map((hit) => (
              <TorrentRow key={`${hit.site_id}:${hit.torrent_id}`} hit={hit} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * 种子海报卡片（issue #74 重设计）：参照媒体库 PosterCard 的「徽章贴图内、
 * 文字放图外」——海报内只留实底徽章（左上促销、右上张数、左下剧集季/集），
 * 标题/年份/清晰度/体积/站点等文字全部放在海报下方的实底文字区，
 * 不再压在海报渐变上（浅色海报下白字会糊进封面，且此前完全看不出集数）。
 * 与媒体条目的 PosterCard（components/poster-card.tsx）是两套东西——这里的数据
 * 是 TorrentHit，徽章是促销信息，点击弹多图灯箱而非进详情页，故不共用卡片层，
 * 仅通过 PosterImage 共用海报图片底座（懒加载 / no-referrer / 失败回退）。
 * 点击卡片弹出多图灯箱（海报 + image_urls 里的截图等），右上角
 * 显示张数徽标；hover 上的详情/下载链接 stopPropagation，不触发灯箱。
 */
/**
 * 海报卡片左上角的促销徽标（免费 / 折扣 / 双倍上传 / H&R）。
 *
 * 与列表模式的 PromoBadges 同一判断口径，但样式为海报适配：用较实的底色
 * 保证叠在明亮海报上依然清晰（不用 backdrop-blur——海报墙每张卡多个模糊
 * 合成层会放大滚动 GPU 压力）；免费最醒目（实绿底深字），
 * 因为「是否免费」是海报模式下用户最关心的下载决策依据。
 * 左上只放促销（回答「值不值得下」）；全集信息挪到了左下季/集 chip
 * （seasonEpisodeChip，回答「是哪些内容」），两类信息各归各位。
 */
function posterPromoBadges(hit: TorrentHit): { text: string; cls: string }[] {
  const badges: { text: string; cls: string }[] = [];
  if (hit.free || hit.download_volume_factor === 0) {
    badges.push({ text: "免费", cls: "bg-[var(--ok)]/90 text-[#052e16]" });
  } else if (hit.download_volume_factor < 1) {
    badges.push({
      text: `${Math.round(hit.download_volume_factor * 100)}%`,
      cls: "bg-[#6aa7ff]/85 text-white",
    });
  }
  if (hit.upload_volume_factor > 1) {
    badges.push({ text: `${hit.upload_volume_factor}× 上传`, cls: "bg-[#c792ff]/85 text-white" });
  }
  if (hit.hit_and_run) {
    badges.push({ text: "H&R", cls: "bg-[#f59e0b]/90 text-[#3a2600]" });
  }
  return badges;
}

/** 数字列表 → 展示串：连续用区间（1-6），不连续用顿号（1、3）。 */
function formatNumList(nums: number[]): string {
  const sorted = [...nums].sort((a, b) => a - b);
  const contiguous = sorted.every((n, i) => i === 0 || n === sorted[i - 1] + 1);
  if (sorted.length > 1 && contiguous) return `${sorted[0]}-${sorted[sorted.length - 1]}`;
  return sorted.join("、");
}

/**
 * 海报卡左下角的剧集季/集 chip（issue #74：图览模式看不出具体集数）。
 * 把解析出的季/集观测拼成一眼可读的一句话：「第2季 · 全12集」（全集包）/
 * 「第2季 · 第5集」（单集）/「第1-6集」（区间）/「第1-3季」（多季包）。
 * 电影或无任何季集信息返回 null，海报保持干净。
 * pack=true（全集 / 整季包，与 completenessTier 的 3+ 档同口径）时用醒目
 * 渐变底（与列表模式全集徽标 COMPLETE_BADGE_CLS 同视觉语言），
 * 单集/区间用黑底弱化——海报墙上扫一眼就能分辨「一包带走」和「零散单集」。
 */
function seasonEpisodeChip(attrs: TorrentAttrs | null): { text: string; pack: boolean } | null {
  if (!attrs || attrs.media_type === "movie") return null;
  const season = attrs.seasons.length > 0 ? `第${formatNumList(attrs.seasons)}季` : null;
  // 明确标注全集时集数展示成「全N集」，否则列出观测到的集
  const episode =
    completeLabel(attrs) ??
    (attrs.episodes.length > 0 ? `第${formatNumList(attrs.episodes)}集` : null);
  if (!season && !episode) return null;
  const pack = attrs.complete === true || (!!season && attrs.episodes.length === 0);
  return { text: [season, episode].filter(Boolean).join(" · "), pack };
}

// memo：与 TorrentRow 同理，流式进结果时已有卡片的 hit 引用不变，整卡跳过
const TorrentPosterCard = memo(function TorrentPosterCard({ hit }: { hit: TorrentHit }) {
  const [viewerOpen, setViewerOpen] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const size = hit.size ?? formatBytes(hit.size_bytes);
  const name = parsedName(hit);
  const seChip = seasonEpisodeChip(hit.attrs);
  // 灯箱图集：海报 + 全部图片（poster_url 通常是 image_urls 第一张，去重兜底）
  const gallery = Array.from(
    new Set([hit.poster_url, ...hit.image_urls].filter((u): u is string => !!u)),
  ).map((url) => cachedImageUrl(url));
  return (
    <li className="group relative overflow-hidden rounded-xl border border-white/[0.08] bg-[rgba(14,16,22,0.75)] transition-colors hover:border-white/[0.2]">
      {/* 手机/纯触摸设备把整张卡作为操作入口；窄屏桌面预览也走同一交互，
          方便响应式调试。桌面鼠标环境下按钮不参与布局与命中。 */}
      {(hit.detail_url || hit.download_url) && (
        <button
          type="button"
          aria-label={`打开「${hit.title}」的资源操作`}
          onClick={() => setActionsOpen(true)}
          className="absolute inset-0 z-10 hidden cursor-pointer rounded-xl max-md:block [@media(hover:none)]:block"
        />
      )}
      <div
        role="button"
        tabIndex={0}
        // 标题展示解析片名后，原始种子名/副标题靠悬停提示查看（图外文字区同）
        title={rawTitleTooltip(hit)}
        aria-label={`浏览「${hit.title}」的 ${gallery.length} 张图片`}
        onClick={() => setViewerOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setViewerOpen(true);
          }
        }}
        className="relative aspect-[2/3] cursor-zoom-in outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
      >
        <PosterImage
          src={hit.poster_url ? cachedImageUrl(hit.poster_url) : undefined}
          alt={hit.title}
          className="absolute inset-0 size-full transition-transform duration-500 ease-out group-hover:scale-[1.04]"
          fallback={
            // 占位：海报缺失/加载失败时的深色底 + 居中站点名
            <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-b from-white/[0.05] to-black/40">
              <span className="px-3 text-center text-caption text-[var(--text-faint)]">
                {hit.site_name}
              </span>
            </div>
          }
        />
        {/* 顶部徽标：左上留给促销（免费最醒目，是下载与否的决策关键），右上图片张数。
            站点名不占这个最显眼的位置——它是次要的来源信息，下沉到底部弱化展示。 */}
        <div className="absolute inset-x-1.5 top-1.5 flex items-start justify-between gap-1">
          <span className="flex flex-col items-start gap-1">
            {posterPromoBadges(hit).map((b) => (
              <span
                key={b.text}
                className={`rounded-md px-1.5 py-0.5 text-micro font-semibold ${b.cls}`}
              >
                {b.text}
              </span>
            ))}
          </span>
          {/* 张数常显（含 1 张）：点开前就知道里面有几张图，只有一张时不必特意点开 */}
          <span
            title={`共 ${gallery.length} 张图片，点击卡片浏览`}
            className="tnum flex shrink-0 items-center gap-1 rounded-md bg-black/70 px-1.5 py-0.5 text-micro font-medium text-white/85"
          >
            <PhotoIcon className="size-3" />
            {gallery.length}
          </span>
        </div>
        {/* 左下：剧集季/集 chip。实底不受海报明暗影响；全集/整季包用渐变底
            醒目，单集/区间黑底弱化；电影不渲染，海报底部保持干净 */}
        {seChip && (
          <span
            className={`absolute bottom-1.5 left-1.5 rounded-md px-1.5 py-0.5 text-micro font-semibold ${
              seChip.pack
                ? "bg-gradient-to-r from-[#4f8cff]/90 to-[#9d6bff]/90 text-white"
                : "bg-black/70 text-[#b9d4ff]"
            }`}
          >
            {seChip.text}
          </span>
        )}
        {/* 桌面 hover：压暗 + 浮出操作（stopPropagation：点链接不触发灯箱）。
            手机端由整卡点击打开底部抽屉，不在海报上常驻操作。 */}
        {(hit.detail_url || hit.download_url) && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 bg-black/0 opacity-0 transition duration-200 group-hover:bg-black/35 group-hover:opacity-100 max-md:hidden [@media(hover:none)]:hidden">
            {hit.detail_url && (
              <a
                href={hit.detail_url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="rounded-lg border border-white/40 bg-black/40 px-2.5 py-1 text-caption text-white backdrop-blur-sm transition-colors hover:bg-black/60"
              >
                详情
              </a>
            )}
            <GrabButton
              hit={hit}
              className="rounded-lg border border-[#6aa7ff]/50 bg-[#6aa7ff]/25 px-2.5 py-1 text-caption font-medium text-white backdrop-blur-sm transition-colors hover:bg-[#6aa7ff]/40"
            />
            <DownloadButton
              hit={hit}
              className="btn-accent rounded-lg px-2.5 py-1 text-caption font-medium"
            />
          </div>
        )}
      </div>

      {/* 图外文字区（实底）：① 片名+年份 ② 清晰度·体积 ③ 站点·时间 + 右下角
          做种/下载数。任何海报明暗都不影响可读性；解析失败时回退原始种子名 */}
      <div title={rawTitleTooltip(hit)} className="px-2.5 pb-2.5 pt-2">
        <p
          className={`truncate text-sub font-medium ${
            name ? "text-[var(--text)]" : "text-[var(--text-muted)]"
          }`}
        >
          {name ? name.primary : hit.title}
          {name && hit.attrs?.year != null && (
            <span className="tnum ml-1.5 text-caption font-normal text-[var(--text-muted)]">
              {hit.attrs.year}
            </span>
          )}
        </p>
        {(hit.attrs?.resolution || size) && (
          <p className="tnum mt-0.5 truncate text-caption text-[var(--text-muted)]">
            {[hit.attrs?.resolution, size].filter(Boolean).join(" · ")}
          </p>
        )}
        <p className="mt-0.5 flex items-baseline gap-1.5 text-micro text-[var(--text-faint)]">
          <span className="min-w-0 truncate">
            {hit.site_name}
            {hit.upload_time ? ` · ${formatRelativeTime(hit.upload_time)}` : ""}
          </span>
          <span className="tnum ml-auto shrink-0">
            <span className="text-[#a7d9b6]">↑{hit.seeders}</span>
            <span className="ml-1 text-[#e0c19f]">↓{hit.leechers}</span>
          </span>
        </p>
      </div>

      {viewerOpen && (
        <ImageLightbox
          images={gallery}
          title={hit.title}
          onClose={() => setViewerOpen(false)}
        />
      )}
      <TorrentActionsSheet
        hit={hit}
        open={actionsOpen}
        onClose={() => setActionsOpen(false)}
        onViewImages={
          gallery.length > 0
            ? () => {
                setActionsOpen(false);
                setViewerOpen(true);
              }
            : undefined
        }
      />
    </li>
  );
});

/* —— 单条种子 —— */

/** 下载按钮的提交状态：终态（done/exists）不可再点，error 可点重试。 */
type DownloadState = "idle" | "submitting" | "done" | "exists" | "error";

const DOWNLOAD_LABEL: Record<DownloadState, string> = {
  idle: "下载",
  submitting: "提交中…",
  done: "已提交",
  exists: "已在下载器",
  error: "失败·重试",
};

/**
 * 下载按钮：点击先弹「选择保存位置」（download-target-dialog），用户确认
 * 文件落点后由弹窗提交到默认下载器（后端带站点登录态取回 .torrent）。
 * 种子解析出实体身份（类型+片名+年份）时，弹窗提供"智能入库"选项
 * （与订阅同源的三级兜底 + 预检警示）；同时列出下载器已配置目录双视角
 * 供手选。提交结果回填在按钮文字上；失败可悬停看原因、点击重试。
 */
function DownloadButton({ hit, className }: { hit: TorrentHit; className: string }) {
  const { canDirectDownload } = usePermissions();
  const toast = useToast();
  const [state, setState] = useState<DownloadState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [request, setRequest] = useState<DownloadTargetRequest | null>(null);
  if (!canDirectDownload || !hit.download_url) return null;

  const settled = state === "done" || state === "exists";

  function openDialog(e: React.MouseEvent) {
    e.stopPropagation();
    if (state === "submitting" || settled || !hit.download_url) return;
    setError(null);
    // 实体身份三件套齐全才提供"智能入库"选项（年份是防错挂的硬门槛）
    const attrs = hit.attrs;
    const title = attrs?.titles_zh?.[0] ?? attrs?.titles_en?.[0];
    const mediaType =
      attrs?.media_type === "movie" || attrs?.media_type === "tv" ? attrs.media_type : null;
    const req: DownloadTargetRequest = {
      site_id: hit.site_id,
      download_url: hit.download_url,
      torrent_id: hit.torrent_id,
      identity:
        mediaType && title && attrs?.year != null
          ? { kind: mediaType, title, year: attrs.year }
          : null,
      subtitle: hit.subtitle || null,
    };
    // 快速通道：记住过保存位置就直接提交，不再弹窗（批量下载 20 次点击 → 10 次）。
    // toast 带「更改」回到弹窗；目标失效（库被删等）自动回落弹窗
    const remembered = readRememberedTarget(req);
    if (remembered) {
      setState("submitting");
      submitRememberedTarget(req, remembered)
        .then((result) => {
          if (result === null) {
            setState("idle");
            setRequest(req);
            return;
          }
          setState(result.already_exists ? "exists" : "done");
          toast.success(
            result.already_exists
              ? "该种子已在下载器中，未重复添加"
              : `已提交到「${result.downloader_name}」${result.save_path ? ` · ${result.save_path}` : ""}`,
            { action: { label: "更改", onClick: () => setRequest(req) } },
          );
        })
        .catch((err) => {
          setState("error");
          setError(err instanceof Error ? err.message : "提交失败");
          toast.error(err instanceof Error ? err.message : "提交失败，请重试");
        });
      return;
    }
    setRequest(req);
  }

  return (
    <>
      <button
        type="button"
        onClick={openDialog}
        disabled={state === "submitting"}
        title={error ?? undefined}
        className={`${className}${settled ? " cursor-default opacity-75" : ""}${
          state === "submitting" ? " cursor-wait opacity-75" : ""
        }`}
      >
        {DOWNLOAD_LABEL[state]}
      </button>
      <DownloadTargetDialog
        request={request}
        onClose={() => setRequest(null)}
        onSubmitted={(result) => setState(result.already_exists ? "exists" : "done")}
      />
    </>
  );
}

/** 「投给订阅」按钮的提交状态：done 终态不可再点，error 可点重试。 */
type GrabState = "idle" | "submitting" | "done" | "error";

const GRAB_LABEL: Record<GrabState, string> = {
  idle: "投给订阅",
  submitting: "投递中…",
  done: "已投递",
  error: "失败·重试",
};

/**
 * 手动选种按钮：仅在选种模式（GrabContext 非空）出现在下载按钮旁。
 * 把当前搜索结果行原样交给 subscriptions.download-selected-torrent；
 * 身份对不上/没有可满足缺口时后端给可读中文错误，进 toast 并可重试。
 */
function GrabButton({ hit, className }: { hit: TorrentHit; className: string }) {
  const grabFor = useContext(GrabContext);
  const toast = useToast();
  const [state, setState] = useState<GrabState>("idle");
  if (!grabFor || !hit.download_url) return null;

  const grab = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (state === "submitting" || state === "done") return;
    setState("submitting");
    try {
      const { units } = await downloadSelectedTorrentForSubscription(grabFor.id, {
        site_id: hit.site_id,
        torrent_id: hit.torrent_id,
        title: hit.title,
        subtitle: hit.subtitle,
        category: hit.category,
        attrs: (hit.attrs as unknown as Record<string, unknown>) ?? null,
        download_url: hit.download_url,
        size_bytes: hit.size_bytes,
        seeders: hit.seeders,
        is_free: hit.free,
        hit_and_run: hit.hit_and_run,
        publish_time: hit.upload_time,
      });
      setState("done");
      toast.success(`已投给《${grabFor.title}》，覆盖 ${units.length} 个追踪单元`);
    } catch (err) {
      setState("error");
      toast.error(err instanceof Error ? err.message : "投递失败，请稍后重试");
    }
  };

  return (
    <button
      type="button"
      onClick={(e) => void grab(e)}
      disabled={state === "submitting"}
      title={`直接投给《${grabFor.title}》（跳过规则组限制）`}
      className={`${className}${state === "done" ? " cursor-default opacity-75" : ""}${
        state === "submitting" ? " cursor-wait opacity-75" : ""
      }`}
    >
      {GRAB_LABEL[state]}
    </button>
  );
}

/**
 * 做种数的健康度着色：让"这个种还活不活跃"变成扫一眼即得的预注意特征，
 * 不用逐行读数字。0=死种警示红，<5=濒危琥珀，≥100=充沛绿加粗，其余常规绿。
 */
function seederTone(n: number): string {
  if (n === 0) return "text-[#ff9a9a]";
  if (n < 5) return "text-[#fbbf24]";
  if (n >= 100) return "font-semibold text-[var(--ok)]";
  return "text-[#79d193]";
}

/**
 * 解析展示名：优先小模型抽取的片名，中文主名在前、外文主名做次级标注；
 * 只有外文名时它就是主名。两者都提取不到返回 null，调用方回退原始种子名。
 */
function parsedName(hit: TorrentHit): { primary: string; secondary: string | null } | null {
  const zh = hit.attrs?.titles_zh?.[0] ?? null;
  const en = hit.attrs?.titles_en?.[0] ?? null;
  if (zh) return { primary: zh, secondary: en };
  if (en) return { primary: en, secondary: null };
  return null;
}

/** 悬停提示：原始种子名 + 站点副标题（片名展示成解析结果后，原始信息从这里看）。 */
function rawTitleTooltip(hit: TorrentHit): string {
  return [hit.title, hit.subtitle].filter(Boolean).join("\n");
}

/**
 * 版本规格摘要（分组模式的行标题）：片名已由组头承担，行内只描述这个版本
 * 与同组其他版本的差异——季集 + 分辨率 + 片源/Remux + 编码 + HDR + 字幕 + 音轨
 * + 压制组。
 */
function specSummary(attrs: TorrentAttrs): string | null {
  const parts = [
    seasonEpLabel(attrs),
    attrs.resolution,
    ...(attrs.platforms ?? []).map(platformLabel),
    attrs.media_source,
    attrs.remux && "Remux",
    attrs.video_codec,
    ...attrs.hdr,
    compactSubtitleBadge(attrs),
    compactAudioBadge(attrs),
    ...attrs.audio.slice(0, 2),
    attrs.release_group,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/**
 * 行布局采用「左弹性标题区 + 右固定数字网格 + 操作区」三段式：
 * 体积/做种/完成/时间锁定在固定宽度的 2×2 网格里右对齐，跨行位置完全一致——
 * 眼睛沿一条垂直线扫下来即可完成全列表对比（Kayak/qBittorrent 式扫描列），
 * 不再混在自由流动的 meta 文本里逐行找数字。
 *
 * memo 化：流式搜索每批新结果都会重渲染整个列表，已有行的 hit 引用不变，
 * 比对通过即整行跳过——上千行时这是流式期间卡顿与否的分界。
 */
const TorrentRow = memo(function TorrentRow({
  hit,
  grouped = false,
  showRawTitles = false,
}: {
  hit: TorrentHit;
  grouped?: boolean;
  /** 列表模式直接展示站点原始种子名与副标题，不使用扩充层解析出的片名。 */
  showRawTitles?: boolean;
}) {
  const [actionsOpen, setActionsOpen] = useState(false);
  const size = hit.size ?? formatBytes(hit.size_bytes);
  const name = showRawTitles ? null : parsedName(hit);
  const complete = completeLabel(hit.attrs);
  // 分组模式且片名已解析：片名由组头承担，行标题换成版本规格摘要
  const asSpecRow = grouped && name !== null;
  const spec = asSpecRow && hit.attrs ? specSummary(hit.attrs) : null;
  return (
    // 行底不用 backdrop-filter：结果动辄上千行，每行一个独立的实时模糊合成层，
    // 滚动时 GPU 要逐层重采样背后内容，是搜索页掉帧的主因；页面蒙版本就压暗了
    // 背景，行底改用更实的半透明底色，观感几乎一致。content-visibility 让
    // 视口外的行跳过布局与绘制，长列表滚动/更新只付可见行的成本。
    <li className="group relative rounded-2xl border border-white/[0.06] bg-[rgba(16,18,25,0.82)] px-4 py-3.5 transition-all [contain-intrinsic-size:auto_72px] [content-visibility:auto] hover:-translate-y-px hover:border-white/[0.13] hover:bg-[rgba(22,25,33,0.9)] hover:shadow-[0_12px_30px_-18px_rgba(0,0,0,0.8)]">
      {/* 移动端把整行作为一个轻量操作入口，不再让详情/下载常驻盖住正文。
          按钮本身透明且只在窄屏存在，滚动手势仍交给浏览器原生处理。 */}
      {(hit.detail_url || hit.download_url) && (
        <button
          type="button"
          aria-label={`打开「${hit.title}」的资源操作`}
          onClick={() => setActionsOpen(true)}
          className="absolute inset-0 z-10 hidden cursor-pointer rounded-2xl max-md:block [@media(hover:none)]:block"
        />
      )}
      <div className="flex items-center gap-5">
        {/* 标题优先，来源和属性下沉为辅助信息，避免徽标抢走首屏注意力。 */}
        <div className="min-w-0 flex-1">
          {asSpecRow ? (
            <p
              title={rawTitleTooltip(hit)}
              className="truncate text-ui font-medium leading-5 text-[var(--text)]"
            >
              {spec ?? hit.title}
            </p>
          ) : name ? (
            // 解析出片名：标题行 = 主名 + 外文名 + 年份，原始种子名/副标题收进悬停提示
            <p
              title={rawTitleTooltip(hit)}
              className="truncate text-ui font-medium leading-5 text-[var(--text)]"
            >
              {name.primary}
              {name.secondary && (
                <span className="ml-2 text-sub font-normal text-[var(--text-muted)]">
                  {name.secondary}
                </span>
              )}
              {hit.attrs?.year != null && (
                <span className="tnum ml-2 text-sub font-normal text-[var(--text-faint)]">
                  {hit.attrs.year}
                </span>
              )}
            </p>
          ) : (
            <p className="truncate text-ui font-medium leading-5 text-[var(--text)]">
              {hit.title}
            </p>
          )}
          {((!name && hit.subtitle) || hit.site_category_name) && (
            <p className="mt-0.5 flex items-baseline gap-2 text-sub text-[var(--text-muted)]">
              {!name && hit.subtitle && (
                <span className="min-w-0 truncate">{hit.subtitle}</span>
              )}
              {hit.site_category_name && (
                <span className="shrink-0 text-caption text-[var(--text-faint)]">
                  {hit.site_category_name}
                </span>
              )}
            </p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="shrink-0 rounded-md bg-white/[0.06] px-1.5 py-0.5 text-micro font-medium text-[var(--accent-2)]">
              {hit.site_name}
            </span>
            {/* 全集徽标紧跟站点：属于资源身份而非促销，任何行模式都展示 */}
            {complete && <span className={COMPLETE_BADGE_CLS}>{complete}</span>}
            <PromoBadges hit={hit} />
            {/* 规格行模式下不再重复属性徽标（规格已在行标题里） */}
            {hit.attrs && !asSpecRow && <AttrBadges attrs={hit.attrs} />}
          </div>
        </div>

        {/* 固定指标列带短标签，数值的含义无需靠图标猜测，跨行仍可垂直比较。 */}
        <div className="tnum hidden shrink-0 grid-cols-[70px_78px_78px] gap-x-3 text-right text-caption lg:grid">
          <Metric label="大小" value={size || "—"} />
          <Metric label="做种 / 下载" value={<><span className={seederTone(hit.seeders)}>{hit.seeders}</span><span className="text-[var(--text-faint)]"> / {hit.leechers}</span></>} />
          <Metric
            label={hit.snatched > 0 ? `完成 ${hit.snatched}` : "发布时间"}
            value={hit.upload_time ? formatRelativeTime(hit.upload_time) : "—"}
            title={hit.upload_time ? formatDateTime(hit.upload_time) : undefined}
          />
        </div>

        {/* 操作区默认收起，hover 整行或键盘聚焦时再浮现：列表静止时专注内容，
            同时保留 focus-within，确保键盘用户可以访问操作。渐变遮罩覆盖下方指标列，
            避免按钮出现时文字相互叠压。手机端统一使用底部操作抽屉。 */}
        {(hit.detail_url || hit.download_url) && (
          <div className="pointer-events-none absolute inset-y-0 right-2 flex items-center gap-1.5 rounded-r-2xl bg-gradient-to-l from-[rgba(20,23,31,0.98)] from-65% to-transparent pl-16 pr-2 opacity-0 transition-opacity duration-150 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100 max-md:hidden [@media(hover:none)]:hidden">
            {hit.detail_url && (
              <a
                href={hit.detail_url}
                target="_blank"
                rel="noreferrer"
                className="btn-glass h-7 px-3 text-caption text-[var(--text-muted)]"
              >
                详情
              </a>
            )}
            <GrabButton
              hit={hit}
              className="flex h-7 items-center rounded-full border border-[#6aa7ff]/50 bg-[#6aa7ff]/20 px-3 text-caption font-medium text-[#b9d4ff] transition-colors hover:bg-[#6aa7ff]/35"
            />
            <DownloadButton
              hit={hit}
              className="btn-accent flex h-7 items-center rounded-full px-3 text-caption font-medium"
            />
          </div>
        )}
      </div>
      <TorrentActionsSheet
        hit={hit}
        open={actionsOpen}
        onClose={() => setActionsOpen(false)}
      />
    </li>
  );
});

/**
 * 移动端单条种子的操作抽屉。列表正文只承担比较信息，详情、投给订阅和下载
 * 收口到一次点按后的 bottom sheet；桌面端仍沿用行/海报 hover 操作层。
 * 抽屉复用 Modal，因此遮罩关闭、Esc 与安全区处理和全站其他移动弹窗一致。
 */
function TorrentActionsSheet({
  hit,
  open,
  onClose,
  onViewImages,
}: {
  hit: TorrentHit;
  open: boolean;
  onClose: () => void;
  /** 海报模式可从抽屉继续进入图片灯箱；列表模式不提供。 */
  onViewImages?: () => void;
}) {
  const name = parsedName(hit);
  const displayTitle = name?.primary ?? hit.title;
  const secondaryTitle = name ? hit.title : hit.subtitle;
  const size = hit.size ?? formatBytes(hit.size_bytes);

  return (
    <Modal open={open} onClose={onClose} label={`${displayTitle}的资源操作`}>
      <div className="px-4 pb-5 pt-2">
        <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-white/20" aria-hidden="true" />
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <p className="truncate text-ui font-semibold text-[var(--text)]">{displayTitle}</p>
            {secondaryTitle && (
              <p className="mt-1 line-clamp-2 text-caption leading-5 text-[var(--text-muted)]">
                {secondaryTitle}
              </p>
            )}
            <p className="tnum mt-1 text-caption text-[var(--text-faint)]">
              {[
                hit.site_name,
                size,
                `${hit.seeders} 做种`,
                hit.upload_time ? formatRelativeTime(hit.upload_time) : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭资源操作"
            onClick={onClose}
            className="touch-target flex size-8 shrink-0 items-center justify-center rounded-full bg-white/[0.06] text-[var(--text-muted)]"
          >
            <XIcon className="size-4" />
          </button>
        </div>

        <div className="mt-5 grid gap-2">
          {onViewImages && (
            <button
              type="button"
              onClick={onViewImages}
              className="btn-glass flex h-11 w-full items-center justify-center gap-2 text-ui font-medium text-[var(--text)]"
            >
              <PhotoIcon className="size-4" />
              浏览图片
            </button>
          )}
          {hit.detail_url && (
            <a
              href={hit.detail_url}
              target="_blank"
              rel="noreferrer"
              onClick={onClose}
              className="btn-glass flex h-11 w-full items-center justify-center text-ui font-medium text-[var(--text)]"
            >
              查看详情
            </a>
          )}
          <GrabButton
            hit={hit}
            className="flex h-11 w-full items-center justify-center rounded-full border border-[#6aa7ff]/50 bg-[#6aa7ff]/20 text-ui font-medium text-[#b9d4ff] transition-colors active:bg-[#6aa7ff]/35"
          />
          <DownloadButton
            hit={hit}
            className="btn-accent flex h-11 w-full items-center justify-center rounded-full text-ui font-medium"
          />
        </div>
      </div>
    </Modal>
  );
}

/** 列表指标单元：固定标签 + 数值的两级层次，让密集数字仍然可快速扫读。 */
function Metric({
  label,
  value,
  title,
}: {
  label: string;
  value: React.ReactNode;
  title?: string;
}) {
  return (
    <span title={title} className="flex min-w-0 flex-col gap-0.5">
      <span className="truncate text-micro text-[var(--text-faint)]">{label}</span>
      <span className="truncate text-[var(--text-muted)]">{value}</span>
    </span>
  );
}

/** 促销角标：免费 / 打折下载系数 / 双倍上传 / H&R 考核。 */
function PromoBadges({ hit }: { hit: TorrentHit }) {
  const badges: { text: string; cls: string }[] = [];
  if (hit.free || hit.download_volume_factor === 0) {
    badges.push({ text: "免费", cls: "bg-[var(--ok)]/15 text-[#79d193]" });
  } else if (hit.download_volume_factor < 1) {
    badges.push({
      text: `${Math.round(hit.download_volume_factor * 100)}%`,
      cls: "bg-[#6aa7ff]/15 text-[#9cc2ff]",
    });
  }
  if (hit.upload_volume_factor > 1) {
    badges.push({
      text: `${hit.upload_volume_factor}× 上传`,
      cls: "bg-[#c792ff]/15 text-[#c8a6ff]",
    });
  }
  if (hit.hit_and_run) {
    badges.push({ text: "H&R", cls: "bg-[#f59e0b]/15 text-[#fbbf24]" });
  }
  return (
    <>
      {badges.map((b) => (
        <span
          key={b.text}
          className={`rounded-md px-1.5 py-0.5 text-micro font-semibold ${b.cls}`}
        >
          {b.text}
        </span>
      ))}
    </>
  );
}

/** 每行常显的属性徽标上限；超出部分折叠成 +N（悬停看全部）。 */
const MAX_ATTR_BADGES = 4;

/**
 * 扩充属性徽标（降噪版）：按信息量优先级取前 4 个常显，其余折叠成 +N。
 *
 * 等权重小徽标超过 4-5 个后可读性反而下降（chip fatigue），且分辨率/片源/编码
 * 这些维度在顶部筛选栏已可见可筛。优先级：季集（剧集的关键标识）> 分辨率 >
 * Remux > HDR > 字幕 > 压制组（PT 用户在意）> 片源 > 编码 > 音频。
 * 影视类型不再入列——顶部的类型分段切换已表达同一信息，每行重复是纯冗余。
 * 标题串（release name）本身也完整携带这些属性，+N 悬停即见全部。
 */
/** 题材轴的中文标签（attrs.content_type → 徽标文案）。 */
const CONTENT_TYPE_LABEL: Record<string, string> = {
  anime: "动漫",
  documentary: "纪录片",
  variety: "综艺",
  music: "音乐",
};

function AttrBadges({ attrs }: { attrs: TorrentAttrs }) {
  const chips: { text: string; cls?: string }[] = [];
  const seasonEp = seasonEpLabel(attrs);
  if (seasonEp) chips.push({ text: seasonEp, cls: "text-[var(--accent-2)]" });
  if (attrs.content_type && CONTENT_TYPE_LABEL[attrs.content_type]) {
    chips.push({ text: CONTENT_TYPE_LABEL[attrs.content_type], cls: "text-[#f0b6d8]" });
  }
  if (attrs.resolution) chips.push({ text: attrs.resolution });
  if (attrs.remux) chips.push({ text: "Remux", cls: "text-[#9cc2ff]" });
  for (const v of attrs.hdr) chips.push({ text: v, cls: "text-[#c8a6ff]" });
  const subBadge = compactSubtitleBadge(attrs);
  if (subBadge) chips.push({ text: subBadge, cls: "text-[#7ee2b8]" });
  const audioBadge = compactAudioBadge(attrs);
  if (audioBadge) chips.push({ text: audioBadge, cls: "text-[#ffd08a]" });
  if (attrs.release_group) chips.push({ text: attrs.release_group, cls: "text-[var(--accent)]" });
  for (const v of attrs.platforms ?? []) {
    chips.push({ text: platformLabel(v), cls: "text-[#8fd8ff]" });
  }
  if (attrs.media_source) chips.push({ text: attrs.media_source });
  if (attrs.video_codec) chips.push({ text: attrs.video_codec });
  for (const v of attrs.audio.slice(0, 2)) chips.push({ text: v });
  if (!chips.length) return null;

  const shown = chips.slice(0, MAX_ATTR_BADGES);
  const folded = chips.slice(MAX_ATTR_BADGES);
  return (
    <>
      {shown.map((c) => (
        <span
          key={c.text}
          className={`rounded-md bg-white/[0.05] px-1.5 py-0.5 text-micro ${c.cls ?? "text-[var(--text-muted)]"}`}
        >
          {c.text}
        </span>
      ))}
      {folded.length > 0 && (
        <span
          title={folded.map((c) => c.text).join(" · ")}
          className="tnum cursor-default rounded-md bg-white/[0.05] px-1.5 py-0.5 text-micro text-[var(--text-faint)]"
        >
          +{folded.length}
        </span>
      )}
    </>
  );
}

/**
 * 季集摘要："S02E03"、"S01-S05"、"E19-E20"。只描述观测值：seasons/episodes
 * 为空时不硬造；全集包的逐集列表是噪音，complete 时不列集（"全集"信息
 * 由独立的全集徽标承担，见 completeLabel）。
 */
function seasonEpLabel(attrs: TorrentAttrs): string | null {
  const pad = (n: number) => String(n).padStart(2, "0");
  const range = (nums: number[], prefix: string) =>
    nums.length === 1
      ? `${prefix}${pad(nums[0])}`
      : `${prefix}${pad(nums[0])}-${prefix}${pad(nums[nums.length - 1])}`;

  const parts: string[] = [];
  if (attrs.seasons.length === 1 && attrs.episodes.length) {
    parts.push(`S${pad(attrs.seasons[0])}${range(attrs.episodes, "E")}`);
  } else {
    if (attrs.seasons.length) parts.push(range(attrs.seasons, "S"));
    if (attrs.episodes.length && !attrs.complete) parts.push(range(attrs.episodes, "E"));
  }
  return parts.length ? parts.join(" ") : null;
}

/**
 * 全集徽标文案：明确标注全集（"全N集"/COMPLETE）的资源返回"全N集"/"全集"，
 * 其余返回 null。所有展示形态（分组行/列表行/海报卡/组头）共用此判定，
 * 徽标样式也统一走 COMPLETE_BADGE_CLS——全集包是"一把带走"的下载决策关键，
 * 用区别于普通属性 chip 的渐变描边样式让它跳出来。
 */
function completeLabel(attrs: TorrentAttrs | null): string | null {
  if (attrs?.complete !== true) return null;
  return attrs.episodes_total ? `全${attrs.episodes_total}集` : "全集";
}

/** 全集徽标的统一样式（列表/分组/组头）；海报卡用实底变体叠图可读。 */
const COMPLETE_BADGE_CLS =
  "rounded-md bg-gradient-to-r from-[#4f8cff]/25 to-[#9d6bff]/25 px-1.5 py-0.5 text-micro font-semibold text-[#b9d4ff] ring-1 ring-inset ring-[#6aa7ff]/35";

/* —— 占位 / 空态 —— */

/**
 * 页头底部的搜索进度条：start 事件未到（不知道站点数）时是来回扫光的
 * 不定进度，之后按「已完成站点 / 总站点」比例填充；完成后随 header 收起。
 */
function SearchProgressBar({ settled, total }: { settled: number; total: number }) {
  return (
    <div className="relative mt-3 h-[2px] overflow-hidden rounded-full bg-white/[0.07]">
      {total === 0 ? (
        <span className="progress-sweep absolute inset-y-0 w-2/5 rounded-full bg-gradient-to-r from-transparent via-[var(--accent)] to-transparent" />
      ) : (
        <span
          className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-[var(--accent)] to-[var(--accent-2)] transition-[width] duration-500 ease-out"
          // 起步给 6% 的可见进度：0 个站点完成时进度条也不该是空的，用户要能看到"已经开始了"
          style={{ width: `${Math.max(6, (settled / total) * 100)}%` }}
        />
      )}
    </div>
  );
}

/** 骨架标题条的宽度序列：错落模拟真实标题长短不一，行数也由它决定。 */
const SKELETON_TITLE_WIDTHS = ["72%", "58%", "80%", "64%", "70%", "52%"];

/**
 * 首批结果到达前的等待态：状态行（旋转指示 + 正在搜索几个站点）+ 模拟结果行
 * 结构的 shimmer 骨架（徽标排 / 标题条 / 元信息条），逐行错峰扫光、透明度递减。
 * 结果是流式渐进的，通常最快的站点亚秒级就会替换掉这里。
 */
function SkeletonList({ siteCount }: { siteCount: number }) {
  return (
    <div>
      <div className="text-on-image flex items-center gap-2 px-1 pb-3 text-sub text-[rgba(243,245,249,0.8)]">
        <svg
          viewBox="0 0 24 24"
          className="size-3.5 animate-spin text-[var(--accent)]"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.5}
          strokeLinecap="round"
          aria-hidden="true"
        >
          <path d="M12 3a9 9 0 1 1-9 9" />
        </svg>
        {siteCount > 0
          ? `正在搜索 ${siteCount} 个站点，结果将实时呈现…`
          : "正在连接搜索服务…"}
      </div>
      <ul className="space-y-2" aria-hidden="true">
        {SKELETON_TITLE_WIDTHS.map((width, i) => (
          <li
            key={i}
            className="rounded-2xl border border-white/[0.05] bg-[rgba(14,16,22,0.35)] px-4 py-3 backdrop-blur-xl"
            style={{ opacity: 1 - i * 0.13, "--stagger": `${i * 120}ms` } as React.CSSProperties}
          >
            {/* 徽标排：站点名 + 属性小徽标 */}
            <div className="flex items-center gap-1.5">
              <span className="skeleton-block h-[17px] w-14 rounded-md" />
              <span className="skeleton-block h-[17px] w-10 rounded-md" />
              <span className="skeleton-block h-[17px] w-12 rounded-md" />
            </div>
            {/* 标题条 */}
            <div className="skeleton-block mt-2.5 h-[13px] rounded-md" style={{ width }} />
            {/* 元信息条：体积 / 做种 / 下载 / 日期 */}
            <div className="mt-2.5 flex items-center gap-3">
              <span className="skeleton-block h-[10px] w-14 rounded" />
              <span className="skeleton-block h-[10px] w-9 rounded" />
              <span className="skeleton-block h-[10px] w-9 rounded" />
              <span className="skeleton-block h-[10px] w-16 rounded" />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function EmptyHint({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex min-h-[240px] flex-col items-center justify-center text-center">
      <p className="text-on-image text-body-lg font-medium text-white">{title}</p>
      <p className="text-on-image mt-1.5 max-w-sm text-ui leading-6 text-[rgba(243,245,249,0.82)]">
        {hint}
      </p>
    </div>
  );
}

/* —— 工具函数 —— */
function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}
