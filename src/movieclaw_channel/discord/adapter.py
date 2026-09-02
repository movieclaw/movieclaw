"""DiscordAdapter —— 实现通道协议的 Discord 收发实现。

收消息走 Gateway websocket(Bot API 没有 HTTP 长轮询收消息的口):
Hello → Identify → 心跳保活 → MESSAGE_CREATE 事件。刻意不做 RESUME——
断线后直接重新 Identify,本场景消息量极小,丢 RESUME 换实现简单。

只处理私聊(无 guild_id 的消息):私聊内容不需要 MESSAGE CONTENT 特权
intent,用户在开发者后台建 bot 时零额外配置。官方文档明确 content /
attachments 等字段的特权限制对「与本 app 的私聊」豁免,所以私聊里的图片
附件同样能收到。发消息走 REST(client.py)。

代理:websocket 连接按 egress 配置解析 "discord" 标签的代理地址,
与 REST 同一开关(websockets 库原生支持 http/socks 代理)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from dataclasses import replace
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.discord.client import DiscordClient
from movieclaw_channel.types import (
    ChannelAuthError,
    InboundImage,
    InboundMessage,
    ReplyContext,
)
from movieclaw_net import resolve_proxy_url

logger = logging.getLogger("movieclaw_channel.discord.adapter")

CHANNEL_ID = "discord"

_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
#: DIRECT_MESSAGES;私聊内容不需要 MESSAGE_CONTENT 特权 intent
_INTENTS = 1 << 12
#: 凭据级关闭码:4004 鉴权失败;4014 intents 未被允许(理论上不会,兜底)
_AUTH_CLOSE_CODES = (4004, 4014)
_RECONNECT_DELAY_S = 5.0

_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10

#: 没有 content_type 时兜底认扩展名(官方文档里该字段是可选的)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _collect_image_attachments(data: dict[str, Any]) -> list[tuple[str, str]]:
    """挑出消息里的图片附件,返回 [(下载 url, 文件名)]。

    ``content_type`` 是官方给的媒体类型,优先按它过滤;字段缺失时退回扩展名。
    真实类型最终仍由入库时的魔数嗅探判定,这里只做粗筛。
    """
    out: list[tuple[str, str]] = []
    for item in data.get("attachments") or []:
        url = str(item.get("url") or item.get("proxy_url") or "")
        name = str(item.get("filename") or "Discord 图片")
        if not url:
            continue
        content_type = str(item.get("content_type") or "")
        if content_type.startswith("image/") or (
            not content_type and name.lower().endswith(_IMAGE_SUFFIXES)
        ):
            out.append((url, name))
    return out


class DiscordAdapter:
    """Discord 通道适配器(单 bot)。"""

    channel_id = CHANNEL_ID
    #: Discord 单条消息 2000 字符上限,留余量
    max_text_len = 1900

    def __init__(self, client: DiscordClient, account_id: str) -> None:
        self._client = client
        self._account_id = account_id

    async def run(self, ctx: ChannelContext) -> None:
        stop = ctx.stop or asyncio.Event()
        logger.info("Discord Gateway 循环启动 account=%s", self._account_id)
        while not stop.is_set():
            try:
                await self._connect_once(ctx, stop)
                # op 7/9 等服务端指示的正常返回也要退避:Identify 有严格限频
                # (官方要求 5 秒 1 次),无延迟紧循环重连会被 Gateway 拉黑
                if not stop.is_set():
                    delay = 1.0 + random.random() * 4.0
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=delay)
            except ChannelAuthError:
                raise
            except Exception as exc:  # noqa: BLE001 -- 网络抖动统一退避重连
                logger.warning("Discord Gateway 断开,%s 秒后重连:%s", _RECONNECT_DELAY_S, exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_RECONNECT_DELAY_S)
        logger.info("Discord Gateway 循环退出 account=%s", self._account_id)

    async def _connect_once(self, ctx: ChannelContext, stop: asyncio.Event) -> None:
        """一次完整的 Gateway 会话:连接 → Identify → 收事件直到断开或 stop。"""
        proxy = resolve_proxy_url("discord")
        # proxy 参数要求 websockets>=14,已在 pyproject 显式声明
        ws_ctx = connect(_GATEWAY_URL, proxy=proxy, open_timeout=15, close_timeout=5)

        async with ws_ctx as ws:
            heartbeat_task: asyncio.Task[None] | None = None
            #: 最近收到的事件序号(心跳携带);用单元素列表让心跳任务读到最新值
            seq_holder: list[int | None] = [None]
            try:
                while not stop.is_set():
                    recv = asyncio.ensure_future(ws.recv())
                    stop_wait = asyncio.ensure_future(stop.wait())
                    done, _ = await asyncio.wait(
                        {recv, stop_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                    stop_wait.cancel()
                    if recv not in done:
                        recv.cancel()
                        return
                    payload = json.loads(recv.result())

                    op = payload.get("op")
                    if payload.get("s") is not None:
                        seq_holder[0] = payload["s"]

                    if op == _OP_HELLO:
                        interval = payload["d"]["heartbeat_interval"] / 1000
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat(ws, interval, seq_holder),
                            name=f"discord-heartbeat-{self._account_id}",
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "op": _OP_IDENTIFY,
                                    "d": {
                                        "token": self._client.token,
                                        "intents": _INTENTS,
                                        "properties": {
                                            "os": "linux",
                                            "browser": "movieclaw",
                                            "device": "movieclaw",
                                        },
                                    },
                                }
                            )
                        )
                    elif op == _OP_DISPATCH and payload.get("t") == "MESSAGE_CREATE":
                        data = payload.get("d") or {}
                        msg = self._normalize(data)
                        if msg is not None:
                            await ctx.on_inbound(await self._with_images(data, msg))
                    elif op in (_OP_RECONNECT, _OP_INVALID_SESSION):
                        logger.info("Discord 要求重连(op=%s)", op)
                        return
                    # HEARTBEAT_ACK 及其他事件忽略
            except ConnectionClosed as exc:
                # 凭据失效只会以关闭码 4004/4014 表达:Gateway 握手阶段不带
                # token(鉴权发生在 Identify),握手被拒(InvalidStatus,如
                # Cloudflare 429 限流、代理 5xx)是暂时性网络问题,绝不能当作
                # 凭据失效——否则账号会被标记 stale 并永久停止收发。这类异常
                # 直接抛给 run() 的通用退避重连路径处理。
                if exc.rcvd is not None and exc.rcvd.code in _AUTH_CLOSE_CODES:
                    raise ChannelAuthError(
                        f"Discord bot token 无效或权限不足(关闭码 {exc.rcvd.code}),请重新绑定"
                    ) from exc
                raise
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task

    @staticmethod
    async def _heartbeat(ws: Any, interval_s: float, seq_holder: list[int | None]) -> None:
        """按服务端节奏发心跳;首拍加随机抖动(协议建议)。"""
        await asyncio.sleep(interval_s * random.random())
        while True:
            await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": seq_holder[0]}))
            await asyncio.sleep(interval_s)

    def _normalize(self, data: dict[str, Any]) -> InboundMessage | None:
        if data.get("guild_id"):
            return None  # 只处理私聊
        author = data.get("author") or {}
        if author.get("bot"):
            return None
        user_id = str(author.get("id") or "")
        channel_id = str(data.get("channel_id") or "")
        if not user_id or not channel_id:
            return None
        reply = ReplyContext(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            token={"dm_channel_id": channel_id},
        )
        return InboundMessage(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            text=str(data.get("content") or ""),
            reply=reply,
            provider_message_id=str(data.get("id") or ""),
        )

    async def _with_images(self, data: dict[str, Any], msg: InboundMessage) -> InboundMessage:
        """下载消息里的图片附件并挂回入站消息(无图或全失败则原样返回)。

        单张失败只跳过这一张:随图正文与其他图都还在。签名 CDN 链接收到即
        有效,所以在收事件的当下就下完,不留到后面用。

        下载期间 Gateway 收事件循环会阻塞,但心跳跑在独立任务里,连接不会因此
        被判死;私聊消息量极小,排队几秒无感。
        """
        attachments = _collect_image_attachments(data)
        if not attachments:
            return msg
        images: list[InboundImage] = []
        for url, name in attachments:
            try:
                content = await self._client.download_attachment(url)
            except Exception as exc:  # noqa: BLE001 -- 丢一张图,不丢整条消息
                logger.error("Discord 附件下载失败,已跳过该图:%s", exc)
                continue
            images.append(InboundImage(data=content, name=name))
        if not images:
            return msg
        logger.info("Discord 入站图片 %d 张 user=%s", len(images), msg.user_id)
        return replace(msg, images=tuple(images))

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        channel_id = reply.token.get("dm_channel_id") or await self._client.get_dm_channel(
            reply.user_id
        )
        await self._client.send_message(str(channel_id), text)

    async def send_photo(self, reply: ReplyContext, photo: bytes, caption: str) -> None:
        """图文消息(通道协议的可选能力,发送泵 getattr 探测后调用)。"""
        channel_id = reply.token.get("dm_channel_id") or await self._client.get_dm_channel(
            reply.user_id
        )
        await self._client.send_photo(str(channel_id), photo, caption)
