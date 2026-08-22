"use client";

import { useEffect, useRef, useState } from "react";

import { plainCueText } from "@/lib/player/subtitles";
import type { SubtitleOption, SubtitleStyle } from "@/lib/player/subtitles";

/**
 * 字幕渲染层（docs/design/web-player.md §6.2）。
 *
 * 两条路径，都**旁挂**，绝不烧录（硬边界 1）：
 *
 * - **VTT**：交给浏览器解析（`<track>` + `mode="hidden"`），但**不用原生渲染**。
 *   原生 cue 的样式只能靠 `::cue` 改一点点，字号/描边/位置这些自建库刚需的项
 *   基本调不动；而 `TextTrackCue.startTime` 是可写的，时间轴微调也只有自己
 *   接管渲染才能做。所以这里只借浏览器的解析器，画面由本组件出。
 * - **ASS/SSA**：交给 JASSUB（libass WASM），特效与排版原样保留。番剧字幕
 *   一旦被转成 VTT 就只剩纯文本，等于毁掉。
 *
 * **时间轴换算**：字幕文件里的时间是**文件绝对时间**，而 `video.currentTime`
 * 在转码会话里是**会话相对时间**（零点在文件的 `start_ms` 处）。两边差一个
 * `baseOffsetSeconds`，这里是全前端唯一给字幕做这个补偿的地方。
 */

export interface SubtitleLayerProps {
  video: HTMLVideoElement | null;
  /** 当前选中的轨；null = 关闭字幕 */
  track: SubtitleOption | null;
  style: SubtitleStyle;
  /** 会话时间轴零点在文件里的位置（秒）= start_ms / 1000 */
  baseOffsetSeconds: number;
}

/** VTT：借浏览器解析，自己渲染，并按 baseOffset + 用户微调整体平移 cue。 */
function useVttCues(
  video: HTMLVideoElement | null,
  track: SubtitleOption | null,
  shiftSeconds: number,
): string[] {
  const [lines, setLines] = useState<string[]>([]);
  // 已经施加到 cue 上的平移量：cue 时间是累加修改的，必须记住上次的值才能算增量
  const appliedShift = useRef(0);
  const textTrack = useRef<TextTrack | null>(null);

  useEffect(() => {
    setLines([]);
    appliedShift.current = 0;
    textTrack.current = null;
    if (!video || !track || track.kind !== "vtt") return;

    const element = document.createElement("track");
    element.kind = "subtitles";
    element.src = track.url;
    if (track.language) element.srclang = track.language;
    element.label = track.label;
    video.appendChild(element);
    // hidden 而不是 showing：解析照常进行、cuechange 照常触发，但浏览器不画，
    // 画面完全由本组件负责。
    element.track.mode = "hidden";

    const onCueChange = () => {
      const active = element.track.activeCues;
      if (!active) return setLines([]);
      const collected: string[] = [];
      for (let i = 0; i < active.length; i += 1) {
        const cue = active[i];
        if ("text" in cue) collected.push(plainCueText(String(cue.text)));
      }
      setLines(collected);
    };
    element.track.addEventListener("cuechange", onCueChange);
    const onLoad = () => {
      textTrack.current = element.track;
      // 加载完成才拿得到 cues；此时补上当前的平移量
      shiftCues(element.track, shiftSeconds - appliedShift.current);
      appliedShift.current = shiftSeconds;
    };
    element.addEventListener("load", onLoad);

    return () => {
      element.track.removeEventListener("cuechange", onCueChange);
      element.removeEventListener("load", onLoad);
      element.remove();
      textTrack.current = null;
    };
    // shiftSeconds 刻意不进依赖：它变了只需平移已有 cue，不该重新下载整条轨
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [video, track]);

  useEffect(() => {
    const current = textTrack.current;
    if (!current) return;
    shiftCues(current, shiftSeconds - appliedShift.current);
    appliedShift.current = shiftSeconds;
  }, [shiftSeconds]);

  return lines;
}

/** 整条轨平移。cue 时间可写是 VTT 能做时间轴微调的唯一原因。 */
function shiftCues(track: TextTrack, delta: number): void {
  if (!delta || !track.cues) return;
  for (let i = 0; i < track.cues.length; i += 1) {
    const cue = track.cues[i];
    cue.startTime = Math.max(0, cue.startTime + delta);
    cue.endTime = Math.max(0, cue.endTime + delta);
  }
}

/** ASS：JASSUB 实例的生命周期。换轨要重建（v2 没有换轨 API），改偏移不用。 */
function useJassub(
  video: HTMLVideoElement | null,
  track: SubtitleOption | null,
  timeOffset: number,
): void {
  const instance = useRef<{ timeOffset: number; destroy(): Promise<void> } | null>(null);

  useEffect(() => {
    if (!video || !track || track.kind !== "ass") return;
    let disposed = false;

    // 动态 import：只有真的遇到 ASS 轨才付这几 MB 的 WASM 代价（§6.1 打包预算）
    void import("jassub").then(({ default: JASSUB }) => {
      if (disposed) return;
      const created = new JASSUB({
        video,
        subUrl: track.url,
        // worker 与 wasm 必须是独立 URL，由 scripts/copy-jassub.mjs 放进 public/
        workerUrl: "/jassub/jassub-worker.js",
        wasmUrl: "/jassub/jassub-worker.wasm",
        modernWasmUrl: "/jassub/jassub-worker-modern.wasm",
        // 兜底字体：字幕指定的字体本机没有时用它，否则整段渲染不出来
        fonts: ["/jassub/default.woff2"],
        timeOffset,
      });
      instance.current = created;
    });

    return () => {
      disposed = true;
      void instance.current?.destroy();
      instance.current = null;
    };
    // timeOffset 变化直接改实例属性（见下一个 effect），不重建
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [video, track]);

  useEffect(() => {
    if (instance.current) instance.current.timeOffset = timeOffset;
  }, [timeOffset]);
}

export function SubtitleLayer({ video, track, style, baseOffsetSeconds }: SubtitleLayerProps) {
  // VTT：cue 时间（文件绝对）→ 会话相对，再加上用户微调（正数 = 字幕延后）
  const vttLines = useVttCues(video, track, -baseOffsetSeconds + style.offsetSeconds);
  // JASSUB：查表时间 = 播放时间 + timeOffset，方向与上面相反
  useJassub(video, track, baseOffsetSeconds - style.offsetSeconds);

  if (!track || track.kind !== "vtt" || vttLines.length === 0) return null;

  return (
    <div
      className="pointer-events-none absolute inset-x-0 z-10 flex flex-col items-center gap-1 px-[6%] text-center"
      style={{ bottom: `${style.bottomPercent}%` }}
      aria-live="off"
    >
      {vttLines.map((line, index) => (
        <p
          key={`${index}-${line}`}
          className="max-w-full whitespace-pre-wrap font-medium leading-snug text-white"
          style={{
            // cqh：字号跟随画面高度而不是视口——分屏、画中画、内联播放时
            // 字幕比例才不会跑掉
            fontSize: `${style.fontScale}cqh`,
            // 描边用四向 text-shadow：亮画面上的白字没有描边基本读不清，
            // 而 -webkit-text-stroke 会把笔画往里吃、中文字形直接糊掉
            textShadow: style.outline
              ? "0 0 0.12em #000, 0.03em 0.03em 0.1em #000, -0.03em 0.03em 0.1em #000, 0.03em -0.03em 0.1em #000, -0.03em -0.03em 0.1em #000"
              : "none",
            background: style.background ? "rgba(0,0,0,0.55)" : "transparent",
            padding: style.background ? "0.05em 0.35em" : undefined,
            borderRadius: style.background ? "0.15em" : undefined,
          }}
        >
          {line}
        </p>
      ))}
    </div>
  );
}
