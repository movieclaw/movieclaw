"""Agent 会话图片附件的存储与水合（docs/design/agent-image-input.md）。

核心不变量：**转录里只存引用（attachment_id），供应商请求里才有字节。**

目录布局（附件是会话的一部分，与转录同生命周期、同目录备份）：

    data/agent-sessions/
      <session_id>.jsonl            # 转录（既有）
      <session_id>.assets/          # 本会话的附件
        <attachment_id>.jpg         #   原始字节，扩展名按嗅探出的真实类型
        <attachment_id>.json        #   sidecar 元数据
      .staging/                     # 上传后尚未绑定会话的中转区

生命周期：

- 上传 → 落 ``.staging/``（此刻可能还没有 session_id）；
- 绑定 → ``session.start`` 引用附件时 ``os.rename`` 进会话 assets 目录
  （同一文件系统内原子）。顺序固定为**先 move、后落消息行**：move 后崩溃
  只留孤儿文件（清理兜底），反过来会留下用户看得见的悬空引用。并发两次
  引用同一 staging 附件时，第二个 rename 天然 ``FileNotFoundError``，
  报「附件已被使用」，无需加锁；
- 删除会话 → 连 assets 目录一起删；
- 孤儿回收 → ``.staging/`` 里超过 24h 的附件惰性清理。

安全：attachment_id 一律先过 32 位 hex 校验再参与路径拼接（防路径穿越）；
图片格式以魔数嗅探为准（拒绝 SVG / 改后缀的任意文件），Pillow 二次校验
可解码性与尺寸。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import uuid as uuid_mod
from base64 import b64encode
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image
from pydantic import BaseModel

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_llm import ChatMessage, ContentPart, ImagePart, ModelInfo, TextPart

logger = logging.getLogger("movieclaw_api.agent_attachments")

# ---------------------------------------------------------------------------
# 限额（分级：单图 / 单消息 / 单请求 / 单会话）
# ---------------------------------------------------------------------------

#: 单张图片原始字节上限（对齐主流视觉 API 的单图限制，给 base64 膨胀留余量）
MAX_IMAGE_BYTES = 5 * 1024 * 1024
#: 单条消息最多携带的图片数
MAX_ATTACHMENTS_PER_MESSAGE = 4
#: 单次运行水合的图片原始字节总预算（base64 后约 10.7MB，给正文与工具
#: schema 留出供应商请求体上限的余量）
MAX_REQUEST_IMAGE_BYTES = 8 * 1024 * 1024
#: 单会话附件总数上限（防滥用刷盘）
MAX_SESSION_ATTACHMENTS = 100
#: 图片最长边上限（超过提示用户缩图；Web 前端上传前已压到 2048）
MAX_IMAGE_EDGE_PX = 8000
#: staging 孤儿附件的回收时限
STAGING_TTL_SECONDS = 24 * 60 * 60

#: 魔数 → (mime, 扩展名)。只认这四种位图；SVG 等可内嵌脚本的格式天然进不来
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GIF87A = b"GIF87a"
_GIF89A = b"GIF89a"
_RIFF = b"RIFF"
_WEBP = b"WEBP"

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def sniff_image_mime(data: bytes) -> str | None:
    """按魔数嗅探真实图片类型；不认识返回 None（不信 Content-Type 与扩展名）。"""
    if data.startswith(_PNG):
        return "image/png"
    if data.startswith(_JPEG):
        return "image/jpeg"
    if data.startswith(_GIF87A) or data.startswith(_GIF89A):
        return "image/gif"
    if data.startswith(_RIFF) and data[8:12] == _WEBP:
        return "image/webp"
    return None


class AttachmentMeta(BaseModel):
    """附件 sidecar 元数据（<attachment_id>.json 的内容）。"""

    attachment_id: str
    mime: str
    bytes: int
    width: int
    height: int
    original_name: str
    created_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _validate_attachment_id(attachment_id: str) -> str:
    """attachment_id 必须是 32 位 hex（uuid4().hex）。

    这是路径拼接前的硬闸：不合法的 id 可能携带路径穿越片段（``../``），
    绝不允许进入文件系统操作。
    """
    if (
        len(attachment_id) == 32
        and all(c in "0123456789abcdef" for c in attachment_id)
    ):
        return attachment_id
    raise BadRequestException("附件编号格式不正确")


class AgentAttachmentStore:
    """会话图片附件的文件存储（与 AgentSessionStore 同根目录）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def _staging_dir(self) -> Path:
        return self._root / ".staging"

    def _assets_dir(self, session_id: str) -> Path:
        return self._root / f"{session_id}.assets"

    def _file_pair(self, directory: Path, meta: AttachmentMeta) -> tuple[Path, Path]:
        ext = _MIME_EXT[meta.mime]
        return (
            directory / f"{meta.attachment_id}.{ext}",
            directory / f"{meta.attachment_id}.json",
        )

    def _read_meta(self, directory: Path, attachment_id: str) -> AttachmentMeta | None:
        sidecar = directory / f"{attachment_id}.json"
        try:
            return AttachmentMeta.model_validate_json(sidecar.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001 - sidecar 损坏视同不存在，由调用方给中文报错
            logger.warning("附件 sidecar 无法解析，按不存在处理：%s", sidecar)
            return None

    # ------------------------------------------------------------------
    # 上传（staging）
    # ------------------------------------------------------------------
    def save_staging(self, data: bytes, original_name: str) -> AttachmentMeta:
        """校验并保存一张上传图片到 staging 区，返回元数据。

        校验链：非空 → 字节数 → 魔数 → Pillow 可解码 + 尺寸。全部通过才落盘，
        任何一步失败都以中文 BadRequest 报错（非开发者也能看懂并自行处理）。
        """
        if not data:
            raise BadRequestException("上传的图片为空，请重新选择")
        if len(data) > MAX_IMAGE_BYTES:
            limit_mb = MAX_IMAGE_BYTES // (1024 * 1024)
            raise BadRequestException(f"图片过大，请控制在 {limit_mb}MB 以内")
        mime = sniff_image_mime(data)
        if mime is None:
            raise BadRequestException("不支持的图片格式，请上传 JPG / PNG / WebP / GIF 图片")
        try:
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
                img.verify()
        except Exception as exc:  # noqa: BLE001 - Pillow 的异常族杂，统一转中文提示
            raise BadRequestException(f"图片无法解码，可能已损坏：{exc}") from exc
        if max(width, height) > MAX_IMAGE_EDGE_PX:
            raise BadRequestException(
                f"图片尺寸 {width}x{height} 超过最长边 {MAX_IMAGE_EDGE_PX}px 的限制，"
                "请缩小后重新上传"
            )

        meta = AttachmentMeta(
            attachment_id=uuid_mod.uuid4().hex,
            mime=mime,
            bytes=len(data),
            width=width,
            height=height,
            original_name=(original_name or "图片").strip()[:120] or "图片",
            created_at=_now_iso(),
        )
        staging = self._staging_dir()
        staging.mkdir(parents=True, exist_ok=True)
        file_path, sidecar_path = self._file_pair(staging, meta)
        file_path.write_bytes(data)
        sidecar_path.write_text(meta.model_dump_json(), encoding="utf-8")
        return meta

    # ------------------------------------------------------------------
    # 绑定（staging → 会话 assets）
    # ------------------------------------------------------------------
    def bind(self, session_id: str, attachment_ids: list[str]) -> list[AttachmentMeta]:
        """把 staging 附件移入会话 assets 目录，返回按入参顺序的元数据。

        已在**本会话** assets 里的 id 视为绑定成功（retry 沿用原附件的路径）；
        staging 与本会话都找不到 → 附件不存在或已被别的会话使用。任何一个 id
        失败即整体报错，不做半绑定——调用方在落消息之前调用，失败时不产生
        悬空引用。
        """
        if len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise BadRequestException(f"单条消息最多携带 {MAX_ATTACHMENTS_PER_MESSAGE} 张图片")
        # 不变量守门：所有 id 先过 32 位 hex 校验，之后才允许参与路径拼接
        attachment_ids = [_validate_attachment_id(a) for a in attachment_ids]
        assets = self._assets_dir(session_id)
        staging = self._staging_dir()

        existing = self.count_session_attachments(session_id)
        # 去重后计数：同一 id 重复传只占一份会话上限额度
        pending = {a for a in attachment_ids if self._read_meta(assets, a) is None}
        if existing + len(pending) > MAX_SESSION_ATTACHMENTS:
            raise BadRequestException(
                f"会话附件数量已达上限（{MAX_SESSION_ATTACHMENTS} 张），"
                "请开启新会话后再发送图片"
            )

        out: list[AttachmentMeta] = []
        for attachment_id in attachment_ids:
            bound = self._read_meta(assets, attachment_id)
            if bound is not None:
                out.append(bound)
                continue
            meta = self._read_meta(staging, attachment_id)
            if meta is None:
                raise BadRequestException("附件不存在、已过期或已被其他会话使用，请重新上传")
            assets.mkdir(parents=True, exist_ok=True)
            src_file, src_sidecar = self._file_pair(staging, meta)
            dst_file, dst_sidecar = self._file_pair(assets, meta)
            try:
                # 先 sidecar 后字节：字节文件是「附件存在」的判据（read_bytes
                # 找不到它才报错），反序崩溃会出现「有字节没元数据」的半态
                os.replace(src_sidecar, dst_sidecar)
                os.replace(src_file, dst_file)
            except FileNotFoundError as exc:
                raise BadRequestException(
                    "附件不存在、已过期或已被其他会话使用，请重新上传"
                ) from exc
            out.append(meta)
        return out

    # ------------------------------------------------------------------
    # 读取（下载路由与水合共用）
    # ------------------------------------------------------------------
    def read_meta(self, session_id: str, attachment_id: str) -> AttachmentMeta | None:
        """读会话内附件的元数据；不存在返回 None。"""
        return self._read_meta(self._assets_dir(session_id), _validate_attachment_id(attachment_id))

    def bound_file_path(self, session_id: str, attachment_id: str) -> Path:
        """会话内附件的文件路径（下载路由用）；不存在抛 404。"""
        meta = self.read_meta(session_id, attachment_id)
        if meta is None:
            raise NotFoundException("附件不存在或已被清理")
        file_path, _ = self._file_pair(self._assets_dir(session_id), meta)
        if not file_path.is_file():
            raise NotFoundException("附件不存在或已被清理")
        return file_path

    def read_base64(self, session_id: str, attachment_id: str) -> tuple[str, AttachmentMeta] | None:
        """读会话内附件字节并编码 base64（水合用）；缺文件或元数据返回 None。"""
        meta = self.read_meta(session_id, attachment_id)
        if meta is None:
            return None
        file_path, _ = self._file_pair(self._assets_dir(session_id), meta)
        try:
            data = file_path.read_bytes()
        except FileNotFoundError:
            return None
        return b64encode(data).decode("ascii"), meta

    def count_session_attachments(self, session_id: str) -> int:
        assets = self._assets_dir(session_id)
        if not assets.is_dir():
            return 0
        return sum(1 for p in assets.glob("*.json"))

    # ------------------------------------------------------------------
    # 复制（fork：新会话不依赖源文件）
    # ------------------------------------------------------------------
    def copy_session_attachments(
        self, source_session_id: str, target_session_id: str, attachment_ids: set[str]
    ) -> int:
        """把源会话中被引用的附件按**同 id** 复制到新会话目录，返回复制条数。

        目录按会话隔离，同 id 不冲突，快照里的引用无需改写。源附件缺失时
        跳过（新会话水合时会降级为占位文本），不阻断 fork。
        """
        copied = 0
        for raw_id in attachment_ids:
            try:
                attachment_id = _validate_attachment_id(raw_id)
            except BadRequestException:
                logger.warning("fork 复制附件时遇到非法编号，已跳过：%r", raw_id)
                continue
            meta = self._read_meta(self._assets_dir(source_session_id), attachment_id)
            if meta is None:
                continue
            src_file, _ = self._file_pair(self._assets_dir(source_session_id), meta)
            try:
                data = src_file.read_bytes()
            except FileNotFoundError:
                continue
            assets = self._assets_dir(target_session_id)
            assets.mkdir(parents=True, exist_ok=True)
            dst_file, dst_sidecar = self._file_pair(assets, meta)
            dst_sidecar.write_text(meta.model_dump_json(), encoding="utf-8")
            dst_file.write_bytes(data)
            copied += 1
        return copied

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def delete_session_attachments(self, session_id: str) -> None:
        """删除会话的整个 assets 目录（幂等）。"""
        assets = self._assets_dir(session_id)
        if not assets.is_dir():
            return
        for path in assets.iterdir():
            path.unlink(missing_ok=True)
        try:
            assets.rmdir()
        except OSError:  # 目录非空（并发写入）就留着，下次删除再收
            logger.warning("会话附件目录未能删净，将在下次删除时重试：%s", assets)

    def cleanup_staging(self, *, now: float | None = None) -> int:
        """回收 staging 里超过 TTL 的孤儿附件（上传了但没发送），返回清理条数。"""
        staging = self._staging_dir()
        if not staging.is_dir():
            return 0
        cutoff = (now if now is not None else time.time()) - STAGING_TTL_SECONDS
        removed = 0
        for sidecar in staging.glob("*.json"):
            try:
                if sidecar.stat().st_mtime > cutoff:
                    continue
            except FileNotFoundError:
                continue  # 与并发绑定竞争，文件刚被 move 走
            attachment_id = sidecar.stem
            meta = self._read_meta(staging, attachment_id)
            if meta is not None:
                file_path, _ = self._file_pair(staging, meta)
                file_path.unlink(missing_ok=True)
            else:
                # sidecar 损坏：按已知扩展名逐个尝试删除字节文件
                for ext in _MIME_EXT.values():
                    (staging / f"{attachment_id}.{ext}").unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            removed += 1
        if removed:
            logger.info("已回收 %d 个未发送的过期图片附件", removed)
        return removed


# ---------------------------------------------------------------------------
# 请求水合（发模型前把引用换成字节；门控 / 缺文件 / 预算三级降级）
# ---------------------------------------------------------------------------

#: 附件提醒文本的固定前缀。它只存在于请求投影（发给模型的消息副本）里：
#: 落盘时由 AgentSessionStore 按此前缀剥除，前端永远看不到这段样板文案
#: （maka 定案："presentation layers never show the folded form"）。
ATTACHMENT_NOTE_PREFIX = "[图片附件]"


def _gate_placeholder(part: ImagePart) -> str:
    name = part.name or "未命名"
    return (
        f"[用户发送了图片 {name}，当前模型不支持图片输入，无法查看。"
        "请告知用户：可在设置中切换视觉模型（如 qwen3-vl-plus）；"
        "若当前模型实际支持视觉，请在供应商设置的「补录模型」中为它声明"
        " modalities 后重试。]"
    )


def _missing_placeholder(part: ImagePart) -> str:
    name = part.name or "未命名"
    return f"[图片 {name} 已过期或被清理，无法查看；如需请让用户重新发送。]"


_BUDGET_PLACEHOLDER = "[更早的一张图片因请求体积限制已省略；如仍需要请让用户重发。]"


def _attachment_note(images: list[ImagePart], has_user_text: bool) -> str:
    """带图 user 消息的附件清单（确定性生成，跨请求稳定，永不落库）。"""
    listing = "、".join(
        f"{p.name or '未命名'}（{p.media_type or 'image'}）" for p in images
    )
    note = f"{ATTACHMENT_NOTE_PREFIX} 本条消息附带 {len(images)} 张图片：{listing}。"
    if not has_user_text:
        note += "用户未附文字，请直接查看图片内容并回应。"
    return note


def _is_attachment_note(part: ContentPart) -> bool:
    return isinstance(part, TextPart) and part.text.startswith(ATTACHMENT_NOTE_PREFIX)


async def hydrate_images(
    messages: list[ChatMessage],
    *,
    session_id: str,
    model_info: ModelInfo,
    store: AgentAttachmentStore | None = None,
) -> list[ChatMessage]:
    """把消息里的引用型 ImagePart 换成可发送形态，返回新列表（原列表不动）。

    两个调用点共用（发起运行的编排层 + 手动压缩接口），规则按优先级：

    1. 视觉门控（fail-closed）：模型未声明 image modality 时所有图片替换为
       占位文本，模型能向用户解释并给出 extra_models 声明的出路；
    2. 读文件：按引用从会话 assets 目录读字节 → base64；缺文件降级占位；
    3. 请求级预算：图片原始字节总量 ≤ MAX_REQUEST_IMAGE_BYTES，**从最新往旧
       保留**（用户最近发的图才是当前任务对象），超预算的旧图替换占位。

    带图的 user 消息在末尾追加附件清单文本（ATTACHMENT_NOTE_PREFIX 开头），
    让模型跨轮记得图片的存在；它只属于请求投影，落盘时被剥除。
    """
    attachment_store = store if store is not None else get_agent_attachment_store()
    vision = "image" in (model_info.modalities or [])

    # 收集全部引用块的位置；没有图的消息原样透传
    refs: list[tuple[int, int, ImagePart]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message.content, list):
            continue
        for part_index, part in enumerate(message.content):
            if isinstance(part, ImagePart) and part.attachment_id:
                refs.append((message_index, part_index, part))
    if not refs:
        return list(messages)

    def read_meta_safe(attachment_id: str) -> AttachmentMeta | None:
        try:
            return attachment_store.read_meta(session_id, attachment_id)
        except BadRequestException:
            return None  # 历史里混入了非法 id：按缺失处理，不让整次运行失败

    # 预算判定：倒序（最新优先）按 sidecar 记录的原始字节数装入
    kept: set[tuple[int, int]] = set()
    if vision:
        remaining = MAX_REQUEST_IMAGE_BYTES
        for message_index, part_index, part in reversed(refs):
            meta = await asyncio.to_thread(read_meta_safe, part.attachment_id or "")
            if meta is None:
                continue  # 缺文件走 missing 占位，不占预算
            if meta.bytes <= remaining:
                kept.add((message_index, part_index))
                remaining -= meta.bytes
    omitted_by_budget = 0

    out: list[ChatMessage] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message.content, list) or not any(
            r[0] == message_index for r in refs
        ):
            out.append(message)
            continue
        parts: list[ContentPart] = []
        images_in_message: list[ImagePart] = []
        for part_index, part in enumerate(message.content):
            if not (isinstance(part, ImagePart) and part.attachment_id):
                # 历史里可能残留旧的清单文本（理论上落盘已剥除），跳过防重复
                if not _is_attachment_note(part):
                    parts.append(part)
                continue
            images_in_message.append(part)
            if not vision:
                parts.append(TextPart(text=_gate_placeholder(part)))
                continue
            if (message_index, part_index) not in kept:
                result = None
            else:
                result = await asyncio.to_thread(
                    attachment_store.read_base64, session_id, part.attachment_id or ""
                )
            if result is not None:
                encoded, meta = result
                parts.append(part.model_copy(update={"data": encoded, "media_type": meta.mime}))
            elif (message_index, part_index) in kept or (
                await asyncio.to_thread(read_meta_safe, part.attachment_id or "") is None
            ):
                # 判定保留但读取失败（绑定后被清理），或元数据本就缺失
                parts.append(TextPart(text=_missing_placeholder(part)))
            else:
                omitted_by_budget += 1
                parts.append(TextPart(text=_BUDGET_PLACEHOLDER))
        # 注文只在至少一张图真正进入请求时追加：图全部降级为占位文本的消息
        # 是纯文本，dehydrate 的「含图才剥注文」判据会失效，注文将泄漏进
        # 压缩 replacement_history 落库（违反「注文永不落库」的不变量）
        if (
            vision
            and message.role == "user"
            and any(isinstance(p, ImagePart) for p in parts)
        ):
            has_user_text = any(
                isinstance(p, TextPart) and p.text.strip() and not _is_attachment_note(p)
                for p in message.content
            )
            parts.append(TextPart(text=_attachment_note(images_in_message, has_user_text)))
        out.append(message.model_copy(update={"content": parts}))

    if omitted_by_budget:
        logger.info(
            "会话 %s 的 %d 张历史图片超出单请求预算（%dMB），已替换为占位文本",
            session_id,
            omitted_by_budget,
            MAX_REQUEST_IMAGE_BYTES // (1024 * 1024),
        )
    return out


def compose_user_content(
    text: str, attachments: list[AttachmentMeta]
) -> str | list[ContentPart]:
    """把用户正文与已绑定附件组装成消息内容。

    无附件时保持纯字符串形态（兼容端点对字符串 content 最稳，现有行为零变化）；
    带附件时是 text/image 内容块列表，图片为**引用型** ImagePart——字节永不
    进消息，落转录即引用形态，发请求前由水合层补 data。
    """
    if not attachments:
        return text
    parts: list[ContentPart] = []
    if text:
        parts.append(TextPart(text=text))
    parts.extend(
        ImagePart(
            attachment_id=meta.attachment_id,
            media_type=meta.mime,
            name=meta.original_name,
        )
        for meta in attachments
    )
    return parts


def extract_attachment_ids(content: str | list[ContentPart]) -> list[str]:
    """取消息内容里引用型图片块的 attachment_id 列表（retry 沿用原附件用）。"""
    if isinstance(content, str):
        return []
    return [
        p.attachment_id
        for p in content
        if isinstance(p, ImagePart) and p.attachment_id
    ]


_store: AgentAttachmentStore | None = None


def get_agent_attachment_store() -> AgentAttachmentStore:
    """进程级单例：与会话转录同根目录（data/agent-sessions）。"""
    global _store
    if _store is None:
        from movieclaw_api.core.config import get_settings

        _store = AgentAttachmentStore(Path(get_settings().agent_sessions_dir).resolve())
    return _store


def reset_agent_attachment_store() -> None:
    """重置单例（测试隔离用：换目录后重新构建）。"""
    global _store
    _store = None
