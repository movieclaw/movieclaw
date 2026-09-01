"""Telegram / Discord 入站图片:按平台文档挑图 + 下载失败只丢这张。

平台差异对照官方文档：

- Telegram：``message.photo`` 是同一张图的多档尺寸(末位最大),要 getFile
  换 file_path 再下载;以文件形式发来的图落在 ``message.document``,按
  mime_type 认领。相册是多条独立消息,合并交给 dispatcher 的聚合窗口。
- Discord：``message.attachments[]`` 直接给带签名的 CDN 直链与 content_type,
  过滤出图片 GET 下载即可。
"""

from __future__ import annotations

from typing import Any

from movieclaw_channel.discord.adapter import DiscordAdapter, _collect_image_attachments
from movieclaw_channel.telegram.adapter import TelegramAdapter, _collect_image_files

_PNG = b"\x89PNG\r\n\x1a\nfake"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class _StubTgClient:
    """按 file_id 返回字节的假客户端;值为 Exception 时模拟下载失败。"""

    def __init__(self, files: dict[str, bytes | Exception]) -> None:
        self._files = files
        self.requested: list[str] = []

    async def download_file(self, file_id: str, **_: Any) -> bytes:
        self.requested.append(file_id)
        result = self._files[file_id]
        if isinstance(result, Exception):
            raise result
        return result


def _tg_update(message: dict[str, Any]) -> dict[str, Any]:
    base = {
        "message_id": 7,
        "date": 1700000000,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 123, "is_bot": False},
    }
    return {"update_id": 1, "message": {**base, **message}}


async def _tg_run(client: _StubTgClient, update: dict[str, Any]):
    adapter = TelegramAdapter(client, "bot1")  # type: ignore[arg-type]
    msg = adapter._normalize(update)
    assert msg is not None
    return await adapter._with_images(update, msg)


def test_tg_picks_largest_photo_size() -> None:
    """photo 数组是同一张图的多档尺寸,必须取最大档(小档是缩略图,字会糊)。"""
    files = _collect_image_files(
        {
            "photo": [
                {"file_id": "small", "width": 90, "file_size": 1000},
                {"file_id": "big", "width": 1280, "file_size": 90000},
            ]
        }
    )
    assert [f[0] for f in files] == ["big"]


def test_tg_accepts_image_document_and_ignores_other_files() -> None:
    assert _collect_image_files(
        {"document": {"file_id": "d1", "mime_type": "image/png", "file_name": "错误截图.png"}}
    ) == [("d1", "错误截图.png")]
    assert (
        _collect_image_files({"document": {"file_id": "d2", "mime_type": "application/pdf"}}) == []
    )
    assert _collect_image_files({"video": {"file_id": "v1"}}) == []
    assert _collect_image_files({}) == []


async def test_tg_downloads_photo_with_caption() -> None:
    client = _StubTgClient({"big": _PNG})
    msg = await _tg_run(
        client,
        _tg_update(
            {"caption": "这是什么电影?", "photo": [{"file_id": "big", "width": 1280}]}
        ),
    )
    assert msg.text == "这是什么电影?"
    assert [i.data for i in msg.images] == [_PNG]
    assert client.requested == ["big"]


async def test_tg_pure_photo_message_has_content() -> None:
    client = _StubTgClient({"big": _PNG})
    msg = await _tg_run(client, _tg_update({"photo": [{"file_id": "big", "width": 1280}]}))
    assert msg.text == ""
    assert msg.has_content


async def test_tg_failed_download_keeps_message() -> None:
    client = _StubTgClient({"big": RuntimeError("getFile 20MB 上限")})
    msg = await _tg_run(
        client, _tg_update({"caption": "文字还在", "photo": [{"file_id": "big"}]})
    )
    assert msg.images == ()
    assert msg.text == "文字还在"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


class _StubDcClient:
    def __init__(self, urls: dict[str, bytes | Exception]) -> None:
        self._urls = urls
        self.requested: list[str] = []

    async def download_attachment(self, url: str, **_: Any) -> bytes:
        self.requested.append(url)
        result = self._urls[url]
        if isinstance(result, Exception):
            raise result
        return result


def _dc_message(**extra: Any) -> dict[str, Any]:
    return {
        "id": "555",
        "channel_id": "777",
        "author": {"id": "42", "bot": False},
        "content": "",
        **extra,
    }


async def _dc_run(client: _StubDcClient, data: dict[str, Any]):
    adapter = DiscordAdapter(client, "bot1")  # type: ignore[arg-type]
    msg = adapter._normalize(data)
    assert msg is not None
    return await adapter._with_images(data, msg)


def test_dc_filters_by_content_type_then_extension() -> None:
    picked = _collect_image_attachments(
        {
            "attachments": [
                {"url": "https://cdn/a.png", "filename": "a.png", "content_type": "image/png"},
                {
                    "url": "https://cdn/b.pdf",
                    "filename": "b.pdf",
                    "content_type": "application/pdf",
                },
                # content_type 是可选字段,缺失时按扩展名兜底
                {"url": "https://cdn/c.jpg", "filename": "c.jpg"},
                {"url": "https://cdn/d.zip", "filename": "d.zip"},
            ]
        }
    )
    assert picked == [("https://cdn/a.png", "a.png"), ("https://cdn/c.jpg", "c.jpg")]


async def test_dc_downloads_attachments_in_order() -> None:
    client = _StubDcClient({"https://cdn/1.png": _PNG, "https://cdn/2.png": b"\x89PNG\r\n\x1a\n2"})
    msg = await _dc_run(
        client,
        _dc_message(
            content="哪张更清楚",
            attachments=[
                {"url": "https://cdn/1.png", "filename": "1.png", "content_type": "image/png"},
                {"url": "https://cdn/2.png", "filename": "2.png", "content_type": "image/png"},
            ],
        ),
    )
    assert [i.name for i in msg.images] == ["1.png", "2.png"]
    assert msg.text == "哪张更清楚"


async def test_dc_one_failed_attachment_does_not_drop_others() -> None:
    client = _StubDcClient(
        {"https://cdn/1.png": RuntimeError("CDN 403"), "https://cdn/2.png": _PNG}
    )
    msg = await _dc_run(
        client,
        _dc_message(
            attachments=[
                {"url": "https://cdn/1.png", "filename": "1.png", "content_type": "image/png"},
                {"url": "https://cdn/2.png", "filename": "2.png", "content_type": "image/png"},
            ]
        ),
    )
    assert [i.data for i in msg.images] == [_PNG]


def test_dc_cdn_client_never_carries_bot_token() -> None:
    """附件走 CDN 域名,绝不能带 Authorization 头(否则等于把 bot token 送出去)。"""
    from movieclaw_channel.discord.client import DiscordClient

    client = DiscordClient("super-secret-token")
    assert "authorization" not in {k.lower() for k in client._cdn.headers}
    assert client._http.headers["Authorization"] == "Bot super-secret-token"
