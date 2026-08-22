"use client";

import { useEffect, useRef, useState } from "react";
import { ActivityIcon, ExpandIcon, GearIcon, ShrinkIcon } from "@/components/icons";
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
 * **图标与尺寸对齐全站**（见 components/page-nav.tsx 的 `PAGE_NAV_BUTTON_CLASS`
 * 与 components/icons.tsx）：功能键的命中区 36px / 移动 44px，图标 18px / 22px，
 * 风格是 24×24、`strokeWidth 1.8` 的描边——能直接用站内图标的就直接用
 * （齿轮、全屏、诊断），站内没有的（字幕、横屏）按同一套描边规格自己画。
 *
 * **只有传输控件是实心的**：播放/暂停/退进十秒/切集。描边的播放三角读起来
 * 是「一个箭头轮廓」而不是「播放」，所有播放器都用实心；站内的 `PlayIcon`
 * 本身也是 `fill=currentColor` 的，所以这不算破例。
 */

/** 功能键图标尺寸：与 page-nav 的顶栏控件一致。 */
const ICON = "size-[18px] max-md:size-[22px]";

/**
 * 描边图标底座：镜像 components/icons.tsx 里的 `Base`（那边没导出）。
 * 播放器里只有字幕与横屏两个图标站内没有，其余一律直接用站内图标。
 */
function StrokeIcon({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className ?? ICON}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

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

/** 字幕：站内没有这个图标，按同一套描边规格画。 */
function SubtitleGlyph() {
  return (
    <StrokeIcon>
      <rect x="3" y="5" width="18" height="14" rx="2.5" />
      <path d="M7 10.6h4M13.5 10.6h3.5M7 14.4h6.5M16 14.4h1" />
    </StrokeIcon>
  );
}

/** 切集按钮里的小箭头。比控制条上的图标小一号，跟着文字走。 */
function NextGlyph() {
  return (
    <svg viewBox="0 0 24 24" className="size-4 shrink-0 fill-current" aria-hidden>
      <path d="M5 4.6v14.8a.6.6 0 0 0 .93.5l11-7.4a.6.6 0 0 0 0-1L5.93 4.1A.6.6 0 0 0 5 4.6Z" />
      <path d="M18.4 4h2.2v16h-2.2z" />
    </svg>
  );
}

/**
 * 横屏：一个旋转弧 + 一台设备，站内没有这个图标，按同一套描边规格画。
 * 设备的方向画的是**点下去之后**的样子——图标表示结果而不是现状，
 * 否则用户要在脑子里做一次取反。
 */
function RotateGlyph({ active }: { active: boolean }) {
  return (
    <StrokeIcon>
      <path d="M4.4 10.4A6.2 6.2 0 0 1 10.4 4.4" />
      <path d="m2.2 8.6 2.2 2.6 2.6-2.2" />
      {active ? (
        <rect x="9.4" y="8.6" width="9.2" height="13.4" rx="2" />
      ) : (
        <rect x="8.6" y="11" width="13.4" height="9.2" rx="2" />
      )}
    </StrokeIcon>
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
  /** 剧集才有右下角那个位；电影不显示（「已完结」对电影是错的说法） */
  isSeries: boolean;
  /** 有下一集时的回调；剧集但为 null = 本季到头了，那个位显示「已完结」 */
  onNext: (() => void) | null;
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
    isSeries,
    onNext,
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
      {/* 渐变铺满整块底部，与控制条同步淡出。它只负责把画面压暗一档，真正
          保证按钮可读的是按钮自己那层磨砂卡片——渐变挡不住亮画面。 */}
      <div
        className={`absolute inset-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent transition-opacity duration-300 ${
          chromeVisible ? "opacity-100" : "opacity-0"
        }`}
      />

      {/* ---- 进度条上方这一行：左边时间，右边横屏键，两端对齐 ----
          横屏不跟字幕/设置放一起：那两个是「调这一路播放怎么放」，横屏是
          「把画面铺满整块屏幕」，属于跟时间同级的观看形态。放在这一行还有
          个实际好处——它和右下角的切集胶囊隔着进度条，不会误按。
          两边高度取同一档，左右才真的对称。 */}
      <div
        className={`relative flex items-center justify-between px-6 pt-24 pb-2 transition-opacity duration-300 max-md:px-3 max-md:pt-16 ${
          chromeVisible ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        {/* 两段各自成元素、靠 gap 分开：药丸是 flex，写在文字里的前导空格会
            被折掉，变成「41:00/ 2:32:00」 */}
        <span className="player-glass inline-flex h-9 items-center gap-1 rounded-full px-3.5 text-[13px] tabular-nums text-white/90 max-md:h-11 max-md:text-[12px]">
          <span>{formatClock(shown)}</span>
          <span className="text-white/45">/ {durationMs ? formatClock(durationMs) : "--:--"}</span>
        </span>

        <IconButton
          glass
          tip={canRotate ? (landscape ? "退出横屏" : "横屏") : landscape ? "退出全屏" : "全屏"}
          onClick={onToggleLandscape}
        >
          {canRotate ? (
            <RotateGlyph active={landscape} />
          ) : landscape ? (
            <ShrinkIcon className={ICON} />
          ) : (
            <ExpandIcon className={ICON} />
          )}
        </IconButton>
      </div>

      {/* ---- 进度条 ----
          控制条收起时下面那一行整体塌成 0 高，进度条自然落到播放器最底边，
          只剩一条贴边的细线；展开时它回到操作区上方。 */}
      <div
        className={`player-scrub-row pointer-events-auto relative transition-[padding] duration-300 ${
          chromeVisible ? "px-6 max-md:px-3" : "px-0"
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
          <div className="player-scrub-track pointer-events-none absolute inset-x-0 top-1/2 h-[3px] -translate-y-1/2 overflow-hidden rounded-full bg-[var(--player-track)] transition-[height] duration-150 [.player-scrub-row:hover_&]:h-[5px]">
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

      {/* ---- 操作区：进度条**下方**，左卡片是本片的操作、右胶囊是去下一集 ----
          用 grid-rows 0fr→1fr 收起而不是定死高度：卡片高度会随字号、断点变，
          写死的高度迟早对不上，收起时留一条空白或把内容切掉半截。 */}
      <div
        className={`grid transition-[grid-template-rows] duration-300 ${
          chromeVisible ? "grid-rows-[1fr]" : "grid-rows-[0fr]"
        }`}
      >
        {/* 收起动画靠这层裁剪；但菜单是**向上**弹的、整个落在这个盒子外面，
            裁着就等于点了没反应。菜单展开时控制条被 chromeMustStayVisible 顶住
            不会收，也就不存在「一边收起一边要显示菜单」的冲突，所以这时候可以
            安全地放开裁剪。 */}
        <div className={menu === "none" ? "overflow-hidden" : ""}>
          <div
            className={`relative flex items-center px-6 pb-4 pt-3 transition-opacity duration-300 max-md:px-3 max-md:pb-3 ${
              chromeVisible ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
            }`}
          >
            <div className="player-glass flex items-center gap-1 rounded-full px-1.5 py-1">
              <div className="relative">
                <IconButton
                  tip="字幕"
                  active={Boolean(selectedSubtitle)}
                  open={menu === "subtitles"}
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
                  open={menu === "settings"}
                  onClick={() => openMenu(menu === "settings" ? "none" : "settings")}
                >
                  <GearIcon className={ICON} />
                </IconButton>
                {menu === "settings" ? (
                  <MenuPanel title="设置" onClose={() => openMenu("none")}>
                    <MenuItem
                      active={diagnosticsOpen}
                      icon={<ActivityIcon className="size-4 shrink-0" />}
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
            </div>

            <div className="flex-1" />

            {/* 右下角切集位。剧集才有：「已完结」这句话对电影是错的，
                而电影本来也没有别的东西会因为这个位空着而移位。 */}
            {isSeries ? (
              onNext ? (
                <button
                  type="button"
                  onClick={onNext}
                  className="player-glass flex items-center gap-2 rounded-full px-4 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-white/20 max-md:px-3 max-md:py-2 max-md:text-[13px]"
                >
                  下一集
                  <NextGlyph />
                </button>
              ) : (
                <span className="player-glass rounded-full px-4 py-2.5 text-[14px] text-white/40 max-md:px-3 max-md:py-2 max-md:text-[13px]">
                  已完结
                </span>
              )
            ) : null}
          </div>
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

/**
 * 控制条上的图标按钮：换底色 + 上方说明气泡，尺寸与全站顶栏控件一致
 * （样式在 globals.css 的 .player-btn）。
 *
 * `glass` 是给**单独浮在画面上**的键用的（横屏键）：它不在那张磨砂卡片里，
 * 得自己带一层玻璃底，否则会直接糊进画面。卡片里的键不能开这个开关——
 * 那会变成「玻璃里的玻璃」。
 */
function IconButton({
  tip,
  active,
  open,
  glass,
  onClick,
  children,
}: {
  tip: string;
  /**
   * 这个功能**当前是开着的**（比如字幕已选中某一轨）。
   *
   * 与 `open` 分开是必须的：一个是「功能开没开」，一个是「菜单展没展」，
   * 合成一个的后果是关掉字幕后只要菜单还开着，按钮看起来仍然「亮着」。
   */
  active?: boolean;
  /** 这个按钮的菜单正展开着 */
  open?: boolean;
  glass?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={tip}
      aria-expanded={open === undefined ? undefined : open}
      data-tip={tip}
      data-active={active ? "true" : undefined}
      data-open={open ? "true" : undefined}
      className={`player-btn player-tip size-9 shrink-0 max-md:size-11 ${
        glass ? "player-glass player-btn--glass" : ""
      }`}
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
      // bottom-full + mb-8：底边落在按钮上方 32px，正好越过操作行的上内边距
      // （pt-3）与进度条那一行。用相对量而不是写死像素——按钮在移动端会从 36
      // 变 44，写死的偏移在两个断点上必然有一个不对。
      className="absolute bottom-full left-0 mb-8 w-[300px] rounded-sm border border-white/15 bg-[rgba(20,20,20,0.94)] py-2 text-[14px] shadow-[0_8px_32px_rgba(0,0,0,0.7)] backdrop-blur-sm"
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
