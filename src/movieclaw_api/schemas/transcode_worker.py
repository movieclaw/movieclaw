"""远程转码 Worker 管理接口的请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.settings.remote_transcode import (
    DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES,
    MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES,
)

RemoteTranscodeBaseUrlSource = Literal[
    "remote_transcode_setting",
    "system_external_url",
    "unset",
]


class RemoteTranscodeConfigPayload(BaseModel):
    """网页保存的远程转码配置。

    ``worker_token`` 使用三态语义：``null`` 表示保持当前令牌，空字符串表示
    清除令牌，其余字符串表示替换令牌。这样 GET 接口无需回传敏感值，
    前端也能在不修改令牌时安全地保存其他字段。
    """

    enabled: bool = Field(default=False, description="是否启用远程硬件转码")
    base_url: str | None = Field(
        default=None,
        max_length=4096,
        description="远程转码专用根地址；null=保持，空字符串=清除并回退系统外部访问地址",
    )
    worker_token: str | None = Field(
        default=None,
        max_length=4096,
        description="Worker 令牌；null=保持，空字符串=清除",
    )
    max_artifact_bytes: int = Field(
        default=DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES,
        gt=0,
        le=MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES,
        description="单个 HLS 产物上传的最大字节数",
    )


class RemoteTranscodeConfigView(BaseModel):
    """网页展示的远程转码配置，不包含令牌明文。"""

    enabled: bool
    worker_token_configured: bool
    base_url: str = Field(default="", description="实际使用的远程转码根地址")
    base_url_override: str = Field(
        default="", description="网页配置的远程转码专用根地址；空表示跟随系统外部访问地址"
    )
    base_url_source: RemoteTranscodeBaseUrlSource
    max_artifact_bytes: int
    ready: bool = Field(description="配置是否满足启用远程转码的前置条件")
    issues: list[str] = Field(
        default_factory=list,
        description="当前配置缺少的前置条件；不包含任何令牌内容",
    )


__all__ = [
    "RemoteTranscodeConfigPayload",
    "RemoteTranscodeConfigView",
    "RemoteTranscodeBaseUrlSource",
]
