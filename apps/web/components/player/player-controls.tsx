"use client";

import { useEffect, useRef, useState } from "react";
import {
  MediaFullscreenButton,
  MediaMuteButton,
  MediaPipButton,
  MediaVolumeRange,
} from "media-chrome/react";

import { SUBTITLE_OFFSET_STEP, clampSubtitleOffset } from "@/lib/player/subtitles";
import type { SubtitleStyle, SubtitleTracks } from "@/lib/player/subtitles";
import { formatClock } from "@/lib/player/timeline";
import { type TrickplayIndex, tileAt } from "@/lib/player/trickplay";

/**
 * 播放器控制条（docs/design/web-player.md §6.1 / §6.5）。
 *
 * **进度条为什么是自建的**：Media Chrome 的 `<media-time-range>` 直接读
 * `video.currentTime` 与 `video.duration`，而转码会话的时间轴零点在文件的
 * `start_ms` 处、`duration` 只到「已经转出来的那一段」。照它渲染，用户从
 * 一小时处续播时进度条会显示成 0、总时长显示成 30 秒。所以进度条按**文件
 * 时间**自建，其余按钮（音量/画中画/全屏）继续用 Media Chrome——那些与
 * 时间轴无关，交给它反而更稳。
 */

const ICON = "size-5 fill-current";

function PlayGlyph({ paused }: { paused: boolean }) {
  return paused ? (
    <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
      <path d="M8 5.14v13.72a.5.5 0 0 0 .76.43l11.02-6.86a.5.5 0 0 0 0-.86L8.76 4.71a.5.5 0 0 0-.76.43Z" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
      <path d="M7 4h3.5v16H7zM13.5 4H17v16h-3.5z" />
    </svg>
  );
}

const BUTTON_CLASS =
  "flex size-9 items-center justify-center rounded-lg text-white/85 transition-colors hover:bg-white/15 hover:text-white";

export interface PlayerControlsProps {
  positionMs: number;
  /** 片长（文件时间）。服务端算不出时为 null，此时进度条只显示已播时间 */
  durationMs: number | null;
  /** 当前会话已缓冲到的文件位置，用于进度条的浅色底 */
  bufferedEndMs: number | null;
  paused: boolean;
  onTogglePlay: () => void;
  onSeek: (fileMs: number) => void;
  onSeekBy: (seconds: number) => void;
  subtitles: SubtitleTracks;
  selectedSubtitle: string | null;
  onSelectSubtitle: (ref: string | null) => void;
  subtitleStyle: SubtitleStyle;
  onSubtitleStyleChange: (style: SubtitleStyle) => void;
  diagnosticsOpen: boolean;
  onToggleDiagnostics: () => void;
  onNext: (() => void) | null;
  /** 进度条缩略图索引。null = 还没生成好，表现为没有预览 */
  trickplay: TrickplayIndex | null;
}

export function PlayerControls(props: PlayerControlsProps) {
  const {
    positionMs,
    durationMs,
    bufferedEndMs,
    paused,
    onTogglePlay,
    onSeek,
    onSeekBy,
    subtitles,
    selectedSubtitle,
    onSelectSubtitle,
    subtitleStyle,
    onSubtitleStyleChange,
    diagnosticsOpen,
    onToggleDiagnostics,
    onNext,
    trickplay,
  } = props;

  // 拖动中的本地值：直接跟 positionMs 会被 timeupdate 反复拉回去，手感是
  // 滑块「粘手」——松手才提交是进度条唯一能用的做法
  const [dragging, setDragging] = useState<number | null>(null);
  const [menu, setMenu] = useState<"none" | "subtitles">("none");
  // 悬停预览的位置（文件毫秒 + 进度条内的像素横坐标）。null = 没在悬停
  const [hover, setHover] = useState<{ ms: number; x: number } | null>(null);
  const shown = dragging ?? positionMs;
  const previewTile = hover ? tileAt(trickplay, hover.ms) : null;
  const progress = durationMs ? Math.min(100, (shown / durationMs) * 100) : 0;
  const buffered = durationMs && bufferedEndMs ? Math.min(100, (bufferedEndMs / durationMs) * 100) : 0;

  return (
    <div className="pointer-events-auto bg-gradient-to-t from-black/85 via-black/55 to-transparent px-4 pb-4 pt-16">
      {/* 进度条 */}
      <div
        className="relative mb-1 h-6"
        onPointerMove={(e) => {
          if (!durationMs) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const x = Math.min(Math.max(e.clientX - rect.left, 0), rect.width);
          setHover({ ms: (x / rect.width) * durationMs, x });
        }}
        onPointerLeave={() => setHover(null)}
      >
        {/* 缩略图预览：拖进度条时能看见画面。没生成好就没有，不影响拖动 */}
        {hover && previewTile && (
          <div
            className="pointer-events-none absolute bottom-7 -translate-x-1/2 rounded-md border border-white/15 bg-black/70 p-1 shadow-lg"
            style={{ left: hover.x }}
          >
            <div
              style={{
                width: previewTile.width,
                height: previewTile.height,
                backgroundImage: `url(${previewTile.url})`,
                backgroundPosition: `${previewTile.offsetX}px ${previewTile.offsetY}px`,
              }}
              className="rounded-sm"
            />
            <p className="mt-0.5 text-center text-caption tabular-nums text-white/80">
              {formatClock(hover.ms)}
            </p>
          </div>
        )}
        <div className="pointer-events-none absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 overflow-hidden rounded-full bg-white/20">
          <div className="h-full bg-white/30" style={{ width: `${buffered}%` }} />
          <div
            className="absolute inset-y-0 left-0 bg-[var(--accent)]"
            style={{ width: `${progress}%` }}
          />
        </div>
        <input
          type="range"
          min={0}
          max={durationMs ?? 0}
          step={1000}
          value={shown}
          disabled={!durationMs}
          aria-label="播放进度"
          onChange={(e) => setDragging(Number(e.target.value))}
          onPointerUp={() => {
            if (dragging !== null) onSeek(dragging);
            setDragging(null);
          }}
          onKeyUp={() => {
            if (dragging !== null) onSeek(dragging);
            setDragging(null);
          }}
          className="player-scrub absolute inset-x-0 top-1/2 h-6 w-full -translate-y-1/2 cursor-pointer appearance-none bg-transparent disabled:cursor-default"
        />
      </div>

      <div className="flex items-center gap-1">
        <button type="button" onClick={onTogglePlay} className={BUTTON_CLASS} aria-label={paused ? "播放" : "暂停"}>
          <PlayGlyph paused={paused} />
        </button>
        <button type="button" onClick={() => onSeekBy(-10)} className={BUTTON_CLASS} aria-label="后退 10 秒">
          <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
            <path d="M12 5V2L7 6l5 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7Z" />
            <text x="12" y="15.5" textAnchor="middle" fontSize="7" fill="currentColor" stroke="none">
              10
            </text>
          </svg>
        </button>
        <button type="button" onClick={() => onSeekBy(10)} className={BUTTON_CLASS} aria-label="前进 10 秒">
          <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
            <path d="M12 5V2l5 4-5 4V7a5 5 0 1 0 5 5h2a7 7 0 1 1-7-7Z" />
            <text x="12" y="15.5" textAnchor="middle" fontSize="7" fill="currentColor" stroke="none">
              10
            </text>
          </svg>
        </button>

        {/* 音量交给 Media Chrome：它已经处理好静音记忆、触屏与键盘无障碍 */}
        <MediaMuteButton className="player-mc-button" />
        <MediaVolumeRange className="player-mc-range" />

        <span className="tnum ml-2 text-[13px] text-white/75">
          {formatClock(shown)}
          <span className="text-white/40"> / {durationMs ? formatClock(durationMs) : "--:--"}</span>
        </span>

        <div className="flex-1" />

        {onNext ? (
          <button type="button" onClick={onNext} className={`${BUTTON_CLASS} w-auto px-3 text-[13px]`}>
            下一集
          </button>
        ) : null}

        <div className="relative">
          <button
            type="button"
            onClick={() => setMenu(menu === "subtitles" ? "none" : "subtitles")}
            className={`${BUTTON_CLASS} ${selectedSubtitle ? "text-[var(--accent)]" : ""}`}
            aria-label="字幕"
          >
            <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
              <path d="M3 5.5A1.5 1.5 0 0 1 4.5 4h15A1.5 1.5 0 0 1 21 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-13ZM6 13h6v1.6H6V13Zm8 0h4v1.6h-4V13ZM6 9.5h4v1.6H6V9.5Zm6 0h6v1.6h-6V9.5Z" />
            </svg>
          </button>
          {menu === "subtitles" ? (
            <SubtitleMenu
              tracks={subtitles}
              selected={selectedSubtitle}
              onSelect={(ref) => onSelectSubtitle(ref)}
              style={subtitleStyle}
              onStyleChange={onSubtitleStyleChange}
              onClose={() => setMenu("none")}
            />
          ) : null}
        </div>

        <button
          type="button"
          onClick={onToggleDiagnostics}
          className={`${BUTTON_CLASS} ${diagnosticsOpen ? "text-[var(--accent)]" : ""}`}
          aria-label="播放诊断"
        >
          <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
            <path d="M4 19h2v-6H4v6Zm5 0h2V5H9v14Zm5 0h2v-9h-2v9Zm5 0h2V8h-2v11Z" />
          </svg>
        </button>

        <MediaPipButton className="player-mc-button" />
        <MediaFullscreenButton className="player-mc-button" />
      </div>
    </div>
  );
}

/** 字幕菜单：选轨 + 时间轴微调 + 外观。都是外挂字幕的日常刚需，不是锦上添花。 */
function SubtitleMenu({
  tracks,
  selected,
  onSelect,
  style,
  onStyleChange,
  onClose,
}: {
  tracks: SubtitleTracks;
  selected: string | null;
  onSelect: (ref: string | null) => void;
  style: SubtitleStyle;
  onStyleChange: (style: SubtitleStyle) => void;
  onClose: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDocPointerDown = (event: PointerEvent) => {
      if (!box.current?.contains(event.target as Node)) onClose();
    };
    // 捕获阶段：菜单里的按钮自己 stopPropagation 时也要能关掉外面的菜单
    document.addEventListener("pointerdown", onDocPointerDown, true);
    return () => document.removeEventListener("pointerdown", onDocPointerDown, true);
  }, [onClose]);

  return (
    <div
      ref={box}
      className="absolute bottom-11 right-0 w-[290px] rounded-xl border border-white/10 bg-black/85 p-2 text-[13px] backdrop-blur-md"
    >
      <div className="max-h-[220px] overflow-y-auto">
        <MenuItem active={selected === null} onClick={() => onSelect(null)}>
          关闭字幕
        </MenuItem>
        {tracks.options.map((option) => (
          <MenuItem
            key={option.ref}
            active={selected === option.ref}
            onClick={() => onSelect(option.ref)}
          >
            {option.label}
          </MenuItem>
        ))}
        {tracks.unavailable.map((item) => (
          <div key={item.label} className="px-3 py-1.5 text-white/35">
            <div className="truncate">{item.label}</div>
            <div className="text-[11px] leading-snug">{item.reason}</div>
          </div>
        ))}
        {tracks.options.length === 0 && tracks.unavailable.length === 0 ? (
          <div className="px-3 py-2 text-white/40">这个文件没有可用字幕</div>
        ) : null}
      </div>

      {selected ? (
        <div className="mt-2 space-y-2 border-t border-white/10 pt-2">
          <StepRow
            label="时间轴"
            value={`${style.offsetSeconds > 0 ? "+" : ""}${style.offsetSeconds.toFixed(1)} 秒`}
            onMinus={() =>
              onStyleChange({
                ...style,
                offsetSeconds: clampSubtitleOffset(style.offsetSeconds - SUBTITLE_OFFSET_STEP),
              })
            }
            onPlus={() =>
              onStyleChange({
                ...style,
                offsetSeconds: clampSubtitleOffset(style.offsetSeconds + SUBTITLE_OFFSET_STEP),
              })
            }
          />
          <StepRow
            label="字号"
            value={`${style.fontScale.toFixed(1)}`}
            onMinus={() =>
              onStyleChange({ ...style, fontScale: Math.max(2, style.fontScale - 0.4) })
            }
            onPlus={() =>
              onStyleChange({ ...style, fontScale: Math.min(10, style.fontScale + 0.4) })
            }
          />
          <StepRow
            label="位置"
            value={`${style.bottomPercent}%`}
            onMinus={() =>
              onStyleChange({ ...style, bottomPercent: Math.max(0, style.bottomPercent - 2) })
            }
            onPlus={() =>
              onStyleChange({ ...style, bottomPercent: Math.min(40, style.bottomPercent + 2) })
            }
          />
          <div className="flex gap-2 px-3 pb-1">
            <Toggle
              on={style.outline}
              onClick={() => onStyleChange({ ...style, outline: !style.outline })}
            >
              描边
            </Toggle>
            <Toggle
              on={style.background}
              onClick={() => onStyleChange({ ...style, background: !style.background })}
            >
              背景
            </Toggle>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function MenuItem({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left transition-colors hover:bg-white/10 ${
        active ? "text-[var(--accent)]" : "text-white/80"
      }`}
    >
      <span className="w-3 shrink-0">{active ? "✓" : ""}</span>
      <span className="truncate">{children}</span>
    </button>
  );
}

function StepRow({
  label,
  value,
  onMinus,
  onPlus,
}: {
  label: string;
  value: string;
  onMinus: () => void;
  onPlus: () => void;
}) {
  return (
    <div className="flex items-center justify-between px-3 text-white/70">
      <span>{label}</span>
      <span className="flex items-center gap-1">
        <StepButton onClick={onMinus}>−</StepButton>
        <span className="tnum w-[68px] text-center text-white/90">{value}</span>
        <StepButton onClick={onPlus}>+</StepButton>
      </span>
    </div>
  );
}

function StepButton({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="size-6 rounded-md bg-white/10 leading-none text-white/85 transition-colors hover:bg-white/20"
    >
      {children}
    </button>
  );
}

function Toggle({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-lg px-3 py-1 text-[12px] transition-colors ${
        on ? "bg-[var(--accent)]/25 text-[var(--accent)]" : "bg-white/10 text-white/60"
      }`}
    >
      {children}
    </button>
  );
}
