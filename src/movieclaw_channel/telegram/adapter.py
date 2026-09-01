"""TelegramAdapter —— 实现通道协议的 Telegram 收发实现。

收消息循环:getUpdates 长轮询,游标 = 已确认的 update_id + 1(字符串形式
持久化到 channel_account.cursor,重启续传)。只处理私聊消息——群聊场景的
鉴权与打扰控制是另一回事,P0 不做。

图片(官方文档口径):
- ``message.photo`` 是同一张图的多档尺寸(PhotoSize 数组,末位最大),取最大档;
- 用户「以文件发送」的图落在 ``message.document``,按 ``mime_type`` 认领;
- 两者都只给 file_id,要 getFile 换 file_path 再下载(Bot API 限 20MB);
- 相册(media_group)在 Bot API 里是**多条独立消息**共享 media_group_id,
  合并由 dispatcher 的入站聚合窗口负责,这里不做特殊处理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from typing import Any

import httpx

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.telegram.client import TelegramApiError, TelegramClient
from movieclaw_channel.types import (
    ChannelAuthError,
    InboundImage,
    InboundMessage,
    ReplyContext,
)

logger = logging.getLogger("movieclaw_channel.telegram.adapter")

CHANNEL_ID = "telegram"

#: 单次失败的基础重试间隔;退避 = min(上限, 基础 × 连续失败次数)
_RETRY_DELAY_S = 2.0
#: 退避间隔上限
_BACKOFF_DELAY_S = 30.0


def _collect_image_files(message: dict[str, Any]) -> list[tuple[str, str]]:
    """挑出消息里可下载的图片,返回 [(file_id, 展示名)](无图返回空表)。

    photo 数组是同一张图的多档尺寸,末位最大——取最大档喂模型(小档是缩略图,
    文字细节会糊)。以文件形式发来的图在 document 里,按 mime_type 认领。
    """
    files: list[tuple[str, str]] = []
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        largest = max(photo, key=lambda p: p.get("file_size") or p.get("width") or 0)
        file_id = str(largest.get("file_id") or "")
        if file_id:
            files.append((file_id, "Telegram 图片"))
    document = message.get("document") or {}
    mime = str(document.get("mime_type") or "")
    file_id = str(document.get("file_id") or "")
    if file_id and mime.startswith("image/"):
        files.append((file_id, str(document.get("file_name") or "Telegram 图片")))
    return files


class TelegramAdapter:
    """Telegram 通道适配器(单 bot)。"""

    channel_id = CHANNEL_ID
    #: Telegram 单条消息 4096 字符上限,留余量
    max_text_len = 3900

    def __init__(self, client: TelegramClient, account_id: str) -> None:
        self._client = client
        self._account_id = account_id

    async def run(self, ctx: ChannelContext) -> None:
        stop = ctx.stop or asyncio.Event()
        logger.info("Telegram 收消息循环启动 account=%s", self._account_id)
        offset = int(ctx.initial_cursor) if ctx.initial_cursor.isdigit() else None
        failures = 0

        while not stop.is_set():
            try:
                updates = await self._client.get_updates(offset, stop=stop)
            except TelegramApiError as exc:
                if exc.auth_failed:
                    raise ChannelAuthError(str(exc)) from exc
                # 退避随连续失败次数线性上升到上限,只在成功后清零——计数不能
                # 在达到上限时重置,否则持续故障会循环打快速重试(2s,2s,30s,2s…)
                failures += 1
                delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                logger.error("getUpdates 失败(连续第 %d 次),%.0f 秒后重试:%s", failures, delay, exc)
                await self._sleep(stop, delay)
                continue
            except httpx.HTTPError as exc:
                failures += 1
                delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                logger.error(
                    "getUpdates 网络错误(连续第 %d 次),%.0f 秒后重试:%s", failures, delay, exc
                )
                await self._sleep(stop, delay)
                continue
            failures = 0

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                    await ctx.save_cursor(str(offset))
                msg = self._normalize(update)
                if msg is not None:
                    await ctx.on_inbound(await self._with_images(update, msg))

        logger.info("Telegram 收消息循环退出 account=%s", self._account_id)

    def _normalize(self, update: dict[str, Any]) -> InboundMessage | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return None
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return None
        user_id = str(sender.get("id") or "")
        if not user_id:
            return None
        reply = ReplyContext(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            token={"chat_id": chat.get("id")},
        )
        return InboundMessage(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            text=str(message.get("text") or message.get("caption") or ""),
            reply=reply,
            provider_message_id=f"{chat.get('id')}:{message.get('message_id')}",
            timestamp_ms=int(message.get("date") or 0) * 1000,
        )

    async def _with_images(self, update: dict[str, Any], msg: InboundMessage) -> InboundMessage:
        """下载消息里的图片并挂回入站消息(无图或全失败则原样返回)。

        单张失败只跳过这一张:随图的 caption 与其他图都还在,比整条消息丢掉
        体验好得多。与微信/Discord 三家同一套语义。
        """
        files = _collect_image_files(update.get("message") or {})
        if not files:
            return msg
        images: list[InboundImage] = []
        for file_id, name in files:
            try:
                data = await self._client.download_file(file_id)
            except Exception as exc:  # noqa: BLE001 -- 丢一张图,不丢整条消息
                logger.error("Telegram 图片下载失败,已跳过该图:%s", exc)
                continue
            images.append(InboundImage(data=data, name=name))
        if not images:
            return msg
        logger.info("Telegram 入站图片 %d 张 user=%s", len(images), msg.user_id)
        return replace(msg, images=tuple(images))

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        # 主动推送时 token 里没有 chat_id,私聊场景 chat_id == user_id
        await self._client.send_message(reply.token.get("chat_id") or reply.user_id, text)

    async def send_photo(self, reply: ReplyContext, photo: bytes, caption: str) -> None:
        """图文消息(通道协议的可选能力,发送泵 getattr 探测后调用)。"""
        await self._client.send_photo(reply.token.get("chat_id") or reply.user_id, photo, caption)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
