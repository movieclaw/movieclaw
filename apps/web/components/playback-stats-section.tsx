"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import Link from "next/link";
import type { Route } from "next";

import { CheckIcon } from "@/components/icons";
import { OverflowText } from "@/components/overflow-text";
import { PosterImage } from "@/components/poster-image";
import {
  fetchPlaybackHistory,
  fetchPlaybackWatchStats,
  type MediaActivityScope,
  type MediaActivityTarget,
  type PlaybackLogEntry,
  type PlaybackWatchStats,
} from "@/lib/api/playback";
import { formatRuntimeMinutes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import { formatClockTime, formatTimelineDayLabel, timelineDayKey } from "@/lib/time";

/**
 * 活动页观看视角的两个历史切片：「播放记录」与「观看统计」
 * （docs/design/activity.md「播放日志与统计」）。
 *
 * 数据来自 playback_log——每场播放一行，回答「最近谁在什么时候用什么看了多久」。
 * 不轮询：日志按场记，几秒一刷没有意义，切周期、成员或口径时重拉一次即可。
 * 切片切换与筛选条件由外层工具栏持有，这里只吃 props。
 */

export const STATS_PERIODS: readonly { value: number; label: string }[] = [
  { value: 7, label: "最近 7 天" },
  { value: 30, label: "最近 30 天" },
  { value: 90, label: "最近 90 天" },
] as const;

const HISTORY_PAGE = 30;

/** 毫秒 → 「2 小时 6 分钟」；不足一分钟按一分钟，零显示「—」。 */
function formatWatched(ms: number): string {
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

function TitleText({ media }: { media: MediaActivityTarget }) {
  const unit = unitLabel(media);
  const href = detailHref(media);
  const text = (
    <>
      {media.title || "（条目已删除）"}
      {unit && <span className="tnum ml-1.5 font-normal text-white/60">{unit}</span>}
      {media.episode_title && (
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
    <OverflowText lines={1} className="text-ui font-semibold text-white/90">
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

function EmptyHint({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-2xl border border-white/[0.07] bg-white/[0.02] px-4 py-6 text-center text-sub text-[var(--text-muted)]">
      {children}
    </p>
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

function HistoryRow({ entry }: { entry: PlaybackLogEntry }) {
  const live = entry.ended_at === null;
  const device = [entry.client, entry.device_name].filter(Boolean).join(" · ");
  return (
    <div className="flex items-center gap-3 px-4 py-2.5 max-md:px-3.5">
      <span className="tnum w-11 shrink-0 text-caption text-white/40">
        {formatClockTime(entry.started_at)}
      </span>
      <PosterImage
        src={entry.media.poster_url ? imageUrl(entry.media.poster_url) : null}
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
  onShowAll,
}: {
  scope: MediaActivityScope;
  /** 按成员筛选；null = 全部 */
  memberId: number | null;
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
    return <EmptyHint>正在读取播放记录…</EmptyHint>;
  }
  if (entries.length === 0 && hiddenCount === 0) {
    return <EmptyHint>还没有播放记录；从现在起的每一场播放都会记在这里。</EmptyHint>;
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

// ---------------------------------------------------------------------------
// 观看统计
// ---------------------------------------------------------------------------

/** 汇总数字：不是图，一个数一句话（dataviz「hero number」形态）。 */
function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] px-4 py-3">
      <p className="text-caption text-white/45">{label}</p>
      <p className="tnum mt-1 text-[22px] font-bold leading-tight text-white">{value}</p>
    </div>
  );
}

/**
 * 每日播放场次：单一序列的细柱，贴基线、4px 圆角、柱间留 2px。
 * 只直接标最高的那一天，其余靠 hover 的 title 读数；首尾两天标日期。
 */
function DailyBars({ rows }: { rows: PlaybackWatchStats["by_day"] }) {
  const max = Math.max(1, ...rows.map((r) => r.plays));
  const peak = rows.reduce((best, r) => (r.plays > best.plays ? r : best), rows[0]);
  const dayLabel = (date: string) => {
    const [, month, day] = date.split("-");
    return `${Number(month)}月${Number(day)}日`;
  };
  return (
    <div>
      <div className="flex h-20 items-end gap-[2px]" role="img" aria-label="每日播放场次">
        {rows.map((row) => (
          <div
            key={row.date}
            title={`${dayLabel(row.date)} · ${row.plays} 场 · ${formatWatched(row.watched_ms)}`}
            className="group relative flex h-full flex-1 items-end"
          >
            <div
              className="w-full rounded-t-[4px] transition-opacity group-hover:opacity-80"
              style={{
                height: `${Math.max(row.plays > 0 ? 6 : 2, (row.plays / max) * 100)}%`,
                backgroundColor: row.plays > 0 ? "var(--info)" : "rgba(255,255,255,0.08)",
              }}
            />
            {row === peak && row.plays > 0 && (
              <span className="tnum absolute -top-4 left-1/2 -translate-x-1/2 text-[11px] font-medium text-white/70">
                {row.plays}
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-white/35">
        <span>{rows.length > 0 ? dayLabel(rows[0].date) : ""}</span>
        <span>{rows.length > 0 ? dayLabel(rows[rows.length - 1].date) : ""}</span>
      </div>
    </div>
  );
}

function MemberTable({ rows }: { rows: PlaybackWatchStats["by_member"] }) {
  if (rows.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded-2xl border border-white/[0.08] bg-white/[0.02]">
      <table className="w-full text-sub">
        <thead>
          <tr className="text-left text-caption text-white/40">
            <th className="px-4 py-2 font-medium">成员</th>
            <th className="tnum px-3 py-2 text-right font-medium">场次</th>
            <th className="tnum px-3 py-2 text-right font-medium">观看时长</th>
            <th className="tnum px-4 py-2 text-right font-medium">看完</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-white/[0.06]">
          {rows.map((row) => (
            <tr key={row.member_id} className="text-white/80">
              <td className="px-4 py-2">{row.member_name}</td>
              <td className="tnum px-3 py-2 text-right">{row.plays}</td>
              <td className="tnum px-3 py-2 text-right">{formatWatched(row.watched_ms)}</td>
              <td className="tnum px-4 py-2 text-right">{row.completed}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TitleRank({ stats, onShowAll }: { stats: PlaybackWatchStats; onShowAll: () => void }) {
  if (stats.top_titles.length === 0 && stats.hidden_title_count === 0) return null;
  return (
    <div className="divide-y divide-white/[0.06] rounded-2xl border border-white/[0.08] bg-white/[0.02]">
      {stats.top_titles.map((row, index) => (
        <div
          key={`${row.media.media_item_id}-${index}`}
          className="flex items-center gap-3 px-4 py-2 max-md:px-3.5"
        >
          <span className="tnum w-4 shrink-0 text-caption text-white/35">{index + 1}</span>
          <PosterImage
            src={row.media.poster_url ? imageUrl(row.media.poster_url) : null}
            alt={row.media.title}
            className="h-[42px] w-[28px] shrink-0 rounded-lg object-cover ring-1 ring-white/10"
          />
          <div className="min-w-0 flex-1">
            <TitleText media={row.media} />
          </div>
          <span className="tnum shrink-0 text-caption text-white/50">
            {row.plays} 场 · {formatWatched(row.watched_ms)}
          </span>
        </div>
      ))}
      <HiddenCountRow count={stats.hidden_title_count} noun="部作品" onShowAll={onShowAll} />
    </div>
  );
}

export function PlaybackStatsPanel({
  scope,
  days,
  onShowAll,
}: {
  scope: MediaActivityScope;
  days: number;
  onShowAll: () => void;
}) {
  const [stats, setStats] = useState<PlaybackWatchStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchPlaybackWatchStats(days, scope)
      .then((next) => {
        if (cancelled) return;
        setStats(next);
        setError(null);
      })
      .catch((caught) => {
        if (!cancelled) setError((caught as Error).message || "观看统计加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, [days, scope]);

  const tiles = useMemo(
    () =>
      stats
        ? [
            { label: "播放场次", value: String(stats.plays) },
            { label: "观看时长", value: formatWatched(stats.watched_ms) },
            { label: "看完", value: String(stats.completed) },
            { label: "活跃成员", value: String(stats.active_members) },
          ]
        : [],
    [stats],
  );

  if (error) {
    return (
      <p className="rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-6 text-amber-100">
        {error}
      </p>
    );
  }
  if (!stats) return <EmptyHint>正在读取观看统计…</EmptyHint>;
  if (stats.plays === 0) {
    return <EmptyHint>最近 {days} 天没有播放记录；从现在起的每一场播放都会计入。</EmptyHint>;
  }
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-2.5 max-md:grid-cols-2">
        {tiles.map((tile) => (
          <StatTile key={tile.label} label={tile.label} value={tile.value} />
        ))}
      </div>
      <div className="rounded-2xl border border-white/[0.08] bg-white/[0.02] px-4 pb-3 pt-6">
        <DailyBars rows={stats.by_day} />
      </div>
      <div className="grid grid-cols-2 gap-2.5 max-md:grid-cols-1">
        <div>
          <p className="mb-1.5 text-caption text-white/45">按成员</p>
          <MemberTable rows={stats.by_member} />
          {stats.by_client.length > 0 && (
            <p className="mt-2 text-caption leading-5 text-white/40">
              客户端：
              {stats.by_client.map((row) => `${row.client} ${row.plays} 场`).join(" · ")}
            </p>
          )}
        </div>
        <div>
          <p className="mb-1.5 text-caption text-white/45">看得最多</p>
          <TitleRank stats={stats} onShowAll={onShowAll} />
        </div>
      </div>
    </div>
  );
}
