"use client";

import { useEffect, useState } from "react";

import type { PlaybackSession } from "@/lib/api/playback";
import type { EngineStats, PlaybackEngine } from "@/lib/player/engine";

/**
 * 诊断面板（docs/design/web-player.md §6.5，对标 YouTube 的 Stats for nerds）。
 *
 * 这一屏是**开源项目支持成本的直接节省**：用户报「放不出来 / 很卡」时，截这
 * 一张图，档位、源与目标编码、有没有走显卡、实时码率、掉帧、缓冲、会话 id、
 * 判定理由全在里面，一来一回就能定位，不用反复问「你的浏览器是什么版本」。
 */

const TIER_LABELS: Record<number, string> = {
  0: "档 0 · 原文件直出",
  1: "档 1 · 换壳（视频音频均直通）",
  2: "档 2 · 换壳 + 转音轨",
  3: "档 3 · 硬件转码",
  4: "档 4 · 软件转码",
};

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-6">
      <span className="shrink-0 text-white/45">{label}</span>
      <span className="tnum truncate text-right text-white/90">{value}</span>
    </div>
  );
}

/** 掉帧率高于这个比例就标红：能解但解不动，通常是该降一档了。 */
const DROP_ALERT_RATIO = 0.02;

export function DiagnosticsPanel({
  session,
  engine,
  onClose,
}: {
  session: PlaybackSession;
  engine: PlaybackEngine | null;
  onClose: () => void;
}) {
  const [stats, setStats] = useState<EngineStats | null>(null);

  useEffect(() => {
    if (!engine) return;
    // 1 秒一次：再快没有信息量（掉帧与码率本身就是秒级统计），只会白烧主线程
    const timer = window.setInterval(() => setStats(engine.stats()), 1000);
    setStats(engine.stats());
    return () => window.clearInterval(timer);
  }, [engine]);

  const { decision } = session;
  const dropRatio =
    stats?.droppedFrames != null && stats.totalFrames
      ? stats.droppedFrames / stats.totalFrames
      : 0;

  return (
    <div className="absolute right-4 top-16 z-20 w-[330px] max-w-[calc(100%-2rem)] rounded-xl border border-white/10 bg-black/80 p-4 text-[12px] backdrop-blur-md">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-[13px] font-semibold text-white">播放诊断</h3>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md px-2 py-0.5 text-white/50 transition-colors hover:bg-white/10 hover:text-white"
        >
          关闭
        </button>
      </div>

      <div className="space-y-1.5">
        <Row label="档位" value={TIER_LABELS[decision.tier ?? -1] ?? "未知"} />
        {decision.degraded_from !== null ? (
          <Row label="降档自" value={`档 ${decision.degraded_from}`} />
        ) : null}
        <Row
          label="视频"
          value={
            decision.video
              ? `${decision.video.action === "copy" ? "直通" : "转码"} · ${decision.video.codec ?? "未知"}` +
                (decision.video.height ? ` · ${decision.video.height}p` : "") +
                (decision.video.tone_map ? " · HDR 转 SDR" : "")
              : "—"
          }
        />
        <Row
          label="音频"
          value={
            decision.audio
              ? `${decision.audio.action === "copy" ? "直通" : "转码"} · ${decision.audio.codec ?? "未知"}` +
                (decision.audio.channels ? ` · ${decision.audio.channels} 声道` : "") +
                (decision.audio.downmix ? " · 已降混" : "")
              : "—"
          }
        />
        <Row
          label="硬件加速"
          value={session.hw_backend ?? (decision.tier === 4 ? "无（软件转码）" : "不适用")}
        />
        <Row label="传输" value={stats?.engine ?? "—"} />
        <Row
          label="实时码率"
          value={stats?.bitrate ? `${(stats.bitrate / 1_000_000).toFixed(1)} Mbps` : "—"}
        />
        <div className={dropRatio > DROP_ALERT_RATIO ? "text-red-300" : undefined}>
          <Row
            label="掉帧"
            value={
              stats?.droppedFrames != null
                ? `${stats.droppedFrames} / ${stats.totalFrames ?? "?"}`
                : "—"
            }
          />
        </div>
        <Row label="缓冲" value={stats ? `${stats.bufferedSeconds.toFixed(1)} 秒` : "—"} />
        <Row label="会话" value={session.session_id ?? "无（直出）"} />
      </div>

      <p className="mt-3 border-t border-white/10 pt-2 leading-relaxed text-white/55">
        {decision.reason}
      </p>
    </div>
  );
}
