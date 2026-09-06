"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { PosterImage } from "@/components/poster-image";
import { HiddenCountRow, TitleText, formatWatched } from "@/components/playback-stats-section";
import {
  fetchPlaybackWatchStats,
  type MediaActivityScope,
  type PlaybackStatsDayRow,
  type PlaybackWatchStats,
} from "@/lib/api/playback";
import { imageUrl } from "@/lib/image-proxy";

/**
 * 观看统计（docs/design/activity.md「观看统计」）。
 *
 * 骨架照仪表盘的通行做法：**指标卡 → 一张主图 → 一组同构的分解面板**，每一层都能
 * 点进下一层。指标卡带较上一周期的变化和迷你走势，点哪张主图就切到哪个指标；主图
 * 把当前周期与上一周期画在同一坐标系里；分解面板同一个模板，成员行可点即钻取；
 * 最后一张星期 × 小时的热力图回答「家里什么时候有人在看」——决定扫描、整理这类
 * 重活该排在什么时候。图全部是内联 SVG：只有一种图型，不值得引图表库，且能严格
 * 按现有设计令牌配色。
 */

type MetricKey = "watched_ms" | "plays" | "completion" | "active_members";

interface MetricDef {
  key: MetricKey;
  label: string;
  /** 从一天的数据里取值（看完率为 0~1 的比例） */
  ofDay: (day: PlaybackStatsDayRow) => number;
  /** 汇总值 */
  ofTotals: (t: PlaybackWatchStats["current"]) => number;
  /** 汇总值的展示 */
  format: (value: number) => string;
  /** 坐标轴刻度的紧凑展示 */
  axis: (value: number) => string;
  /** 变化用百分点而不是百分比（看完率本身就是比例） */
  deltaInPoints?: boolean;
}

const METRICS: readonly MetricDef[] = [
  {
    key: "watched_ms",
    label: "观看时长",
    ofDay: (d) => d.watched_ms,
    ofTotals: (t) => t.watched_ms,
    // 指标卡只有一行的位置，「45 小时 28 分钟」在窄屏会折成三行，改成「45.5 小时」
    format: (v) =>
      v >= 3_600_000
        ? `${(v / 3_600_000).toFixed(v >= 360_000_000 ? 0 : 1)} 小时`
        : formatWatched(v),
    axis: (v) => `${(v / 3_600_000).toFixed(v >= 36_000_000 ? 0 : 1)}h`,
  },
  {
    key: "plays",
    label: "播放场次",
    ofDay: (d) => d.plays,
    ofTotals: (t) => t.plays,
    format: (v) => `${v}`,
    axis: (v) => `${Math.round(v)}`,
  },
  {
    key: "completion",
    label: "看完率",
    ofDay: (d) => (d.plays > 0 ? d.completed / d.plays : 0),
    ofTotals: (t) => (t.plays > 0 ? t.completed / t.plays : 0),
    format: (v) => `${Math.round(v * 100)}%`,
    axis: (v) => `${Math.round(v * 100)}%`,
    deltaInPoints: true,
  },
  {
    key: "active_members",
    label: "活跃成员",
    ofDay: (d) => d.members,
    ofTotals: (t) => t.active_members,
    format: (v) => `${v}`,
    axis: (v) => `${Math.round(v)}`,
  },
] as const;

const SERIES_COLOR = "var(--info)";
const PREVIOUS_COLOR = "rgba(255,255,255,0.32)";
const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

function dayLabel(date: string): string {
  const [, month, day] = date.split("-");
  return `${Number(month)}月${Number(day)}日`;
}

/** 坐标轴的整齐上限：1 / 2 / 5 × 10^n 里第一个不小于最大值的。 */
function niceMax(max: number): number {
  if (max <= 0) return 1;
  const exponent = Math.floor(Math.log10(max));
  const base = Math.pow(10, exponent);
  for (const step of [1, 2, 5, 10]) {
    if (max <= step * base) return step * base;
  }
  return 10 * base;
}

/** 容器宽度：图按真实像素画，坐标轴文字才不会被拉伸。 */
function useElementWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

// ---------------------------------------------------------------------------
// 指标卡
// ---------------------------------------------------------------------------

function Sparkline({ values }: { values: number[] }) {
  const width = 88;
  const height = 26;
  const max = Math.max(1e-9, ...values);
  if (values.length < 2) return <svg width={width} height={height} aria-hidden="true" />;
  const step = width / (values.length - 1);
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - 2 - (v / max) * (height - 4)).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={width} height={height} aria-hidden="true" className="shrink-0">
      <polyline
        points={points}
        fill="none"
        stroke={SERIES_COLOR}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Delta({
  current,
  previous,
  available,
  inPoints,
}: {
  current: number;
  previous: number;
  available: boolean;
  inPoints?: boolean;
}) {
  if (!available) {
    return <span className="text-caption text-white/35">暂无上一周期数据</span>;
  }
  let text: string;
  let tone: string;
  if (inPoints) {
    const points = Math.round((current - previous) * 100);
    text = `${points > 0 ? "▲" : points < 0 ? "▼" : "—"} ${Math.abs(points)} 个百分点`;
    tone = points > 0 ? "text-[var(--ok)]" : points < 0 ? "text-[var(--danger)]" : "text-white/45";
  } else if (previous <= 0) {
    text = current > 0 ? "▲ 上一周期为 0" : "— 持平";
    tone = current > 0 ? "text-[var(--ok)]" : "text-white/45";
  } else {
    const ratio = (current - previous) / previous;
    const pct = Math.round(Math.abs(ratio) * 100);
    text = `${ratio > 0 ? "▲" : ratio < 0 ? "▼" : "—"} ${pct}%`;
    tone = ratio > 0 ? "text-[var(--ok)]" : ratio < 0 ? "text-[var(--danger)]" : "text-white/45";
  }
  return (
    <span className="text-caption">
      <span className={`tnum font-semibold ${tone}`}>{text}</span>
      <span className="ml-1 text-white/35">较上一周期</span>
    </span>
  );
}

function MetricCard({
  metric,
  stats,
  selected,
  onSelect,
}: {
  metric: MetricDef;
  stats: PlaybackWatchStats;
  selected: boolean;
  onSelect: () => void;
}) {
  const current = metric.ofTotals(stats.current);
  const previous = metric.ofTotals(stats.previous);
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={`rounded-2xl border px-4 py-3 text-left transition ${
        selected
          ? "border-[var(--info)]/50 bg-[var(--info)]/[0.08]"
          : "border-white/[0.08] bg-white/[0.03] hover:bg-white/[0.05]"
      }`}
    >
      <p className="text-caption text-white/45">{metric.label}</p>
      <div className="mt-1 flex items-end justify-between gap-2">
        <p className="tnum whitespace-nowrap text-[22px] font-bold leading-tight text-white">
          {metric.format(current)}
        </p>
        <Sparkline values={stats.by_day.map(metric.ofDay)} />
      </div>
      <div className="mt-1.5">
        <Delta
          current={current}
          previous={previous}
          available={stats.previous_available}
          inPoints={metric.deltaInPoints}
        />
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// 主图：当前周期实线 + 上一周期虚线，同一坐标系
// ---------------------------------------------------------------------------

const CHART_HEIGHT = 220;
const PAD = { top: 14, right: 12, bottom: 26, left: 46 };

function TrendChart({ stats, metric }: { stats: PlaybackWatchStats; metric: MetricDef }) {
  const [ref, width] = useElementWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const current = stats.by_day.map(metric.ofDay);
  const previous = stats.previous_by_day.map(metric.ofDay);
  const showPrevious = stats.previous_available;
  const count = current.length;
  const innerWidth = Math.max(0, width - PAD.left - PAD.right);
  const innerHeight = CHART_HEIGHT - PAD.top - PAD.bottom;
  const yMax = niceMax(Math.max(...current, ...(showPrevious ? previous : [0])));
  const x = (i: number) => PAD.left + (count > 1 ? (i / (count - 1)) * innerWidth : innerWidth / 2);
  const y = (v: number) => PAD.top + innerHeight - (v / yMax) * innerHeight;
  const path = (values: number[]) =>
    values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${path(current)} L${x(count - 1).toFixed(1)},${(PAD.top + innerHeight).toFixed(1)} L${PAD.left},${(PAD.top + innerHeight).toFixed(1)} Z`;
  const gridSteps = 4;
  // x 轴刻度：按宽度定个数（一个「8月12日」约 60px），落在整天上，窄屏就少画几个
  const tickTarget = Math.max(2, Math.min(6, Math.floor(innerWidth / 70)));
  const tickEvery = Math.max(1, Math.round(count / tickTarget));
  const ticks = stats.by_day.map((d, i) => i).filter((i) => i % tickEvery === 0 || i === count - 1);

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    if (innerWidth <= 0 || count === 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - rect.left - PAD.left;
    const index = Math.round((px / innerWidth) * (count - 1));
    setHover(Math.max(0, Math.min(count - 1, index)));
  };

  return (
    <div ref={ref} className="relative">
      {width > 0 && (
        <svg
          width={width}
          height={CHART_HEIGHT}
          role="img"
          aria-label={`${metric.label}走势`}
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        >
          {Array.from({ length: gridSteps + 1 }, (_, i) => {
            const v = (yMax / gridSteps) * i;
            const yy = y(v);
            return (
              <g key={i}>
                <line
                  x1={PAD.left}
                  x2={width - PAD.right}
                  y1={yy}
                  y2={yy}
                  stroke="rgba(255,255,255,0.07)"
                />
                <text
                  x={PAD.left - 8}
                  y={yy + 3.5}
                  textAnchor="end"
                  fontSize={10}
                  fill="rgba(255,255,255,0.4)"
                  className="tnum"
                >
                  {metric.axis(v)}
                </text>
              </g>
            );
          })}
          {ticks.map((i) => (
            <text
              key={i}
              x={x(i)}
              y={CHART_HEIGHT - 8}
              textAnchor={i === 0 ? "start" : i === count - 1 ? "end" : "middle"}
              fontSize={10}
              fill="rgba(255,255,255,0.4)"
            >
              {dayLabel(stats.by_day[i].date)}
            </text>
          ))}
          {showPrevious && (
            <path
              d={path(previous)}
              fill="none"
              stroke={PREVIOUS_COLOR}
              strokeWidth={1.5}
              strokeDasharray="4 4"
              strokeLinejoin="round"
            />
          )}
          <path d={area} fill={SERIES_COLOR} opacity={0.1} />
          <path
            d={path(current)}
            fill="none"
            stroke={SERIES_COLOR}
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
          {hover !== null && (
            <g>
              <line
                x1={x(hover)}
                x2={x(hover)}
                y1={PAD.top}
                y2={PAD.top + innerHeight}
                stroke="rgba(255,255,255,0.25)"
              />
              {showPrevious && (
                <circle cx={x(hover)} cy={y(previous[hover])} r={3.5} fill={PREVIOUS_COLOR} />
              )}
              <circle
                cx={x(hover)}
                cy={y(current[hover])}
                r={4.5}
                fill={SERIES_COLOR}
                stroke="rgba(10,12,18,0.9)"
                strokeWidth={2}
              />
            </g>
          )}
        </svg>
      )}
      {hover !== null && width > 0 && (
        <div
          className="menu-surface pointer-events-none absolute z-10 min-w-[10rem] px-3 py-2 text-caption"
          style={{
            left: Math.min(Math.max(x(hover) - 80, 0), width - 170),
            top: 0,
          }}
        >
          <p className="font-semibold text-white/85">{dayLabel(stats.by_day[hover].date)}</p>
          <p className="tnum mt-1 flex items-center gap-1.5 text-white/80">
            <span className="inline-block size-2 rounded-full" style={{ background: SERIES_COLOR }} />
            本周期 {metric.format(current[hover])}
          </p>
          {showPrevious && (
            <p className="tnum mt-0.5 flex items-center gap-1.5 text-white/55">
              <span
                className="inline-block h-0 w-2 border-t border-dashed"
                style={{ borderColor: PREVIOUS_COLOR }}
              />
              上一周期 {metric.format(previous[hover])}
              <span className="text-white/35">{dayLabel(stats.previous_by_day[hover].date)}</span>
            </p>
          )}
        </div>
      )}
      <div className="mt-1 flex items-center gap-4 text-caption text-white/45">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-4 rounded" style={{ background: SERIES_COLOR }} />
          本周期
        </span>
        {showPrevious && (
          <span className="flex items-center gap-1.5">
            <span
              className="inline-block h-0 w-4 border-t-2 border-dashed"
              style={{ borderColor: PREVIOUS_COLOR }}
            />
            上一周期
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 分解面板：同一个模板
// ---------------------------------------------------------------------------

interface BreakdownRow {
  key: string;
  label: React.ReactNode;
  value: number;
  valueLabel: string;
  secondary?: string;
  leading?: React.ReactNode;
  onSelect?: () => void;
  selected?: boolean;
}

function BreakdownPanel({
  title,
  note,
  rows,
  total,
  footer,
}: {
  title: string;
  note?: string;
  rows: BreakdownRow[];
  total: number;
  footer?: React.ReactNode;
}) {
  const max = Math.max(1e-9, ...rows.map((r) => r.value));
  return (
    <section aria-label={title}>
      <div className="mb-1.5 flex items-baseline gap-2">
        <p className="text-caption font-semibold text-white/55">{title}</p>
        {note && <p className="text-[11px] text-white/30">{note}</p>}
      </div>
      <div className="divide-y divide-white/[0.06] rounded-2xl border border-white/[0.08] bg-white/[0.02]">
        {rows.length === 0 && (
          <p className="px-4 py-3 text-caption text-white/40">本周期没有数据</p>
        )}
        {rows.map((row) => {
          const share = total > 0 ? Math.round((row.value / total) * 100) : 0;
          const body = (
            <>
              {row.leading}
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-3">
                  <div className="min-w-0 text-ui font-medium text-white/85">{row.label}</div>
                  <div className="tnum shrink-0 text-caption text-white/70">
                    {row.valueLabel}
                    <span className="ml-1.5 text-white/35">{share}%</span>
                  </div>
                </div>
                <div className="mt-1.5 h-1 rounded-full bg-white/[0.06]">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max(2, (row.value / max) * 100)}%`,
                      background: row.selected ? "var(--ok)" : SERIES_COLOR,
                    }}
                  />
                </div>
                {row.secondary && (
                  <p className="mt-1 text-[11px] text-white/35">{row.secondary}</p>
                )}
              </div>
            </>
          );
          const className = "flex items-center gap-3 px-4 py-2.5 max-md:px-3.5";
          return row.onSelect ? (
            <button
              key={row.key}
              type="button"
              aria-pressed={row.selected}
              onClick={row.onSelect}
              className={`${className} w-full text-left transition hover:bg-white/[0.04]`}
            >
              {body}
            </button>
          ) : (
            <div key={row.key} className={className}>
              {body}
            </div>
          );
        })}
        {footer}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// 时段热力图：星期 × 小时
// ---------------------------------------------------------------------------

function HourHeatmap({ matrix }: { matrix: number[][] }) {
  const max = Math.max(1e-9, ...matrix.flat());
  const total = matrix.flat().reduce((a, b) => a + b, 0);
  return (
    <section aria-label="观看时段">
      <div className="mb-1.5 flex items-baseline gap-2">
        <p className="text-caption font-semibold text-white/55">观看时段</p>
        <p className="text-[11px] text-white/30">星期 × 小时的观看时长，越深越多</p>
      </div>
      <div className="overflow-x-auto rounded-2xl border border-white/[0.08] bg-white/[0.02] px-4 py-3">
        {total === 0 ? (
          <p className="text-caption text-white/40">本周期没有数据</p>
        ) : (
          <div className="min-w-[520px]">
            <div className="ml-6 grid grid-cols-24 gap-[2px] text-[10px] text-white/35">
              {Array.from({ length: 24 }, (_, h) => (
                <span key={h} className="tnum text-center">
                  {h % 6 === 0 ? h : ""}
                </span>
              ))}
            </div>
            {matrix.map((row, day) => (
              <div key={day} className="mt-[2px] flex items-center gap-[2px]">
                <span className="w-6 shrink-0 text-[10px] text-white/40">{WEEKDAYS[day]}</span>
                <div className="grid flex-1 grid-cols-24 gap-[2px]">
                  {row.map((v, h) => (
                    <div
                      key={h}
                      title={`周${WEEKDAYS[day]} ${String(h).padStart(2, "0")}:00 · ${formatWatched(v)}`}
                      className="h-4 rounded-[3px]"
                      style={{
                        background:
                          v > 0
                            ? `rgba(127,176,255,${(0.18 + (v / max) * 0.82).toFixed(2)})`
                            : "rgba(255,255,255,0.04)",
                      }}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// 面板
// ---------------------------------------------------------------------------

export function WatchStatsPanel({
  scope,
  days,
  memberId,
  onMemberSelect,
  onShowAll,
}: {
  scope: MediaActivityScope;
  days: number;
  /** 钻取到某个成员；null = 全部 */
  memberId: number | null;
  onMemberSelect: (memberId: number | null) => void;
  onShowAll: () => void;
}) {
  const [stats, setStats] = useState<PlaybackWatchStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metricKey, setMetricKey] = useState<MetricKey>("watched_ms");
  const metric = useMemo(() => METRICS.find((m) => m.key === metricKey) ?? METRICS[0], [metricKey]);

  useEffect(() => {
    let cancelled = false;
    void fetchPlaybackWatchStats(days, scope, memberId)
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
  }, [days, scope, memberId]);

  if (error) {
    return (
      <p className="rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-6 text-amber-100">
        {error}
      </p>
    );
  }
  if (!stats) {
    return (
      <p className="rounded-2xl border border-white/[0.07] bg-white/[0.02] px-4 py-6 text-center text-sub text-[var(--text-muted)]">
        正在读取观看统计…
      </p>
    );
  }
  if (stats.current.plays === 0 && !stats.previous_available) {
    return (
      <p className="rounded-2xl border border-white/[0.07] bg-white/[0.02] px-4 py-6 text-center text-sub text-[var(--text-muted)]">
        最近 {days} 天没有播放记录；从现在起的每一场播放都会计入。
      </p>
    );
  }

  const totalWatched = stats.current.watched_ms;
  const tierTotal = stats.by_tier.reduce((sum, r) => sum + r.plays, 0);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-4 gap-2.5 max-md:grid-cols-2">
        {METRICS.map((m) => (
          <MetricCard
            key={m.key}
            metric={m}
            stats={stats}
            selected={m.key === metricKey}
            onSelect={() => setMetricKey(m.key)}
          />
        ))}
      </div>

      <div className="rounded-2xl border border-white/[0.08] bg-white/[0.02] px-3 pb-3 pt-3">
        <p className="mb-1 px-1 text-caption font-semibold text-white/55">
          {metric.label}
          <span className="ml-1.5 font-normal text-white/35">按天</span>
        </p>
        <TrendChart stats={stats} metric={metric} />
      </div>

      <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
        <BreakdownPanel
          title="按成员"
          note={memberId != null ? "已钻取到一个成员，再点一次取消" : "点成员名钻取"}
          total={totalWatched}
          rows={stats.by_member.map((row) => ({
            key: String(row.member_id),
            label: row.member_name,
            value: row.watched_ms,
            valueLabel: formatWatched(row.watched_ms),
            secondary: `${row.plays} 场 · 看完 ${row.completed}`,
            selected: memberId === row.member_id,
            onSelect: () => onMemberSelect(memberId === row.member_id ? null : row.member_id),
          }))}
        />
        <BreakdownPanel
          title="按客户端"
          total={totalWatched}
          rows={stats.by_client.map((row) => ({
            key: row.client,
            label: row.client,
            value: row.watched_ms,
            valueLabel: formatWatched(row.watched_ms),
            secondary: `${row.plays} 场`,
          }))}
        />
        <BreakdownPanel
          title="看得最多"
          total={totalWatched}
          rows={stats.top_titles.map((row, index) => ({
            key: `${row.media.media_item_id}-${index}`,
            label: <TitleText media={row.media} />,
            value: row.watched_ms,
            valueLabel: formatWatched(row.watched_ms),
            secondary: `${row.plays} 场`,
            leading: (
              <PosterImage
                src={row.media.poster_url ? imageUrl(row.media.poster_url) : null}
                alt={row.media.title}
                className="h-[42px] w-[28px] shrink-0 rounded-lg object-cover ring-1 ring-white/10"
              />
            ),
          }))}
          footer={
            <HiddenCountRow count={stats.hidden_title_count} noun="部作品" onShowAll={onShowAll} />
          }
        />
        <BreakdownPanel
          title="按播放方式"
          note="仅网页播放；Jellyfin 客户端恒为直连"
          total={tierTotal}
          rows={stats.by_tier.map((row) => ({
            key: String(row.tier),
            label: row.label,
            value: row.plays,
            valueLabel: `${row.plays} 场`,
          }))}
        />
      </div>

      <HourHeatmap matrix={stats.by_hour} />
    </div>
  );
}
