"""MCP 服务端点配置域（docs/design/mcp-server.md §5）。

一条配置里挂一个端点列表：每个端点有自己的地址标识、选中的服务、工具展开开关
与一枚独立令牌。落库的是**令牌哈希**而不是明文——与 ``ApiTokenRecord`` 同一
立场：明文只在创建/轮换那一次回显，服务端此后无法再取出（丢了只能轮换）。

为什么用配置域而不是新建表：端点是「管理员配的几条记录」，不是业务数据。
``app_setting`` 表 + ``SettingSchema`` 已经提供校验、默认值与缓存，也就不需要
alembic 迁移（发布规范硬约束 3 说迁移只能向前兼容，能不加就不加）。
"""

from __future__ import annotations

import hashlib
import re
import secrets

from pydantic import BaseModel, ConfigDict, Field

from movieclaw_api.settings.base import SettingSchema, register_setting

MCP_NAMESPACE = "mcp.endpoints"

#: 令牌明文前缀：肉眼可辨来源，误提交到代码仓时扫描器也好识别。
TOKEN_PREFIX = "mcp_"

#: 地址标识的合法形态：小写字母数字与连字符，不以连字符开头结尾。
#: 它会成为 URL 的一段（/mcp/<slug>），所以不允许出现需要转义的字符。
SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")

#: 单次工具调用的超时上限。再长的任务应该走「立即返回 job_id + jobs_wait」，
#: 而不是让一个 HTTP 请求挂在那里（客户端与反代都有自己的超时）。
MAX_TIMEOUT_SECONDS = 900
DEFAULT_TIMEOUT_SECONDS = 300


def generate_token() -> str:
    """生成一枚端点令牌明文（前缀 + 32 字节随机）。"""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌明文 → sha256 十六进制。比对时用 ``hmac.compare_digest``。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_hint(token: str) -> str:
    """列表里展示用的令牌指纹：保留前缀与随后 4 位，其余打码。"""
    visible = len(TOKEN_PREFIX) + 4
    return token[:visible] + "****"


class McpEndpoint(BaseModel):
    """一个 MCP 端点。

    ``services`` 存的是服务域名（``operation_id`` 的第一段，如 ``subscriptions``）。
    读取时对「已不存在的域」静默忽略而不是报错：升级后某个域被移除，不该让整个
    端点连同其余服务一起坏掉——管理页会显示一行「有 N 个服务已不存在，已忽略」。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="端点 id（编辑/删除时使用）")
    slug: str = Field(default="", description="地址标识，URL 末段（/mcp/<slug>）")
    name: str = Field(default="", description="展示名，如「家庭影音助理」")
    description: str = Field(default="", description="备注：这个端点给谁用的")
    services: list[str] = Field(
        default_factory=list, description="选中的服务域，决定这个端点有哪些工具"
    )
    expand_tools: bool = Field(
        default=True,
        description="展开：一条命令一个工具（类型化参数）；关闭则一个服务一个工具",
    )
    enabled: bool = Field(default=True, description="端点开关；关闭后该地址返回 404")
    token_hash: str = Field(default="", description="令牌明文的 sha256，明文不落库")
    token_hint: str = Field(default="", description="打码后的令牌，仅供列表识别")
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS, ge=5, le=MAX_TIMEOUT_SECONDS,
        description="单次工具调用的超时秒数",
    )
    created_at: str = Field(default="", description="创建时间（ISO8601）")
    last_used_at: str | None = Field(
        default=None, description="最近一次被调用的时间（按分钟粒度落盘）"
    )


@register_setting(namespace=MCP_NAMESPACE, title="MCP 服务")
class McpEndpointsSetting(SettingSchema):
    """MCP 服务总配置。默认关闭、零端点——存量部署升级后对外零变化。"""

    enabled: bool = Field(
        default=False,
        description="总开关。关闭时所有端点一律 404（等于对外完全隐身）",
    )
    endpoints: list[McpEndpoint] = Field(default_factory=list)
