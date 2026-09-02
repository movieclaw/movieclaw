"""Telegram Bot API 的最小 HTTP 客户端。

只封装本项目用到的方法(getMe / getUpdates / sendMessage / sendPhoto /
sendChatAction / getFile + 文件下载),不引入 python-telegram-bot 这类重依赖。出口走
egress_transport("telegram"):国内部署 api.telegram.org 被墙,用户在
「设置 → 网络」勾选 telegram 走代理即可。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from movieclaw_channel.media import MAX_INBOUND_IMAGE_BYTES, download_capped
from movieclaw_net import egress_transport

#: 401/404 = bot token 无效或被吊销,对应通道层的凭据失效语义
_AUTH_ERROR_STATUS = (401, 404)


class TelegramApiError(Exception):
    """Bot API 返回 ok=false 时抛出;``auth_failed`` 标记凭据级错误。"""

    def __init__(self, message: str, *, auth_failed: bool = False) -> None:
        super().__init__(message)
        self.auth_failed = auth_failed


class TelegramClient:
    """单 bot 的 Bot API 客户端(线程不安全,通道层单任务使用)。"""

    def __init__(self, token: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        #: 文件下载走另一个前缀(官方文档:/file/bot<token>/<file_path>)
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        # 长轮询 50s + 余量;连接池与微信客户端同款「每账号一个」
        self._http = httpx.AsyncClient(
            transport=egress_transport("telegram"),
            timeout=httpx.Timeout(65.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        resp = await self._http.post(f"{self._base}/{method}", json=payload or {})
        return self._parse(method, resp)

    def _parse(self, method: str, resp: httpx.Response) -> Any:
        """Bot API 响应的统一解析:凭据错误分类 + ok=false 抛业务错。"""
        if resp.status_code in _AUTH_ERROR_STATUS:
            raise TelegramApiError(
                f"Telegram bot token 无效或已吊销(HTTP {resp.status_code})", auth_failed=True
            )
        try:
            data = resp.json()
        except ValueError as exc:
            # 5xx/代理错误页常返回 HTML:归入可重试的 API 错误,走退避而不是裸抛
            raise TelegramApiError(
                f"Telegram API {method} 返回异常响应(HTTP {resp.status_code},非 JSON)"
            ) from exc
        if not data.get("ok"):
            raise TelegramApiError(
                f"Telegram API {method} 失败:{data.get('description') or resp.status_code}"
            )
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def get_updates(
        self, offset: int | None, *, timeout_s: int = 50, stop: asyncio.Event | None = None
    ) -> list[dict[str, Any]]:
        """长轮询取增量消息;stop 置位时通过取消在飞请求尽快返回。"""
        payload: dict[str, Any] = {
            "timeout": timeout_s,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        call = asyncio.ensure_future(self._call("getUpdates", payload))
        if stop is None:
            return await call
        stop_wait = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait({call, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if call in done:
                return call.result()
            call.cancel()
            return []
        finally:
            stop_wait.cancel()

    async def download_file(
        self, file_id: str, *, max_bytes: int = MAX_INBOUND_IMAGE_BYTES
    ) -> bytes:
        """按 file_id 取回文件字节(官方两步:getFile 换 file_path,再下载)。

        下载地址是 ``https://api.telegram.org/file/bot<token>/<file_path>``
        (官方文档口径,token 就在路径里,同域名复用同一连接池与代理)。
        Bot API 明确只支持下载 20MB 以内的文件,超出时 getFile 直接报错。
        """
        info = await self._call("getFile", {"file_id": file_id})
        file_path = str((info or {}).get("file_path") or "")
        if not file_path:
            raise TelegramApiError("getFile 未返回 file_path,无法下载该文件")
        return await download_capped(
            self._http,
            f"{self._file_base}/{file_path}",
            max_bytes=max_bytes,
            label="Telegram 文件",
        )

    async def send_message(self, chat_id: str | int, text: str) -> None:
        # 纯文本发送:Agent 输出是 Markdown,但 TG 的 parse_mode 对不配对的
        # 星号/下划线会整条报错,宁可裸发也不能丢消息
        await self._call("sendMessage", {"chat_id": chat_id, "text": text})

    async def send_photo(self, chat_id: str | int, photo: bytes, caption: str) -> None:
        """发送图文消息:图片字节 multipart 上传,文案作 caption(≤1024 字符)。"""
        resp = await self._http.post(
            f"{self._base}/sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption},
            files={"photo": ("poster.jpg", photo)},
        )
        self._parse("sendPhoto", resp)

    async def send_chat_action(self, chat_id: str | int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action})
