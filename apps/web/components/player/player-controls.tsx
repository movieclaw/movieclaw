"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ActivityIcon, CheckIcon, ExpandIcon, MoreIcon, ShrinkIcon } from "@/components/icons";
import type { PlaybackChapterMark } from "@/lib/api/playback";
import type { AudioOption } from "@/lib/player/audio-tracks";
import { SUBTITLE_OFFSET_STEP, clampSubtitleOffset } from "@/lib/player/subtitles";
import { QUALITY_OPTIONS } from "@/lib/player/quality";
import type { SubtitleStyle, SubtitleTracks } from "@/lib/player/subtitles";
import { pointerOffsetX } from "@/lib/player/touch-adjust";
import { formatClock, progressRatio, shownPositionMs, toFileMs } from "@/lib/player/timeline";
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

/**
 * 功能键图标尺寸：与 page-nav 的顶栏控件一致。
 *
 * **放大的判据是 `pointer-coarse`（手指），不是 `max-md`（窄视口）**——播放器里
 * 所有控件的尺寸分档都照这条。两者在竖屏手机上恰好同时成立，看不出区别，但在
 * **横屏手机**上会分家：iPhone 横屏是 844/852/932 宽，越过了 md 断点，按视口
 * 分档就把命中区从 44 降到 36，低于 HIG 的 44pt 最小值（Material 是 48dp）——
 * 而横屏正是看片的主要姿势，iPad 更是全程落在这一档。手指的大小跟屏幕转没转
 * 没有关系。
 *
 * 进度条的命中带（.player-scrub 的 `pointer-coarse:h-11`）本来就是这么判的，
 * 按钮这边曾用 `max-md`，于是同一条控制条里两套判据：横屏下进度条给足 44、
 * 旁边的按钮只有 36。2026-09-09 统一到手指这一条。
 */
const ICON = "size-[18px] pointer-coarse:size-[22px]";


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
  const cls = "size-[52px] fill-current pointer-coarse:size-11";
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
    <svg viewBox="0 0 24 24" className="size-9 fill-current pointer-coarse:size-8" aria-hidden>
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
  /**
   * 播放位置（文件毫秒）的**状态**值，跟着 `timeupdate` 走（约 4Hz）。
   *
   * 它只负责时间文字与静止态的进度条；**播放中进度条的位置不看它**——
   * 4Hz 会让圆点每 250 毫秒跳一格。见下面 `video` 的说明。
   */
  positionMs: number;
  /**
   * 正在播的 video 元素。进度条据此每帧自绘位置（见 paint effect）。
   *
   * 传元素而不是位置数字，是因为 60fps 的位置更新不能走 React state：
   * 这个组件重，每帧 setState 会把整条控制条重渲染 60 次。
   */
  video: HTMLVideoElement | null;
  /** 时间轴参照点（会话相对制的 start_ms；VOD/直通恒为 0）。文件时间 =
   * startMs + video.currentTime × 1000，与 timeline.ts 的 toFileMs 同式 */
  startMs: number;
  /**
   * 覆盖位置：横滑拖进度的落点、连按快进的累积落点。非 null 时进度条与
   * 时间文字都显示它而不是真实播放位置——**屏幕上任何时刻只能有一个读数**
   * （2026-09-08 真机反馈：胶囊报 9:58、进度条停在 19:00）。
   */
  overrideMs: number | null;
  /** 片长（文件时间）。服务端算不出时为 null，此时进度条只显示已播时间 */
  durationMs: number | null;
  /** 当前会话已缓冲到的文件位置，用于进度条的浅色底 */
  bufferedEndMs: number | null;
  /** 控制条是否可见。进度条与其它控件一起淡入淡出（全出全收） */
  chromeVisible: boolean;
  onSeek: (fileMs: number) => void;
  /**
   * 拖动过程中的实时跟随。父组件自己判断这次跳转值不值得做（跳转不要钱的
   * 直通/已缓冲区间才跟随，转码会话拖出缓冲要换会话，一路拖过去就是连着
   * 捅十几刀），这里只管把落点递过去。
   */
  onScrub: (fileMs: number) => void;
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
  /** 章节刻度（文件毫秒）。空表 = 这个文件没有内嵌章节，轨道保持干净 */
  chapters: PlaybackChapterMark[];
  /**
   * iOS 伪横屏（整个容器 rotate(90deg)）。
   *
   * 进度条上所有「指针位置 → 时间」的换算都要知道它：转过来之后元素的
   * 布局 x 轴沿物理 y 轴，`rect.width` 量到的是条的厚度而不是长度
   * （换算见 touch-adjust.ts 的 pointerOffsetX）。
   */
  fakeLandscape: boolean;
}

export function PlayerControls(props: PlayerControlsProps) {
  const {
    positionMs,
    video,
    startMs,
    overrideMs,
    durationMs,
    bufferedEndMs,
    chromeVisible,
    onSeek,
    onScrub,
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
    chapters,
    fakeLandscape,
  } = props;

  // 拖动中的本地值：直接跟 positionMs 会被 timeupdate 反复拉回去，手感是
  // 滑块「粘手」——松手才提交是进度条唯一能用的做法
  const [dragging, setDragging] = useState<number | null>(null);
  // 片长转为未知（换会话的空档里 durationMs 会短暂变 null）会让下面的 input
  // 变成 disabled，而 disabled 的元素**不再收到 pointerup/pointercancel**——
  // 正拖着的那次手势就此没了收尾，dragging 会永远钉在最后一个拖动值上：
  // 进度条和时间读数从此不跟画面走，画面照常播，两处读数各说各话
  // （2026-09-08 反馈）。片长一没就地清掉，退回 positionMs 这个真值。
  useEffect(() => {
    if (!durationMs) setDragging(null);
  }, [durationMs]);
  const [menu, setMenu] = useState<"none" | "audio" | "subtitles" | "settings">("none");
  // 悬停预览的位置（文件毫秒 + 进度条内的像素横坐标）。null = 没在悬停
  const [hover, setHover] = useState<{ ms: number; x: number } | null>(null);
  /** 时间文字用的位置。取值规则与进度条自绘**同一个函数**，见 shownPositionMs */
  const shown = shownPositionMs({
    draggingMs: dragging,
    overrideMs,
    // 文字是秒级读数，用不着每帧去读 video：4Hz 的状态值足够
    livePositionMs: null,
    positionMs,
  });
  const previewTile = hover ? tileAt(trickplay, hover.ms) : null;
  /** 刻度位置。0 秒那条不画——片头永远在最左端，画出来只是一条噪音 */
  const chapterMarks = useMemo(
    () =>
      durationMs
        ? chapters
            .filter((mark) => mark.start_ms > 0 && mark.start_ms < durationMs)
            .map((mark) => ({ start_ms: mark.start_ms, ratio: progressRatio(mark.start_ms, durationMs) }))
        : [],
    [chapters, durationMs],
  );
  /** 悬停/拖动位置落在哪一章：取最后一个起点不晚于它的章节 */
  const hoverChapter = useMemo(() => {
    if (!hover) return null;
    let title: string | null = null;
    for (const mark of chapters) {
      if (mark.start_ms > hover.ms) break;
      title = mark.title;
    }
    return title;
  }, [chapters, hover]);
  /**
   * 气泡贴边时夹回轨道内。
   *
   * 气泡是「按触点居中」的，拖到两端时有一半会探出播放器——缩略图被裁一半、
   * 章节名直接跑到画面外（加了章节名之后更宽，更明显）。ArtPlayer 与
   * jellyfin-web 都在这里夹一次，我们此前漏了。
   *
   * 夹的是渲染后的实际宽度，所以只能在布局阶段直接改 style，不走 state——
   * 用 state 会「渲染 → 量 → 再渲染」抖一帧。
   */
  const bubbleRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const bubble = bubbleRef.current;
    if (!bubble || !hover) return;
    const trackWidth = bubble.parentElement?.clientWidth ?? 0;
    const half = bubble.offsetWidth / 2;
    const max = Math.max(half, trackWidth - half);
    bubble.style.left = `${Math.min(Math.max(hover.x, half), max)}px`;
  }, [hover, previewTile, hoverChapter]);

  const buffered =
    durationMs && bufferedEndMs ? Math.min(100, (bufferedEndMs / durationMs) * 100) : 0;

  // ---------------------------------------------------------------------
  // 进度条自绘（docs/design/player-feel.md §2.A1）
  //
  // 已播段的宽度与圆点的位置**不走 React**：位置的唯一状态来源 `timeupdate`
  // 只有约 4Hz 且间隔不均，跟着它渲染就是「圆点每 250 毫秒跳一格」——这是
  // 「播放器不够丝滑」最主要的来源。改成 rAF 每帧直接读 video.currentTime
  // 写这两个元素的 style，React 那侧只留 4Hz 的时间文字。
  //
  // 每帧 setState 是不行的：这个组件带着菜单、缩略图、按钮簇，一秒重渲染
  // 60 次会把省下来的流畅又赔回去。
  // ---------------------------------------------------------------------
  const playedRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);
  /** 供 rAF 回调读最新值：跟着依赖重建循环会在播放中反复起停 */
  const paintInputRef = useRef({ video, startMs, durationMs, positionMs, dragging, overrideMs });
  paintInputRef.current = { video, startMs, durationMs, positionMs, dragging, overrideMs };

  const paint = useCallback(() => {
    const {
      video: el,
      startMs: origin,
      durationMs: total,
      positionMs: state,
      dragging: draggingMs,
      overrideMs: override,
    } = paintInputRef.current;
    if (!total) {
      // 片长未知（换会话的空档）时进度条是禁用态：**必须清零**而不是直接
      // 返回——留着上一路会话画的宽度，用户看到的是一条与新内容无关的进度。
      if (playedRef.current) playedRef.current.style.width = "0%";
      if (thumbRef.current) thumbRef.current.style.left = "0%";
      return;
    }
    // 取值规则与时间文字**同一个函数**，这里只多喂一路「正在播的真实位置」
    // ——它是唯一每帧都在变的来源。不可用（暂停 / seek 途中 / 换会话空档，
    // 那时 video 还挂着旧流）就传 null，由函数退回状态值。
    const live =
      el && !el.paused && !el.seeking && el.readyState >= 2
        ? toFileMs(el.currentTime, origin)
        : null;
    const ratio = progressRatio(
      shownPositionMs({ draggingMs, overrideMs: override, livePositionMs: live, positionMs: state }),
      total,
    );
    const percent = `${ratio * 100}%`;
    if (playedRef.current) playedRef.current.style.width = percent;
    if (thumbRef.current) thumbRef.current.style.left = percent;
  }, []);

  useEffect(() => {
    if (!video) {
      paint();
      return;
    }
    // 合帧：排下一帧前先撤掉上一帧，保证一帧最多写一次 DOM（emby-slider
    // 同款做法）。事件与 rAF 会在同一帧里同时要求重绘，不合帧就是重复布局。
    let frame = 0;
    let loop = 0;
    const schedule = () => {
      // 循环在跑时它这一帧本来就会画，再排一帧就是同一帧写两次
      if (loop) return;
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        frame = 0;
        paint();
      });
    };
    const tick = () => {
      paint();
      loop = requestAnimationFrame(tick);
    };
    const start = () => {
      if (!loop) loop = requestAnimationFrame(tick);
    };
    const stop = () => {
      if (loop) cancelAnimationFrame(loop);
      loop = 0;
      schedule();
    };
    // 只在真的在播时跑循环：暂停/缓冲/看不见的时候位置不动，空转 60 次/秒
    // 没有意义（页面切到后台时浏览器自己会停 rAF，这里管的是前台暂停）。
    video.addEventListener("playing", start);
    video.addEventListener("play", start);
    video.addEventListener("pause", stop);
    video.addEventListener("ended", stop);
    // 暂停态的位置变化（seek、换会话后的落点）靠这两个事件补画
    video.addEventListener("seeked", schedule);
    video.addEventListener("timeupdate", schedule);
    if (!video.paused) start();
    else schedule();
    return () => {
      video.removeEventListener("playing", start);
      video.removeEventListener("play", start);
      video.removeEventListener("pause", stop);
      video.removeEventListener("ended", stop);
      video.removeEventListener("seeked", schedule);
      video.removeEventListener("timeupdate", schedule);
      if (loop) cancelAnimationFrame(loop);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [video, paint]);

  // 拖动值、落点、片长、状态位置的每一次变化都要立刻见效：rAF 循环在暂停
  // 时是停的，只靠它这些变化会等到下一次播放才画出来。
  useEffect(paint, [paint, shown, positionMs, durationMs, startMs]);

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

      {/* ---- 进度条上方这一行：只有时间读数 ----
          横屏/全屏原本占着这行右端，2026-09-09 随切集位一起挪到了进度条下方
          （那里现在是左右两张同形制的按钮卡片）。这一行因此只剩一个读数，
          整行不吃指针事件。 */}
      <div
        // 行容器**永远 pointer-events-none**：pt-24 那截透明内边距只是撑视觉
        // 间距，挂上 auto 它就会吃掉底下的点击——横屏只有 320~390pt 高，这截
        // 正好罩在中央簇的退十秒按钮上，按钮看得见按不动（层级在下、命中被
        // 这行截胡）。现在这行只有读数，没有任何需要命中的东西。
        className={`player-inset-x pointer-events-none relative flex items-center pt-24 pb-2 transition-opacity duration-300 max-md:pt-16 ${
          chromeVisible ? "opacity-100" : "opacity-0"
        }`}
      >
        {/* 时间是**读数**，比进度条下方那排操作键明显矮一档（28 vs 44/52）：
            最不需要被点的东西不该看着最像能点的。也不跟着断点放大——那 44px
            是最小触控目标，给一个点不了的读数套触控尺寸纯属白占地方。
            主次靠颜色分：已播时间实白 + medium，总时长压到 40%。

            两段各自成元素、靠 gap 分开：药丸是 flex，写在文字里的前导空格会
            被折掉，变成「41:00/ 2:32:00」。 */}
        <span className="player-glass inline-flex h-7 items-center gap-1 rounded-full px-2.5 text-[12px] font-medium tabular-nums text-white">
          <span>{formatClock(shown)}</span>
          <span className="font-normal text-white/40">
            / {durationMs ? formatClock(durationMs) : "--:--"}
          </span>
        </span>
      </div>

      {/* ---- 进度条 ----
          与其它控件同一个显隐语义：控制层收起时整条淡出（曾走 YouTube 手机端
          的「收起留 3px 细线」，实际反馈是显隐不一致、读不出点击切换了什么，
          2026-08-25 拍板改 Netflix 派的全出全收——一致、可预期优先）。
          pointer-events-none 必须跟着：透明但可拖的进度条会把「点屏幕下缘
          唤出控制层」截胡成一次误 seek。 */}
      <div
        className={`player-scrub-row player-inset-x relative transition-opacity duration-300 ${
          chromeVisible ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <div
          className="player-scrub-shade relative h-5"
          onPointerMove={(e) => {
            if (!durationMs) return;
            const { offset, length } = pointerOffsetX(
              e,
              e.currentTarget.getBoundingClientRect(),
              fakeLandscape,
            );
            const x = Math.min(Math.max(offset, 0), length);
            setHover({ ms: (x / length) * durationMs, x });
          }}
          onPointerLeave={() => setHover(null)}
        >
          {/* 缩略图预览：拖进度条时能看见画面。没生成好就只剩时间戳，
              不影响拖动——预览是锦上添花，时间戳是刚需。
              触屏抬高一档（bottom-8 → bottom-16）：32px 的间距正好被手指
              压着下缘，64px 让预览完整露在指尖上方。 */}
          {hover ? (
            <div
              ref={bubbleRef}
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
              {/* 章节名 + 时间：拖动时知道自己拖到了哪一段，比只有一个
                  时间戳有用得多（jellyfin-web 的气泡同样是三合一）。
                  章节名在上、时间在下——时间是刚需，永远在固定位置。 */}
              {hoverChapter ? (
                <p className="mt-1.5 max-w-[220px] truncate text-center text-[12px] text-white/75 drop-shadow">
                  {hoverChapter}
                </p>
              ) : null}
              <p className="mt-0.5 text-center text-[13px] font-medium tabular-nums text-white drop-shadow">
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
            {/* 宽度由上面的 paint 每帧写，不在这里跟 React 的渲染节奏。
                data-player-played 是给端到端验收脚本认的锚点（按第几个子元素
                找会在轨道里多一层时悄悄量错东西，见 scripts/perf/e2e_player_feel.py）*/}
            <div
              ref={playedRef}
              data-player-played=""
              className="absolute inset-y-0 left-0 w-0 bg-[var(--player-accent)]"
            />
            {/* 章节刻度：压在已播段之上，两侧留白靠 2px 宽的暗色竖条本身。
                画在轨道内部（overflow-hidden）所以不用再夹一次边界。 */}
            {chapterMarks.map((mark) => (
              <span
                key={mark.start_ms}
                className="absolute inset-y-0 w-[2px] -translate-x-1/2 bg-black/55"
                style={{ left: `${mark.ratio * 100}%` }}
              />
            ))}
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
              // 只认主指针的主键起手。不挡的话右键点进度条会**当场 seek**
              // 再弹出上下文菜单（中键同理），而右键的意图从来不是跳转；
              // 触屏上第二根手指落在条上也会顶掉第一根正在进行的拖动。
              // 触摸/笔的主接触点 button 恒为 0，这条不会误伤它们。
              if (!durationMs || e.button !== 0 || !e.isPrimary) return;
              e.currentTarget.setPointerCapture(e.pointerId);
              const { offset, length } = pointerOffsetX(
                e,
                e.currentTarget.getBoundingClientRect(),
                fakeLandscape,
              );
              const ratio = Math.min(1, Math.max(0, offset / length));
              setDragging(Math.round(ratio * durationMs));
            }}
            onPointerMove={(e) => {
              if (dragging === null || !durationMs) return;
              const { offset, length } = pointerOffsetX(
                e,
                e.currentTarget.getBoundingClientRect(),
                fakeLandscape,
              );
              const ratio = Math.min(1, Math.max(0, offset / length));
              const next = Math.round(ratio * durationMs);
              setDragging(next);
              // 画面跟着手指走——能免费跳的时候不跟随是白白浪费手感
              onScrub(next);
            }}
            onPointerUp={() => {
              if (dragging !== null) onSeek(dragging);
              setDragging(null);
            }}
            // 手势被系统收走时浏览器**只发 pointercancel、不再发 pointerup**：
            // 进度条贴着屏幕最底边，正压在 iOS 的 Home 指示条上滑区里，拖到
            // 边上一带就会被系统当成返回桌面的起手式；通知中心下拉、第二根
            // 手指落下同理。不接这条的话 dragging 会永远停在最后一个拖动值
            // 上——进度点和时间从此钉死在那儿不再跟画面走，而画面照常播，
            // 中央的退进十秒/快捷键还能把画面跳走却带不动进度条，直到下一次
            // 完整拖拽把它清掉才「自己好了」。
            // 取消的手势**不提交** seek：用户没松手确认过这个位置，退回
            // positionMs 才是真值。
            onPointerCancel={() => setDragging(null)}
            onKeyUp={() => {
              if (dragging !== null) onSeek(dragging);
              setDragging(null);
            }}
            // 键盘拖动（方向键改 range 的值走 onChange）对称的一条：焦点离开
            // 时那次键盘调整已经结束，没等到 keyup 就不能让它继续遮着 positionMs
            onBlur={() => setDragging(null)}
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
            ref={thumbRef}
            data-player-thumb=""
            className={`pointer-events-none absolute left-0 top-1/2 size-[14px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--player-thumb)] shadow-[0_0_0_4px_var(--accent-soft)] transition-transform duration-150 pointer-coarse:size-[18px] ${
              durationMs
                ? dragging !== null
                  ? // 按下的一刻回弹到原尺寸：指下有「按住了」的反馈
                    "scale-100 pointer-coarse:scale-110"
                  : // 收起态整行 opacity-0，触屏常显不用再按 chromeVisible 分岔
                    "scale-0 [.player-scrub-row:hover_&]:scale-110 pointer-coarse:scale-100"
                : "scale-0"
            }`}
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
                  {/* 三点而不是齿轮：齿轮在播放器里指向「偏好设置」，而这颗后面
                      是画质与播放诊断——一组针对**这次播放**的杂项，三点的
                      「还有别的」正是这个语义。说明气泡与面板标题仍是「设置」，
                      点开看到的东西没变。 */}
                  <MoreIcon className={ICON} />
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

            {/* 右下角：横屏 + 全屏。两颗共一张卡片，与左边那张**完全同形制**
                （同高、同圆角、同内边距），一行两端因此是对称的两块，而不是
                一块卡片对一组文字胶囊。

                横屏管方向、全屏管铺满——真横屏会顺带进全屏，此时全屏键自然
                成为退出键。iPhone 没有元素级全屏，全屏键走系统原生播放器，
                字幕靠 video 上的原生 VTT 轨跟进去（见 video-player 的 pip 轨）。

                原来这里是上一集/下一集，2026-09-09 按产品决定移除；片尾窗口内
                的「下一集」卡片仍在（video-player 的 nextCard），片尾之外与
                「上一集」改由详情页承担。 */}
            <div className="player-glass flex items-center gap-1 rounded-full px-1.5 py-1">
              {canRotate ? (
                <IconButton tip={landscape ? "退出横屏" : "横屏"} onClick={onToggleLandscape}>
                  <RotateGlyph active={landscape} />
                </IconButton>
              ) : null}
              <IconButton tip={fullscreen ? "退出全屏" : "全屏"} onClick={onToggleFullscreen}>
                {fullscreen ? <ShrinkIcon className={ICON} /> : <ExpandIcon className={ICON} />}
              </IconButton>
            </div>
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
        primary ? "size-[68px] pointer-coarse:size-14" : "size-12 pointer-coarse:size-11"
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
 * 自己不带玻璃底：控制条上的键一律装在磨砂卡片里，每个再包一层会变成
 * 「玻璃里的玻璃」。单独浮在画面上的键（顶栏的返回/画中画）不走这个组件。
 */
function IconButton({
  tip,
  active,
  open,
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
      className="player-btn player-tip size-9 shrink-0 pointer-coarse:size-11"
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
