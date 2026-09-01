"""微信入站图片的服务层编排:落会话附件目录、限额与失败降级。

覆盖 WeixinChannelService._compose_input 的语义约定:

1. 图片落进**本会话**的 assets 目录,消息里只留引用(转录不含字节);
2. 单条消息超过 4 张时只取前 4 张,并把「只看了前几张」明确回执给用户;
3. 坏图只跳过这一张,随图文字与其他图照常送进 Agent;
4. 纯图消息的图片全部失败时返回空串——调用方据此放弃本轮,不留空消息。
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from movieclaw_api.services import weixin_channel
from movieclaw_api.services.agent_attachments import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_IMAGE_BYTES,
    AgentAttachmentStore,
)
from movieclaw_channel.types import InboundImage, InboundMessage, ReplyContext
from movieclaw_llm import ImagePart, TextPart

_SESSION_ID = "sess-weixin"


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch) -> AgentAttachmentStore:
    """把附件存储指向临时目录(不碰 data/)。"""
    instance = AgentAttachmentStore(tmp_path)
    monkeypatch.setattr(weixin_channel, "get_agent_attachment_store", lambda: instance)
    return instance


def _msg(text: str, *images: bytes) -> InboundMessage:
    reply = ReplyContext(channel_id="weixin", account_id="bot1", user_id="u1")
    return InboundMessage(
        channel_id="weixin",
        account_id="bot1",
        user_id="u1",
        text=text,
        reply=reply,
        provider_message_id="m1",
        images=tuple(InboundImage(data=d, name=f"微信图片{i}") for i, d in enumerate(images, 1)),
    )


async def _compose(msg: InboundMessage) -> tuple[object, list[str]]:
    notices: list[str] = []

    async def emit(text: str) -> None:
        notices.append(text)

    content = await weixin_channel.WeixinChannelService._compose_input(msg, _SESSION_ID, emit)
    return content, notices


async def test_text_only_message_stays_plain_string(store) -> None:
    content, notices = await _compose(_msg("帮我找沙丘2"))
    assert content == "帮我找沙丘2"
    assert notices == []


async def test_image_becomes_bound_reference(store, tmp_path) -> None:
    content, notices = await _compose(_msg("这是什么电影?", png_bytes()))
    assert notices == []
    assert isinstance(content, list)
    assert isinstance(content[0], TextPart) and content[0].text == "这是什么电影?"
    image = content[1]
    assert isinstance(image, ImagePart)
    # 消息里只有引用,字节在会话 assets 目录(转录永不含 base64)
    assert image.data is None
    assert image.media_type == "image/png"
    assert store.read_meta(_SESSION_ID, image.attachment_id) is not None
    assert (tmp_path / f"{_SESSION_ID}.assets" / f"{image.attachment_id}.png").is_file()


async def test_pure_image_message_has_no_text_part(store) -> None:
    content, _ = await _compose(_msg("", png_bytes()))
    assert isinstance(content, list)
    assert [type(p) for p in content] == [ImagePart]


async def test_extra_images_are_trimmed_with_notice(store) -> None:
    images = [png_bytes(w, w) for w in range(8, 8 + MAX_ATTACHMENTS_PER_MESSAGE + 2)]
    content, notices = await _compose(_msg("看看这些", *images))
    assert isinstance(content, list)
    assert sum(isinstance(p, ImagePart) for p in content) == MAX_ATTACHMENTS_PER_MESSAGE
    assert notices and "最多处理" in notices[0]


async def test_broken_image_is_skipped_but_message_survives(store) -> None:
    content, notices = await _compose(_msg("第二张能看吗", b"not-an-image", png_bytes()))
    assert isinstance(content, list)
    assert sum(isinstance(p, ImagePart) for p in content) == 1
    assert notices and "没能处理" in notices[0]


async def test_pure_image_all_failed_returns_empty(store) -> None:
    """纯图消息的图全废时返回空串:调用方放弃本轮,不写一条空消息进转录。"""
    content, notices = await _compose(_msg("", b"not-an-image"))
    assert content == ""
    assert notices and "没能处理" in notices[0]


async def test_oversized_image_is_shrunk_into_limits(store) -> None:
    """微信原图没有前端可压,服务端兜底缩到限额内(否则会被上传校验拒掉)。"""
    # 随机噪声:PNG 压不动,才能真的造出一张超过 5MB 的原图
    big = Image.frombytes("RGB", (2400, 1600), os.urandom(2400 * 1600 * 3))
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    raw = buf.getvalue()
    assert len(raw) > MAX_IMAGE_BYTES

    content, notices = await _compose(_msg("", raw))
    assert notices == []
    assert isinstance(content, list)
    meta = store.read_meta(_SESSION_ID, content[0].attachment_id)
    assert meta is not None
    assert meta.bytes <= MAX_IMAGE_BYTES
    assert max(meta.width, meta.height) <= 2048
