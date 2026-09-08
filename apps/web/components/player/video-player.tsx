"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { MediaController } from "media-chrome/react";

import { ChevronLeftIcon, LockIcon } from "@/components/icons";

import { ConsentDialog } from "@/components/player/consent-dialog";
import { DiagnosticsPanel } from "@/components/player/diagnostics-panel";
import { PlayerCenterControls, PlayerControls } from "@/components/player/player-controls";
import { SubtitleLayer, useVideoContentBox } from "@/components/player/subtitle-layer";
import {
  type ClientCapability,
  DEFAULT_PLAYBACK_SCOPE,
  type PlaybackApiScope,
  fetchPlaybackDiagnostics,
  type PlaybackUnit,
  type PlaybackDiagnostics,
  type PlaybackWatchState,
  pingPlaybackSession,
  reportPlaybackProgress,
  reportPlaybackProgressOnUnload,
  resolveStreamUrl,
  startPlaybackSession,
  type PlaybackMetricPayload,
  fetchTrickplay,
  reportPlaybackMetric,
  reportPlaybackMetricOnUnload,
  stopPlaybackSession,
  stopPlaybackSessionOnUnload,
} from "@/lib/api/playback";
import { type AutoplayOutcome, attemptAutoplay, shouldAttemptAutoplay } from "@/lib/player/autoplay";
import { formatBandwidth } from "@/lib/player/bandwidth";
import { getCapabilitySnapshot } from "@/lib/player/capability";
import type { PlaybackEngine } from "@/lib/player/engine";
import { createEngine, preloadHlsEngine } from "@/lib/player/engine";
import { bufferedAhead } from "@/lib/player/stall";
import {
  awaitsUserDecision,
  initialPlayerState,
  isBusy,
  playerReducer,
} from "@/lib/player/machine";
import {
  asNativeVideo,
  enterLandscape,
  enterNativeVideoFullscreen,
  exitLandscape,
  requestFullscreen,
  screenOrientation,
} from "@/lib/player/orientation";
import { planAudioOptions } from "@/lib/player/audio-tracks";
import { chromeMustStayVisible, shouldHideOnPointerLeave } from "@/lib/player/chrome";
import { createFrameDropTracker } from "@/lib/player/framedrop";
import {
  FREEZE_FRAME_MAX_MS,
  canReleaseFreeze,
  captureFrame,
  drawTile,
} from "@/lib/player/freeze-frame";
import {
  planSystemTrackModes,
  resolvePlaybackMode,
  shouldApplyPostAttachSeek,
} from "@/lib/player/playback-mode";
import { loadQualityPreference, saveQualityPreference } from "@/lib/player/quality";
import { createSessionReleaser } from "@/lib/player/session-release";
import {
  type QoeEvent,
  initialQoe,
  isReportable,
  liveStats,
  reduceQoe,
  summarize,
} from "@/lib/player/qoe";
import { type TrickplayIndex, tileAt } from "@/lib/player/trickplay";
import { FALLBACK_FRAME_RATE, isEditableTarget, resolveShortcut } from "@/lib/player/shortcuts";
import {
  type AdjustKind,
  type SwipeIntent,
  EDGE_GUARD_PX,
  applySwipe,
  classifyIntent,
  classifyTouchZone,
  pointerOffsetX,
  seekDeltaMs,
  toLayoutPoint,
} from "@/lib/player/touch-adjust";
import type { SubtitleStyle } from "@/lib/player/subtitles";
import {
  loadSubtitleStyle,
  pickInitialSubtitle,
  planSubtitleTracks,
  saveSubtitleStyle,
} from "@/lib/player/subtitles";
import {
  HOLD_SPEED_DELAY_MS,
  HOLD_SPEED_RATE,
  type HoldSpeedEvent,
  type HoldSpeedState,
  canHoldSpeed,
  holdSpeedReducer,
} from "@/lib/player/hold-speed";
import { nextSeekTarget, seekBatchWindowMs } from "@/lib/player/seek-batch";
import { resolveTap } from "@/lib/player/tap";
import {
  clampSeekTarget,
  formatClock,
  isInEndCredits,
  isWithinRanges,
  planSeek,
  toFileMs,
  toSessionSeconds,
} from "@/lib/player/timeline";

/**
 * 网页播放器（docs/design/web-player.md §6）。
 *
 * 本组件是**唯一的编排层**：决策、会话生命周期、降档回路、进度上报、字幕与
 * 快捷键都在这里接线，而每一段判断本身都在 `lib/player/` 的纯函数里（状态机、
 * 时间轴换算、字幕规划、快捷键映射、自动播放兜底），因此它们能被 node --test
 * 直接覆盖，这里只剩「什么时候调它们」。
 *
 * 起播是「决策 → 开会话 → 缓冲 → 出画」四段异步，中间随时可能插进 seek、
 * 降档、切集，所以状态一律走 `playerReducer`，不用 boolean 拼——拼出来的
 * 组合里一定有「正在降档又正在缓冲」这种自相矛盾的中间态。
 */

export interface VideoPlayerProps {
  unit: PlaybackUnit;
  /** 片名。条目信息与起播并行加载（§6.10），晚到时先空着 */
  title: string | null;
  /** 副标题：剧集显示「S01E05 · 集名」，电影为 null */
  episodeLabel: string | null;
  posterUrl: string | null;
  /**
   * 分享链接的 `?t=` 起播覆盖（毫秒）。只对**进入播放页的第一个单元**生效
   * ——切下一集之后回到「服务端按各自观看状态定」的默认语义。
   * undefined = 不覆盖：start_ms 不发，服务端接续播点（看完的从头播）。
   */
  startMsOverride?: number;
  /** 下一集；没有（电影 / 本季最后一集）为 null */
  next: { unit: PlaybackUnit; label: string } | null;
  /** 上一集；没有（电影 / 本季第一集）为 null */
  prev: { unit: PlaybackUnit; label: string } | null;
  onPlayNext: () => void;
  onPlayPrev: () => void;
  onExit: () => void;
  /**
   * 播放接口作用域（docs/design/media-share.md §5.3）：影片分享的访客走
   * `/share/{slug}/playback`、进度只记本浏览器、不上报遥测。缺省 = 登录态。
   * 组件生命周期内视为常量（换作用域就换 key 重挂）。
   */
  api?: PlaybackApiScope;
}

/** 进度心跳间隔。服务端另有节流，这里给足密度即可。 */
const PROGRESS_INTERVAL_MS = 10_000;
/** 会话续命间隔。必须明显短于服务端的空闲回收窗口。 */
const PING_INTERVAL_MS = 15_000;
/** 播放中控制条自动隐藏的**静止**时长——任何操作（触摸、鼠标移动、快捷键）
 * 都会把倒计时从头来过（见 chromeActivity 的注释）。4 秒取 Netflix 手机端
 * 的手感：3 秒在真机上「刚找到按钮就没了」（用户反馈）。 */
const IDLE_HIDE_MS = 4000;

/**
 * iOS Safari 的画中画：没有 W3C 那套 API，只有带前缀的 presentationMode。
 * TS 的 DOM 类型至今没收录，本文件里多处要用，收成一个类型别名。
 */
interface WebkitPresentationVideo {
  webkitSupportsPresentationMode?: (mode: string) => boolean;
  webkitSetPresentationMode?: (mode: string) => void;
  webkitPresentationMode?: string;
}

/** 画中画图标：一个大屏 + 右下角的小窗；退出态把小窗画到左上，表示「收回大屏」。 */
function PipGlyph({ exit }: { exit: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className="size-[18px] max-md:size-[22px]" fill="currentColor" aria-hidden>
      <path d="M3 5.5A1.5 1.5 0 0 1 4.5 4h15A1.5 1.5 0 0 1 21 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-13Zm2 .5v12h14V6H5Z" />
      <path d={exit ? "M7 8h7v5H7V8Z" : "M12 12h6v5h-6v-5Z"} />
    </svg>
  );
}

/** 亮度胶囊的太阳图标。 */
function BrightnessGlyph() {
  return (
    <svg viewBox="0 0 24 24" className="size-4 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8" />
    </svg>
  );
}

/** 音量胶囊的喇叭图标；静音时画一道斜杠。 */
function VolumeGlyph({ muted }: { muted: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className="size-4 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M11 5 6.5 9H3v6h3.5L11 19V5Z" />
      {muted ? <path d="M15 9l6 6M21 9l-6 6" /> : <path d="M15.5 8.5a5 5 0 0 1 0 7M18.4 6a9 9 0 0 1 0 12" />}
    </svg>
  );
}

/** 快进/快退提示的双箭头。 */
function SeekChevrons({ back }: { back: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`size-4 shrink-0 ${back ? "-scale-x-100" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth={2.2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="m6 6 6 6-6 6M13 6l6 6-6 6" />
    </svg>
  );
}

function unitKeyOf(unit: PlaybackUnit): string {
  return `${unit.media_item_id}/${unit.season_number ?? 0}/${unit.episode_number ?? 0}`;
}

export function VideoPlayer(props: VideoPlayerProps) {
  const {
    unit,
    title,
    episodeLabel,
    posterUrl,
    startMsOverride,
    next,
    prev,
    onPlayNext,
    onPlayPrev,
    onExit,
    api = DEFAULT_PLAYBACK_SCOPE,
  } = props;
  const unitKey = unitKeyOf(unit);
  // 接口作用域放 ref：各回调 / effect 的依赖数组一律不变，作用域在组件生命周期内是常量
  const apiRef = useRef(api);
  apiRef.current = api;

  const [state, dispatch] = useReducer(playerReducer, initialPlayerState);
  const [resume, setResume] = useState<PlaybackWatchState | null>(null);
  const [positionMs, setPositionMs] = useState(0);
  const [bufferedEndMs, setBufferedEndMs] = useState<number | null>(null);
  const [paused, setPaused] = useState(true);
  const [selectedSubtitle, setSelectedSubtitle] = useState<string | null>(null);
  /**
   * 向服务端点名要的音轨。null = 没表态，由服务端自动选。
   *
   * 与「现在实际在放哪条」分开：后者读决策里的 `audio.track_ref`，永远是真值
   * （点名的轨可能不在这个文件里，服务端会自动回退）。菜单打勾用真值，
   * 请求用这个——用一个变量同时干两件事，换版本文件时勾就会打空。
   */
  const [requestedAudio, setRequestedAudio] = useState<string | null>(null);
  /**
   * 用户点选的字幕轨请求值。null = 让服务端用观看记忆；"off"/轨引用 = 显式。
   * 只有涉及**烧录**（选中/撤下 PGS）才会随会话请求上送并触发重开——
   * 文本轨切换纯前端完成，不惊动服务端。
   */
  const [requestedSubtitle, setRequestedSubtitle] = useState<string | null>(null);
  /**
   * 本单元内用户是否亲手选过字幕（选轨或关闭都算）。表过态之后，「初始
   * 字幕选择」不再插手——换会话（换音轨/画质/撤烧/seek 重开）会带来新的
   * watch 快照，里面的记忆轨是**旧值**（比如刚撤下的 PGS），拿它覆盖用户
   * 刚点的选择，表现为「切了 SRT 字幕却没加载」（真机踩中）。
   */
  const subtitleTouchedRef = useRef(false);
  // 惰性初始化从 localStorage 读：字幕调好的字号/位置不该每次进来都重调
  const [subtitleStyle, setSubtitleStyle] = useState<SubtitleStyle>(loadSubtitleStyle);
  /** 画质上限（max_height）。null = 自动。持久化，弱网用户不必每部片重选 */
  const [quality, setQuality] = useState<number | null>(loadQualityPreference);
  const [trickplay, setTrickplay] = useState<TrickplayIndex | null>(null);
  /** 供冻结帧读最新索引：写进依赖会让 freezeFrame 换身份，牵连挂引擎的 effect */
  const trickplayRef = useRef<TrickplayIndex | null>(null);
  trickplayRef.current = trickplay;
  // 播放质量累计。放 ref 而不是 state：每秒都在变，进渲染只会白重绘。
  const qoeRef = useRef(initialQoe());
  // 快照函数放 ref：卸载与切集的 effect 都不依赖 state.session，
  // 直接闭包会拿到过期的会话，上报到错误的档位上。
  const qoeSnapshotRef = useRef<() => PlaybackMetricPayload | null>(() => null);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [serverDiagnostics, setServerDiagnostics] = useState<PlaybackDiagnostics | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [chromeVisible, setChromeVisible] = useState(true);
  /**
   * 控制层的活动信号：每次用户操作 +1，自动隐藏的倒计时以它为依赖从头再排。
   *
   * 没有它的话计时器只在 chromeVisible **翻转**时重排——控制层已经露着时，
   * `setChromeVisible(true)` 被 React 按同值去重，effect 不再执行，于是
   * 「首次唤出后 N 秒」准时消失：用户手指还按在按钮上、鼠标还在动，控制层
   * 也照样收走（真机反馈「很快就消失」的根源）。所有播放器的语义都是
   * 「静止 N 秒才收」，静止的定义需要这个信号来承载。
   */
  const [chromeActivity, setChromeActivity] = useState(0);
  const lastActivityBumpRef = useRef(0);
  /** 活动信号节流到 500ms 一跳：鼠标 move 是指针频率的事件，逐次 setState
   * 会让整棵播放器组件树跟着指针重渲染。粒度换来的误差上限是倒计时短
   * 500ms，感知不到。 */
  const bumpChromeActivity = useCallback(() => {
    const now = performance.now();
    if (now - lastActivityBumpRef.current < 500) return;
    lastActivityBumpRef.current = now;
    setChromeActivity((n) => n + 1);
  }, []);
  /** 用户明确关掉过本集的「下一集」提示：关掉后不能因为还在片尾窗口里又弹回来 */
  const [nextDismissed, setNextDismissed] = useState(false);
  /** 元数据里的时长（毫秒）。档 0 直出时它就是片长，服务端算不出时的兜底 */
  const [videoDurationMs, setVideoDurationMs] = useState<number | null>(null);
  // video 元素挂载后要触发依赖它的 effect，所以用 state 而不是纯 ref 持有
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);

  /**
   * 冻结帧：换流/远跳时盖住画面的那一张（lib/player/freeze-frame.ts）。
   *
   * canvas 元素常驻（ref 要稳定，抓帧发生在盖上去之前），只靠 `frozen`
   * 切显隐。
   */
  const freezeCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const [frozen, setFrozen] = useState(false);
  /**
   * 这是第几次冻结。缩略图是异步加载的，回来时这一冻可能早就撤了、或者用户
   * 又跳了一次——拿它对一下，过期的那张绝不能画上去。
   */
  const freezeTokenRef = useRef(0);
  /** 雪碧图的解码缓存：连按快进会连着要同一张，每次新建 Image 等于重解一遍 */
  const sheetCacheRef = useRef(new Map<string, HTMLImageElement>());

  /**
   * 把画面冻住，盖住接下来那段没有画面的空窗
   * （docs/design/player-feel.md §2.G2，实现见 lib/player/freeze-frame.ts）。
   *
   * 该在哪些时刻调：**凡是会让 `<video>` 手里一帧都不剩的动作**——换会话
   * （seek 出界 / 换画质 / 换音轨 / 取流断了原地重开）、以及 hls.js 会清缓冲
   * 重新装载的远跳。跳转落在缓冲里的那种不调：数据在手上，本来就不会黑。
   *
   * 传了 `targetFileMs`（远跳）就**盖落点的缩略图**而不是上一帧（§2.G4）：
   * 远跳与近跳的体感差别有一大半是「等的时候画面还停在原地，看着像没拖动」，
   * 换成落点的画面，这一跳视觉上当场就落地了。两步走：
   *
   * 1. 先同步抓当前帧盖上——雪碧图要现加载，这一步保证任何时候都不黑；
   * 2. 缩略图到手再原地换成落点那一格（还没生成 trickplay 就停在第 1 步）。
   *
   * 抓不到帧、也没有缩略图时什么都不做，黑屏照旧——盖一张空白 canvas 更糟。
   */
  const freezeFrame = useCallback(
    (targetFileMs?: number) => {
      const canvas = freezeCanvasRef.current;
      if (!video || !canvas) return;
      const token = freezeTokenRef.current + 1;
      freezeTokenRef.current = token;
      if (captureFrame(video, canvas)) setFrozen(true);

      // 走 ref 读 trickplay：把它写进依赖会让 freezeFrame 在索引加载完成时
      // 换掉身份，而挂引擎那个 effect 依赖它——那就是一次无谓的销毁重挂流。
      const tile = targetFileMs === undefined ? null : tileAt(trickplayRef.current, targetFileMs);
      if (!tile) return;
      const cache = sheetCacheRef.current;
      let image = cache.get(tile.url);
      if (!image) {
        // 一张雪碧图解码后就是几 MB，连着远跳十几次能攒出几十 MB。留最近
        // 四张：连按快进反复要的是同一张，四张足够接住，再多是纯占内存。
        // Map 按插入序，超了就丢最早那张。
        while (cache.size >= 4) {
          const oldest = cache.keys().next().value;
          if (oldest === undefined) break;
          cache.delete(oldest);
        }
        image = new Image();
        image.src = tile.url;
        cache.set(tile.url, image);
      }
      const paint = () => {
        // 过期的那张不能画：这几十毫秒里可能已经撤了冻结、或者又跳了一次
        if (freezeTokenRef.current !== token) return;
        if (drawTile(canvas, image, tile)) setFrozen(true);
      };
      if (image.complete) paint();
      else image.addEventListener("load", paint, { once: true });
    },
    [video],
  );

  /**
   * 撤掉冻结帧：新位置真的出画了就撤。
   *
   * 四个事件轮流来问同一个判据（`canReleaseFreeze`）——`seeked` 管暂停态下的
   * 跳转，`playing` 管换会话后重新起播，`canplay` 管自动播放被拦下、流已就绪
   * 但没在放，`timeupdate` 是兜底的常规心跳。宁可多问几次也不能漏：撤不掉的
   * 后果是画面永远冻着而声音在走，比黑屏糟得多。再加一道硬时限，四个事件
   * 全都不来（流起不来、解码器彻底卡死）时到点强撤，让用户看到该看到的转圈
   * 或错误页，而不是一张假死的画。
   */
  useEffect(() => {
    if (!frozen || !video) return;
    const release = () => {
      if (canReleaseFreeze({ seeking: video.seeking, readyState: video.readyState })) {
        // 递增 token 让在途的缩略图作废：晚到的那张不能把已经出画的画面
        // 重新盖回去（远跳时雪碧图与首帧常常是前后脚到）
        freezeTokenRef.current += 1;
        setFrozen(false);
      }
    };
    const events = ["seeked", "playing", "canplay", "timeupdate"] as const;
    for (const event of events) video.addEventListener(event, release);
    const timer = window.setTimeout(() => setFrozen(false), FREEZE_FRAME_MAX_MS);
    return () => {
      for (const event of events) video.removeEventListener(event, release);
      window.clearTimeout(timer);
    };
  }, [frozen, video]);

  /**
   * 要用户拍板的两个态（报错页、同意弹窗）立刻撤冻结帧：它们本来就是终态，
   * 上面那四个事件一个都不会来，留着冻结帧只会把弹窗垫在一张过期画面上。
   * 切集同理——新一集不该先闪上一集的最后一帧。
   */
  useEffect(() => {
    if (awaitsUserDecision(state.phase)) setFrozen(false);
  }, [state.phase]);
  useEffect(() => {
    setFrozen(false);
    freezeTokenRef.current += 1;
    sheetCacheRef.current.clear();
  }, [unitKey]);

  /** 本集自动播放的结果。只有 blocked 需要界面兜底（中央大播放键） */
  const [autoplay, setAutoplay] = useState<AutoplayOutcome | null>(null);
  /** 当前是否在横屏（全屏 + 锁横向，或 iOS 上的 CSS 伪横屏）里 */
  const [landscape, setLandscape] = useState(false);
  /**
   * 设备当前是不是横着（与我们自己的横屏按钮无关）。
   *
   * 锁屏要跟着**画面真的横过来**出现：多数人是直接把手机转过来（系统自动
   * 旋转），根本不会去按播放器的横屏键——只认 `landscape` 的话，最需要防
   * 误触的那批人反而看不到这颗按钮。
   */
  const [deviceLandscape, setDeviceLandscape] = useState(false);
  /**
   * 走的是 CSS 伪横屏（把播放器容器旋转 90° 铺满视口）。
   *
   * iPhone Safari 既没有元素级全屏也没有 `screen.orientation.lock`，真横屏
   * 做不了；但「点一下横过来」是移动端播放器的基本操作，国内视频站在 iOS
   * 上全是这么伪的。与 `landscape` 分开存：退出时要知道该解方向锁还是摘类名。
   */
  const [fakeLandscape, setFakeLandscape] = useState(false);
  /** 当前在元素级全屏里（无论是全屏键还是真横屏带进去的） */
  const [fullscreen, setFullscreen] = useState(false);
  /**
   * 这台设备的方向锁真的能用。放 state 而不是渲染时直接算：服务端渲染没有
   * `screen`，直接算会造成水合不一致，按钮文案在首帧闪一下。
   */
  const [canRotate, setCanRotate] = useState(false);
  /**
   * 这个浏览器能不能**由网页发起**画中画。
   *
   * Firefox 的画中画只存在于浏览器自己的界面里（那颗浮在视频上的原生小按钮），
   * 网页调不动；不判一下就会留一个点了没反应的死按钮。iOS Safari 则是另一套
   * 前缀 API，两套都要认。
   */
  const [canPip, setCanPip] = useState(false);
  /** 已经在小窗里。按钮据此翻成「退出画中画」 */
  const [pipActive, setPipActive] = useState(false);
  /** 画面亮度（CSS brightness 滤镜，0.1~1）。左半屏上下滑调节 */
  const [brightness, setBrightness] = useState(1);
  /** 调节反馈的胶囊读数（顶部居中）；触摸滑动与键盘 ↑↓/M 共用。null = 没在调 */
  const [adjust, setAdjust] = useState<{
    kind: AdjustKind;
    value: number;
    /** iOS：video.volume 赋值被系统忽略，改为提示用侧键 */
    unsupported?: boolean;
    /** 静音中（M 键 / 静音下调音量）：图标画斜杠、读数换成「静音」 */
    muted?: boolean;
    /** 淡出阶段：还挂着但在播退场动画，动画走完才卸载 */
    leaving?: boolean;
  } | null>(null);
  /** 键盘快进/快退的方向提示（画面左/右侧）；连续按键在窗口内累计秒数 */
  const [seekFlash, setSeekFlash] = useState<{ seconds: number; leaving: boolean } | null>(
    null,
  );
  const adjustTimersRef = useRef<{ hide: number | null; gone: number | null }>({
    hide: null,
    gone: null,
  });
  const seekFlashTimersRef = useRef<{ hide: number | null; gone: number | null }>({
    hide: null,
    gone: null,
  });
  /**
   * 横屏锁屏（docs/design/player-feel.md §2.B3）。
   *
   * 横躺着看片时手掌、拇指常常压在屏幕上，一次误触就是暂停或跳走。锁上之后
   * 所有触摸手势与轻点全部作废，控制层也不再被唤出——只留一颗解锁键，点一下
   * 画面它露 3 秒。
   */
  const [locked, setLocked] = useState(false);
  /** 锁屏态下解锁键的露出（点画面唤出、3 秒后自己收） */
  const [lockHint, setLockHint] = useState(false);
  const lockHintTimerRef = useRef<number | null>(null);
  /** 长按倍速是否生效中（HUD 与还原都看它） */
  const [holdSpeed, setHoldSpeed] = useState(false);
  /**
   * 实测取流速度的**已格式化读数**（「3.2 MB/s」）；null = 样本还不够。
   *
   * 存字符串而不是数字，是为了让 React 在读数没变时自己 bail out：这一格
   * 每秒刷一次，存 bps 的话每次都是新数字、每秒把整条控制条重渲染一遍，
   * 而屏幕上那行字十有八九一模一样（进度条 60fps 自绘刻意绕开 state 就是
   * 为了这个，见 player-feel.md §2.A1，别从这里把它加回来）。
   */
  const [speedLabel, setSpeedLabel] = useState<string | null>(null);

  /** 横滑拖进度时的落点读数。手势期间常显，松手/取消后淡出 */
  const [seekPreview, setSeekPreview] = useState<{
    targetMs: number;
    deltaMs: number;
    leaving: boolean;
  } | null>(null);
  const seekPreviewTimerRef = useRef<number | null>(null);

  /**
   * 弹出/刷新调节胶囊，并重排它的退场：每次调用都把「0.9 秒后开始淡出、
   * 淡出 0.16 秒后卸载」的两段计时从头来过。滑动中每次 move 都会刷新，
   * 所以手指不离开胶囊就不走；键盘连按同理。两段式（leaving → null）是
   * 为了退场也有动画——直接卸载是「啪」地消失，与控制条 300ms 的淡入
   * 淡出不成体系。
   */
  const flashAdjust = useCallback(
    (next: { kind: AdjustKind; value: number; unsupported?: boolean; muted?: boolean }) => {
      const timers = adjustTimersRef.current;
      if (timers.hide !== null) window.clearTimeout(timers.hide);
      if (timers.gone !== null) window.clearTimeout(timers.gone);
      timers.gone = null;
      setAdjust({ ...next, leaving: false });
      timers.hide = window.setTimeout(() => {
        timers.hide = null;
        setAdjust((prev) => (prev ? { ...prev, leaving: true } : prev));
        timers.gone = window.setTimeout(() => {
          timers.gone = null;
          setAdjust(null);
        }, 180);
      }, 900);
    },
    [],
  );

  /** 键盘快进/快退的方向提示：同方向连按在提示存续期内累计秒数
   * （按三下 → 就显示 ±15 秒，YouTube 同款），换方向从头计。 */
  const flashSeek = useCallback((seconds: number) => {
    const timers = seekFlashTimersRef.current;
    if (timers.hide !== null) window.clearTimeout(timers.hide);
    if (timers.gone !== null) window.clearTimeout(timers.gone);
    timers.gone = null;
    setSeekFlash((prev) => ({
      seconds:
        prev && !prev.leaving && Math.sign(prev.seconds) === Math.sign(seconds)
          ? prev.seconds + seconds
          : seconds,
      leaving: false,
    }));
    timers.hide = window.setTimeout(() => {
      timers.hide = null;
      setSeekFlash((prev) => (prev ? { ...prev, leaving: true } : prev));
      timers.gone = window.setTimeout(() => {
        timers.gone = null;
        setSeekFlash(null);
      }, 180);
    }, 800);
  }, []);

  /**
   * 横滑拖进度的落点胶囊。
   *
   * 与亮度/音量胶囊不同，它**没有自动退场计时**：手势没结束就该一直挂着，
   * 手指停在半路时读数消失会让人以为手势断了。退场由 `hideSeekPreview` 在
   * 松手/取消时显式触发。
   */
  const showSeekPreview = useCallback((targetMs: number, deltaMs: number) => {
    if (seekPreviewTimerRef.current !== null) {
      window.clearTimeout(seekPreviewTimerRef.current);
      seekPreviewTimerRef.current = null;
    }
    setSeekPreview({ targetMs, deltaMs, leaving: false });
  }, []);

  const hideSeekPreview = useCallback(() => {
    setSeekPreview((prev) => (prev ? { ...prev, leaving: true } : prev));
    seekPreviewTimerRef.current = window.setTimeout(() => {
      seekPreviewTimerRef.current = null;
      setSeekPreview(null);
    }, 180);
  }, []);

  /** 一次性文字提示（顶部胶囊，与调节读数同视觉）；给「画中画被系统拒绝」
   * 这类**本来会静默失败**的操作一个出口——用户分不清「没反应」和「被拒绝」
   * 是最难排查的一类反馈。 */
  const [notice, setNotice] = useState<{ text: string; leaving: boolean } | null>(null);
  const noticeTimersRef = useRef<{ hide: number | null; gone: number | null }>({
    hide: null,
    gone: null,
  });
  const flashNotice = useCallback((text: string) => {
    const timers = noticeTimersRef.current;
    if (timers.hide !== null) window.clearTimeout(timers.hide);
    if (timers.gone !== null) window.clearTimeout(timers.gone);
    timers.gone = null;
    setNotice({ text, leaving: false });
    timers.hide = window.setTimeout(() => {
      timers.hide = null;
      setNotice((prev) => (prev ? { ...prev, leaving: true } : prev));
      timers.gone = window.setTimeout(() => {
        timers.gone = null;
        setNotice(null);
      }, 180);
    }, 2600);
  }, []);

  /** 卸载时清掉各组反馈计时器，别让它们对着已卸载的组件 setState。 */
  useEffect(
    () => () => {
      for (const timers of [
        adjustTimersRef.current,
        seekFlashTimersRef.current,
        noticeTimersRef.current,
      ]) {
        if (timers.hide !== null) window.clearTimeout(timers.hide);
        if (timers.gone !== null) window.clearTimeout(timers.gone);
      }
      if (seekPreviewTimerRef.current !== null) {
        window.clearTimeout(seekPreviewTimerRef.current);
      }
    },
    [],
  );

  const containerRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<PlaybackEngine | null>(null);
  /** 事件监听器捕获的会话；切档时旧 video 事件不得污染新会话。 */
  const activeSessionRef = useRef(state.session);
  activeSessionRef.current = state.session;
  const capabilityRef = useRef<ClientCapability | null>(null);
  /** 已发起的起播请求指纹：React 严格模式下 effect 会跑两遍，靠它去重 */
  const startedKeyRef = useRef<string | null>(null);
  /** 本轮要落到的文件位置（续播点 / seek 目标 / 降档前的位置） */
  const pendingFileMsRef = useRef(0);
  /** 供事件回调读取最新值，避免为了拿一个数字反复重绑监听 */
  const startMsRef = useRef(0);
  const positionRef = useRef(0);
  const reportedStartRef = useRef<string | null>(null);
  /** 管理员结束本次播放的信号只处理一次：心跳与暂停上报可能同时带回来 */
  const endedByAdminRef = useRef(false);
  /** 用户还想不想自动播放：他自己按过暂停之后就不再替他做主 */
  const wantsPlayRef = useRef(true);
  const autoplayAttemptsRef = useRef(0);
  const autoplayLastRef = useRef<AutoplayOutcome | null>(null);
  /** 供心跳探活读当前阶段：重开已经在路上时绝不能再触发一次重开 */
  const phaseRef = useRef(initialPlayerState.phase);
  /**
   * 供心跳探活确认「404 说的还是不是当前会话」。
   *
   * 换字幕（PGS 烧录）/换音轨/换画质会重开会话，而服务端开新会话的第一步
   * 就是杀掉同文件的旧会话（stop_for_file）。若旧定时器的那拍探活恰好在
   * 新会话落地之后才拿到 404，此刻 phase 已经是 buffering——只靠 phase
   * 守卫会放行一次多余的 restart，其 stop_for_file 又把刚起好的新会话
   * 杀掉，播放器随即撞上 404 的 playlist，报「浏览器不支持这个格式」。
   */
  const sessionIdRef = useRef<string | null>(null);
  /**
   * 会话已被服务端回收、但用户正暂停着，我们**没有**代他重开。
   *
   * 暂停中替他拉起一路 ffmpeg 是浪费（他可能再也不回来了），所以只立这个
   * 标记，等他点播放的那一刻再原地重开——那一下点击就是「我还要看」。
   */
  const deadSessionRef = useRef(false);
  /**
   * 按下的那一刻控制条在不在。
   *
   * 画面轻点是「控制层开关」：click 按这份快照取反。用 ref 而不是在 click
   * 里直接读 state：按下到抬起之间显隐可能被别的路径改过（鼠标 move 唤出、
   * 自动隐藏计时器收起），按「按下瞬间」的状态判断才符合用户的意图。
   */
  const chromeWasVisibleRef = useRef(true);
  /** 最近一次按下的指针类型：dblclick 事件不带 pointerType，双击全屏靠它
   * 判定「只属于鼠标」——触屏双击是两次控制层开关，不该顺带进全屏 */
  const lastPointerTypeRef = useRef("");
  // 会话释放器：区分「真的离开了」与「StrictMode 把同一个会话重新挂了一遍」。
  const sessionReleaser = useMemo(
    () =>
      createSessionReleaser((id) => {
        void stopPlaybackSession(id, apiRef.current).catch(() => undefined);
      }),
    [],
  );

  /**
   * 本次会话的播放模式：引擎/地址/时间轴参照/字幕渲染器，一次算清、处处
   * 引用这一份（决策逻辑与矩阵见 lib/player/playback-mode.ts）。
   * capability 在开会话前必已探测完，会话就位触发的重渲染里读 ref 是稳定的。
   */
  const mode = useMemo(
    () =>
      state.session && capabilityRef.current
        ? resolvePlaybackMode(state.session, capabilityRef.current)
        : null,
    [state.session],
  );
  const systemSubtitles = mode?.subtitleRenderer === "system-track";
  // system-track 内联的系统 cue 两处失真都源于 WebKit 以**整个元素**为
  // 基准（竖屏元素高是画面高的近 4 倍）：位置掉进黑边、字号巨大。量出画面
  // 矩形后用 CSS 变量双管齐下——位置抬到画面下沿再留 8% 安全边距（Apple/
  // Netflix caption safe area），字号按画面高 5.2% 显式指定（与自绘层同一
  // 标准；PiP/全屏由系统基于小窗渲染，本来就对，页面 CSS 也够不着）。
  const contentBox = useVideoContentBox(systemSubtitles ? video : null);
  const cueFontPx = systemSubtitles ? Math.max(16, contentBox.height * 0.052) : 0;
  // 服务端已给 cue 加 line:84%（系统层用它定位），内联的 WebKit 同样会把
  // cue 顶放在**元素高**的 84% 处——竖屏时那还在黑边里。目标：cue 顶落在
  // 「画面底 − 8% 安全边距 − 一行字高」，向上平移量 = 当前顶 − 目标顶；
  // 控制条展开时目标再抬到控制条上方。横屏满屏时元素≈画面，平移量≈0，
  // 与系统层（全屏/PiP）的 line:84% 自然一致。
  let cueLiftPx = 0;
  if (systemSubtitles && contentBox.height > 0) {
    const elementH = contentBox.height + contentBox.bottomInset * 2;
    const lineH = cueFontPx * 1.3;
    const currentTop = elementH * 0.84;
    const safeTop =
      elementH - contentBox.bottomInset - contentBox.height * 0.08 - lineH;
    const chromeTop = elementH - 172 - lineH;
    const targetTop = chromeVisible ? Math.min(safeTop, chromeTop) : safeTop;
    cueLiftPx = Math.max(0, currentTop - targetTop);
  }
  startMsRef.current = mode?.originMs ?? 0;
  positionRef.current = positionMs;
  phaseRef.current = state.phase;

  const failedKey = state.failedTiers.join(",");
  const sessionId = state.session?.session_id ?? null;
  sessionIdRef.current = sessionId;

  // 片长：服务端算的真值优先。**旧会话相对制**里 video.duration 只到「已经
  // 转出来的那一段」，拿它当分母会让进度条一路自己缩放；VOD 全片列表
  // （timeline="file"）和档 0 直出的 video.duration 都是真片长，可以兜底。
  const durationMs = useMemo(() => {
    if (resume?.duration_ms) return resume.duration_ms;
    if (state.session?.timeline === "file") return videoDurationMs;
    return sessionId ? null : videoDurationMs;
  }, [resume?.duration_ms, state.session?.timeline, sessionId, videoDurationMs]);
  const durationMsRef = useRef<number | null>(null);
  durationMsRef.current = durationMs;

  /** 进度条上的章节刻度。会话没换就保持同一个数组身份，免得下游白算一遍 */
  const chapters = useMemo(() => state.session?.chapters ?? [], [state.session]);

  const subtitles = useMemo(() => {
    const session = state.session;
    if (!session) return { options: [], unavailable: [] };
    return planSubtitleTracks(session.decision.subtitles, session.subtitle_urls);
  }, [state.session]);

  /** 服务端正在烧录进画面的字幕轨（Emby 语义「字幕压制」）；null = 没烧。 */
  const burnedSubtitle = state.session?.decision.video?.burn_subtitle ?? null;

  /** 音轨菜单项。少于两条时为空数组，控制条据此把整个按钮藏掉。 */
  const audioOptions = useMemo(
    () => planAudioOptions(state.session?.decision.audio_tracks),
    [state.session],
  );

  const activeSubtitle = useMemo(
    () => subtitles.options.find((option) => option.ref === selectedSubtitle) ?? null,
    [subtitles, selectedSubtitle],
  );

  /**
   * 画中画字幕的原生 <track> 地址。
   *
   * PiP 窗口只渲染 <video> 本身，自绘字幕层（VTT 自绘 / ASS canvas）跟不
   * 进去；但 Safari 的 PiP 会渲染 video 上 mode="showing" 的原生 VTT cue。
   * 所以给 video 常挂一条原生轨：VTT 直接用，ASS 要服务端降级转 VTT
   * （丢特效保文本，小窗里本来也看不清特效）。平时 mode="hidden" 不出画，
   * 由自绘层负责；进 PiP 才切 showing——两层同显会出双字幕。
   */
  const pipSubtitleUrl = useMemo(() => {
    if (!mode?.pipPatchTrack) return null; // system-track：字幕组已是系统轨
    if (!activeSubtitle) return null;
    if (activeSubtitle.ref === burnedSubtitle) return null; // 已烧进画面，哪里都带着
    if (activeSubtitle.kind === "vtt") return activeSubtitle.url;
    if (activeSubtitle.kind === "ass") return `${activeSubtitle.url}&format=vtt`;
    return null;
  }, [activeSubtitle, burnedSubtitle, mode?.pipPatchTrack]);

  // ---------------------------------------------------------------------
  // 起播链路
  // ---------------------------------------------------------------------

  /** `?t=` 起播覆盖只吃一次：切集之后回到「各自的续播点」默认语义。 */
  const overrideConsumedRef = useRef(false);

  /**
   * 刚在同意弹窗里点过「开启并播放」（保存成功后置位）。
   *
   * 用来识别「开关已保存、下一次决策却仍要求同意」的异常闭环：不识别的话
   * 一模一样的弹窗会在几百毫秒内原样闪回，用户看到的就是「确认按钮点了
   * 没反应」，而真正的故障（设置未生效/服务端异常）被完全吞掉。命中时改走
   * 明确的错误页（见起播 effect）。正常拿到非 consent 决策即清零。
   */
  const consentGrantedRef = useRef(false);

  // hls.js 热身与会话请求并行，别等会话回来才开始下载（§6.10）
  useEffect(() => {
    preloadHlsEngine();
  }, []);

  /** 切集 / 首次进入：直接发起决策，续播点由服务端并入会话响应（§6.10）。 */
  useEffect(() => {
    startedKeyRef.current = null;
    reportedStartRef.current = null;
    setPositionMs(0);
    setBufferedEndMs(null);
    setResume(null);
    setNextDismissed(false);
    setVideoDurationMs(null);
    setSelectedSubtitle(null);
    setRequestedAudio(null);
    setRequestedSubtitle(null);
    subtitleTouchedRef.current = false;
    setTrickplay(null); // 换片必须清掉：旧片的缩略图配新片的进度条是错的
    // 新的一集重新获得自动播放的资格：上一集用户按过暂停不代表下一集也不想看
    wantsPlayRef.current = true;
    autoplayAttemptsRef.current = 0;
    autoplayLastRef.current = null;
    // 上一集的「会话已死」标记不能带进新的一集：带着的话，起播完成前用户点
    // 播放会被当成「重开死会话」，凭空多拉一次决策
    deadSessionRef.current = false;
    endedByAdminRef.current = false;
    consentGrantedRef.current = false;
    setAutoplay(null);
    dispatch({ type: "reset" });

    // `?t=` 只覆盖进入播放页的第一个单元；其余一律 null = 服务端按观看状态
    // 定起点（看完的从头播）。以前这里要先问一次 /resume 再发决策——两跳
    // 串行是首帧延迟的白等大头，现在续播点由会话响应一并带回。
    const override = overrideConsumedRef.current ? undefined : startMsOverride;
    overrideConsumedRef.current = true;
    if (override !== undefined) setPositionMs(override);
    dispatch({ type: "request", startMs: override ?? null });
    // unit 是对象字面量，按内容比较；startMsOverride 只在首个单元消费
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitKey]);

  /** 决策 / 降档 / 换会话：三种都是「去后端要一个能播的地址」。 */
  useEffect(() => {
    const phase = state.phase;
    if (phase !== "deciding" && phase !== "degrading" && phase !== "session-starting") return;

    // 指纹必须覆盖**全部**会影响请求参数的依赖（quality 也在内）：漏一项的
    // 后果是该项单独变化时 effect 重跑却撞上相同指纹被去重跳过，而上一次
    // 在途请求已被 cancelled——响应回来没人认领，起播就永远停在转圈里。
    const key = `${unitKey}|${phase}|${state.startMs}|${failedKey}|${state.failureCount}|${state.attempt}|${requestedAudio ?? ""}|${requestedSubtitle ?? ""}|${quality ?? ""}`;
    if (startedKeyRef.current === key) return;
    startedKeyRef.current = key;

    let cancelled = false;
    void (async () => {
      try {
        if (!capabilityRef.current) capabilityRef.current = await getCapabilitySnapshot();
        pendingFileMsRef.current = state.startMs ?? 0;
        // 首帧从"用户要求播放"这一刻算起，而不是从会话就位算起——
        // 决策与起会话的耗时正是首帧延迟的大头
        qoe({ type: "play-requested", at: performance.now() });
        const session = await startPlaybackSession(
          {
            ...unit,
            capability: capabilityRef.current,
            failed_tiers: state.failedTiers,
            // null = 服务端接续播点；显式值（seek 重开 / 换轨 / ?t=）原样发
            start_ms: state.startMs ?? undefined,
            max_height: quality ?? undefined,
            audio_track: requestedAudio ?? undefined,
            subtitle_track: requestedSubtitle ?? undefined,
          },
          apiRef.current,
        );
        if (cancelled) {
          // 请求已被超越（退出播放器/切集/换参数重发）：响应里可能带着一个
          // 刚拉起的转码会话，此后没有任何代码会认领它——心跳、释放器都只认
          // 落进状态机的会话。不当场掐掉的话，这路 ffmpeg 会占着转码并发
          // 名额空烧三分钟 CPU，直到服务端超时回收（「关了播放 ffmpeg 还在
          // 跑」的主要来路；同文件重开有 stop_for_file 兜底，这里补上退出与
          // 跨文件切换的口子）。
          if (session.session_id) {
            void stopPlaybackSession(session.session_id, apiRef.current).catch(() => undefined);
          }
          return;
        }
        if (session.decision.outcome === "consent") {
          // 刚点过「开启并播放」、开关也保存成功，服务端却仍要求同意：这是
          // 异常闭环（设置没生效/后端异常），绝不能把一模一样的弹窗原样闪
          // 回去——那在用户眼里就是「确认按钮点了没反应」，出错这件事被完全
          // 吞掉。翻成明确的错误页，用户至少知道出了什么、该做什么。
          if (consentGrantedRef.current) {
            dispatch({
              type: "fatal",
              message: "软件转码开关已保存，但服务端仍在请求开启确认",
              suggestion:
                "开关可能没有生效（例如服务端刚重启）。请刷新页面重试；若反复出现，请查看服务端日志排查。",
            });
            return;
          }
        } else {
          consentGrantedRef.current = false;
        }
        if (state.startMs === null) {
          // 起点是服务端定的（续播点）：seek 目标与时间轴预填都跟上，
          // 不填的话起播那几秒进度条停在 00:00，看起来像「时间没加载出来」
          pendingFileMsRef.current = session.start_ms;
          setPositionMs(session.start_ms);
        }
        // 观看状态随响应带回：时间轴兜底片长、字幕记忆轨都从这里来
        if (session.watch) setResume(session.watch);
        dispatch({ type: "session", session });
      } catch (error) {
        if (cancelled) return;
        dispatch({
          type: "fatal",
          message: error instanceof Error ? error.message : "播放启动失败",
        });
      }
    })();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    state.phase,
    state.startMs,
    failedKey,
    state.failureCount,
    state.attempt,
    unitKey,
    requestedAudio,
    requestedSubtitle,
    quality,
  ]);

  /**
   * 自动播放：试一次，被拒绝了按原因决定重试还是降级。
   *
   * **为什么必须能重试**：`play()` 的调用时机在 hls.js 刚 attachMedia、
   * MediaSource 还没 open 的那一瞬间，浏览器可能直接抛 `AbortError`。只试
   * 一次的后果就是用户从条目页点了播放，进来却停在第一帧，非得再点一下——
   * 也就是这次要修的现象。所以起播链路上每一个「现在应该能播了」的时机
   * （挂流完成、loadedmetadata、canplay）都来敲一次这个门，闸门逻辑在
   * `shouldAttemptAutoplay` 里，有单测。
   */
  const tryAutoplay = useCallback(async () => {
    if (!video) return;
    if (
      !shouldAttemptAutoplay({
        wanted: wantsPlayRef.current,
        paused: video.paused,
        attempts: autoplayAttemptsRef.current,
        last: autoplayLastRef.current,
      })
    ) {
      return;
    }
    autoplayAttemptsRef.current += 1;
    const outcome = await attemptAutoplay(video);
    autoplayLastRef.current = outcome;
    // interrupted 是中间态，界面上什么都不该变——它等下一个时机再试
    if (outcome !== "interrupted") setAutoplay(outcome);
  }, [video]);

  /** 会话就位：按 mode 挂引擎、落回目标位置、起播。 */
  useEffect(() => {
    const session = state.session;
    if (!session?.stream_url || !video || !mode) return;

    let disposed = false;
    const engine = createEngine({
      video,
      streamUrl: resolveStreamUrl(mode.streamUrl),
      container: session.decision.container ?? "mp4",
      hasMse: capabilityRef.current?.mse !== "none",
      mse: capabilityRef.current?.mse ?? "none",
      preferNativeHls: mode.engine === "native-hls",
      // 直出档没有分片字节数可数，取流速度只能用「缓冲涨了几秒 × 码率」反推
      sourceBitrateBps: session.source?.bit_rate ?? null,
      // 首帧就从续播点开始装载。会话相对制下换算结果≈0，与从前无异；VOD
      // 全片列表下这是防止 hls.js 先去拉第 0 段的关键（engine.ts 有注释）
      startPositionS: Math.max(0, toSessionSeconds(pendingFileMsRef.current, mode.originMs)),
      telemetry: apiRef.current.telemetry,
      onFailed: (reason) => {
        if (!disposed) dispatch({ type: "failed", reason });
      },
      // 取流持续失败（token 过期 / 服务端中断）：与心跳自愈同一条路，同档位
      // 原地重开（新会话 = 新 token），不走降档——这一档没有失败。真断网时
      // 重开请求本身会失败，落到 fatal 错误页，比无限转圈至少让人知道出了事。
      onNetworkDead: () => {
        if (disposed) return;
        freezeFrame();
        // 刻意不动 wantsPlayRef：断流也可能发生在用户暂停期间（暂停时
        // hls.js 还在预载），置 true 会替他续播。在播的话它本来就是 true。
        video.pause();
        pendingFileMsRef.current = positionRef.current;
        dispatch({ type: "restart", startMs: positionRef.current });
      },
    });
    engineRef.current = engine;

    void engine.attach().then(() => {
      // hls.js 是动态 import；切档恰好发生在 import 完成前时，destroy() 会
      // 早于真正 attach。attach 完成后再补一次销毁，避免旧引擎变成孤儿。
      if (disposed) {
        engine.destroy();
        return;
      }
      // 会话相对制：转码会话已从 start_ms 起转，换算结果≈0；档 0 直出没有
      // 偏移，续播点在这里真的跳一次。文件绝对制：参照点为 0，target 即
      // 文件秒数，seek 由播放器按 VOD 列表直接取对应分片。
      const target = toSessionSeconds(pendingFileMsRef.current, mode.originMs);
      // 原生 HLS 的 DirectEngine 已在自己的 loadedmetadata listener 中完成
      // 首次 seek；这里不能再挂第二个 listener，否则 Safari 可能取消并重拉
      // 首个 init/segment，触发 MEDIA_ERR_DECODE。
      if (shouldApplyPostAttachSeek(mode.engine, target)) {
        const seek = () => {
          // 已经在目标附近就不跳（jellyfin-web 的 setCurrentTimeIfNeeded 同款）：
          // VOD 列表里流本来就从目标分片起，再赋一次 currentTime 是白白触发一次
          // seek——起播那一刻的 seek 会打断刚建立的缓冲，首帧要多等一拍。
          if (Math.abs((video.currentTime || 0) - target) < 1) return;
          video.currentTime = target;
        };
        if (video.readyState >= 1) seek();
        else video.addEventListener("loadedmetadata", seek, { once: true });
      }
      void tryAutoplay();
    }).catch((error: unknown) => {
      if (disposed) return;
      dispatch({
        type: "fatal",
        message: error instanceof Error ? error.message : "播放引擎启动失败",
      });
    });

    return () => {
      disposed = true;
      engine.destroy();
      engineRef.current = null;
    };
  }, [state.session, mode, video, tryAutoplay, freezeFrame]);

  /**
   * 换了会话（降档 / seek 换流）就重新获得自动播放的机会。
   *
   * 计数与结果都清空：新会话是一条新的流，上一条流上的「被拦下了」不能
   * 直接继承过来——真被拦的话这一轮会再判一次，代价只是一次 play()。
   */
  useEffect(() => {
    if (!state.session) return;
    autoplayAttemptsRef.current = 0;
    autoplayLastRef.current = null;
    deadSessionRef.current = false;
    setAutoplay(null);
  }, [state.session]);

  /** 诊断面板打开时轮询服务端会话，关闭后不额外占用 NAS 请求。 */
  const diagnosticsSessionId = state.session?.session_id;
  const diagnosticsStreamUrl = state.session?.stream_url;
  useEffect(() => {
    if (!diagnosticsOpen || !diagnosticsSessionId || !diagnosticsStreamUrl) {
      setServerDiagnostics(null);
      return;
    }
    let cancelled = false;
    let timer: number | null = null;

    const poll = async () => {
      try {
        const snapshot = await fetchPlaybackDiagnostics(
          diagnosticsSessionId,
          diagnosticsStreamUrl,
          apiRef.current,
        );
        if (!cancelled) setServerDiagnostics(snapshot);
      } catch {
        // 诊断是旁路信息，服务端瞬时不可用时保留上一份快照，不影响播放。
      }
      if (!cancelled) timer = window.setTimeout(poll, 2_000);
    };

    setServerDiagnostics(null);
    void poll();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [diagnosticsOpen, diagnosticsSessionId, diagnosticsStreamUrl]);

  /**
   * 取流速度读数：每秒问一次引擎，格式化后进 state（docs/design/player-feel.md §2.G3）。
   *
   * 1Hz 的 setState 看着像是往热路径上加重渲染，但这个组件本来就吃着
   * `timeupdate` 的 4Hz 位置更新，多这一下可以忽略；真正要守住的是**读数没变
   * 时不要重渲染**，所以 state 存的是格式化后的字符串，一样的字符串 React
   * 自己会 bail out（进度条那条 60fps 自绘绕开 state 的规矩仍然有效，别从
   * 这里把它加回来）。
   *
   * **换会话的空档里保留上一个读数**，不清空：那几秒没有引擎可问，但「上次
   * 量到的线路速度」并没有失效，而那恰恰是用户最想看它的时刻（转圈的时候）。
   * 清成 null 的结果是转圈时这一格必然空着——正好把它最有用的一段掐掉。
   * 只有切集才归零（换了一部片，上一部的读数不该跟过来）。
   */
  useEffect(() => {
    setSpeedLabel(null);
  }, [unitKey]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      const engine = engineRef.current;
      if (!engine) return;
      const label = formatBandwidth(engine.stats().downlinkBps);
      // 新引擎刚挂上、样本还没攒够时同样保留上一个读数（理由同上）
      if (label !== null) setSpeedLabel(label);
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  /** 累计一条播放质量事件。归约是纯函数，这里只负责喂事件。 */
  const qoe = useCallback((event: QoeEvent) => {
    qoeRef.current = reduceQoe(qoeRef.current, event);
  }, []);

  /** video 元素事件 → 状态机 / 进度 / 质量采集；每个会话单独绑定并校验身份。 */
  useEffect(() => {
    const session = state.session;
    if (!video || !session) return;
    const isCurrentSession = () => activeSessionRef.current === session;
    const onPlaying = () => {
      if (!isCurrentSession()) return;
      setPaused(false);
      qoe({ type: "playing", at: performance.now() });
      dispatch({ type: "playing" });
    };
    const onPause = () => {
      if (isCurrentSession()) setPaused(true);
    };
    const onWaiting = () => {
      if (!isCurrentSession()) return;
      qoe({ type: "waiting", at: performance.now() });
      dispatch({ type: "buffering" });
    };
    const onSeeking = () => {
      if (!isCurrentSession()) return;
      const scrub = scrubRef.current;
      // 同一次拖动的第二次及以后：位置是我们自己每 100 毫秒写进去的，
      // 不是用户又跳了一次（第一次照常计入——他确实跳了）
      const fromScrub = scrub.count > 0 && performance.now() - scrub.at < 250;
      if (!fromScrub) qoe({ type: "seeking", at: performance.now() });
      dispatch({ type: "seeking" });
    };
    const onSeeked = () => {
      if (isCurrentSession()) qoe({ type: "seeked", at: performance.now() });
    };
    const onEnded = () => {
      if (isCurrentSession()) dispatch({ type: "ended" });
    };
    const onDurationChange = () => {
      if (!isCurrentSession()) return;
      setVideoDurationMs(
          Number.isFinite(video.duration) && video.duration > 0
            ? Math.round(video.duration * 1000)
            : null,
        );
    };
    // 只认**播放头所在**那段连续缓冲，不能取 buffered 的最后一段：往回拖
    // 之后旧的前向缓冲还挂在时间轴后面（back buffer 只回收播放头之后 30
    // 秒以前的部分，拖回去之后那一段落在播放头**前方**，不会被回收），
    // 取最后一段会让浅色底一路铺到一小时开外，而那里根本没有连着的数据。
    const onBuffered = () => {
      if (!isCurrentSession()) return;
      setBufferedEndMs(toFileMs(video.currentTime + bufferedAhead(video), startMsRef.current));
    };
    const onTimeUpdate = () => {
      if (!isCurrentSession()) return;
      setPositionMs(toFileMs(video.currentTime, startMsRef.current));
      onBuffered();
    };
    // 「现在应该能播了」的两个时机：挂流那一次可能太早，这两次是补刀
    const onReady = () => {
      if (isCurrentSession()) void tryAutoplay();
    };

    video.addEventListener("playing", onPlaying);
    video.addEventListener("pause", onPause);
    video.addEventListener("waiting", onWaiting);
    video.addEventListener("seeking", onSeeking);
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("ended", onEnded);
    video.addEventListener("durationchange", onDurationChange);
    video.addEventListener("timeupdate", onTimeUpdate);
    // 缓冲条不能只跟 timeupdate：暂停时它根本不发，而 hls.js 照样在往前缓，
    // 浅色底就一直停在按下暂停的那一刻，看不出还要等多久才能接着放。
    // progress 是「又收到数据了」的通知，暂停期间照发。
    video.addEventListener("progress", onBuffered);
    video.addEventListener("loadedmetadata", onReady);
    video.addEventListener("canplay", onReady);
    return () => {
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("waiting", onWaiting);
      video.removeEventListener("seeking", onSeeking);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("ended", onEnded);
      video.removeEventListener("durationchange", onDurationChange);
      video.removeEventListener("timeupdate", onTimeUpdate);
      video.removeEventListener("progress", onBuffered);
      video.removeEventListener("loadedmetadata", onReady);
      video.removeEventListener("canplay", onReady);
    };
  }, [state.session, video, qoe, tryAutoplay]);

  // ---------------------------------------------------------------------
  // 字幕：轨记忆来自 playback_state，用户明确关掉过就不要再自作主张打开
  // ---------------------------------------------------------------------

  useEffect(() => {
    if (subtitles.options.length === 0) return;
    // 用户亲手选过就不再插手：新会话的 watch 快照里躺着的是旧记忆轨，
    // 拿它覆盖刚点的选择等于把用户的操作悄悄撤销
    if (subtitleTouchedRef.current) return;
    // 服务端在烧录哪条，菜单就选中哪条——烧录会话里画面本身就是真值
    if (burnedSubtitle) {
      setSelectedSubtitle(burnedSubtitle);
      return;
    }
    setSelectedSubtitle(pickInitialSubtitle(subtitles.options, resume?.subtitle_track ?? null));
  }, [subtitles, resume?.subtitle_track, burnedSubtitle]);

  // ---------------------------------------------------------------------
  // 观看进度：开始 / 心跳 / 停止
  // ---------------------------------------------------------------------

  const trackRefs = useCallback(
    () => ({
      audio_track: state.session?.decision.audio?.track_ref ?? undefined,
      subtitle_track: selectedSubtitle ?? "off",
    }),
    [state.session, selectedSubtitle],
  );
  /** 供停止上报的 cleanup/pagehide 闭包读**当下**的轨选择。停止上报若不带
   * 轨，用户「切完字幕就退出」的那次选择会丢——下一次进来又回到旧轨
   * （切换后 10 秒内退出必现，PGS→文本尤其明显：进来直接又开始烧录）。 */
  const trackRefsRef = useRef(trackRefs);
  trackRefsRef.current = trackRefs;

  /**
   * 管理员在活动页结束了本次播放：退出播放器并说明原因。服务端同时进入
   * 拒绝窗口（取流与开会话都会被拒），所以这里绝不能走「会话没了就重开」
   * 那条路，直接退出才是对的。
   */
  const onEndedByAdmin = useCallback(() => {
    if (endedByAdminRef.current) return;
    endedByAdminRef.current = true;
    dispatch({
      type: "fatal",
      message: "管理员已结束本次播放",
      suggestion: "稍后可以重新开始播放；观看进度已经保存。",
    });
  }, []);

  /** 一次进度心跳；响应里带着「已被管理员结束」就退出。 */
  const sendProgress = useCallback(
    (paused: boolean | undefined) => {
      void reportPlaybackProgress(
        {
          ...unit,
          event: "progress",
          position_ms: positionRef.current,
          // 暂停态给活动页「正在播放」的徽标用；读元素原生状态，与 Jellyfin
          // 客户端上报的 IsPaused 同义
          paused,
          ...trackRefsRef.current(),
        },
        apiRef.current,
      )
        .then((watch) => {
          if (watch.ended_by_admin) onEndedByAdmin();
        })
        .catch(() => undefined);
    },
    // unit 是对象字面量，按内容比较
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [unitKey, onEndedByAdmin],
  );
  const sendProgressRef = useRef(sendProgress);
  sendProgressRef.current = sendProgress;

  useEffect(() => {
    if (state.phase !== "playing") return;
    if (reportedStartRef.current !== unitKey) {
      reportedStartRef.current = unitKey;
      void reportPlaybackProgress({ ...unit, event: "start", ...trackRefs() }, apiRef.current)
        .then((watch) => {
          if (watch.ended_by_admin) onEndedByAdmin();
        })
        .catch(() => undefined);
    }
    const timer = window.setInterval(
      () => sendProgress(video?.paused ?? undefined),
      PROGRESS_INTERVAL_MS,
    );
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.phase, unitKey, trackRefs, sendProgress]);

  /**
   * 暂停 / 恢复即时上报，不等下一次心跳：活动页「正在播放」的暂停徽标滞后
   * 十秒会让人以为没反应。只在本单元已经报过开始之后才报——否则一次早到的
   * pause 事件会在服务端凭空建出一个没有开始的会话。
   */
  useEffect(() => {
    if (!video) return;
    const report = (paused: boolean) => {
      if (reportedStartRef.current !== unitKey) return;
      sendProgressRef.current(paused);
    };
    const onPlaying = () => report(false);
    const onPause = () => report(true);
    video.addEventListener("playing", onPlaying);
    video.addEventListener("pause", onPause);
    return () => {
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("pause", onPause);
    };
  }, [video, unitKey]);

  /** 这次播放的质量快照。离开与卸载两条路径共用。 */
  const qoeSnapshot = useCallback(() => {
    const summary = summarize(qoeRef.current);
    if (!isReportable(summary)) return null;
    const decision = state.session?.decision;
    if (!decision || decision.tier == null) return null;
    return {
      library_file_id: decision.file_id ?? null,
      tier: decision.tier,
      degraded_from: decision.degraded_from ?? null,
      engine: engineRef.current?.stats().engine ?? "",
      hw_backend: state.session?.hw_backend ?? "",
      ...summary,
    };
  }, [state.session]);

  useEffect(() => {
    qoeSnapshotRef.current = qoeSnapshot;
  }, [qoeSnapshot]);

  /** 离开这一集（切集或退出播放器）时补一次停止上报，并交出质量快照。 */
  useEffect(() => {
    const snapshot = { ...unit };
    return () => {
      if (reportedStartRef.current === null) return;
      // 停止之后再到的暂停 / 心跳不能再上报：服务端会把刚结束的会话重建回来
      reportedStartRef.current = null;
      void reportPlaybackProgress(
        {
          ...snapshot,
          event: "stop",
          position_ms: positionRef.current,
          // 停止也要带轨记忆：切完字幕/音轨立刻退出的那次选择不能丢
          ...trackRefsRef.current(),
        },
        apiRef.current,
      ).catch(() => undefined);
      const metric = qoeSnapshotRef.current();
      if (metric) void reportPlaybackMetric(metric, apiRef.current).catch(() => undefined);
      qoeRef.current = initialQoe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitKey]);

  /** 关标签页/切走：进度用 sendBeacon 补发，会话用 keepalive 掐掉。 */
  useEffect(() => {
    const onPageHide = () => {
      if (reportedStartRef.current !== null) {
        reportPlaybackProgressOnUnload(
          {
            ...unit,
            event: "stop",
            position_ms: positionRef.current,
            // 同 SPA 离开路径：停止上报带上轨记忆
            ...trackRefsRef.current(),
          },
          apiRef.current,
        );
      }
      // iOS 切后台也触发 pagehide——画中画还播着呢，这时杀掉转码会话，
      // 小窗播完缓冲就断流。PiP 中不杀，真关页面后由服务端超时回收兜底。
      const webkitMode = (video as { webkitPresentationMode?: string } | null)
        ?.webkitPresentationMode;
      const inPip =
        (video && document.pictureInPictureElement === video) ||
        webkitMode === "picture-in-picture";
      if (sessionId && !inPip) stopPlaybackSessionOnUnload(sessionId, apiRef.current);
      const metric = qoeSnapshotRef.current();
      if (metric) reportPlaybackMetricOnUnload(metric, apiRef.current);
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitKey, sessionId]);

  /**
   * 首帧只能用 `requestVideoFrameCallback` 量。
   *
   * `canplay` / `playing` / `loadeddata` 全都早于真实出画（有时早几百毫秒），
   * 用它们量首帧会系统性偏乐观，然后困惑「数据好看但用户说慢」。
   */
  useEffect(() => {
    if (!video) return;
    const withFrameCallback = video as HTMLVideoElement & {
      requestVideoFrameCallback?: (cb: () => void) => number;
      cancelVideoFrameCallback?: (handle: number) => void;
    };
    if (!withFrameCallback.requestVideoFrameCallback) return;
    const handle = withFrameCallback.requestVideoFrameCallback(() => {
      qoe({ type: "first-frame", at: performance.now() });
    });
    return () => withFrameCallback.cancelVideoFrameCallback?.(handle);
  }, [video, state.session?.session_id, qoe]);

  /**
   * 每秒采一次观看时长与掉帧。掉帧既进 QoE 指标，也是**降档回路的真实证据**：
   * 决策层已不再预测流畅度（§12.15），「放不放得动」由这里持续掉帧超阈值来
   * 回答——报废当前档，failed_tiers 回路换转码重来。
   *
   * 只在视频**直通**（copy）时武装 watchdog：转码档的视频已经是 h264 了，
   * 再掉帧说明设备连转码产物都放不动，继续降档只会转得更狠、更卡。
   *
   * 两种假掉帧要挡住：后台标签页浏览器主动丢帧（不可见时不喂样本，回前台
   * 先清窗口）；seek 落点的瞬时掉帧（seeking 时清窗口）。换会话由 tracker
   * 的负增量自愈兜底，依赖里带 session_id 时 effect 重建也会重置。
   */
  useEffect(() => {
    if (!video) return;
    const tracker = createFrameDropTracker();
    const videoIsCopy = state.session?.decision.video?.action === "copy";
    const onVisibility = () => tracker.reset();
    const onSeeking = () => tracker.reset();
    document.addEventListener("visibilitychange", onVisibility);
    video.addEventListener("seeking", onSeeking);
    const timer = window.setInterval(() => {
      qoe({ type: "tick", at: performance.now(), playing: !video.paused && !video.ended });
      const stats = engineRef.current?.stats();
      if (stats?.totalFrames == null || stats.droppedFrames == null) return;
      qoe({ type: "frames", dropped: stats.droppedFrames, total: stats.totalFrames });
      if (!videoIsCopy || document.visibilityState !== "visible" || video.paused) return;
      const verdict = tracker.sample({ dropped: stats.droppedFrames, total: stats.totalFrames });
      if (verdict.degrade) {
        dispatch({
          type: "failed",
          reason: `直通播放持续掉帧（${Math.round((verdict.ratio ?? 0) * 100)}%），正在换转码重试`,
        });
      }
    }, 1000);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      video.removeEventListener("seeking", onSeeking);
    };
  }, [video, qoe, state.session]);

  /**
   * 进度条缩略图：服务端在开会话时后台生成，这里轮询到就绪为止。
   *
   * 轮询而不是等推送：生成时长取决于片长与磁盘，几秒到几十秒都有可能，为它
   * 拉一条长连接不值当。没就绪就是没有预览，不影响播放，所以失败一律吞掉；
   * 试满十次（约两分钟）还拿不到就是这部片生成不了，别一直打服务端。
   */
  useEffect(() => {
    const source = state.session?.stream_url;
    if (!source) return;
    let cancelled = false;
    let attempts = 0;
    let timer = 0;
    const tick = async () => {
      attempts += 1;
      try {
        const index = await fetchTrickplay(source);
        if (cancelled) return;
        if (index?.ready) {
          setTrickplay(index);
          return;
        }
      } catch {
        // 预览不是关键路径，静默重试
      }
      if (!cancelled && attempts < 10) timer = window.setTimeout(tick, 12_000);
    };
    timer = window.setTimeout(tick, 1_000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [state.session?.stream_url]);

  /**
   * 会话续命 + 掉线自愈 + 离开时显式收尾。
   *
   * 心跳：用户关页面不会发任何信号，服务端的超时回收是唯一可靠兜底。
   *
   * **自愈**：心跳并不总喂得上——页面切到后台后浏览器会把定时器节流到一分钟
   * 一次，挂久了干脆整页冻结（Chrome 的 tab freezing），暂停着离开一会儿再
   * 回来，服务端那边的会话早被巡检回收了。此时流的地址还在、取流 token 也
   * 没过期（有效期 12 小时），但分片已经不存在，表现就是**一直转圈**，只能
   * 刷新页面——这次修的就是它。
   *
   * 做法是把心跳同时当探活用：服务端明确说会话没了（404）就带着当前位置原地
   * 重开一个（`restart`，与换音轨/换画质同一条路），而不是走降档回路——这一
   * 档并没有失败，只是流没了，白降一档反而会把画质压下去。用户自己按过暂停
   * 的话新会话不会自动续播（`wantsPlayRef` 说了算），他回来点播放即可。
   *
   * 页面在后台时只探不修：那时重开会话等于替一个没人看的页面拉起一路 ffmpeg，
   * 留到 `visibilitychange` 回前台再修。
   *
   * 收尾：`pagehide` 只在真正卸载文档时触发，SPA 内部返回媒体库、切下一集
   * 都不会触发。释放的时机与 StrictMode 规避都在 `createSessionReleaser` 里，
   * 那边有单测；这里只负责「什么时候调它」。
   */
  useEffect(() => {
    if (!sessionId) return;
    sessionReleaser.acquire(sessionId);
    // 一次探活没回来之前不发下一次：回前台那一下与定时器可能撞在一起，
    // 两次都判定「没了」就会连开两个会话，其中一个当场变成孤儿
    let probing = false;
    const beat = async () => {
      if (probing) return;
      probing = true;
      try {
        const alive = await pingPlaybackSession(sessionId, apiRef.current);
        // null = 这次请求本身失败（断网/5xx），不能据此判定会话没了
        if (alive !== false) return;
        // 探活途中会话已经换了（换字幕烧录/换音轨/换画质重开）：这个 404
        // 说的是上一个会话，拿它去 restart 会让服务端 stop_for_file 把刚
        // 起好的新会话一起杀掉（见 sessionIdRef 的注释）
        if (sessionIdRef.current !== sessionId) return;
        if (document.visibilityState !== "visible") return;
        // 只在「确实在播/该播」的阶段重开。重开路上（deciding /
        // session-starting / degrading，此时 state.session 还是旧会话）再触发
        // 一次会形成风暴：新会话没就绪前每次心跳都 404，而每次重开都会让
        // 服务端把上一次刚起的会话杀掉（stop_for_file），永远收敛不了。
        // 报错 / 同意弹窗 / 播完同理——那些界面正等用户拍板，别在背后换流。
        const phase = phaseRef.current;
        if (phase !== "playing" && phase !== "buffering" && phase !== "seeking") return;
        if (video?.paused && !wantsPlayRef.current) {
          // 用户自己暂停着：不替他白烧一路转码，等他点播放再重开
          deadSessionRef.current = true;
          return;
        }
        // 客户端缓冲里可能还有几十秒余粮在放，先停住：换流期间进度条不该
        // 继续跳（与换音轨/换画质同一处理）。这次暂停是我们造成的，不算
        // 用户表态——wantsPlay 保持原值，新会话就位后自动续播。
        video?.pause();
        pendingFileMsRef.current = positionRef.current;
        dispatch({ type: "restart", startMs: positionRef.current });
      } finally {
        probing = false;
      }
    };
    const timer = window.setInterval(() => void beat(), PING_INTERVAL_MS);
    const onVisibility = () => void beat();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      sessionReleaser.release(sessionId);
    };
  }, [sessionId, sessionReleaser, video]);

  // ---------------------------------------------------------------------
  // 播放控制
  // ---------------------------------------------------------------------

  const togglePlay = useCallback(() => {
    if (!video) return;
    if (video.paused) {
      // 用户亲手点的播放：手势就在这一刻，一定放得起来；中央大播放键该消失。
      wantsPlayRef.current = true;
      autoplayLastRef.current = null;
      setAutoplay(null);
      if (deadSessionRef.current) {
        // 暂停期间会话已被服务端回收（见心跳探活）：流是死的，play() 只会
        // 永远转圈。带着当前位置原地重开，新会话就位后自动续播。
        deadSessionRef.current = false;
        pendingFileMsRef.current = positionRef.current;
        dispatch({ type: "restart", startMs: positionRef.current });
        return;
      }
      void video.play().catch(() => undefined);
    } else {
      // 用户主动暂停：从这一刻起不再替他自动续播
      wantsPlayRef.current = false;
      video.pause();
    }
  }, [video]);

  /** 供触摸/点击回调读最新的锁屏态：它们绑在 video 元素上，不重绑 */
  const lockedRef = useRef(false);
  lockedRef.current = locked;
  /** 长按倍速松手后要吞掉的那一次 click */
  const suppressClickRef = useRef(false);
  /** 上一次轻点：时刻 + 那一下之前控制层的显隐（双击时要恢复回去） */
  const tapRef = useRef<{ at: number; chromeBefore: boolean } | null>(null);

  /**
   * 锁屏态下把解锁键唤出来，3 秒后自己收。
   *
   * 常显是不行的：锁屏本来就是「别让我碰到任何东西」，画面上却挂着一颗
   * 亮着的按钮，等于把误触目标从整块屏幕缩成一个点而不是消掉。
   */
  const revealLock = useCallback(() => {
    setLockHint(true);
    if (lockHintTimerRef.current !== null) window.clearTimeout(lockHintTimerRef.current);
    lockHintTimerRef.current = window.setTimeout(() => {
      lockHintTimerRef.current = null;
      setLockHint(false);
    }, 3000);
  }, []);

  /** 锁上的那一刻把控制层收掉：锁屏还挂着一排能点的按钮说不过去 */
  useEffect(() => {
    if (locked) setChromeVisible(false);
  }, [locked]);

  useEffect(
    () => () => {
      if (lockHintTimerRef.current !== null) window.clearTimeout(lockHintTimerRef.current);
    },
    [],
  );

  /**
   * 点画面只负责控制层的显隐：藏着就唤出，露着就收起。
   *
   * 播放/暂停只属于按钮和快捷键——整块画面都是暂停键的话，找按钮时点偏
   * 一点、擦一下屏幕，片子就停了（真机反馈的误触来源）。Netflix / YouTube
   * 手机端同款取舍：画面点击 = 控制层开关，动作 = 各自的按钮。
   * 按「按下瞬间」的显隐态取反（容器 pointerdown 记录）：滑动手势没有
   * click，不会走到这里——控制层的显隐只回应真正的轻点。暂停中收不掉是
   * 有意的——chromeMustStayVisible 会立刻把它拉回来，暂停画面本来就该
   * 带着控制条。
   *
   * **触屏的双击另有含义**（左右三分之一 = 退/进十秒，§2.B1）：这推翻了
   * 从前「触屏双击就是两次控制层开关、净效果回到原状」的取舍——双击左右
   * 快退快进已经是手机上的肌肉记忆，白白空着不值。判定在 tap.ts。
   */
  const onSurfaceClick = useCallback(
    (event: React.MouseEvent<HTMLVideoElement>) => {
      // 锁屏中：轻点只把解锁键唤出来 3 秒，不碰控制层也不跳转
      if (lockedRef.current) {
        revealLock();
        return;
      }
      // 长按倍速松手时浏览器仍会补一次 click，吞掉它——否则每次长按结束
      // 都顺带把控制层切一下
      if (suppressClickRef.current) {
        suppressClickRef.current = false;
        return;
      }
      // 伪横屏下画面整体转了 90°，用物理 clientX 分左右会把三等分转到
      // 竖直方向上去——换算与进度条同源（touch-adjust.ts 的 pointerOffsetX）
      const { offset, length } = pointerOffsetX(
        event,
        event.currentTarget.getBoundingClientRect(),
        fakeLandscapeRef.current,
      );
      const xRatio = length > 0 ? offset / length : 0.5;
      const now = performance.now();
      const previous = tapRef.current;
      // 双击跳转只属于触屏：桌面的双击是全屏（onSurfaceDoubleClick），
      // 一个手势不能在同一种指针上有两种含义
      const action =
        lastPointerTypeRef.current === "mouse"
          ? ({ type: "chrome" } as const)
          : resolveTap({ nowMs: now, lastTapMs: previous?.at ?? null, xRatio });
      if (action.type === "seek") {
        // 第二下：撤掉第一下对控制层的开关，净效果只剩跳转
        if (previous) setChromeVisible(previous.chromeBefore);
        // **链不能断在这里**：连点第三下、第四下要继续累加（YouTube 同款，
        // 点三下就是 30 秒）。清成 null 的话第三下会退回「开关控制层」，
        // 而胶囊上的累加读数还在往上走——手上的动作与屏幕说的对不上。
        tapRef.current = { at: now, chromeBefore: previous?.chromeBefore ?? chromeVisible };
        seekByRef.current(action.seconds);
        // 双击是「盲操作」：没有按钮的按下反馈，必须给方向提示，
        // 且连点累加（点三下显示 30 秒，与键盘同一套胶囊）
        flashSeek(action.seconds);
        return;
      }
      tapRef.current = { at: now, chromeBefore: chromeWasVisibleRef.current };
      setChromeVisible(!chromeWasVisibleRef.current);
    },
    [flashSeek, revealLock, chromeVisible],
  );

  /**
   * 换音轨。
   *
   * 音轨在开会话时就被 ffmpeg `-map` 进命令里了，改不了——所以换轨走的是和
   * 「seek 拖出已转区间」完全一样的路：停住旧流、记下当前位置、重开会话。
   * 中间会有一次约一秒的停顿，菜单里已经写明。
   *
   * 点的就是正在放的那条时只记住、不重开：那一秒停顿白花了。
   */
  const selectAudio = useCallback(
    (ref: string) => {
      setRequestedAudio(ref);
      if (ref === (state.session?.decision.audio?.track_ref ?? null)) return;
      // 换会话会让画面空一段，先把当前帧冻住盖上去
      freezeFrame();
      // 换流期间先把旧流停住，否则进度条会在新会话起来前继续跳
      video?.pause();
      // 这次暂停是我们造成的，不是用户不想看——换完要接着播
      wantsPlayRef.current = true;
      pendingFileMsRef.current = positionRef.current;
      dispatch({ type: "request", startMs: positionRef.current });
    },
    [state.session, video, freezeFrame],
  );

  /**
   * 换字幕。
   *
   * 文本轨（VTT/ASS）纯前端换：旁挂渲染层立即切，零停顿。涉及**烧录**的
   * 两种情况要走「换音轨」同一条重开会话的路（约一秒停顿，菜单里写明）：
   * - 选中 PGS 轨 → 服务端转码并把字幕压制进画面（Emby 语义）；
   * - 当前正在烧录、换成文本轨/关闭 → 撤下烧录回到直通策略。
   */
  const selectSubtitle = useCallback(
    (ref: string | null) => {
      subtitleTouchedRef.current = true;
      setSelectedSubtitle(ref);
      const target = ref ? subtitles.options.find((o) => o.ref === ref) : null;
      const wantBurn = target?.kind === "pgs";
      if (!wantBurn && burnedSubtitle === null) return; // 纯文本切换，前端搞定
      if (wantBurn && burnedSubtitle === ref) return; // 已在烧这条，白重启
      setRequestedSubtitle(ref ?? "off");
      freezeFrame();
      video?.pause();
      wantsPlayRef.current = true;
      pendingFileMsRef.current = positionRef.current;
      dispatch({ type: "request", startMs: positionRef.current });
    },
    [subtitles, burnedSubtitle, video, freezeFrame],
  );

  /**
   * 烧录撞上软件转码同意：**自动退回旁挂渲染，不打断观看**。
   *
   * 用户点的是「换条字幕」，不是「给我弹一个转码协商」。服务器没显卡又没
   * 开软转时，把烧录意图撤掉重开会话——canvas 旁挂照样能看（只是进不了
   * 画中画），观看不中断。只有非烧录导致的 consent（片子本来就要软转）才
   * 走原来的弹窗。记忆轨触发的烧录（requestedSubtitle 为 null）同样弹窗：
   * 那是用户上次的明确选择，值得问一次。
   */
  const burnFallbackRef = useRef(false);
  useEffect(() => {
    if (state.phase !== "consent") {
      burnFallbackRef.current = false;
      return;
    }
    if (burnFallbackRef.current) return;
    if (requestedSubtitle && requestedSubtitle !== "off") {
      burnFallbackRef.current = true;
      setRequestedSubtitle("off");
      dispatch({ type: "reset" });
      dispatch({ type: "request", startMs: positionRef.current });
    }
  }, [state.phase, requestedSubtitle]);

  /**
   * 换画质上限。与换音轨同一条路：重开会话（约一秒停顿）。
   *
   * 唯一跳过重启的情况：当前是直通（copy）且新上限装得下源分辨率——
   * 此时服务端会给出一模一样的计划，重启纯属白断一次。videoHeight 在
   * copy 档就是源高度，可以直接拿来判。
   */
  const selectQuality = useCallback(
    (maxHeight: number | null) => {
      setQuality(maxHeight);
      saveQualityPreference(maxHeight);
      if (maxHeight === quality) return;
      const copying = state.session?.decision.video?.action === "copy";
      if (copying && (maxHeight === null || (video?.videoHeight ?? 0) <= maxHeight)) return;
      // 换会话会让画面空一段，先把当前帧冻住盖上去
      freezeFrame();
      video?.pause();
      wantsPlayRef.current = true;
      pendingFileMsRef.current = positionRef.current;
      // 用户主动切档是新的播放尝试：清掉自动降档历史，并让旧会话先进入
      // cleanup，避免上一个硬件档的迟到错误把本次请求推回软件档。
      dispatch({ type: "request", startMs: positionRef.current });
    },
    [quality, state.session, video, freezeFrame],
  );

  /**
   * 跳转。转码会话是「从 start_ms 起、边转边给」的单向流：拖出已转区间必须
   * 换会话，干等 ffmpeg 追上来只会让用户看着永远转不完的圈。
   */
  const seekToFileMs = useCallback(
    (rawFileMs: number) => {
      if (!video) return;
      // 先夹进片长之内：越过片尾的落点会让换会话那条路开出一个转不出任何
      // 东西的会话（理由见 timeline.ts 的 clampSeekTarget）。
      const fileMs = clampSeekTarget(rawFileMs, durationMs);
      // 会话正在重开的空档（换音轨 / 换画质 / 心跳自愈 / 上一次 seek 换流）：
      // video 上已经没有流了，native seek 打在空元素上什么也不会发生，而在途
      // 的新会话仍会落回 pendingFileMs 那个**旧**位置——用户看到进度条跳过去
      // 又自己弹回来，这一拖白拖。改走换会话这条路：restart 递增 attempt 顶掉
      // 在途的那次请求，新会话直接从目标位置起。
      // 报错页与同意弹窗同样没有会话，但它们是等用户拍板的终态（见
      // machine.ts 的 awaitsUserDecision），绝不能在背后替他换流。
      if (!state.session) {
        // startMs 为 null = 首次起播，起点还等着服务端按观看状态定（§6.10）。
        // 那一刻按下的快进键不该把续播点顶掉，让它照旧落空。
        if (!isBusy(state.phase) || state.startMs === null) return;
        pendingFileMsRef.current = fileMs;
        setPositionMs(fileMs);
        dispatch({ type: "restart", startMs: fileMs });
        return;
      }
      const seekable = video.seekable;
      const plan = planSeek(fileMs, {
        startMs: startMsRef.current,
        seekableEndSeconds: seekable.length ? seekable.end(seekable.length - 1) : 0,
        // 是否越界换会话由播放模式定：VOD/档 0 列表覆盖全片，永远列表内跳
        hasSession: Boolean(sessionId) && (mode?.seekBeyondBufferedRestarts ?? false),
      });
      if (plan.kind === "native") {
        const seconds = Math.max(0, plan.seconds);
        // 落点在缓冲之外：hls.js 会清掉当前这段缓冲从新落点重新装载，中间
        // 那几百毫秒到几秒（外网上就是几秒）`<video>` 一帧都没有，浏览器只
        // 能画黑。先把当前帧冻住盖上去，等新位置出画再撤。缓冲之内的跳转
        // 不冻——数据在手上，本来就不会黑，多盖一层反而会让本该瞬时的跳转
        // 看着像卡了一下。
        // 带上落点：远跳盖的是**落点的缩略图**而不是上一帧，这一跳视觉上
        // 当场就落地（§2.G4）。
        if (!isWithinRanges(video.buffered, seconds)) freezeFrame(fileMs);
        if (engineRef.current?.seek) engineRef.current.seek(seconds);
        else video.currentTime = seconds;
        // 乐观更新进度条：seek 落到未缓冲区间（往回拖出 back buffer、往前
        // 拖到没转的段）时，规范只在 seek **完成**后才发 timeupdate——
        // 服务端供片要一两秒，这期间进度条会弹回旧位置，用户以为没拖上。
        // currentTime 已经同步改过去了，把 UI 一起带过去；seek 完成后的
        // timeupdate 读同一个值，不会跳。
        setPositionMs(toFileMs(seconds, startMsRef.current));
        return;
      }
      // 换会话必然让画面空一段（旧引擎销毁会把 src 摘干净），先把画面冻住；
      // 这条路恒是远跳，盖落点的缩略图
      freezeFrame(plan.startMs);
      // 换会话期间先把旧流停住：不停的话旧会话还在往前走，进度条会在新会话
      // 起来之前继续跳动，看着像"拖了没反应"
      video.pause();
      // 这次暂停是我们自己造成的，不是用户不想看了——换流后必须继续自动播放
      wantsPlayRef.current = true;
      pendingFileMsRef.current = plan.startMs;
      setPositionMs(plan.startMs);
      dispatch({ type: "restart", startMs: plan.startMs });
    },
    [video, sessionId, mode, durationMs, state.session, state.phase, state.startMs, freezeFrame],
  );

  /**
   * 连按快进/快退的累积落点（docs/design/player-feel.md §2.B4）。
   *
   * 转码会话里一次 seek 可能就是一次「杀 ffmpeg 换会话」，连按三次 ⟳10
   * 发三次 seek 就是黑三下才走到 +30 秒。所以按键只累积落点、立刻更新读数，
   * 静默 400ms 后提交唯一一次跳转。null = 没有在途的累积。
   */
  const [pendingSeekMs, setPendingSeekMs] = useState<number | null>(null);
  const pendingSeekRef = useRef<{ targetMs: number | null; timer: number | null }>({
    targetMs: null,
    timer: null,
  });

  /** 撤销在途的累积（进度条拖动、横滑等其它跳转入口接管时必须先清） */
  const cancelPendingSeek = useCallback(() => {
    const pending = pendingSeekRef.current;
    if (pending.timer !== null) window.clearTimeout(pending.timer);
    pending.timer = null;
    pending.targetMs = null;
    setPendingSeekMs(null);
  }, []);

  /** 供延时提交的合并计时器读最新值：它跨过一段时间才执行 */
  const seekToFileMsRef = useRef(seekToFileMs);
  seekToFileMsRef.current = seekToFileMs;

  // 上下界都由 nextSeekTarget / seekToFileMs 里的 clampSeekTarget 收住
  const seekBy = useCallback(
    (seconds: number) => {
      const target = nextSeekTarget({
        pendingMs: pendingSeekRef.current.targetMs,
        positionMs: positionRef.current,
        deltaMs: seconds * 1000,
        durationMs: durationMsRef.current,
      });
      const windowMs = seekBatchWindowMs({
        hasSession: Boolean(sessionIdRef.current),
        buffered: isCheapSeekRef.current(target),
      });
      if (windowMs <= 0) {
        // 便宜的跳转立刻执行，手感最好——这里合并只是凭空加延迟
        cancelPendingSeek();
        seekToFileMs(target);
        return;
      }
      const pending = pendingSeekRef.current;
      pending.targetMs = target;
      // 读数立刻跟上：进度条与时间文字都走 overrideMs，用户按下就看到落点
      setPendingSeekMs(target);
      if (pending.timer !== null) window.clearTimeout(pending.timer);
      pending.timer = window.setTimeout(() => {
        pending.timer = null;
        const committed = pending.targetMs;
        pending.targetMs = null;
        setPendingSeekMs(null);
        // 走 ref 而不是闭包里的那个：这 400 毫秒里会话可能已经换过一轮
        // （降档、换轨、心跳自愈），旧闭包会拿着过期的 session/startMs 去跳
        if (committed !== null) seekToFileMsRef.current(committed);
      }, windowMs);
    },
    [seekToFileMs, cancelPendingSeek],
  );

  /** 供点击/触摸回调读最新值：它们定义在 seekBy 之前，且不该跟着它重绑 */
  const seekByRef = useRef(seekBy);
  seekByRef.current = seekBy;

  /**
   * 换集与卸载都要撤掉在途的合并计时器。
   *
   * **播放器在切集时不重挂**（player-page 没给它 key，只换 unit），所以那个
   * 400 毫秒的计时器会活过切集：用户在片尾按一下快进、随手点了「下一集」，
   * 400 毫秒后它就把**新一集**跳到上一集的落点上去。
   */
  useEffect(() => {
    // 这个 ref 装的对象身份恒定（只改字段不换对象），可以放心带进 cleanup
    const pending = pendingSeekRef.current;
    cancelPendingSeek();
    return () => {
      if (pending.timer !== null) window.clearTimeout(pending.timer);
    };
  }, [unitKey, cancelPendingSeek]);

  /**
   * 拖动进度条时的实时跟随（docs/design/player-feel.md §2.C2）。
   *
   * 松手才提交是转码会话逼出来的规矩：拖动中每次 move 都跳会让服务端一路杀
   * ffmpeg 重启，画面永远追不上手指。但**跳转不要钱的时候没有理由不跟随**：
   *
   * - 档 0 直出：整个文件都能跳，浏览器自己按 range 取数据；
   * - 任何模式落在已缓冲区间内：数据就在手上，跳过去是零成本。
   *
   * 其余情况（拖到没缓冲的地方、旧会话相对制）原样按下不表，等松手那一次。
   * 100ms 节流：hls.js 在列表内跳转会取消在途的分片请求，一秒跳六十次反而
   * 让缓冲永远建立不起来。
   */
  /**
   * 拖动跟随自己写 currentTime 的节流与计数。
   *
   * `count` 是**这一次拖动里写到第几次**：第一次要照常算作用户跳转（他确实
   * 跳了），第二次起是同一个动作的延续——一次拖动写十几次，全算进 QoE 的话
   * 「拖动次数」就从「用户跳了几次」变成「写了几次 currentTime」，指标废掉。
   */
  const scrubRef = useRef({ at: 0, count: 0 });

  /**
   * 这一跳贵不贵：落点已在缓冲里、或档 0 直出（整个文件随便跳）就是零成本，
   * 否则转码会话要按分片请求把 ffmpeg 杀掉重启直奔目标。
   *
   * 拖动实时跟随（只在便宜时跟）与连按合并（只在贵时等）共用同一条判据。
   */
  const isCheapSeek = useCallback(
    (fileMs: number) => {
      if (!video) return false;
      if (mode?.engine === "direct") return true;
      return isWithinRanges(video.buffered, toSessionSeconds(fileMs, startMsRef.current));
    },
    [video, mode],
  );
  const isCheapSeekRef = useRef(isCheapSeek);
  isCheapSeekRef.current = isCheapSeek;

  const scrubTo = useCallback(
    (fileMs: number) => {
      // 拖动就是活动：不重排自动隐藏的倒计时的话，手指按着不动四秒钟，
      // 控制条会**在拖动过程中**淡出——指针捕获让拖动照旧生效，用户却是
      // 对着一条看不见的进度条在拖，松手才知道跳到了哪儿。
      bumpChromeActivity();
      if (!video) return;
      const now = performance.now();
      if (now - scrubRef.current.at < 100) return;
      const seconds = toSessionSeconds(fileMs, startMsRef.current);
      if (seconds < 0 || !isCheapSeek(fileMs)) return;
      // 500 毫秒之内的连续写视为同一次拖动
      const continuing = now - scrubRef.current.at < 500;
      scrubRef.current = { at: now, count: continuing ? scrubRef.current.count + 1 : 0 };
      // **只动 currentTime，不走 engine.seek**：后者会 stopLoad + startLoad
      // 把在途的分片请求全掐掉重来——那是给「跳到没缓冲的地方」准备的重手段。
      // 拖动跟随只在数据已经在手上时才发生（isCheapSeek），一秒十次地掐断
      // 加载管线，恰恰会把这条路本来想改善的手感反过来毁掉。
      // fastSeek 是浏览器为「拖动预览」准备的：就近落在关键帧上，省掉从
      // 关键帧解到精确帧的那段解码。Safari / Firefox 有，Chrome 至今没有，
      // 所以要探测。松手那次提交仍走精确 seek——落点差半秒用户是看得出来的。
      if (typeof video.fastSeek === "function") video.fastSeek(seconds);
      else video.currentTime = seconds;
    },
    [video, isCheapSeek, bumpChromeActivity],
  );

  /**
   * 进度条拖动与横滑落点的提交入口：先撤掉在途的连按累积，再跳。
   *
   * 不撤的话，用户「连按两下快进又改主意去拖进度条」时，那个 400ms 的
   * 计时器会在拖动落地之后再把画面拽回连按的落点。
   */
  const commitSeek = useCallback(
    (fileMs: number) => {
      cancelPendingSeek();
      seekToFileMs(fileMs);
    },
    [cancelPendingSeek, seekToFileMs],
  );
  /** 供触摸手势回调读最新值：跟着依赖重绑 touch 监听会在手势中途换掉监听器 */
  const commitSeekRef = useRef(commitSeek);
  commitSeekRef.current = commitSeek;

  useEffect(() => {
    // 粗指针（手指）就是能转的设备。不再要求 orientation.lock 存在：
    // iPhone Safari 没有它，但可以走 CSS 伪横屏，按钮照样要给
    setCanRotate(window.matchMedia("(pointer: coarse)").matches);
    const landscapeQuery = window.matchMedia("(orientation: landscape)");
    const sync = () => setDeviceLandscape(landscapeQuery.matches);
    sync();
    landscapeQuery.addEventListener("change", sync);
    return () => landscapeQuery.removeEventListener("change", sync);
  }, []);

  /**
   * 画中画的能力探测与状态跟随。
   *
   * `disablePictureInPicture` 也要看：视频自己声明了不许进小窗时，
   * 标准 API 存在但调用必然被拒。
   */
  useEffect(() => {
    if (!video) {
      setCanPip(false);
      setPipActive(false);
      return;
    }
    const webkit = video as WebkitPresentationVideo;
    setCanPip(
      (document.pictureInPictureEnabled === true && !video.disablePictureInPicture) ||
        webkit.webkitSupportsPresentationMode?.("picture-in-picture") === true,
    );
    // 小窗可以被用户从系统 UI 直接关掉，按钮状态只能跟着事件走，不能自己记
    const sync = () =>
      setPipActive(
        document.pictureInPictureElement === video ||
          webkit.webkitPresentationMode === "picture-in-picture",
      );
    sync();
    video.addEventListener("enterpictureinpicture", sync);
    video.addEventListener("leavepictureinpicture", sync);
    video.addEventListener("webkitpresentationmodechanged", sync);
    return () => {
      video.removeEventListener("enterpictureinpicture", sync);
      video.removeEventListener("leavepictureinpicture", sync);
      video.removeEventListener("webkitpresentationmodechanged", sync);
    };
  }, [video]);

  /**
   * 进 / 出画中画。
   *
   * 只在用户手势里调用才合法（浏览器硬性要求），所以这件事必须有一颗真按钮，
   * 不能挂在可见性变化之类的自动时机上——「切走标签页自动进小窗」那条路是
   * 另一套机制（媒体会话动作，见下面的 mediaSession）。
   * 失败一律吞掉：用户在系统弹窗里点了取消不是错误。
   */
  const togglePip = useCallback(() => {
    if (!video) return;
    const webkit = video as WebkitPresentationVideo;
    if (webkit.webkitSetPresentationMode && document.pictureInPictureEnabled !== true) {
      const target =
        webkit.webkitPresentationMode === "picture-in-picture" ? "inline" : "picture-in-picture";
      webkit.webkitSetPresentationMode(target);
      // 这个调用是同步 void：被系统拒绝时**什么都不发生**（模式不变、无异常、
      // 无事件），按钮看起来就是「点了没反应」——真机上没法排查。稍等半秒查
      // 模式有没有真的切过去，没切就把拒绝这件事说出来。
      // 最常见的拒绝就是 iOS 的桌面网页应用（PWA）形态：WebKit 的独立容器
      // 不给网页画中画的通路，webkitSupportsPresentationMode 却照样报 true，
      // 网页侧无解——能做的只有把去处说清楚（Safari 里打开就能用）。
      if (target === "picture-in-picture") {
        window.setTimeout(() => {
          if (webkit.webkitPresentationMode !== "picture-in-picture") {
            const standalone =
              (navigator as { standalone?: boolean }).standalone === true;
            flashNotice(
              standalone
                ? "iOS 桌面应用暂不支持画中画，请在 Safari 中打开本站使用"
                : "系统未开启画中画（低电量模式或当前环境可能不支持）",
            );
          }
        }, 500);
      }
      return;
    }
    if (document.pictureInPictureElement === video) {
      void document.exitPictureInPicture().catch(() => undefined);
      return;
    }
    void video.requestPictureInPicture?.().catch((error: unknown) => {
      // 静默失败是「点了没反应」类反馈的根源：把浏览器的拒绝翻成人话
      const name = typeof error === "object" && error && "name" in error ? String((error as { name: unknown }).name) : "";
      flashNotice(
        name === "NotAllowedError"
          ? "浏览器拒绝了画中画（需要页面处于可交互状态）"
          : "画中画启动失败（此视频源或环境不支持）",
      );
      console.warn("画中画请求被拒绝：", error);
    });
  }, [video, flashNotice]);

  /**
   * 全屏 / 退出全屏。与横屏是两个独立按钮：横屏管方向、全屏管铺满，
   * 移动端两个都要（真横屏本身会带进全屏，此时这个键就是退出键）。
   */
  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
      return;
    }
    const target = containerRef.current;
    if (!target) return;
    void requestFullscreen(target).then((ok) => {
      // iPhone Safari 没有元素级全屏，把 <video> 交给系统播放器。字幕层
      // 跟不进去，但 video 上挂着原生 VTT 轨（见 pipSubtitleUrl）——系统
      // 播放器渲染 mode="showing" 的原生 cue，进出时由 presentationMode
      // 监听切换，字幕不丢
      if (!ok) enterNativeVideoFullscreen(asNativeVideo(video));
    });
  }, [video]);

  /** 桌面惯例：双击画面进/出全屏（YouTube/Netflix 网页端同款）。只认鼠标——
   * 触屏的双击另有含义（左右三分之一 = 退/进十秒，见 onSurfaceClick），
   * 一个手势不能在同一种指针上有两种结果。 */
  const onSurfaceDoubleClick = useCallback(() => {
    if (lastPointerTypeRef.current !== "mouse") return;
    toggleFullscreen();
  }, [toggleFullscreen]);

  /** 横屏 / 退出横屏（真锁或 iOS 伪横屏），只在触屏设备上出现。 */
  const toggleLandscape = useCallback(() => {
    if (landscape || document.fullscreenElement) {
      setLandscape(false);
      setFakeLandscape(false);
      void exitLandscape(
        screenOrientation(),
        document.fullscreenElement ? () => document.exitFullscreen() : null,
      );
      return;
    }
    const target = containerRef.current;
    if (!target) return;
    void enterLandscape(target, screenOrientation()).then((outcome) => {
      // unsupported = 连元素级全屏都没有（iPhone Safari）。以前是把 <video>
      // 丢给系统播放器，但那样字幕/降档/自动下一集全部失效，也没有横屏键——
      // 改走 CSS 伪横屏，自定义 UI 原样保留
      if (outcome === "unsupported") setFakeLandscape(true);
      setLandscape(true);
    });
  }, [landscape]);

  /**
   * 用户按 Esc / 系统手势退出全屏时把状态同步回来，并解掉方向锁。
   *
   * 不解锁的后果很难查：退出播放器之后整个站点还被钉在横屏，用户只会觉得
   * 「这网站把我手机转坏了」。
   */
  useEffect(() => {
    const onChange = () => {
      const active = Boolean(document.fullscreenElement);
      setFullscreen(active);
      if (active) return;
      setLandscape(false);
      void exitLandscape(screenOrientation(), null);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  /** 离开播放器时一定要把方向锁还回去，哪怕是被直接卸载的。 */
  useEffect(() => {
    return () => {
      void exitLandscape(
        screenOrientation(),
        typeof document !== "undefined" && document.fullscreenElement
          ? () => document.exitFullscreen()
          : null,
      );
    };
  }, []);

  /** 左上角的后退：先退横屏、再退全屏，最后一层才是退出播放。 */
  const goBack = useCallback(() => {
    if (landscape) toggleLandscape();
    else if (document.fullscreenElement) void document.exitFullscreen();
    else onExit();
  }, [landscape, toggleLandscape, onExit]);

  /** 键盘快捷键按 YouTube 惯例，映射本身在纯函数里。 */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const action = resolveShortcut({
        key: event.key,
        inEditable: isEditableTarget(event.target),
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        altKey: event.altKey,
      });
      if (!action) return;
      event.preventDefault();
      setChromeVisible(true);
      bumpChromeActivity();
      switch (action.type) {
        case "toggle-play":
          togglePlay();
          break;
        case "seek-by":
          seekBy(action.seconds);
          // 键盘是「盲操作」：不给方向提示，用户只能盯着进度条数字猜有没有
          // 按上。按钮点击不发这个提示——按钮自己就是可见反馈
          flashSeek(action.seconds);
          break;
        case "seek-percent":
          // 走 commitSeek：数字键是「跳到片子的某个比例」这种绝对跳转，
          // 必须先撤掉在途的连按累积，否则 400 毫秒后它会把画面拽回去
          if (durationMs) commitSeek((durationMs * action.percent) / 100);
          break;
        case "volume-by":
          if (video) {
            video.volume = Math.min(1, Math.max(0, video.volume + action.delta));
            flashAdjust({ kind: "volume", value: video.volume, muted: video.muted });
          }
          break;
        case "toggle-mute":
          if (video) {
            video.muted = !video.muted;
            flashAdjust({ kind: "volume", value: video.volume, muted: video.muted });
          }
          break;
        case "toggle-fullscreen":
          toggleFullscreen();
          break;
        case "step-frame":
          // 逐帧只在暂停时有意义（播着的画面根本看不出走了一帧），
          // 与 jellyfin 的 seekFrames 同一条规矩。帧率取台账真值。
          if (video?.paused) {
            // 同上：逐帧是精确到一帧的绝对定位，在途的连按累积必须先作废
            cancelPendingSeek();
            const fps = state.session?.source?.frame_rate || FALLBACK_FRAME_RATE;
            video.currentTime = Math.max(0, video.currentTime + action.direction / fps);
          }
          break;
        case "toggle-subtitles":
          setSelectedSubtitle((current) =>
            current ? null : (subtitles.options[0]?.ref ?? null),
          );
          break;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [togglePlay, seekBy, commitSeek, cancelPendingSeek, toggleFullscreen, durationMs, video, state.session, subtitles.options, flashAdjust, flashSeek, bumpChromeActivity]);

  // ---------------------------------------------------------------------
  // 系统集成：媒体键 / 锁屏信息 / 防息屏
  // ---------------------------------------------------------------------

  useEffect(() => {
    const mediaSession = navigator.mediaSession;
    if (!mediaSession) return;
    // 片名与起播并行加载（§6.10），晚到的那几百毫秒先用占位——锁屏卡片
    // 会在下一次 effect 里拿到真名
    const shownTitle = title ?? "正在播放";
    mediaSession.metadata = new MediaMetadata({
      title: episodeLabel ? `${shownTitle} · ${episodeLabel}` : shownTitle,
      artist: episodeLabel ? shownTitle : undefined,
      artwork: posterUrl ? [{ src: posterUrl, sizes: "512x512" }] : undefined,
    });
    mediaSession.setActionHandler("play", () => togglePlay());
    mediaSession.setActionHandler("pause", () => togglePlay());
    mediaSession.setActionHandler("seekbackward", () => seekBy(-10));
    mediaSession.setActionHandler("seekforward", () => seekBy(10));
    mediaSession.setActionHandler("nexttrack", next ? () => onPlayNext() : null);
    mediaSession.setActionHandler("previoustrack", prev ? () => onPlayPrev() : null);
    // 锁屏/通知栏那条进度条**能拖**，靠的就是这个动作。不注册的话它要么
    // 不出现、要么拖了没反应——而 `seekbackward/forward` 顶不了它的位：
    // 那两个是「±10 秒」按钮，跳到某一点是另一回事（jellyfin-web 同样注册）。
    try {
      mediaSession.setActionHandler("seekto", (details) => {
        if (typeof details.seekTime !== "number") return;
        // 走 commitSeek：锁屏上拖一下与拖进度条是同一件事，
        // 在途的连按累积要先撤掉，否则 400ms 后它会把画面拽回去
        commitSeekRef.current(details.seekTime * 1000);
      });
    } catch {
      // 老浏览器不认这个动作，锁屏上就只剩按钮，不影响播放
    }
    /**
     * 「切走标签页就自动进画中画」。
     *
     * 网页**没法**自己实现这件事：`requestPictureInPicture()` 要求用户手势，
     * 而 `visibilitychange` 里根本没有手势。浏览器给的唯一入口就是这个媒体
     * 会话动作——由浏览器判断该不该自动进，然后回调我们，此时调用是合法的。
     * Chrome 目前主要对**已安装为应用**的站点触发（本项目有 manifest，装到
     * 桌面/主屏后即生效）。
     *
     * 手机上另有一条不需要代码的路：Android Chrome 与 iOS Safari 在视频处于
     * **全屏**时离开浏览器会自动进画中画，我们的「横屏」按钮正好就是全屏，
     * 而且这里从不在页面隐藏时暂停，所以那条路本来就是通的。
     *
     * 不认识这个动作名的浏览器（Safari）会直接抛，所以必须裹 try。
     */
    try {
      mediaSession.setActionHandler("enterpictureinpicture" as MediaSessionAction, () => {
        void video?.requestPictureInPicture?.().catch(() => undefined);
      });
    } catch {
      // 这个浏览器不认这个动作，自动画中画交给系统自己的行为
    }
    return () => {
      mediaSession.metadata = null;
      for (const action of [
        "play",
        "pause",
        "seekbackward",
        "seekforward",
        "nexttrack",
        "previoustrack",
      ] as const) {
        mediaSession.setActionHandler(action, null);
      }
      try {
        mediaSession.setActionHandler("seekto", null);
      } catch {
        // 同上，没注册上自然也不用清
      }
      try {
        mediaSession.setActionHandler("enterpictureinpicture" as MediaSessionAction, null);
      } catch {
        // 同上，没注册上自然也不用清
      }
    };
  }, [title, episodeLabel, posterUrl, togglePlay, seekBy, next, onPlayNext, prev, onPlayPrev, video]);

  /**
   * 原生 HLS 模式：选中的系统字幕轨**常开**（showing），所有表面（内联/
   * 原生全屏/画中画）都由系统渲染同一条轨。
   *
   * 走过的弯路必须记下来：曾试过「内联 hidden 走自绘、进 PiP 才切 showing」
   * 的混合制——切换依赖页面 JS，而 iOS 滑回桌面自动进 PiP 时 JS 冻结的
   * 时机不可控，字幕十次有九次跟不进小窗。恒开不赌时序。内联时 cue 落在
   * 元素底部（竖屏是黑边）的问题由 CSS 解决：见 globals.css 的
   * ::-webkit-media-text-track-container 抬升——那段 CSS 只影响内联，
   * 原生全屏与 PiP 是系统层，页面样式够不着，位置本来就对。
   *
   * 轨的顺序 = master 列表里 EXT-X-MEDIA 的顺序 = 服务端按 decision.subtitles
   * 过滤文本轨（vtt/ass）的顺序 = 前端 subtitles.options 的顺序——四者同一
   * 条过滤规则，下标可以直接对位（服务端注释里锚了这个约定）。
   */
  useEffect(() => {
    if (!systemSubtitles || !video) return;
    const apply = () => {
      // 只留 HLS 内生轨（system-track 模式没有自绘 <track>，防御性剔除保留）
      const domTracks = new Set(
        Array.from(video.querySelectorAll("track"), (el) => el.track),
      );
      const list = Array.from(video.textTracks).filter(
        (t) => !domTracks.has(t) && (t.kind === "subtitles" || t.kind === "captions"),
      );
      // master 字幕组只收文本轨（服务端 _MASTER_SUBTITLE_KINDS = vtt/ass），
      // 下标必须在**同一过滤规则**下对位——pgs 位图轨在 options 里但进不了
      // HLS 字幕组，直接用全量下标会错位到别人的轨上
      const textOptions = subtitles.options.filter((o) => o.kind !== "pgs");
      const target = activeSubtitle
        ? textOptions.findIndex((o) => o.ref === activeSubtitle.ref)
        : -1;
      const modes = planSystemTrackModes(list.length, target);
      list.forEach((track, i) => {
        track.mode = modes[i];
      });
    };
    apply();
    video.textTracks.addEventListener("addtrack", apply);
    return () => video.textTracks.removeEventListener("addtrack", apply);
  }, [systemSubtitles, video, activeSubtitle, subtitles]);

  /** 画中画进出时切换原生字幕轨的显隐（理由见 pipSubtitleUrl 注释）。 */
  useEffect(() => {
    if (!video || !mode?.pipPatchTrack) return; // system-track：系统轨常开，由上面的 effect 管
    const applyMode = () => {
      // iPhone Safari 没有标准 PiP API，进出画中画/原生全屏走 WebKit 前缀
      // 的 presentationMode，事件名也不同——两套都得认。原生全屏与 PiP
      // 同待遇：都只渲染 <video> 本身，字幕都得靠原生轨
      const webkitMode = (video as { webkitPresentationMode?: string }).webkitPresentationMode;
      // 页面不可见也算「原生表面」：iOS 从全屏滑回桌面自动转 PiP 时，mode
      // 事件序列是 fullscreen → inline → picture-in-picture，而页面 JS 在
      // 滑出去的瞬间就冻结了——inline 那步把轨切回 hidden 后，最后的 PiP
      // 事件根本跑不到，小窗里就没字幕。页面都看不见了不存在双字幕问题，
      // 不可见时一律 showing 最稳。
      const nativeSurface =
        document.pictureInPictureElement === video ||
        webkitMode === "picture-in-picture" ||
        webkitMode === "fullscreen" ||
        document.visibilityState === "hidden";
      for (const track of Array.from(video.textTracks)) {
        track.mode = nativeSurface ? "showing" : "hidden";
      }
    };
    applyMode();
    video.addEventListener("enterpictureinpicture", applyMode);
    video.addEventListener("leavepictureinpicture", applyMode);
    video.addEventListener("webkitpresentationmodechanged", applyMode);
    document.addEventListener("visibilitychange", applyMode);
    return () => {
      video.removeEventListener("enterpictureinpicture", applyMode);
      video.removeEventListener("leavepictureinpicture", applyMode);
      video.removeEventListener("webkitpresentationmodechanged", applyMode);
      document.removeEventListener("visibilitychange", applyMode);
    };
  }, [video, pipSubtitleUrl, mode?.pipPatchTrack]);

  /**
   * 拦掉屏幕边缘起手的历史滑动（返回/前进）手势——只在播放器存续期间。
   *
   * 能拦与拦不了要分清：**PWA（添加到主屏）里**，iOS 把历史滑动交给页面
   * 先裁决，边缘触摸的 touchstart 上 preventDefault 就能拦下——这是标准
   * 手段（也是各视频类 PWA 的通行做法）；**Safari 标签页里**该手势属于
   * 浏览器 chrome，网页无权禁用，只能靠进度条让位（.player-scrub-inset）
   * 把可拖元素挪出手势区。桌面触控板的双指历史滑动由下面的
   * overscroll-behavior 规则（globals.css）负责。
   *
   * 两个刻意的细节：
   * - 判定用**物理视口**坐标：伪横屏只是容器转了 90°，事件坐标仍是物理
   *   方向的，恰好与系统手势的起手边一致，不用换算；
   * - **只拦裸画面（video 元素本身）上的触摸**。按 target 白名单排除按钮
   *   的第一版仍会误伤：字幕/设置菜单、诊断面板都能滚动，在靠边的非按钮
   *   处起手拖动会被 preventDefault 吞掉滚动，表现为「列表滑不动」；
   *   preventDefault 还会吞掉合成 click，把「禁手势」连坐成「控件失灵」。
   *   历史滑动的典型起手场景本来就是裸画面，控件上放行的风险可以忽略。
   */
  useEffect(() => {
    const onTouchStart = (event: TouchEvent) => {
      const touch = event.touches[0];
      if (!touch) return;
      if (!(event.target instanceof HTMLVideoElement)) return;
      const nearEdge =
        touch.clientX <= EDGE_GUARD_PX || touch.clientX >= window.innerWidth - EDGE_GUARD_PX;
      if (nearEdge) event.preventDefault();
    };
    document.addEventListener("touchstart", onTouchStart, { passive: false });
    return () => document.removeEventListener("touchstart", onTouchStart);
  }, []);

  // ---------------------------------------------------------------------
  // 触屏滑动手势：竖滑调亮度/音量（左半屏亮度、右半屏音量），横滑拖进度
  // （分区、方向裁决、换算都在 touch-adjust.ts）
  // ---------------------------------------------------------------------

  /**
   * 进行中的滑动手势。
   *
   * `intent` 为 null = 只是「手指放上来了」，位移过门槛后一次性裁决方向，
   * 到松手为止不再改判——中途改判会让一次手势前半段调音量、后半段拖进度。
   */
  const swipeGestureRef = useRef<{
    /** 竖滑调哪一个量（由起手落在左半还是右半屏决定） */
    kind: AdjustKind;
    startX: number;
    startY: number;
    /** 竖滑起点的亮度/音量 */
    startValue: number;
    /** 横滑起点的播放位置（文件毫秒） */
    startPositionMs: number;
    intent: SwipeIntent | null;
    /** 横滑当前算出的落点；松手才提交，中途只喂胶囊 */
    targetMs: number;
  } | null>(null);
  /** 长按倍速的状态机进度 + 计时器 + 长按前的速率（迁移规则见 hold-speed.ts） */
  const holdRef = useRef<{ state: HoldSpeedState; timer: number | null; rate: number }>({
    state: "idle",
    timer: null,
    rate: 1,
  });
  /** 音量能不能调：null=还没探完；不可调 = iOS（WebKit 只认硬件侧键）。 */
  const volumeAdjustableRef = useRef<boolean | null>(null);
  const fakeLandscapeRef = useRef(false);
  fakeLandscapeRef.current = fakeLandscape;
  const brightnessRef = useRef(1);
  brightnessRef.current = brightness;

  /** 音量可调性探测。iOS 的坑分两代：老版本赋值被同步忽略（读回还是 1），
   * 新版本会**先把赋的值反映出来、稍后又异步弹回 1**——同步「赋值后读回」
   * 在新版上会把不可调误判成可调，表现就是真机上胶囊读数在变、实际响度
   * 纹丝不动、下一次滑动又从 100% 起步（2026-08-25 真机反馈）。所以必须
   * 赋一个真正不同的值、**过一拍再读回**才能下结论。探测用 ±6% 的扰动，
   * 250ms 后即还原，可调平台上听不出来。 */
  useEffect(() => {
    if (!video) return;
    const original = video.volume;
    const probe = original > 0.5 ? original - 0.06 : original + 0.06;
    video.volume = probe;
    const timer = window.setTimeout(() => {
      volumeAdjustableRef.current = Math.abs(video.volume - probe) < 0.01;
      video.volume = original;
    }, 250);
    return () => {
      window.clearTimeout(timer);
      video.volume = original;
    };
  }, [video]);

  useEffect(() => {
    if (!video) return;

    // ---- 长按倍速（docs/design/player-feel.md §2.B2）----
    const hold = holdRef.current;
    const setRate = (rate: number) => {
      // 变速必须保音高，不然 2× 是鸭子叫。Safari 到现在仍只认带前缀的那个。
      video.preservesPitch = true;
      (video as HTMLVideoElement & { webkitPreservesPitch?: boolean }).webkitPreservesPitch = true;
      video.playbackRate = rate;
    };
    const dispatchHold = (event: HoldSpeedEvent) => {
      const next = holdSpeedReducer(hold.state, event);
      if (next === hold.state) return;
      if (hold.timer !== null) {
        window.clearTimeout(hold.timer);
        hold.timer = null;
      }
      if (next === "pending") {
        hold.timer = window.setTimeout(() => dispatchHold("elapsed"), HOLD_SPEED_DELAY_MS);
      } else if (next === "active") {
        // 记住长按前的速率再改：用户可能自己调过（将来有倍速菜单时更要）
        hold.rate = video.playbackRate || 1;
        setRate(HOLD_SPEED_RATE);
        setHoldSpeed(true);
      } else if (hold.state === "active") {
        setRate(hold.rate);
        setHoldSpeed(false);
        // 松手后浏览器还会补一次 click，吞掉它，否则每次长按都顺带切控制层
        if (event === "release") suppressClickRef.current = true;
        if (event === "starve") flashNotice("缓冲跟不上，已退出 2 倍速");
      }
      hold.state = next;
    };
    // 倍速把前向缓冲吃得比转码器产出更快时，video 会发 waiting——那就是
    // 「追上编码器了」的信号，立刻还原，别让用户按着不放对着转圈
    const onWaiting = () => dispatchHold("starve");
    video.addEventListener("waiting", onWaiting);

    const onTouchStart = (event: TouchEvent) => {
      // 新的一次触摸开始 = 上一次要吞的 click 要么已经来过、要么不会来了
      // （长按松手后浏览器不一定补 click：Android 弹了长按菜单就没有）。
      // 不清的话那个标记会一直挂着，把下一次正经的轻点吞掉。
      suppressClickRef.current = false;
      // 锁屏：所有手势作废（轻点由 onSurfaceClick 处理成「露出解锁键」）
      if (lockedRef.current) {
        swipeGestureRef.current = null;
        return;
      }
      if (event.touches.length !== 1) {
        // 第二根手指落下 = 这次手势作废。作废之后 `finishGesture` 拿到的
        // gesture 是 null，**不会再走到 hideSeekPreview**，而落点胶囊没有
        // 自动退场计时——留着它就是画面正中钉死一个旧落点，进度条却跟着
        // 画面继续往前走，两个读数各说各话（2026-09-08 真机反馈）。
        if (swipeGestureRef.current?.intent === "horizontal") hideSeekPreview();
        swipeGestureRef.current = null;
        dispatchHold("release");
        return;
      }
      const touch = event.touches[0];
      // 排除带（顶部通知中心起手区、底部进度条、左右缘返回手势）对三种手势
      // 一视同仁：横滑同样不能在贴着进度条的地方起手，否则与拖进度条打架。
      const kind = classifyTouchZone(touch.clientX, touch.clientY, {
        width: window.innerWidth,
        height: window.innerHeight,
        fakeLandscape: fakeLandscapeRef.current,
      });
      if (!kind) return;
      swipeGestureRef.current = {
        kind,
        startX: touch.clientX,
        startY: touch.clientY,
        startValue: kind === "brightness" ? brightnessRef.current : video.volume,
        startPositionMs: positionRef.current,
        intent: null,
        targetMs: positionRef.current,
      };
      // 长按与滑动共用这一根手指：先起计时，位移过门槛（下面裁决方向那一步）
      // 就撤掉——「按住不动」与「按住划走」必须是两件事
      if (canHoldSpeed({ paused: video.paused, locked: lockedRef.current, touchCount: 1 })) {
        dispatchHold("press");
      }
    };
    const onTouchMove = (event: TouchEvent) => {
      const gesture = swipeGestureRef.current;
      if (!gesture || event.touches.length !== 1) return;
      const touch = event.touches[0];
      const viewport = {
        width: window.innerWidth,
        height: window.innerHeight,
        fakeLandscape: fakeLandscapeRef.current,
      };
      // 一律换算到**布局**坐标：伪横屏是容器转了 90°，用户觉得自己在横划，
      // 物理上是竖着划的——直接用事件坐标会把两种手势对调。
      const start = toLayoutPoint(gesture.startX, gesture.startY, viewport);
      const now = toLayoutPoint(touch.clientX, touch.clientY, viewport);
      const deltaX = now.x - start.x;
      const deltaY = now.y - start.y;
      if (!gesture.intent) {
        const intent = classifyIntent(deltaX, deltaY);
        if (!intent) return;
        // 手指开始滑了：这一定不是长按
        dispatchHold("move");
        if (intent === "horizontal" && !durationMsRef.current) {
          // 片长未知就没有落点可算（那时进度条本身也是禁用的）。整根手指
          // 作废而不是回落到调音量：用户明明在横划，半路改判成调音量比
          // 「什么都没发生」更费解。
          swipeGestureRef.current = null;
          return;
        }
        gesture.intent = intent;
      }
      // 生效后接管这根手指：不让它同时滚动页面/触发下拉刷新
      event.preventDefault();
      if (gesture.intent === "horizontal") {
        // 滑动中**只更新胶囊、不 seek**：每次 move 都跳会让服务端一路杀
        // ffmpeg 重启（seek 走的就是那条路），画面永远追不上手指。与进度条
        // 拖拽同一套语义，松手才提交一次。
        const target = clampSeekTarget(
          gesture.startPositionMs + seekDeltaMs(deltaX, now.width),
          durationMsRef.current,
        );
        gesture.targetMs = target;
        showSeekPreview(target, target - gesture.startPositionMs);
        return;
      }
      const value = applySwipe(gesture.kind, gesture.startValue, deltaY, now.height);
      if (gesture.kind === "brightness") {
        setBrightness(value);
        flashAdjust({ kind: "brightness", value });
        return;
      }
      video.volume = value;
      // 可调性由挂载时的延时探测裁决（见 volumeAdjustableRef 旁的探测
      // effect）；探测未出结论前（挂载后 250ms 内就有手势的极端情况）
      // 先乐观显示，touchend 的复核兜底
      if (volumeAdjustableRef.current !== false) {
        // 只有**往上滑**才视同「要声音」解除静音；向下调音量时静音保持——
        // 用户按过静音键后想把音量预调小一点，不该突然出声
        if (value > gesture.startValue && video.muted) video.muted = false;
        flashAdjust({ kind: "volume", value, muted: video.muted });
      } else {
        flashAdjust({ kind: "volume", value: video.volume, unsupported: true });
      }
    };
    /**
     * 手势收尾。`commit` 区分抬手与被打断：
     *
     * - 抬手（touchend）= 用户认了这个落点，横滑在这里提交唯一那次 seek；
     * - 打断（touchcancel，系统手势/来电/第二根手指）= **不提交**，与进度条
     *   拖拽被取消时同一条规矩——用户没抬手确认过这个位置。
     *
     * 亮度/音量没有「提交」一说（滑动中已经实时生效），两条路都只是收尾。
     * 音量手势收尾时复核一次「赋的值是否真的留住了」——挂载探测万一误判
     * 可调（WebKit 行为随版本漂），这里能把结论纠回来，下一次滑动就会如实
     * 提示由侧键控制。
     */
    const finishGesture = (commit: boolean) => {
      dispatchHold("release");
      const gesture = swipeGestureRef.current;
      swipeGestureRef.current = null;
      if (gesture?.intent === "horizontal") {
        if (commit) commitSeekRef.current(gesture.targetMs);
        hideSeekPreview();
        return;
      }
      if (gesture?.intent !== "vertical" || gesture.kind !== "volume") return;
      if (volumeAdjustableRef.current === false) return;
      const expected = video.volume;
      window.setTimeout(() => {
        if (Math.abs(video.volume - expected) > 0.05) {
          volumeAdjustableRef.current = false;
        }
      }, 300);
    };
    const onTouchEnd = () => finishGesture(true);
    const onTouchCancel = () => finishGesture(false);
    video.addEventListener("touchstart", onTouchStart, { passive: true });
    video.addEventListener("touchmove", onTouchMove, { passive: false });
    video.addEventListener("touchend", onTouchEnd);
    video.addEventListener("touchcancel", onTouchCancel);
    return () => {
      video.removeEventListener("touchstart", onTouchStart);
      video.removeEventListener("touchmove", onTouchMove);
      video.removeEventListener("touchend", onTouchEnd);
      video.removeEventListener("touchcancel", onTouchCancel);
      video.removeEventListener("waiting", onWaiting);
      // 换 video 元素/卸载时把倍速还原，否则新流会带着 2× 起播
      dispatchHold("release");
      // 换 video 元素（或组件卸载）时手势就此断掉，touchend 再也不会来：
      // 与上面第二根手指同一条理由，胶囊必须在这里一起收走，否则它会一直
      // 挂着一个旧落点。手势本身也作废——不提交没抬手确认过的落点。
      if (swipeGestureRef.current?.intent === "horizontal") hideSeekPreview();
      swipeGestureRef.current = null;
    };
  }, [video, flashAdjust, flashNotice, showSeekPreview, hideSeekPreview]);

  /**
   * 锁屏/通知栏卡片上的位置与时长。
   *
   * 不喂这个，系统那条进度条要么空着、要么停在 0——注册了 `seekto` 却没有
   * 位置可拖，比不注册更奇怪。跟 `positionMs` 走（约 4Hz）就够：那是给人
   * 眼看的读数，不需要每帧。
   *
   * 两条守卫都是规范要求，违反会抛：`position` 不能超过 `duration`（换会话
   * 的空档里位置可能短暂越界），`playbackRate` 不能为 0（长按倍速结束的
   * 那一瞬间读到 0 会把整块卡片清掉）。
   */
  useEffect(() => {
    const mediaSession = navigator.mediaSession;
    if (!mediaSession?.setPositionState || !durationMs || !video) return;
    try {
      mediaSession.setPositionState({
        duration: durationMs / 1000,
        position: Math.min(positionMs, durationMs) / 1000,
        playbackRate: video.playbackRate || 1,
      });
    } catch {
      // 读数暂时自相矛盾（换会话空档）时跳过这一次，下一拍就正常
    }
  }, [positionMs, durationMs, video]);

  /**
   * 播放中申请防息屏。
   *
   * **必须跟着可见性重新申请**：规范规定文档一转入后台，系统就把锁收走，而
   * 且此时 `request()` 会直接拒绝。只在 effect 里申请一次的话，用户切出去接
   * 个消息再回来，锁就永远没有了——电影放到一半屏幕自己暗下去，而这正是这
   * 段代码要防的事（这条注释以前就写着「回来时重新申请」，但没人真的写）。
   */
  useEffect(() => {
    if (paused || state.phase !== "playing") return;
    let sentinel: WakeLockSentinel | null = null;
    let released = false;
    const acquire = () => {
      // 后台申请必被拒；有锁在手也不重复申请
      if (released || sentinel || document.visibilityState !== "visible") return;
      void navigator.wakeLock
        ?.request("screen")
        .then((lock) => {
          if (released) {
            void lock.release().catch(() => undefined);
            return;
          }
          sentinel = lock;
          // 系统收走时把引用一起丢掉，否则回到前台会以为还锁着、不再申请
          lock.addEventListener?.("release", () => {
            if (sentinel === lock) sentinel = null;
          });
        })
        .catch(() => undefined);
    };
    acquire();
    document.addEventListener("visibilitychange", acquire);
    return () => {
      released = true;
      document.removeEventListener("visibilitychange", acquire);
      void sentinel?.release().catch(() => undefined);
    };
  }, [paused, state.phase]);

  // ---------------------------------------------------------------------
  // 控制条自动隐藏 + 片尾下一集
  // ---------------------------------------------------------------------

  const awaitingUser = awaitsUserDecision(state.phase);

  useEffect(() => {
    // 锁屏优先级最高：锁上之后暂停、开菜单都不该把一排能点的按钮放回来
    if (locked) return;
    if (chromeMustStayVisible({ paused, menuOpen, awaitingUser })) {
      setChromeVisible(true);
      return;
    }
    const timer = window.setTimeout(() => setChromeVisible(false), IDLE_HIDE_MS);
    return () => window.clearTimeout(timer);
    // chromeActivity：用户的每次操作都重排这个倒计时（声明见 state 注释）
  }, [locked, paused, menuOpen, awaitingUser, chromeVisible, chromeActivity]);

  /**
   * 片尾「下一集」卡片该不该显示。
   *
   * 纯派生、**不带任何计时器**：卡片进了片尾窗口就一直挂着，直到用户点它或
   * 点关闭。曾经这里是一个 10 秒倒计时，数到 0 自动换集——片尾还没看完画面
   * 就被抢走，而用户以为「按钮自己消失了」。要连播由用户自己点。
   *
   * 往回拖出片尾窗口会收起来（那时它已经不是「即将播放」了），再放到片尾还
   * 会回来；只有明确关掉的那次才对本集永久生效。
   */
  const showNextCard =
    next !== null &&
    !nextDismissed &&
    (isInEndCredits(positionMs, durationMs) || state.phase === "ended");

  // 自动播放被彻底拦下时不能再转圈：状态机要等 `playing` 才离开 buffering，
  // 而那一刻永远不会来——转圈叠着中央播放键是最典型的「界面卡住了」观感。
  const busy = isBusy(state.phase) && autoplay !== "blocked";
  // 暂停海报：出过画之后才显示，否则起播那几秒会先闪一张「已暂停」。
  //
  // 必须跟「用户意图」而不是 video 元素的原生 paused：iOS 在缓冲饥饿、seek
  // 后追流时会自己发 pause（间隙里 busy 还没立起来），直通片在远程挂载上
  // seek 后的短暂反复饥饿能让这层黑罩以饥饿周期反复淡入扯出——真机表现是
  // 快进后黑屏连闪（2026-08-26 反馈；200ms 入场延迟只能吃掉更短的抖动）。
  // wantsPlayRef 只在用户亲手按暂停时为 false（程序性暂停都刻意置 true），
  // 拿它把「饥饿暂停」整类排除。代价是系统侧发起的暂停（拔耳机等，不经
  // togglePlay）不压暗，只有静止画面——可接受。渲染期读 ref 是安全的：
  // 每次 pause/playing 都伴随 setPaused，取值时刻恒有新鲜渲染。
  const showPauseOverlay =
    paused &&
    !busy &&
    !wantsPlayRef.current &&
    state.phase !== "error" &&
    state.phase !== "consent" &&
    positionMs > 0;

  /**
   * 进度条与时间读数的**覆盖位置**：有它就显示它，没有才显示真实播放位置。
   *
   * 两个来源，都是「用户已经表达了意图、但画面还没跳过去」的中间态：
   *
   * - **横滑拖进度**：手势期间画面照常播，进度条要是继续跟画面，屏幕上就
   *   同时挂着两个各说各话的读数——正中的胶囊报 9:58、底下的进度条停在
   *   19:00，用户根本不知道松手会落到哪儿（2026-09-08 真机反馈）。淡出阶段
   *   （leaving）交还给真实位置：提交那条路 seek 已经把位置带到落点、读数
   *   不会跳，取消那条路本来就该弹回真值。
   * - **连按快进的累积落点**：合并窗口内画面还没动，读数必须先走到累积落点，
   *   否则连按三下屏幕上什么都不变，用户只会以为按键没生效。
   */
  const overrideMs =
    seekPreview && !seekPreview.leaving ? seekPreview.targetMs : pendingSeekMs;

  return (
    <div
      ref={containerRef}
      className={`player-root relative size-full bg-black ${
        fakeLandscape ? "player-fake-landscape" : ""
      }`}
      style={
        {
          "--cue-lift": `${cueLiftPx}px`,
          "--cue-font-size": cueFontPx ? `${cueFontPx}px` : undefined,
        } as React.CSSProperties
      }
      data-chrome={chromeVisible ? "visible" : "hidden"}
      // 悬停唤出只属于鼠标：触摸的 pointermove（滑动调节、拖进度）不该把
      // 控制层惊出来——亮度/音量手势恰恰设计成在控制层藏着时用，滑一下
      // 弹一屏按钮就是视觉噪音。触屏的唤出走「轻点 → click → onSurfaceClick」。
      onPointerMove={(event) => {
        if (event.pointerType === "mouse") {
          setChromeVisible(true);
          bumpChromeActivity();
        }
      }}
      // 只记录按下瞬间的显隐态，不在这里唤出：唤出等 click（轻点才有 click，
      // 滑动没有），手势滑动全程控制层保持原样。顺带记指针类型——双击全屏
      // 只属于鼠标（dblclick 事件本身不带 pointerType）。
      // 控制层已露出时，任何按下（含按钮点按）都重排自动隐藏的倒计时；
      // 藏着时不动它——那会让滑动手势把控制层惊出来
      onPointerDown={(event) => {
        chromeWasVisibleRef.current = chromeVisible;
        lastPointerTypeRef.current = event.pointerType;
        if (chromeVisible) bumpChromeActivity();
      }}
      // 只认鼠标：触屏上每点一下都会触发 pointerleave（pointerdown → pointerup
      // → pointerleave → click），当成「用户不看了」会让控制条闪一下就没
      onPointerLeave={(event) => {
        if (shouldHideOnPointerLeave({ pointerType: event.pointerType, paused })) {
          setChromeVisible(false);
        }
      }}
    >
      <MediaController
        noHotkeys
        // media-chrome 自带的手势层会在点击媒体时切换播放/暂停——与我们的
        // 「画面点击只开关控制层」直接冲突（它就是"点哪都会暂停"的元凶之一，
        // 浏览器 E2E 抓出来的）。快捷键已经 noHotkeys 自管，手势也一样自管。
        // autohide="-1" 关掉它的第二套显隐时钟：负值时 media-chrome 永远不会
        // 进 userinactive，也就不会按它自己的倒计时把菜单浮层、控制条统一
        // 淡出——显隐完全由我们的 chromeVisible 单一所有（静止 4 秒收、菜单
        // 打开时 chromeMustStayVisible 钉住）。不禁的话菜单开着等几秒会被它
        // 淡掉，绕过我们的 menuOpen 钉住逻辑（真机反馈的根因）。
        {...{ gesturesdisabled: "", autohide: "-1" }}
        className="size-full"
        style={{ width: "100%", height: "100%", backgroundColor: "#000" }}
      >
        <video
          ref={setVideo}
          slot="media"
          playsInline
          poster={posterUrl ?? undefined}
          onClick={onSurfaceClick}
          onDoubleClick={onSurfaceDoubleClick}
          className="size-full object-contain"
        >
          {pipSubtitleUrl ? (
            // key 让换轨时整个 <track> 重建——改 src 复用元素时 Safari 不重载 cue。
            // 不带 default：iOS 开着系统字幕偏好时 Safari 会自动点亮 default
            // 轨，和自绘层叠成双字幕；显隐完全由 PiP 切换逻辑控制。
            <track key={pipSubtitleUrl} kind="subtitles" src={pipSubtitleUrl} />
          ) : null}
        </video>

        {/* 冻结帧：换流 / 跳出缓冲时盖住那段没有画面的空窗（freeze-frame.ts）。
            尺寸与 object-contain 跟 <video> 一模一样，盖上去时画面纹丝不动，
            看起来就是「画面停住了」而不是「换了一张图」。

            常驻不卸载：抓帧要在盖上去**之前**完成，ref 必须一直在手上；
            noautohide 是因为 media-controller 会把无操作时的普通子元素统一
            淡出——盖画面的这层被淡掉就等于黑屏照旧。 */}
        <canvas
          ref={freezeCanvasRef}
          {...{ noautohide: "" }}
          aria-hidden
          className={`pointer-events-none absolute inset-0 size-full object-contain ${
            frozen ? "" : "hidden"
          }`}
        />

        {/* 亮度遮罩。不用 video 上的 CSS filter：iOS 的视频走独立合成层，
            filter 在真机上时常被绕过（滤镜不生效，2026-08-25 真机反馈）；
            黑色遮罩按不透明度压暗是全平台都吃的等效实现（只往暗调，数学上
            与 brightness(x) 同为乘法压暗）。放在 video 之后、字幕与控制层
            之前——用户调的是「画面」亮度，字幕和按钮不该跟着暗。 */}
        {brightness < 1 ? (
          <div
            {...{ noautohide: "" }}
            className="player-brightness-mask pointer-events-none absolute inset-0 bg-black"
            style={{ opacity: 1 - brightness }}
          />
        ) : null}

        <SubtitleLayer
          video={video}
          // 烧录中的轨绝不旁挂——画面里已经有了，再挂一层是双字幕。
          // system-track（iOS 原生 HLS）时文本轨交系统渲染、自绘层闲置；
          // 但 PGS 位图轨进不了 HLS 字幕组，内联播放仍由 canvas 层负责
          // （原生全屏/画中画只渲染视频帧，位图字幕跟不进去——ASS 特效
          // 在那两个面上同样只剩降级文本，属于同一类既有限制）
          track={
            activeSubtitle?.ref === burnedSubtitle
              ? null
              : systemSubtitles && activeSubtitle?.kind !== "pgs"
                ? null
                : activeSubtitle
          }
          style={subtitleStyle}
          baseOffsetSeconds={(mode?.originMs ?? 0) / 1000}
          // 控制条三行（时间行 + 进度条 + 操作行）展开时的实占高度，字幕
          // 压到会被盖住；收起时只剩贴边细进度条，不用让
          avoidBottomPx={chromeVisible ? 172 : 8}
        />

        {/* 中央播放簇：退十秒 / 播放暂停 / 进十秒。与控制条同步淡入淡出。
            起播转圈时不出——那时候按什么都没用，两个东西叠在画面正中最乱。 */}
        <PlayerCenterControls
          paused={paused}
          visible={chromeVisible && !busy && state.phase !== "error" && state.phase !== "consent"}
          onTogglePlay={togglePlay}
          onSeekBy={seekBy}
        />

        {/* 顶栏：返回 + 片名。全屏时也要留着，否则退不出去只能按 Esc */}
        <div
          // pt 叠加 --safe-top：PWA（black-translucent + viewport-fit=cover）里
          // 页面画到状态栏底下，不让开的话返回键会顶进系统时钟里
          className={`player-inset-x pointer-events-none absolute inset-x-0 top-0 z-20 flex items-center gap-4 bg-gradient-to-b from-black/80 via-black/30 to-transparent pb-16 pt-[calc(1.25rem_+_var(--safe-top))] transition-opacity duration-300 max-md:pt-[calc(0.75rem_+_var(--safe-top))] ${
            chromeVisible ? "opacity-100" : "opacity-0"
          }`}
        >
          {/* 返回键单独浮在画面上，和其它页面顶栏的返回键是同一个东西，
              所以样式也照抄那边（page-nav.tsx 的 PAGE_NAV_BUTTON_CLASS）：
              36/44px 圆形玻璃底 + 发丝描边 + 站内同一颗 ChevronLeft。
              不 import 那个常量是因为 page-nav 会把搜索命令面板等一整串东西
              拖进播放路由，而这条路由的初始 JS 是刻意压着的。 */}
          <button
            type="button"
            onClick={goBack}
            // 淡出后必须同时断掉命中：隐形却仍能点的按钮会在用户想点画面时误触
            className={`grid size-9 shrink-0 place-items-center rounded-full border border-white/[0.09] bg-black/30 text-white/85 backdrop-blur-md transition hover:bg-black/50 hover:text-white active:scale-[0.94] max-md:size-11 ${
              chromeVisible ? "pointer-events-auto" : "pointer-events-none"
            }`}
            aria-label={landscape ? "退出横屏" : "退出播放"}
          >
            <ChevronLeftIcon className="size-[18px] max-md:size-[22px]" />
          </button>
          <div className="min-w-0">
            <h1 className="truncate text-[17px] font-semibold text-white drop-shadow">{title}</h1>
            {episodeLabel ? (
              <p className="truncate text-[13px] text-white/65">{episodeLabel}</p>
            ) : null}
          </div>

          {/* 画中画放顶栏右上角，不回控制条：它不是「控制这次播放」的动作，
              而是「把这次播放带走」——和左上角的退出键成对，一个离开播放、
              一个带着继续。控制条右簇留给字幕/设置/全屏那批真正的播放控制。

              浏览器不支持由网页发起时整颗不渲染（Firefox 的画中画只在它自己
              的界面里，留着就是个死按钮）。 */}
          {/* 锁屏只在触屏的横屏里出现：横躺着看片时手掌压在屏幕上是常态，
              而竖屏握持时误触少得多，多一颗按钮反而是噪音。 */}
          {canRotate && (landscape || fakeLandscape || deviceLandscape) ? (
            <button
              type="button"
              onClick={() => {
                setLocked(true);
                revealLock();
              }}
              className={`ml-auto grid size-9 shrink-0 place-items-center rounded-full border border-white/[0.09] bg-black/30 text-white/85 backdrop-blur-md transition hover:bg-black/50 hover:text-white active:scale-[0.94] max-md:size-11 ${
                chromeVisible ? "pointer-events-auto" : "pointer-events-none"
              }`}
              aria-label="锁屏"
              title="锁屏（防误触）"
            >
              <LockIcon className="size-[18px] max-md:size-[22px]" />
            </button>
          ) : null}
          {canPip ? (
            <button
              type="button"
              onClick={togglePip}
              className={`grid size-9 shrink-0 place-items-center rounded-full border border-white/[0.09] bg-black/30 text-white/85 backdrop-blur-md transition hover:bg-black/50 hover:text-white active:scale-[0.94] max-md:size-11 ${
                canRotate && (landscape || fakeLandscape || deviceLandscape) ? "" : "ml-auto"
              } ${chromeVisible ? "pointer-events-auto" : "pointer-events-none"}`}
              aria-label={pipActive ? "退出画中画" : "画中画"}
              title={pipActive ? "退出画中画" : "画中画"}
            >
              <PipGlyph exit={pipActive} />
            </button>
          ) : null}
        </div>

        {/* 锁屏中的解锁键：画面左侧居中（拇指够得到，又不压在中央播放键上）。
            点画面唤出、3 秒后自己收——常显等于把误触目标从整块屏幕缩成一个点，
            而锁屏要的是「碰哪儿都不响应」。noautohide：它不属于控制层。 */}
        {locked ? (
          <div
            {...{ noautohide: "" }}
            className={`absolute left-[6%] top-1/2 z-30 -translate-y-1/2 transition-opacity duration-300 ${
              lockHint ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
            }`}
          >
            <button
              type="button"
              onClick={() => {
                setLocked(false);
                setChromeVisible(true);
              }}
              className="grid size-11 place-items-center rounded-full border border-white/[0.09] bg-black/45 text-white backdrop-blur-md transition active:scale-[0.94]"
              aria-label="解锁"
              title="解锁"
            >
              <LockIcon className="size-[22px]" />
            </button>
          </div>
        ) : null}

        {/* 长按倍速的 HUD：顶部居中，与调节胶囊同一套视觉。生效期间常驻
            （手指还按着就还在倍速），松手即消失，所以不做两段式退场。 */}
        {holdSpeed ? (
          <div
            {...{ noautohide: "" }}
            className="pointer-events-none absolute left-1/2 top-[calc(1rem_+_var(--safe-top))] z-30 -translate-x-1/2"
          >
            <div className="player-flash-in flex items-center gap-2 rounded-full bg-black/70 px-4 py-2 text-[13px] font-medium text-white shadow-[0_10px_28px_rgba(0,0,0,0.45)]">
              <span className="tnum">{HOLD_SPEED_RATE}× 快进中</span>
              <SeekChevrons back={false} />
            </div>
          </div>
        ) : null}

        {/* 调节反馈胶囊：顶部居中，触摸滑动与键盘 ↑↓/M 共用；调节中常驻、
            停手 0.9 秒后淡出（两段式，动画类见 globals 的 .player-flash-*）。
            noautohide：不随控制层显隐——手势恰恰常在控制层藏着时用。 */}
        {adjust ? (
          <div
            {...{ noautohide: "" }}
            className="pointer-events-none absolute left-1/2 top-[calc(1rem_+_var(--safe-top))] z-30 -translate-x-1/2"
          >
            <div
              className={`flex items-center gap-2.5 rounded-full bg-black/70 px-4 py-2 text-[13px] font-medium text-white shadow-[0_10px_28px_rgba(0,0,0,0.45)] ${
                adjust.leaving ? "player-flash-out" : "player-flash-in"
              }`}
            >
              {adjust.kind === "brightness" ? (
                <BrightnessGlyph />
              ) : (
                <VolumeGlyph muted={Boolean(adjust.muted) || adjust.value <= 0.001} />
              )}
              {adjust.unsupported ? (
                <span>音量由系统侧键控制</span>
              ) : adjust.muted ? (
                <span>静音</span>
              ) : (
                <>
                  <div className="h-1 w-24 overflow-hidden rounded-full bg-white/25">
                    <div
                      className="h-full rounded-full bg-white"
                      style={{ width: `${Math.round(adjust.value * 100)}%` }}
                    />
                  </div>
                  <span className="tnum w-9 text-right">{Math.round(adjust.value * 100)}%</span>
                </>
              )}
            </div>
          </div>
        ) : null}

        {/* 横滑拖进度的落点读数。放**画面正中**：手指在画面上滑，读数就跟在
            眼睛看的地方，不必再去够底边那条进度条（各家手机播放器同此）。
            大字是落点、小字是相对起点的增量——只给落点的话，用户不知道自己
            这一划到底走了多远，也就没法凭手感修正。 */}
        {seekPreview ? (
          <div
            {...{ noautohide: "" }}
            className="pointer-events-none absolute left-1/2 top-1/2 z-30 -translate-x-1/2 -translate-y-1/2"
          >
            <div
              className={`flex flex-col items-center gap-1.5 rounded-2xl bg-black/70 px-6 py-3.5 text-white shadow-[0_10px_28px_rgba(0,0,0,0.45)] ${
                seekPreview.leaving ? "player-flash-out" : "player-flash-in"
              }`}
            >
              <p className="tnum text-[24px] font-semibold leading-none max-md:text-[22px]">
                {formatClock(seekPreview.targetMs)}
                <span className="text-[15px] font-normal text-white/45">
                  {" / "}
                  {durationMs ? formatClock(durationMs) : "--:--"}
                </span>
              </p>
              <p className="tnum text-[13px] leading-none text-white/70">
                {seekPreview.deltaMs < 0 ? "−" : "+"}
                {formatClock(Math.abs(seekPreview.deltaMs))}
              </p>
            </div>
          </div>
        ) : null}

        {/* 一次性文字提示：与调节胶囊同视觉，压低一档（两者可共存不叠）。
            承接本来会静默失败的操作（画中画被系统拒绝等）。 */}
        {notice ? (
          <div
            {...{ noautohide: "" }}
            className="pointer-events-none absolute left-1/2 top-[calc(3.75rem_+_var(--safe-top))] z-30 -translate-x-1/2"
          >
            <div
              className={`max-w-[85vw] rounded-full bg-black/70 px-4 py-2 text-center text-[13px] font-medium text-white shadow-[0_10px_28px_rgba(0,0,0,0.45)] ${
                notice.leaving ? "player-flash-out" : "player-flash-in"
              }`}
            >
              {notice.text}
            </div>
          </div>
        ) : null}

        {/* 键盘快进/快退的方向提示：快退在画面左侧、快进在右侧，连按累计。
            与暂停压暗层同 z：它只是读数，不该压过报错/同意这些要拍板的层。 */}
        {seekFlash ? (
          <div
            {...{ noautohide: "" }}
            className={`pointer-events-none absolute top-1/2 z-10 -translate-y-1/2 ${
              seekFlash.seconds < 0 ? "left-[10%]" : "right-[10%]"
            }`}
          >
            <div
              className={`flex items-center gap-1.5 rounded-full bg-black/65 px-3.5 py-2 text-[13px] font-medium text-white shadow-[0_10px_28px_rgba(0,0,0,0.45)] ${
                seekFlash.leaving ? "player-flash-out" : "player-flash-in"
              }`}
            >
              {seekFlash.seconds < 0 ? <SeekChevrons back /> : null}
              <span className="tnum">{Math.abs(seekFlash.seconds)} 秒</span>
              {seekFlash.seconds > 0 ? <SeekChevrons back={false} /> : null}
            </div>
          </div>
        ) : null}

        {/* 暂停压暗层：整屏 45% 黑，一眼知道「现在是停着的」。片名大字不在
            这里——它长在下面控制条那个布局流里（见 bottom 容器），跟时间行
            排队而不是各自绝对定位再拿 padding 去猜对方的高度。

            常驻 + 透明度切换，绝不 mount/unmount：挂载瞬间 transition 不生效，
            直接 mount 就是「啪」一下整屏变黑。而 paused 跟的是 video 的原生
            pause/playing 事件——iOS 的 AVPlayer 在缓冲、起播、seek 重启时会
            连发 pause→waiting→playing，间隙帧里这层会反复挂上又摘掉，真机上
            表现为黑屏快速闪烁几次（2026-08-25 反馈）。入场延迟 200ms：短于
            这个窗口的状态抖动完全不可见；真暂停则柔和淡入，松手退场不带延迟。 */}
        <div
          className={`pointer-events-none absolute inset-0 z-10 bg-black/45 transition-opacity ${
            showPauseOverlay ? "opacity-100 duration-300 delay-200" : "opacity-0 duration-200 delay-0"
          }`}
        />

        {busy ? (
          // noautohide：media-controller 会把无操作时的普通子元素统一淡出，
          // 状态提示（转圈/报错/同意）必须豁免——黑屏配一个隐形的错误是最差体验
          <div
            {...{ noautohide: "" }}
            className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center"
          >
            {/* player-busy-appear：450ms 后才现身。一次 200ms 的短缓冲不该
                闪一下转圈——「转圈晃过」比「短暂无提示」更让人觉得卡
                （Netflix 同款延迟）。busy 恢复时整块卸载，即时消失。 */}
            <div className="player-busy-appear flex flex-col items-center gap-4">
              <span className="size-12 animate-spin rounded-full border-[3px] border-white/15 border-t-[var(--player-accent)]" />
              <div className="flex flex-col items-center gap-1.5">
                <span className="text-[14px] text-white/75">{busyLabel(state.phase)}</span>
                {/* 转圈的时候正是用户最想知道「到底是谁慢」的时候：这里补一行
                    实测取流速度，贴着码率跑说明线路没问题、卡的是服务端转码，
                    远低于码率说明就是带宽不够，该去降一档画质。没有读数（样本
                    还不够）时整行不出现，不占位也不显示占位符。 */}
                {speedLabel ? (
                  <span className="tnum text-[12px] text-white/45">↓ {speedLabel}</span>
                ) : null}
              </div>
            </div>
          </div>
        ) : null}


        {state.phase === "error" && state.error ? (
          <div
            {...{ noautohide: "" }}
            className="absolute inset-0 z-30 flex items-center justify-center bg-black/85 px-6"
          >
            <div className="max-w-[480px] text-center">
              <p className="text-[17px] font-semibold text-white">{state.error.message}</p>
              {state.error.suggestion ? (
                <p className="mt-3 text-[14px] leading-relaxed text-white/60">
                  {state.error.suggestion}
                </p>
              ) : null}
              <div className="mt-6 flex justify-center gap-3">
                <button
                  type="button"
                  onClick={() => dispatch({ type: "request", startMs: positionRef.current })}
                  className="rounded-full bg-[var(--player-accent)] px-6 py-2.5 text-[14px] font-semibold text-black transition-colors hover:bg-[var(--player-accent-hover)]"
                >
                  重试
                </button>
                <button
                  type="button"
                  onClick={onExit}
                  className="rounded-full bg-white/15 px-6 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-white/25"
                >
                  返回
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {state.phase === "consent" && state.decision ? (
          <ConsentDialog
            decision={state.decision}
            onGranted={() => {
              // 置位在重新决策之前：新决策若仍是 consent 就走错误页而不是
              // 把弹窗原样闪回（见 consentGrantedRef 的注释）
              consentGrantedRef.current = true;
              dispatch({ type: "consent-granted" });
            }}
            onCancel={onExit}
          />
        ) : null}

        {diagnosticsOpen && state.session ? (
          <DiagnosticsPanel
            session={state.session}
            engine={engineRef.current}
            qoe={() => liveStats(qoeRef.current)}
            landscape={landscape}
            diagnostics={serverDiagnostics}
            onClose={() => setDiagnosticsOpen(false)}
          />
        ) : null}

        {/* 下一集卡片：片尾窗口内常驻，换集完全由用户决定 */}
        {showNextCard && next ? (
          <div
            // 定位内联：.menu-surface 自带 position:relative（不在 @layer，
            // className 的 absolute 压不过它）
            style={{ position: "absolute" }}
            className="menu-surface bottom-32 right-6 z-30 w-[300px] p-4 max-md:bottom-28 max-md:right-3 max-md:w-[240px]"
          >
            <p className="text-[12px] uppercase tracking-wide text-white/50">即将播放</p>
            <p className="mt-1.5 truncate text-[15px] font-semibold text-white">{next.label}</p>
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => setNextDismissed(true)}
                className="rounded-full bg-white/15 px-4 py-2 text-[13px] text-white/85 transition-colors hover:bg-white/25"
              >
                关闭
              </button>
              <button
                type="button"
                onClick={onPlayNext}
                className="flex-1 rounded-full bg-white px-4 py-2 text-[13px] font-semibold text-black transition-colors hover:bg-white/85"
              >
                立即播放
              </button>
            </div>
          </div>
        ) : null}

        {/* pointer-events-none 必须有：这层透明容器的高度由内容撑（时间行的
            pt-24 也算），横屏时上沿会探进中央簇的区域——普通 div 即使全透明
            也拦命中，退十秒会看得见按不动。可点元素在 PlayerControls 里各自
            开回 auto。 */}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20">
          {/* 暂停片名：Netflix 式大字，靠左下、落在控制条正上方（Disney+ /
              Apple TV+ 同款位置）：垂直中央是播放簇的地盘，左上顶栏已有片名。
              放进控制条的布局流而不是绝对定位 + 猜高度的 padding——控制条在
              手机上比桌面高一截，猜出来的数字压上时间药丸（真机截图证实），
              而且以后控制条每改一次布局都要重猜。排队永远不重叠。
              矮屏（手机横屏）底部这点空间会顶到中央簇，直接藏大字。 */}
          {showPauseOverlay && title ? (
            <div className="player-inset-x min-w-0 [@media(max-height:480px)]:hidden">
              <p className="text-[12px] uppercase tracking-[0.3em] text-white/55">已暂停</p>
              <h2 className="mt-2 truncate text-[32px] font-bold leading-tight text-white drop-shadow-lg max-md:text-[22px]">
                {title}
              </h2>
              {episodeLabel ? (
                <p className="mt-1 truncate text-[16px] text-white/70 max-md:text-[13px]">
                  {episodeLabel}
                </p>
              ) : null}
            </div>
          ) : null}
          <PlayerControls
            positionMs={positionMs}
            video={video}
            startMs={mode?.originMs ?? 0}
            overrideMs={overrideMs}
            durationMs={durationMs}
            bufferedEndMs={bufferedEndMs}
            networkSpeed={speedLabel}
            chromeVisible={chromeVisible}
            onSeek={commitSeek}
            onScrub={scrubTo}
            subtitles={subtitles}
            selectedSubtitle={selectedSubtitle}
            onSelectSubtitle={selectSubtitle}
            audioOptions={audioOptions}
            selectedAudio={state.session?.decision.audio?.track_ref ?? null}
            onSelectAudio={selectAudio}
            subtitleStyle={subtitleStyle}
            onSubtitleStyleChange={(style) => {
              setSubtitleStyle(style);
              saveSubtitleStyle(style);
            }}
            diagnosticsOpen={diagnosticsOpen}
            onToggleDiagnostics={() => setDiagnosticsOpen((open) => !open)}
            // 剧集才有右下角那个切集位；电影 episodeLabel 为 null
            isSeries={episodeLabel !== null}
            onNext={next ? onPlayNext : null}
            onPrev={prev ? onPlayPrev : null}
            landscape={landscape}
            canRotate={canRotate}
            onToggleLandscape={toggleLandscape}
            systemSubtitles={systemSubtitles}
            quality={quality}
            onSelectQuality={selectQuality}
            fullscreen={fullscreen}
            onToggleFullscreen={toggleFullscreen}
            onMenuOpenChange={setMenuOpen}
            trickplay={trickplay}
            chapters={chapters}
            fakeLandscape={fakeLandscape}
          />
        </div>
      </MediaController>
    </div>
  );
}

/** 转圈时说清楚卡在哪一段——「正在启动」和「正在降档重试」不是一回事。 */
function busyLabel(phase: string): string {
  switch (phase) {
    case "deciding":
      return "正在判断播放方式…";
    case "session-starting":
      return "正在准备视频流…";
    case "degrading":
      return "这一档放不出来，正在降档重试…";
    default:
      return "正在缓冲…";
  }
}
