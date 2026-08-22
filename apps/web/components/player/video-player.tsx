"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { MediaController } from "media-chrome/react";

import { ChevronLeftIcon } from "@/components/icons";

import { ConsentDialog } from "@/components/player/consent-dialog";
import { DiagnosticsPanel } from "@/components/player/diagnostics-panel";
import { PlayerCenterControls, PlayerControls } from "@/components/player/player-controls";
import { SubtitleLayer } from "@/components/player/subtitle-layer";
import {
  type ClientCapability,
  type PlaybackUnit,
  type PlaybackWatchState,
  fetchResumeState,
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
import { getCapabilitySnapshot } from "@/lib/player/capability";
import type { PlaybackEngine } from "@/lib/player/engine";
import { createEngine } from "@/lib/player/engine";
import { initialPlayerState, isBusy, playerReducer } from "@/lib/player/machine";
import {
  asNativeVideo,
  enterLandscape,
  enterNativeVideoFullscreen,
  exitLandscape,
  screenOrientation,
} from "@/lib/player/orientation";
import { chromeMustStayVisible, shouldHideOnPointerLeave } from "@/lib/player/chrome";
import { createSessionReleaser } from "@/lib/player/session-release";
import {
  type QoeEvent,
  initialQoe,
  isReportable,
  reduceQoe,
  summarize,
} from "@/lib/player/qoe";
import type { TrickplayIndex } from "@/lib/player/trickplay";
import { isEditableTarget, resolveShortcut } from "@/lib/player/shortcuts";
import type { SubtitleStyle } from "@/lib/player/subtitles";
import {
  DEFAULT_SUBTITLE_STYLE,
  pickInitialSubtitle,
  planSubtitleTracks,
} from "@/lib/player/subtitles";
import { isInEndCredits, planSeek, toFileMs, toSessionSeconds } from "@/lib/player/timeline";

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
  title: string;
  /** 副标题：剧集显示「S01E05 · 集名」，电影为 null */
  episodeLabel: string | null;
  posterUrl: string | null;
  /** 下一集；没有（电影 / 本季最后一集）为 null */
  next: { unit: PlaybackUnit; label: string } | null;
  onPlayNext: () => void;
  onExit: () => void;
}

/** 进度心跳间隔。服务端另有节流，这里给足密度即可。 */
const PROGRESS_INTERVAL_MS = 10_000;
/** 会话续命间隔。必须明显短于服务端的空闲回收窗口。 */
const PING_INTERVAL_MS = 15_000;
/** 播放中控制条自动隐藏的静止时长。 */
const IDLE_HIDE_MS = 3000;
/** 片尾「下一集」倒计时秒数。 */
const NEXT_COUNTDOWN_S = 10;

function unitKeyOf(unit: PlaybackUnit): string {
  return `${unit.media_item_id}/${unit.season_number ?? 0}/${unit.episode_number ?? 0}`;
}

export function VideoPlayer(props: VideoPlayerProps) {
  const { unit, title, episodeLabel, posterUrl, next, onPlayNext, onExit } = props;
  const unitKey = unitKeyOf(unit);

  const [state, dispatch] = useReducer(playerReducer, initialPlayerState);
  const [resume, setResume] = useState<PlaybackWatchState | null>(null);
  const [positionMs, setPositionMs] = useState(0);
  const [bufferedEndMs, setBufferedEndMs] = useState<number | null>(null);
  const [paused, setPaused] = useState(true);
  const [selectedSubtitle, setSelectedSubtitle] = useState<string | null>(null);
  const [subtitleStyle, setSubtitleStyle] = useState<SubtitleStyle>(DEFAULT_SUBTITLE_STYLE);
  const [trickplay, setTrickplay] = useState<TrickplayIndex | null>(null);
  // 播放质量累计。放 ref 而不是 state：每秒都在变，进渲染只会白重绘。
  const qoeRef = useRef(initialQoe());
  // 快照函数放 ref：卸载与切集的 effect 都不依赖 state.session，
  // 直接闭包会拿到过期的会话，上报到错误的档位上。
  const qoeSnapshotRef = useRef<() => PlaybackMetricPayload | null>(() => null);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [chromeVisible, setChromeVisible] = useState(true);
  const [countdown, setCountdown] = useState<number | null>(null);
  /** 用户明确取消过本集的「下一集」提示：取消后不能因为还在片尾窗口里又弹回来 */
  const [nextDismissed, setNextDismissed] = useState(false);
  /** 元数据里的时长（毫秒）。档 0 直出时它就是片长，服务端算不出时的兜底 */
  const [videoDurationMs, setVideoDurationMs] = useState<number | null>(null);
  // video 元素挂载后要触发依赖它的 effect，所以用 state 而不是纯 ref 持有
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  /** 本集自动播放的结果。只有 blocked 需要界面兜底（中央大播放键） */
  const [autoplay, setAutoplay] = useState<AutoplayOutcome | null>(null);
  /**
   * 是**我们**为了绕过自动播放策略把它静音的。
   *
   * 与 `autoplay` 分开的理由：静音是**跨集延续**的状态（video 元素一直是
   * 同一个），而 `autoplay` 每集重置。合成一个的后果是切下一集时提示消失、
   * 声音却还没回来，用户以为这部剧没有音轨。
   */
  const [forcedMute, setForcedMute] = useState(false);
  /** 当前是否在横屏（全屏 + 锁横向）里 */
  const [landscape, setLandscape] = useState(false);
  /**
   * 这台设备的方向锁真的能用。放 state 而不是渲染时直接算：服务端渲染没有
   * `screen`，直接算会造成水合不一致，按钮文案在首帧闪一下。
   */
  const [canRotate, setCanRotate] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<PlaybackEngine | null>(null);
  const capabilityRef = useRef<ClientCapability | null>(null);
  /** 已发起的起播请求指纹：React 严格模式下 effect 会跑两遍，靠它去重 */
  const startedKeyRef = useRef<string | null>(null);
  /** 本轮要落到的文件位置（续播点 / seek 目标 / 降档前的位置） */
  const pendingFileMsRef = useRef(0);
  /** 供事件回调读取最新值，避免为了拿一个数字反复重绑监听 */
  const startMsRef = useRef(0);
  const positionRef = useRef(0);
  const reportedStartRef = useRef<string | null>(null);
  /** 用户还想不想自动播放：他自己按过暂停之后就不再替他做主 */
  const wantsPlayRef = useRef(true);
  const autoplayAttemptsRef = useRef(0);
  const autoplayLastRef = useRef<AutoplayOutcome | null>(null);
  /**
   * 按下的那一刻控制条在不在。
   *
   * 触屏上没有悬停，控制条藏起来时第一下点击应该只是**把它叫出来**，而不是
   * 顺手把片子暂停了（YouTube 手机端就是这样）。但 `setChromeVisible(true)`
   * 是异步的，click 回调读到的可能已经是新值，所以要在 pointerdown 时把
   * 「当时」的状态记进 ref，判断才稳定。桌面上鼠标一动控制条早就出来了，
   * 点击行为跟以前一样。
   */
  const chromeWasVisibleRef = useRef(true);
  // 会话释放器：区分「真的离开了」与「StrictMode 把同一个会话重新挂了一遍」。
  const sessionReleaser = useMemo(
    () =>
      createSessionReleaser((id) => {
        void stopPlaybackSession(id).catch(() => undefined);
      }),
    [],
  );

  startMsRef.current = state.session?.start_ms ?? 0;
  positionRef.current = positionMs;

  const failedKey = state.failedTiers.join(",");
  const sessionId = state.session?.session_id ?? null;

  // 片长：服务端算的真值优先。转码会话里 video.duration 只到「已经转出来的
  // 那一段」，拿它当分母会让进度条一路自己缩放。
  const durationMs = useMemo(() => {
    if (resume?.duration_ms) return resume.duration_ms;
    // 只有没会话（档 0 原文件直出）时 video.duration 才是整片时长
    return sessionId ? null : videoDurationMs;
  }, [resume?.duration_ms, sessionId, videoDurationMs]);

  const subtitles = useMemo(() => {
    const session = state.session;
    if (!session) return { options: [], unavailable: [] };
    return planSubtitleTracks(session.decision.subtitles, session.subtitle_urls);
  }, [state.session]);

  const activeSubtitle = useMemo(
    () => subtitles.options.find((option) => option.ref === selectedSubtitle) ?? null,
    [subtitles, selectedSubtitle],
  );

  // ---------------------------------------------------------------------
  // 起播链路
  // ---------------------------------------------------------------------

  /** 切集 / 首次进入：先问续播点，再发起决策。 */
  useEffect(() => {
    let cancelled = false;
    startedKeyRef.current = null;
    reportedStartRef.current = null;
    setPositionMs(0);
    setBufferedEndMs(null);
    setResume(null);
    setCountdown(null);
    setNextDismissed(false);
    setVideoDurationMs(null);
    setSelectedSubtitle(null);
    setTrickplay(null); // 换片必须清掉：旧片的缩略图配新片的进度条是错的
    // 新的一集重新获得自动播放的资格：上一集用户按过暂停不代表下一集也不想看
    wantsPlayRef.current = true;
    autoplayAttemptsRef.current = 0;
    autoplayLastRef.current = null;
    setAutoplay(null);
    dispatch({ type: "reset" });

    fetchResumeState(unit)
      .then((watched) => {
        if (cancelled) return;
        setResume(watched);
        // 已看完的重播从头开始——续播到最后三十秒等于点开就是片尾
        dispatch({ type: "request", startMs: watched.played ? 0 : watched.position_ms });
      })
      .catch(() => {
        if (cancelled) return;
        dispatch({ type: "request", startMs: 0 });
      });

    return () => {
      cancelled = true;
    };
    // unit 是对象字面量，按内容比较
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitKey]);

  /** 决策 / 降档 / 换会话：三种都是「去后端要一个能播的地址」。 */
  useEffect(() => {
    const phase = state.phase;
    if (phase !== "deciding" && phase !== "degrading" && phase !== "session-starting") return;

    const key = `${unitKey}|${phase}|${state.startMs}|${failedKey}|${state.failureCount}`;
    if (startedKeyRef.current === key) return;
    startedKeyRef.current = key;

    let cancelled = false;
    void (async () => {
      try {
        if (!capabilityRef.current) capabilityRef.current = await getCapabilitySnapshot();
        pendingFileMsRef.current = state.startMs;
        // 首帧从"用户要求播放"这一刻算起，而不是从会话就位算起——
        // 决策与起会话的耗时正是首帧延迟的大头
        qoe({ type: "play-requested", at: performance.now() });
        const session = await startPlaybackSession({
          ...unit,
          capability: capabilityRef.current,
          failed_tiers: state.failedTiers,
          start_ms: state.startMs,
        });
        if (cancelled) return;
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
  }, [state.phase, state.startMs, failedKey, state.failureCount, unitKey]);

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
    if (outcome === "muted") setForcedMute(true);
    // interrupted 是中间态，界面上什么都不该变——它等下一个时机再试
    if (outcome !== "interrupted") setAutoplay(outcome);
  }, [video]);

  /** 会话就位：挂引擎、落回目标位置、起播。 */
  useEffect(() => {
    const session = state.session;
    if (!session?.stream_url || !video) return;

    const engine = createEngine({
      video,
      streamUrl: resolveStreamUrl(session.stream_url),
      container: session.decision.container ?? "mp4",
      hasFullMse: capabilityRef.current?.mse === "full",
      onFailed: (reason) => dispatch({ type: "failed", reason }),
    });
    engineRef.current = engine;

    let disposed = false;
    void engine.attach().then(() => {
      if (disposed) return;
      // 转码会话已经从 start_ms 起转，换算结果≈0；档 0 直出没有偏移，
      // 续播点必须在这里真的跳一次。两种情况同一行代码。
      const target = toSessionSeconds(pendingFileMsRef.current, session.start_ms);
      if (target > 1) {
        const seek = () => {
          video.currentTime = target;
        };
        if (video.readyState >= 1) seek();
        else video.addEventListener("loadedmetadata", seek, { once: true });
      }
      void tryAutoplay();
    });

    return () => {
      disposed = true;
      engine.destroy();
      engineRef.current = null;
    };
  }, [state.session, video, tryAutoplay]);

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
    setAutoplay(null);
  }, [state.session]);

  /** 累计一条播放质量事件。归约是纯函数，这里只负责喂事件。 */
  const qoe = useCallback((event: QoeEvent) => {
    qoeRef.current = reduceQoe(qoeRef.current, event);
  }, []);

  /** video 元素事件 → 状态机 / 进度 / 质量采集。绑一次，值从 ref 读。 */
  useEffect(() => {
    if (!video) return;
    const onPlaying = () => {
      setPaused(false);
      qoe({ type: "playing", at: performance.now() });
      dispatch({ type: "playing" });
    };
    const onPause = () => setPaused(true);
    const onWaiting = () => {
      qoe({ type: "waiting", at: performance.now() });
      dispatch({ type: "buffering" });
    };
    const onSeeking = () => {
      qoe({ type: "seeking", at: performance.now() });
      dispatch({ type: "seeking" });
    };
    const onSeeked = () => qoe({ type: "seeked", at: performance.now() });
    const onEnded = () => dispatch({ type: "ended" });
    const onDurationChange = () =>
      setVideoDurationMs(
        Number.isFinite(video.duration) && video.duration > 0
          ? Math.round(video.duration * 1000)
          : null,
      );
    const onTimeUpdate = () => {
      setPositionMs(toFileMs(video.currentTime, startMsRef.current));
      const ranges = video.buffered;
      setBufferedEndMs(
        ranges.length ? toFileMs(ranges.end(ranges.length - 1), startMsRef.current) : null,
      );
    };
    // 「现在应该能播了」的两个时机：挂流那一次可能太早，这两次是补刀
    const onReady = () => void tryAutoplay();
    // 用户也可能绕过我们的按钮、直接用控制条上的静音键解除，提示要跟着消失
    const onVolumeChange = () => {
      if (!video.muted) setForcedMute(false);
    };

    video.addEventListener("playing", onPlaying);
    video.addEventListener("pause", onPause);
    video.addEventListener("waiting", onWaiting);
    video.addEventListener("seeking", onSeeking);
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("ended", onEnded);
    video.addEventListener("durationchange", onDurationChange);
    video.addEventListener("timeupdate", onTimeUpdate);
    video.addEventListener("loadedmetadata", onReady);
    video.addEventListener("canplay", onReady);
    video.addEventListener("volumechange", onVolumeChange);
    return () => {
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("waiting", onWaiting);
      video.removeEventListener("seeking", onSeeking);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("ended", onEnded);
      video.removeEventListener("durationchange", onDurationChange);
      video.removeEventListener("timeupdate", onTimeUpdate);
      video.removeEventListener("loadedmetadata", onReady);
      video.removeEventListener("canplay", onReady);
      video.removeEventListener("volumechange", onVolumeChange);
    };
  }, [video, qoe, tryAutoplay]);

  // ---------------------------------------------------------------------
  // 字幕：轨记忆来自 playback_state，用户明确关掉过就不要再自作主张打开
  // ---------------------------------------------------------------------

  useEffect(() => {
    if (subtitles.options.length === 0) return;
    setSelectedSubtitle(pickInitialSubtitle(subtitles.options, resume?.subtitle_track ?? null));
  }, [subtitles, resume?.subtitle_track]);

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

  useEffect(() => {
    if (state.phase !== "playing") return;
    if (reportedStartRef.current !== unitKey) {
      reportedStartRef.current = unitKey;
      void reportPlaybackProgress({ ...unit, event: "start", ...trackRefs() }).catch(
        () => undefined,
      );
    }
    const timer = window.setInterval(() => {
      void reportPlaybackProgress({
        ...unit,
        event: "progress",
        position_ms: positionRef.current,
        ...trackRefs(),
      }).catch(() => undefined);
    }, PROGRESS_INTERVAL_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.phase, unitKey, trackRefs]);

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
      hw_backend: "",
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
      void reportPlaybackProgress({
        ...snapshot,
        event: "stop",
        position_ms: positionRef.current,
      }).catch(() => undefined);
      const metric = qoeSnapshotRef.current();
      if (metric) void reportPlaybackMetric(metric).catch(() => undefined);
      qoeRef.current = initialQoe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitKey]);

  /** 关标签页/切走：进度用 sendBeacon 补发，会话用 keepalive 掐掉。 */
  useEffect(() => {
    const onPageHide = () => {
      if (reportedStartRef.current !== null) {
        reportPlaybackProgressOnUnload({
          ...unit,
          event: "stop",
          position_ms: positionRef.current,
        });
      }
      if (sessionId) stopPlaybackSessionOnUnload(sessionId);
      const metric = qoeSnapshotRef.current();
      if (metric) reportPlaybackMetricOnUnload(metric);
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

  /** 每秒采一次观看时长与掉帧。掉帧率是「解码能力判对没有」的直接证据。 */
  useEffect(() => {
    if (!video) return;
    const timer = window.setInterval(() => {
      qoe({ type: "tick", at: performance.now(), playing: !video.paused && !video.ended });
      const stats = engineRef.current?.stats();
      if (stats?.totalFrames != null && stats.droppedFrames != null) {
        qoe({ type: "frames", dropped: stats.droppedFrames, total: stats.totalFrames });
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [video, qoe]);

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
   * 会话续命 + 离开时显式收尾。
   *
   * 心跳：用户关页面不会发任何信号，服务端的超时回收是唯一可靠兜底。
   *
   * 收尾：`pagehide` 只在真正卸载文档时触发，SPA 内部返回媒体库、切下一集
   * 都不会触发。释放的时机与 StrictMode 规避都在 `createSessionReleaser` 里，
   * 那边有单测；这里只负责「什么时候调它」。
   */
  useEffect(() => {
    if (!sessionId) return;
    sessionReleaser.acquire(sessionId);
    const timer = window.setInterval(() => {
      void pingPlaybackSession(sessionId).catch(() => undefined);
    }, PING_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
      sessionReleaser.release(sessionId);
    };
  }, [sessionId, sessionReleaser]);

  // ---------------------------------------------------------------------
  // 播放控制
  // ---------------------------------------------------------------------

  const togglePlay = useCallback(() => {
    if (!video) return;
    if (video.paused) {
      // 用户亲手点的播放：手势就在这一刻，一定放得起来；中央大播放键该消失。
      // 但静音提示留着——静音是另一回事，得他自己点「开启声音」。
      wantsPlayRef.current = true;
      autoplayLastRef.current = null;
      setAutoplay(null);
      void video.play().catch(() => undefined);
    } else {
      // 用户主动暂停：从这一刻起不再替他自动续播
      wantsPlayRef.current = false;
      video.pause();
    }
  }, [video]);

  /** 点画面：控制条藏着时只把它叫出来，露着时才是播放/暂停。 */
  const onSurfaceClick = useCallback(() => {
    if (chromeWasVisibleRef.current) togglePlay();
    else setChromeVisible(true);
  }, [togglePlay]);

  /** 恢复声音：自动播放被策略拦下时我们静音救回来的那一路，交还给用户。 */
  const restoreSound = useCallback(() => {
    if (!video) return;
    video.muted = false;
    // 静音时用户可能顺手把音量也拖到 0，只解静音会得到「点了没反应」
    if (video.volume === 0) video.volume = 1;
    setForcedMute(false);
  }, [video]);

  /**
   * 跳转。转码会话是「从 start_ms 起、边转边给」的单向流：拖出已转区间必须
   * 换会话，干等 ffmpeg 追上来只会让用户看着永远转不完的圈。
   */
  const seekToFileMs = useCallback(
    (fileMs: number) => {
      if (!video) return;
      const seekable = video.seekable;
      const plan = planSeek(fileMs, {
        startMs: startMsRef.current,
        seekableEndSeconds: seekable.length ? seekable.end(seekable.length - 1) : 0,
        hasSession: Boolean(sessionId),
      });
      if (plan.kind === "native") {
        video.currentTime = Math.max(0, plan.seconds);
        return;
      }
      // 换会话期间先把旧流停住：不停的话旧会话还在往前走，进度条会在新会话
      // 起来之前继续跳动，看着像"拖了没反应"
      video.pause();
      // 这次暂停是我们自己造成的，不是用户不想看了——换流后必须继续自动播放
      wantsPlayRef.current = true;
      pendingFileMsRef.current = plan.startMs;
      setPositionMs(plan.startMs);
      dispatch({ type: "restart", startMs: plan.startMs });
    },
    [video, sessionId],
  );

  const seekBy = useCallback(
    (seconds: number) => seekToFileMs(Math.max(0, positionRef.current + seconds * 1000)),
    [seekToFileMs],
  );

  useEffect(() => {
    // 桌面浏览器普遍有 screen.orientation.lock 这个方法但一调就抛，所以还要
    // 看指针类型：粗指针（手指）才是真能转的那类设备
    setCanRotate(
      typeof screenOrientation()?.lock === "function" &&
        window.matchMedia("(pointer: coarse)").matches,
    );
  }, []);

  /**
   * 横屏 / 退出横屏。
   *
   * 桌面上没有方向可锁，这个按钮退化成纯全屏——所以它是**唯一**的全屏入口，
   * 不再另外摆一个「全屏」按钮：同一个动作两个按钮只会让人猜哪个才对。
   */
  const toggleLandscape = useCallback(() => {
    if (landscape || document.fullscreenElement) {
      setLandscape(false);
      void exitLandscape(
        screenOrientation(),
        document.fullscreenElement ? () => document.exitFullscreen() : null,
      );
      return;
    }
    const target = containerRef.current;
    if (!target) return;
    void enterLandscape(target, screenOrientation()).then((outcome) => {
      // unsupported = 连元素级全屏都没有（iPhone Safari），把 <video> 交给
      // 系统播放器——iOS 上本来就不硬撑自定义 UI（§6.4）
      if (outcome === "unsupported") {
        enterNativeVideoFullscreen(asNativeVideo(video));
        return;
      }
      setLandscape(true);
    });
  }, [landscape, video]);

  /**
   * 用户按 Esc / 系统手势退出全屏时把状态同步回来，并解掉方向锁。
   *
   * 不解锁的后果很难查：退出播放器之后整个站点还被钉在横屏，用户只会觉得
   * 「这网站把我手机转坏了」。
   */
  useEffect(() => {
    const onChange = () => {
      if (document.fullscreenElement) return;
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

  /** 左上角的后退：在横屏里先回到普通，再按一次才是退出播放。 */
  const goBack = useCallback(() => {
    if (landscape) toggleLandscape();
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
      switch (action.type) {
        case "toggle-play":
          togglePlay();
          break;
        case "seek-by":
          seekBy(action.seconds);
          break;
        case "seek-percent":
          if (durationMs) seekToFileMs((durationMs * action.percent) / 100);
          break;
        case "volume-by":
          if (video) video.volume = Math.min(1, Math.max(0, video.volume + action.delta));
          break;
        case "toggle-mute":
          if (video) video.muted = !video.muted;
          break;
        case "toggle-fullscreen":
          toggleLandscape();
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
  }, [togglePlay, seekBy, seekToFileMs, toggleLandscape, durationMs, video, subtitles.options]);

  // ---------------------------------------------------------------------
  // 系统集成：媒体键 / 锁屏信息 / 防息屏
  // ---------------------------------------------------------------------

  useEffect(() => {
    const mediaSession = navigator.mediaSession;
    if (!mediaSession) return;
    mediaSession.metadata = new MediaMetadata({
      title: episodeLabel ? `${title} · ${episodeLabel}` : title,
      artist: episodeLabel ? title : undefined,
      artwork: posterUrl ? [{ src: posterUrl, sizes: "512x512" }] : undefined,
    });
    mediaSession.setActionHandler("play", () => togglePlay());
    mediaSession.setActionHandler("pause", () => togglePlay());
    mediaSession.setActionHandler("seekbackward", () => seekBy(-10));
    mediaSession.setActionHandler("seekforward", () => seekBy(10));
    mediaSession.setActionHandler("nexttrack", next ? () => onPlayNext() : null);
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
      for (const action of ["play", "pause", "seekbackward", "seekforward", "nexttrack"] as const) {
        mediaSession.setActionHandler(action, null);
      }
      try {
        mediaSession.setActionHandler("enterpictureinpicture" as MediaSessionAction, null);
      } catch {
        // 同上，没注册上自然也不用清
      }
    };
  }, [title, episodeLabel, posterUrl, togglePlay, seekBy, next, onPlayNext, video]);

  /** 播放中申请防息屏。切到后台会被系统收走，回来时重新申请。 */
  useEffect(() => {
    if (paused || state.phase !== "playing") return;
    let sentinel: WakeLockSentinel | null = null;
    let released = false;
    void navigator.wakeLock
      ?.request("screen")
      .then((lock) => {
        if (released) void lock.release().catch(() => undefined);
        else sentinel = lock;
      })
      .catch(() => undefined);
    return () => {
      released = true;
      void sentinel?.release().catch(() => undefined);
    };
  }, [paused, state.phase]);

  // ---------------------------------------------------------------------
  // 控制条自动隐藏 + 片尾下一集
  // ---------------------------------------------------------------------

  useEffect(() => {
    if (chromeMustStayVisible({ paused, menuOpen, diagnosticsOpen })) {
      setChromeVisible(true);
      return;
    }
    const timer = window.setTimeout(() => setChromeVisible(false), IDLE_HIDE_MS);
    return () => window.clearTimeout(timer);
  }, [paused, diagnosticsOpen, menuOpen, chromeVisible]);

  useEffect(() => {
    if (!next || nextDismissed) return;
    const inCredits = isInEndCredits(positionMs, durationMs);
    if (!inCredits && state.phase !== "ended") {
      setCountdown(null);
      return;
    }
    setCountdown((current) => current ?? NEXT_COUNTDOWN_S);
  }, [positionMs, durationMs, next, state.phase, nextDismissed]);

  useEffect(() => {
    if (countdown === null) return;
    if (countdown <= 0) {
      onPlayNext();
      return;
    }
    const timer = window.setTimeout(() => setCountdown(countdown - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [countdown, onPlayNext]);

  // 自动播放被彻底拦下时不能再转圈：状态机要等 `playing` 才离开 buffering，
  // 而那一刻永远不会来——转圈叠着中央播放键是最典型的「界面卡住了」观感。
  const busy = isBusy(state.phase) && autoplay !== "blocked";
  // 暂停海报：出过画之后才显示，否则起播那几秒会先闪一张「已暂停」
  const showPauseOverlay =
    paused && !busy && state.phase !== "error" && state.phase !== "consent" && positionMs > 0;

  return (
    <div
      ref={containerRef}
      className="player-root relative size-full bg-black"
      data-chrome={chromeVisible ? "visible" : "hidden"}
      onPointerMove={() => setChromeVisible(true)}
      onPointerDown={() => {
        chromeWasVisibleRef.current = chromeVisible;
        setChromeVisible(true);
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
        className="size-full"
        style={{ width: "100%", height: "100%", backgroundColor: "#000" }}
      >
        <video
          ref={setVideo}
          slot="media"
          playsInline
          poster={posterUrl ?? undefined}
          onClick={onSurfaceClick}
          className="size-full object-contain"
        />

        <SubtitleLayer
          video={video}
          track={activeSubtitle}
          style={subtitleStyle}
          baseOffsetSeconds={(state.session?.start_ms ?? 0) / 1000}
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
          className={`pointer-events-none absolute inset-x-0 top-0 z-20 flex items-center gap-4 bg-gradient-to-b from-black/80 via-black/30 to-transparent px-6 pb-16 pt-5 transition-opacity duration-300 max-md:px-3 max-md:pt-3 ${
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
        </div>

        {/* 暂停海报：Netflix 在暂停时把片名大字压在画面上，同时压暗背景，
            一眼知道「停在哪部片」。不拦指针——点画面仍然是继续播放。 */}
        {showPauseOverlay ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center bg-black/45 px-16 transition-opacity duration-300 max-md:px-6">
            <div className="min-w-0">
              <p className="text-[13px] uppercase tracking-[0.3em] text-white/55">已暂停</p>
              <h2 className="mt-3 truncate text-[44px] font-bold leading-tight text-white drop-shadow-lg max-md:text-[26px]">
                {title}
              </h2>
              {episodeLabel ? (
                <p className="mt-1 truncate text-[18px] text-white/70 max-md:text-[14px]">
                  {episodeLabel}
                </p>
              ) : null}
            </div>
          </div>
        ) : null}

        {busy ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
            <div className="flex flex-col items-center gap-4">
              <span className="size-12 animate-spin rounded-full border-[3px] border-white/15 border-t-[var(--player-accent)]" />
              <span className="text-[14px] text-white/75">{busyLabel(state.phase)}</span>
            </div>
          </div>
        ) : null}

        {/* 自动播放被静音救回来了：给一个显眼但不挡画面的恢复声音入口。
            不给的话用户会以为这部片没有音轨。 */}
        {forcedMute ? (
          <button
            type="button"
            onClick={restoreSound}
            // 放左下角而不是右上角：右上是诊断面板、右下是下一集卡片，
            // 三个浮层挤在同一角上一定会盖住彼此
            className={`absolute bottom-32 left-6 z-30 flex items-center gap-2 rounded-sm bg-white px-4 py-2.5 text-[14px] font-semibold text-black shadow-lg transition-opacity duration-300 hover:opacity-90 max-md:bottom-28 max-md:left-3 ${
              chromeVisible ? "opacity-100" : "pointer-events-none opacity-0"
            }`}
          >
            <svg viewBox="0 0 24 24" className="size-5 fill-current" aria-hidden>
              <path d="M4 9.5h3.4L12 5.2v13.6L7.4 14.5H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1Z" />
              <path d="M15.4 8.6a4.6 4.6 0 0 1 0 6.8l-1.3-1.5a2.6 2.6 0 0 0 0-3.8l1.3-1.5Z" />
              <path d="M17.6 5.9a8.2 8.2 0 0 1 0 12.2l-1.3-1.5a6.2 6.2 0 0 0 0-9.2l1.3-1.5Z" />
            </svg>
            开启声音
          </button>
        ) : null}

        {state.phase === "error" && state.error ? (
          <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/85 px-6">
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
                  className="rounded-sm bg-[var(--player-accent)] px-6 py-2.5 text-[14px] font-semibold text-black transition-colors hover:bg-[var(--player-accent-hover)]"
                >
                  重试
                </button>
                <button
                  type="button"
                  onClick={onExit}
                  className="rounded-sm bg-white/15 px-6 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-white/25"
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
            onGranted={() => dispatch({ type: "consent-granted" })}
            onCancel={onExit}
          />
        ) : null}

        {diagnosticsOpen && state.session ? (
          <DiagnosticsPanel
            session={state.session}
            engine={engineRef.current}
            onClose={() => setDiagnosticsOpen(false)}
          />
        ) : null}

        {/* 下一集卡片：Netflix 把倒计时画成按钮里逐渐填满的底色，比一个
            跳动的数字更好读——余量一眼可见，也不会读到一半就跳走。 */}
        {countdown !== null && next ? (
          <div className="absolute bottom-32 right-6 z-30 w-[300px] rounded-sm bg-[rgba(20,20,20,0.95)] p-4 shadow-[0_8px_32px_rgba(0,0,0,0.7)] max-md:bottom-28 max-md:right-3 max-md:w-[240px]">
            <p className="text-[12px] uppercase tracking-wide text-white/50">即将播放</p>
            <p className="mt-1.5 truncate text-[15px] font-semibold text-white">{next.label}</p>
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => {
                  setCountdown(null);
                  setNextDismissed(true);
                }}
                className="rounded-sm bg-white/15 px-4 py-2 text-[13px] text-white/85 transition-colors hover:bg-white/25"
              >
                取消
              </button>
              <button
                type="button"
                onClick={onPlayNext}
                className="relative flex-1 overflow-hidden rounded-sm bg-white px-4 py-2 text-[13px] font-semibold text-black"
              >
                <span
                  className="absolute inset-y-0 left-0 bg-black/15 transition-[width] duration-1000 ease-linear"
                  style={{
                    width: `${((NEXT_COUNTDOWN_S - countdown) / NEXT_COUNTDOWN_S) * 100}%`,
                  }}
                />
                <span className="relative">立即播放 · {countdown}</span>
              </button>
            </div>
          </div>
        ) : null}

        <div className="absolute inset-x-0 bottom-0 z-20">
          <PlayerControls
            positionMs={positionMs}
            durationMs={durationMs}
            bufferedEndMs={bufferedEndMs}
            chromeVisible={chromeVisible}
            onSeek={seekToFileMs}
            subtitles={subtitles}
            selectedSubtitle={selectedSubtitle}
            onSelectSubtitle={setSelectedSubtitle}
            subtitleStyle={subtitleStyle}
            onSubtitleStyleChange={setSubtitleStyle}
            diagnosticsOpen={diagnosticsOpen}
            onToggleDiagnostics={() => setDiagnosticsOpen((open) => !open)}
            // 剧集才有右下角那个切集位；电影 episodeLabel 为 null
            isSeries={episodeLabel !== null}
            onNext={next ? onPlayNext : null}
            landscape={landscape}
            canRotate={canRotate}
            onToggleLandscape={toggleLandscape}
            onMenuOpenChange={setMenuOpen}
            trickplay={trickplay}
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
