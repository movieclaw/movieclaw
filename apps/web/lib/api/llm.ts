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

/** 一个已接入的供应商实例（见 schemas.llm.LlmProviderView，脱敏无 API Key）。 */
export interface LlmProviderConfig {
  id: number;
  /** 实例名（全局唯一，「实例名/模型id」路由引用的前半段） */
  name: string;
  provider_type: LlmProviderType;
  base_url: string | null;
  /** 自定义 User-Agent；null 表示用 SDK 自带 UA */
  user_agent: string | null;
  /** 连接测试用的模型（目录里第一个，服务端自动填） */
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

/** 新增 / 编辑实例的请求体（见 schemas.llm.LlmProviderPayload）。 */
export interface LlmProviderPayload {
  /** 实例名（全局唯一，不含斜杠） */
  name: string;
  provider_type: LlmProviderType;
  base_url?: string | null;
  /** 自定义 User-Agent：留空（null）使用 SDK 自带 UA */
  user_agent?: string | null;
  api_key: string;
  /** 连接测试模型；留空由服务端取目录第一个 */
  default_model?: string | null;
  /** 自定义模型目录（openai_compat 端点至少一条，每条带齐参数） */
  extra_models?: LlmModelInfo[];
}

/** AI 设定（见 schemas.llm.LlmDefaultsView）：各用途的默认模型引用。
 *  `*_model` 是用户设定（null = 未设置）；`effective_*` 是实际生效的引用——
 *  设定可解析就用设定，否则按第一个实例的连接测试模型兜底。 */
export interface LlmDefaults {
  agent_model: string | null;
  subtitle_model: string | null;
  effective_agent_model: string | null;
  effective_subtitle_model: string | null;
}

export interface LlmDefaultsPayload {
  agent_model: string | null;
  subtitle_model: string | null;
}

/** 列出可接入的供应商类型及其模型目录。 */
export function listLlmPresets(init?: RequestInit): Promise<LlmPreset[]> {
  return unwrap(request<ApiEnvelope<LlmPreset[]>>("/llm/presets", init));
}

/** 对话框模型选择器的一个选项（见 schemas.llm.LlmModelOptionView）。
 *  同一模型 id 只在一个实例里有：ref 与 label 都是裸 id；出现在多个实例里：
 *  ref 为「实例名/模型id」精确路由，label 为「模型id（实例名）」。 */
export interface LlmModelOption {
  ref: string;
  label: string;
  model_id: string;
  provider_id: number;
  provider_name: string;
  /** 智能体默认模型（AI 设定），清单里恰有一个 */
  is_default: boolean;
  /** 该模型的思考档位菜单；空数组 = 隐藏档位选择器 */
  thinking_levels: string[];
}

/** 列出已接入的实例（默认实例在前）；一个都没有时为空数组。 */
export function listLlmProviders(init?: RequestInit): Promise<LlmProviderConfig[]> {
  return unwrap(request<ApiEnvelope<LlmProviderConfig[]>>("/llm/providers", init));
}

/** 对话框可选的全部模型（跨实例，默认实例在前）。 */
export function listLlmModels(init?: RequestInit): Promise<LlmModelOption[]> {
  return unwrap(request<ApiEnvelope<LlmModelOption[]>>("/llm/models", init));
}

/** 接入一个实例（保存后后端异步测试连接；第一个实例自动成为默认）。 */
export function createLlmProvider(payload: LlmProviderPayload): Promise<LlmProviderConfig> {
  return unwrap(
    request<ApiEnvelope<LlmProviderConfig>>("/llm/providers", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 修改一个实例（整体覆盖，保存后后端异步测试连接）。 */
export function updateLlmProvider(
  id: number,
  payload: LlmProviderPayload,
): Promise<LlmProviderConfig> {
  return unwrap(
    request<ApiEnvelope<LlmProviderConfig>>(`/llm/providers/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 手动重新测试一个实例的连接。 */
export function reverifyLlmProvider(id: number): Promise<LlmProviderConfig> {
  return unwrap(
    request<ApiEnvelope<LlmProviderConfig>>(`/llm/providers/${id}/verify`, { method: "POST" }),
  );
}

/** 读取 AI 设定。 */
export function getLlmDefaults(init?: RequestInit): Promise<LlmDefaults> {
  return unwrap(request<ApiEnvelope<LlmDefaults>>("/llm/defaults", init));
}

/** 保存 AI 设定（引用取自 listLlmModels 的 ref，null 清除）。 */
export function updateLlmDefaults(payload: LlmDefaultsPayload): Promise<LlmDefaults> {
  return unwrap(
    request<ApiEnvelope<LlmDefaults>>("/llm/defaults", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 删除一个实例（AI 设定里指向它的默认模型会自动兜底）。 */
export function deleteLlmProvider(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/llm/providers/${id}`, { method: "DELETE" }),
  );
}
