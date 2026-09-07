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

/** 一个可勾选的服务（见 schemas.mcp.ServiceView）。 */
export interface McpService {
  domain: string;
  description: string;
  /** 该服务的命令数 = 展开模式下它贡献的工具数 */
  command_count: number;
  /** 展开 / 折叠两种模式下的工具定义体积，用来告诉用户「这套工具面占多少上下文」 */
  expanded_bytes: number;
  collapsed_bytes: number;
}

/** 一个端点（见 schemas.mcp.EndpointView）。永远不含令牌明文。 */
export interface McpEndpoint {
  id: string;
  slug: string;
  name: string;
  description: string;
  services: string[];
  /** 配置里存在但当前版本已没有的服务域：界面要提示「已忽略」而不是装作没事 */
  missing_services: string[];
  expand_tools: boolean;
  enabled: boolean;
  token_hint: string;
  timeout_seconds: number;
  tool_count: number;
  url: string;
  created_at: string;
  last_used_at: string | null;
}

export interface McpStatus {
  enabled: boolean;
  base_url: string;
  /** 未配置外部访问地址时，端点地址只有相对路径，外部客户端连不上 */
  external_url_configured: boolean;
  endpoints: McpEndpoint[];
  services: McpService[];
}

/** 创建/轮换的响应：唯一一次带令牌明文。 */
export interface McpEndpointCreated {
  endpoint: McpEndpoint;
  token: string;
}

/** 工具的一个参数（见 schemas.mcp.ToolParameter）：类型化参数面正是开发者要看的东西。 */
export interface McpToolParameter {
  name: string;
  type: string;
  required: boolean;
  description: string;
  /** 落点：path / query / body。展示出来是为了让人能把工具对回 API 文档 */
  location: string;
  /** 枚举取值；折叠模式下 command 的取值就是这个服务覆盖的命令清单 */
  options: string[];
}

/** 折叠模式下一个服务工具覆盖的一条命令（见 schemas.mcp.ToolCommand）。 */
export interface McpToolCommand {
  name: string;
  summary: string;
  /** params 里可填的字段名，必填的带 * 后缀 */
  params: string[];
  /** confirm | destructive | 空 */
  dangerous: string;
  is_job: boolean;
}

export interface McpToolPreview {
  name: string;
  /** 一行摘要，列表里显示这个；完整说明在 description */
  summary: string;
  description: string;
  /** 所属服务域，详情页按它分组 */
  service: string;
  read_only: boolean;
  destructive: boolean;
  parameters: McpToolParameter[];
  /** 仅折叠模式：这个服务工具覆盖的命令清单，页面按表格渲染 */
  commands: McpToolCommand[];
}

export interface McpPreview {
  tool_count: number;
  command_count: number;
  approx_bytes: number;
  tools: McpToolPreview[];
}

export interface McpEndpointPayload {
  name: string;
  slug: string;
  services: string[];
  description?: string;
  expand_tools?: boolean;
  timeout_seconds?: number;
}

/** 自检结果（见 schemas.mcp.SelfCheckView）：断在哪一环，这里会说清楚。 */
export interface McpSelfCheck {
  ok: boolean;
  message: string;
  protocol_version: string;
  tool_count: number;
  elapsed_ms: number;
  /** 被试调的只读工具；空串 = 这个端点没有可安全试调的工具 */
  probe_tool: string;
  probe_ok: boolean;
  probe_message: string;
  /** 自检通过、但仍会挡住外部客户端的情况（没配外部地址、端点停用等） */
  warnings: string[];
}

export async function checkMcpEndpoint(endpointId: string): Promise<McpSelfCheck> {
  return unwrap(
    request<ApiEnvelope<McpSelfCheck>>(
      `/mcp/endpoints/${encodeURIComponent(endpointId)}/check`,
      { method: "POST" },
    ),
  );
}

export async function getMcpStatus(): Promise<McpStatus> {
  return unwrap(request<ApiEnvelope<McpStatus>>("/mcp/status"));
}

export async function setMcpEnabled(enabled: boolean): Promise<McpStatus> {
  return unwrap(
    request<ApiEnvelope<McpStatus>>("/mcp/status", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  );
}

export async function createMcpEndpoint(
  payload: McpEndpointPayload,
): Promise<McpEndpointCreated> {
  return unwrap(
    request<ApiEnvelope<McpEndpointCreated>>("/mcp/endpoints", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

export async function updateMcpEndpoint(
  endpointId: string,
  payload: Partial<Omit<McpEndpointPayload, "slug">> & { enabled?: boolean },
): Promise<McpEndpoint> {
  return unwrap(
    request<ApiEnvelope<McpEndpoint>>(`/mcp/endpoints/${encodeURIComponent(endpointId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

export async function rotateMcpToken(endpointId: string): Promise<McpEndpointCreated> {
  return unwrap(
    request<ApiEnvelope<McpEndpointCreated>>(
      `/mcp/endpoints/${encodeURIComponent(endpointId)}/token`,
      { method: "POST" },
    ),
  );
}

export async function deleteMcpEndpoint(endpointId: string): Promise<void> {
  await request<ApiEnvelope<unknown>>(`/mcp/endpoints/${encodeURIComponent(endpointId)}`, {
    method: "DELETE",
  });
}

export async function previewMcpTools(
  services: string[],
  expandTools: boolean,
): Promise<McpPreview> {
  return unwrap(
    request<ApiEnvelope<McpPreview>>("/mcp/endpoints/preview", {
      method: "POST",
      body: JSON.stringify({ services, expand_tools: expandTools }),
    }),
  );
}
