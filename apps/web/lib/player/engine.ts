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

import type { MseKind } from "@/lib/api/playback";
import { reportPlaybackClientLog } from "@/lib/api/playback";

import { NUDGE_STEP_S, bufferedAhead, classifyStall, shouldNudge, stallReason } from "./stall";

/** 把 `<video>` 的当下状态压成一行上报（排障用，字段都很小）。 */
function videoSnapshot(video: HTMLVideoElement): Record<string, unknown> {
  const buffered: string[] = [];
  for (let i = 0; i < video.buffered.length; i += 1) {
    buffered.push(`${video.buffered.start(i).toFixed(1)}-${video.buffered.end(i).toFixed(1)}`);
  }
  return {
    error_code: video.error?.code ?? null,
    error_message: video.error?.message ?? null,
    current_time: Number(video.currentTime.toFixed(2)),
    ready_state: video.readyState,
    network_state: video.networkState,
    paused: video.paused,
    buffered: buffered.join(","),
    src_tail: video.currentSrc.slice(-80),
  };
}

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
  /** HTMLMediaElement 的实时状态：定位「有缓冲但解码没推进」很有用。 */
  currentTimeSeconds: number;
  readyState: number;
  networkState: number;
  paused: boolean;
  seeking: boolean;
}

export interface PlaybackEngine {
  attach(): Promise<void>;
  destroy(): void;
  /** 在 MSE 流内跳转时，允许引擎取消旧分片并从目标时间重新装载。 */
  seek?(seconds: number): void;
  stats(): EngineStats;
}

export interface EngineOptions {
  video: HTMLVideoElement;
  streamUrl: string;
  /** "mp4" = 原文件直出；"hls-fmp4" = 转码会话的分片 */
  container: string;
  /** 客户端有没有 MSE；managed 是 iOS 的 ManagedMediaSource */
  hasMse: boolean;
  /** MSE 的具体形态，用于让 hls.js 在 iOS 明确选择 ManagedMediaSource */
  mse?: MseKind;
  /** 走系统原生 HLS（仅无 MSE 的旧设备），streamUrl 应传 master 列表 */
  preferNativeHls?: boolean;
  /**
   * 首帧起播位置（**列表时间轴**的秒数）。
   *
   * 会话相对制的流从 0 起，传 0（或不传）；VOD 全片列表（timeline="file"）
   * 必须传续播点——hls.js 不知道的话会从列表头装载，在上层 seek 落地之前就
   * 把第 0 段的请求发出去。服务端按需供片会把这个杂散请求当成「seek 回开头」，
   * 杀掉刚在续播点起好的转码从头重启，续播直接变成从零转。
   */
  startPositionS?: number;
  /**
   * 播放链路失败：error 事件或长时间无进展。**这是降档回路的唯一入口**——
   * 「看起来 codec 兼容、实际 copy 出来是坏流」的源片穷举不完（MKV header
   * compression、参数集只在 CodecPrivate、开放 GOP……），只能靠这条回路兜。
   */
  onFailed: (reason: string) => void;
  /**
   * 取流**持续**失败（连续多次网络类致命错误、期间没有任何一个分片成功）。
   *
   * 与 `onFailed` 分开是语义问题：网络断/token 过期不是「这一档播不了」，
   * 走降档回路会白白把画质降下去还修不好。正确处置是**同档位原地重开会话**
   * （重开会签发新取流 token），由上层接线。不传则退回 `onFailed`。
   */
  onNetworkDead?: (reason: string) => void;
}

/**
 * 回收多少秒的已播缓冲（hls.js `backBufferLength`）。
 *
 * 不回收的话，一部三小时的片子会把已播分片一路堆在 SourceBuffer 里，吃掉
 * 几个 G 内存然后整个标签页崩掉——长片播放最典型的一种"放到一半就没了"。
 */
const BACK_BUFFER_S = 30;

/** 掉帧与缓冲读数：三种引擎共用一份取法。 */
function readCommonStats(video: HTMLVideoElement): Omit<EngineStats, "engine" | "bitrate"> {
  const quality = video.getVideoPlaybackQuality?.();
  // Safari 老前缀回退：标准 getVideoPlaybackQuality 在部分 WebKit 上缺失或
  // 返回全零，但 webkitDroppedFrameCount / webkitDecodedFrameCount 一直在。
  // 这两个计数不喂的话，掉帧 watchdog 在 iOS（恰恰是 smooth 误报最重、最
  // 需要真实证据的平台）就是哑的。decodedFrameCount 语义≈totalVideoFrames
  // （解码数 vs 应显示数），对「窗口掉帧率」这个用途足够等价。
  const legacy = video as HTMLVideoElement & {
    webkitDroppedFrameCount?: number;
    webkitDecodedFrameCount?: number;
  };
  return {
    bufferedSeconds: bufferedAhead(video),
    droppedFrames: quality?.droppedVideoFrames ?? legacy.webkitDroppedFrameCount ?? null,
    totalFrames: quality?.totalVideoFrames ?? legacy.webkitDecodedFrameCount ?? null,
    currentTimeSeconds: video.currentTime,
    readyState: video.readyState,
    networkState: video.networkState,
    paused: video.paused,
    seeking: video.seeking,
  };
}

/**
 * 卡死看门狗：播放中 currentTime 长时间不动就当作失败。
 *
 * 只看 `error` 事件是不够的——坏流最常见的表现不是报错，而是解码器悄悄停住、
 * 界面永远转圈。用户等三分钟然后关掉页面，我们连一条日志都拿不到。
 *
 * 归因交给 `classifyStall`：解码卡死与「追上了编码器」的正确处置完全相反，
 * 混为一谈会把「服务器转得慢」误判成「这一档播不了」而白白降档。
 */
function watchStall(
  video: HTMLVideoElement,
  onFailed: (reason: string) => void,
  onNudge?: (attempt: number) => void,
): () => void {
  let lastTime = video.currentTime;
  let stalledFor = 0;
  let nudges = 0;
  let sinceNudge = 99;
  let everAdvanced = false;
  const timer = window.setInterval(() => {
    const advanced = video.currentTime > lastTime;
    // 「真正播起来过」只认小步前进：起播 jsseek / 用户拖动是一次大跳，
    // 不算播放推进——它击穿预滚闸的话，推动又会回到打断起播的老路
    if (advanced && !video.seeking && video.currentTime - lastTime < 5) {
      everAdvanced = true;
    }
    sinceNudge += 1;
    const verdict = classifyStall({
      paused: video.paused,
      ended: video.ended,
      seeking: video.seeking,
      advanced,
      bufferedAhead: bufferedAhead(video),
      stalledFor: stalledFor + 1,
    });
    lastTime = video.currentTime;
    if (video.paused || video.ended || video.seeking || advanced) {
      stalledFor = 0;
      // 只有远离上次推动的真实前进才算恢复——推动自己造成的 currentTime
      // 变化 / seeking 不作数，否则每 3 秒推一次、永远到不了判死
      if (advanced && sinceNudge > 3) nudges = 0;
      return;
    }
    stalledFor += 1;
    if (verdict !== "ok") {
      stalledFor = 0;
      nudges = 0;
      onFailed(stallReason(verdict));
      return;
    }
    // 有数据却不动：先推一把（见 stall.ts shouldNudge 的 iOS wedge 注释），
    // 推不动再让 classifyStall 走到 decode-stalled 降档
    if (
      shouldNudge({
        stalledFor,
        nudges,
        bufferedAhead: bufferedAhead(video),
        readyState: video.readyState,
        everAdvanced,
      })
    ) {
      nudges += 1;
      sinceNudge = 0;
      stalledFor = 0;
      onNudge?.(nudges);
      video.currentTime = video.currentTime + NUDGE_STEP_S;
      void video.play().catch(() => undefined);
      lastTime = video.currentTime;
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
  private readonly onErrorEvent = () => {
    // 报错瞬间的客户端现场进服务端日志：iPhone 上没有控制台可看，
    // MediaError 的 code/message 与播放器状态只有这里能拿到
    reportPlaybackClientLog(`${this.label}-media-error`, videoSnapshot(this.options.video));
    this.options.onFailed(describeMediaError(this.options.video));
  };
  private onMetadataSeek: (() => void) | null = null;

  constructor(
    private readonly options: EngineOptions,
    private readonly label: "direct" | "native-hls",
  ) {}

  async attach(): Promise<void> {
    const { video, streamUrl, onFailed, startPositionS } = this.options;
    video.addEventListener("error", this.onErrorEvent);
    // 起播点的两条路（2026-08-25 真机结论，iPhone 逐场景实测）：
    // - 直出 mp4：媒体片段（#t=）可用且零副作用，保留；
    // - 原生 HLS：**#t= 在 iOS 上对 HLS 列表不生效**——AVPlayer 会拉对
    //   起播点所在的分片，却永远不进入播放，几秒后报笼统的解码错误
    //   （冷加载都如此），必须改回 loadedmetadata 后 JS seek。
    //   代价是 seek 落地前 AVPlayer 会探测列表头（实测狂打 seg00000），
    //   这些杂散请求由服务端的重启宽限期挡住（_maybe_restart_for），
    //   不再劫持转码器回第 0 段。
    if (this.label === "native-hls" && startPositionS && startPositionS > 1) {
      video.src = streamUrl;
      this.onMetadataSeek = () => {
        video.currentTime = startPositionS;
      };
      video.addEventListener("loadedmetadata", this.onMetadataSeek, { once: true });
      // 挂流路径进服务端日志：与 media-error 对照，可确证客户端跑的是
      // 哪个版本的代码、走的哪条起播路径
      reportPlaybackClientLog("native-attach-jsseek", { start_s: startPositionS });
    } else {
      video.src =
        startPositionS && startPositionS > 1 ? `${streamUrl}#t=${startPositionS}` : streamUrl;
    }
    video.load();
    this.stopStallWatch = watchStall(
      video,
      (reason) => {
        // 停滞判死同样要留客户端现场：它与真 MediaError 的处置完全不同
        reportPlaybackClientLog(`${this.label}-stall`, {
          reason,
          ...videoSnapshot(video),
        });
        onFailed(reason);
      },
      (attempt) => {
        // 推动也留痕：日志里「nudge 后恢复」与「nudge 无效判死」是两种病
        reportPlaybackClientLog(`${this.label}-nudge`, {
          attempt,
          ...videoSnapshot(video),
        });
      },
    );
  }

  destroy(): void {
    this.stopStallWatch?.();
    this.stopStallWatch = null;
    this.options.video.removeEventListener("error", this.onErrorEvent);
    if (this.onMetadataSeek) {
      this.options.video.removeEventListener("loadedmetadata", this.onMetadataSeek);
      this.onMetadataSeek = null;
    }
    // removeAttribute + load()：只置空 src 会让部分浏览器继续持有连接，
    // 表现是切集后旧会话的取流不断开、服务端并发额度被占着不放。
    this.options.video.removeAttribute("src");
    this.options.video.load();
  }

  stats(): EngineStats {
    return { engine: this.label, bitrate: null, ...readCommonStats(this.options.video) };
  }
}

/**
 * 连续多少次网络类致命错误后放弃原地重试。
 *
 * 每次致命错误前 hls.js 已按 fragLoadPolicy 自行重试过好几轮，走到这里的
 * 都是持续性故障（token 过期、服务端掉了、断网）。没有这个上限时是无限
 * `startLoad()` 循环：播放器永远安静地转圈，用户和日志都得不到任何信号。
 */
const MAX_NETWORK_RECOVERIES = 4;

/** 档 1–4：hls.js 喂 fMP4 分片。 */
class HlsEngine implements PlaybackEngine {
  private hls: Hls | null = null;
  private stopStallWatch: (() => void) | null = null;
  private currentBitrate: number | null = null;
  /** 连续网络恢复计数；任何一个分片成功落地就清零 */
  private networkRecoveries = 0;

  constructor(private readonly options: EngineOptions) {}

  async attach(): Promise<void> {
    const { video, streamUrl, onFailed } = this.options;
    const { default: HlsCtor } = await import("hls.js");
    this.hls = new HlsCtor({
      // 已播缓冲回收，见 BACK_BUFFER_S
      backBufferLength: BACK_BUFFER_S,
      // 前向缓冲拉到 60 秒（hls.js 默认 30）：局域网抢先缓、弱网抗抖动都
      // 受益。服务端 readrate 1.5 倍限速决定了缓冲天然追不过这个数太多。
      maxBufferLength: 60,
      // 转码会话的 playlist 是 EVENT 类型、只增不改，边转边给。低延迟模式
      // 的那套 part 级请求在这里没有意义，只会多打服务端。
      lowLatencyMode: false,
      // 分片请求超时要**盖过服务端的按需供片等待**（ensure_segment 最长挂
      // 请求 30 秒等转码追上来）：默认 20 秒会在服务端即将给出分片前把请求
      // 掐掉重发，慢转码场景下反复空转。120s 的 maxLoadTimeMs 是 hls.js 对
      // 单次加载的总上限，照默认。
      fragLoadPolicy: {
        default: {
          maxTimeToFirstByteMs: 45_000,
          maxLoadTimeMs: 120_000,
          timeoutRetry: { maxNumRetry: 4, retryDelayMs: 0, maxRetryDelayMs: 0 },
          errorRetry: { maxNumRetry: 6, retryDelayMs: 1_000, maxRetryDelayMs: 8_000 },
        },
      },
      // iOS 只暴露 ManagedMediaSource 时显式选它；hls.js 在没有完整
      // MediaSource 的浏览器也会自动回退，但这里把能力快照传进来能避免
      // 同时暴露两种实现的 WebKit 版本选错后端。
      preferManagedMediaSource: this.options.mse === "managed",
      // 必须显式给起播位置，不能留默认(-1)：EVENT playlist 没有 ENDLIST，
      // hls.js 把它当直播，默认从「直播边缘」起播——续播时 ffmpeg 已 burst
      // 转出几十秒，就会从几十秒处开播。会话相对制传 0（续播位置由服务端
      // -ss 决定，流头即续播点）；VOD 全片列表传文件内的续播秒数（见
      // EngineOptions.startPositionS 的注释——传 0 会造成杂散的第 0 段请求）。
      startPosition: this.options.startPositionS ?? 0,
      enableWorker: true,
    });

    this.hls.on(HlsCtor.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;
      // 网络类致命错误先就地重试：转码会话是边转边给的，客户端偶尔会抢在
      // 分片写完之前拉到 404，这类不该触发降档（降了也一样）。但重试必须有
      // 上限——token 过期、服务端掉线这类持续故障靠 startLoad 永远修不好，
      // 交给 onNetworkDead 原地重开会话（新会话 = 新 token）。
      if (data.type === HlsCtor.ErrorTypes.NETWORK_ERROR) {
        this.networkRecoveries += 1;
        if (this.networkRecoveries <= MAX_NETWORK_RECOVERIES) {
          this.hls?.startLoad();
          return;
        }
        const reason = `取流持续失败（${data.details}），已连续重试 ${MAX_NETWORK_RECOVERIES} 次`;
        (this.options.onNetworkDead ?? onFailed)(reason);
        return;
      }
      if (data.type === HlsCtor.ErrorTypes.MEDIA_ERROR) {
        onFailed(`码流解码失败（${data.details}）`);
        return;
      }
      onFailed(`播放失败（${data.details}）`);
    });
    // 任何一个分片成功到手都说明链路是通的，连续失败计数从头数
    this.hls.on(HlsCtor.Events.FRAG_LOADED, () => {
      this.networkRecoveries = 0;
    });
    this.hls.on(HlsCtor.Events.LEVEL_SWITCHED, () => {
      this.currentBitrate = this.hls?.levels[this.hls.currentLevel]?.bitrate ?? null;
    });

    this.hls.loadSource(streamUrl);
    this.hls.attachMedia(video);
    this.stopStallWatch = watchStall(video, onFailed);
  }

  seek(seconds: number): void {
    const target = Math.max(0, seconds);
    // 先更新 media.currentTime，让 hls.js 的 seeking 处理器同步丢弃旧
    // fragment；再显式 stop/start，确保正在服务端等待的旧分片请求也被取消。
    this.options.video.currentTime = target;
    this.hls?.stopLoad();
    this.hls?.startLoad(target, true);
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
 * 按计划挑引擎。支持 ManagedMediaSource 的 iOS 也走 hls.js；只有没有
 * MSE 的老设备才把 HLS 交给系统原生播放器。
 */
/**
 * 起播热身：把 hls.js 的动态包在**会话请求在途时**就开始下载解析（§6.10）。
 * 不热身的话 import 要等会话响应回来才发起，弱网上白排一跳。完全没有
 * MSE 的原生 HLS 路径用不上它，不下。
 */
export function preloadHlsEngine(): void {
  if (typeof window === "undefined") return;
  const w = window as unknown as { MediaSource?: unknown; ManagedMediaSource?: unknown };
  if (typeof w.MediaSource === "undefined" && typeof w.ManagedMediaSource === "undefined") {
    return;
  }
  void import("hls.js").catch(() => undefined);
}

export function createEngine(options: EngineOptions): PlaybackEngine {
  if (options.container !== "hls-fmp4") return new DirectEngine(options, "direct");
  // 无 MSE 的设备才回归系统原生 HLS（AVPlayer）。现代 iOS 的
  // ManagedMediaSource 由上层模式矩阵选 hls.js，避免原生 AVPlayer 对按需
  // VOD 清单的严格限制。
  if (options.preferNativeHls) return new DirectEngine(options, "native-hls");
  if (options.hasMse) return new HlsEngine(options);
  return new DirectEngine(options, "native-hls");
}
