"""movieclaw_channel —— IM 通道模块(微信为首个实现)。

把外部 IM 平台(微信 iLink Bot 网关等)接入 movieclaw 的 Agent 能力。
分层设计(docs 讨论定稿):

- ``types``      平台无关的消息 DTO(入站消息 / 回复上下文 / 出站信封);
- ``adapter``    通道适配器协议:一个平台只需实现「收消息循环 + 发文本」;
- ``pusher``     StepReplyPusher:把 AgentRunner 事件流按「步」收敛成离散 IM 消息;
- ``dispatcher`` 事件驱动的收发解耦:会话串行队列(入站) + 出站发送泵;
- ``manager``    每账号一个后台任务的生命周期管理(启动/停止/崩溃重启);
- ``weixin``     微信 iLink 适配器(扫码绑定 / getUpdates 长轮询 / sendMessage)。

依赖方向:本包只依赖 movieclaw_agent 的事件类型,不反向依赖 API 层;
Agent 的装配(LLM 路由、工具集、会话历史)由 API 服务层以回调注入。
"""

from movieclaw_channel.adapter import ChannelAdapter, ChannelContext
from movieclaw_channel.dispatcher import ChannelDispatcher, split_message
from movieclaw_channel.manager import ChannelManager
from movieclaw_channel.pusher import StepReplyPusher
from movieclaw_channel.types import (
    ChannelAuthError,
    InboundImage,
    InboundMessage,
    OutboundEnvelope,
    ReplyContext,
)

__all__ = [
    "ChannelAdapter",
    "ChannelAuthError",
    "ChannelContext",
    "ChannelDispatcher",
    "ChannelManager",
    "InboundImage",
    "InboundMessage",
    "OutboundEnvelope",
    "ReplyContext",
    "StepReplyPusher",
    "split_message",
]
