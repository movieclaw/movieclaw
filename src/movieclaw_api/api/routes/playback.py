"""播放记录的 Web 业务接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_admin, require_login
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.playback import (
    MediaActivityView,
    PlaybackDecideRequest,
    PlaybackDecisionView,
    RecentWatchView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.services.playback import plan as playback_plan
from movieclaw_api.services.playback_activity import media_activity_overview, revoke_device
from movieclaw_api.services.playback_recent import recent_watch_items
from movieclaw_db.engine import get_session
from movieclaw_playback.decide import PlaybackTier as Tier

router = APIRouter(prefix="/playback", tags=["playback"])


@router.get(
    "/recent",
    response_model=ApiResponse[RecentWatchView],
    summary="最近观看",
    operation_id="playback.recent",
    openapi_extra={"x-cli-hidden": True},
)
async def list_recent_watch(
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RecentWatchView]:
    """列出当前账号在可见媒体库中的最近观看作品。"""
    visible_ids = await visible_library_ids(session, principal)
    member_id = principal.member_id if principal.member_id is not None else 0
    items = await recent_watch_items(
        session,
        member_id=member_id,
        visible_library_ids=visible_ids,
        limit=limit,
    )
    return ok(RecentWatchView(items=items))


@router.get(
    "/activity",
    response_model=ApiResponse[MediaActivityView],
    summary="媒体库活动快照",
    operation_id="playback.activity",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-hidden": True},
)
async def get_media_activity(
    recent_limit: Annotated[int, Query(ge=1, le=100)] = 30,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[MediaActivityView]:
    """活动页「观看」视角：正在播放/下载、设备清单与全成员最近观看。

    管理员运维视角（跨成员可见），与首页按成员隔离的最近观看接口分离。
    """
    return ok(await media_activity_overview(session, recent_limit=recent_limit))


@router.delete(
    "/devices/{device_id}",
    response_model=ApiResponse[None],
    summary="注销播放器设备",
    operation_id="playback.device.revoke",
    dependencies=[Depends(require_admin)],
    # confirm 而非 destructive：注销会中断该设备正在进行的播放/下载并要求重新
    # 登录，但不销毁任何数据——观看进度、收藏按成员保存，与设备无关。
    openapi_extra={"x-cli-hidden": True, "x-cli-dangerous": "confirm"},
)
async def revoke_playback_device(
    device_id: Annotated[str, Path(min_length=1, max_length=256)],
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    """注销一台播放器设备：凭据即刻失效，正在进行的播放与取流一并停止。

    该设备下次访问需重新登录；已看进度、收藏等观看状态按成员保存，不受影响。
    """
    label = await revoke_device(session, device_id)
    if label is None:
        raise NotFoundException("设备不存在或已注销")
    return ok(None, message=f"已注销「{label}」，该设备需重新登录")


@router.post(
    "/decide",
    response_model=ApiResponse[PlaybackDecisionView],
    summary="播放决策",
    operation_id="playback.decide",
    openapi_extra={"x-cli-hidden": True},
)
async def decide_playback_route(
    payload: PlaybackDecideRequest,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackDecisionView]:
    """算出「这部片在你的浏览器上该怎么放」（docs/design/web-player.md §3）。

    请求带上客户端的解码能力快照，服务端结合入库时 ffprobe 落下的规格真值，
    在五档降级阶梯里取能成立的最小档——尽可能不转码。

    返回三态：``plan`` 可以播；``consent`` 需要用户同意开启软件转码；
    ``rejected`` 放不了，附中文原因与下一步建议。
    """
    visible = await visible_library_ids(session, principal)
    capability = playback_plan.capability_from_request(payload.capability)
    failed = frozenset(Tier(t) for t in payload.failed_tiers if t in Tier._value2member_map_)

    if payload.file_id is not None:
        decision = await playback_plan.decide_for_file(
            session,
            payload.file_id,
            capability,
            can_self_enable=principal.is_admin,
            failed_tiers=failed,
            visible_library_ids=visible,
        )
    elif payload.media_item_id is not None:
        files = await playback_plan.library_files_for_unit(
            session,
            payload.media_item_id,
            payload.season_number,
            payload.episode_number,
            visible_library_ids=visible,
        )
        decision = await playback_plan.decide_for_files(
            files,
            capability,
            can_self_enable=principal.is_admin,
            failed_tiers=failed,
        )
    else:
        raise BadRequestException("需要提供 file_id 或 media_item_id")

    if decision is None:
        raise NotFoundException("没有找到可播放的文件")
    return ok(playback_plan.to_view(decision))
