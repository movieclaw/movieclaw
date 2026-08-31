"""movieclaw_llm —— LLM 接入领域库。

分层设计（参考 pi-ai 的「协议 / 供应商」两层分离）：

- ``models``     统一的请求/响应/流式事件模型，供应商无关，全部可 JSON 序列化；
- ``protocols``  API 协议实现（当前只有 openai_chat，覆盖所有 OpenAI 兼容端点）；
- ``providers``  供应商预设注册表（yaml 预置 base_url / 兼容性标志 / 模型目录）；
- ``router``     路由层：按 model 引用解析供应商实例并分发请求，未来 agent
                 的所有 LLM 调用都从这里进出。

本库是纯领域逻辑，不依赖 FastAPI / 数据库；供应商实例配置
（LlmProviderConfig）由上层（movieclaw_api / movieclaw_db）组装后注入。
"""

from movieclaw_llm.exceptions import (
    LlmAuthError,
    LlmConnectError,
    LlmContentFilterError,
    LlmError,
    LlmRateLimitError,
    LlmRequestError,
    LlmRoutingError,
)
from movieclaw_llm.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ContentPart,
    ImagePart,
    LlmProviderConfig,
    ModelInfo,
    ModelSettings,
    ProviderInfo,
    TextPart,
    ThinkingControl,
    ThinkingLevel,
    ThinkingPart,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from movieclaw_llm.router import LlmRouter
from movieclaw_llm.tools import validate_tool_call

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ChatStreamEvent",
    "ContentPart",
    "ImagePart",
    "LlmAuthError",
    "LlmConnectError",
    "LlmContentFilterError",
    "LlmError",
    "LlmProviderConfig",
    "LlmRateLimitError",
    "LlmRequestError",
    "LlmRouter",
    "LlmRoutingError",
    "ModelInfo",
    "ModelSettings",
    "ProviderInfo",
    "TextPart",
    "ThinkingControl",
    "ThinkingLevel",
    "ThinkingPart",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
    "validate_tool_call",
]
