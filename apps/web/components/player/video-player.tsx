"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { MediaController } from "media-chrome/react";

import { ConsentDialog } from "@/components/player/consent-dialog";
import { DiagnosticsPanel } from "@/components/player/diagnostics-panel";
import { PlayerControls } from "@/components/player/player-controls";
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
  fetchTrickplay,
  stopPlaybackSession,
  stopPlaybackSessionOnUnload,
} from "@/lib/api/playback";
import { getCapabilitySnapshot } from "@/lib/player/capability";
import type { PlaybackEngine } from "@/lib/player/engine";
import { createEngine } from "@/lib/player/engine";
import { initialPlayerState, isBusy, playerReducer } from "@/lib/player/machine";
import { createSessionReleaser } from "@/lib/player/session-release";
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
 * 时间轴换算、字幕规划、快捷键映射），因此它们能被 node --test 直接覆盖，
 * 这里只剩「什么时候调它们」。
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
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [chromeVisible, setChromeVisible] = useState(true);
  const [countdown, setCountdown] = useState<number | null>(null);
  /** 用户明确取消过本集的「下一集」提示：取消后不能因为还在片尾窗口里又弹回来 */
  const [nextDismissed, setNextDismissed] = useState(false);
  /** 元数据里的时长（毫秒）。档 0 直出时它就是片长，服务端算不出时的兜底 */
  const [videoDurationMs, setVideoDurationMs] = useState<number | null>(null);
  // video 元素挂载后要触发依赖它的 effect，所以用 state 而不是纯 ref 持有
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);

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
      // 自动播放被浏览器拦下不是错误：用户点一下就好，别弹错误吓人
      void video.play().catch(() => undefined);
    });

    return () => {
      disposed = true;
      engine.destroy();
      engineRef.current = null;
    };
  }, [state.session, video]);

  /** video 元素事件 → 状态机 / 进度。绑一次，值从 ref 读。 */
  useEffect(() => {
    if (!video) return;
    const onPlaying = () => {
      setPaused(false);
      dispatch({ type: "playing" });
    };
    const onPause = () => setPaused(true);
    const onWaiting = () => dispatch({ type: "buffering" });
    const onSeeking = () => dispatch({ type: "seeking" });
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

    video.addEventListener("playing", onPlaying);
    video.addEventListener("pause", onPause);
    video.addEventListener("waiting", onWaiting);
    video.addEventListener("seeking", onSeeking);
    video.addEventListener("ended", onEnded);
    video.addEventListener("durationchange", onDurationChange);
    video.addEventListener("timeupdate", onTimeUpdate);
    return () => {
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("waiting", onWaiting);
      video.removeEventListener("seeking", onSeeking);
      video.removeEventListener("ended", onEnded);
      video.removeEventListener("durationchange", onDurationChange);
      video.removeEventListener("timeupdate", onTimeUpdate);
    };
  }, [video]);

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

  /** 离开这一集（切集或退出播放器）时补一次停止上报。 */
  useEffect(() => {
    const snapshot = { ...unit };
    return () => {
      if (reportedStartRef.current === null) return;
      void reportPlaybackProgress({
        ...snapshot,
        event: "stop",
        position_ms: positionRef.current,
      }).catch(() => undefined);
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
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitKey, sessionId]);

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
    if (video.paused) void video.play().catch(() => undefined);
    else video.pause();
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

  const toggleFullscreen = useCallback(() => {
    const target = containerRef.current;
    if (!target) return;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    else void target.requestFullscreen().catch(() => undefined);
  }, []);

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
          toggleFullscreen();
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
  }, [togglePlay, seekBy, seekToFileMs, toggleFullscreen, durationMs, video, subtitles.options]);

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
    return () => {
      mediaSession.metadata = null;
      for (const action of ["play", "pause", "seekbackward", "seekforward", "nexttrack"] as const) {
        mediaSession.setActionHandler(action, null);
      }
    };
  }, [title, episodeLabel, posterUrl, togglePlay, seekBy, next, onPlayNext]);

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
    if (paused || diagnosticsOpen) {
      setChromeVisible(true);
      return;
    }
    const timer = window.setTimeout(() => setChromeVisible(false), IDLE_HIDE_MS);
    return () => window.clearTimeout(timer);
  }, [paused, diagnosticsOpen, chromeVisible]);

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

  const busy = isBusy(state.phase);

  return (
    <div
      ref={containerRef}
      className="player-root relative size-full bg-black"
      onPointerMove={() => setChromeVisible(true)}
      onPointerLeave={() => !paused && setChromeVisible(false)}
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
          onClick={togglePlay}
          className="size-full object-contain"
        />

        <SubtitleLayer
          video={video}
          track={activeSubtitle}
          style={subtitleStyle}
          baseOffsetSeconds={(state.session?.start_ms ?? 0) / 1000}
        />

        {/* 顶栏：返回 + 片名。全屏时也要留着，否则退不出去只能按 Esc */}
        <div
          className={`pointer-events-none absolute inset-x-0 top-0 z-20 flex items-start gap-3 bg-gradient-to-b from-black/70 to-transparent px-4 pb-12 pt-4 transition-opacity duration-200 ${
            chromeVisible ? "opacity-100" : "opacity-0"
          }`}
        >
          <button
            type="button"
            onClick={onExit}
            // 淡出后必须同时断掉命中：隐形却仍能点的按钮会在用户想点画面时误触
            className={`flex size-9 shrink-0 items-center justify-center rounded-lg text-white/85 transition-colors hover:bg-white/15 ${
              chromeVisible ? "pointer-events-auto" : "pointer-events-none"
            }`}
            aria-label="退出播放"
          >
            <svg viewBox="0 0 24 24" className="size-5 fill-current" aria-hidden>
              <path d="M10.7 4.3 3 12l7.7 7.7 1.4-1.4L6.8 13H21v-2H6.8l5.3-5.3-1.4-1.4Z" />
            </svg>
          </button>
          <div className="min-w-0">
            <h1 className="truncate text-[15px] font-medium text-white">{title}</h1>
            {episodeLabel ? (
              <p className="truncate text-[13px] text-white/60">{episodeLabel}</p>
            ) : null}
          </div>
        </div>

        {busy ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
            <div className="flex flex-col items-center gap-3">
              <span className="size-9 animate-spin rounded-full border-2 border-white/25 border-t-white" />
              <span className="text-[13px] text-white/70">{busyLabel(state.phase)}</span>
            </div>
          </div>
        ) : null}

        {state.phase === "error" && state.error ? (
          <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/80 px-6">
            <div className="max-w-[440px] text-center">
              <p className="text-[15px] text-white">{state.error.message}</p>
              {state.error.suggestion ? (
                <p className="mt-2 text-[13px] leading-relaxed text-white/60">
                  {state.error.suggestion}
                </p>
              ) : null}
              <div className="mt-5 flex justify-center gap-2">
                <button
                  type="button"
                  onClick={onExit}
                  className="rounded-xl bg-white/10 px-4 py-2 text-[13px] text-white transition-colors hover:bg-white/15"
                >
                  返回
                </button>
                <button
                  type="button"
                  onClick={() => dispatch({ type: "request", startMs: positionRef.current })}
                  className="rounded-xl bg-white px-4 py-2 text-[13px] font-medium text-black transition-opacity hover:opacity-90"
                >
                  重试
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

        {countdown !== null && next ? (
          <div className="absolute bottom-28 right-4 z-20 w-[260px] rounded-xl border border-white/10 bg-black/85 p-4 backdrop-blur-md">
            <p className="text-[12px] text-white/50">即将播放</p>
            <p className="mt-1 truncate text-[14px] text-white">{next.label}</p>
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={() => {
                  setCountdown(null);
                  setNextDismissed(true);
                }}
                className="flex-1 rounded-lg bg-white/10 px-3 py-1.5 text-[13px] text-white/80 transition-colors hover:bg-white/15"
              >
                取消
              </button>
              <button
                type="button"
                onClick={onPlayNext}
                className="flex-1 rounded-lg bg-white px-3 py-1.5 text-[13px] font-medium text-black"
              >
                立即播放 {countdown}
              </button>
            </div>
          </div>
        ) : null}

        <div
          className={`absolute inset-x-0 bottom-0 z-20 transition-opacity duration-200 ${
            chromeVisible ? "opacity-100" : "pointer-events-none opacity-0"
          }`}
        >
          <PlayerControls
            positionMs={positionMs}
            durationMs={durationMs}
            bufferedEndMs={bufferedEndMs}
            paused={paused}
            onTogglePlay={togglePlay}
            onSeek={seekToFileMs}
            onSeekBy={seekBy}
            subtitles={subtitles}
            selectedSubtitle={selectedSubtitle}
            onSelectSubtitle={setSelectedSubtitle}
            subtitleStyle={subtitleStyle}
            onSubtitleStyleChange={setSubtitleStyle}
            diagnosticsOpen={diagnosticsOpen}
            onToggleDiagnostics={() => setDiagnosticsOpen((open) => !open)}
            trickplay={trickplay}
            onNext={next ? onPlayNext : null}
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
