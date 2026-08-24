"use client";

import { useEffect, useRef, useState } from "react";

import { fetchEmbeddedFonts } from "@/lib/api/playback";
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
  /** 控制条展开时要让出的底部高度（px）；收起时传 0 */
  avoidBottomPx: number;
}

/**
 * object-contain 之后画面在元素里的实际矩形（竖屏看横片时上下是大片黑边）。
 * 字幕必须锚定这个矩形而不是容器：按容器定位，竖屏时字幕会悬在离画面很远
 * 的黑边中间，观感脱节；字号同理——原来用 cqh 号称「跟随画面高度」，其实
 * cqh 是容器高，竖屏时字会大得离谱。
 */
export function useVideoContentBox(video: HTMLVideoElement | null): {
  bottomInset: number;
  height: number;
} {
  const [box, setBox] = useState({ bottomInset: 0, height: 0 });
  useEffect(() => {
    if (!video) return;
    const measure = () => {
      const cw = video.clientWidth;
      const ch = video.clientHeight;
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      if (!vw || !vh || !cw || !ch) {
        setBox({ bottomInset: 0, height: ch });
        return;
      }
      const height = vh * Math.min(cw / vw, ch / vh);
      setBox({ bottomInset: (ch - height) / 2, height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(video);
    // video 的 resize 事件在 videoWidth/videoHeight 变化时触发，比只听
    // loadedmetadata 可靠：metadata 可能早于本 effect 挂载就到了（此时首次
    // measure 读到 0 走了兜底），换会话、降档换流也都靠它兜住
    video.addEventListener("resize", measure);
    video.addEventListener("loadedmetadata", measure);
    return () => {
      observer.disconnect();
      video.removeEventListener("resize", measure);
      video.removeEventListener("loadedmetadata", measure);
    };
  }, [video]);
  return box;
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

/**
 * PGS：libbitsub 渲染器的生命周期（docs/design/web-player.md §6.2）。
 *
 * 蓝光位图轨没有任何浏览器原生渲染路径，Jellyfin 10.9+ 的解法是服务端把轨
 * 抽成 .sup、网页端 WASM 解码 + canvas 合成——我们用同一个库（libbitsub，
 * libpgs 的后继）。动态 import：不选 PGS 轨不付 WASM 代价，与 JASSUB 同款
 * 懒加载。timeOffset 语义与 JASSUB 相同（查表时间 = 播放时间 + offset）。
 *
 * streamingLoad：.sup 可达几十 MB，边收边解，首条字幕不用等整个文件；
 * rangeRequests 不开——字幕端点是 FileResponse 整流下发，没实现 Range。
 */
function usePgs(
  video: HTMLVideoElement | null,
  track: SubtitleOption | null,
  timeOffset: number,
): void {
  const instance = useRef<{ timeOffset: number; dispose(): void } | null>(null);

  useEffect(() => {
    if (!video || !track || track.kind !== "pgs") return;
    let disposed = false;

    void import("libbitsub").then(({ PgsRenderer }) => {
      if (disposed) return;
      // 渲染 canvas 会被追加到 video 的父节点末尾，而且是**异步**创建的
      // （要先等 WASM 初始化）；先记下已有的 canvas，之后新出现的那块就是
      // 它——libbitsub 没把 canvas 暴露成公开属性。
      const parent = video.parentElement;
      const before = new Set(parent?.querySelectorAll("canvas") ?? []);
      // media-controller 会在用户无操作时把普通子元素统一淡出，字幕是内容
      // 不是控制件，必须常显（noautohide 豁免，理由同 VTT 层与 JASSUB）。
      // 必须在 onLoaded 里补一次：构造返回时 canvas 多半还没插进 DOM，
      // 只在构造后设会漏掉——表现为字幕渲染成功却在控制条淡出时一起消失。
      const exemptNewCanvases = () => {
        parent?.querySelectorAll("canvas").forEach((canvas) => {
          if (!before.has(canvas)) canvas.setAttribute("noautohide", "");
        });
      };
      const created = new PgsRenderer({
        video,
        subUrl: track.url,
        timeOffset,
        streamingLoad: true,
        rangeRequests: false,
        onLoaded: exemptNewCanvases,
        onError: (error: Error) => {
          console.error("PGS 字幕加载失败：", error);
        },
        // 非致命诊断保留在控制台：位图轨的坏段/解析退化肉眼只看得到
        // 「字幕没出来」，没有这行用户报障时什么线索都没有
        onWarning: (warning: unknown) => {
          console.warn("PGS 字幕警告：", warning);
        },
      });
      instance.current = created;
      exemptNewCanvases();
    });

    return () => {
      disposed = true;
      instance.current?.dispose();
      instance.current = null;
    };
    // timeOffset 变化直接改实例属性（见下一个 effect），不重建
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [video, track]);

  useEffect(() => {
    if (instance.current) instance.current.timeOffset = timeOffset;
  }, [timeOffset]);
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
    // 内嵌字体与 WASM 并行拿：字体抽取要通读整个容器，串行会让字幕晚出来。
    // 字体拿不到不是灾难——JASSUB 还有兜底字体，只是排版会走样，所以吞掉异常。
    void Promise.all([
      import("jassub"),
      fetchEmbeddedFonts(track.url).catch(() => [] as string[]),
    ]).then(([{ default: JASSUB }, embeddedFonts]) => {
      if (disposed) return;
      const created = new JASSUB({
        video,
        subUrl: track.url,
        // worker 与 wasm 必须是独立 URL，由 scripts/copy-jassub.mjs 放进 public/
        workerUrl: "/jassub/jassub-worker.js",
        wasmUrl: "/jassub/jassub-worker.wasm",
        modernWasmUrl: "/jassub/jassub-worker-modern.wasm",
        // 兜底字体放最后：字幕指定的字体在内嵌字体里找不到时才用它，
        // 否则整段渲染不出来
        fonts: [...embeddedFonts, "/jassub/default.woff2"],
        timeOffset,
      });
      instance.current = created;
      // JASSUB 把渲染 canvas 插在 video 旁边，也在 media-controller 的
      // 自动淡出范围内——同样要豁免，理由同上面 VTT 层的 noautohide
      (created as { _canvas?: HTMLElement })._canvas?.setAttribute("noautohide", "");
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

export function SubtitleLayer({
  video,
  track,
  style,
  baseOffsetSeconds,
  avoidBottomPx,
}: SubtitleLayerProps) {
  // VTT：cue 时间（文件绝对）→ 会话相对，再加上用户微调（正数 = 字幕延后）
  const vttLines = useVttCues(video, track, -baseOffsetSeconds + style.offsetSeconds);
  // JASSUB / libbitsub：查表时间 = 播放时间 + timeOffset，方向与上面相反
  useJassub(video, track, baseOffsetSeconds - style.offsetSeconds);
  usePgs(video, track, baseOffsetSeconds - style.offsetSeconds);
  const contentBox = useVideoContentBox(video);

  if (!track || track.kind !== "vtt" || vttLines.length === 0) return null;

  // 锚在画面内底部；控制条展开且会压到时整体抬升（YouTube 同款避让）。
  // 竖屏时画面下沿本来就远高于控制条，max 自然不生效，字幕纹丝不动。
  const bottomPx = Math.max(
    contentBox.bottomInset + (style.bottomPercent / 100) * contentBox.height,
    avoidBottomPx,
  );

  return (
    // noautohide：media-controller 会在用户无操作时把普通子元素统一淡出，
    // 字幕是内容不是控制件，必须常显——否则表现为「鼠标一动字幕才出现」
    <div
      {...{ noautohide: "" }}
      className="pointer-events-none absolute inset-x-0 z-10 flex flex-col items-center gap-1 px-[6%] text-center transition-[bottom] duration-300"
      style={{ bottom: `${bottomPx}px` }}
      aria-live="off"
    >
      {vttLines.map((line, index) => (
        <p
          key={`${index}-${line}`}
          className="max-w-full whitespace-pre-wrap font-medium leading-snug text-white"
          style={{
            // 字号 = 画面实际高度的百分比（不是容器高）：竖屏 letterbox
            // 时容器高是画面高的好几倍，按容器算字会大得离谱。16px 下限是
            // 移动端通用做法——竖屏小画面按比例算不到 10px，谁也读不了
            fontSize: `${Math.max(16, (style.fontScale / 100) * contentBox.height)}px`,
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
