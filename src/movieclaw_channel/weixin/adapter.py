"""WeixinAdapter —— 实现通道协议的微信收发实现。

收消息循环(镜像 openclaw-weixin 的 monitor 语义):
- getupdates 长轮询,服务端可通过 longpolling_timeout_ms 动态调整下轮超时;
- ``get_updates_buf`` 是增量游标:每轮返回新值就持久化,重启续传(至少一次
  投递,幂等去重由 dispatcher 负责);
- 图片 item 在归一化后按 CDN 引用下载+解密(见 media 模块),随消息带给
  dispatcher;下载失败只丢这张图,消息本身照常投递;
- errcode -14(token 失效)→ 抛 ChannelAuthError,由 manager 停账号标 stale;
- 其余错误:连续失败 <3 次退避 2s,达到 3 次退避 30s,自愈后清零。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import httpx

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.types import (
    ChannelAuthError,
    InboundImage,
    InboundMessage,
    ReplyContext,
)
from movieclaw_channel.weixin.client import (
    STALE_TOKEN_ERRCODE,
    WeixinApiError,
    WeixinClient,
)
from movieclaw_channel.weixin.media import collect_image_refs, decrypt_image

logger = logging.getLogger("movieclaw_channel.weixin.adapter")

CHANNEL_ID = "weixin"

_MAX_CONSECUTIVE_FAILURES = 3
_RETRY_DELAY_S = 2.0
_BACKOFF_DELAY_S = 30.0
_DEFAULT_LONG_POLL_S = 35.0

#: 消息 item 类型(iLink 协议):1=文本 2=图片 3=语音
_ITEM_TEXT = 1
_ITEM_VOICE = 3
#: message_type:2=BOT(自己发出的,收侧跳过)
_MSG_TYPE_BOT = 2


def _extract_text(item_list: list[dict[str, Any]] | None) -> str:
    """从 item_list 提取文本正文:文本 item 优先,语音 item 取平台转写文字。"""
    for item in item_list or []:
        if item.get("type") == _ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text")
            if text:
                return str(text)
        if item.get("type") == _ITEM_VOICE:
            text = (item.get("voice_item") or {}).get("text")
            if text:
                return str(text)
    return ""


class WeixinAdapter:
    """微信通道适配器(单账号)。

    会话令牌(context_token)的去向:iLink 的发送接口必须绑定一条会话,而令牌
    只随入站消息下发。回复入站消息时原样回带即可;**主动推送**(订阅投递、
    入库完成等)没有对应的入站消息,只能复用最近一次记住的令牌——这也是
    Telegram(chat_id == user_id)、Discord(现开 DM 频道)各自的兜底位置。
    令牌变化时经 ``on_context_token`` 落库,进程重启后由 ``initial_context_token``
    读回,避免重启后第一次推送必然失联。

    **只记绑定人的令牌**:陌生人也能给 bot 发消息(dispatcher 白名单会把这类
    消息丢掉,但收消息循环仍会看到),不按 ``bound_user_id`` 过滤的话,推送就会
    带上陌生人会话的令牌——轻则发不出去,重则发错人。同理,兜底令牌只在
    收件人正是绑定人时才启用。
    """

    channel_id = CHANNEL_ID
    #: 微信单条文本约 4000 字上限,留余量
    max_text_len = 3800

    def __init__(
        self,
        client: WeixinClient,
        account_id: str,
        *,
        bound_user_id: str = "",
        initial_context_token: str = "",
        on_context_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._bound_user_id = bound_user_id
        self._context_token = initial_context_token
        self._on_context_token = on_context_token

    async def run(self, ctx: ChannelContext) -> None:
        stop = ctx.stop or asyncio.Event()
        await self._client.notify_start()
        logger.info("微信收消息循环启动 account=%s", self._account_id)

        cursor = ctx.initial_cursor
        timeout_s = _DEFAULT_LONG_POLL_S
        failures = 0

        try:
            while not stop.is_set():
                try:
                    resp = await self._client.get_updates(cursor, stop=stop, timeout_s=timeout_s)
                except (httpx.HTTPError, WeixinApiError) as exc:
                    # 传输层/网关错误在循环内退避重试(与业务错误同节奏),
                    # 不抛给 supervisor 整层重启——那会重发 notifystart 且粒度太粗
                    # 与 telegram 侧同款：退避线性升到上限后保持,不再周期性归零
                    # (归零会让持续故障循环打快速重试 2s,2s,30s,2s…),成功后才清零
                    failures += 1
                    delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                    logger.error(
                        "getupdates 请求失败(连续第 %d 次),%.0f 秒后重试:%s",
                        failures,
                        delay,
                        exc,
                    )
                    await self._sleep(stop, delay)
                    continue

                ret = resp.get("ret") or 0
                errcode = resp.get("errcode") or 0
                if ret == STALE_TOKEN_ERRCODE or errcode == STALE_TOKEN_ERRCODE:
                    raise ChannelAuthError(
                        f"微信 bot 凭据已失效(errcode {STALE_TOKEN_ERRCODE}),请重新扫码绑定"
                    )
                if ret != 0 or errcode != 0:
                    failures += 1
                    delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                    logger.error(
                        "getupdates 返回错误 ret=%s errcode=%s errmsg=%s"
                        "(连续第 %d 次,%.0f 秒后重试)",
                        ret,
                        errcode,
                        resp.get("errmsg"),
                        failures,
                        delay,
                    )
                    await self._sleep(stop, delay)
                    continue
                failures = 0

                # 服务端建议的下轮长轮询超时
                suggested = resp.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_s = suggested / 1000

                new_cursor = resp.get("get_updates_buf")
                if new_cursor:
                    cursor = new_cursor
                    await ctx.save_cursor(new_cursor)

                for raw in resp.get("msgs") or []:
                    msg = self._normalize(raw)
                    if msg is not None:
                        msg = await self._with_images(raw, msg)
                        await self._remember_context_token(msg)
                        await ctx.on_inbound(msg)
        finally:
            await self._client.notify_stop()
            logger.info("微信收消息循环退出 account=%s", self._account_id)

    def _normalize(self, raw: dict[str, Any]) -> InboundMessage | None:
        """iLink WeixinMessage → 归一化入站消息;非用户消息返回 None。"""
        if raw.get("message_type") == _MSG_TYPE_BOT:
            return None
        from_user_id = str(raw.get("from_user_id") or "")
        if not from_user_id:
            return None

        token: dict[str, Any] = {}
        context_token = raw.get("context_token")
        if context_token:
            token["context_token"] = context_token

        provider_id = str(raw.get("message_id") or raw.get("client_id") or raw.get("seq") or "")
        reply = ReplyContext(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=from_user_id,
            token=token,
        )
        return InboundMessage(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=from_user_id,
            text=_extract_text(raw.get("item_list")),
            reply=reply,
            provider_message_id=provider_id,
            timestamp_ms=int(raw.get("create_time_ms") or 0),
        )

    async def _with_images(self, raw: dict[str, Any], msg: InboundMessage) -> InboundMessage:
        """下载并解密消息里的图片,挂回入站消息(无图或全失败则原样返回)。

        单张图失败只跳过这一张:用户发的另外几张、以及随图的文字都还在,
        比整条消息丢掉体验好得多。下载是串行的——收消息循环本来就串行投递,
        且图片数量个位数,并发的复杂度换不来收益。
        """
        refs = collect_image_refs(raw.get("item_list"))
        if not refs:
            return msg
        images: list[InboundImage] = []
        for index, ref in enumerate(refs, start=1):
            try:
                data = await self._client.download_media(ref.url)
                if ref.aes_key is not None:
                    data = decrypt_image(data, ref.aes_key)
            except Exception as exc:  # noqa: BLE001 -- 丢一张图,不丢整条消息
                logger.error("微信图片下载/解密失败,已跳过该图:%s", exc)
                continue
            images.append(InboundImage(data=data, name=f"微信图片{index}"))
        if not images:
            return msg
        logger.info("微信入站图片 %d 张 user=%s", len(images), msg.user_id)
        return replace(msg, images=tuple(images))

    async def _remember_context_token(self, msg: InboundMessage) -> None:
        """记住并持久化绑定人的会话令牌(仅在变化时写库,正常一次会话只写一次)。"""
        if not self._bound_user_id or msg.user_id != self._bound_user_id:
            return  # 陌生人的会话令牌不能拿来推送,见类文档
        token = str(msg.reply.token.get("context_token") or "")
        if not token or token == self._context_token:
            return
        self._context_token = token
        if self._on_context_token is None:
            return
        try:
            await self._on_context_token(token)
        except Exception as exc:  # noqa: BLE001 -- 落库失败只影响重启后的首次推送
            logger.warning("微信会话令牌落库失败(本进程内仍可用):%s", exc)

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        # 主动推送构造的 ReplyContext 没有会话令牌(它只随入站消息下发),
        # 回落到最近一次记住的那个——与 telegram 的 chat_id 兜底同一位置。
        # 兜底只对绑定人生效:记住的令牌属于他的会话,不能拿去发给别人。
        token = reply.token.get("context_token")
        if not token and self._bound_user_id and reply.user_id == self._bound_user_id:
            token = self._context_token or None
        await self._client.send_text(reply.user_id, text, token)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        """可被 stop 打断的退避等待。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
