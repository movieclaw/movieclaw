"""设置健康聚合视图（GET /settings/health 的响应体）。"""

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class SettingsHealthView(BaseModel):
    """各设置分区的异常/待办计数，一次下发给前端画侧栏角标。

    语义分两类，前端据此选角标颜色：
    - **异常**（红点，出事了要修）：sites_failed / downloaders_failed /
      im_push_need_rebind；
    - **待办**（蓝点，有人在等你操作）：device_requests_pending。
    """

    sites_failed: int = Field(description="验证失败的资源站点数（status=failed）")
    downloaders_failed: int = Field(description="连接失败的下载器数（status=failed）")
    im_push_need_rebind: int = Field(
        description="需重新绑定的推送通道账号数（微信/TG/Discord 的 stale 账号）"
    )
    device_requests_pending: int = Field(description="待批准的设备接入请求数")
