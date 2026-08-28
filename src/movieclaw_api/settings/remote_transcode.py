"""远程转码 Worker 的持久化配置域。"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
# Worker 的内存上传代理也以此为硬上限；服务端不能允许保存一个 Worker
# 永远无法接收的更大值，否则配置看似成功，播放时才会在上传阶段失败。
MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES = DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES


@register_setting(
    namespace="playback.remote_transcode",
    title="远程转码",
    secret_fields=["worker_token"],
)
class RemoteTranscodeSetting(SettingSchema):
    """远程转码的网页配置。

    ``base_url`` 是可选的远程转码专用根地址；留空时跟随系统「外部访问地址」，
    需要让 Worker 直连服务端内网端口时，可以只为远程转码设置另一条入口。
    """

    enabled: bool = Field(default=False, description="是否启用远程硬件转码")
    worker_token: str = Field(default="", description="Worker 控制面共享令牌")
    base_url: str = Field(
        default="",
        description="远程转码专用根地址；为空时使用系统外部访问地址",
    )
    max_artifact_bytes: int = Field(
        default=DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES,
        gt=0,
        description="单个 HLS 产物上传的最大字节数",
    )


__all__ = [
    "DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES",
    "MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES",
    "RemoteTranscodeSetting",
]
