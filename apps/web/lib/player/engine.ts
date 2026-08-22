/**
 * 播放引擎抽象（docs/design/web-player.md §6.1）。
 *
 * 上层（播放器组件）只认这个接口，不认 hls.js 也不认 `<video src>`。这一层
 * 不可省的原因是 2026 年这块正在剧烈洗牌：Vidstack / Media Chrome / Plyr /
 * Mux Player 已合并重建 Video.js v10。引擎与 UI 都在接口后面，等 v10 GA 后
 * 才谈得上「按需评估」，否则届时要动的是整个播放页。
 *
 * **打包预算**：hls.js 只在计划为 hls-fmp4 时动态 import，档 0 的播放路由
 * 初始 JS 完全不带它。
 */

import type Hls from "hls.js";

/** 诊断面板（§6.5「Stats for nerds」）要的实时读数。 */
export interface EngineStats {
  /** 当前正在用的实现，出问题时第一眼要看的就是这个 */
  engine: "direct" | "hls.js" | "native-hls";
  /** 缓冲了多少秒（当前播放点之后） */
  bufferedSeconds: number;
  /** 实时码率（bps）；直出档拿不到，为 null */
  bitrate: number | null;
  /** 累计掉帧数：判断「能解但解不动」的唯一硬指标 */
  droppedFrames: number | null;
  totalFrames: number | null;
}

export interface PlaybackEngine {
  attach(): Promise<void>;
  destroy(): void;
  stats(): EngineStats;
}

export interface EngineOptions {
  video: HTMLVideoElement;
  streamUrl: string;
  /** "mp4" = 原文件直出；"hls-fmp4" = 转码会话的分片 */
  container: string;
  /** 客户端有没有完整 MSE。没有（iOS）时 HLS 交给系统原生播放 */
  hasFullMse: boolean;
  /**
   * 播放链路失败：error 事件或长时间无进展。**这是降档回路的唯一入口**——
   * 「看起来 codec 兼容、实际 copy 出来是坏流」的源片穷举不完（MKV header
   * compression、参数集只在 CodecPrivate、开放 GOP……），只能靠这条回路兜。
   */
  onFailed: (reason: string) => void;
}

/** 判定为卡死的无进展时长（秒）。低于这个值会把正常的网络抖动误判成失败。 */
const STALL_TIMEOUT_S = 8;

/**
 * 回收多少秒的已播缓冲（hls.js `backBufferLength`）。
 *
 * 不回收的话，一部三小时的片子会把已播分片一路堆在 SourceBuffer 里，吃掉
 * 几个 G 内存然后整个标签页崩掉——长片播放最典型的一种"放到一半就没了"。
 */
const BACK_BUFFER_S = 30;

/** 掉帧与缓冲读数：三种引擎共用一份取法。 */
function readCommonStats(video: HTMLVideoElement): Omit<EngineStats, "engine" | "bitrate"> {
  let bufferedSeconds = 0;
  for (let i = 0; i < video.buffered.length; i += 1) {
    if (video.buffered.start(i) <= video.currentTime && video.buffered.end(i) >= video.currentTime) {
      bufferedSeconds = video.buffered.end(i) - video.currentTime;
      break;
    }
  }
  const quality = video.getVideoPlaybackQuality?.();
  return {
    bufferedSeconds,
    droppedFrames: quality?.droppedVideoFrames ?? null,
    totalFrames: quality?.totalVideoFrames ?? null,
  };
}

/**
 * 卡死看门狗：播放中 currentTime 长时间不动就当作失败。
 *
 * 只看 `error` 事件是不够的——坏流最常见的表现不是报错，而是解码器悄悄停住、
 * 界面永远转圈。用户等三分钟然后关掉页面，我们连一条日志都拿不到。
 */
function watchStall(video: HTMLVideoElement, onFailed: (reason: string) => void): () => void {
  let lastTime = video.currentTime;
  let stalledFor = 0;
  const timer = window.setInterval(() => {
    if (video.paused || video.ended || video.seeking) {
      stalledFor = 0;
      lastTime = video.currentTime;
      return;
    }
    if (video.currentTime > lastTime) {
      stalledFor = 0;
      lastTime = video.currentTime;
      return;
    }
    stalledFor += 1;
    if (stalledFor >= STALL_TIMEOUT_S) {
      stalledFor = 0;
      onFailed(`播放停滞超过 ${STALL_TIMEOUT_S} 秒`);
    }
  }, 1000);
  return () => window.clearInterval(timer);
}

/** 把 `<video>` 的 error 码翻成中文——用户报障时这句话就是全部线索。 */
function describeMediaError(video: HTMLVideoElement): string {
  switch (video.error?.code) {
    case MediaError.MEDIA_ERR_ABORTED:
      return "播放被中断";
    case MediaError.MEDIA_ERR_NETWORK:
      return "取流中断，网络或服务端连接断开";
    case MediaError.MEDIA_ERR_DECODE:
      return "解码失败，这一档的码流浏览器吃不下";
    case MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED:
      return "浏览器不支持这个格式或编码";
    default:
      return "播放失败";
  }
}

/** 档 0 与原生 HLS 共用：直接把地址交给 `<video src>`。 */
class DirectEngine implements PlaybackEngine {
  private stopStallWatch: (() => void) | null = null;
  private readonly onErrorEvent = () => this.options.onFailed(describeMediaError(this.options.video));

  constructor(
    private readonly options: EngineOptions,
    private readonly label: "direct" | "native-hls",
  ) {}

  async attach(): Promise<void> {
    const { video, streamUrl, onFailed } = this.options;
    video.addEventListener("error", this.onErrorEvent);
    video.src = streamUrl;
    video.load();
    this.stopStallWatch = watchStall(video, onFailed);
  }

  destroy(): void {
    this.stopStallWatch?.();
    this.stopStallWatch = null;
    this.options.video.removeEventListener("error", this.onErrorEvent);
    // removeAttribute + load()：只置空 src 会让部分浏览器继续持有连接，
    // 表现是切集后旧会话的取流不断开、服务端并发额度被占着不放。
    this.options.video.removeAttribute("src");
    this.options.video.load();
  }

  stats(): EngineStats {
    return { engine: this.label, bitrate: null, ...readCommonStats(this.options.video) };
  }
}

/** 档 1–4：hls.js 喂 fMP4 分片。 */
class HlsEngine implements PlaybackEngine {
  private hls: Hls | null = null;
  private stopStallWatch: (() => void) | null = null;
  private currentBitrate: number | null = null;

  constructor(private readonly options: EngineOptions) {}

  async attach(): Promise<void> {
    const { video, streamUrl, onFailed } = this.options;
    const { default: HlsCtor } = await import("hls.js");
    this.hls = new HlsCtor({
      // 已播缓冲回收，见 BACK_BUFFER_S
      backBufferLength: BACK_BUFFER_S,
      // 转码会话的 playlist 是 EVENT 类型、只增不改，边转边给。低延迟模式
      // 的那套 part 级请求在这里没有意义，只会多打服务端。
      lowLatencyMode: false,
      enableWorker: true,
    });

    this.hls.on(HlsCtor.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;
      // 网络类致命错误先就地重试一次：转码会话是边转边给的，客户端偶尔会
      // 抢在分片写完之前拉到 404，这类不该触发降档（降了也一样）。
      if (data.type === HlsCtor.ErrorTypes.NETWORK_ERROR) {
        this.hls?.startLoad();
        return;
      }
      if (data.type === HlsCtor.ErrorTypes.MEDIA_ERROR) {
        onFailed(`码流解码失败（${data.details}）`);
        return;
      }
      onFailed(`播放失败（${data.details}）`);
    });
    this.hls.on(HlsCtor.Events.LEVEL_SWITCHED, () => {
      this.currentBitrate = this.hls?.levels[this.hls.currentLevel]?.bitrate ?? null;
    });

    this.hls.loadSource(streamUrl);
    this.hls.attachMedia(video);
    this.stopStallWatch = watchStall(video, onFailed);
  }

  destroy(): void {
    this.stopStallWatch?.();
    this.stopStallWatch = null;
    this.hls?.destroy();
    this.hls = null;
  }

  stats(): EngineStats {
    return {
      engine: "hls.js",
      bitrate: this.currentBitrate,
      ...readCommonStats(this.options.video),
    };
  }
}

/**
 * 按计划挑引擎。
 *
 * iOS（只有 ManagedMediaSource）走系统原生 HLS——§6.4 定下的取舍：iOS 上
 * 原生全屏强制用系统控件，自定义 UI 在全屏时会整个消失，硬撑的结果是投入
 * 巨大且永远差一口气。
 */
export function createEngine(options: EngineOptions): PlaybackEngine {
  if (options.container !== "hls-fmp4") return new DirectEngine(options, "direct");
  if (options.hasFullMse) return new HlsEngine(options);
  return new DirectEngine(options, "native-hls");
}
