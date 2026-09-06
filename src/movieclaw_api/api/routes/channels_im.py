"""IM 通道接口(Telegram / Discord 的配对码绑定与账号管理)。

前端(设置 → 消息推送)交互流程:
1. 进页面 GET accounts 展示两个平台的绑定列表;
2. 填 bot token POST bindings → 返回 6 位配对码;
3. 用户去 TG/Discord 私聊 bot 发配对码;前端每 2 秒 GET bindings/{id} 轮询;
4. confirmed → 刷新列表(通道已在收发,发码人即白名单与推送目标)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.channels import (
    ChannelPushConfigView,
    ImAccountView,
    ImBindingView,
    ImBindTokenPayload,
    PushTestPayload,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.channel_push import push_to_all_channels
from movieclaw_api.services.im_channel import (
    IM_CHANNEL_IDS,
    ImChannelId,
    PairChallenge,
    get_im_channels,
)
from movieclaw_api.settings import ChannelPushSetting, get_setting_store
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository
from movieclaw_db.repositories.llm_provider_repo import LlmProviderRepository

router = APIRouter(prefix="/channels/im", tags=["channels"])


def _require_channel(channel: str) -> ImChannelId:
    if channel not in IM_CHANNEL_IDS:
        raise NotFoundException(f"未知通道:{channel}")
    return channel  # type: ignore[return-value]


async def _binding_view(challenge: PairChallenge, session: AsyncSession) -> ImBindingView:
    account: ImAccountView | None = None
    if challenge.status == "confirmed":
        row = await ChannelAccountRepository(session).get(challenge.bot_id)
        if row is not None:
            service = get_im_channels()
            account = ImAccountView.from_model(
                row, running=service.manager.is_running(challenge.channel_id, row.account_id)
            )
    return ImBindingView(
        challenge_id=challenge.challenge_id,
        status=challenge.status,
        pair_code=challenge.pair_code,
        bot_name=challenge.bot_name,
        message=challenge.message,
        account=account,
    )


@router.get(
    "/{channel}/accounts",
    response_model=ApiResponse[list[ImAccountView]],
    summary="已绑定的 TG/Discord 账号列表",
    operation_id="channels.im.accounts.list",
)
async def list_im_accounts(
    channel: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ImAccountView]]:
    channel_id = _require_channel(channel)
    service = get_im_channels()
    rows = await ChannelAccountRepository(session).list_by_channel(channel_id)
    return ok(
        [
            ImAccountView.from_model(
                row, running=service.manager.is_running(channel_id, row.account_id)
            )
            for row in rows
        ]
    )


@router.post(
    "/{channel}/bindings",
    response_model=ApiResponse[ImBindingView],
    status_code=201,
    summary="发起配对绑定(提交 bot token,返回配对码)",
    operation_id="channels.im.bindings.start",
)
async def start_im_binding(
    channel: str,
    payload: ImBindTokenPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ImBindingView]:
    """校验 token 并生成配对码;用户私聊 bot 发码即完成绑定。

    前置:必须已配置 AI 模型(与微信绑定同口径)。
    """
    channel_id = _require_channel(channel)
    if not await LlmProviderRepository(session).has_any():
        raise BadRequestException(
            "绑定需要先完成 AI 模型配置:请先在「设置 → AI 模型」接入模型供应商,再进行绑定"
        )
    try:
        challenge = await get_im_channels().begin_binding(channel_id, payload.token.strip())
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- 网络不可达等,转成中文业务错误
        raise BadRequestException(f"发起绑定失败:{exc}") from exc
    return ok(await _binding_view(challenge, session), message="绑定已发起")


@router.get(
    "/{channel}/bindings/{challenge_id}",
    response_model=ApiResponse[ImBindingView],
    summary="查询配对状态(前端轮询)",
    operation_id="channels.im.bindings.status",
)
async def get_im_binding_status(
    channel: str,
    challenge_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ImBindingView]:
    _require_channel(channel)
    challenge = get_im_channels().get_challenge(challenge_id)
    if challenge is None:
        raise NotFoundException("绑定流程不存在或已过期,请重新发起")
    return ok(await _binding_view(challenge, session))


@router.delete(
    "/{channel}/accounts/{account_id}",
    response_model=ApiResponse[dict],
    summary="解绑 TG/Discord 账号",
    operation_id="channels.im.accounts.unbind",
    # 解绑删除凭据、停止通道,须二次确认(CLI 据此决定 --yes 门槛)
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def unbind_im_account(channel: str, account_id: str) -> ApiResponse[dict]:
    channel_id = _require_channel(channel)
    removed = await get_im_channels().unbind(channel_id, account_id)
    if not removed:
        raise NotFoundException("账号不存在或已解绑")
    return ok({}, message="已解绑")


@router.post(
    "/push-test",
    response_model=ApiResponse[dict],
    summary="向所有已绑定通道发送测试推送",
    operation_id="channels.im.push.test",
)
async def send_test_push(payload: PushTestPayload) -> ApiResponse[dict]:
    text = payload.text.strip() or "📣 这是一条来自 movieclaw 的测试推送。收到说明通道工作正常!"
    sent = await push_to_all_channels(text)
    if sent == 0:
        raise BadRequestException("没有可推送的通道账号:请先完成绑定且确保通道在运行")
    return ok({"sent": sent}, message=f"已推送到 {sent} 个账号")


@router.get(
    "/push-config",
    response_model=ApiResponse[ChannelPushConfigView],
    summary="读取推送内容开关",
    operation_id="channels.im.push.config.get",
)
async def get_push_config() -> ApiResponse[ChannelPushConfigView]:
    setting = await get_setting_store().get(ChannelPushSetting)
    return ok(
        ChannelPushConfigView(
            push_dispatch=setting.push_dispatch, push_imported=setting.push_imported
        )
    )


@router.put(
    "/push-config",
    response_model=ApiResponse[ChannelPushConfigView],
    summary="保存推送内容开关",
    operation_id="channels.im.push.config.update",
)
async def update_push_config(payload: ChannelPushConfigView) -> ApiResponse[ChannelPushConfigView]:
    await get_setting_store().set(
        ChannelPushSetting(push_dispatch=payload.push_dispatch, push_imported=payload.push_imported)
    )
    return ok(payload, message="推送设置已保存")
