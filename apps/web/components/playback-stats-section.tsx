"use client";

import { useEffect, useMemo, useState } from "react";

import Link from "next/link";
import type { Route } from "next";

import { CheckIcon, HistoryIcon } from "@/components/icons";
import { OverflowText } from "@/components/overflow-text";
import { PosterImage } from "@/components/poster-image";
import {
  fetchPlaybackHistory,
  fetchPlaybackWatchStats,
  type MediaActivityScope,
  type MediaActivityTarget,
  type PlaybackHistory,
  type PlaybackLogEntry,
  type PlaybackWatchStats,
} from "@/lib/api/playback";
import { formatRuntimeMinutes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import { formatRelativeTime } from "@/lib/time";

/**
 * 活动页「观看统计」与「播放记录」（docs/design/activity.md「播放日志与统计」）。
 *
 * 数据来自 playback_log——每场播放一行，所以这里回答的是「最近谁在什么时候用
 * 什么看了多久」，与上面按成员×作品保留进度的「最近观看」是两个口径。
 * 不轮询：日志按场记，几秒一刷没有意义，切周期或口径时重拉一次即可。
 */

const PERIODS: readonly { days: number; label: string }[] = [
  { days: 7, label: "7 天" },
  { days: 30, label: "30 天" },
  { days: 90, label: "90 天" },
] as const;

const HISTORY_LIMIT = 20;

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

function PeriodSelect({
  value,
  onChange,
}: {
  value: number;
  onChange: (days: number) => void;
}) {
  return (
    <div
      role="group"
      aria-label="统计周期"
      className="flex shrink-0 rounded-full border border-white/10 bg-black/30 p-0.5"
    >
      {PERIODS.map((option) => (
        <button
          key={option.days}
          type="button"
          aria-pressed={value === option.days}
          onClick={() => onChange(option.days)}
          className={`rounded-full px-2.5 py-0.5 text-caption font-semibold transition ${
            value === option.days
              ? "bg-white/15 text-white shadow-sm"
              : "text-[var(--text-muted)] hover:text-white"
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

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

function TitleRank({ stats }: { stats: PlaybackWatchStats }) {
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
      {stats.hidden_title_count > 0 && (
        <p className="px-4 py-2 text-caption text-white/45 max-md:px-3.5">
          另有 {stats.hidden_title_count} 部作品不在你的可见范围内
        </p>
      )}
    </div>
  );
}

function HistoryRow({ entry }: { entry: PlaybackLogEntry }) {
  const live = entry.ended_at === null;
  const device = [entry.client, entry.device_name].filter(Boolean).join(" · ");
  return (
    <div className="flex items-center gap-3 px-4 py-2.5 max-md:px-3.5">
      <PosterImage
        src={entry.media.poster_url ? imageUrl(entry.media.poster_url) : null}
        alt={entry.media.title}
        className="h-[42px] w-[28px] shrink-0 rounded-lg object-cover ring-1 ring-white/10"
      />
      <div className="min-w-0 flex-1">
        <TitleText media={entry.media} />
        <p className="mt-0.5 truncate text-caption leading-5 text-white/40">
          {formatRelativeTime(entry.started_at)}
          <span className="mx-1.5 text-white/20">·</span>
          {entry.member_name}
          {device && (
            <>
              <span className="mx-1.5 text-white/20">·</span>
              {device}
            </>
          )}
        </p>
      </div>
      <div className="shrink-0 text-right text-caption">
        {live ? (
          <span className="text-[var(--ok)]">播放中</span>
        ) : entry.completed ? (
          <span className="inline-flex items-center gap-1 text-[var(--ok)]">
            <CheckIcon className="size-3" />
            看完
          </span>
        ) : (
          <span className="text-white/45">{formatWatched(entry.watched_ms)}</span>
        )}
      </div>
    </div>
  );
}

export function PlaybackStatsSection({ scope }: { scope: MediaActivityScope }) {
  const [days, setDays] = useState(30);
  const [stats, setStats] = useState<PlaybackWatchStats | null>(null);
  const [history, setHistory] = useState<PlaybackHistory | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [nextStats, nextHistory] = await Promise.all([
          fetchPlaybackWatchStats(days, scope),
          fetchPlaybackHistory({ limit: HISTORY_LIMIT, scope }),
        ]);
        if (cancelled) return;
        setStats(nextStats);
        setHistory(nextHistory);
        setError(null);
      } catch (caught) {
        if (!cancelled) setError((caught as Error).message || "观看统计加载失败");
      }
    })();
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

  const nothingYet =
    stats != null &&
    stats.plays === 0 &&
    history != null &&
    history.entries.length === 0 &&
    history.hidden_count === 0;

  return (
    <>
      <section className="mt-7" aria-label="观看统计">
        <div className="mb-3 flex items-center gap-2.5">
          <HistoryIcon className="size-4 text-white/40" />
          <h2 className="text-ui font-semibold text-white/65">观看统计</h2>
          <span aria-hidden="true" className="h-px min-w-8 flex-1 bg-white/[0.09]" />
          <PeriodSelect value={days} onChange={setDays} />
        </div>
        {error && (
          <p className="rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-6 text-amber-100">
            {error}
          </p>
        )}
        {nothingYet ? (
          <p className="rounded-2xl border border-white/[0.07] bg-white/[0.02] px-4 py-6 text-center text-sub text-[var(--text-muted)]">
            最近 {days} 天没有播放记录；从现在起的每一场播放都会记在这里。
          </p>
        ) : (
          stats && (
            <div className="space-y-3">
              <div className="grid grid-cols-4 gap-2.5 max-md:grid-cols-2">
                {tiles.map((tile) => (
                  <StatTile key={tile.label} label={tile.label} value={tile.value} />
                ))}
              </div>
              {stats.plays > 0 && (
                <div className="rounded-2xl border border-white/[0.08] bg-white/[0.02] px-4 pb-3 pt-6">
                  <DailyBars rows={stats.by_day} />
                </div>
              )}
              <div className="grid grid-cols-2 gap-2.5 max-md:grid-cols-1">
                <div>
                  <p className="mb-1.5 text-caption text-white/45">按成员</p>
                  <MemberTable rows={stats.by_member} />
                  {stats.by_client.length > 0 && (
                    <p className="mt-2 text-caption leading-5 text-white/40">
                      客户端：
                      {stats.by_client
                        .map((row) => `${row.client} ${row.plays} 场`)
                        .join(" · ")}
                    </p>
                  )}
                </div>
                <div>
                  <p className="mb-1.5 text-caption text-white/45">看得最多</p>
                  <TitleRank stats={stats} />
                </div>
              </div>
            </div>
          )
        )}
      </section>

      {history && (history.entries.length > 0 || history.hidden_count > 0) && (
        <section className="mt-7" aria-label="播放记录">
          <div className="mb-3 flex items-center gap-2.5">
            <HistoryIcon className="size-4 text-white/40" />
            <h2 className="text-ui font-semibold text-white/65">播放记录</h2>
            <span className="tnum text-caption text-white/30">
              {history.entries.length + history.hidden_count}
            </span>
            <span aria-hidden="true" className="h-px min-w-8 flex-1 bg-white/[0.09]" />
          </div>
          <div className="divide-y divide-white/[0.06] rounded-2xl border border-white/[0.08] bg-white/[0.02]">
            {history.entries.map((entry) => (
              <HistoryRow key={entry.id} entry={entry} />
            ))}
            {history.hidden_count > 0 && (
              <p className="px-4 py-2.5 text-caption text-white/45 max-md:px-3.5">
                另有 {history.hidden_count} 条记录不在你的可见范围内，切到「全部」可查看
              </p>
            )}
          </div>
        </section>
      )}
    </>
  );
}
