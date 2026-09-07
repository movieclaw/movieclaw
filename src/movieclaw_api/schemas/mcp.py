"""MCP 服务管理接口的请求/响应模型（「设置 → MCP 服务」页）。"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class ServiceView(BaseModel):
    """一个可勾选的服务：给管理页算「选了它会多几个工具」用。"""

    domain: str = Field(description="服务域名，如 subscriptions")
    description: str = Field(default="", description="这个服务能做什么（一行）")
    command_count: int = Field(description="该服务的命令数 = 展开模式下的工具数")
    expanded_bytes: int = Field(description="展开模式下这些工具定义的大致体积（字节）")
    collapsed_bytes: int = Field(description="折叠模式下这一个工具的描述体积（字节）")


class EndpointView(BaseModel):
    """端点的展示形态。永远不含令牌明文——它只在创建与轮换时返回一次。"""

    id: str
    slug: str
    name: str
    description: str = ""
    services: list[str] = Field(default_factory=list)
    missing_services: list[str] = Field(
        default_factory=list, description="配置里存在但当前版本已没有的服务域（已忽略）"
    )
    expand_tools: bool = True
    enabled: bool = True
    token_hint: str = ""
    timeout_seconds: int = 300
    tool_count: int = Field(default=0, description="当前配置下的实际工具数")
    url: str = Field(default="", description="供客户端填写的完整地址")
    created_at: str = ""
    last_used_at: str | None = None


class StatusView(BaseModel):
    """分区首屏需要的一切：总开关、端点、可选服务目录。"""

    enabled: bool = Field(description="MCP 总开关")
    base_url: str = Field(default="", description="端点地址的公共前缀")
    external_url_configured: bool = Field(
        default=False, description="是否已配置外部访问地址；否则上面的地址只在局域网可用"
    )
    endpoints: list[EndpointView] = Field(default_factory=list)
    services: list[ServiceView] = Field(default_factory=list)


class SelfCheckView(BaseModel):
    """自检结果：这个端点现在能不能用，断在哪一环。"""

    ok: bool
    message: str = ""
    protocol_version: str = ""
    tool_count: int = 0
    elapsed_ms: int = 0
    probe_tool: str = Field(default="", description="被试调的只读工具；空=没有可安全试调的工具")
    probe_ok: bool = False
    probe_message: str = ""
    warnings: list[str] = Field(default_factory=list)


class EndpointCreateRequest(BaseModel):
    name: str = Field(description="展示名，如「家庭影音助理」")
    slug: str = Field(description="地址标识，URL 末段")
    services: list[str] = Field(description="选中的服务域")
    description: str = ""
    expand_tools: bool = True
    timeout_seconds: int = 300


class EndpointUpdateRequest(BaseModel):
    """只更新给出的字段；地址标识建成后不可改。"""

    name: str | None = None
    description: str | None = None
    services: list[str] | None = None
    expand_tools: bool | None = None
    enabled: bool | None = None
    timeout_seconds: int | None = None


class EndpointCreatedView(BaseModel):
    """创建/轮换的响应：唯一一次带令牌明文。"""

    endpoint: EndpointView
    token: str = Field(description="令牌明文，仅本次返回，服务端只存哈希")


class ToggleRequest(BaseModel):
    enabled: bool


class PreviewRequest(BaseModel):
    """试算：给定服务集合与模式，返回将暴露的工具清单。"""

    services: list[str] = Field(default_factory=list)
    expand_tools: bool = True


class ToolParameter(BaseModel):
    """工具的一个参数。管理页要展示它，用户才能判断「这个工具好不好用」。"""

    name: str
    type: str = Field(default="", description="JSON Schema 类型，如 integer / string[]")
    required: bool = False
    description: str = ""
    location: str = Field(default="", description="落点：path / query / body")
    options: list[str] = Field(
        default_factory=list,
        description="枚举取值。折叠模式下 command 的取值就靠它列出该服务覆盖了哪些命令",
    )


class ToolCommand(BaseModel):
    """折叠模式下一个服务工具覆盖的一条命令。

    折叠模式把整个服务压成一个工具，命令清单本来只以自然语言写在 description
    里给模型看。管理页要是照搬那段文本，用户面对的就是一堵几百行的散文墙——
    所以这里把同一份元数据结构化再给一遍，页面按表格渲染。
    """

    name: str = Field(description="命令名，即 command 参数的取值")
    summary: str = ""
    params: list[str] = Field(
        default_factory=list, description="params 里可填的字段名，必填的带 * 后缀"
    )
    dangerous: str = Field(default="", description="confirm | destructive | 空")
    is_job: bool = Field(default=False, description="提交后台任务，返回 job_id")


class ToolPreview(BaseModel):
    name: str
    #: 一行摘要（description 的第一句），列表里显示这个
    summary: str = ""
    description: str
    service: str = Field(default="", description="所属服务域，详情页按它分组")
    read_only: bool = False
    destructive: bool = False
    parameters: list[ToolParameter] = Field(default_factory=list)
    commands: list[ToolCommand] = Field(
        default_factory=list, description="仅折叠模式：这个服务工具覆盖的命令清单"
    )


class PreviewView(BaseModel):
    tool_count: int
    command_count: int
    approx_bytes: int
    tools: list[ToolPreview] = Field(default_factory=list)
