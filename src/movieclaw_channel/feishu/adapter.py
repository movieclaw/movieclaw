"""FeishuAdapter —— 飞书群自定义机器人的纯推送实现。

自定义机器人没有收消息能力(不存在 bot 会话),run() 只是一个等待停止的
空转循环:ChannelManager 要求每个账号有一个可守护的「收消息任务」,这里
用它满足生命周期契约,真正的价值在 send_text——推送扇出经 dispatcher
发送泵到达这里。

不实现 send_photo:飞书 Webhook 发图要先走开放平台上传接口换 image_key,
自定义机器人没有该凭据,发送泵探测不到 send_photo 会自动退回纯文本
(与微信同款降级语义)。
"""

from __future__ import annotations

import asyncio

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.feishu.client import FeishuClient
from movieclaw_channel.types import ReplyContext

CHANNEL_ID = "feishu"


class FeishuAdapter:
    """飞书通道适配器(单机器人,推送-only)。"""

    channel_id = CHANNEL_ID
    #: 纯文本分片上限:远小于 Webhook 消息体上限,推送文案足够用且留足余量
    max_text_len = 4000

    def __init__(self, client: FeishuClient, account_id: str) -> None:
        self._client = client
        self._account_id = account_id

    async def run(self, ctx: ChannelContext) -> None:
        # 无收消息循环:挂起直到账号被停止/解绑(满足 manager 的守护契约)
        stop = ctx.stop or asyncio.Event()
        await stop.wait()

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        # Webhook 地址由客户端持有,与 reply 无关:群维度推送,没有私聊目标
        await self._client.send_text(text)
