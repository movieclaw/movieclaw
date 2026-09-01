"""Agent 图片附件存储：魔数校验、staging 绑定原子性、清理与配额。

覆盖设计定案（docs/design/agent-image-input.md）的关键约束：
1. 魔数嗅探判真实类型（伪装文件 / SVG 进不来），Pillow 二次校验可解码；
2. attachment_id 强校验 32 位 hex（路径穿越的硬闸）；
3. staging → assets 的绑定原子性：同一附件只能被一个会话拿走；
4. 会话删除联动清理、staging 孤儿按 TTL 回收；
5. fork 同 id 复制，新会话不依赖源会话文件。
"""

from __future__ import annotations

import io
import time

import pytest
from PIL import Image

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services.agent_attachments import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_IMAGE_BYTES,
    AgentAttachmentStore,
    sniff_image_mime,
)


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(30, 30, 200)).save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 魔数嗅探与上传校验
# ---------------------------------------------------------------------------


def test_sniff_recognizes_supported_formats() -> None:
    assert sniff_image_mime(png_bytes()) == "image/png"
    assert sniff_image_mime(jpeg_bytes()) == "image/jpeg"
    assert sniff_image_mime(b"GIF89a" + b"\x00" * 10) == "image/gif"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4) == "image/webp"


def test_sniff_rejects_disguised_files() -> None:
    # SVG（可内嵌脚本）与改后缀的文本都过不了魔数
    assert sniff_image_mime(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert sniff_image_mime(b"MZ\x90\x00") is None


def test_save_staging_rejects_non_image(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    with pytest.raises(BadRequestException, match="不支持的图片格式"):
        store.save_staging(b"<svg/>", "fake.png")


def test_save_staging_rejects_truncated_image(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    # 魔数正确但内容截断：Pillow 解码兜底拦下
    with pytest.raises(BadRequestException, match="无法解码"):
        store.save_staging(png_bytes()[:20], "broken.png")


def test_save_staging_rejects_oversize(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    data = png_bytes() + b"\x00" * MAX_IMAGE_BYTES
    with pytest.raises(BadRequestException, match="图片过大"):
        store.save_staging(data, "big.png")


def test_save_staging_records_metadata(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    meta = store.save_staging(png_bytes(16, 9), "海报.png")
    assert meta.mime == "image/png"
    assert (meta.width, meta.height) == (16, 9)
    assert meta.original_name == "海报.png"
    assert len(meta.attachment_id) == 32
    assert (tmp_path / ".staging" / f"{meta.attachment_id}.png").is_file()


# ---------------------------------------------------------------------------
# attachment_id 硬闸
# ---------------------------------------------------------------------------


def test_traversal_attachment_id_rejected(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    for bad in ("../../etc/passwd", "A" * 32, "abc", ""):
        with pytest.raises(BadRequestException, match="附件编号格式不正确"):
            store.bind("s1", [bad])
        with pytest.raises(BadRequestException, match="附件编号格式不正确"):
            store.read_meta("s1", bad)


# ---------------------------------------------------------------------------
# 绑定
# ---------------------------------------------------------------------------


def test_bind_moves_staging_into_session_assets(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    meta = store.save_staging(png_bytes(), "a.png")
    [bound] = store.bind("s1", [meta.attachment_id])
    assert bound.attachment_id == meta.attachment_id
    assert (tmp_path / "s1.assets" / f"{meta.attachment_id}.png").is_file()
    assert not (tmp_path / ".staging" / f"{meta.attachment_id}.png").exists()
    # 已绑定本会话的 id 重复绑定（retry 沿用）是幂等成功
    [again] = store.bind("s1", [meta.attachment_id])
    assert again.attachment_id == meta.attachment_id


def test_bind_same_attachment_to_second_session_fails(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    meta = store.save_staging(png_bytes(), "a.png")
    store.bind("s1", [meta.attachment_id])
    with pytest.raises(BadRequestException, match="已被其他会话使用"):
        store.bind("s2", [meta.attachment_id])


def test_bind_rejects_too_many_per_message(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    ids = [store.save_staging(png_bytes(), "a.png").attachment_id
           for _ in range(MAX_ATTACHMENTS_PER_MESSAGE + 1)]
    with pytest.raises(BadRequestException, match="最多携带"):
        store.bind("s1", ids)


def test_bind_duplicate_ids_count_once_toward_session_cap(tmp_path, monkeypatch) -> None:
    """同一 id 重复传只占一份会话额度（校验先于路径拼接的守门重构回归）。"""
    from movieclaw_api.services import agent_attachments

    monkeypatch.setattr(agent_attachments, "MAX_SESSION_ATTACHMENTS", 1)
    store = AgentAttachmentStore(tmp_path)
    meta = store.save_staging(png_bytes(), "a.png")
    bound = store.bind("s1", [meta.attachment_id, meta.attachment_id])
    assert [b.attachment_id for b in bound] == [meta.attachment_id, meta.attachment_id]


def test_read_base64_roundtrip(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    data = jpeg_bytes()
    meta = store.save_staging(data, "b.jpg")
    store.bind("s1", [meta.attachment_id])
    result = store.read_base64("s1", meta.attachment_id)
    assert result is not None
    encoded, got_meta = result
    assert got_meta.mime == "image/jpeg"
    import base64

    assert base64.b64decode(encoded) == data
    # 其他会话读不到
    assert store.read_base64("s2", meta.attachment_id) is None


def test_bound_file_path_missing_raises_404(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    with pytest.raises(NotFoundException):
        store.bound_file_path("s1", "a" * 32)


# ---------------------------------------------------------------------------
# fork 复制与清理
# ---------------------------------------------------------------------------


def test_copy_session_attachments_survives_source_deletion(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    meta = store.save_staging(png_bytes(), "a.png")
    store.bind("src", [meta.attachment_id])
    assert store.copy_session_attachments("src", "dst", {meta.attachment_id}) == 1
    store.delete_session_attachments("src")
    # 源会话删除后，新会话仍能按同 id 读到字节
    assert store.read_base64("dst", meta.attachment_id) is not None
    assert store.read_base64("src", meta.attachment_id) is None


def test_delete_session_attachments_removes_directory(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    meta = store.save_staging(png_bytes(), "a.png")
    store.bind("s1", [meta.attachment_id])
    store.delete_session_attachments("s1")
    assert not (tmp_path / "s1.assets").exists()
    store.delete_session_attachments("s1")  # 幂等


def test_cleanup_staging_reclaims_only_expired(tmp_path) -> None:
    store = AgentAttachmentStore(tmp_path)
    fresh = store.save_staging(png_bytes(), "fresh.png")
    stale = store.save_staging(png_bytes(), "stale.png")
    staging = tmp_path / ".staging"
    old = time.time() - 25 * 3600
    for suffix in ("png", "json"):
        path = staging / f"{stale.attachment_id}.{suffix}"
        import os

        os.utime(path, (old, old))
    assert store.cleanup_staging() == 1
    assert (staging / f"{fresh.attachment_id}.png").is_file()
    assert not (staging / f"{stale.attachment_id}.png").exists()
