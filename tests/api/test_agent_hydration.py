"""请求水合与落库脱水：视觉门控、缺文件、预算、清单文本与不变量。

对应 docs/design/agent-image-input.md §6 的核心规则：
- 门控 fail-closed：非视觉模型只见占位文本，并给出 extra_models 的出路；
- 缺文件降级占位，不失败；
- 预算从最新往旧保留，超预算旧图占位；
- 附件清单文本只存在于请求投影，store 写入口剥除（脱水不变量）。
"""

from __future__ import annotations

import base64
import io

from PIL import Image

import movieclaw_api.services.agent_attachments as attachments_mod
from movieclaw_api.services.agent_attachments import (
    ATTACHMENT_NOTE_PREFIX,
    AgentAttachmentStore,
    compose_user_content,
    hydrate_images,
)
from movieclaw_api.services.agent_sessions import AgentSessionStore, dehydrate_message
from movieclaw_llm import ChatMessage, ImagePart, ModelInfo, TextPart


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 120, 240)).save(buf, format="PNG")
    return buf.getvalue()


VISION = ModelInfo(id="qwen3-vl-plus", modalities=["text", "image"])
TEXT_ONLY = ModelInfo(id="deepseek-chat", modalities=["text"])


def bound_image(store: AgentAttachmentStore, session_id: str, name: str = "图.png") -> ImagePart:
    meta = store.save_staging(png_bytes(), name)
    [bound] = store.bind(session_id, [meta.attachment_id])
    return ImagePart(attachment_id=bound.attachment_id, media_type=bound.mime, name=name)


def user_message(text: str, *images: ImagePart) -> ChatMessage:
    metas = []  # compose_user_content 走 AttachmentMeta，这里直接手拼 parts
    del metas
    parts = ([TextPart(text=text)] if text else []) + list(images)
    return ChatMessage(role="user", content=parts)


# ---------------------------------------------------------------------------
# 视觉门控
# ---------------------------------------------------------------------------


async def test_non_vision_model_gets_placeholder_and_guidance(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    img = bound_image(store, "s1", "报错.png")
    [hydrated] = await hydrate_images(
        [user_message("这是什么", img)], session_id="s1", model_info=TEXT_ONLY, store=store
    )
    texts = [p.text for p in hydrated.content if isinstance(p, TextPart)]
    assert texts[0] == "这是什么"
    assert "不支持图片输入" in texts[1] and "报错.png" in texts[1]
    assert "modalities" in texts[1]  # 目录外视觉模型被静默门控时的出路提示
    assert not any(isinstance(p, ImagePart) for p in hydrated.content)
    # 非视觉路径不追加清单文本（占位文本已说明一切）
    assert not any(t.startswith(ATTACHMENT_NOTE_PREFIX) for t in texts)


# ---------------------------------------------------------------------------
# 水合与清单文本
# ---------------------------------------------------------------------------


async def test_vision_model_hydrates_and_appends_note(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    img = bound_image(store, "s1", "海报.png")
    [hydrated] = await hydrate_images(
        [user_message("识别一下", img)], session_id="s1", model_info=VISION, store=store
    )
    image_part = next(p for p in hydrated.content if isinstance(p, ImagePart))
    assert base64.b64decode(image_part.data or "").startswith(b"\x89PNG")
    assert image_part.attachment_id == img.attachment_id  # 引用保留，落库脱水靠它
    note = hydrated.content[-1]
    assert isinstance(note, TextPart) and note.text.startswith(ATTACHMENT_NOTE_PREFIX)
    assert "海报.png" in note.text
    assert "用户未附文字" not in note.text


async def test_image_only_message_note_prompts_direct_response(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    img = bound_image(store, "s1")
    [hydrated] = await hydrate_images(
        [user_message("", img)], session_id="s1", model_info=VISION, store=store
    )
    note = hydrated.content[-1]
    assert isinstance(note, TextPart)
    assert "用户未附文字，请直接查看图片内容并回应" in note.text


async def test_missing_file_degrades_to_placeholder(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    ghost = ImagePart(attachment_id="a" * 32, media_type="image/png", name="没了.png")
    [hydrated] = await hydrate_images(
        [user_message("看图", ghost)], session_id="s1", model_info=VISION, store=store
    )
    texts = [p.text for p in hydrated.content if isinstance(p, TextPart)]
    assert any("已过期或被清理" in t and "没了.png" in t for t in texts)
    assert not any(isinstance(p, ImagePart) for p in hydrated.content)
    # 图全部降级后消息是纯文本：附件清单注文不得追加，否则 dehydrate 的
    # 「含图才剥注文」判据失效，注文会泄漏进压缩 replacement_history 落库
    from movieclaw_api.services.agent_attachments import ATTACHMENT_NOTE_PREFIX

    assert not any(t.startswith(ATTACHMENT_NOTE_PREFIX) for t in texts)


async def test_messages_without_images_pass_through_unchanged(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    plain = ChatMessage(role="user", content="纯文本")
    assistant = ChatMessage(role="assistant", content="回答")
    out = await hydrate_images(
        [plain, assistant], session_id="s1", model_info=VISION, store=store
    )
    assert out[0] is plain and out[1] is assistant


# ---------------------------------------------------------------------------
# 请求级预算：从最新往旧保留
# ---------------------------------------------------------------------------


async def test_budget_keeps_newest_and_placeholders_oldest(tmp_path, monkeypatch) -> None:
    store = AgentAttachmentStore(tmp_path)
    old_img = bound_image(store, "s1", "旧图.png")
    new_img = bound_image(store, "s1", "新图.png")
    single = store.read_meta("s1", old_img.attachment_id or "")
    assert single is not None
    # 预算只装得下一张：倒序装入 → 新图保留，旧图占位
    monkeypatch.setattr(attachments_mod, "MAX_REQUEST_IMAGE_BYTES", single.bytes)
    old_msg = user_message("第一张", old_img)
    new_msg = user_message("第二张", new_img)
    hydrated = await hydrate_images(
        [old_msg, new_msg], session_id="s1", model_info=VISION, store=store
    )
    old_texts = [p.text for p in hydrated[0].content if isinstance(p, TextPart)]
    assert any("因请求体积限制已省略" in t for t in old_texts)
    assert not any(isinstance(p, ImagePart) for p in hydrated[0].content)
    new_image = next(p for p in hydrated[1].content if isinstance(p, ImagePart))
    assert new_image.data  # 最新的图完整保留


# ---------------------------------------------------------------------------
# 落库脱水（store 写入口的不变量）
# ---------------------------------------------------------------------------


def test_dehydrate_strips_bytes_and_note_but_keeps_refs() -> None:
    message = ChatMessage(
        role="user",
        content=[
            TextPart(text="看图"),
            ImagePart(attachment_id="a" * 32, data="QUJD", media_type="image/png", name="x.png"),
            TextPart(text=f"{ATTACHMENT_NOTE_PREFIX} 本条消息附带 1 张图片：x.png（image/png）。"),
        ],
    )
    out = dehydrate_message(message)
    assert out.content[0].text == "看图"
    image = out.content[1]
    assert isinstance(image, ImagePart) and image.data is None
    assert image.attachment_id == "a" * 32
    assert len(out.content) == 2  # 清单文本被剥除


def test_dehydrate_keeps_user_typed_prefix_text_without_images() -> None:
    """用户自己打出前缀文字、且消息不含图片时，正文原样保留。"""
    message = ChatMessage(
        role="user", content=[TextPart(text=f"{ATTACHMENT_NOTE_PREFIX} 这是我打的字")]
    )
    assert dehydrate_message(message) is message


def test_store_writes_are_dehydrated_including_compaction(tmp_path) -> None:
    """三个写入口统一脱水：手动压缩不经 recorder 也不会把 base64 写进转录。"""
    from movieclaw_agent import CompactionResult

    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    hydrated = ChatMessage(
        role="user",
        content=[
            ImagePart(attachment_id="b" * 32, data="QUJD", media_type="image/png"),
            TextPart(text=f"{ATTACHMENT_NOTE_PREFIX} 清单"),
        ],
    )
    store.append(sid, hydrated)
    store.append_compaction(
        sid,
        CompactionResult(
            summary="摘要", replacement_history=[hydrated], tokens_before=10, tokens_after=5
        ),
    )
    raw = (tmp_path / f"{sid}.jsonl").read_text("utf-8")
    assert "QUJD" not in raw
    assert ATTACHMENT_NOTE_PREFIX not in raw
    assert "b" * 32 in raw  # 引用完好


def test_compose_user_content_plain_text_stays_string(tmp_path) -> None:
    assert compose_user_content("你好", []) == "你好"
