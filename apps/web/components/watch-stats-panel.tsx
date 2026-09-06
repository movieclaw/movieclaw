"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { PosterImage } from "@/components/poster-image";
import { HiddenCountRow, TitleText, formatWatched } from "@/components/playback-stats-section";
import {
  fetchPlaybackWatchStats,
  type MediaActivityScope,
  type PlaybackStatsDayRow,
  type PlaybackStatsTitleRow,
  type PlaybackWatchStats,
} from "@/lib/api/playback";
import { imageUrl } from "@/lib/image-proxy";

/**
 * 观看统计（docs/design/activity.md「观看统计」）。
 *
 * 骨架照仪表盘的通行做法：**指标卡 → 一张主图 → 一组同构的分解面板**，每一层都能
 * 点进下一层。指标卡只有数字与较上一周期的变化——走势交给主图画，卡片里再画一遍
 * 迷你图是没有标注的重复信息，容易误读；点哪张主图就切到哪个指标。主图
 * 把当前周期与上一周期画在同一坐标系里；分解面板同一个模板，成员行可点即钻取；
 * 最后一张星期 × 小时的热力图回答「家里什么时候有人在看」——决定扫描、整理这类
 * 重活该排在什么时候。图全部是内联 SVG：只有一种图型，不值得引图表库，且能严格
 * 按现有设计令牌配色。
 */

type MetricKey = "watched_ms" | "plays" | "completion" | "active_members";

interface MetricDef {
  key: MetricKey;
  label: string;
  /**
   * 从一段连续日期的数据里取值（看完率为 0~1 的比例）。传一天就是当天的值，
   * 传一周就是这周的值——主图在柱子摆不下时会按周折桶。
   */
  ofDays: (rows: PlaybackStatsDayRow[]) => number;
  /** 汇总值 */
  ofTotals: (t: PlaybackWatchStats["current"]) => number;
  /** 汇总值的展示 */
  format: (value: number) => string;
  /** 指标卡上的大数字：[数值, 单位]，单位单独排小字 */
  parts: (value: number) => [string, string];
  /** 坐标轴刻度的紧凑展示 */
  axis: (value: number) => string;
  /** 变化用百分点而不是百分比（看完率本身就是比例） */
  deltaInPoints?: boolean;
  /** 折桶后的值不是求和而是日均，提示里要说清 */
  bucketNote?: string;
}

const sum = (rows: PlaybackStatsDayRow[], pick: (d: PlaybackStatsDayRow) => number) =>
  rows.reduce((acc, d) => acc + pick(d), 0);

/** 小时数：一位小数，过百取整。 */
const hours = (ms: number) => (ms / 3_600_000).toFixed(ms >= 360_000_000 ? 0 : 1);

const METRICS: readonly MetricDef[] = [
  {
    key: "watched_ms",
    label: "观看时长",
    ofDays: (rows) => sum(rows, (d) => d.watched_ms),
    ofTotals: (t) => t.watched_ms,
    // 「45 小时 28 分钟」在指标卡上占不下一行，满一小时就按「45.5 小时」
    format: (v) => (v >= 3_600_000 ? `${hours(v)} 小时` : formatWatched(v)),
    parts: (v) =>
      v >= 3_600_000
        ? [hours(v), "小时"]
        : [`${Math.max(v > 0 ? 1 : 0, Math.round(v / 60_000))}`, "分钟"],
    axis: (v) => `${(v / 3_600_000).toFixed(v >= 36_000_000 ? 0 : 1)}h`,
  },
  {
    key: "plays",
    label: "播放场次",
    ofDays: (rows) => sum(rows, (d) => d.plays),
    ofTotals: (t) => t.plays,
    format: (v) => `${v} 场`,
    parts: (v) => [`${v}`, "场"],
    axis: (v) => `${Math.round(v)}`,
  },
  {
    key: "completion",
    label: "看完率",
    ofDays: (rows) => {
      const plays = sum(rows, (d) => d.plays);
      return plays > 0 ? sum(rows, (d) => d.completed) / plays : 0;
    },
    ofTotals: (t) => (t.plays > 0 ? t.completed / t.plays : 0),
    format: (v) => `${Math.round(v * 100)}%`,
    parts: (v) => [`${Math.round(v * 100)}`, "%"],
    axis: (v) => `${Math.round(v * 100)}%`,
    deltaInPoints: true,
  },
  {
    key: "active_members",
    label: "活跃成员",
    // 按天是当天的去重人数；折成一周没法从日数据里去重，退而取日均
    ofDays: (rows) => (rows.length > 0 ? sum(rows, (d) => d.members) / rows.length : 0),
    ofTotals: (t) => t.active_members,
    format: (v) => `${Math.round(v * 10) / 10} 人`,
    parts: (v) => [`${Math.round(v * 10) / 10}`, "人"],
    axis: (v) => `${Math.round(v)}`,
    bucketNote: "日均",
  },
] as const;

/**
 * 把按天的序列折成若干桶：从末尾往前每 size 天一桶，最新的桶一定是满的，
 * 首桶可能不满（提示里会标出天数）。size=1 即不折。
 */
function bucketize(rows: PlaybackStatsDayRow[], size: number): PlaybackStatsDayRow[][] {
  const out: PlaybackStatsDayRow[][] = [];
  for (let end = rows.length; end > 0; end -= size) {
    out.unshift(rows.slice(Math.max(0, end - size), end));
  }
  return out;
}

function bucketLabel(rows: PlaybackStatsDayRow[]): string {
  if (rows.length === 1) return dayLabel(rows[0].date);
  return `${dayLabel(rows[0].date)} – ${dayLabel(rows[rows.length - 1].date)}`;
}

/** 顶部圆角的柱子：圆角只在数据端，底边贴着基线。 */
function barPath(x: number, y: number, w: number, h: number): string {
  const r = Math.min(3, w / 2, h);
  if (h <= 0) return "";
  return [
    `M${x.toFixed(1)},${(y + h).toFixed(1)}`,
    `V${(y + r).toFixed(1)}`,
    `Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)}`,
    `H${(x + w - r).toFixed(1)}`,
    `Q${(x + w).toFixed(1)},${y.toFixed(1)} ${(x + w).toFixed(1)},${(y + r).toFixed(1)}`,
    `V${(y + h).toFixed(1)} Z`,
  ].join(" ");
}

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

/** 较上一周期的变化，做成一枚小标签：涨绿、跌红、持平灰；没有上期就不出。 */
function DeltaChip({
  current,
  previous,
  available,
  inPoints,
  title,
}: {
  current: number;
  previous: number;
  available: boolean;
  inPoints?: boolean;
  title: string;
}) {
  if (!available) return null;
  let text: string;
  let direction: 1 | 0 | -1;
  if (inPoints) {
    const points = Math.round((current - previous) * 100);
    direction = points > 0 ? 1 : points < 0 ? -1 : 0;
    text = `${Math.abs(points)} 个百分点`;
  } else if (previous <= 0) {
    direction = current > 0 ? 1 : 0;
    text = current > 0 ? "新增" : "持平";
  } else {
    const ratio = (current - previous) / previous;
    direction = ratio > 0 ? 1 : ratio < 0 ? -1 : 0;
    text = direction === 0 ? "持平" : `${Math.round(Math.abs(ratio) * 100)}%`;
  }
  const tone =
    direction > 0
      ? "bg-[var(--ok)]/15 text-[var(--ok)]"
      : direction < 0
        ? "bg-[var(--danger)]/15 text-[var(--danger)]"
        : "bg-white/[0.08] text-white/55";
  const arrow = direction > 0 ? "▲" : direction < 0 ? "▼" : "";
  return (
    <span
      title={title}
      className={`tnum inline-flex shrink-0 items-center gap-0.5 rounded-md px-1.5 py-0.5 text-[11px] font-semibold leading-4 ${tone}`}
    >
      {arrow && <span className="text-[9px]">{arrow}</span>}
      {text}
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
  const [value, unit] = metric.parts(current);
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={`rounded-2xl border px-4 py-3.5 text-left transition max-md:px-3.5 ${
        selected
          ? "border-[var(--info)]/60 bg-[var(--info)]/[0.07]"
          : "border-white/[0.08] bg-white/[0.03] hover:border-white/[0.14] hover:bg-white/[0.05]"
      }`}
    >
      <p className="truncate text-caption font-medium text-white/55">{metric.label}</p>
      <p className="mt-2 whitespace-nowrap leading-none">
        <span className="tnum text-[26px] font-bold tracking-tight text-white">{value}</span>
        <span className="ml-1 text-sub font-medium text-white/45">{unit}</span>
      </p>
      <div className="mt-2.5 flex min-w-0 items-center gap-2">
        <DeltaChip
          current={current}
          previous={previous}
          available={stats.previous_available}
          inPoints={metric.deltaInPoints}
          title="较上一周期"
        />
        <span className="tnum truncate text-[11px] text-white/35">
          {stats.previous_available ? `上期 ${metric.format(previous)}` : "暂无上一周期数据"}
        </span>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// 主图：当前周期的柱子 + 上一周期的虚线，同一坐标系
// ---------------------------------------------------------------------------

const CHART_HEIGHT = 220;
const PAD = { top: 14, right: 12, bottom: 26, left: 46 };
/** 一根柱子（含间隙）至少占这么宽，摆不下就按周折桶 */
const MIN_SLOT = 6;

function TrendChart({ stats, metric }: { stats: PlaybackWatchStats; metric: MetricDef }) {
  const [ref, width] = useElementWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const innerWidth = Math.max(0, width - PAD.left - PAD.right);
  const innerHeight = CHART_HEIGHT - PAD.top - PAD.bottom;
  // 按天合计是离散量，柱子比折线诚实：零值日就是空位，有观看的日子一眼可数。
  // 90 天在手机上每根不到 4px，这时按周折桶（最新一桶一定是完整的一周）。
  const weekly = stats.by_day.length > 0 && innerWidth / stats.by_day.length < MIN_SLOT;
  const buckets = bucketize(stats.by_day, weekly ? 7 : 1);
  const prevBuckets = bucketize(stats.previous_by_day, weekly ? 7 : 1);
  const current = buckets.map(metric.ofDays);
  const previous = prevBuckets.map(metric.ofDays);
  const showPrevious = stats.previous_available && previous.length === current.length;
  const count = current.length;
  const yMax = niceMax(Math.max(...current, ...(showPrevious ? previous : [0])));
  const slot = count > 0 ? innerWidth / count : 0;
  const gap = Math.min(6, Math.max(1, slot * 0.3));
  const barWidth = Math.max(1, slot - gap);
  const cx = (i: number) => PAD.left + slot * i + slot / 2;
  const y = (v: number) => PAD.top + innerHeight - (v / yMax) * innerHeight;
  const baseline = PAD.top + innerHeight;
  const previousLine = previous
    .map((v, i) => `${i === 0 ? "M" : "L"}${cx(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
  const gridSteps = 4;
  // x 轴刻度：按宽度定个数（一个「8月12日」约 60px），落在柱子中心
  const tickTarget = Math.max(2, Math.min(6, Math.floor(innerWidth / 70)));
  const tickEvery = Math.max(1, Math.round(count / tickTarget));
  const ticks = buckets.map((_, i) => i).filter((i) => i % tickEvery === 0 || i === count - 1);
  const bucketNote = weekly && metric.bucketNote ? `${metric.bucketNote} ` : "";

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    if (slot <= 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - rect.left - PAD.left;
    setHover(Math.max(0, Math.min(count - 1, Math.floor(px / slot))));
  };

  return (
    <div ref={ref} className="relative">
      <p className="mb-1 px-1 text-caption font-semibold text-white/55">
        {metric.label}
        <span className="ml-1.5 font-normal text-white/35">{weekly ? "按周" : "按天"}</span>
      </p>
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
                  stroke={i === 0 ? "rgba(255,255,255,0.16)" : "rgba(255,255,255,0.07)"}
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
              x={cx(i)}
              y={CHART_HEIGHT - 8}
              textAnchor={i === 0 ? "start" : i === count - 1 ? "end" : "middle"}
              fontSize={10}
              fill="rgba(255,255,255,0.4)"
            >
              {dayLabel(buckets[i][0].date)}
            </text>
          ))}
          {current.map((v, i) =>
            v > 0 ? (
              <path
                key={i}
                d={barPath(cx(i) - barWidth / 2, y(v), barWidth, baseline - y(v))}
                fill={SERIES_COLOR}
                opacity={hover === null || hover === i ? 0.95 : 0.55}
              />
            ) : null,
          )}
          {showPrevious && (
            <path
              d={previousLine}
              fill="none"
              stroke={PREVIOUS_COLOR}
              strokeWidth={1.5}
              strokeDasharray="4 4"
              strokeLinejoin="round"
            />
          )}
          {hover !== null && (
            <g>
              <rect
                x={cx(hover) - slot / 2}
                y={PAD.top}
                width={slot}
                height={innerHeight}
                fill="rgba(255,255,255,0.05)"
              />
              {showPrevious && (
                <circle
                  cx={cx(hover)}
                  cy={y(previous[hover])}
                  r={3.5}
                  fill={PREVIOUS_COLOR}
                  stroke="rgba(10,12,18,0.9)"
                  strokeWidth={1.5}
                />
              )}
            </g>
          )}
        </svg>
      )}
      {hover !== null && width > 0 && (
        // menu-surface 自带 position: relative，定位交给外层
        <div
          className="pointer-events-none absolute z-10"
          style={{
            left: Math.min(Math.max(cx(hover) - 80, 0), width - 170),
            top: 24,
          }}
        >
          <div className="menu-surface min-w-[10rem] px-3 py-2 text-caption">
            <p className="font-semibold text-white/85">
              {bucketLabel(buckets[hover])}
              {weekly && buckets[hover].length < 7 && (
                <span className="ml-1 font-normal text-white/35">{buckets[hover].length} 天</span>
              )}
            </p>
            <p className="tnum mt-1 flex items-center gap-1.5 text-white/80">
              <span
                className="inline-block size-2 rounded-[2px]"
                style={{ background: SERIES_COLOR }}
              />
              本周期 {bucketNote}
              {metric.format(current[hover])}
            </p>
            {showPrevious && (
              <p className="tnum mt-0.5 flex items-center gap-1.5 text-white/55">
                <span
                  className="inline-block h-0 w-2 border-t border-dashed"
                  style={{ borderColor: PREVIOUS_COLOR }}
                />
                上一周期 {bucketNote}
                {metric.format(previous[hover])}
                <span className="text-white/35">{bucketLabel(prevBuckets[hover])}</span>
              </p>
            )}
          </div>
        </div>
      )}
      <div className="mt-1 flex items-center gap-4 text-caption text-white/45">
        <span className="flex items-center gap-1.5">
          <span
            className="inline-block size-2.5 rounded-[3px]"
            style={{ background: SERIES_COLOR }}
          />
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
                {row.secondary && <p className="mt-1 text-[11px] text-white/35">{row.secondary}</p>}
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
// 最受欢迎：整页唯一的图像锚点
// ---------------------------------------------------------------------------

/**
 * 本期最受欢迎的一部：看过的成员最多，并列取时长长的。与「看得最多」（按时长）是
 * 两个问题，同一部片同时占两头也是信息——既有人看又看得久。钻取到单个成员时人数
 * 没有意义，退成「TA 本期看得最多」。右侧对照上一周期：同一部就是「蝉联」。
 */
function FavoriteStrip({
  favorite,
  previous,
  memberId,
}: {
  favorite: PlaybackStatsTitleRow | null;
  previous: PlaybackStatsTitleRow | null;
  memberId: number | null;
}) {
  if (!favorite) return null;
  const drilled = memberId != null;
  const eyebrow = drilled ? "本期看得最多" : "本期最受欢迎";
  const same = previous?.media.media_item_id === favorite.media.media_item_id;
  const meta = [
    drilled ? null : `${favorite.members} 位成员看过`,
    formatWatched(favorite.watched_ms),
    `${favorite.plays} 场`,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <section
      aria-label={eyebrow}
      className="flex items-center gap-4 rounded-2xl border border-white/[0.08] bg-white/[0.02] p-3 pr-4 max-md:gap-3"
    >
      <PosterImage
        src={favorite.media.poster_url ? imageUrl(favorite.media.poster_url) : null}
        alt={favorite.media.title}
        className="h-[84px] w-[56px] shrink-0 rounded-xl object-cover ring-1 ring-white/10"
      />
      <div className="min-w-0 flex-1">
        <p className="text-caption font-semibold text-white/55">{eyebrow}</p>
        <div className="mt-1">
          <TitleText media={favorite.media} episode={false} />
        </div>
        <p className="tnum mt-1 text-caption text-white/45">{meta}</p>
      </div>
      {previous && (
        <p className="shrink-0 text-right text-caption text-white/40 max-md:hidden">
          {same ? (
            <span className="rounded-md bg-[var(--ok)]/15 px-1.5 py-0.5 font-semibold text-[var(--ok)]">
              蝉联
            </span>
          ) : (
            <>
              上期
              <span className="ml-1 text-white/60">《{previous.media.title}》</span>
            </>
          )}
        </p>
      )}
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
        <TrendChart stats={stats} metric={metric} />
      </div>

      <FavoriteStrip
        favorite={stats.favorite}
        previous={stats.previous_favorite}
        memberId={memberId}
      />

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
