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
      <span className="shrink-0 text-white/55">{label}</span>
      <span className="tnum truncate text-right text-white/95">{value}</span>
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
    <div
      /*
       * 外观照 YouTube 的 Stats for nerds：**一块半透明的黑，仅此而已**。
       *
       * 位置照它放在画面左上，但有两条约束是真机截图逼出来的：
       *
       * - **`top` 必须自己叠 `--safe-top`**。顶栏也叠了它，PWA（black-translucent
       *   + viewport-fit=cover）里状态栏把顶栏整体往下推 59px，面板不跟着让就
       *   正好压在片名那一行上。面板是半透明的，压上去不是"盖住"而是"透出来"，
       *   两组文字叠在一起谁也读不了——真机截图里就是这个样子。
       * - **不能长到盖住中央的播放键**。竖屏宽度不够，面板必然横跨屏幕中线，
       *   而播放/退进十秒那一簇就在画面正中；面板挡住它，用户要先关诊断才能
       *   暂停。所以竖屏限高到中线上方一档（`50% - top - 3rem`），超出滚动。
       *   宽屏不受这条约束：面板 320px 靠左，与居中的按钮簇在横向上本就错开，
       *   那里只留一个防撑破的兜底限高。
       *
       * 限高一律配 `overflow-y-auto`：判定理由那段长度不可控（多轨、降档、
       * HDR 处置都会往里加句子），撑破了会一路盖到控制条上。
       *
       * 刻意不用站内浮层的 .menu-surface（磨砂玻璃 + 高光描边 + 大投影）：
       * 那套语言是给「压在内容上、等你操作完就走」的菜单用的，而这块面板会
       * 挂在画面角上几十分钟，越素越好。圆角保留——播放器里不该出现方角牌子。
       *
       * 顺带解决的一件事：磨砂一走，backdrop-filter 也就没了。面板长时间叠在
       * 视频上，每帧重采样模糊是掉帧的头号大户（globals 里的 QoE 注释），而
       * 诊断面板恰恰是用来量掉帧的，不能自己污染读数。
       */
      className="
        absolute z-20 overflow-y-auto overscroll-contain rounded-[14px] bg-black/70 px-3.5 py-2.5 text-[11.5px]
        left-6 top-[calc(4.5rem_+_var(--safe-top))] w-[320px] max-h-[calc(100%-12rem)]
        max-md:left-3 max-md:right-3 max-md:w-auto
        max-md:max-h-[calc(50%_-_4.5rem_-_var(--safe-top)_-_2.5rem)]
      "
    >
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[12px] font-semibold text-white/90">播放诊断</h3>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭播放诊断"
          className="-mr-1 grid size-6 place-items-center rounded-full text-white/50 transition-colors hover:bg-white/10 hover:text-white"
        >
          <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden>
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>

      <div className="space-y-0.5">
        <Row label="档位" value={TIER_LABELS[decision.tier ?? -1] ?? "未知"} />
        {decision.degraded_from != null ? (
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
        {/* 直通档（0/1/2）根本不经编码器，「硬件加速：不适用」是句废话；
            码率拿不到时那条 `—` 同理。竖屏能显示的行数有限，没信息量的行
            就是在把真正要看的判定理由挤出屏幕。 */}
        {decision.tier != null && decision.tier >= 3 ? (
          <Row
            label="硬件加速"
            value={session.hw_backend ?? (decision.tier === 4 ? "无（软件转码）" : "不适用")}
          />
        ) : null}
        <Row label="传输" value={stats?.engine ?? "—"} />
        {stats?.bitrate ? (
          <Row label="实时码率" value={`${(stats.bitrate / 1_000_000).toFixed(1)} Mbps`} />
        ) : null}
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

      <p className="mt-2.5 border-t border-white/10 pt-2 leading-relaxed text-white/60">
        {decision.reason}
      </p>
    </div>
  );
}
