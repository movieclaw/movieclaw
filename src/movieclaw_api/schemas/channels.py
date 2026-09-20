"""IM 通道(微信绑定)相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel
from movieclaw_db.models.channel_account import ChannelAccount


class WeixinAccountView(BaseModel):
    """已绑定账号(绑定页列表项)。"""

    account_id: str
    #: 扫码绑定人的微信用户 id(白名单)
    bound_user_id: str | None
    #: active=正常;stale=凭据失效,需重新扫码
    status: str
    #: 当前进程内收发循环是否在运行
    running: bool
    last_error: str | None
    bound_at: datetime

    @classmethod
    def from_model(cls, row: ChannelAccount, *, running: bool) -> WeixinAccountView:
        return cls(
            account_id=row.account_id,
            bound_user_id=row.bound_user_id,
            status=row.status,
            running=running,
            last_error=row.last_error,
            bound_at=row.created_at,
        )


class WeixinBindingStartView(BaseModel):
    """发起绑定的返回:前端渲染二维码并开始 poll。"""

    challenge_id: str
    #: 二维码内容链接(扫码目标;备用展示)
    qrcode_url: str
    #: 服务端渲染好的二维码图(SVG data URL),前端 <img> 直接显示
    qrcode_image: str
    message: str


class WeixinBindingStatusView(BaseModel):
    """绑定状态快照(前端每 1-2 秒 poll 一次,后端只读内存,毫秒级返回)。

    status 取值:
    pending / scanned / need_verify_code / confirmed / already_bound /
    expired / failed。confirmed 时通道已启动,account 字段附带账号信息。
    """

    challenge_id: str
    status: str
    message: str
    #: 二维码可能中途刷新(过期自动换新),前端每次 poll 都以此为准重绘
    qrcode_url: str
    qrcode_image: str
    account: WeixinAccountView | None = None


class WeixinVerifyCodePayload(BaseModel):
    """提交手机微信上显示的配对数字。"""

    code: str = Field(min_length=1, max_length=16, description="配对码")


# ---------------------------------------------------------------------------
# Telegram / Discord(配对码绑定)
# ---------------------------------------------------------------------------


class ImAccountView(BaseModel):
    """已绑定的 TG/Discord bot 账号(绑定页列表项)。"""

    channel_id: str
    account_id: str
    #: 完成配对的用户 id(白名单,同时是推送目标)
    bound_user_id: str | None
    status: str
    running: bool
    last_error: str | None
    bound_at: datetime

    @classmethod
    def from_model(cls, row: ChannelAccount, *, running: bool) -> ImAccountView:
        return cls(
            channel_id=row.channel_id,
            account_id=row.account_id,
            bound_user_id=row.bound_user_id,
            status=row.status,
            running=running,
            last_error=row.last_error,
            bound_at=row.created_at,
        )


class ImBindTokenPayload(BaseModel):
    """发起绑定:提交 bot token。"""

    token: str = Field(min_length=8, max_length=256, description="bot token")


class FeishuBindPayload(BaseModel):
    """接入飞书群自定义机器人(粘贴 Webhook 地址即绑即用,无配对码)。"""

    webhook_url: str = Field(
        min_length=16, max_length=512, description="飞书自定义机器人 Webhook 地址"
    )
    secret: str = Field(default="", max_length=256, description="签名校验密钥;未开启签名校验留空")


class ImBindingView(BaseModel):
    """配对绑定状态(发起返回 + 前端 poll 同一结构)。

    status 取值:pending / confirmed / expired / failed。
    """

    challenge_id: str
    status: str
    #: 面板展示给用户的 6 位配对码(用户私聊 bot 发这串数字完成绑定)
    pair_code: str
    bot_name: str
    message: str
    account: ImAccountView | None = None


class PushTestPayload(BaseModel):
    """测试推送文本(缺省用默认文案)。"""

    text: str = Field(default="", max_length=500)


class ChannelPushConfigView(BaseModel):
    """推送内容开关(GET 返回与 PUT 载荷同构)。"""

    push_dispatch: bool = Field(description="订阅开始下载时推送")
    push_imported: bool = Field(description="入库完成时推送")
