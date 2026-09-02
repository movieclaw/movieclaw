"""网络与代理设置的请求/响应模型（「设置 → 网络」页）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class EgressServiceOption(BaseModel):
    """设置页「走代理」开关列表里的一项。"""

    id: str
    label: str
    description: str = ""


class NetworkConfigPayload(BaseModel):
    """保存请求体：与配置域字段一一对应。

    整体覆盖语义：保存会替换全部字段（未传字段按默认值处理），
    建议先读取当前配置、改动后整体写回。
    """

    proxy_mode: Literal["off", "env", "manual"] = Field(
        default="env",
        description="代理模式：off=全部直连；env=代理地址取自环境变量；"
        "manual=手动填写（需同时给 proxy_url）",
    )
    proxy_url: str = Field(
        default="",
        description="manual 模式的代理地址，如 http://127.0.0.1:7890 或 socks5://…（可含账号密码）",
    )
    proxy_services: list[str] = Field(
        default_factory=list,
        description="走代理的服务 id 列表：tmdb / image / douban / llm / telegram / "
        "discord / webhook / github / site:<站点id>；不在列表内的服务直连",
    )
    tmdb_api_base_url: str = Field(
        default="", description="TMDB 接口镜像地址；留空用默认（见读取接口的 mirror_defaults）"
    )
    tmdb_image_base_url: str = Field(default="", description="TMDB 图床镜像地址；留空用默认")
    douban_api_base_url: str = Field(default="", description="豆瓣接口镜像地址；留空用默认")


class NetworkConfigView(NetworkConfigPayload):
    """读取响应：配置本体 + 前端渲染所需的目录与默认值。"""

    services: list[EgressServiceOption] = Field(default_factory=list)
    mirror_defaults: dict[str, str] = Field(
        default_factory=dict, description="三个镜像地址的生效默认值（设置为空时的回落）"
    )
    env_proxy_detected: str = Field(
        default="", description="环境变量中探测到的代理地址；env 模式下供用户确认"
    )


class NetworkTestPayload(BaseModel):
    service: str = Field(
        min_length=1,
        max_length=100,
        description="要测试连通性的服务 id：tmdb / image / douban / llm / telegram / "
        "discord / webhook / github / site:<站点id>",
    )


class NetworkTestResult(BaseModel):
    ok: bool
    latency_ms: int | None = None
    message: str
