"use client";

import { useEffect, useRef, useState } from "react";
import { ActivityIcon, CheckIcon, ExpandIcon, GearIcon, ShrinkIcon } from "@/components/icons";
import type { AudioOption } from "@/lib/player/audio-tracks";
import { SUBTITLE_OFFSET_STEP, clampSubtitleOffset } from "@/lib/player/subtitles";
import { QUALITY_OPTIONS } from "@/lib/player/quality";
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
 * - **进度条**：贴播放器**最底边**，与控制条同显同隐（Netflix 派全出全收，
 *   取舍记录见 docs/design/web-player.md §6）。
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

/**
 * 退/进十秒：细描边圆弧 + 顶部箭头 + 居中的 10，YouTube / Material 同款。
 *
 * 两个几何要点，都是返工换来的：
 * - 弧用描边而不是填充环：环带会把内腔挤到数字装不下，10 直接压在弧上；
 * - 箭头必须贴在弧的**正顶部**——只有那里切线是水平的，水平三角形才能
 *   与弧自然顺接；放在别处就是一个歪着的钩子。
 */
function SkipGlyph({ forward }: { forward: boolean }) {
  // 弧心 (12,12.5)、半径 8。一端在正顶部 (12,4.5)，另一端留 60° 缺口——
  // 箭头要盖掉顶端一段，缺口小了箭头尖会怼上弧尾，圆环看起来是闭合的。
  return (
    <svg viewBox="0 0 24 24" className="size-9 fill-current max-md:size-8" aria-hidden>
      <path
        d={forward ? "M12 4.5A8 8 0 1 0 18.93 8.5" : "M12 4.5A8 8 0 1 1 5.07 8.5"}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      {/* 箭头：盖在弧顶端点上，指向行进方向 */}
      {forward ? (
        <path d="M11.7 1.9v5.2l4.6-2.6Z" />
      ) : (
        <path d="M12.3 1.9v5.2L7.7 4.5Z" />
      )}
      <text
        x="12"
        y="15"
        textAnchor="middle"
        fontSize="7"
        fontWeight="600"
        fill="currentColor"
        stroke="none"
      >
        10
      </text>
    </svg>
  );
}

/** 音轨：一个音符。站内没有这个图标，按同一套描边规格画。 */
function AudioGlyph() {
  return (
    <StrokeIcon>
      <path d="M9 18V5.4l10-2v12.2" />
      <circle cx="6.4" cy="18" r="2.6" />
      <circle cx="16.4" cy="15.6" r="2.6" />
    </StrokeIcon>
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

/**
 * 横屏：一条环绕的大弧箭头 + 一台设备（Material screen_rotation_alt 的
 * 构图），站内没有这个图标，按同一套描边规格画。设备的方向画的是**点下去
 * 之后**的样子——图标表示结果而不是现状，否则用户要在脑子里做一次取反。
 * 弧和箭头必须占到图标的一半以上：小弧挤在角落里 36px 下根本读不出旋转。
 */
function RotateGlyph({ active }: { active: boolean }) {
  return (
    <StrokeIcon>
      {active ? (
        <>
          <path d="M20.5 11.5A8 8 0 0 0 12.5 3.5" />
          <path d="m15.2 1.9-2.7 1.6 1.6 2.7" />
          <rect x="3" y="8.5" width="9.5" height="12.5" rx="2" />
        </>
      ) : (
        <>
          <path d="M3.5 11.5A8 8 0 0 1 11.5 3.5" />
          <path d="m8.8 1.9 2.7 1.6-1.6 2.7" />
          <rect x="8.5" y="11" width="12.5" height="9.5" rx="2" />
        </>
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
  /** 控制条是否可见。进度条与其它控件一起淡入淡出（全出全收） */
  chromeVisible: boolean;
  onSeek: (fileMs: number) => void;
  subtitles: SubtitleTracks;
  selectedSubtitle: string | null;
  onSelectSubtitle: (ref: string | null) => void;
  /** 可选音轨。**少于两条时为空数组**，那时整个按钮都不出现——没得选的菜单是噪音 */
  audioOptions: AudioOption[];
  /** 当前音轨；null = 还没表态，由服务端自动选 */
  selectedAudio: string | null;
  onSelectAudio: (ref: string) => void;
  subtitleStyle: SubtitleStyle;
  onSubtitleStyleChange: (style: SubtitleStyle) => void;
  diagnosticsOpen: boolean;
  onToggleDiagnostics: () => void;
  /** 剧集才有右下角那个位；电影不显示（「已完结」对电影是错的说法） */
  isSeries: boolean;
  /** 有下一集时的回调；剧集但为 null = 本季到头了，那个位显示「已完结」 */
  onNext: (() => void) | null;
  /**
   * 有上一集时的回调；null = 已经是本季第一集（或往前没有在位文件）。
   *
   * 与 `onNext` 不同，没有上一集时**整颗按钮不出现**而不是留一个灰字：
   * 「已完结」是对剧集状态的陈述、用户需要知道，「没有上一集」则不是信息。
   */
  onPrev: (() => void) | null;
  /** 横屏（全屏 + 锁横向）；已经在里面时点它就是退出 */
  landscape: boolean;
  /** 触屏设备（手机/平板）：显示横屏按钮。桌面只有全屏按钮 */
  canRotate: boolean;
  onToggleLandscape: () => void;
  /** 当前在元素级全屏里 */
  fullscreen: boolean;
  onToggleFullscreen: () => void;
  /** 字幕由系统渲染（iOS 原生 HLS），字幕菜单隐藏无效的样式调节 */
  systemSubtitles: boolean;
  /** 画质上限（max_height）；null = 自动 */
  quality: number | null;
  onSelectQuality: (maxHeight: number | null) => void;
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
    audioOptions,
    selectedAudio,
    onSelectAudio,
    subtitleStyle,
    onSubtitleStyleChange,
    diagnosticsOpen,
    onToggleDiagnostics,
    isSeries,
    onNext,
    onPrev,
    landscape,
    canRotate,
    onToggleLandscape,
    fullscreen,
    onToggleFullscreen,
    systemSubtitles,
    quality,
    onSelectQuality,
    onMenuOpenChange,
    trickplay,
  } = props;

  // 拖动中的本地值：直接跟 positionMs 会被 timeupdate 反复拉回去，手感是
  // 滑块「粘手」——松手才提交是进度条唯一能用的做法
  const [dragging, setDragging] = useState<number | null>(null);
  const [menu, setMenu] = useState<"none" | "audio" | "subtitles" | "settings">("none");
  // 悬停预览的位置（文件毫秒 + 进度条内的像素横坐标）。null = 没在悬停
  const [hover, setHover] = useState<{ ms: number; x: number } | null>(null);
  const shown = dragging ?? positionMs;
  const previewTile = hover ? tileAt(trickplay, hover.ms) : null;
  const progress = durationMs ? Math.min(100, (shown / durationMs) * 100) : 0;
  const buffered =
    durationMs && bufferedEndMs ? Math.min(100, (bufferedEndMs / durationMs) * 100) : 0;

  const openMenu = (next: "none" | "audio" | "subtitles" | "settings") => {
    setMenu(next);
    onMenuOpenChange(next !== "none");
  };

  /**
   * 操作区展开动画**已经放完**。
   *
   * 只用来决定要不要继续裁剪那一层（见下方 grid 收起处的注释）：展开途中
   * 必须裁，否则卡片会以完整高度探出播放器底边再被拉回去；完全展开之后必须
   * 放开，否则按钮上方的说明气泡（.player-tip，冒在按钮上方 32px 处）会被
   * 这层裁掉一半——字幕/设置那几颗键的气泡看不见就是这么来的。
   */
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    if (!chromeVisible) {
      setExpanded(false);
      return;
    }
    // 比 duration-300 略长一点，等动画真的落地
    const timer = window.setTimeout(() => setExpanded(true), 320);
    return () => window.clearTimeout(timer);
  }, [chromeVisible]);

  return (
    // pb 让开 Home 指示条：进度条「贴最底边」在有指示条的设备上指的是安全区
    // 下沿——压到指示条底下既看不见也会跟上滑手势打架（iOS 标准播放器同此）
    <div className="pointer-events-none relative pb-[var(--safe-bottom)]">
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
        // 行容器**永远 pointer-events-none**，命中权在右侧按钮组那个子元素上：
        // pt-24 那截透明内边距只是撑视觉间距，但挂上 auto 它就会吃掉底下的
        // 点击——横屏只有 320~390pt 高，这截正好罩在中央簇的退十秒按钮上，
        // 按钮看得见按不动（层级在下、命中被这行截胡）。
        className={`player-inset-x pointer-events-none relative flex items-center justify-between pt-24 pb-2 transition-opacity duration-300 max-md:pt-16 ${
          chromeVisible ? "opacity-100" : "opacity-0"
        }`}
      >
        {/* 两段各自成元素、靠 gap 分开：药丸是 flex，写在文字里的前导空格会
            被折掉，变成「41:00/ 2:32:00」 */}
        <span className="player-glass inline-flex h-9 items-center gap-1 rounded-full px-3.5 text-[13px] tabular-nums text-white/90 max-md:h-11 max-md:text-[12px]">
          <span>{formatClock(shown)}</span>
          <span className="text-white/45">/ {durationMs ? formatClock(durationMs) : "--:--"}</span>
        </span>

        {/* 横屏管方向、全屏管铺满——真横屏会顺带进全屏，此时全屏键自然
            成为退出键。iPhone 没有元素级全屏，全屏键走系统原生播放器，
            字幕靠 video 上的原生 VTT 轨跟进去（见 video-player 的 pip 轨） */}
        <div className={`flex items-center gap-2 ${chromeVisible ? "pointer-events-auto" : ""}`}>
          {canRotate ? (
            <IconButton
              glass
              tip={landscape ? "退出横屏" : "横屏"}
              onClick={onToggleLandscape}
            >
              <RotateGlyph active={landscape} />
            </IconButton>
          ) : null}
          <IconButton
            glass
            tip={fullscreen ? "退出全屏" : "全屏"}
            onClick={onToggleFullscreen}
          >
            {fullscreen ? <ShrinkIcon className={ICON} /> : <ExpandIcon className={ICON} />}
          </IconButton>
        </div>
      </div>

      {/* ---- 进度条 ----
          与其它控件同一个显隐语义：控制层收起时整条淡出（曾走 YouTube 手机端
          的「收起留 3px 细线」，实际反馈是显隐不一致、读不出点击切换了什么，
          2026-08-25 拍板改 Netflix 派的全出全收——一致、可预期优先）。
          pointer-events-none 必须跟着：透明但可拖的进度条会把「点屏幕下缘
          唤出控制层」截胡成一次误 seek。 */}
      <div
        className={`player-scrub-row player-scrub-inset relative transition-opacity duration-300 ${
          chromeVisible ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <div
          className="player-scrub-shade relative h-5"
          onPointerMove={(e) => {
            if (!durationMs) return;
            const rect = e.currentTarget.getBoundingClientRect();
            const x = Math.min(Math.max(e.clientX - rect.left, 0), rect.width);
            setHover({ ms: (x / rect.width) * durationMs, x });
          }}
          onPointerLeave={() => setHover(null)}
        >
          {/* 缩略图预览：拖进度条时能看见画面。没生成好就只剩时间戳，
              不影响拖动——预览是锦上添花，时间戳是刚需。
              触屏抬高一档（bottom-8 → bottom-16）：32px 的间距正好被手指
              压着下缘，64px 让预览完整露在指尖上方。 */}
          {hover ? (
            <div
              className="pointer-events-none absolute bottom-8 -translate-x-1/2 pointer-coarse:bottom-16"
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
                  className="rounded-[10px] shadow-[0_10px_28px_rgba(0,0,0,0.55)] ring-1 ring-white/30"
                />
              ) : null}
              <p className="mt-1.5 text-center text-[13px] font-medium tabular-nums text-white drop-shadow">
                {formatClock(hover.ms)}
              </p>
            </div>
          ) : null}

          {/* 轨道三段，亮度一路递增：未播（半透白，自己在暗画面上就读得出来，
              不靠已播段衬）→ 已缓冲 → 已播（冷银）。亮画面上的对比度由外层
              .player-scrub-shade 那条随身暗渐变兜底，见 globals.css。
              静止 3px、悬停 5px，Netflix 的细红线就是这个手感 */}
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
            // 拖拽不走 range 的原生行为，用指针事件自己算：iOS 只有按中
            // **原生把手**才进入连续拖拽，而那个把手被缩到 1px 藏起来了
            // （见下方圆点注释），手指永远按不中——表现为拖动时圆点不跟手、
            // 松手 seek 到的是按下点。setPointerCapture 让移出条外也不断跟。
            onPointerDown={(e) => {
              if (!durationMs) return;
              e.currentTarget.setPointerCapture(e.pointerId);
              const rect = e.currentTarget.getBoundingClientRect();
              const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
              setDragging(Math.round(ratio * durationMs));
            }}
            onPointerMove={(e) => {
              if (dragging === null || !durationMs) return;
              const rect = e.currentTarget.getBoundingClientRect();
              const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
              setDragging(Math.round(ratio * durationMs));
            }}
            onPointerUp={() => {
              if (dragging !== null) onSeek(dragging);
              setDragging(null);
            }}
            onKeyUp={() => {
              if (dragging !== null) onSeek(dragging);
              setDragging(null);
            }}
            // 触屏把命中带加高到 44px（Apple HIG 的最小触控目标）：视觉上还是
            // 那条细线，但手指按在线的上下 20px 内都算按中了——竖屏上「滑不准、
            // 按不中」的直接解法。桌面维持 20px，不跟鼠标抢悬停区。
            // 收起态整行 pointer-events-none，命中带不用再单独收。
            className="player-scrub absolute inset-x-0 top-1/2 h-5 w-full -translate-y-1/2 cursor-pointer touch-none appearance-none bg-transparent disabled:cursor-default pointer-coarse:h-11"
          />

          {/* 把手自己画，不用 input 原生的那个。
              原生把手在 `宽度 - 把手宽` 的范围里走：0% 时它的圆心在左边缘往里
              半个把手，100% 时往里半个把手，而我们画的已播段是按整条宽度铺的，
              两者只有在正中才对得上，两端各差半个把手（14px 的把手就是 7px）。
              这就是「圆点没对齐进度」的来源。把原生把手缩到 1px 隐藏掉，改成
              按 `left: 进度%` 定位一个自己的圆点，两者从此永远同一个位置；
              1px 的把手同时也让指针位置到时间的换算变成精确的线性映射。 */}
          {/* 触屏没有悬停：圆点在控制条露出时**常显**（YouTube 手机端同款），
              不然「拖拽那个点」根本无从下手——用户不知道该按哪里；拖动中再
              放大一号，指下有反馈。桌面维持悬停才现，不挡画面。 */}
          <div
            className={`pointer-events-none absolute top-1/2 size-[14px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--player-thumb)] shadow-[0_0_0_4px_var(--accent-soft)] transition-transform duration-150 pointer-coarse:size-[18px] ${
              durationMs
                ? dragging !== null
                  ? "scale-100 pointer-coarse:scale-110"
                  : // 收起态整行 opacity-0，触屏常显不用再按 chromeVisible 分岔
                    "scale-0 [.player-scrub-row:hover_&]:scale-100 pointer-coarse:scale-100"
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
        {/* 收起动画靠这层裁剪；但**向上冒的东西全都落在这个盒子外面**——菜单
            （bottom-full）与按钮说明气泡（.player-tip）都是，裁着就等于菜单点了
            没反应、气泡只露出一角。
            只在「收起中/已收起」时裁：展开动画途中不裁，卡片会以完整高度探出
            播放器底边再被拉回来；展开落地之后不放，气泡与菜单就永远露不出来。
            菜单展开时控制条被 chromeMustStayVisible 顶住不会收，所以那时无论
            动画走到哪一步都可以安全放开。 */}
        <div className={menu === "none" && !expanded ? "overflow-hidden" : ""}>
          <div
            className={`player-inset-x relative flex items-center pb-4 pt-3 transition-opacity duration-300 max-md:pb-3 ${
              chromeVisible ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
            }`}
          >
            <div className="player-glass flex items-center gap-1 rounded-full px-1.5 py-1">
              {/* 音轨排在字幕左边：多音轨片子里「先挑语言、再挑字幕」是自然顺序。
                  只有一条轨时 audioOptions 为空，整个按钮不出现。 */}
              {audioOptions.length > 0 ? (
                <div className="relative">
                  <IconButton
                    tip="音轨"
                    open={menu === "audio"}
                    onClick={() => openMenu(menu === "audio" ? "none" : "audio")}
                  >
                    <AudioGlyph />
                  </IconButton>
                  {menu === "audio" ? (
                    <MenuPanel title="音轨" onClose={() => openMenu("none")}>
                      {audioOptions.map((option) => (
                        <MenuItem
                          key={option.ref}
                          active={
                            option.ref === selectedAudio ||
                            (selectedAudio === null && option.isDefault)
                          }
                          onClick={() => {
                            onSelectAudio(option.ref);
                            openMenu("none");
                          }}
                        >
                          {option.label}
                        </MenuItem>
                      ))}
                      <p className="mt-1 border-t border-white/[0.08] px-3 pb-1 pt-2 text-[12px] leading-relaxed text-white/40">
                        换音轨需要重新起流，会从当前位置续上，中间大约停顿一秒。
                      </p>
                    </MenuPanel>
                  ) : null}
                </div>
              ) : null}

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
                    systemRendered={systemSubtitles}
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
                    {/* 画质：语义是上限——源不超所选档就照常直通（无损），
                        超了才转码降下去。弱网选低档换低带宽（§10）。 */}
                    <div className="px-4 pb-1 pt-0.5 text-[12px] font-medium text-white/45">画质</div>
                    {QUALITY_OPTIONS.map((option) => (
                      <MenuItem
                        key={option.label}
                        active={quality === option.maxHeight}
                        onClick={() => {
                          onSelectQuality(option.maxHeight);
                          openMenu("none");
                        }}
                      >
                        {option.label}
                        {option.hint ? (
                          <span className="ml-2 text-[12px] text-white/40">{option.hint}</span>
                        ) : null}
                      </MenuItem>
                    ))}
                    <div className="my-1.5 h-px bg-white/10" />
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
                而电影本来也没有别的东西会因为这个位空着而移位。

                上一集恒在下一集左边（包括本季放到头、右边是「已完结」的
                时候）——切集是双向的，只给单向会逼用户退回详情页点集。

                纯文字胶囊：中文标签已把方向说全，箭头小图标是冗余装饰，
                去掉后与「已完结」（本就无图标）风格统一。 */}
            {isSeries ? (
              <div className="flex items-center gap-2 max-md:gap-1.5">
                {onPrev ? (
                  <button
                    type="button"
                    onClick={onPrev}
                    className="player-glass flex items-center rounded-full px-4 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-white/20 max-md:px-3 max-md:py-2 max-md:text-[13px]"
                  >
                    上一集
                  </button>
                ) : null}
                {onNext ? (
                  <button
                    type="button"
                    onClick={onNext}
                    className="player-glass flex items-center rounded-full px-4 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-white/20 max-md:px-3 max-md:py-2 max-md:text-[13px]"
                  >
                    下一集
                  </button>
                ) : (
                  <span className="player-glass rounded-full px-4 py-2.5 text-[14px] text-white/40 max-md:px-3 max-md:py-2 max-md:text-[13px]">
                    已完结
                  </span>
                )}
              </div>
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
      // z-20：要压在诊断面板（z-10）**之上**。手机横屏只有 320~390pt 高，
      // 面板再怎么摆都会与中央簇相交；YouTube 的处理就是传输控件永远画在
      // Stats for nerds 上层——面板是被动读数，播放/退进十秒不能被它埋掉。
      className={`pointer-events-none absolute inset-0 z-20 flex items-center justify-center gap-14 transition-opacity duration-300 max-md:gap-10 ${
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
  systemRendered,
  onClose,
}: {
  tracks: SubtitleTracks;
  selected: string | null;
  onSelect: (ref: string | null) => void;
  style: SubtitleStyle;
  onStyleChange: (style: SubtitleStyle) => void;
  /** 字幕由系统渲染（iOS 原生 HLS）：样式与时间轴调节不经我们的手，全部隐藏 */
  systemRendered: boolean;
  onClose: () => void;
}) {
  return (
    <MenuPanel title="字幕" onClose={onClose}>
      <div className="scroll-thin max-h-[240px] overflow-y-auto">
        {/* 选完即关（音轨/画质菜单同款）：换轨是一次决定，不是连续调节；
            要调时间轴/样式的用户重新打开菜单，此时样式区因已选中而展开 */}
        <MenuItem
          active={selected === null}
          onClick={() => {
            onSelect(null);
            onClose();
          }}
        >
          关闭
        </MenuItem>
        {tracks.options.map((option) => (
          <MenuItem
            key={option.ref}
            active={selected === option.ref}
            badge={option.isAi ? <AiChip /> : null}
            onClick={() => {
              onSelect(option.ref);
              onClose();
            }}
          >
            {option.label}
          </MenuItem>
        ))}
        {tracks.unavailable.map((item) => (
          <div key={item.ref} className="px-4 py-1.5 text-white/35">
            <div className="truncate">{item.label}</div>
            <div className="text-[12px] leading-snug">{item.reason}</div>
          </div>
        ))}
        {tracks.options.length === 0 && tracks.unavailable.length === 0 ? (
          <div className="px-4 py-2 text-white/45">这个文件没有可用字幕</div>
        ) : null}
      </div>

      {tracks.options.some((option) => option.kind === "pgs") ? (
        // 用户点之前就该知道代价：选图形字幕会换成转码播放（约一秒切换），
        // 换来的是画中画/投屏里也带字幕——与 Emby 的「字幕压制」同语义
        <p className="mt-2 border-t border-white/10 px-4 pt-2.5 text-[12px] leading-relaxed text-white/55">
          图形字幕会转码压制进画面（切换约一秒），画中画等场景也能看到
        </p>
      ) : null}

      {selected && systemRendered ? (
        // 调了没反应比没有选项更糟——iOS 上系统渲染字幕，样式跟随系统的
        // 辅助功能设置，时间轴微调也不经过我们，如实告知去哪调
        <p className="mt-2 border-t border-white/10 px-4 pt-2.5 text-[12px] leading-relaxed text-white/55">
          字幕由 iOS 系统渲染，样式在系统设置 → 辅助功能 → 字幕与隐藏式字幕中调整
        </p>
      ) : null}
      {selected && !systemRendered ? (
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
      //
      // 外观与诊断面板同一套语言（YouTube 播放器菜单同款取舍）：
      // - **一块半透明的黑（bg-black/85），不用站内的磨砂玻璃**。磨砂的
      //   backdrop-filter 叠在视频上每帧都要重采样模糊，是掉帧大户
      //   （globals 的 QoE 注释）；且播放器里已经有诊断面板/调节胶囊两个
      //   同语言的浮层，菜单跟站内玻璃反而是异类。
      // - 行是**通宽命中**（YouTube/Netflix 菜单都不给行画圆角胶囊），
      //   面板自己 overflow-hidden 让首尾行贴住 14px 圆角。
      // - 进场与调节胶囊同一个 0.16s 动画，origin 指向锚点按钮那一角。
      className="player-flash-in absolute bottom-full left-0 mb-8 w-[300px] origin-bottom-left overflow-hidden rounded-[14px] bg-black/85 py-2 text-[13px] shadow-[0_18px_44px_rgba(0,0,0,0.5)]"
    >
      <p className="px-4 pb-1.5 pt-0.5 text-[12px] font-semibold text-white/55">{title}</p>
      {children}
    </div>
  );
}

function MenuItem({
  active,
  icon,
  badge,
  onClick,
  children,
}: {
  active: boolean;
  /** 可选的行首小图标。必须与文字**并列**，不能塞进 truncate 的 span 里——
   *  Tailwind preflight 把 svg 设成 display:block，塞进去会自己换一行。 */
  icon?: React.ReactNode;
  /** 可选的行尾标记（AI 生成…）。靠右贴边，长文案截断时它不跟着被切掉 */
  badge?: React.ReactNode;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      // 选中态照 YouTube/Netflix：**只用行尾对勾说话**，不给选中行铺高亮底
      // ——菜单里同时存在 hover 高亮时，两种高亮叠在一起分不清哪个是选中、
      // 哪个只是鼠标路过。行通宽、无圆角，与面板的黑色语言一体。
      className={`flex w-full cursor-pointer items-center gap-2.5 px-4 py-2 text-left font-medium transition-colors hover:bg-white/10 ${
        active ? "text-white" : "text-white/80 hover:text-white"
      }`}
    >
      {icon}
      <span className="truncate">{children}</span>
      <span className="ml-auto flex shrink-0 items-center gap-2">
        {badge}
        {active ? <CheckIcon className="size-4 text-white" /> : null}
      </span>
    </button>
  );
}

/**
 * 「AI 生成」标记。
 *
 * 值得一个渐变胶囊 + 扫光（样式在 globals.css 的 .ai-chip）：AI 字幕的译文与
 * 时间轴都可能有偏差，而它在菜单里就挤在发行方字幕中间，一行灰白小字根本
 * 分不出来。用户有权在**选中之前**就知道这条是机器产的。
 */
function AiChip() {
  return (
    <span className="ai-chip">
      {/* 四角星：业界通用的「AI」手势（站内 WandIcon 在 10px 下糊成一团） */}
      <svg viewBox="0 0 24 24" className="size-[9px] fill-current" aria-hidden>
        <path d="M12 1.5c.9 4.6 2.9 6.7 8 7.6-5.1.9-7.1 3-8 7.6-.9-4.6-2.9-6.7-8-7.6 5.1-.9 7.1-3 8-7.6Z" />
        <path d="M18.6 15c.45 2.3 1.45 3.35 4 3.8-2.55.45-3.55 1.5-4 3.8-.45-2.3-1.45-3.35-4-3.8 2.55-.45 3.55-1.5 4-3.8Z" />
      </svg>
      AI 生成
    </span>
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
      className="size-6 rounded-[7px] border border-white/10 bg-white/[0.06] leading-none text-white/85 transition-colors hover:bg-white/[0.14] hover:text-white"
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
      // 两态都带 border 占位，否则开关瞬间会差 1px 抖一下
      className={`rounded-full border px-3 py-1 text-[12px] font-medium transition-colors ${
        on
          ? "border-transparent bg-[var(--player-accent)] text-black"
          : "border-white/10 bg-white/[0.06] text-white/65 hover:bg-white/[0.14] hover:text-white"
      }`}
    >
      {children}
    </button>
  );
}
