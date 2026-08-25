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
 * 外挂与内封轨一视同仁：内封轨由服务端按需 ffmpeg 抽出来再下发（首次要通读
 * 整个容器，之后走缓存）。PT 片源的字幕绝大多数是内封的，只服务外挂等于对
 * 大部分片子没字幕。
 *
 * 拿不到的轨进"不可用"清单并给出中文原因，而不是给一个点了没反应的选项——
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
  /** vtt 交 `<track>`，ass 交 JASSUB，pgs（蓝光位图轨的 .sup）交 libbitsub */
  kind: "vtt" | "ass" | "pgs";
  /** 已带签名 token 的下载地址 */
  url: string;
  language: string | null;
  isDefault: boolean;
  /**
   * 本机 AI 生成的字幕（翻译 / 双语）。
   *
   * 菜单要把它标出来：AI 字幕的译文与时间轴都可能有偏差，同一部片里它常常
   * 和发行方的字幕并排列着，不标的话用户选中之后才发现，还以为是播放器的
   * 字幕功能不准。判定在服务端（按台账里解析好的命名段，见
   * movieclaw_playback/subtitles.py 的 is_ai_generated）。
   */
  isAi: boolean;
}

/** 一条拿不到的字幕轨，连同面向用户的中文原因。 */
export interface UnavailableSubtitle {
  /** 轨引用，天然唯一（embedded:N / external:文件名），列表渲染用它当 key */
  ref: string;
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
  const name = language ?? refLabel(plan.track_ref);
  return `${name} · ${kind}`;
}

/**
 * 没有语言标记时的兜底名。外挂轨用文件名（用户自己放的，认得出），内封轨
 * 用序号——总比一律叫"未知语言"强，那样多条无语言标记的轨会长得一模一样。
 */
function refLabel(ref: string): string {
  if (ref.startsWith("external:")) return ref.slice("external:".length);
  if (ref.startsWith("embedded:")) return `内封轨 ${ref.slice("embedded:".length)}`;
  return "未知语言";
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
      unavailable.push({ ref: plan.track_ref, label, reason: "服务端没有给出这条轨的地址" });
      return;
    }
    if (plan.kind !== "vtt" && plan.kind !== "ass" && plan.kind !== "pgs") {
      unavailable.push({ ref: plan.track_ref, label, reason: `暂不支持的字幕格式：${plan.kind}` });
      return;
    }
    options.push({
      ref: plan.track_ref,
      label,
      kind: plan.kind,
      // 文本轨统一要 VTT：`<track>` 只认这个格式，服务端现读现转。
      // ASS 不能转——转成 VTT 就丢掉了特效与排版，番剧字幕直接崩。
      // PGS 原样要 .sup 二进制，交 libbitsub 在 canvas 上渲染。
      url: plan.kind === "vtt" ? `${url}&format=vtt` : url,
      language: plan.language,
      isDefault: plan.is_default,
      isAi: plan.is_ai === true,
    });
  });

  return { options, unavailable };
}

/**
 * 选哪条轨：优先用户上次记住的，其次服务端裁决的默认轨；都没有就不自动开。
 *
 * `remembered` 是 playback_state 里存的中性引用，值为 "off" 表示用户明确
 * 关掉了字幕——**这条必须尊重**，否则每集都要手动关一次。
 *
 * `isDefault` 是服务端 pick_default_subtitle 的结论（外挂 > AI 优先 >
 * 内封 default 旗标 > 非 forced，全不命中谁都不标）——与 Jellyfin 协议端
 * 同一个函数，两个入口对同一部片给出同一条默认轨。这里刻意**不再兜底选
 * 第一条**：服务端说「不该自动开」（比如只有 forced 轨之外全无候选）时，
 * 网页端硬开第一条就是两端漂移的来源（2026-08-25 对齐）。
 */
export function pickInitialSubtitle(
  options: SubtitleOption[],
  remembered: string | null,
): string | null {
  if (remembered === "off") return null;
  if (remembered && options.some((o) => o.ref === remembered)) return remembered;
  return options.find((o) => o.isDefault)?.ref ?? null;
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

const STYLE_STORAGE_KEY = "movieclaw.player.subtitle-style";

/** 读持久化的字幕样式；没存过/损坏/无 localStorage 都回默认值。
    offsetSeconds 刻意不持久化——时间轴偏移是逐文件的修正，跨片带着只会错。 */
export function loadSubtitleStyle(): SubtitleStyle {
  try {
    const raw = window.localStorage.getItem(STYLE_STORAGE_KEY);
    if (!raw) return DEFAULT_SUBTITLE_STYLE;
    const parsed = JSON.parse(raw) as Partial<SubtitleStyle>;
    return {
      ...DEFAULT_SUBTITLE_STYLE,
      fontScale: typeof parsed.fontScale === "number" ? parsed.fontScale : DEFAULT_SUBTITLE_STYLE.fontScale,
      bottomPercent:
        typeof parsed.bottomPercent === "number" ? parsed.bottomPercent : DEFAULT_SUBTITLE_STYLE.bottomPercent,
      outline: typeof parsed.outline === "boolean" ? parsed.outline : DEFAULT_SUBTITLE_STYLE.outline,
      background:
        typeof parsed.background === "boolean" ? parsed.background : DEFAULT_SUBTITLE_STYLE.background,
    };
  } catch {
    return DEFAULT_SUBTITLE_STYLE;
  }
}

export function saveSubtitleStyle(style: SubtitleStyle): void {
  try {
    const { offsetSeconds: _drop, ...persisted } = style;
    window.localStorage.setItem(STYLE_STORAGE_KEY, JSON.stringify(persisted));
  } catch {
    // 隐私模式写不进：本次会话内仍生效
  }
}

export const DEFAULT_SUBTITLE_STYLE: SubtitleStyle = {
  // 画面高的 5.2%：Netflix 规范约 5.3%、BBC 指南约 5.5%、YouTube 默认 4~5%，
  // 取这个区间中值。渲染层另有最小像素下限兜底小画面（见 subtitle-layer）。
  fontScale: 5.2,
  offsetSeconds: 0,
  // 底部安全边距 8%：Apple/Netflix/BBC 的 caption safe area 都在 8~10%，
  // 字幕不贴画面底边
  bottomPercent: 8,
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
