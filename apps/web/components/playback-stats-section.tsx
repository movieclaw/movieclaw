"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import Link from "next/link";
import type { Route } from "next";

import { CheckIcon, HistoryIcon, LockIcon } from "@/components/icons";
import { OverflowText } from "@/components/overflow-text";
import { PosterImage } from "@/components/poster-image";
import {
  fetchPlaybackHistory,
  type MediaActivityScope,
  type MediaActivityTarget,
  type PlaybackLogEntry,
} from "@/lib/api/playback";
import { formatRuntimeMinutes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import { formatClockTime, formatTimelineDayLabel, timelineDayKey } from "@/lib/time";

/**
 * 活动页观看视角的「播放记录」切片（docs/design/activity.md「播放日志与统计」），
 * 以及与「观看统计」（watch-stats-panel.tsx）共用的小件。
 *
 * 数据来自 playback_log——每场播放一行，回答「最近谁在什么时候用什么看了多久」。
 * 不轮询：日志按场记，几秒一刷没有意义，切成员或口径时重拉一次即可。
 * 切片切换与筛选条件由外层工具栏持有，这里只吃 props。
 */

export const STATS_PERIODS: readonly { value: number; label: string }[] = [
  { value: 7, label: "最近 7 天" },
  { value: 30, label: "最近 30 天" },
  { value: 90, label: "最近 90 天" },
] as const;

const HISTORY_PAGE = 30;

/** 毫秒 → 「2 小时 6 分钟」；不足一分钟按一分钟，零显示「—」。 */
export function formatWatched(ms: number): string {
  if (ms <= 0) return "—";
  return formatRuntimeMinutes(Math.max(1, Math.round(ms / 60_000)));
}

function unitLabel(media: MediaActivityTarget): string | null {
  if (media.kind !== "tv") return null;
  const season = String(media.season_number).padStart(2, "0");
  const episode = String(media.episode_number).padStart(2, "0");
  return `S${season}E${episode}`;
}

function detailHref(media: MediaActivityTarget): Route | null {
  if (media.library_id == null || !media.browsable) return null;
  return `/library/${media.library_id}/item/${media.media_item_id}` as Route;
}

export function TitleText({
  media,
  episode = true,
  large = false,
}: {
  media: MediaActivityTarget;
  /** 剧集是否带上「S01E03 第 3 集」；按作品聚合的地方（最受欢迎）只要剧名 */
  episode?: boolean;
  /** 领奖台第一名用的大号标题 */
  large?: boolean;
}) {
  const unit = episode ? unitLabel(media) : null;
  const href = detailHref(media);
  const text = (
    <>
      {media.title || "（条目已删除）"}
      {unit && <span className="tnum ml-1.5 font-normal text-white/60">{unit}</span>}
      {episode && media.episode_title && (
        <span className="ml-1.5 font-normal text-white/45">{media.episode_title}</span>
      )}
      {!media.browsable && media.library_id != null && (
        <span className="ml-1.5 rounded-md border border-white/15 px-1.5 py-px text-[11px] font-medium text-white/50">
          仅管理
        </span>
      )}
    </>
  );
  return (
    <OverflowText
      lines={large ? 2 : 1}
      className={`text-white/90 ${large ? "text-[17px] font-bold leading-6" : "text-ui font-semibold"}`}
    >
      {href ? (
        <Link href={href} className="transition hover:text-white">
          {text}
        </Link>
      ) : (
        text
      )}
    </OverflowText>
  );
}

/** 骨架块：读取中用来占住最终布局，内容到达时整页不跳动。 */
export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={`animate-pulse rounded-lg bg-white/[0.06] motion-reduce:animate-none ${className}`}
    />
  );
}

/**
 * 空状态：不是故障，不用警告色，也不留一片空白。说清三件事——这里本来会有什么、
 * 为什么现在没有、接下来能做什么（可选的动作，如清掉筛选、放宽范围、换个周期）。
 * compact 版用在面板内部，只占一两行，不抢整页的空状态。
 */
export function EmptyState({
  icon,
  title,
  description,
  actions,
  compact = false,
}: {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  compact?: boolean;
}) {
  if (compact) {
    return (
      <div className="flex items-center gap-3 px-4 py-4 max-md:px-3.5">
        {icon && (
          <span className="grid size-7 shrink-0 place-items-center rounded-full bg-white/[0.05] text-white/40">
            {icon}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <p className="text-sub text-white/60">{title}</p>
          {description && <p className="mt-0.5 text-caption text-white/40">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
    );
  }
  return (
    <section
      aria-label={title}
      className="rounded-2xl border border-white/[0.07] bg-white/[0.02] px-6 py-10 text-center max-md:px-4 max-md:py-8"
    >
      <div className="mx-auto flex max-w-[420px] flex-col items-center">
        {icon && (
          <span className="mb-3 grid size-12 place-items-center rounded-full border border-white/[0.08] bg-white/[0.04] text-white/55 shadow-[0_0_40px_rgba(159,176,201,0.10)]">
            {icon}
          </span>
        )}
        <h3 className="text-ui font-semibold text-white/90">{title}</h3>
        {description && (
          <p className="mt-1.5 text-caption leading-6 text-white/45">{description}</p>
        )}
        {actions && (
          <div className="mt-4 flex flex-wrap items-center justify-center gap-2">{actions}</div>
        )}
      </div>
    </section>
  );
}

/** 空状态里的动作：文字按钮，与「显示全部」同一套语气。 */
export function EmptyAction({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center rounded-full border border-white/[0.12] bg-white/[0.05] px-3.5 py-1.5 text-caption font-medium text-white/80 transition hover:border-white/20 hover:bg-white/[0.09] hover:text-white"
    >
      {children}
    </button>
  );
}

/** 范围外记录的折叠行：只报个数，不出片名与海报；就地可切到「全部」。 */
export function HiddenCountRow({
  count,
  noun,
  onShowAll,
}: {
  count: number;
  noun: string;
  onShowAll?: () => void;
}) {
  if (count <= 0) return null;
  return (
    <p className="px-4 py-2.5 text-caption text-white/45 max-md:px-3.5">
      另有 {count} {noun}不在你的浏览范围内
      {onShowAll && (
        <>
          <span className="mx-1.5 text-white/20">·</span>
          <button
            type="button"
            onClick={onShowAll}
            className="font-medium text-[var(--info)] transition hover:text-white"
          >
            显示全部
          </button>
        </>
      )}
    </p>
  );
}

// ---------------------------------------------------------------------------
// 播放记录：每场一行，按天分组
// ---------------------------------------------------------------------------

/** 读取中的骨架：一个日期组头 + 四行，与真实行同高。 */
function HistorySkeleton() {
  return (
    <div aria-busy="true" aria-label="正在读取播放记录">
      <Skeleton className="mb-2 h-4 w-24" />
      <div className="divide-y divide-white/[0.06] rounded-2xl border border-white/[0.08] bg-white/[0.02]">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="flex items-center gap-3 px-4 py-2.5 max-md:px-3.5">
            <Skeleton className="h-3 w-11" />
            <Skeleton className="h-[42px] w-[28px]" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-3.5 w-1/3" />
              <Skeleton className="h-3 w-1/2" />
            </div>
            <Skeleton className="h-3 w-14" />
          </div>
        ))}
      </div>
    </div>
  );
}

function HistoryRow({ entry }: { entry: PlaybackLogEntry }) {
  const live = entry.ended_at === null;
  const device = [entry.client, entry.device_name].filter(Boolean).join(" · ");
  return (
    <div className="flex items-center gap-3 px-4 py-2.5 max-md:px-3.5">
      <span className="tnum w-11 shrink-0 text-caption text-white/40">
        {formatClockTime(entry.started_at)}
      </span>
      {/* 28×42 的小图走 poster-card 派生图：一页 30 行若取 w780 原图（设置里选了
          original 就是 MB 级），冷缓存那次要下几 MB 并在主线程同步解码，
          就是「最近播放偶尔打开特别卡」的浏览器侧那一半 */}
      <PosterImage
        src={entry.media.poster_url ? imageUrl(entry.media.poster_url, "poster-card") : null}
        alt={entry.media.title}
        className="h-[42px] w-[28px] shrink-0 rounded-lg object-cover ring-1 ring-white/10"
      />
      <div className="min-w-0 flex-1">
        <TitleText media={entry.media} />
        <p className="mt-0.5 truncate text-caption leading-5 text-white/40">
          {entry.member_name}
          {device && (
            <>
              <span className="mx-1.5 text-white/20">·</span>
              {device}
            </>
          )}
        </p>
      </div>
      <div className="shrink-0 text-right text-caption leading-5">
        {live ? (
          <span className="text-[var(--ok)]">播放中</span>
        ) : entry.completed ? (
          <span className="inline-flex items-center gap-1 text-[var(--ok)]">
            <CheckIcon className="size-3" />
            看完
          </span>
        ) : (
          <span className="tnum text-white/60">
            {entry.progress_percent != null ? `看到 ${entry.progress_percent}%` : "播放过"}
          </span>
        )}
        {entry.watched_ms > 0 && (
          <p className="tnum text-white/35">{formatWatched(entry.watched_ms)}</p>
        )}
      </div>
    </div>
  );
}

/**
 * 播放记录列表：滚动到底自动续载（IntersectionObserver 盯着列表尾部的哨兵），
 * 「加载更多」按钮留作兜底。翻页用服务端游标而不是 offset：记录会一直往前追加，
 * 续载期间新开的一场会把 offset 整体后推，同一行就会在两页里各出现一次。
 */
export function PlaybackHistoryList({
  scope,
  memberId,
  memberLabel,
  onClearMember,
  onShowAll,
}: {
  scope: MediaActivityScope;
  /** 按成员筛选；null = 全部 */
  memberId: number | null;
  /** 筛选中成员的显示名，空状态里点名用 */
  memberLabel: string | null;
  onClearMember: () => void;
  onShowAll: () => void;
}) {
  const [entries, setEntries] = useState<PlaybackLogEntry[]>([]);
  const [hiddenCount, setHiddenCount] = useState(0);
  const [cursor, setCursor] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  // 一次只允许一个在途请求：哨兵在续载期间会反复进出视口
  const pending = useRef(false);
  // 切筛选后，仍在途的旧请求不能把旧口径的行拼进新列表
  const generation = useRef(0);

  const load = useCallback(
    async (before: number | null) => {
      if (pending.current) return;
      pending.current = true;
      const gen = generation.current;
      setLoading(true);
      try {
        const page = await fetchPlaybackHistory({ limit: HISTORY_PAGE, before, scope, memberId });
        if (gen !== generation.current) return;
        setEntries((prev) => (before === null ? page.entries : [...prev, ...page.entries]));
        setHiddenCount((prev) => (before === null ? page.hidden_count : prev + page.hidden_count));
        setCursor(page.next_cursor);
        setHasMore(page.has_more);
        setError(null);
      } catch (caught) {
        if (gen === generation.current) {
          setError((caught as Error).message || "播放记录加载失败");
        }
      } finally {
        pending.current = false;
        if (gen === generation.current) setLoading(false);
      }
    },
    [scope, memberId],
  );

  useEffect(() => {
    generation.current += 1;
    pending.current = false;
    setEntries([]);
    setHiddenCount(0);
    setCursor(null);
    setHasMore(false);
    void load(null);
  }, [load]);

  const loadMore = useCallback(() => {
    if (hasMore && cursor !== null) void load(cursor);
  }, [hasMore, cursor, load]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMore || typeof IntersectionObserver === "undefined") return;
    // 提前半屏触发，滚到底时下一页多半已经就位
    const observer = new IntersectionObserver(
      (records) => {
        if (records.some((record) => record.isIntersecting)) loadMore();
      },
      { rootMargin: "0px 0px 50% 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loadMore]);

  const groups = useMemo(() => {
    const byDay = new Map<string, PlaybackLogEntry[]>();
    for (const entry of entries) {
      const key = timelineDayKey(entry.started_at);
      const bucket = byDay.get(key);
      if (bucket) bucket.push(entry);
      else byDay.set(key, [entry]);
    }
    return [...byDay.entries()];
  }, [entries]);

  if (error) {
    return (
      <p className="rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-6 text-amber-100">
        {error}
      </p>
    );
  }
  if (loading && entries.length === 0) {
    return <HistorySkeleton />;
  }
  if (entries.length === 0 && hiddenCount === 0) {
    return memberId != null ? (
      <EmptyState
        icon={<HistoryIcon className="size-5" />}
        title={`${memberLabel ?? "这位成员"}还没有播放记录`}
        description="从这位成员下一次播放起，谁、什么时候、用什么设备、看了多久都会记在这里。"
        actions={<EmptyAction onClick={onClearMember}>查看全部成员</EmptyAction>}
      />
    ) : (
      <EmptyState
        icon={<HistoryIcon className="size-5" />}
        title="还没有播放记录"
        description="从现在起每一场播放都会记在这里：谁、什么时候、用什么设备、看到哪、看了多久。网页播放器和 Jellyfin 客户端都算。"
      />
    );
  }
  if (entries.length === 0) {
    return (
      <EmptyState
        icon={<LockIcon className="size-5" />}
        title="这些播放都在你的浏览范围外"
        description={`另有 ${hiddenCount} 场播放来自你设为不可见的库；切到「全部」即可看到片名。`}
        actions={<EmptyAction onClick={onShowAll}>显示全部</EmptyAction>}
      />
    );
  }
  return (
    <div className="space-y-5">
      {groups.map(([day, items]) => (
        <section key={day} aria-label={day}>
          <h3 className="mb-2 text-caption font-semibold text-white/45">
            {formatTimelineDayLabel(items[0].started_at)}
            <span className="tnum ml-1.5 font-normal text-white/30">{items.length} 场</span>
          </h3>
          <div className="divide-y divide-white/[0.06] rounded-2xl border border-white/[0.08] bg-white/[0.02]">
            {items.map((entry) => (
              <HistoryRow key={entry.id} entry={entry} />
            ))}
          </div>
        </section>
      ))}
      {hiddenCount > 0 && (
        <div className="rounded-2xl border border-white/[0.07] bg-white/[0.02]">
          <HiddenCountRow count={hiddenCount} noun="场播放" onShowAll={onShowAll} />
        </div>
      )}
      {hasMore && (
        <div ref={sentinelRef}>
          <button
            type="button"
            disabled={loading}
            onClick={loadMore}
            className="glass-row w-full rounded-xl py-2.5 text-center text-sub text-white/70 disabled:opacity-40"
          >
            {loading ? "正在加载更早的记录…" : "加载更多"}
          </button>
        </div>
      )}
      {!hasMore && entries.length >= HISTORY_PAGE && (
        <p className="py-2 text-center text-caption text-white/30">已经到最早的记录了</p>
      )}
    </div>
  );
}
