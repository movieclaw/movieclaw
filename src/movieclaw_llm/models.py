"""统一的 LLM 请求/响应模型 —— 供应商无关的「中间语言」。

设计要点（对齐 pi-ai 的实践）：

1. 消息内容是 ``str | list[ContentPart]`` 联合：日常调用直接写字符串，
   多模态（图片）/ 思考内容才用块列表，与 OpenAI 线协议的形态一致；
2. 全部模型可 JSON 无损序列化，为对话历史落库和未来跨供应商切换留路；
3. ToolCall.arguments 由协议层解析为 dict，解析失败时保留原始串并记录
   parse_error —— 校验和纠错交给 ``tools.validate_tool_call``，不在
   传输层崩掉 agent loop。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, computed_field, field_validator

# ---------------------------------------------------------------------------
# 内容块：消息正文的最小单元
# ---------------------------------------------------------------------------


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """图片输入。三种形态（docs/design/agent-image-input.md）：

    - ``url``：http(s) 直链，原样发给供应商；
    - ``data`` + ``media_type``：base64 内联，仅存在于发请求前的内存消息里；
    - ``attachment_id``：服务端附件引用（会话 assets 目录下的 id）。**转录里
      只存引用**，发请求前由 API 层水合成 data；传输层遇到「只有引用、没有
      data/url」的块会降级为占位文本，绝不把内部引用发给供应商。
    """

    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None  # 如 image/jpeg，仅 data 形态需要
    attachment_id: str | None = None
    #: 原始文件名。前端 chip/alt 文案，也是水合层生成附件清单文本的原料
    name: str | None = None


class ThinkingPart(BaseModel):
    """模型的思考过程（如 deepseek-r1 的 reasoning_content）。

    仅出现在响应侧的历史消息里；发回供应商时协议层会将其丢弃——
    各家 API 均不接受思考内容作为输入。
    """

    type: Literal["thinking"] = "thinking"
    text: str


ContentPart = Annotated[TextPart | ImagePart | ThinkingPart, Field(discriminator="type")]


# ---------------------------------------------------------------------------
# 消息与工具
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """模型发起的一次工具调用。"""

    id: str
    name: str
    #: 协议层尽力解析出的参数；解析失败时为空 dict，原文见 raw_arguments
    arguments: dict = Field(default_factory=dict)
    #: 供应商返回的原始 JSON 字符串（回传历史消息时优先使用，保真）
    raw_arguments: str = ""
    #: 参数 JSON 解析失败的原因；正常为 None
    parse_error: str | None = None


class ChatMessage(BaseModel):
    """对话消息。四种角色覆盖完整的 agent loop 往返：

    - system:    系统提示词
    - user:      用户输入（支持图文混合块）
    - assistant: 模型输出（正文 + 可选的 tool_calls）
    - tool:      工具执行结果，tool_call_id 关联到发起调用的 id
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart] = ""
    tool_calls: list[ToolCall] | None = None  # 仅 assistant
    tool_call_id: str | None = None  # 仅 tool
    name: str | None = None  # 仅 tool：工具名

    def text(self) -> str:
        """提取纯文本正文（忽略图片与思考块），便于日志与降级场景。"""
        if isinstance(self.content, str):
            return self.content
        return "".join(p.text for p in self.content if isinstance(p, TextPart))


class ToolDefinition(BaseModel):
    """暴露给模型的工具声明，parameters 为标准 JSON Schema。"""

    name: str
    description: str
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


class ModelSettings(BaseModel):
    """模型采样与输出控制参数。全部可选，None 表示不传、用供应商默认值。"""

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    response_format: Literal["text", "json_object"] | None = None
    #: 思维链强度档位（docs/design/agent-thinking-level.md）。None = 默认，
    #: 不发送任何思考参数；具体档位由协议层按模型的 thinking_control 声明
    #: 翻译成该端点的方言，模型菜单不含该档位时静默回落默认（不就近取整）
    thinking_level: ThinkingLevel | None = None
    #: 供应商私有参数逃生舱，原样并入请求体（如百炼的 enable_search）。
    #: 与思考翻译结果冲突时**用户显式值优先**
    extra_body: dict | None = None


class ChatRequest(BaseModel):
    """一次 LLM 调用的完整入参。model 引用的三种写法见 LlmRouter。"""

    model: str = ""
    messages: list[ChatMessage]
    tools: list[ToolDefinition] | None = None
    #: "auto" / "none" / "required"，或直接写某个工具名强制调用它
    tool_choice: str | None = None
    settings: ModelSettings = Field(default_factory=ModelSettings)


# ---------------------------------------------------------------------------
# 响应与流式事件
# ---------------------------------------------------------------------------

FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error", "aborted"]


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: 命中提示词缓存的 token 数（OpenAI / 百炼均返回），用于成本核算
    cache_read_tokens: int = 0


class ChatResponse(BaseModel):
    """一次调用的最终结果；流式场景下也作为事件里的累积快照（partial）。"""

    content: str | None = None
    thinking: str | None = None  # 思考内容（reasoning_content），无则为 None
    tool_calls: list[ToolCall] | None = None
    finish_reason: FinishReason | None = None  # 流式进行中为 None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""  # 实际执行的模型 id
    provider: str = ""  # 实际执行的供应商实例名
    error: str | None = None  # finish_reason=error 时的中文错误说明

    def to_message(self) -> ChatMessage:
        """转成 assistant 历史消息，直接追加回对话上下文（agent loop 用）。"""
        parts: list[ContentPart] = []
        if self.thinking:
            parts.append(ThinkingPart(text=self.thinking))
        if self.content:
            parts.append(TextPart(text=self.content))
        content: str | list[ContentPart] = parts if self.thinking else (self.content or "")
        return ChatMessage(role="assistant", content=content, tool_calls=self.tool_calls)


StreamEventType = Literal[
    "start",
    "text_delta",
    "thinking_delta",
    "toolcall_start",
    "toolcall_delta",
    "toolcall_end",
    "done",
    "error",
]


class ChatStreamEvent(BaseModel):
    """流式增量事件。

    partial 始终携带当前累积的完整快照——消费方不必自己拼接 delta，
    任意时刻中断都能拿到已生成的部分（pi-ai 同款设计）。
    错误不抛异常而是 error 事件收尾，agent loop 里用户打断是常态。
    """

    type: StreamEventType
    delta: str | None = None  # text_delta / thinking_delta / toolcall_delta 的增量
    tool_call: ToolCall | None = None  # toolcall_start / toolcall_delta / toolcall_end 时给出
    partial: ChatResponse


# ---------------------------------------------------------------------------
# 思维链强度（docs/design/agent-thinking-level.md）
# ---------------------------------------------------------------------------

#: 统一档位词汇表：各家原生 effort 枚举词的**并集**（maka 同款七档），不是
#: 自创梯子。off 是关闭线协议而非强度档，只有声明 supports_off 的模型才在
#: 菜单出现。供应商发明新词时扩表收编，未收编前静默丢弃。
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

#: 展示顺序（浅 → 深）；菜单渲染与声明归一都按它排序
THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

#: budget 制的档位 → 思考预算比例（占 max_thinking_tokens 的份额）。
#: maka 无预算制先例，25/50/100 是本项目自创约定，等真实反馈再调。
BUDGET_LEVEL_RATIOS: dict[str, float] = {"low": 0.25, "medium": 0.5, "high": 1.0}


class ThinkingControl(BaseModel):
    """模型的思考控制方言声明（能力事实，不是功能承诺）。

    三种 kind 覆盖已知端点：
    - ``effort``：档位原词直传（请求体 reasoning_effort），levels 声明该模型
      的原生档位子集 = 用户菜单；off 编码为 reasoning_effort="none"；
    - ``budget``：enable_thinking + thinking_budget，档位按模型
      max_thinking_tokens 比例分段（低 25% / 中 50% / 高 100%）；未声明
      预算上限的模型退化为仅开关；
    - ``toggle``：仅开/关（如 GLM 的 thinking.type），菜单只有「关」。

    未声明本字段的模型没有菜单、档位永不落请求（fail-closed）。
    """

    kind: Literal["effort", "budget", "toggle"]
    #: effort 制的原生档位子集；词汇表外的值静默丢弃，off 不在此声明
    levels: list[str] = Field(default_factory=list)
    #: 是否存在真正的关闭线协议；true 时菜单多出「关」
    supports_off: bool = False

    @field_validator("levels")
    @classmethod
    def _known_levels_in_order(cls, value: list[str]) -> list[str]:
        # maka 同款宽进：未知词与 off 静默丢弃（off 走 supports_off 声明），
        # 归一为词汇表固定顺序——声明不是有序集合
        known = {v for v in value if v in THINKING_LEVELS and v != "off"}
        return [level for level in THINKING_LEVELS if level in known]


# ---------------------------------------------------------------------------
# 供应商配置与元数据
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    """模型目录条目：agent 做上下文预算 / 能力判断的依据。

    token 三元组的语义（None 一律表示官方未单独公布）：
    - context_window     输入+输出共享的总上下文；
    - max_input_tokens   单独的输入上限（百炼公布，OpenAI 不单独公布）；
    - max_output_tokens  单次响应的输出上限，agent 设 max_tokens 的依据。
    """

    id: str
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = True
    #: 是否支持一次响应发起多个工具调用（parallel tool calls）
    supports_parallel_tool_calls: bool = False
    #: 是否会输出思考内容（reasoning_content 或同等字段）
    supports_thinking: bool = False
    #: 思维链预算上限（thinking_budget 可设的最大 token 数）；
    #: None = 不支持思考或官方未公布（百炼部分模型仅控制台模型卡片可见）
    max_thinking_tokens: int | None = None
    #: 思考控制方言声明；None = 强度不可控（supports_thinking 只表示会输出
    #: 思考内容，不兼职暗示可控性）
    thinking_control: ThinkingControl | None = None
    modalities: list[str] = Field(default_factory=lambda: ["text"])

    @computed_field  # type: ignore[prop-decorator]
    @property
    def thinking_levels(self) -> list[str]:
        """本模型的思考档位菜单（服务端推导，前端不必理解方言）。

        空列表 = 无菜单，UI 隐藏选择器。budget 制未声明预算上限时退化为
        仅开关（没有预算可分段）。
        """
        control = self.thinking_control
        if control is None:
            return []
        if control.kind == "effort":
            menu = list(control.levels)
        elif control.kind == "budget" and self.max_thinking_tokens:
            menu = ["low", "medium", "high"]
        else:  # toggle，或退化的 budget
            menu = []
        if control.supports_off:
            menu = ["off", *menu]
        return menu


class ProviderCompat(BaseModel):
    """OpenAI 兼容端点的方言差异声明（pi-ai 的 compat 思路）。"""

    #: 响应/流里承载思考内容的字段名（如 reasoning_content），None 表示无
    thinking_field: str | None = None
    #: 流式请求是否支持 stream_options.include_usage 返回用量
    supports_stream_usage: bool = True


class ProviderPreset(BaseModel):
    """供应商预设（随代码分发的 yaml）：协议 + 默认端点 + 方言 + 模型目录。"""

    id: str
    display_name: str
    protocol: str  # 协议实现 id，当前仅 openai_chat
    base_url: str | None = None  # None 表示用 SDK 默认或必须由用户配置
    #: base_url 是否必须由用户提供（通用兼容端点没有可用的默认值）
    requires_base_url: bool = False
    compat: ProviderCompat = Field(default_factory=ProviderCompat)
    models: list[ModelInfo] = Field(default_factory=list)


class LlmProviderConfig(BaseModel):
    """供应商实例配置：用户接入的一个具体账号/端点。

    由上层（DB 配置表）组装注入。provider_type 关联到 ProviderPreset，
    base_url / 模型目录等留空时继承预设值。
    """

    name: str  # 实例名，路由用，全局唯一
    provider_type: str  # 预设 id：openai / bailian / openai_compat
    api_key: str
    base_url: str | None = None  # 覆盖预设端点
    default_model: str | None = None
    #: 用户补录的预设外模型（如自建 vLLM 上的模型），与预设目录合并
    extra_models: list[ModelInfo] = Field(default_factory=list)
    enabled: bool = True
    is_default: bool = False
    timeout_seconds: float | None = None  # None 用 SDK 默认
    #: 自定义 User-Agent 请求头。None = 用 openai SDK 自带的 UA；
    #: 自建网关 / 反代前置 WAF 常按 UA 放行或限流，这里给用户一个覆盖口子。
    user_agent: str | None = None

    @field_validator("name")
    @classmethod
    def _name_no_slash(cls, v: str) -> str:
        # 路由引用格式是「实例名/模型id」，实例名含斜杠会破坏解析
        if "/" in v:
            raise ValueError("供应商实例名不能包含斜杠 /")
        if not v.strip():
            raise ValueError("供应商实例名不能为空")
        return v


class ProviderInfo(BaseModel):
    """test_connection 的返回：连通性验证结果与端点上报的可用模型。"""

    models: list[str] = Field(default_factory=list)
