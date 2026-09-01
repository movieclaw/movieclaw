"""IM 入站图片的服务层编排(微信 / Telegram / Discord 共用的同一入口)。

覆盖 im_attachments 的语义约定:

1. 图片落进**本会话**的 assets 目录,消息里只留引用(转录不含字节);
2. **模型不支持视觉时给确定性提醒**——IM 没有 Web 那样的模型选择器,
   这句提醒是用户唯一的反馈渠道,不能指望模型自己说清楚;
3. 单条消息超过 4 张时只取前 4 张,并把「只看了前几张」明确回执给用户;
4. 坏图只跳过这一张,随图文字与其他图照常送进 Agent;
5. 纯图消息的图片全部失败时返回空内容——调用方据此放弃本轮,不留空消息;
6. 微信原图没有前端可压,服务端兜底缩到限额内。
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from movieclaw_api.services import agent_attachments, im_attachments
from movieclaw_api.services.agent_attachments import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_IMAGE_BYTES,
    AgentAttachmentStore,
)
from movieclaw_api.services.im_attachments import ingest_inbound_images, prepare_agent_input
from movieclaw_channel.types import InboundImage, InboundMessage, ReplyContext
from movieclaw_llm import ImagePart, LlmRoutingError, ModelInfo, TextPart

#: 视觉 / 非视觉模型(门控与提醒都按 modalities 判定)
VISION_MODEL = ModelInfo(id="qwen3-vl-plus", modalities=["text", "image"])
TEXT_MODEL = ModelInfo(id="deepseek-chat", modalities=["text"])

_SESSION_ID = "sess-weixin"


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch) -> AgentAttachmentStore:
    """把附件存储指向临时目录(不碰 data/)。"""
    instance = AgentAttachmentStore(tmp_path)
    monkeypatch.setattr(im_attachments, "get_agent_attachment_store", lambda: instance)
    # 水合层在自己的模块命名空间里取单例,两处都要指向临时目录
    monkeypatch.setattr(agent_attachments, "get_agent_attachment_store", lambda: instance)
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


async def _compose(
    msg: InboundMessage, model_info: ModelInfo | None = VISION_MODEL
) -> tuple[object, list[str]]:
    result = await ingest_inbound_images(
        session_id=_SESSION_ID, text=msg.text, images=msg.images, model_info=model_info
    )
    return result.content, result.notices


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


# ---------------------------------------------------------------------------
# 非视觉模型:提醒必须是确定性的服务端文案
# ---------------------------------------------------------------------------


async def test_non_vision_model_gets_explicit_notice(store) -> None:
    content, notices = await _compose(_msg("这是什么", png_bytes()), TEXT_MODEL)
    assert len(notices) == 1
    notice = notices[0]
    # 说清三件事:哪个模型不行、怎么换、如果它其实支持怎么补录
    assert "deepseek-chat" in notice
    assert "不支持看图" in notice
    assert "qwen3-vl-plus" in notice
    assert "补录模型" in notice
    # 仍然入库:用户之后在 Web 里换视觉模型 retry 同一条消息就能看图
    assert isinstance(content, list)
    assert store.read_meta(_SESSION_ID, content[1].attachment_id) is not None


async def test_vision_model_says_nothing(store) -> None:
    _, notices = await _compose(_msg("这是什么", png_bytes()), VISION_MODEL)
    assert notices == []


async def test_text_only_message_never_warns_about_vision(store) -> None:
    """没发图的普通对话,不该被这条提醒打扰。"""
    content, notices = await _compose(_msg("帮我找沙丘2"), TEXT_MODEL)
    assert content == "帮我找沙丘2"
    assert notices == []


async def test_unresolvable_model_skips_notice(store) -> None:
    """路由解析不出模型时不抢答:错误由 runner 以 agent_error 统一呈现。"""
    _, notices = await _compose(_msg("看图", png_bytes()), None)
    assert notices == []


# ---------------------------------------------------------------------------
# prepare_agent_input:落转录的形态与发模型的形态必须分开
# ---------------------------------------------------------------------------


class _StubRouter:
    """只实现 get_model_info 的假路由(prepare_agent_input 只用到它)。"""

    def __init__(self, model_info: ModelInfo | None) -> None:
        self._model_info = model_info

    def get_model_info(self, _ref: str) -> ModelInfo:
        if self._model_info is None:
            raise LlmRoutingError("没有可用的模型")
        return self._model_info


async def _prepare(msg: InboundMessage, model_info: ModelInfo | None):
    notices: list[str] = []

    async def emit(text: str) -> None:
        notices.append(text)

    prepared = await prepare_agent_input(
        llm_router=_StubRouter(model_info),  # type: ignore[arg-type]
        session_id=_SESSION_ID,
        msg=msg,
        history=[],
        emit=emit,
    )
    return prepared, notices


async def test_prepare_separates_transcript_form_from_request_form(store) -> None:
    prepared, notices = await _prepare(_msg("这是什么电影", png_bytes()), VISION_MODEL)
    assert prepared is not None and notices == []

    # 落转录的是引用态:没有字节,也没有附件清单文本
    recorded = prepared.user_content
    assert isinstance(recorded, list)
    assert [type(p) for p in recorded] == [TextPart, ImagePart]
    assert recorded[1].data is None

    # 发模型的是水合态:有 base64 字节,并追加了附件清单文本
    sent = prepared.input_for_run
    assert isinstance(sent, list)
    sent_image = next(p for p in sent if isinstance(p, ImagePart))
    assert sent_image.data
    assert any(
        isinstance(p, TextPart) and p.text.startswith(agent_attachments.ATTACHMENT_NOTE_PREFIX)
        for p in sent
    )


async def test_prepare_gates_images_for_non_vision_model(store) -> None:
    """非视觉模型:提醒发给用户,占位文本发给模型——两条路都不能断。"""
    prepared, notices = await _prepare(_msg("这是什么电影", png_bytes()), TEXT_MODEL)
    assert prepared is not None
    assert notices and "不支持看图" in notices[0]

    sent = prepared.input_for_run
    assert isinstance(sent, list)
    assert not any(isinstance(p, ImagePart) for p in sent)  # 图绝不发给非视觉模型
    assert any(isinstance(p, TextPart) and "不支持图片输入" in p.text for p in sent)
    # 转录里图片引用仍在(换模型 retry 还能看)
    assert any(isinstance(p, ImagePart) for p in prepared.user_content)


async def test_prepare_abandons_turn_when_all_images_fail(store) -> None:
    prepared, notices = await _prepare(_msg("", b"not-an-image"), VISION_MODEL)
    assert prepared is None
    assert notices and "没能处理" in notices[0]
