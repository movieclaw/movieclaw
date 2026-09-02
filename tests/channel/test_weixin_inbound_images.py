"""微信入站图片挂回消息:下载解密成功 / 单张失败只丢这张 / 无图不受影响。

配合 test_weixin_media.py(纯函数层)覆盖 adapter 这一层的编排语义:
收消息循环拿到的每条消息,图片下载失败绝不能让整条消息(含随图文字)丢掉。
"""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from movieclaw_channel.weixin.adapter import WeixinAdapter

_KEY = bytes(range(16))
_PLAIN = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


def _encrypt(plain: bytes) -> bytes:
    pad = 16 - len(plain) % 16
    encryptor = Cipher(algorithms.AES(_KEY), modes.ECB()).encryptor()
    return encryptor.update(plain + bytes([pad]) * pad) + encryptor.finalize()


class _StubClient:
    """按 URL 返回预置字节的假客户端;值为 Exception 时模拟下载失败。"""

    def __init__(self, responses: dict[str, bytes | Exception]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    async def download_media(self, url: str, **_: Any) -> bytes:
        self.requested.append(url)
        result = self._responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    async def send_text(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise NotImplementedError


def _raw(*image_urls: str, text: str = "") -> dict[str, Any]:
    """一条带图(可选带文字)的 iLink 原始入站消息。"""
    items: list[dict[str, Any]] = []
    if text:
        items.append({"type": 1, "text_item": {"text": text}})
    items += [
        {"type": 2, "image_item": {"aeskey": _KEY.hex(), "media": {"full_url": url}}}
        for url in image_urls
    ]
    return {"from_user_id": "u1", "message_id": "m1", "item_list": items}


async def _run(client: _StubClient, raw: dict[str, Any]):
    adapter = WeixinAdapter(client, "bot1", bound_user_id="u1")  # type: ignore[arg-type]
    msg = adapter._normalize(raw)
    assert msg is not None
    return await adapter._with_images(raw, msg)


async def test_image_downloaded_and_decrypted() -> None:
    client = _StubClient({"https://cdn/1": _encrypt(_PLAIN)})
    msg = await _run(client, _raw("https://cdn/1", text="这是什么电影?"))
    assert msg.text == "这是什么电影?"
    assert [i.data for i in msg.images] == [_PLAIN]
    assert msg.images[0].name == "微信图片1"
    assert msg.has_content


async def test_pure_image_message_has_content() -> None:
    """纯图消息(没有一个字)也要算「有内容」,否则会被当成不支持的类型退回。"""
    client = _StubClient({"https://cdn/1": _encrypt(_PLAIN)})
    msg = await _run(client, _raw("https://cdn/1"))
    assert msg.text == ""
    assert msg.has_content


async def test_one_failed_image_does_not_drop_message() -> None:
    client = _StubClient(
        {"https://cdn/1": RuntimeError("CDN 502"), "https://cdn/2": _encrypt(_PLAIN)}
    )
    msg = await _run(client, _raw("https://cdn/1", "https://cdn/2", text="看第二张"))
    assert [i.data for i in msg.images] == [_PLAIN]
    assert msg.text == "看第二张"


async def test_all_images_failed_keeps_text_only() -> None:
    client = _StubClient({"https://cdn/1": RuntimeError("CDN 502")})
    msg = await _run(client, _raw("https://cdn/1", text="配的文字还在"))
    assert msg.images == ()
    assert msg.text == "配的文字还在"


async def test_text_only_message_does_not_touch_cdn() -> None:
    client = _StubClient({})
    msg = await _run(client, _raw(text="纯文字"))
    assert client.requested == []
    assert msg.images == ()
