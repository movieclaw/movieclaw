"""IM 通道接口(微信绑定与账号管理)。

前端(设置 → AI → 微信绑定)交互流程:
1. 进页面先 GET accounts:非空 → 只展示列表;为空 → 自动 POST bindings 出码;
2. 拿到 challenge_id 后每 1-2 秒 GET bindings/{id} 轮询状态;
3. need_verify_code → 页面弹输入框,POST verify-code 后继续轮询;
4. confirmed → 二维码消失,刷新列表(此时通道已在收发,扫码人可直接对话)。
"""

from __future__ import annotations

import base64
from functools import lru_cache

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.channels import (
    WeixinAccountView,
    WeixinBindingStartView,
    WeixinBindingStatusView,
    WeixinVerifyCodePayload,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.weixin_channel import get_weixin_channel
from movieclaw_channel.weixin.adapter import CHANNEL_ID
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository
from movieclaw_db.repositories.llm_provider_repo import LlmProviderRepository

router = APIRouter(prefix="/channels/weixin", tags=["channels"])


@lru_cache(maxsize=8)
def _qrcode_data_url(content: str) -> str:
    """把二维码内容渲染成 SVG data URL,前端 <img> 直接显示。

    纯 Python SVG 路径(不依赖 pillow);缓存最近几张——同一 challenge 的
    poll 每 1-2 秒来一次,内容不变时不必重复编码。
    """
    if not content:
        return ""
    img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    return "data:image/svg+xml;base64," + base64.b64encode(img.to_string()).decode()


@router.get(
    "/accounts",
    response_model=ApiResponse[list[WeixinAccountView]],
    summary="已绑定的微信账号列表",
    operation_id="channels.weixin.accounts.list",
)
async def list_weixin_accounts(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[WeixinAccountView]]:
    service = get_weixin_channel()
    rows = await ChannelAccountRepository(session).list_by_channel(CHANNEL_ID)
    return ok(
        [
            WeixinAccountView.from_model(
                row, running=service.manager.is_running(CHANNEL_ID, row.account_id)
            )
            for row in rows
        ]
    )


@router.post(
    "/bindings",
    response_model=ApiResponse[WeixinBindingStartView],
    status_code=201,
    summary="发起微信扫码绑定",
    operation_id="channels.weixin.bindings.start",
)
async def start_weixin_binding(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WeixinBindingStartView]:
    """获取二维码并启动后台状态轮询;前端拿 challenge_id 开始 poll。

    前置:必须已配置 AI 模型——微信侧对话完全由模型驱动,没配置时绑定了
    也无法使用,不如在入口拦下并引导去设置(与 acquire_llm_router 同口径,
    只看是否已配置,连通性验证失败仍放行由运行时报错)。
    """
    if not await LlmProviderRepository(session).has_any():
        raise BadRequestException(
            "微信绑定需要先完成 AI 模型配置:请先在「设置 → AI 模型」接入模型供应商,再进行绑定"
        )
    try:
        challenge = await get_weixin_channel().binding.begin()
    except Exception as exc:  # noqa: BLE001 -- 网关不可达等,转成中文业务错误
        raise BadRequestException(f"发起微信绑定失败:{exc}") from exc
    return ok(
        WeixinBindingStartView(
            challenge_id=challenge.challenge_id,
            qrcode_url=challenge.qrcode_url,
            qrcode_image=_qrcode_data_url(challenge.qrcode_url),
            message=challenge.message,
        ),
        message="绑定已发起",
    )


@router.get(
    "/bindings/{challenge_id}",
    response_model=ApiResponse[WeixinBindingStatusView],
    summary="查询绑定状态(前端轮询)",
    operation_id="channels.weixin.bindings.status",
)
async def get_weixin_binding_status(
    challenge_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WeixinBindingStatusView]:
    service = get_weixin_channel()
    challenge = service.binding.get(challenge_id)
    if challenge is None:
        raise NotFoundException("绑定流程不存在或已过期,请重新发起")

    account_view: WeixinAccountView | None = None
    if challenge.status == "confirmed" and challenge.result is not None:
        row = await ChannelAccountRepository(session).get(challenge.result.bot_id)
        if row is not None:
            account_view = WeixinAccountView.from_model(
                row, running=service.manager.is_running(CHANNEL_ID, row.account_id)
            )
    return ok(
        WeixinBindingStatusView(
            challenge_id=challenge.challenge_id,
            status=challenge.status,
            message=challenge.message,
            qrcode_url=challenge.qrcode_url,
            qrcode_image=_qrcode_data_url(challenge.qrcode_url),
            account=account_view,
        )
    )


@router.post(
    "/bindings/{challenge_id}/verify-code",
    response_model=ApiResponse[dict],
    summary="提交扫码配对数字",
    operation_id="channels.weixin.bindings.verify",
)
async def submit_weixin_verify_code(
    challenge_id: str, payload: WeixinVerifyCodePayload
) -> ApiResponse[dict]:
    accepted = get_weixin_channel().binding.submit_verify_code(challenge_id, payload.code)
    if not accepted:
        raise NotFoundException("绑定流程不存在或已结束,请重新发起")
    return ok({}, message="配对码已提交,正在校验")


@router.delete(
    "/accounts/{account_id}",
    response_model=ApiResponse[dict],
    summary="解绑微信账号",
    operation_id="channels.weixin.accounts.unbind",
    # 解绑删除凭据、停止通道,须二次确认(CLI 据此决定 --yes 门槛)
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def unbind_weixin_account(account_id: str) -> ApiResponse[dict]:
    """停止收发循环并删除凭据;之后该 bot 需重新扫码才能使用。"""
    removed = await get_weixin_channel().unbind(account_id)
    if not removed:
        raise NotFoundException("账号不存在或已解绑")
    return ok({}, message="已解绑")
