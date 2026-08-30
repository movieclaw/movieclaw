"""设置健康聚合接口：一次下发各设置分区的异常/待办计数。

设计动机（新手体验第三期：异常状态外显到设置侧栏）：前端要在设置侧栏给
分区行点角标，若逐分区各拉各的接口，管理员每开一次设置就是一排请求再乘上
轮询——这里把「哪个分区有事」压缩成一个只读聚合，前端低频轮询它一个就够。

判定口径与各分区页面**同源**，不另造一套体检逻辑（否则迟早出现「侧栏亮点、
点进去却一切正常」的口径漂移）：

- 站点 / 下载器：``ConfigStatus.FAILED``——分区列表把它渲染成「验证失败」
  的同一判定（订阅链路体检的站点/下载器段用的也是同一状态机）；
- 推送通道：``ChannelAccountStatus.STALE``——绑定页「需重新绑定」的同一
  语义，微信 / Telegram / Discord 共用一张 channel_account 表；
- 设备：待批准的接入请求数，与设备分区审批卡的数据源
  （auth.list_device_requests）同一份内存清单。

异常与待办分开计数：异常是「出事了要修」（前端红点），待办是「有人在等你
操作」（前端蓝点）——语义不同，绝不混成一种颜色。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.settings_health import SettingsHealthView
from movieclaw_api.services import auth as auth_service
from movieclaw_db.engine import get_session
from movieclaw_db.models.channel_account import ChannelAccount, ChannelAccountStatus
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus, SiteCredential

router = APIRouter(prefix="/settings", tags=["settings"])


async def _count(session: AsyncSession, model: type, *conditions: object) -> int:
    """按条件数行。空表 / 无命中都返回 0，绝不返回 None。"""
    result = await session.scalar(select(func.count()).select_from(model).where(*conditions))
    return int(result or 0)


@router.get(
    "/health",
    response_model=ApiResponse[SettingsHealthView],
    summary="聚合各设置分区的异常与待办计数（侧栏角标数据源）",
    operation_id="settings.health",
    # 纯前端角标数据源：CLI / Agent 用各分区自己的接口看详情即可
    openapi_extra={"x-cli-hidden": True},
)
async def settings_health(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SettingsHealthView]:
    """管理员专属（挂载在管理区）。只读聚合，前端 10 分钟级低频轮询。"""
    return ok(
        SettingsHealthView(
            sites_failed=await _count(
                session, SiteCredential, SiteCredential.status == ConfigStatus.FAILED
            ),
            downloaders_failed=await _count(
                session, DownloaderClient, DownloaderClient.status == ConfigStatus.FAILED
            ),
            im_push_need_rebind=await _count(
                session, ChannelAccount, ChannelAccount.status == ChannelAccountStatus.STALE
            ),
            device_requests_pending=len(auth_service.list_device_requests()),
        )
    )
