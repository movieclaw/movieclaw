/**
 * 客户端解码能力探测（docs/design/web-player.md §3.1）。
 *
 * **不用 `canPlayType()`**：它只返回 ""/"maybe"/"probably"，分不清「能解码」
 * 和「能流畅解码」——4K HEVC 在没有硬解的老笔记本上它会说 probably，实际
 * 掉帧到 5fps。这里一律走 `navigator.mediaCapabilities.decodingInfo()`，
 * 把 supported / smooth / powerEfficient 三态原样带给服务端。
 *
 * 两处必须注意的翻译：
 *
 * 1. **codec 名要归一到家族名**。浏览器探测必须喂 RFC 6381 全串
 *    （`avc1.640028`），而服务端要拿结果和 ffprobe 落库的 `codec_name`
 *    （`h264`）比对。所以探测用全串、上报用家族名，映射在本模块内闭合。
 * 2. **max_height 要探出来而不是猜**。同一个 codec 在 1080p 流畅、4K 掉帧
 *    是最常见的情况，只报「支持 HEVC」会让服务端把 4K 直通给一台放不动的机器。
 *
 * ⚠️ 已知偏差（两个方向都有，§12.15 Jellyfin 对照）：Chrome 在没有本机统计
 * 前会把 supported 的配置乐观报成 smooth=true；Safari 则会对 HEVC 整族悲观
 * 报 smooth=false，而同一台设备直通同一文件 0 掉帧。所以 smooth /
 * powerEfficient **只作参考随快照上报**（诊断与指标用），服务端决策只认
 * supported；流畅与否交给运行期证据——真掉帧由 framedrop watchdog 降档。
 */

import type { ClientCapability, MseKind } from "@/lib/api/playback";

/** 快照结构版本。改了探测矩阵或字段含义就 +1，旧缓存自动失效。 */
// 2：快照加了 native_hls 字段（选原生 HLS 引擎用）。老缓存没有它，
// iPhone 会误走 hls.js 路径——版本不同即作废重探。
// 3：max_height 语义从「流畅上限」改为「支持上限」（smooth 降为参考）。
// 老缓存里被 smooth=false 压低过的上限会让服务端继续错误转码，必须重探。
// 4：ManagedMediaSource 优先于 MediaSource，确保能力快照与 hls.js 实际后端一致。
export const CAPABILITY_SCHEMA_VERSION = 4;

const CACHE_KEY = "movieclaw.player.capability";

/** 探测高度，从高到低——取「能流畅解到的最大高度」。 */
const PROBE_HEIGHTS = [2160, 1440, 1080, 720] as const;

/**
 * 视频探测矩阵：家族名 → 各高度用的 RFC 6381 串。
 *
 * 4K 一档特意用 Main 10 profile（`hvc1.2.4.L153.B0` / `avc1.640033`）：真实的
 * 4K 片源几乎都是 10bit，用 8bit 串探出来的结论对不上要播的东西。
 *
 * VC-1 与 MPEG-2 不在矩阵里：没有任何浏览器能解，探了也只会拿到 false，
 * 白白多四次 async 调用。它们在服务端一律走转码。
 */
const VIDEO_MATRIX: Record<string, (height: number) => string> = {
  h264: (height) => (height >= 2160 ? "avc1.640033" : "avc1.640028"),
  hevc: (height) => (height >= 2160 ? "hvc1.2.4.L153.B0" : "hvc1.1.6.L120.90"),
  av1: (height) => (height >= 2160 ? "av01.0.12M.10" : "av01.0.08M.08"),
  vp9: (height) => (height >= 2160 ? "vp09.02.51.10" : "vp09.00.41.08"),
};

/**
 * 音频探测矩阵：家族名 → RFC 6381 串 + 要探的声道数。
 *
 * 声道数是硬约束而不是附注：Chrome 能解 AAC，但输出设备只有立体声时 5.1 轨
 * 仍需降混，而降混是音频转码（档 2）不是直通。DTS / TrueHD 不探——没有浏览器
 * 能解，服务端一律转码。
 */
const AUDIO_MATRIX: Record<string, string> = {
  aac: "mp4a.40.2",
  ac3: "ac-3",
  eac3: "ec-3",
  opus: "opus",
  flac: "flac",
  mp3: "mp4a.69",
};

/** 声道数从多到少：取能直通的最大声道数。 */
const PROBE_CHANNELS = [8, 6, 2] as const;

/** decodingInfo 的返回三态，抽出来是为了让纯函数部分能脱离浏览器测试。 */
export interface DecodeProbe {
  supported: boolean;
  smooth: boolean;
  powerEfficient: boolean;
}

export type ProbeFn = (config: {
  kind: "video" | "audio";
  contentType: string;
  height?: number;
  channels?: number;
}) => Promise<DecodeProbe>;

/**
 * 从一组「高度 → 探测结果」里挑出该编码家族的上报值。
 *
 * 规则（与服务端 `_judge_video` 的消费方式对应，§12.15）：
 * - 一个高度都不支持 → 返回 null，该家族**整个不出现在快照里**（服务端据此转码）；
 * - 否则取**最大的支持高度**。smooth / powerEfficient 取该高度探测的原值，
 *   只作参考随行——**不再拿 smooth 压低上限**：Safari 对 HEVC 整族报
 *   smooth=false 而实际直通 0 掉帧，压低上限等于替一台放得动的设备转码。
 *   真掉帧由运行期 framedrop watchdog 兜底。
 */
export function pickVideoSupport(
  probes: { height: number; probe: DecodeProbe }[],
): { max_height: number; smooth: boolean; power_efficient: boolean } | null {
  const supported = probes.filter((p) => p.probe.supported);
  if (supported.length === 0) return null;
  const pick = supported.reduce((best, current) =>
    current.height > best.height ? current : best,
  );
  return {
    max_height: pick.height,
    smooth: pick.probe.smooth,
    power_efficient: pick.probe.powerEfficient,
  };
}

/** 音频只关心「最多能直通几个声道」；一个都不支持返回 null。 */
export function pickAudioChannels(
  probes: { channels: number; probe: DecodeProbe }[],
): number | null {
  const supported = probes.filter((p) => p.probe.supported);
  if (supported.length === 0) return null;
  return supported.reduce((best, current) =>
    current.channels > best.channels ? current : best,
  ).channels;
}

/** 环境事实：MSE 形态、HDR 直出、是否移动端。浏览器相关，注入以便测试。 */
export interface PlaybackEnvironment {
  mse: MseKind;
  hdr_passthrough: boolean;
  is_mobile: boolean;
  native_hls: boolean;
}

/**
 * 把浏览器暴露的 MSE 构造器归一成三态。
 *
 * ManagedMediaSource 要优先于 MediaSource：部分较新的 WebKit 版本可能同时
 * 暴露两者，但 hls.js 默认也会优先选择 ManagedMediaSource。能力快照必须和
 * 实际引擎一致，否则服务端会记成 full，播放器却可能落到另一条 WebKit 实现。
 */
export function detectMseKind(environment: {
  MediaSource?: unknown;
  ManagedMediaSource?: unknown;
}): MseKind {
  if (environment.ManagedMediaSource) return "managed";
  if (environment.MediaSource) return "full";
  return "none";
}

/**
 * 跑完整个探测矩阵，产出可直接上送的能力快照。
 *
 * 纯函数（探测与环境都由参数注入），因此能在 node --test 里覆盖到「4K 掉帧
 * 应降到 1080p」这类判断，不需要真浏览器。
 */
export async function buildCapabilitySnapshot(
  probe: ProbeFn,
  env: PlaybackEnvironment,
): Promise<ClientCapability> {
  const video: ClientCapability["video"] = [];
  for (const [family, contentTypeFor] of Object.entries(VIDEO_MATRIX)) {
    const probes = [];
    for (const height of PROBE_HEIGHTS) {
      probes.push({
        height,
        probe: await probe({
          kind: "video",
          contentType: `video/mp4; codecs="${contentTypeFor(height)}"`,
          height,
        }),
      });
    }
    const picked = pickVideoSupport(probes);
    if (picked) video.push({ codec: family, ...picked });
  }

  const audio: ClientCapability["audio"] = [];
  for (const [family, contentType] of Object.entries(AUDIO_MATRIX)) {
    const probes = [];
    for (const channels of PROBE_CHANNELS) {
      probes.push({
        channels,
        probe: await probe({
          kind: "audio",
          contentType: `audio/mp4; codecs="${contentType}"`,
          channels,
        }),
      });
    }
    const maxChannels = pickAudioChannels(probes);
    if (maxChannels !== null) audio.push({ codec: family, max_channels: maxChannels });
  }

  // "mp4" 恒成立：任何浏览器都能 <video src> 拉一个 MP4。"hls-fmp4" 要么靠
  // MSE（hls.js 喂分片），要么靠原生 HLS（Safari）——两者都没有的客户端
  // 只能走档 0，服务端会据此判定。
  const containers = ["mp4"];
  if (env.mse !== "none" || env.native_hls) containers.push("hls-fmp4");

  return {
    video,
    audio,
    containers,
    hdr_passthrough: env.hdr_passthrough,
    mse: env.mse,
    is_mobile: env.is_mobile,
    native_hls: env.native_hls,
  };
}

// ---------------------------------------------------------------------------
// 浏览器侧：真实探测 + 缓存
// ---------------------------------------------------------------------------

/** 探测配置里的码率/帧率：给 decodingInfo 一组有代表性的真实负载。 */
function bitrateFor(height: number): number {
  if (height >= 2160) return 25_000_000;
  if (height >= 1440) return 14_000_000;
  if (height >= 1080) return 8_000_000;
  return 4_000_000;
}

function browserProbe(mseAvailable: boolean): ProbeFn {
  return async ({ kind, contentType, height, channels }) => {
    const unsupported = { supported: false, smooth: false, powerEfficient: false };
    const capabilities = navigator.mediaCapabilities;
    if (!capabilities?.decodingInfo) return unsupported;
    // type 选 media-source 还是 file：档 1–4 都经 MSE 喂分片，档 0 是整文件
    // 直出。没有 MSE 的客户端（iOS 原生 HLS）只可能走 file 语义，用它探。
    const type = mseAvailable ? "media-source" : "file";
    try {
      const result = await capabilities.decodingInfo(
        kind === "video"
          ? {
              type,
              video: {
                contentType,
                width: Math.round(((height ?? 1080) * 16) / 9),
                height: height ?? 1080,
                bitrate: bitrateFor(height ?? 1080),
                framerate: 24,
              },
            }
          : {
              type,
              audio: { contentType, channels: String(channels ?? 2) },
            },
      );
      return {
        supported: result.supported,
        smooth: result.smooth,
        powerEfficient: result.powerEfficient,
      };
    } catch {
      // 非法的 codec 串会抛 TypeError（浏览器之间对「非法」的判定并不一致），
      // 语义上等同于不支持。
      return unsupported;
    }
  };
}

function readEnvironment(): PlaybackEnvironment {
  const w = window as unknown as {
    MediaSource?: unknown;
    ManagedMediaSource?: unknown;
  };
  // iOS 17+ 只给 ManagedMediaSource（MSE 的子集），要单独识别：现代 iOS
  // 交给 hls.js，只有完全没有 MSE 的老设备才交给系统原生 HLS（§6.4）。
  const mse = detectMseKind(w);
  const probeVideo = document.createElement("video");
  const nativeHls =
    probeVideo.canPlayType("application/vnd.apple.mpegurl") !== "";
  return {
    mse,
    // 屏幕与浏览器都得支持才算能 HDR 直出；判 false 只是让服务端 tone-map，
    // 代价是一次转码，比把 HDR 直接甩给 SDR 屏幕（画面灰暗发白）好。
    hdr_passthrough: window.matchMedia("(dynamic-range: high)").matches,
    is_mobile:
      window.matchMedia("(pointer: coarse)").matches &&
      window.matchMedia("(hover: none)").matches,
    native_hls: nativeHls,
  };
}

interface CachedSnapshot {
  version: number;
  uaHash: string;
  probedAt: string;
  snapshot: ClientCapability;
}

/**
 * UA 串的 FNV-1a 哈希。缓存键带上它，浏览器一升级（UA 里的版本号变了）
 * 快照就自动失效——新版本可能新增了 codec 支持，拿旧快照会一直多转一路。
 */
function hashUserAgent(ua: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < ua.length; i += 1) {
    hash ^= ua.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16);
}

function readCache(uaHash: string): ClientCapability | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as CachedSnapshot;
    if (cached.version !== CAPABILITY_SCHEMA_VERSION || cached.uaHash !== uaHash) {
      return null;
    }
    return cached.snapshot;
  } catch {
    // 存储被禁用（隐私模式）或内容损坏：当作没有缓存，重新探一次即可
    return null;
  }
}

let inflight: Promise<ClientCapability> | null = null;

/**
 * 取当前浏览器的能力快照：优先读缓存，没有就跑一遍完整探测并写回。
 *
 * 整个矩阵是几十次 async 调用，同一页面里若干处（起播、诊断面板、降档重来）
 * 都要用，因此做进程内去重——并发调用共享同一次探测。
 */
export async function getCapabilitySnapshot(): Promise<ClientCapability> {
  const uaHash = hashUserAgent(navigator.userAgent);
  const cached = readCache(uaHash);
  if (cached) return cached;
  if (inflight) return inflight;

  inflight = (async () => {
    const env = readEnvironment();
    const snapshot = await buildCapabilitySnapshot(browserProbe(env.mse !== "none"), env);
    try {
      window.localStorage.setItem(
        CACHE_KEY,
        JSON.stringify({
          version: CAPABILITY_SCHEMA_VERSION,
          uaHash,
          probedAt: new Date().toISOString(),
          snapshot,
        } satisfies CachedSnapshot),
      );
    } catch {
      // 写不进去不影响本次播放，只是下次还要再探一遍
    }
    return snapshot;
  })();

  try {
    return await inflight;
  } finally {
    inflight = null;
  }
}

/** 诊断面板的「重新探测」：清缓存，下次起播重新跑矩阵。 */
export function clearCapabilityCache(): void {
  try {
    window.localStorage.removeItem(CACHE_KEY);
  } catch {
    // 同上：存储不可用时本来也没有缓存
  }
}
