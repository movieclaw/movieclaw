import { request } from "@/lib/http";
import type { JobView } from "@/lib/api/jobs";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/**
 * AI 字幕生成（docs/design/subtitle-ai-translate.md）：选源 → 同步质检 →
 * LLM 翻译 → 机检 → 落盘外挂 sidecar。管理员专属（消费 LLM 配额）。
 */

/** 一个候选参考字幕的打分结论（预检确认框展示）。 */
export interface SubtitleSourceCandidate {
  kind: "embedded" | "external";
  key: string;
  language: string | null;
  format: string | null;
  provenance: "original" | "pgs_ocr" | "ai" | "ai_bilingual";
  /** 非空 = 被排除原因（forced/目标语言/图形轨） */
  excluded: string | null;
  /** 打分明细："为什么选了这条"要能看懂 */
  reasons: string[];
  /** 文本轨可直接选，PGS 可选后进入 OCR。 */
  selectable: boolean;
  requires_ocr: boolean;
}

export interface SubtitleGenerationPreview {
  candidates: SubtitleSourceCandidate[];
  /** "kind:key"；null = 没有可用参考 */
  chosen_key: string | null;
  /** 用户指定或默认英语策略实际选中的候选，PGS 也会有值。 */
  selected_source_key: string | null;
  event_count: number;
  estimated_tokens: number;
  /** 目标语言 AI 字幕已存在（再生成 = 覆盖） */
  already_generated: boolean;
  warnings: string[];
  /** 仅有 PGS 图形字幕时的转换候选与服务端运行环境检测。 */
  pgs_conversion: {
    candidate_key: string;
    language: string | null;
    available: boolean;
    engine: string | null;
    platform: string;
    architecture: string;
    cached: boolean;
    message: string;
    suggestions: string[];
    /** PGS 图片里的语言；与最终翻译目标语言无关。 */
    ocr_language: string | null;
    ocr_language_label: string | null;
    language_confirmation_required: boolean;
    language_reason: string;
    language_options: Array<{ code: string; label: string }>;
  } | null;
  /** 无法生成时的稳定原因与可执行建议；可生成时为 null。 */
  blocker: {
    code: string;
    title: string;
    message: string;
    suggestions: string[];
  } | null;
  /** 规范化后的最终外挂字幕文件名。 */
  output_filename: string | null;
  /**
   * 非空 = 这次还没有结论：内封轨正在后台抽取，按 retry_after_ms 重拉即可。
   *
   * 与 blocker 是两回事——blocker 说「这份片源做不了」，pending 说「再等
   * 一会儿」。等待期间绝不能把 blocker 的文案显示出来。
   */
  pending: {
    message: string;
    candidate_key: string;
    retry_after_ms: number;
  } | null;
}

export interface CalibrateResult {
  ok: boolean;
  message: string;
  scale: number | null;
  offset_ms: number | null;
  score: number | null;
}

/**
 * 生成预检：选源结果 + 成本估算（确认框素材，不动 LLM）。
 *
 * 后端保证这个接口**不会**在里面等 ffmpeg 通读大文件：内封轨没抽好就回
 * `pending`，抽取转后台（issue #432）。所以这里可以放心给一个短超时——
 * 超过它就是真的不对劲，而不是「文件大，再等等」。
 */
const PREVIEW_TIMEOUT_MS = 20_000;

export function previewSubtitleGeneration(
  fileId: number,
  targetLanguage = "chs",
  secondaryLanguage: string | null = null,
  sourceCandidateKey: string | null = null,
  signal?: AbortSignal,
): Promise<SubtitleGenerationPreview> {
  const query = new URLSearchParams({ target_language: targetLanguage });
  if (secondaryLanguage) query.set("secondary_language", secondaryLanguage);
  if (sourceCandidateKey) query.set("source_candidate_key", sourceCandidateKey);
  return unwrap(
    request<ApiEnvelope<SubtitleGenerationPreview>>(
      `/libraries/files/${fileId}/subtitles/generation-preview?${query}`,
      { signal },
      { timeoutMs: PREVIEW_TIMEOUT_MS },
    ),
  );
}

/** 发起生成（后台执行；同文件已在跑时后端 400）。 */
export function generateSubtitles(
  fileId: number,
  targetLanguage = "chs",
  options: {
    convertPgs?: boolean;
    pgsOcrLanguage?: string | null;
    secondaryLanguage?: string | null;
    sourceCandidateKey?: string | null;
  } = {},
): Promise<JobView> {
  const {
    convertPgs = false,
    pgsOcrLanguage = null,
    secondaryLanguage = null,
    sourceCandidateKey = null,
  } = options;
  return unwrap(
    request<ApiEnvelope<JobView>>(
      `/libraries/files/${fileId}/subtitles/generations`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_language: targetLanguage,
          secondary_language: secondaryLanguage,
          source_candidate_key: sourceCandidateKey,
          convert_pgs: convertPgs,
          pgs_ocr_language: pgsOcrLanguage,
        }),
      },
    ),
  );
}

/** 一键校准外挂字幕时间轴（零 LLM 成本；校准后覆盖写回）。 */
export function calibrateSubtitleTiming(
  fileId: number,
  filename: string,
): Promise<CalibrateResult> {
  return unwrap(
    request<ApiEnvelope<CalibrateResult>>(
      `/libraries/files/${fileId}/subtitles/timing-calibration`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename }),
      },
    ),
  );
}
