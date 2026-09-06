"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ActivityIcon, LockIcon, StarIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import {
  EmptyAction,
  EmptyState,
  HiddenCountRow,
  Skeleton,
  TitleText,
  formatWatched,
} from "@/components/playback-stats-section";
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
    // 两期都是 0，没什么可比的，不出标签
    if (current <= 0) return null;
    direction = 1;
    text = "新增";
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
      {width > 0 && current.every((v) => v <= 0) && (
        <p className="pointer-events-none absolute inset-x-0 top-[44px] text-center">
          <span className="rounded-full bg-[#0b0d13]/85 px-3 py-1 text-caption text-white/50 ring-1 ring-white/[0.08]">
            本周期没有播放{showPrevious ? "，虚线是上一周期" : ""}
          </span>
        </p>
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
  /** 「其他」这类合成行：字色与条色都压暗，不可点 */
  muted?: boolean;
}

/**
 * 分解面板：同一个模板（名称 · 值 · 占比条）。
 *
 * 行数**封顶**：成员三五个、客户端两三个、作品榜十个、播放方式最多五档，不封顶四块
 * 面板高矮参差。默认五行，多出来的按 fold 处理——`other` 把余下的合成一行「其他 N 个」
 * （占比仍合计 100%，高度钉死在六行内，Stripe / GA 的做法）；`expand` 留一个「展开
 * 全部」，作品不适合合并成「其他」，再长是用户自己点开的。面板撑满网格行高，同一行
 * 两块底边对齐，留白在卡片里而不是卡片外。
 */
function BreakdownPanel({
  title,
  note,
  rows,
  total,
  footer,
  emptyText = "本周期没有数据",
  unit,
  formatValue,
  fold = "other",
  limit = 5,
}: {
  title: string;
  note?: string;
  rows: BreakdownRow[];
  total: number;
  footer?: React.ReactNode;
  /** 没有行时的说明；传 null 表示由 footer（如范围外折叠行）自己解释 */
  emptyText?: string | null;
  /** 行的量词：「个成员」「部」，用在「其他 3 个成员」「展开全部 10 部」 */
  unit: string;
  /** 合成「其他」行时给值配文案 */
  formatValue: (value: number) => string;
  fold?: "other" | "expand";
  limit?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  let visible = rows;
  let tail: React.ReactNode = null;
  if (rows.length > limit) {
    if (fold === "expand") {
      visible = expanded ? rows : rows.slice(0, limit);
      tail = (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="w-full px-4 py-2.5 text-left text-caption font-medium text-[var(--info)] transition hover:text-white max-md:px-3.5"
        >
          {expanded ? "收起" : `展开全部 ${rows.length} ${unit}`}
        </button>
      );
    } else {
      const rest = rows.slice(limit);
      const value = rest.reduce((acc, r) => acc + r.value, 0);
      visible = [
        ...rows.slice(0, limit),
        {
          key: "__other",
          label: `其他 ${rest.length} ${unit}`,
          value,
          valueLabel: formatValue(value),
          muted: true,
        },
      ];
    }
  }
  return (
    <section aria-label={title} className="flex h-full flex-col">
      <div className="mb-1.5 flex items-baseline gap-2">
        <p className="text-caption font-semibold text-white/55">{title}</p>
        {note && <p className="text-[11px] text-white/30">{note}</p>}
      </div>
      <div className="flex flex-1 flex-col divide-y divide-white/[0.06] rounded-2xl border border-white/[0.08] bg-white/[0.02]">
        {rows.length === 0 && emptyText && (
          <p className="px-4 py-3 text-caption text-white/40">{emptyText}</p>
        )}
        {visible.map((row) => {
          const share = total > 0 ? Math.round((row.value / total) * 100) : 0;
          const body = (
            <>
              {row.leading}
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-3">
                  {/* 附注跟在名称后面而不是另起一行：四块面板的行高才一致，
                      同一行两块面板的高矮只由行数决定 */}
                  <div className="flex min-w-0 items-baseline gap-2">
                    <div
                      className={`min-w-0 text-ui font-medium ${row.muted ? "text-white/50" : "text-white/85"}`}
                    >
                      {row.label}
                    </div>
                    {row.secondary && (
                      <span className="tnum shrink-0 text-[11px] text-white/35">
                        {row.secondary}
                      </span>
                    )}
                  </div>
                  <div className="tnum shrink-0 text-caption text-white/70">
                    {row.valueLabel}
                    <span className="ml-1.5 text-white/35">{share}%</span>
                  </div>
                </div>
                {/* 条的长度就是份额：与右边的百分比、与总量三者自洽，几根条加起来
                    正好填满一行；不按第一名满格来缩放，那样条与数字会打架 */}
                <div className="mt-1.5 h-[3px] rounded-full bg-white/[0.06]">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max(1, share)}%`,
                      background: row.selected
                        ? "var(--ok)"
                        : row.muted
                          ? "rgba(255,255,255,0.22)"
                          : SERIES_COLOR,
                      opacity: row.selected || row.muted ? 1 : 0.9,
                    }}
                  />
                </div>
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
        {tail}
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
        {/* 没数据也把 7×24 的格子画出来：空格子本身就在说明这里会是什么 */}
        {total === 0 && (
          <p className="mb-2 text-caption text-white/40">
            本周期还没有累计到观看时长；有人看过之后这里会显示星期 × 小时的分布
          </p>
        )}
        {
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
        }
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// 最受欢迎：Netflix「TOP 10」式的前三，整页唯一允许「隆重」的地方
// ---------------------------------------------------------------------------

/** 与上一周期前三的对照：同名次「蝉联」，换了名次「上期第 n」，上期不在榜「新上榜」。 */
function rankChange(
  row: PlaybackStatsTitleRow,
  rank: number,
  previous: PlaybackStatsTitleRow[],
): { text: string; tone: string } | null {
  if (previous.length === 0) return null;
  const was = previous.findIndex((p) => p.media.media_item_id === row.media.media_item_id);
  if (was === rank) return { text: "蝉联", tone: "bg-[var(--ok)]/15 text-[var(--ok)]" };
  if (was === -1) return { text: "新上榜", tone: "bg-[var(--info)]/15 text-[var(--info)]" };
  return {
    text: `${was > rank ? "▲" : "▼"} 上期第 ${was + 1}`,
    tone: was > rank ? "bg-[var(--ok)]/15 text-[var(--ok)]" : "bg-white/[0.08] text-white/55",
  };
}

const KIND_LABELS: Partial<Record<string, string>> = { movie: "电影", tv: "剧集" };

/**
 * 一个名次：巨大的描边数字压在海报左后方，海报盖住数字的右侧——Netflix 的 TOP 10
 * 就是这套语法，数字本身就是装饰，不再需要徽标。第一名的数字与海报都更大。
 */
function TopEntry({
  row,
  rank,
  hero,
  drilled,
  previous,
}: {
  row: PlaybackStatsTitleRow;
  rank: number;
  hero: boolean;
  drilled: boolean;
  previous: PlaybackStatsTitleRow[];
}) {
  const change = rankChange(row, rank, previous);
  const kind = KIND_LABELS[row.media.kind];
  const facts = [row.media.year, kind].filter(Boolean).join(" · ");
  const watched = formatWatched(row.watched_ms);
  const plays = `${row.plays} 场`;
  const audience = drilled ? null : `${row.members} 人看过`;
  // 文字栏窄：人数与场次并一行、时长单独一行，别让「· 7 场」孤零零折到下一行
  const meta = [[audience, plays].filter(Boolean).join(" · "), watched];
  return (
    <div className="flex min-w-0 items-end">
      {/* 数字盒子定宽（按 em）、靠左排，「1」「2」「3」宽窄不一也只让海报盖住右侧一角 */}
      <span
        aria-hidden="true"
        className={`relative inline-block shrink-0 select-none text-left font-black leading-[0.78] tracking-[-0.06em] text-transparent [-webkit-text-stroke:2px_rgba(255,255,255,0.62)] ${
          hero ? "-mr-[0.12em] w-[0.6em] text-[128px]" : "-mr-[0.12em] w-[0.6em] text-[96px]"
        }`}
      >
        {rank + 1}
      </span>
      <PosterImage
        src={row.media.poster_url ? imageUrl(row.media.poster_url, "poster-card") : null}
        alt={row.media.title}
        className={`relative z-10 shrink-0 rounded-lg object-cover shadow-[0_18px_40px_rgba(0,0,0,0.6)] ring-1 ring-white/15 ${
          hero ? "h-[180px] w-[120px]" : "h-[120px] w-[80px]"
        }`}
      />
      <div className="relative z-10 ml-3 min-w-0 flex-1 self-center">
        <p className="mb-1 text-[10px] font-bold tracking-[0.2em] text-white/40">
          {["冠军", "亚军", "季军"][rank]}
        </p>
        <TitleText media={row.media} episode={false} large={hero} />
        {facts && <p className="tnum mt-0.5 text-caption text-white/45">{facts}</p>}
        {meta.map((line) => (
          <p key={line} className="tnum mt-1 text-caption leading-5 text-white/60">
            {line}
          </p>
        ))}
        {change && (
          <p className="mt-2">
            <span
              className={`tnum rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${change.tone}`}
            >
              {change.text}
            </span>
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * 本期最受欢迎前三：看过的成员最多，并列取时长长的。与「看得最多」（按时长）是
 * 两个问题，同一部片同时占两头也是信息——既有人看又看得久。背景是第一名海报
 * 放大模糊后的环境光，整块随作品换色；钻取到单个成员时人数没有意义，退成
 * 「本期看得最多」。
 */
function FavoritePodium({
  favorites,
  previous,
  memberId,
  hiddenCount,
  onShowAll,
}: {
  favorites: PlaybackStatsTitleRow[];
  previous: PlaybackStatsTitleRow[];
  memberId: number | null;
  /** 作品榜里不在浏览范围内的条数：前三全被折掉时要说明去向 */
  hiddenCount: number;
  onShowAll: () => void;
}) {
  if (favorites.length === 0) {
    // 有播放却没有上榜作品：要么都在范围外，要么条目已被删除；说清去向，不留空白
    return (
      <div className="rounded-2xl border border-white/[0.08] bg-white/[0.02]">
        <EmptyState
          compact
          icon={
            hiddenCount > 0 ? <LockIcon className="size-3.5" /> : <StarIcon className="size-3.5" />
          }
          title={
            hiddenCount > 0 ? "本期最受欢迎的作品都在你的浏览范围外" : "本期还没有可以上榜的作品"
          }
          description={
            hiddenCount > 0
              ? `${hiddenCount} 部作品来自你设为不可见的库`
              : "播放过的条目已被删除，不再进榜"
          }
          actions={
            hiddenCount > 0 ? <EmptyAction onClick={onShowAll}>显示全部</EmptyAction> : undefined
          }
        />
      </div>
    );
  }
  const drilled = memberId != null;
  const [first, ...rest] = favorites;
  const ambient = first.media.poster_url ? imageUrl(first.media.poster_url, "poster-card") : null;
  // 不足三部时列数跟着少，别留空位
  const columns = ["1.5fr", ...rest.map(() => "1fr")].join(" ");
  return (
    <section
      aria-label={drilled ? "本期看得最多" : "本期最受欢迎"}
      className="relative overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.02]"
    >
      {ambient && (
        <div aria-hidden="true" className="pointer-events-none absolute inset-0">
          <PosterImage
            src={ambient}
            alt=""
            className="h-full w-full scale-150 object-cover opacity-45 blur-3xl saturate-150"
          />
          <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(11,13,19,0.88),rgba(11,13,19,0.72)_45%,rgba(11,13,19,0.9))]" />
        </div>
      )}
      <div className="relative p-4 md:p-5">
        <div className="flex items-center gap-2.5">
          <span className="rounded-[3px] bg-[var(--info)] px-1.5 py-[3px] text-[10px] font-black leading-none tracking-[0.12em] text-[#0b0d13]">
            TOP 3
          </span>
          <p className="text-ui font-semibold text-white/90">
            {drilled ? "本期看得最多" : "本期最受欢迎"}
          </p>
          <p className="text-caption text-white/35">
            {drilled ? "按观看时长" : "按看过的人数，并列看时长"}
          </p>
        </div>
        <div
          className="mt-5 grid items-end gap-x-5 gap-y-6 md:[grid-template-columns:var(--podium-cols)]"
          style={{ "--podium-cols": columns } as React.CSSProperties}
        >
          <TopEntry row={first} rank={0} hero drilled={drilled} previous={previous} />
          {rest.map((row, i) => (
            <TopEntry
              key={row.media.media_item_id}
              row={row}
              rank={i + 1}
              hero={false}
              drilled={drilled}
              previous={previous}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// 面板
// ---------------------------------------------------------------------------

/** 读取中的骨架：与真实布局同形（指标卡、主图、TOP 3、四块分解），内容到达时不跳动。 */
function StatsSkeleton() {
  return (
    <div aria-busy="true" aria-label="正在读取观看统计" className="space-y-4">
      <div className="grid grid-cols-4 gap-2.5 max-md:grid-cols-2">
        {Array.from({ length: 4 }, (_, i) => (
          <div
            key={i}
            className="rounded-2xl border border-white/[0.08] bg-white/[0.03] px-4 py-3.5"
          >
            <Skeleton className="h-3 w-14" />
            <Skeleton className="mt-3 h-7 w-24" />
            <Skeleton className="mt-3 h-3 w-28" />
          </div>
        ))}
      </div>
      <div className="rounded-2xl border border-white/[0.08] bg-white/[0.02] p-5">
        <Skeleton className="h-4 w-32" />
        <div className="mt-5 flex items-end gap-6 max-md:flex-col max-md:items-stretch">
          <Skeleton className="h-[180px] w-full md:w-[38%]" />
          <Skeleton className="h-[120px] w-full md:w-[26%]" />
          <Skeleton className="h-[120px] w-full md:w-[26%]" />
        </div>
      </div>
      <div className="rounded-2xl border border-white/[0.08] bg-white/[0.02] p-3">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="mt-3 h-[220px] w-full" />
      </div>
      <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-40 w-full rounded-2xl" />
        ))}
      </div>
    </div>
  );
}

export function WatchStatsPanel({
  scope,
  days,
  memberId,
  memberLabel,
  onMemberSelect,
  onDaysChange,
  onShowAll,
}: {
  scope: MediaActivityScope;
  days: number;
  /** 钻取到某个成员；null = 全部 */
  memberId: number | null;
  /** 钻取中成员的显示名，空状态里点名用 */
  memberLabel: string | null;
  onMemberSelect: (memberId: number | null) => void;
  onDaysChange: (days: number) => void;
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
    return <StatsSkeleton />;
  }

  // 空状态里能做的事：清掉成员钻取、把周期拉到最长——两者都是「换个口径再看」
  const widenActions = (
    <>
      {memberId != null && (
        <EmptyAction onClick={() => onMemberSelect(null)}>查看全部成员</EmptyAction>
      )}
      {days < 90 && <EmptyAction onClick={() => onDaysChange(90)}>看最近 90 天</EmptyAction>}
    </>
  );
  const who = memberId != null ? (memberLabel ?? "这位成员") : null;

  if (stats.current.plays === 0 && !stats.previous_available) {
    // 两个周期都没有日志：整块用一个空状态承接，不摆一排 0
    return (
      <EmptyState
        icon={<ActivityIcon className="size-5" />}
        title={who ? `${who}最近 ${days} 天没有播放` : `最近 ${days} 天没有播放记录`}
        description="统计从播放日志来：从现在起每一场播放都会计入，看得越久这里越有得看。"
        actions={widenActions}
      />
    );
  }

  const totalWatched = stats.current.watched_ms;
  const tierTotal = stats.by_tier.reduce((sum, r) => sum + r.plays, 0);
  const cards = (
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
  );
  const chart = (
    <div className="rounded-2xl border border-white/[0.08] bg-white/[0.02] px-3 pb-3 pt-3">
      <TrendChart stats={stats} metric={metric} />
    </div>
  );

  if (stats.current.plays === 0) {
    // 本期一场都没有、上期有：指标卡与主图仍有对照价值，其余分解没有内容，
    // 用一个空状态代替五块各自写「没有数据」
    return (
      <div className="space-y-4">
        {cards}
        {chart}
        <EmptyState
          icon={<ActivityIcon className="size-5" />}
          title={who ? `${who}本周期没有播放` : "本周期没有播放"}
          description={`上一周期有 ${stats.previous.plays} 场；本周期一场都没有，没有可以分解的数据。`}
          actions={widenActions}
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {cards}

      {/* TOP 3 紧跟指标卡：先给结论（谁在被看），走势与分解在后 */}
      <FavoritePodium
        favorites={stats.favorites}
        previous={stats.previous_favorites}
        memberId={memberId}
        hiddenCount={stats.hidden_title_count}
        onShowAll={onShowAll}
      />

      {chart}

      <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
        <BreakdownPanel
          title="按成员"
          note={memberId != null ? "已钻取到一个成员，再点一次取消" : "点成员名钻取"}
          total={totalWatched}
          unit="个成员"
          formatValue={formatWatched}
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
          unit="个客户端"
          formatValue={formatWatched}
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
          unit="部"
          formatValue={formatWatched}
          fold="expand"
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
                className="h-9 w-6 shrink-0 rounded-md object-cover ring-1 ring-white/10"
              />
            ),
          }))}
          footer={
            <HiddenCountRow count={stats.hidden_title_count} noun="部作品" onShowAll={onShowAll} />
          }
          emptyText={stats.hidden_title_count > 0 ? null : "播放过的条目已被删除"}
        />
        <BreakdownPanel
          title="按播放方式"
          note="仅网页播放；Jellyfin 客户端恒为直连"
          total={tierTotal}
          unit="种"
          formatValue={(v) => `${v} 场`}
          emptyText="本周期没有网页播放；Jellyfin 客户端不经过转码，不在这里分解"
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
