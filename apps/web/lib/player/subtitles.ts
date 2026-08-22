/**
 * 字幕轨规划：服务端给的 SubtitlePlan → 前端真能渲染的选项（§6.2）。
 *
 * 字幕**永远旁挂、绝不烧录**（硬边界 1）——烧录会把任何档位瞬间拖进全转码。
 * 因此这里只做"用哪条渲染路径"的分派：
 *
 * | 类型 | 路径 |
 * |---|---|
 * | SRT / mov_text | 服务端转 VTT，`<track>` 原生渲染 |
 * | ASS / SSA | 原样下发 + JASSUB（libass WASM），保留特效与排版 |
 * | PGS | 位图轨，P0 不渲染 |
 *
 * **当前后端只服务外挂字幕**（`resolve_external_subtitle` 只认
 * `external:<文件名>`，内封轨一律 404）。内封轨因此进"不可用"清单并给出
 * 原因，而不是给一个点了没反应的选项——番剧的内封 ASS 是最常见的情况，
 * 用户点了没反应会以为播放器坏了。
 */

import type { SubtitlePlan } from "@/lib/api/playback";
// 相对路径而不是 @/ 别名：本模块进 node --test，而 Node 的类型擦除只会抹掉
// `import type`，值导入会照原样解析——别名在测试进程里没有解析器。
import { languageLabel } from "../language-labels.ts";

/** 一条能渲染的字幕轨。 */
export interface SubtitleOption {
  /** 中性轨引用，同时用作 React key 与轨记忆的值 */
  ref: string;
  label: string;
  /** vtt 交 `<track>`，ass 交 JASSUB */
  kind: "vtt" | "ass";
  /** 已带签名 token 的下载地址 */
  url: string;
  language: string | null;
  isDefault: boolean;
}

/** 一条拿不到的字幕轨，连同面向用户的中文原因。 */
export interface UnavailableSubtitle {
  label: string;
  reason: string;
}

export interface SubtitleTracks {
  options: SubtitleOption[];
  unavailable: UnavailableSubtitle[];
}

const KIND_LABELS: Record<string, string> = {
  vtt: "文本",
  ass: "特效",
  pgs: "图形",
};

function trackLabel(plan: SubtitlePlan): string {
  const language = languageLabel(plan.language);
  const kind = KIND_LABELS[plan.kind] ?? plan.kind;
  const name = language ?? fileNameOf(plan.track_ref) ?? "未知语言";
  return `${name} · ${kind}`;
}

/** external:<文件名> → 文件名；不是外挂引用返回 null。 */
function fileNameOf(ref: string): string | null {
  return ref.startsWith("external:") ? ref.slice("external:".length) : null;
}

/**
 * 把决策里的字幕计划配上取流地址，分成"能渲染"和"拿不到"两堆。
 *
 * `urls` 是开会话接口返回的 `subtitle_urls`，与 `plans` 严格一一对应
 * （服务端按同一个列表生成）。少一个就当那条轨没有地址，不做位移匹配——
 * 错位下发的字幕比没有字幕更难排查。
 */
export function planSubtitleTracks(
  plans: SubtitlePlan[],
  urls: string[],
): SubtitleTracks {
  const options: SubtitleOption[] = [];
  const unavailable: UnavailableSubtitle[] = [];

  plans.forEach((plan, index) => {
    const url = urls[index];
    const label = trackLabel(plan);
    if (!url) {
      unavailable.push({ label, reason: "服务端没有给出这条轨的地址" });
      return;
    }
    if (!plan.track_ref.startsWith("external:")) {
      unavailable.push({
        label,
        reason: "内封字幕暂不支持在网页端渲染，可用同名外挂字幕文件替代",
      });
      return;
    }
    if (plan.kind === "pgs") {
      unavailable.push({ label, reason: "PGS 是图形字幕，网页端暂不支持渲染" });
      return;
    }
    if (plan.kind !== "vtt" && plan.kind !== "ass") {
      unavailable.push({ label, reason: `暂不支持的字幕格式：${plan.kind}` });
      return;
    }
    options.push({
      ref: plan.track_ref,
      label,
      kind: plan.kind,
      // 文本轨统一要 VTT：`<track>` 只认这个格式，服务端现读现转。
      // ASS 不能转——转成 VTT 就丢掉了特效与排版，番剧字幕直接崩。
      url: plan.kind === "vtt" ? `${url}&format=vtt` : url,
      language: plan.language,
      isDefault: plan.is_default,
    });
  });

  return { options, unavailable };
}

/**
 * 选哪条轨：优先用户上次记住的，其次服务端标记的默认轨，再次第一条。
 *
 * `remembered` 是 playback_state 里存的中性引用，值为 "off" 表示用户明确
 * 关掉了字幕——**这条必须尊重**，否则每集都要手动关一次。
 */
export function pickInitialSubtitle(
  options: SubtitleOption[],
  remembered: string | null,
): string | null {
  if (remembered === "off") return null;
  if (remembered && options.some((o) => o.ref === remembered)) return remembered;
  return options.find((o) => o.isDefault)?.ref ?? options[0]?.ref ?? null;
}

/**
 * VTT cue 文本 → 可直接渲染的纯文本。
 *
 * 富文本标签一律剥掉。字幕文件是用户丢进媒体库的任意文本，`<i>` 与
 * `<img onerror=...>` 在解析器眼里没有区别——把 cue 内容当 HTML 插进 DOM
 * 就是一条现成的 XSS 通道。斜体这点观感换不来这个风险；要保留完整排版的
 * 场景走 ASS 路径（JASSUB 在 canvas 上画，根本不碰 DOM）。
 */
export function plainCueText(text: string): string {
  return text.replace(/<[^>]*>/g, "");
}

/** 字幕外观配置。外挂字幕经常不同步、字号也常年偏小，这两项是刚需不是锦上添花。 */
export interface SubtitleStyle {
  /** 相对视频高度的字号百分比 */
  fontScale: number;
  /** 时间轴微调（秒），正数=字幕延后 */
  offsetSeconds: number;
  /** 距画面底部的百分比 */
  bottomPercent: number;
  outline: boolean;
  background: boolean;
}

export const DEFAULT_SUBTITLE_STYLE: SubtitleStyle = {
  fontScale: 4.4,
  offsetSeconds: 0,
  bottomPercent: 6,
  outline: true,
  background: false,
};

/** 时间轴微调步进（秒）：0.1 秒是人耳能分辨的最小对不齐量级。 */
export const SUBTITLE_OFFSET_STEP = 0.1;

/** 微调范围钳制：超过 ±30 秒基本不是"没对齐"而是拿错了字幕文件。 */
export function clampSubtitleOffset(seconds: number): number {
  const clamped = Math.max(-30, Math.min(30, seconds));
  // 浮点累加会攒出 0.30000000000000004 这种值，显示成"+0.30000000000000004 秒"
  return Math.round(clamped * 10) / 10;
}
