"use client";

import { useEffect, useRef, useState } from "react";
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
 * 时间轴无关，交给它反而更稳；只是图标全部用 slot 换成本文件里这一套，
 * 免得一条控制条上出现两种线宽的图标。
 *
 * **布局照 YouTube**，分三块：
 *
 * - **中央簇**（`PlayerCenterControls`）：退十秒 / 播放暂停 / 进十秒。播放
 *   控制是最高频的动作，放画面正中比塞在左下角更好够到，触屏上尤其明显。
 * - **控制行**：左端时间，右簇下一集/字幕/诊断/画中画/横屏。片名不再重复
 *   放这里——顶栏已经有了，重复只会让静止画面更吵。音量条也去掉了：网页
 *   播放器上调音量的人远比想象中少（系统音量、耳机、键盘 M 与上下键都能
 *   管），留着只是让静止画面多一件东西。
 * - **进度条**：常驻播放器**最底边**，控制条淡出后它仍在，只是收成一条
 *   贴边的细线。这是「安静时也知道播到哪」与「安静时画面干净」唯一能同时
 *   成立的做法，也是 YouTube 控件隐藏后的样子。
 *
 * 图标之间靠间距和悬停放大区分，不用背景色块——黑底上的浅灰方块最显廉价。
 */

const ICON = "size-6 fill-current";

/** 播放 / 暂停。只出现在中央簇，所以尺寸按中央簇给。 */
function PlayGlyph({ paused }: { paused: boolean }) {
  const cls = "size-[52px] fill-current max-md:size-11";
  return paused ? (
    <svg viewBox="0 0 24 24" className={cls} aria-hidden>
      <path d="M6 4.3v15.4a.7.7 0 0 0 1.07.6l12.3-7.7a.7.7 0 0 0 0-1.2L7.07 3.7A.7.7 0 0 0 6 4.3Z" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" className={cls} aria-hidden>
      <path d="M6.5 4h3.6v16H6.5zM13.9 4h3.6v16h-3.6z" />
    </svg>
  );
}

/** 退/进十秒：圆弧箭头 + 中间的 10，Netflix 与 YouTube 用的都是这一种。 */
function SkipGlyph({ forward }: { forward: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className="size-9 fill-current max-md:size-8" aria-hidden>
      {forward ? (
        <path d="M12 4.5V1.8l5.2 4.1L12 10V7.2a4.9 4.9 0 1 0 4.9 4.9h2.1A7 7 0 1 1 12 4.5Z" />
      ) : (
        <path d="M12 4.5V1.8L6.8 5.9 12 10V7.2a4.9 4.9 0 1 1-4.9 4.9H5A7 7 0 1 0 12 4.5Z" />
      )}
      <text
        x="12"
        y="16.2"
        textAnchor="middle"
        fontSize="7.5"
        fontWeight="600"
        fill="currentColor"
        stroke="none"
      >
        10
      </text>
    </svg>
  );
}

/** 字幕图标：外框 + 挖空的字幕线。必须用 evenodd，非零缠绕会把线条填实成白块。 */
function SubtitleGlyph() {
  return (
    <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
      <path
        fillRule="evenodd"
        d="M3 5.8A1.8 1.8 0 0 1 4.8 4h14.4A1.8 1.8 0 0 1 21 5.8v12.4a1.8 1.8 0 0 1-1.8 1.8H4.8A1.8 1.8 0 0 1 3 18.2V5.8ZM6 9.3h4.1v1.8H6V9.3Zm6 0h6v1.8h-6V9.3ZM6 13.1h6.2v1.8H6v-1.8Zm8.1 0H18v1.8h-3.9v-1.8Z"
      />
    </svg>
  );
}

/** 设置齿轮：八颗齿 + 一圈 + 中心点。手画而不是抄图标库，省一个依赖。 */
function GearGlyph() {
  return (
    <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
      {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
        <rect
          key={i}
          x="10.5"
          y="2.6"
          width="3"
          height="3.6"
          rx="1.2"
          transform={`rotate(${i * 45} 12 12)`}
        />
      ))}
      <circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" strokeWidth="3" />
      <circle cx="12" cy="12" r="2.2" />
    </svg>
  );
}

/** 菜单里的柱状图小图标：只用来标「播放诊断」这一项。 */
function StatsGlyph() {
  return (
    <svg viewBox="0 0 24 24" className="size-4 shrink-0 fill-current" aria-hidden>
      <path d="M4 19h2.6v-6H4v6Zm5.2 0h2.6V5H9.2v14Zm5.2 0H17v-9h-2.6v9Zm5.2 0H22V8h-2.4v11Z" />
    </svg>
  );
}

/**
 * 横屏：一个旋转弧 + 一台设备。设备的方向画的是**点下去之后**的样子——
 * 图标表示结果而不是现状，否则用户要在脑子里做一次取反。
 */
function RotateGlyph({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
      <path
        d="M4 10.2A6.2 6.2 0 0 1 10.2 4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
      />
      <path d="M4 13.4 1.3 8.7h5.4z" />
      {active ? (
        <rect x="9.6" y="8.8" width="9" height="13.2" rx="1.8" fill="none" stroke="currentColor" strokeWidth="1.9" />
      ) : (
        <rect x="8.2" y="11" width="14" height="9" rx="1.8" fill="none" stroke="currentColor" strokeWidth="1.9" />
      )}
    </svg>
  );
}

function FullscreenGlyph({ exit }: { exit?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className={ICON} aria-hidden>
      {exit ? (
        <path d="M9.4 3v4.6a1.8 1.8 0 0 1-1.8 1.8H3v-2h4.4V3h2Zm5.2 0h2v4.4H21v2h-4.6a1.8 1.8 0 0 1-1.8-1.8V3ZM3 14.6h4.6a1.8 1.8 0 0 1 1.8 1.8V21h-2v-4.4H3v-2Zm11.6 1.8a1.8 1.8 0 0 1 1.8-1.8H21v2h-4.4V21h-2v-4.6Z" />
      ) : (
        <path d="M3 3h6.6v2H5v4.6H3V3Zm11.4 0H21v6.6h-2V5h-4.6V3ZM3 14.4h2V19h4.6v2H3v-6.6ZM19 14.4h2V21h-6.6v-2H19v-4.6Z" />
      )}
    </svg>
  );
}

export interface PlayerControlsProps {
  positionMs: number;
  /** 片长（文件时间）。服务端算不出时为 null，此时进度条只显示已播时间 */
  durationMs: number | null;
  /** 当前会话已缓冲到的文件位置，用于进度条的浅色底 */
  bufferedEndMs: number | null;
  /** 控制条是否可见。进度条不跟着淡出，只是收成贴底边的细线 */
  chromeVisible: boolean;
  onSeek: (fileMs: number) => void;
  subtitles: SubtitleTracks;
  selectedSubtitle: string | null;
  onSelectSubtitle: (ref: string | null) => void;
  subtitleStyle: SubtitleStyle;
  onSubtitleStyleChange: (style: SubtitleStyle) => void;
  diagnosticsOpen: boolean;
  onToggleDiagnostics: () => void;
  /** 横屏（全屏 + 锁横向）；已经在里面时点它就是退出 */
  landscape: boolean;
  /** 这台设备的方向锁真的能用（手机/平板）。桌面上同一个按钮叫「全屏」 */
  canRotate: boolean;
  onToggleLandscape: () => void;
  /** 菜单展开时要顶住控制条的自动隐藏，否则菜单会连着控制条一起淡掉 */
  onMenuOpenChange: (open: boolean) => void;
  /** 进度条缩略图索引。null = 还没生成好，表现为没有预览 */
  trickplay: TrickplayIndex | null;
}

export function PlayerControls(props: PlayerControlsProps) {
  const {
    positionMs,
    durationMs,
    bufferedEndMs,
    chromeVisible,
    onSeek,
    subtitles,
    selectedSubtitle,
    onSelectSubtitle,
    subtitleStyle,
    onSubtitleStyleChange,
    diagnosticsOpen,
    onToggleDiagnostics,
    landscape,
    canRotate,
    onToggleLandscape,
    onMenuOpenChange,
    trickplay,
  } = props;

  // 拖动中的本地值：直接跟 positionMs 会被 timeupdate 反复拉回去，手感是
  // 滑块「粘手」——松手才提交是进度条唯一能用的做法
  const [dragging, setDragging] = useState<number | null>(null);
  const [menu, setMenu] = useState<"none" | "subtitles" | "settings">("none");
  // 悬停预览的位置（文件毫秒 + 进度条内的像素横坐标）。null = 没在悬停
  const [hover, setHover] = useState<{ ms: number; x: number } | null>(null);
  const shown = dragging ?? positionMs;
  const previewTile = hover ? tileAt(trickplay, hover.ms) : null;
  const progress = durationMs ? Math.min(100, (shown / durationMs) * 100) : 0;
  const buffered =
    durationMs && bufferedEndMs ? Math.min(100, (bufferedEndMs / durationMs) * 100) : 0;

  const openMenu = (next: "none" | "subtitles" | "settings") => {
    setMenu(next);
    onMenuOpenChange(next !== "none");
  };

  return (
    <div className="pointer-events-none relative">
      {/* 渐变铺满整块底部（含进度条那一条），与控制行同步淡出——只淡控制行
          会留下一道空的黑带，只淡渐变又会让按钮浮在亮画面上看不清 */}
      <div
        className={`absolute inset-0 bg-gradient-to-t from-black/90 via-black/55 to-transparent transition-opacity duration-300 ${
          chromeVisible ? "opacity-100" : "opacity-0"
        }`}
      />

      {/* ---- 控制行：左端时间，右簇功能键 ---- */}
      <div
        className={`relative flex items-center gap-5 px-6 pt-24 transition-opacity duration-300 max-md:gap-3 max-md:px-3 max-md:pt-16 ${
          chromeVisible ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <span className="text-[14px] tabular-nums text-white/85 max-md:text-[12px]">
          {formatClock(shown)}
          <span className="text-white/45"> / {durationMs ? formatClock(durationMs) : "--:--"}</span>
        </span>

        <div className="flex-1" />

        <div className="relative">
          <IconButton
            tip="字幕"
            active={Boolean(selectedSubtitle) || menu === "subtitles"}
            onClick={() => openMenu(menu === "subtitles" ? "none" : "subtitles")}
          >
            <SubtitleGlyph />
          </IconButton>
          {menu === "subtitles" ? (
            <SubtitleMenu
              tracks={subtitles}
              selected={selectedSubtitle}
              onSelect={(ref) => onSelectSubtitle(ref)}
              style={subtitleStyle}
              onStyleChange={onSubtitleStyleChange}
              onClose={() => openMenu("none")}
            />
          ) : null}
        </div>

        <div className="relative">
          <IconButton
            tip="设置"
            active={menu === "settings"}
            onClick={() => openMenu(menu === "settings" ? "none" : "settings")}
          >
            <GearGlyph />
          </IconButton>
          {menu === "settings" ? (
            <MenuPanel title="设置" onClose={() => openMenu("none")}>
              <MenuItem
                active={diagnosticsOpen}
                icon={<StatsGlyph />}
                onClick={() => {
                  onToggleDiagnostics();
                  openMenu("none");
                }}
              >
                播放诊断
              </MenuItem>
            </MenuPanel>
          ) : null}
        </div>

        <IconButton
          tip={canRotate ? (landscape ? "退出横屏" : "横屏") : landscape ? "退出全屏" : "全屏"}
          onClick={onToggleLandscape}
        >
          {canRotate ? <RotateGlyph active={landscape} /> : <FullscreenGlyph exit={landscape} />}
        </IconButton>
      </div>

      {/* ---- 进度条：常驻最底边 ----
          控制条淡出后它不淡出，只是把左右内边距和高度收掉，变成一条贴着
          播放器下沿的细线。安静时画面干净，又不用点一下才知道播到哪。 */}
      <div
        className={`player-scrub-row pointer-events-auto relative transition-[padding] duration-300 ${
          chromeVisible ? "px-6 pb-2.5 max-md:px-3" : "px-0 pb-0"
        }`}
      >
        <div
          className={`relative transition-[height] duration-300 ${chromeVisible ? "h-5" : "h-[3px]"}`}
          onPointerMove={(e) => {
            if (!durationMs) return;
            const rect = e.currentTarget.getBoundingClientRect();
            const x = Math.min(Math.max(e.clientX - rect.left, 0), rect.width);
            setHover({ ms: (x / rect.width) * durationMs, x });
          }}
          onPointerLeave={() => setHover(null)}
        >
          {/* 缩略图预览：拖进度条时能看见画面。没生成好就只剩时间戳，
              不影响拖动——预览是锦上添花，时间戳是刚需。 */}
          {hover ? (
            <div
              className="pointer-events-none absolute bottom-8 -translate-x-1/2"
              style={{ left: hover.x }}
            >
              {previewTile ? (
                <div
                  style={{
                    width: previewTile.width,
                    height: previewTile.height,
                    backgroundImage: `url(${previewTile.url})`,
                    backgroundPosition: `${previewTile.offsetX}px ${previewTile.offsetY}px`,
                  }}
                  className="rounded-[3px] shadow-[0_4px_18px_rgba(0,0,0,0.6)] ring-2 ring-white/85"
                />
              ) : null}
              <p className="mt-1.5 text-center text-[13px] font-medium tabular-nums text-white drop-shadow">
                {formatClock(hover.ms)}
              </p>
            </div>
          ) : null}

          {/* 轨道：静止 3px、悬停 5px。Netflix 的细红线就是这个手感 */}
          <div className="pointer-events-none absolute inset-x-0 top-1/2 h-[3px] -translate-y-1/2 overflow-hidden rounded-full bg-[var(--player-track)] transition-[height] duration-150 [.player-scrub-row:hover_&]:h-[5px]">
            <div className="h-full bg-[var(--player-buffered)]" style={{ width: `${buffered}%` }} />
            <div
              className="absolute inset-y-0 left-0 bg-[var(--player-accent)]"
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
            className="player-scrub absolute inset-x-0 top-1/2 h-5 w-full -translate-y-1/2 cursor-pointer appearance-none bg-transparent disabled:cursor-default"
          />

          {/* 把手自己画，不用 input 原生的那个。
              原生把手在 `宽度 - 把手宽` 的范围里走：0% 时它的圆心在左边缘往里
              半个把手，100% 时往里半个把手，而我们画的已播段是按整条宽度铺的，
              两者只有在正中才对得上，两端各差半个把手（14px 的把手就是 7px）。
              这就是「圆点没对齐进度」的来源。把原生把手缩到 1px 隐藏掉，改成
              按 `left: 进度%` 定位一个自己的圆点，两者从此永远同一个位置；
              1px 的把手同时也让指针位置到时间的换算变成精确的线性映射。 */}
          <div
            className={`pointer-events-none absolute top-1/2 size-[14px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--player-thumb)] shadow-[0_0_0_4px_var(--accent-soft)] transition-transform duration-150 ${
              durationMs
                ? dragging !== null
                  ? "scale-100"
                  : "scale-0 [.player-scrub-row:hover_&]:scale-100"
                : "scale-0"
            }`}
            style={{ left: `${progress}%` }}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * 中央播放簇：退十秒 / 播放暂停 / 进十秒。
 *
 * 与控制行分开是因为它们的显示位置不同（一个铺在画面正中、一个贴着底边），
 * 但淡入淡出必须同步——所以可见性由上层的 `chromeVisible` 统一给，这里只
 * 负责画。容器不吃指针事件，点画面本身仍然是播放/暂停。
 */
export function PlayerCenterControls({
  paused,
  visible,
  onTogglePlay,
  onSeekBy,
}: {
  paused: boolean;
  visible: boolean;
  onTogglePlay: () => void;
  onSeekBy: (seconds: number) => void;
}) {
  return (
    <div
      className={`pointer-events-none absolute inset-0 z-10 flex items-center justify-center gap-14 transition-opacity duration-300 max-md:gap-10 ${
        visible ? "opacity-100" : "opacity-0"
      }`}
    >
      <CenterButton label="后退 10 秒" visible={visible} onClick={() => onSeekBy(-10)}>
        <SkipGlyph forward={false} />
      </CenterButton>
      <CenterButton label={paused ? "播放" : "暂停"} visible={visible} onClick={onTogglePlay} primary>
        <PlayGlyph paused={paused} />
      </CenterButton>
      <CenterButton label="前进 10 秒" visible={visible} onClick={() => onSeekBy(10)}>
        <SkipGlyph forward />
      </CenterButton>
    </div>
  );
}

function CenterButton({
  label,
  visible,
  primary,
  onClick,
  children,
}: {
  label: string;
  visible: boolean;
  primary?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      // 淡出后必须同时断掉命中，否则隐形的按钮会在用户想点画面时误触
      className={`player-btn drop-shadow-[0_2px_8px_rgba(0,0,0,0.65)] ${
        primary ? "size-[68px] max-md:size-14" : "size-12 max-md:size-11"
      } ${visible ? "pointer-events-auto" : "pointer-events-none"}`}
    >
      {children}
    </button>
  );
}

/** 控制条上的图标按钮：悬停放大 + 上方说明气泡，样式统一在 globals.css。 */
function IconButton({
  tip,
  active,
  onClick,
  children,
}: {
  tip: string;
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={tip}
      data-tip={tip}
      data-active={active ? "true" : undefined}
      className="player-btn player-tip size-10 shrink-0"
    >
      {children}
    </button>
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
  return (
    <MenuPanel title="字幕" onClose={onClose}>
      <div className="max-h-[240px] overflow-y-auto">
        <MenuItem active={selected === null} onClick={() => onSelect(null)}>
          关闭
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
          <div key={item.label} className="px-4 py-1.5 text-white/35">
            <div className="truncate">{item.label}</div>
            <div className="text-[12px] leading-snug">{item.reason}</div>
          </div>
        ))}
        {tracks.options.length === 0 && tracks.unavailable.length === 0 ? (
          <div className="px-4 py-2 text-white/40">这个文件没有可用字幕</div>
        ) : null}
      </div>

      {selected ? (
        <div className="mt-2 space-y-2 border-t border-white/10 pt-3">
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
          <div className="flex gap-2 px-4 pb-1">
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
    </MenuPanel>
  );
}

/**
 * 控制条上方弹出的小菜单：字幕与设置共用。
 *
 * 抽出来是因为「点外面关掉」这段必须写在捕获阶段（菜单里的按钮自己
 * stopPropagation 时也要能关），复制两份迟早有一份忘了改。
 */
function MenuPanel({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDocPointerDown = (event: PointerEvent) => {
      if (!box.current?.contains(event.target as Node)) onClose();
    };
    document.addEventListener("pointerdown", onDocPointerDown, true);
    return () => document.removeEventListener("pointerdown", onDocPointerDown, true);
  }, [onClose]);

  return (
    <div
      ref={box}
      className="absolute bottom-14 right-0 w-[300px] rounded-sm border border-white/15 bg-[rgba(20,20,20,0.94)] py-2 text-[14px] shadow-[0_8px_32px_rgba(0,0,0,0.7)] backdrop-blur-sm"
    >
      <p className="px-4 pb-2 text-[12px] font-semibold uppercase tracking-wide text-white/45">
        {title}
      </p>
      {children}
    </div>
  );
}

function MenuItem({
  active,
  icon,
  onClick,
  children,
}: {
  active: boolean;
  /** 可选的行首小图标。必须与文字**并列**，不能塞进 truncate 的 span 里——
   *  Tailwind preflight 把 svg 设成 display:block，塞进去会自己换一行。 */
  icon?: React.ReactNode;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 border-l-[3px] px-4 py-1.5 text-left transition-colors hover:bg-white/10 ${
        active
          ? "border-[var(--player-accent)] font-semibold text-white"
          : "border-transparent text-white/70"
      }`}
    >
      {icon}
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
    <div className="flex items-center justify-between px-4 text-[13px] text-white/65">
      <span>{label}</span>
      <span className="flex items-center gap-1.5">
        <StepButton onClick={onMinus}>−</StepButton>
        <span className="w-[68px] text-center tabular-nums text-white/90">{value}</span>
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
      className="size-6 rounded-sm bg-white/12 leading-none text-white/85 transition-colors hover:bg-white/25"
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
      className={`rounded-sm px-3 py-1 text-[12px] transition-colors ${
        on ? "bg-[var(--player-accent)] text-black" : "bg-white/12 text-white/60 hover:bg-white/20"
      }`}
    >
      {children}
    </button>
  );
}
