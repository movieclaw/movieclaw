"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";
import { createPortal } from "react-dom";

import { DirectoryPicker } from "@/components/directory-picker";
import { ContentEmptyState } from "@/components/content-empty-state";
import { useConfirm } from "@/components/feedback";
import { HScroller } from "@/components/h-scroller";
import { FilmIcon, FolderIcon, MoreIcon, PlusIcon, TvIcon, XIcon } from "@/components/icons";
import { LibraryOrganizeDialog } from "@/components/library-organize-dialog";
import { LibraryScrapeSettings } from "@/components/library-scrape-settings";
import { Modal } from "@/components/modal";
import { MediaRow } from "@/components/media-row";
import type { PosterCardAction } from "@/components/poster-card";
import { RecentWatchRow } from "@/components/recent-watch-row";
import { Tooltip } from "@/components/tooltip";
import {
  type LibraryItem,
  type LibraryPayload,
  type MatchRule,
  type MediaLibrary,
  type RoutingOptions,
  createLibrary,
  deleteLibrary,
  listLibraryRoutingOptions,
  listLibraries,
  listLibraryItems,
  reorderLibraries,
  SCAN_PHASE_LABELS,
  setDefaultLibrary,
  startLibraryMetadataRefresh,
  startLibraryScan,
  stopLibraryMetadataRefresh,
  stopLibraryScan,
  updateLibrary,
} from "@/lib/api/libraries";
import { listRecentWatch, type RecentWatchItem } from "@/lib/api/playback";
import { refreshLibraryConfirm, scanLibraryConfirm } from "@/lib/library-confirm";
import type { Subscription } from "@/lib/api/subscriptions";
import { publicEnv } from "@/lib/env";
import { formatBytes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import { libraryInventoryAction } from "@/lib/library-inventory-summary";
import type { MediaItem, MediaType } from "@/lib/media-types";
import { usePermissions } from "@/lib/permissions";
import { buildRecentAdditionOverlay } from "@/lib/recent-addition";
import { formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/** 库类型 → 展示名与图标 */
export const LIBRARY_KIND_META: Record<MediaType, { label: string; Icon: typeof FilmIcon }> = {
  movie: { label: "电影", Icon: FilmIcon },
  tv: { label: "剧集", Icon: TvIcon },
};

/** 每个库「最近添加」行的格数（也是本页向服务端要的条目数上限）。 */
const RECENT_COUNT = 20;

/** 收藏范围可选项的模块级缓存：这是后端静态常量，但 useRoutingOptions 被
 *  每张库卡片和表单弹窗各自调用——不缓存的话库首页一次挂载就打出 N 个
 *  相同请求。失败时清空缓存，下一个调用方重试。 */
let routingOptionsPromise: Promise<RoutingOptions> | null = null;

/** 收藏范围可选项（后端静态常量）；加载失败降级为 null（相关 UI 不渲染）。 */
function useRoutingOptions(): RoutingOptions | null {
  const [options, setOptions] = useState<RoutingOptions | null>(null);
  useEffect(() => {
    let cancelled = false;
    routingOptionsPromise ??= listLibraryRoutingOptions();
    void routingOptionsPromise
      .then((o) => {
        if (!cancelled) setOptions(o);
      })
      .catch(() => {
        /* 静默降级：收藏范围区显示加载失败提示 */
        routingOptionsPromise = null;
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return options;
}

/** 收藏范围条件 → 表单状态（v1 两个维度：类型 ID / 区域国家码）。 */
function parseMatchRules(rules: MatchRule[]): { genres: number[]; regions: string[] } {
  const genres = rules.find((r) => r.field === "genres")?.values ?? [];
  const regions = rules.find((r) => r.field === "origin_countries")?.values ?? [];
  return {
    genres: genres.filter((v): v is number => typeof v === "number"),
    regions: regions.filter((v): v is string => typeof v === "string"),
  };
}

/** 表单状态 → 收藏范围条件（空维度不生成条件；两者都空 = 不声明）。 */
function buildMatchRules(genres: number[], regions: string[]): MatchRule[] {
  const rules: MatchRule[] = [];
  if (genres.length > 0) rules.push({ field: "genres", op: "any_of", values: genres });
  if (regions.length > 0)
    rules.push({ field: "origin_countries", op: "any_of", values: regions });
  return rules;
}

/** 把区域国家码折叠成展示名：整组命中的折叠成预设组名（如「日韩」），
 *  折不进组的逐个显示中文名。用于表单弹窗底栏的当前声明摘要。 */
function regionLabels(regions: string[], options: RoutingOptions): string[] {
  const parts: string[] = [];
  let rest = [...regions];
  for (const preset of options.region_presets) {
    if (preset.countries.every((c) => rest.includes(c))) {
      parts.push(preset.label);
      rest = rest.filter((c) => !preset.countries.includes(c));
    }
  }
  parts.push(...rest.map((c) => options.country_names[c] ?? c));
  return parts;
}

/** 规则字段 → 界面维度名（v1 仅两个维度），用于重叠提示里点名该补哪个条件。 */
const RULE_FIELD_LABELS: Record<string, string> = {
  genres: "类型",
  origin_countries: "区域",
};

/**
 * 同类型两库的收藏范围可能同时命中同一部作品、且特异性（条件条数）相同
 * ——命中顺序只能靠创建先后，给只读提示（不阻断：加一个条件即可消解）。
 * 「可能同时命中」= 两库共同声明的每个字段取值都有交集。
 * 提示不止陈述事实，还给出具体做法：点名给创建更晚（即将吃亏）的那个库
 * 补上它缺的维度——条件数多者优先命中，补完歧义即消。
 */
function routingOverlapWarnings(libraries: MediaLibrary[]): string[] {
  const declared = libraries.filter((l) => l.match_rules.length > 0);
  const warnings: string[] = [];
  for (let i = 0; i < declared.length; i++) {
    for (let j = i + 1; j < declared.length; j++) {
      const a = declared[i];
      const b = declared[j];
      if (a.kind !== b.kind || a.match_rules.length !== b.match_rules.length) continue;
      const compatible = a.match_rules.every((ra) => {
        const rb = b.match_rules.find((r) => r.field === ra.field);
        if (!rb) return true; // 字段只在一边：不妨碍同时命中
        return ra.values.some((v) => rb.values.includes(v));
      });
      if (compatible) {
        const [first, later] = (a.id ?? 0) < (b.id ?? 0) ? [a, b] : [b, a];
        const laterFields = new Set<string>(later.match_rules.map((r) => r.field));
        const missing = Object.entries(RULE_FIELD_LABELS)
          .filter(([field]) => !laterFields.has(field))
          .map(([, label]) => label);
        const fix =
          missing.length > 0
            ? `想让「${later.name}」优先收这类作品：编辑它，补上「${missing.join("」或「")}」条件` +
              `（用「全选」也可以）——条件多的库优先命中；想维持现状则无需改动。`
            : `两库已声明相同的维度：错开重叠的取值即可消除歧义。`;
        warnings.push(
          `「${a.name}」与「${b.name}」的收藏范围可能同时命中同一部作品且条件数相同，` +
            `届时优先进创建更早的「${first.name}」。${fix}`,
        );
      }
    }
  }
  return warnings;
}

/**
 * 库存条目的悬浮操作与本卡 hover 的完整度文案同源：季或集有一项未齐就
 * 「补齐缺集」；当前已知季集全部在库则「自动续订」，等待未来出现的新季。
 * 已在“我的订阅”中的条目由 PosterCardVisual 统一隐藏操作，不再显示
 * 没有决策价值的“已订阅”按钮。
 */
export function libraryCardAction(item: LibraryItem): PosterCardAction {
  return libraryInventoryAction(item.kind, item.inventory_summary);
}

/**
 * 订阅的实际归属库：显式指定优先，否则该类型的默认库。
 * 与后端 resolve_for_subscription 同一语义，库页与单库页共用。
 */
export function effectiveLibraryId(
  sub: Subscription,
  libraries: MediaLibrary[],
): number | null {
  if (sub.library_id != null) return sub.library_id;
  return libraries.find((l) => l.kind === sub.media.kind && l.is_default)?.id ?? null;
}

/** 媒体库首页摘要：只聚合接口随库返回的预计算快照，不触发额外请求。 */
export function libraryStatsSummary(libraries: MediaLibrary[] | null): string {
  if (libraries === null) return "正在汇总媒体库统计…";
  if (libraries.length === 0) return "还没有媒体库，创建后会在这里显示库存统计";
  const movieCount = libraries
    .filter((library) => library.kind === "movie")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const tvCount = libraries
    .filter((library) => library.kind === "tv")
    .reduce((total, library) => total + library.stats.item_count, 0);
  const totalSizeBytes = libraries.reduce(
    (total, library) => total + library.stats.total_size_bytes,
    0,
  );
  return `${libraries.length} 个媒体库 · ${movieCount} 部电影 · ${tvCount} 部剧集 · 共占用 ${formatBytes(totalSizeBytes)} 存储空间`;
}

/**
 * 媒体库页（/library）：全部库的 Emby 风格卡片横排。
 *
 * 每张卡是一个库：封面用库内作品的海报做「货架」展示（最多 4 张站立海报
 * 带底部倒影，纯前端 CSS 合成、零后端开销），叠库名/类型/统计；
 * 点击进入单库海报墙（/library/[id]）。库的增删改/设默认/扫描都在本页
 * 完成——媒体库是内容的一等入口，不是配置项。
 *
 * 数据源是 library_file 台账的**真实库存**（L3 起）：入库管线与存量扫描
 * 落账的文件聚合，不再用订阅占位。
 */
export function LibraryView() {
  const { canManageLibraries } = usePermissions();
  const scrollRef = useScrollRestoration("library");
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [itemsByLibrary, setItemsByLibrary] = useState<Map<number, LibraryItem[]>>(
    new Map(),
  );
  const [recentWatch, setRecentWatch] = useState<RecentWatchItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 弹窗态：新增（"new"）/ 编辑（库对象）/ 关闭(null)
  const [editing, setEditing] = useState<MediaLibrary | "new" | null>(null);
  // 整理文件名对话框的目标库；null = 关闭
  const [organizeTarget, setOrganizeTarget] = useState<MediaLibrary | null>(null);
  // 刚创建的库：卡片区是横滚，新库排在末位，库一多就落在可视区外——
  // 用户会以为"没建上"。这里记下 id，列表刷新后把它滚进视野并短暂高亮
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const routingWarnings = useMemo(() => routingOverlapWarnings(libraries ?? []), [libraries]);

  // 轮询乱序守卫：扫描期间后端响应时间抖动大，上一轮的慢响应可能晚于
  // 下一轮到达，不作废就会用旧快照覆盖新状态（进度回跳、卡片状态闪烁）
  const reloadSeq = useRef(0);
  // 上一轮已拉过条目时的库列表快照：库状态（扫描/整理/入账数/刷新进度）
  // 没有任何变化，说明库存也不会变，封面拼图不必再逐库全量拉一遍条目
  // ——否则空闲时每 30 秒也要打出 1 + N 个请求
  const lastLibsSnapshot = useRef<string | null>(null);
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    Promise.all([
      listLibraries(),
      // 最近观看失败不拖垮媒体库首页；保留旧数据，下一轮轮询自动重试。
      listRecentWatch(RECENT_COUNT).catch(() => null),
    ])
      .then(async ([libs, latestWatch]) => {
        if (seq !== reloadSeq.current) return;
        setFailed(false);
        if (latestWatch !== null) setRecentWatch(latestWatch);
        else setRecentWatch((previous) => previous ?? []);
        const snapshot = JSON.stringify(libs);
        // 内容没变就复用旧引用，跳过整页卡片的无谓重渲染
        setLibraries((prev) => (prev && JSON.stringify(prev) === snapshot ? prev : libs));
        if (snapshot === lastLibsSnapshot.current) return;
        // 本页每个库只用到最近入账的 20 部（「最近添加」行 + 卡片封面拼图取前 4），
        // 排序与截断都交给服务端——早先在这里拉整库再本地切片，一个几千部的库
        // 光这一个请求就是几百 KB、后端还要把全库台账聚合一遍
        const entries = await Promise.all(
          libs.map(
            async (lib) =>
              [
                lib.id,
                await listLibraryItems(lib.id, { sort: "added_at", limit: RECENT_COUNT }).catch(
                  () => [],
                ),
              ] as const,
          ),
        );
        if (seq !== reloadSeq.current) return;
        lastLibsSnapshot.current = snapshot;
        setItemsByLibrary(new Map(entries));
      })
      // 瞬时失败不清已有数据：failed 只决定提示条，卡片继续用上一份快照，
      // 下一轮轮询成功即自动恢复（整页错误屏只留给一次都没加载成功的情况）
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // 有库在扫描/整理时轮询刷新，任务完成即看到最新库存与文件名
  const busyAny = (libraries ?? []).some((l) => l.scanning || l.organizing);
  // 元数据刷新单独一档：它以分钟计，而本页每轮 reload 还要把每个库的条目
  // 列表拉一遍，用扫描那档 3 秒会打出上百次无谓请求；5 秒足够让进度环和
  // "到哪部了"看着在动（完整阶段列表在单库页的面板里）
  const refreshingAny = (libraries ?? []).some((l) => l.metadata_refresh?.refreshing);
  // 有文件写入中暂缓入账（拷贝/下载进行时 watchdog 已发现，等补扫落定）：
  // 中速轮询让「入库中」徽标与随后的库存变化自动呈现；完全空闲时低频兜底
  // ——后台自发的扫描（实时监控/定时对账）页面开着不动也能感知到
  const importingAny = (libraries ?? []).some(
    (l) => !l.scanning && !l.organizing && (l.last_scan?.deferred ?? 0) > 0,
  );
  // busy 刚结束后保持快轮询一小段再降速：监控去抖触发的连环扫描之间隔着
  // 几秒空档，一采样到空档就降去 30 秒档的话，下一轮扫描的开始要很久
  // 才被发现，卡片状态看起来就是"时隐时现"
  const [recentlyBusy, setRecentlyBusy] = useState(false);
  useEffect(() => {
    if (busyAny) {
      setRecentlyBusy(true);
      return;
    }
    if (!recentlyBusy) return;
    const timer = setTimeout(() => setRecentlyBusy(false), 12_000);
    return () => clearTimeout(timer);
  }, [busyAny, recentlyBusy]);
  // 页面隐藏时暂停轮询、恢复可见立即补一次（useVisiblePolling）
  useVisiblePolling(
    reload,
    busyAny || recentlyBusy ? 3000 : refreshingAny ? 5000 : importingAny ? 10_000 : 30_000,
  );

  // 调整展示顺序：与相邻库换位后整单提交（后端要求一次给全所有 id）。
  // 先乐观换位（卡片区与下方「最近添加」分区同吃 libraries 的顺序，立即
  // 一起换位），失败回滚并提示
  const moveLibrary = (libraryId: number, offset: -1 | 1) => {
    if (!libraries) return;
    const index = libraries.findIndex((l) => l.id === libraryId);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= libraries.length) return;
    const next = [...libraries];
    [next[index], next[target]] = [next[target], next[index]];
    const prev = libraries;
    setLibraries(next);
    void reorderLibraries(next.map((l) => l.id))
      .then(reload)
      .catch((e) => {
        setLibraries(prev);
        setError((e as Error).message);
      });
  };

  // 新建的库进入列表后滚进视野（依赖 libraries：创建到列表刷新之间隔着
  // 一次请求，元素这时才存在），高亮 2.5 秒后自行褪去
  useEffect(() => {
    if (highlightId === null) return;
    const el = document.querySelector(`[data-library-card="${highlightId}"]`);
    el?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    const timer = setTimeout(() => setHighlightId(null), 2500);
    return () => clearTimeout(timer);
  }, [highlightId, libraries]);

  // 每个非空库一行「最近添加」：服务端已按最近入账倒序给到前 20，复用发现页的
  // 横滚海报行。这里只呈现入库上下文，订阅/补齐操作留在单库页，避免 hover
  // 被“已订阅”等与最近添加无关的状态占据。
  const recentRows = useMemo(
    () =>
      (libraries ?? [])
        .map((library) => {
          const recent = itemsByLibrary.get(library.id) ?? [];
          // 已在库的条目点击进**媒体库条目详情**（本地刮削信息 + 片源规格 +
          // 条目操作），与单库页库存格同一目标，不再跳发现页的 TMDB 详情
          const hrefs = new Map(
            recent.map((it) => [
              String(it.tmdb_id),
              `/library/${library.id}/item/${it.media_item_id}` as Route,
            ]),
          );
          return {
            library,
            items: recent.map(libraryItemToMediaItem),
            hrefOf: (m: MediaItem) => hrefs.get(m.id),
          };
        })
        .filter((row) => row.items.length > 0),
    [libraries, itemsByLibrary],
  );

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <div className="px-6 pt-7 max-md:px-4 max-md:pt-4">
        <div>
          <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
            媒体库
          </h2>
          <p className="text-on-image mt-1.5 text-ui text-[var(--text-muted)] max-md:mt-1 max-md:line-clamp-2 max-md:text-sub">
            {failed && libraries === null
              ? "暂时无法获取媒体库统计，正在自动重试"
              : libraryStatsSummary(libraries)}
          </p>
        </div>
      </div>

      {error && (
        <div className="mx-6 mt-4 rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b] max-md:mx-4">
          {error}
        </div>
      )}

      {/* 收藏范围重叠提示（只读不阻断）：同类型两库范围重叠且特异性相同时，
          命中顺序只能靠创建先后——亮出来让用户自己拍板要不要加条件消解 */}
      {canManageLibraries && routingWarnings.map((w) => (
        <div
          key={w}
          className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-relaxed text-amber-200 max-md:mx-4"
        >
          {w}
        </div>
      ))}

      {libraries === null && !failed && (
        <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载媒体库…
        </div>
      )}

      {/* 只有一次都没加载成功过才整页报错；已有数据在手时，瞬时失败只挂
          提示条（stale-while-error），卡片照常展示上一份快照 */}
      {failed && libraries === null && (
        <div className="mt-16 flex flex-col items-center gap-3 text-center">
          <p className="text-ui text-[var(--text-muted)]">媒体库加载失败</p>
          <button
            type="button"
            onClick={reload}
            className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            重试
          </button>
        </div>
      )}

      {failed && libraries !== null && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200 max-md:mx-4">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}

      {/* 当前账号跨可见库聚合的播放状态；空列表时组件整段隐藏。 */}
      {(!failed || libraries !== null) && <RecentWatchRow items={recentWatch} />}

      {libraries !== null && libraries.length === 0 && (
        <ContentEmptyState
          variant="library"
          title={canManageLibraries ? "为收藏准备一个家" : "还没有可浏览的媒体库"}
          description={
            canManageLibraries
              ? "创建电影库或剧集库，选好根目录后，订阅完成的内容会自动整理到这里。"
              : "当前账号暂时没有可浏览的媒体库，请联系管理员分配媒体库权限。"
          }
          action={
            canManageLibraries ? (
              <button
                type="button"
                onClick={() => setEditing("new")}
                className="btn-accent flex items-center gap-1 rounded-full py-2 pl-3 pr-4 text-ui font-semibold"
              >
                <PlusIcon className="size-4" />
                创建第一个媒体库
              </button>
            ) : undefined
          }
        />
      )}

      {/* 库卡片横排：库多了不换行堆高，改为一行横滚（与下方「最近添加」
          同一交互），首屏始终保住「最近观看 → 我的媒体库 → 最近添加」的层次。 */}
      {libraries !== null && libraries.length > 0 && (
        <section className="mt-8 max-md:mt-6" aria-labelledby="my-libraries-title">
          <div className="flex items-center justify-between gap-4 px-6 max-md:px-4">
            <h3
              id="my-libraries-title"
              className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]"
            >
              我的媒体库
            </h3>
            {canManageLibraries && (
              <button
                type="button"
                onClick={() => setEditing("new")}
                className="btn-glass h-7 shrink-0 gap-1 px-2.5 text-caption font-medium"
              >
                <PlusIcon className="size-3.5" />
                添加媒体库
              </button>
            )}
          </div>
          <HScroller className="mt-3 gap-5 px-6 pb-1 pt-1 max-md:gap-3.5 max-md:px-4">
            {libraries.map((library, index) => (
              <div
                key={library.id}
                data-library-card={library.id}
                className={`w-[268px] shrink-0 rounded-2xl transition max-md:w-[230px] ${
                  highlightId === library.id ? "ring-2 ring-[var(--accent-2)] ring-offset-4 ring-offset-transparent" : ""
                }`}
              >
                <LibraryCard
                  library={library}
                  items={itemsByLibrary.get(library.id) ?? []}
                  canManage={canManageLibraries}
                  onEdit={() => setEditing(library)}
                  onOrganize={() => setOrganizeTarget(library)}
                  onRefresh={reload}
                  onError={setError}
                  canMoveLeft={index > 0}
                  canMoveRight={index < libraries.length - 1}
                  onMove={(offset) => moveLibrary(library.id, offset)}
                />
              </div>
            ))}
          </HScroller>
        </section>
      )}

      {/* —— 最近添加：Emby 首页式分区，每个非空库一行横滚海报 —— */}
      {recentRows.length > 0 && (
        <div className="mt-10 space-y-8">
          {recentRows.map(({ library, items, hrefOf }) => (
            <MediaRow
              key={library.id}
              row={{
                id: `library-recent-${library.id}`,
                title: `最近添加的${library.name}`,
                items,
              }}
              moreHref={`/library/${library.id}` as Route}
              moreLabel="查看全部"
              cardAction="none"
              cardHref={hrefOf}
              cardRevealInfoOnTouch
            />
          ))}
        </div>
      )}

      {canManageLibraries && (
        <>
          <LibraryFormDialog
            state={editing}
            onClose={() => setEditing(null)}
            onSaved={(saved) => {
              const isNew = editing === "new";
              setEditing(null);
              // 新库排在末位（按创建顺序），卡片区又是横滚——库多了新卡片直接
              // 落在可视区外，用户以为"没建上"。滚进视野并短暂高亮
              if (isNew) setHighlightId(saved.id);
              reload();
            }}
          />
          <LibraryOrganizeDialog
            library={organizeTarget}
            onClose={() => setOrganizeTarget(null)}
            onChanged={reload}
          />
        </>
      )}
    </div>
  );
}

/**
 * 库存条目 → 发现页海报卡的数据形态。点击走 /media/{type}/{tmdb_id} 详情
 * （与单库页库存格同一目标）。卡片底部只留片名与年份；本批季集范围和入库
 * 时间进入 hover，不能拿累计库存季集数冒充新增内容。海报不打清晰度徽章。
 */
function libraryItemToMediaItem(item: LibraryItem): MediaItem {
  const overlayDetails = buildRecentAdditionOverlay(
    item.kind,
    item.recent_addition,
    item.added_at ? `${formatRelativeTime(item.added_at)}入库` : null,
  );
  return {
    id: String(item.tmdb_id),
    source: "tmdb",
    type: item.kind,
    title: item.title,
    originalTitle: "",
    year: item.year ?? 0,
    rating: 0,
    genres: [],
    extent: "",
    badges: [],
    overview: "",
    overlayDetails,
    // 海报可能是本地刮削资产的相对路径（/images/assets/...），也可能是
    // TMDB 图床绝对地址——统一经 imageUrl 解析（补 API base / 走缓存代理）。
    // 取 poster-card 派生图而非原图：格子实测渲染 150~170 CSS px，328px 的
    // 预设覆盖 2x 屏绰绰有余，而原图是 500px 宽的刮削资产——一屏 60 格直出
    // 原图要 4.9 MB，取派生图只要 1.7 MB（实测单张 82KB → 29KB）
    posterUrl: imageUrl(item.poster_url, "poster-card"),
  };
}

/* —— 库卡片：海报货架封面 + 库名/徽标/计数，Emby「我的媒体」磁贴风 —— */

function LibraryCard({
  library,
  items,
  canManage,
  onEdit,
  onOrganize,
  onRefresh,
  onError,
  canMoveLeft,
  canMoveRight,
  onMove,
}: {
  library: MediaLibrary;
  items: LibraryItem[];
  canManage: boolean;
  onEdit: () => void;
  onOrganize: () => void;
  onRefresh: () => void;
  onError: (message: string) => void;
  canMoveLeft: boolean;
  canMoveRight: boolean;
  onMove: (offset: -1 | 1) => void;
}) {
  const meta = LIBRARY_KIND_META[library.kind];
  // 封面海报取最近入库的 4 部（items 已是服务端按最近入账排好的那批）
  const posters = items
    .map((s) => s.poster_url)
    .filter((u): u is string => Boolean(u))
    .slice(0, 4);
  const { stats } = library;
  // 扫描/整理/元数据刷新进行中：封面归进度环，其余状态徽标一律让位。
  // 三种长任务在卡片上同一套呈现——用户不该因为"哪种任务"而看不到进度
  const refreshingMeta = Boolean(library.metadata_refresh?.refreshing);
  const busy = library.scanning || library.organizing || refreshingMeta;
  // 写入中暂缓入账的文件数（watchdog 已发现、等拷贝/下载落定后自动补扫入库）
  const importing = busy ? 0 : (library.last_scan?.deferred ?? 0);

  return (
    <div className="group/lib relative">
      <Link
        href={`/library/${library.id}` as Route}
        scroll={false}
        aria-label={`打开「${library.name}」`}
        className="block overflow-hidden rounded-2xl ring-1 ring-white/10 outline-none transition duration-300 hover:ring-white/35 focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
      >
        <div className="relative aspect-[21/10] bg-[#0a0c12]">
          <LibraryCover libraryId={library.id} posters={posters} Icon={meta.Icon} />
          {/* 状态徽标叠在封面左下的倒影暗区：那块本就没有信息、又足够暗
              压得住字；标题行因此永远只有库名，长库名不会被徽标挤没。
              扫描/整理时封面归进度环，徽标让位（否则隔着蒙版透出来像脏渲染） */}
          {!busy && (importing > 0 || stats.unidentified_count > 0) && (
            <div className="absolute inset-x-2.5 bottom-2 flex flex-wrap items-center gap-1.5">
              {importing > 0 && (
                <span className="flex items-center gap-1.5 rounded-full border border-[var(--info)]/35 bg-black/55 px-2 py-0.5 text-micro font-semibold text-[var(--info)] backdrop-blur-md">
                  <span className="size-1.5 animate-pulse rounded-full bg-[var(--info)]" />
                  {importing} 个新文件入库中
                </span>
              )}
              {stats.unidentified_count > 0 && (
                <span className="rounded-full border border-[var(--warn)]/35 bg-black/55 px-2 py-0.5 text-micro font-semibold text-[var(--warn)] backdrop-blur-md">
                  {stats.unidentified_count} 个待识别
                </span>
              )}
            </div>
          )}
          {busy && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-1 bg-black/55 backdrop-blur-[2px]">
              <ScanProgressRing
                progress={
                  library.scanning
                    ? library.scan_progress
                    : library.organizing
                      ? library.organize_progress
                      : library.metadata_refresh
                }
              />
              {/* 进度环只有百分比，说不清在干什么，补一行状态词。扫描内部
                  分阶段（盘点/入账/补图），阶段变了这里必须跟着变——否则
                  文件扫完后还要下几分钟图片，环停在 100% 配一句"扫描中"，
                  看起来就是卡死了 */}
              <span className="text-caption font-semibold text-white/85">
                {library.scanning
                  ? (SCAN_PHASE_LABELS[library.scan_progress?.phase ?? "ingesting"] ?? "扫描中")
                  : library.organizing
                    ? "整理中"
                    : "刷新元数据"}
              </span>
              {/* 刷新是全量重刷、以分钟计，多给一行"到哪部了"（并发若干路
                  时取第一部即可，完整列表在单库页的面板里） */}
              {refreshingMeta && library.metadata_refresh?.active?.[0] && (
                <span className="max-w-[86%] truncate text-micro text-white/60">
                  {library.metadata_refresh.active[0].title} ·{" "}
                  {library.metadata_refresh.active[0].phase}
                </span>
              )}
            </div>
          )}
        </div>
      </Link>

      {/* 库名：Emby 式放在封面下方居中，只与「默认」共处一行 */}
      <div className="mt-2.5 flex items-center justify-center gap-2 px-2">
        <h3 className="truncate text-body-lg font-semibold text-white">{library.name}</h3>
        {library.is_default && (
          <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.1] px-2 py-0.5 text-micro font-semibold text-white/80">
            默认
          </span>
        )}
      </div>
      {/* 管理操作：悬停浮现在右上角（Link 外层，避免点菜单触发跳转） */}
      {canManage && (
        <LibraryCardMenu
          library={library}
          onEdit={onEdit}
          onScan={() => {
            void startLibraryScan(library.id)
              .then(onRefresh)
              .catch((e) => onError((e as Error).message));
          }}
          onOrganize={onOrganize}
          onRefresh={onRefresh}
          onError={onError}
          canMoveLeft={canMoveLeft}
          canMoveRight={canMoveRight}
          onMove={onMove}
        />
      )}
    </div>
  );
}

/**
 * 封面「氛围光货架」：首张海报重模糊后铺满做氛围光晕（每个库有自己的
 * 色调），最多 4 张海报立体站排，底部倒影直接落在氛围暗底上表达
 * 「反光地面」；0 张=类型图标底纹。卡片 21/10 比例，海报占约 2/3。
 */
/** 扫描进度环：有分母画百分比，刚起步（进度未知）转圈占位。 */
/** 扫描/整理/元数据刷新共用的进度环（三者的进度都是 已处理/总数）。 */
function ScanProgressRing({ progress }: { progress: { processed: number; total: number } | null }) {
  const pct =
    progress && progress.total > 0
      ? Math.min(100, Math.round((progress.processed / progress.total) * 100))
      : null;
  const R = 26;
  const C = 2 * Math.PI * R;
  return (
    <div className="relative size-[72px]">
      <svg
        viewBox="0 0 64 64"
        className={`size-full -rotate-90 ${pct === null ? "animate-spin" : ""}`}
      >
        <circle cx="32" cy="32" r={R} fill="none" stroke="rgba(255,255,255,0.2)" strokeWidth="5" />
        <circle
          cx="32"
          cy="32"
          r={R}
          fill="none"
          stroke="white"
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={pct === null ? C * 0.75 : C * (1 - pct / 100)}
          className="transition-[stroke-dashoffset] duration-500 ease-out"
        />
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-ui font-semibold text-white">
        {pct === null ? "…" : `${pct}%`}
      </span>
    </div>
  );
}

function LibraryCover({
  libraryId,
  posters,
  Icon,
}: {
  libraryId: number;
  posters: string[];
  Icon: typeof FilmIcon;
}) {
  // 服务端渲染的「氛围光货架」拼贴（与 Jellyfin 兼容层给播放器的是同一张图）：
  // 一次 <img> 请求替代 9+ 张图的客户端合成，ETag 协商缓存，渲染显著更快。
  // 拼贴尚未生成/加载失败时回退到原客户端 CSS 货架（素材同源，观感一致）。
  const [collageFailed, setCollageFailed] = useState(false);
  if (posters.length === 0) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-[#1c2230] to-[#10131c]">
        <Icon className="size-12 text-white/[0.13]" />
      </div>
    );
  }
  if (!collageFailed) {
    return (
      <div className="absolute inset-0 overflow-hidden">
        <img
          src={`${publicEnv.apiBaseUrl}/libraries/${libraryId}/cover`}
          alt=""
          loading="lazy"
          className="absolute inset-0 size-full object-cover transition duration-300 group-hover/lib:scale-[1.02]"
          onError={() => setCollageFailed(true)}
        />
        {/* 悬停扫光沿用：一道斜向柔光掠过底部倒影区 */}
        <div className="pointer-events-none absolute -left-[45%] bottom-0 h-[25%] w-[45%] -skew-x-12 bg-gradient-to-r from-transparent via-white/[0.14] to-transparent transition-transform duration-700 ease-out group-hover/lib:translate-x-[350%]" />
      </div>
    );
  }
  return (
    <div className="absolute inset-0 overflow-hidden">
      {/* 氛围光：首图放大重模糊 + 提饱和，再整体压暗保证前景对比度 */}
      <img
        src={imageUrl(posters[0])}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        className="absolute inset-0 size-full scale-150 object-cover opacity-70 blur-3xl saturate-150"
      />
      <div className="absolute inset-0 bg-[#080a10]/50" />
      {/* 灯箱底光：首图模糊后以 screen 混合从底边向上发光，颜色天然
          取自海报主色；再叠一个中性地面光斑，像射灯打在舞台地面上 */}
      <img
        src={imageUrl(posters[0])}
        alt=""
        aria-hidden
        loading="lazy"
        referrerPolicy="no-referrer"
        className="absolute inset-x-0 bottom-0 h-1/2 w-full object-cover opacity-55 blur-3xl saturate-150 mix-blend-screen [mask-image:linear-gradient(to_top,black,transparent)]"
      />
      <div className="absolute inset-x-[8%] bottom-0 h-[28%] [background:radial-gradient(60%_100%_at_50%_100%,rgba(255,255,255,0.09),transparent_70%)]" />
      {/* 海报排：立在玻璃搁板上，悬停整排轻微上浮 */}
      <div className="absolute inset-x-0 top-[4.5%] flex justify-center gap-[2%] px-[2%]">
        {posters.map((url, i) => (
          <div
            key={i}
            className="w-[22.5%] shrink-0 transition duration-300 group-hover/lib:-translate-y-1"
          >
            <img
              src={imageUrl(url)}
              alt=""
              loading="lazy"
              referrerPolicy="no-referrer"
              className="aspect-[2/3] w-full rounded-[4px] object-cover shadow-[0_6px_18px_rgba(0,0,0,0.5)] ring-1 ring-white/20"
            />
            {/* 倒影：翻转副本贴着底边，向下快速渐隐。注意 mask 在元素本地
                坐标系生效、会跟着 scaleY(-1) 一起翻转，所以这里写 to top，
                翻转后在屏幕上才是「贴近海报处最实、向下淡出」 */}
            <img
              src={imageUrl(url)}
              alt=""
              aria-hidden
              loading="lazy"
              referrerPolicy="no-referrer"
              className="mt-[2px] aspect-[2/3] w-full -scale-y-100 rounded-[4px] object-cover opacity-55 blur-[1px] [mask-image:linear-gradient(to_top,rgba(0,0,0,0.7),transparent_26%)]"
            />
          </div>
        ))}
      </div>
      {/* 悬停扫光：一道斜向柔光从左扫到右掠过倒影区（transform 过渡
          实现单次扫过，移出卡片后自动滑回原位待命） */}
      <div className="pointer-events-none absolute -left-[45%] bottom-0 h-[25%] w-[45%] -skew-x-12 bg-gradient-to-r from-transparent via-white/[0.14] to-transparent transition-transform duration-700 ease-out group-hover/lib:translate-x-[350%]" />
    </div>
  );
}

/** 卡片右上角的管理菜单（Portal 到 body，同侧栏会话菜单的处理）。 */
function LibraryCardMenu({
  library,
  onEdit,
  onScan,
  onOrganize,
  onRefresh,
  onError,
  canMoveLeft,
  canMoveRight,
  onMove,
}: {
  library: MediaLibrary;
  onEdit: () => void;
  onScan: () => void;
  onOrganize: () => void;
  onRefresh: () => void;
  onError: (message: string) => void;
  canMoveLeft: boolean;
  canMoveRight: boolean;
  onMove: (offset: -1 | 1) => void;
}) {
  const confirm = useConfirm();
  const [menuPos, setMenuPos] = useState<{ left: number; top: number } | null>(null);
  const open = menuPos != null;

  useEffect(() => {
    if (!open) return;
    const close = () => setMenuPos(null);
    document.addEventListener("mousedown", close);
    // passive：只做关闭动作、不会 preventDefault，别让浏览器为它放弃滚动快路径
    document.addEventListener("scroll", close, { capture: true, passive: true });
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("scroll", close, { capture: true });
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const guard = (fn: () => Promise<unknown>) => {
    setMenuPos(null);
    void fn()
      .then(onRefresh)
      .catch((e) => onError((e as Error).message));
  };

  return (
    <>
      <button
        type="button"
        aria-label={`管理「${library.name}」`}
        onClick={(e) => {
          if (open) {
            setMenuPos(null);
            return;
          }
          const rect = e.currentTarget.getBoundingClientRect();
          setMenuPos({ left: rect.right - 144, top: rect.bottom + 6 });
        }}
        className={`absolute right-3 top-3 flex size-8 items-center justify-center rounded-lg border border-white/[0.14] bg-black/45 text-white/90 backdrop-blur-md transition-opacity duration-200 hover:bg-black/65 ${
          open ? "opacity-100" : "touch-reveal opacity-0 group-hover/lib:opacity-100"
        }`}
      >
        <MoreIcon className="size-4" />
      </button>

      {open &&
        createPortal(
          <div
            onMouseDown={(e) => e.stopPropagation()}
            className="menu-surface w-36 overflow-hidden p-1.5"
            style={{ position: "fixed", left: menuPos.left, top: menuPos.top, zIndex: 50 }}
          >
            {/* 扫描/整理中锁定编辑与删除：进行中的任务在按当前根路径读写台账 */}
            <button
              type="button"
              disabled={library.scanning || library.organizing}
              onClick={() => {
                setMenuPos(null);
                onEdit();
              }}
              className="glass-row px-2.5 py-2 text-ui font-medium disabled:opacity-40"
            >
              编辑库
            </button>
            {/* 重识别与扫描共用库级锁，但它不接受中途停止（后端会拒绝）：
                入口置灰并如实标出阶段，不给一个按了没反应的按钮 */}
            <button
              type="button"
              disabled={
                library.organizing ||
                (library.scanning && library.scan_progress?.phase === "reidentifying")
              }
              onClick={() => {
                setMenuPos(null);
                if (library.scanning) {
                  guard(() => stopLibraryScan(library.id));
                  return;
                }
                // 重操作先确认（停止不确认：停止本身就是在纠正）
                void confirm(scanLibraryConfirm(library.name)).then((ok) => {
                  if (ok) onScan();
                });
              }}
              className="glass-row px-2.5 py-2 text-ui font-medium disabled:opacity-40"
            >
              {!library.scanning
                ? "扫描库"
                : library.scan_progress?.phase === "reidentifying"
                  ? "正在重新识别…"
                  : "停止扫描"}
            </button>
            {/* 整库元数据刷新（与库详情页 ⋯ 菜单同款入口）：与扫描不互斥
                ——刷新只写元数据表，不碰台账与文件 */}
            <button
              type="button"
              onClick={() => {
                setMenuPos(null);
                if (library.metadata_refresh) {
                  guard(() => stopLibraryMetadataRefresh(library.id));
                  return;
                }
                void confirm(refreshLibraryConfirm(library.name)).then((ok) => {
                  if (ok) guard(() => startLibraryMetadataRefresh(library.id));
                });
              }}
              className="glass-row px-2.5 py-2 text-ui font-medium"
            >
              {library.metadata_refresh ? "停止刷新元数据" : "刷新元数据"}
            </button>
            <button
              type="button"
              disabled={library.scanning || library.organizing}
              onClick={() => {
                setMenuPos(null);
                onOrganize();
              }}
              className="glass-row px-2.5 py-2 text-ui font-medium disabled:opacity-40"
            >
              {library.organizing ? "正在整理…" : "整理文件名"}
            </button>
            <button
              type="button"
              disabled={library.is_default}
              onClick={() => guard(() => setDefaultLibrary(library.id))}
              className="glass-row px-2.5 py-2 text-ui font-medium disabled:opacity-40"
            >
              设为默认库
            </button>
            {/* 展示顺序：决定首页卡片与「最近添加」分区的排列（两处同步换位） */}
            <button
              type="button"
              disabled={!canMoveLeft}
              onClick={() => {
                setMenuPos(null);
                onMove(-1);
              }}
              className="glass-row px-2.5 py-2 text-ui font-medium disabled:opacity-40"
            >
              向前移动
            </button>
            <button
              type="button"
              disabled={!canMoveRight}
              onClick={() => {
                setMenuPos(null);
                onMove(1);
              }}
              className="glass-row px-2.5 py-2 text-ui font-medium disabled:opacity-40"
            >
              向后移动
            </button>
            <button
              type="button"
              disabled={library.scanning || library.organizing}
              onClick={() => {
                setMenuPos(null);
                void confirm({
                  title: `删除媒体库「${library.name}」？`,
                  description: "磁盘文件不受影响，挂在它上面的订阅将回落到该类型的默认库。",
                  confirmLabel: "删除库",
                  tone: "danger",
                }).then((ok) => {
                  if (ok) guard(() => deleteLibrary(library.id));
                });
              }}
              className="glass-row px-2.5 py-2 text-ui font-medium !text-[var(--danger)] hover:!bg-[rgba(255,107,107,0.12)] disabled:opacity-40"
            >
              删除库
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}

/* —— 新增 / 编辑库的弹窗（订阅弹层同款视觉），库页与单库页共用 —— */

export function LibraryFormDialog({
  state,
  onClose,
  onSaved,
}: {
  /** "new"=新增；库对象=编辑；null=关闭 */
  state: MediaLibrary | "new" | null;
  onClose: () => void;
  /** 保存成功回调，带上服务端返回的库（新建时调用方据此把新卡片滚进视野） */
  onSaved: (saved: MediaLibrary) => void;
}) {
  const library = state === "new" ? null : state;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<MediaType>("movie");
  const [name, setName] = useState("");
  // 根路径列表：第一项为主根；通过目录选择器逐个添加
  const [roots, setRoots] = useState<string[]>([]);
  // 选择器目标："add"=追加新根；数字=更改该下标的既有根（原位替换）；null=关闭
  const [pickerTarget, setPickerTarget] = useState<"add" | number | null>(null);
  // 收藏范围（可选声明）：类型 ID 多选 + 区域国家码多选，条件间是"且"
  const [matchGenres, setMatchGenres] = useState<number[]>([]);
  const [matchRegions, setMatchRegions] = useState<string[]>([]);
  // 扫描后自动清理已确认丢失的库存记录（默认关，见 Library.auto_clear_missing）
  const [autoClearMissing, setAutoClearMissing] = useState(false);
  // 实时文件监控（默认开）：SMB/NFS 网络挂载收不到远端变更事件、递归建
  // 监听还很慢，按库关闭后靠定期对账与手动扫描（见 Library.realtime_watch）
  const [realtimeWatch, setRealtimeWatch] = useState(true);
  // 库级刮削覆盖（语言/选图/命名/目录写入，即刮削设置的全部字段）：
  // **只存显式覆盖的键**，空对象 = 全跟全局设置
  const [scrapeOverrides, setScrapeOverrides] = useState<Record<string, unknown>>({});
  // 表单分三个页签：必填的基本信息 / 可选的收藏范围 / 可选的刮削设置，
  // 避免单页长滚动拥挤（刮削设置见 docs/design/scrape-customization.md §14.5）
  const [tab, setTab] = useState<"basic" | "scope" | "scrape">("basic");
  const routingOptions = useRoutingOptions();

  // 每次打开时按目标重置表单（编辑带入现值，新增清空）
  useEffect(() => {
    if (state === null) return;
    setError(null);
    setKind(library?.kind ?? "movie");
    setName(library?.name ?? "");
    setRoots(library?.root_paths ?? []);
    const parsed = parseMatchRules(library?.match_rules ?? []);
    setMatchGenres(parsed.genres);
    setMatchRegions(parsed.regions);
    setAutoClearMissing(library?.auto_clear_missing ?? false);
    setRealtimeWatch(library?.realtime_watch ?? true);
    setScrapeOverrides({ ...(library?.scrape_overrides ?? {}) });
    setPickerTarget(null);
    setTab("basic");
  }, [state, library]);

  if (state === null) return null;

  const canSubmit = !busy && name.trim().length > 0 && roots.length > 0;
  const effectiveKind = library?.kind ?? kind;
  const genreOptions =
    routingOptions === null
      ? []
      : effectiveKind === "movie"
        ? routingOptions.movie_genres
        : routingOptions.tv_genres;

  const submit = () => {
    setBusy(true);
    setError(null);
    // 类型 ID 按当前库类型过滤：新建时切换过库类型的话，另一类型独有的
    // ID（如剧集的"真人秀"）不带进电影库的声明
    const validIds = new Set(genreOptions.map((g) => g.id));
    const genres =
      routingOptions === null ? matchGenres : matchGenres.filter((id) => validIds.has(id));
    const payload: LibraryPayload = {
      name: name.trim(),
      kind,
      root_paths: roots,
      match_rules: buildMatchRules(genres, matchRegions),
      auto_clear_missing: autoClearMissing,
      realtime_watch: realtimeWatch,
      scrape_overrides: scrapeOverrides,
    };
    void (library ? updateLibrary(library.id, payload) : createLibrary(payload))
      .then(onSaved)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  const inputClass =
    "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui " +
    "text-[var(--text)] outline-none focus:border-[var(--accent)]/60";
  const labelClass = "mb-1.5 block text-sub font-medium text-[var(--text-muted)]";

  // 收藏范围的当前声明摘要（底栏常显）：切到基本信息页签也能看到已设了什么。
  // 区域走 regionLabels 折叠（整组折叠 + 零散国家码兜底）；genre 只算当前
  // 库类型下有效的（另一类型独有的提交时会被过滤）
  const activeRegionLabels = routingOptions === null ? [] : regionLabels(matchRegions, routingOptions);
  const activeGenreLabels = genreOptions.filter((g) => matchGenres.includes(g.id)).map((g) => g.label);
  const scopeParts = [activeRegionLabels.join(" / "), activeGenreLabels.join(" / ")].filter(Boolean);
  // 可选项未加载完时按原始选择兜底，避免编辑刚打开的一瞬小圆点闪灭
  const scopeDeclared =
    routingOptions === null
      ? matchRegions.length > 0 || matchGenres.length > 0
      : scopeParts.length > 0;
  const scopeSummary =
    scopeParts.length > 0
      ? `只收：${scopeParts.join(" + ")}`
      : scopeDeclared
        ? "已声明收藏范围"
        : "收藏范围未声明";
  // 保存按钮置灰时说明缺什么，不让用户对着灰按钮猜
  const missingFields = [name.trim() ? null : "名称", roots.length > 0 ? null : "根路径"].filter(
    (v): v is string => v !== null,
  );

  return (
    <>
      <Modal
        open
        onClose={onClose}
        label={library ? `编辑「${library.name}」` : "添加媒体库"}
        width="lg"
        panelClassName="flex max-h-[82dvh] flex-col"
      >
        {/* 头部：标题 + 分段页签 */}
        <div className="shrink-0 border-b border-white/[0.06] px-6 pt-5 max-md:px-5">
          <h2 className="text-title font-bold text-white">
            {library ? "编辑媒体库" : "添加媒体库"}
            {library && (
              <span className="ml-2 text-ui font-normal text-[var(--text-muted)]">
                {library.name}
              </span>
            )}
          </h2>
          <div role="tablist" aria-label="表单分区" className="mt-3 flex gap-5">
            {(
              [
                ["basic", "基本信息"],
                ["scope", "收藏范围"],
                ["scrape", "刮削设置"],
              ] as const
            ).map(([key, tabLabel]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={tab === key}
                onClick={() => setTab(key)}
                className={`relative pb-2.5 text-ui font-medium transition-colors ${
                  tab === key ? "text-white" : "text-[var(--text-muted)] hover:text-white"
                }`}
              >
                {tabLabel}
                {/* 收藏范围已有声明 / 刮削设置有覆盖时点亮小圆点：
                    不点开页签也知道设过 */}
                {((key === "scope" && scopeDeclared) ||
                  (key === "scrape" && Object.keys(scrapeOverrides).length > 0)) && (
                  <span className="ml-1 inline-block size-1.5 -translate-y-1 rounded-full bg-[var(--accent)]" />
                )}
                {tab === key && (
                  <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-[var(--accent)]" />
                )}
              </button>
            ))}
          </div>
        </div>

        {/* 主体：当前页签的表单区。视口够高时 min-h 抑制切页签的高度跳动；
            矮视口（横屏手机）必须允许收缩，否则底栏会被挤出面板裁掉——
            这里跟的是高度不是宽度，故用 min-height 媒体查询而非 md: 断点 */}
        <div className="scroll-thin min-h-0 flex-1 space-y-4 overflow-y-auto p-6 max-md:p-5 [@media(min-height:600px)]:min-h-[280px]">
          {tab === "basic" && (
            <>
          {/* 类型：创建后不可改（订阅按类型挂库） */}
          <div>
            <label className={labelClass}>库类型{library ? "（创建后不可修改）" : ""}</label>
            <div className="flex flex-wrap gap-2">
              {(Object.keys(LIBRARY_KIND_META) as MediaType[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  disabled={library !== null}
                  onClick={() => setKind(k)}
                  data-active={(library?.kind ?? kind) === k}
                  className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium disabled:opacity-60"
                >
                  {LIBRARY_KIND_META[k].label}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className={labelClass}>名称</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                // isComposing：中文输入法选词的回车不当提交
                if (e.key === "Enter" && !e.nativeEvent.isComposing && canSubmit) submit();
              }}
              placeholder="如：电影库 / 动漫库"
              autoComplete="off"
              className={inputClass}
            />
          </div>

          <div>
            <label className={labelClass}>根路径（第一个为主根）</label>
            <div className="space-y-1.5">
              {roots.map((root, i) => (
                <div
                  key={root}
                  className="group flex items-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2"
                >
                  <FolderIcon className="size-4 shrink-0 text-[var(--accent)]/80" />
                  {/* 保尾截断（dir=rtl）：路径的区分信息在尾部，省略号出现在头部；
                      LRM 标记防止首尾的 "/" 在 RTL 下跳位。点击可原位更改目录 */}
                  <Tooltip
                    content={
                      <>
                        <p className="mb-1 break-all font-mono text-caption text-[var(--text-muted)]">{root}</p>
                        点击更改：从当前路径开始重新选择目录。
                      </>
                    }
                  >
                    <button
                      type="button"
                      dir="rtl"
                      onClick={() => setPickerTarget(i)}
                      className="min-w-0 flex-1 truncate rounded text-left font-mono text-ui text-[var(--text)] transition-colors hover:text-[var(--accent)]"
                    >
                      {"‎" + root + "‎"}
                    </button>
                  </Tooltip>
                  {i === 0 ? (
                    <Tooltip
                      content={
                        <>
                          <strong>主根 = 新内容的落盘位置。</strong>
                          订阅与手动下载完成后，按「主根/标题 (年份)」建目录入库；
                          一个库可挂多个根，但写入点只有主根这一个。
                        </>
                      }
                    >
                      <span className="shrink-0 cursor-default rounded-full bg-[var(--accent)]/15 px-2 py-0.5 text-micro font-semibold text-[var(--accent)]">
                        主根
                      </span>
                    </Tooltip>
                  ) : (
                    <Tooltip content="把该路径设为新内容的落盘位置（移到列表第一位）。已有文件不会被移动。">
                      <button
                        type="button"
                        onClick={() => setRoots([root, ...roots.filter((r) => r !== root)])}
                        className="touch-reveal shrink-0 rounded-full px-2 py-0.5 text-micro font-medium text-[var(--text-faint)] opacity-0 transition-opacity hover:bg-white/10 hover:text-white group-hover:opacity-100"
                      >
                        设为主根
                      </button>
                    </Tooltip>
                  )}
                  <button
                    type="button"
                    aria-label={`移除 ${root}`}
                    onClick={() => setRoots(roots.filter((r) => r !== root))}
                    className="shrink-0 rounded-md p-1 text-[var(--text-faint)] transition-colors hover:bg-white/10 hover:text-white"
                  >
                    <XIcon className="size-3.5" />
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={() => setPickerTarget("add")}
                className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-white/15 px-3 py-2.5 text-ui font-medium text-[var(--text-muted)] transition-colors hover:border-[var(--accent)]/50 hover:text-white"
              >
                <PlusIcon className="size-4" />
                {roots.length === 0 ? "浏览服务器目录并添加" : "添加目录"}
              </button>
            </div>
            <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
              新入库的内容落在<strong className="font-medium text-[var(--text-muted)]">主根</strong>下：主根/标题
              (年份)。其余为扩展根：扫描与监控照常覆盖，但不写入新内容。
            </p>
          </div>

          {/* 实时文件监控：默认开。SMB/NFS 网络挂载收不到远端变更事件、
              递归建监听又慢（大库可达分钟级），按库关闭后新文件由定期
              对账与手动扫描发现（不实时但不缺失） */}
          <div>
            <label className="flex cursor-pointer select-none items-start gap-2.5">
              <input
                type="checkbox"
                checked={realtimeWatch}
                onChange={(e) => setRealtimeWatch(e.target.checked)}
                className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
              />
              <span className="min-w-0">
                <span className="block text-ui font-medium text-[var(--text)]">
                  实时监控目录变化
                </span>
                <span className="mt-1 block text-caption leading-relaxed text-[var(--text-faint)]">
                  监听根路径的文件变动，新文件落盘后自动增量扫描入库。
                  <strong className="font-medium text-[var(--text-muted)]">
                    SMB/NFS 等网络挂载建议关闭
                  </strong>
                  ——远端的文件变化收不到通知，建立监听还可能非常缓慢。关闭后新文件由定期对账和手动扫描发现，不实时但不会缺失。
                </span>
              </span>
            </label>
          </div>

          {/* 自动清理丢失记录：默认关。开着才在扫描收尾把"磁盘上确认没了"的
              台账行删掉——自己在磁盘上删片的用户不必每次扫完再手动清一遍；
              不开则保留记录（缺失清单的「重新下载」与改名归并都靠它） */}
          <div>
            <label className="flex cursor-pointer select-none items-start gap-2.5">
              <input
                type="checkbox"
                checked={autoClearMissing}
                onChange={(e) => setAutoClearMissing(e.target.checked)}
                className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
              />
              <span className="min-w-0">
                <span className="block text-ui font-medium text-[var(--text)]">
                  扫描后自动清理丢失记录
                </span>
                <span className="mt-1 block text-caption leading-relaxed text-[var(--text-faint)]">
                  自己在磁盘上删了片子后，扫描结束即把这些记录清出台账，文件数与磁盘保持一致，
                  不必再手动清一次缺失。<strong className="font-medium text-[var(--text-muted)]">只删台账、不动磁盘，但记录删了不可恢复</strong>
                  ——「缺失」清单里的「重新下载」也会随之消失。关闭时记录保留，文件回归自动恢复。
                  目录读不动（权限/掉盘/网络挂载抖动）的那一轮不会清理。
                </span>
              </span>
            </label>
          </div>

            </>
          )}

          {/* 收藏范围（可选）：声明"本库收什么"，订阅与自动入库按它自动选库。
              区域逐国勾选（条件本就是 any_of，预设组降级为一键整组勾选的快捷键）；
              两个维度间是"且" */}
          {tab === "scope" && (
            <>
          <p className="text-sub leading-relaxed text-[var(--text-muted)]">
            可选：声明「本库收什么」后，订阅与自动入库按作品特征自动选进本库。
            全部留空 = 不声明，该类型的默认库承接未命中的作品；订阅时永远可以手动改库。
          </p>
          {routingOptions === null ? (
            <p className="rounded-xl border border-white/[0.08] bg-white/[0.03] px-3 py-2.5 text-sub text-[var(--text-faint)]">
              正在加载可选项…
            </p>
          ) : (
            <>
              <div>
                <label className={labelClass}>区域（勾选任一即匹配）</label>
                {/* 逐个国家勾选是真正的选项（可任意组合，如只收日本）；
                    已选中但不在内置映射里的码补进列表，保证选了就能看见、能取消 */}
                <div className="flex flex-wrap gap-1.5">
                  {[
                    ...Object.entries(routingOptions.country_names),
                    ...matchRegions
                      .filter((c) => !(c in routingOptions.country_names))
                      .map((c): [string, string] => [c, c]),
                  ].map(([code, name]) => (
                    <button
                      key={code}
                      type="button"
                      data-active={matchRegions.includes(code)}
                      onClick={() =>
                        setMatchRegions((prev) =>
                          prev.includes(code)
                            ? prev.filter((c) => c !== code)
                            : [...prev, code],
                        )
                      }
                      className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
                    >
                      {name}
                    </button>
                  ))}
                </div>
                {/* 预设组保留为快捷键：一键选中/取消整组，避免「欧美」要点十下 */}
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <span className="text-caption text-[var(--text-faint)]">快捷组合</span>
                  {/* 全选/清空：如「纪录片库收全部区域」这类声明逐个点太费劲；
                      全选后条件数 +1，还能顺带压过单条件区域库消除重叠歧义 */}
                  <button
                    type="button"
                    onClick={() => {
                      const all = Object.keys(routingOptions.country_names);
                      setMatchRegions((prev) =>
                        all.every((c) => prev.includes(c)) ? [] : [...new Set([...prev, ...all])],
                      );
                    }}
                    className="glass-row nav-item !w-auto px-2.5 py-1 text-caption font-medium"
                  >
                    {Object.keys(routingOptions.country_names).every((c) =>
                      matchRegions.includes(c),
                    )
                      ? "清空"
                      : "全选"}
                  </button>
                  {routingOptions.region_presets.map((preset) => {
                    const active = preset.countries.every((c) => matchRegions.includes(c));
                    return (
                      <button
                        key={preset.key}
                        type="button"
                        data-active={active}
                        title={preset.countries
                          .map((c) => routingOptions.country_names[c] ?? c)
                          .join(" / ")}
                        onClick={() =>
                          setMatchRegions((prev) =>
                            active
                              ? prev.filter((c) => !preset.countries.includes(c))
                              : [...new Set([...prev, ...preset.countries])],
                          )
                        }
                        className="glass-row nav-item !w-auto px-2.5 py-1 text-caption font-medium"
                      >
                        {preset.label}
                      </button>
                    );
                  })}
                </div>
              </div>
              <div>
                <label className={labelClass}>类型（勾选任一即匹配）</label>
                <div className="flex flex-wrap items-center gap-1.5">
                  {/* 与区域侧同款的全选/清空快捷键（类型没有预设组，只此一个） */}
                  <button
                    type="button"
                    onClick={() =>
                      setMatchGenres((prev) =>
                        genreOptions.every((g) => prev.includes(g.id))
                          ? []
                          : [...new Set([...prev, ...genreOptions.map((g) => g.id)])],
                      )
                    }
                    className="glass-row nav-item !w-auto px-2.5 py-1 text-caption font-medium"
                  >
                    {genreOptions.every((g) => matchGenres.includes(g.id)) ? "清空" : "全选"}
                  </button>
                  {genreOptions.map((g) => (
                    <button
                      key={g.id}
                      type="button"
                      data-active={matchGenres.includes(g.id)}
                      onClick={() =>
                        setMatchGenres((prev) =>
                          prev.includes(g.id)
                            ? prev.filter((id) => id !== g.id)
                            : [...prev, g.id],
                        )
                      }
                      className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
                    >
                      {g.label}
                    </button>
                  ))}
                </div>
              </div>
              {matchRegions.length > 0 && matchGenres.length > 0 && (
                <p className="text-caption leading-relaxed text-[var(--text-faint)]">
                  区域与类型须<strong className="font-medium text-[var(--text-muted)]">同时满足</strong>
                  （如「日韩 + 动画」= 只收日韩的动画）。
                </p>
              )}
            </>
          )}
            </>
          )}

          {/* 刮削设置（可选）：本库单独的语言/选图/命名/目录写入口味。
              控件与「设置 → 刮削与整理」共用一套，外面包三态壳 */}
          {tab === "scrape" && (
            <LibraryScrapeSettings overrides={scrapeOverrides} onChange={setScrapeOverrides} />
          )}
        </div>

        {/* 底栏：错误 + 范围摘要 + 操作，任一页签下常显 */}
        <div className="shrink-0 border-t border-white/[0.06] px-6 py-4 max-md:px-5">
          {error && (
            <p className="mb-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-ui leading-6 text-red-200">
              {error}
            </p>
          )}
          <div className="flex items-center justify-between gap-3">
            <p className="min-w-0 truncate text-caption text-[var(--text-faint)]">
              {missingFields.length > 0 ? `还需填写：${missingFields.join("、")}` : scopeSummary}
            </p>
            <div className="flex shrink-0 items-center gap-3">
              <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
                取消
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={!canSubmit}
                className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
              >
                {busy ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </div>
      </Modal>

      {/* 服务端目录选择器：追加时从最近添加的根起步，更改时从被改的根起步；
          追加去重，更改为原位替换（改主根仍是主根），撞上已有路径时合并去重 */}
      <DirectoryPicker
        open={pickerTarget !== null}
        initialPath={
          pickerTarget === "add" || pickerTarget === null
            ? roots.length > 0
              ? roots[roots.length - 1]
              : undefined
            : roots[pickerTarget]
        }
        onClose={() => setPickerTarget(null)}
        onSelect={(path) => {
          setRoots((prev) => {
            if (pickerTarget === "add" || pickerTarget === null) {
              return prev.includes(path) ? prev : [...prev, path];
            }
            const next = prev.map((r, idx) => (idx === pickerTarget ? path : r));
            return next.filter((r, idx) => r !== path || idx === pickerTarget);
          });
          setPickerTarget(null);
        }}
      />
    </>
  );
}
