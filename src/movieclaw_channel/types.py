"""通道层的平台无关 DTO。

设计要点:``ReplyContext.token`` 是**通道私有的不透明字典**——微信往里放
context_token,未来别的通道放别的;通用层(dispatcher/pusher/服务编排)
永远不解读它,只原样带回给同一个 adapter。这是把 iLink 私有协议细节封死
在微信适配器内部的关键约定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


class ChannelAuthError(Exception):
    """通道凭据失效(如微信 errcode -14 token 过期)。

    adapter 在收发循环里抛出本异常时,manager 会停止该账号并回调服务层
    把账号标记为 stale——用户须重新扫码绑定,而不是无脑重试。
    """


@dataclass(frozen=True, slots=True)
class ReplyContext:
    """定位「往哪里回消息」所需的全部信息。"""

    channel_id: str
    account_id: str
    #: 平台内用户标识,如微信的 xxx@im.wechat
    user_id: str
    #: 通道私有回带数据(微信: {"context_token": "..."}),通用层不解读
    token: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundImage:
    """随入站消息附带的一张图片(已由 adapter 下载/解密成明文字节)。

    只带字节与展示名:真实图片类型由 API 层落盘时按魔数嗅探判定,平台声明的
    Content-Type/扩展名一律不信(见 agent_attachments.sniff_image_mime)。
    """

    data: bytes
    #: 展示名(前端 chip 与模型的附件清单文案用),如「微信图片」
    name: str = "图片"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """归一化后的入站消息(adapter 产出,dispatcher 消费)。"""

    channel_id: str
    account_id: str
    user_id: str
    #: 文本正文(语音消息取平台转写文字)
    text: str
    reply: ReplyContext
    #: 平台侧消息标识,用于幂等去重(getUpdates 游标是至少一次投递)
    provider_message_id: str
    timestamp_ms: int = 0
    #: 随消息附带的图片(纯图消息 text 为空);adapter 下载解密后填充,
    #: 服务层落进会话附件目录再喂给视觉模型(docs/design/agent-image-input.md)
    images: tuple[InboundImage, ...] = ()

    @property
    def has_content(self) -> bool:
        """是否有可交给 Agent 的内容(文字或图片)。"""
        return bool(self.text.strip() or self.images)

    @property
    def session_key(self) -> str:
        """会话键:同一账号同一用户 = 一条串行处理的会话。"""
        return f"{self.channel_id}:{self.account_id}:{self.user_id}"


@dataclass(frozen=True, slots=True)
class OutboundEnvelope:
    """出站信封:发送泵唯一认识的载荷。

    Agent 回复、命令回执、主动推送(下载/入库通知等)都走这一个口,
    保证同账号出站顺序与限流集中在发送泵一处。
    """

    reply: ReplyContext
    text: str
    #: 来源标记,仅用于日志排障
    origin: Literal["agent", "system", "push"] = "agent"
    #: 随消息附带的图片字节(主动推送的海报等);adapter 不支持发图时
    #: 发送泵自动退回纯文本——图可以丢,文字不能丢
    photo: bytes | None = None
