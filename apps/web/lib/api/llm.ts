import { request } from "@/lib/http";

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

/** 供应商类型（与后端 movieclaw_llm 预设 id 对应）。 */
export type LlmProviderType =
  | "openai"
  | "bailian"
  | "deepseek"
  | "kimi"
  | "glm"
  | "openai_compat";

/** 连接验证状态（与站点/下载器共用同一状态机语义）。 */
export type LlmProviderStatus = "pending" | "verifying" | "active" | "failed";

/** 预设模型目录条目（见 movieclaw_llm.models.ModelInfo）。 */
export interface LlmModelInfo {
  id: string;
  /** 输入+输出共享的总上下文（token）；null 表示官方未公布 */
  context_window: number | null;
  /** 单独的输入上限（百炼公布，OpenAI 不单独公布） */
  max_input_tokens: number | null;
  /** 单次响应的输出上限 */
  max_output_tokens: number | null;
  supports_tools: boolean;
  /** 是否支持一次响应发起多个工具调用 */
  supports_parallel_tool_calls: boolean;
  /** 是否会输出思考内容（reasoning_content） */
  supports_thinking: boolean;
  /** 思维链预算上限（thinking_budget 最大值）；null = 不支持或未公布 */
  max_thinking_tokens: number | null;
  /** 思考控制方言声明；null/缺省 = 强度不可控 */
  thinking_control?: LlmThinkingControl | null;
  /** 服务端推导的思考档位菜单（按声明裁剪）；空数组 = 隐藏档位选择器 */
  thinking_levels?: string[];
  modalities: string[];
}

/** 思考控制方言（movieclaw_llm.ThinkingControl 的前端投影）。 */
export interface LlmThinkingControl {
  /** effort=档位直传 / budget=预算分段 / toggle=仅开关 */
  kind: "effort" | "budget" | "toggle";
  /** effort 制的原生档位子集 */
  levels?: string[];
  /** 是否有真关闭协议（菜单出现「关」） */
  supports_off?: boolean;
}

/** 供应商预设（见 schemas.llm.LlmPresetView）：设置页渲染选项用。 */
export interface LlmPreset {
  id: LlmProviderType;
  display_name: string;
  /** 预设默认端点；null 表示走官方默认或必须自填 */
  base_url: string | null;
  /** 是否必须填写 base_url（通用兼容端点没有默认值） */
  requires_base_url: boolean;
  /** 用户不覆盖 User-Agent 时实际发送的 SDK 自带 UA（输入框占位提示） */
  default_user_agent: string;
  models: LlmModelInfo[];
}

/** 当前配置的对外视图（见 schemas.llm.LlmProviderView，脱敏无 API Key）。 */
export interface LlmProviderConfig {
  provider_type: LlmProviderType;
  base_url: string | null;
  /** 自定义 User-Agent；null 表示用 SDK 自带 UA */
  user_agent: string | null;
  default_model: string;
  status: LlmProviderStatus;
  /** 是否可用 = 连接测试通过 */
  usable: boolean;
  last_error: string | null;
  last_checked_at: string | null;
  /** 最近验证成功时端点上报的可用模型列表 */
  available_models: string[] | null;
  /** 用户补录的自定义模型目录（含参数），设置页下拉框的数据源之一 */
  extra_models: LlmModelInfo[];
  created_at: string;
  updated_at: string;
}

/** 保存配置的请求体（见 schemas.llm.LlmProviderPayload）。 */
export interface LlmProviderPayload {
  provider_type: LlmProviderType;
  base_url?: string | null;
  /** 自定义 User-Agent：留空（null）使用 SDK 自带 UA */
  user_agent?: string | null;
  api_key: string;
  default_model: string;
  /** 自定义模型目录：default_model 不在预设目录时，必须在这里带齐参数 */
  extra_models?: LlmModelInfo[];
}

/** 列出可接入的供应商类型及其模型目录。 */
export function listLlmPresets(init?: RequestInit): Promise<LlmPreset[]> {
  return unwrap(request<ApiEnvelope<LlmPreset[]>>("/llm/presets", init));
}

/** 获取当前配置；尚未配置时返回 null（用于轮询连接测试进度）。 */
export function getLlmProvider(init?: RequestInit): Promise<LlmProviderConfig | null> {
  return unwrap(request<ApiEnvelope<LlmProviderConfig | null>>("/llm/provider", init));
}

/** 保存配置（单例 upsert，保存后后端异步测试连接）。 */
export function saveLlmProvider(payload: LlmProviderPayload): Promise<LlmProviderConfig> {
  return unwrap(
    request<ApiEnvelope<LlmProviderConfig>>("/llm/provider", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 手动重新测试一次连接。 */
export function reverifyLlmProvider(): Promise<LlmProviderConfig> {
  return unwrap(
    request<ApiEnvelope<LlmProviderConfig>>("/llm/provider/verify", { method: "POST" }),
  );
}

/** 删除配置。 */
export function deleteLlmProvider(): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>("/llm/provider", { method: "DELETE" }),
  );
}
