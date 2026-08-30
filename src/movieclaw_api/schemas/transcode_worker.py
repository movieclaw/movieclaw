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
    # 网页「高级」里填了覆盖地址
    "remote_transcode_setting",
    # 默认：用接单 Worker 自己连上来的地址，没有静态值可展示
    "worker_connection",
]


class RemoteTranscodeConfigPayload(BaseModel):
    """网页保存的远程转码配置。

    没有令牌字段：Worker 的凭证在「设置 → 设备」里配对签发与吊销
    （docs/design/device-auth.md §5.4）。
    """

    enabled: bool = Field(default=False, description="是否启用远程硬件转码")
    base_url: str | None = Field(
        default=None,
        max_length=4096,
        description="取源/回传根地址的覆盖项；null=保持，空字符串=清除并回到自动推断",
    )
    max_artifact_bytes: int = Field(
        default=DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES,
        gt=0,
        le=MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES,
        description="单个 HLS 产物上传的最大字节数",
    )


class RemoteTranscodeConfigView(BaseModel):
    """网页展示的远程转码配置。"""

    enabled: bool
    base_url: str = Field(
        default="", description="静态配置出的根地址；空表示自动使用 Worker 连上来的地址"
    )
    base_url_override: str = Field(
        default="", description="网页配置的覆盖地址；空表示不覆盖"
    )
    base_url_source: RemoteTranscodeBaseUrlSource
    max_artifact_bytes: int
    ready: bool = Field(description="开关已开，且填过的覆盖地址（如果填了）合法")
    issues: list[str] = Field(
        default_factory=list,
        description="覆盖地址的格式问题；地址留空不算问题，不包含任何令牌内容",
    )


__all__ = [
    "RemoteTranscodeConfigPayload",
    "RemoteTranscodeConfigView",
    "RemoteTranscodeBaseUrlSource",
]
