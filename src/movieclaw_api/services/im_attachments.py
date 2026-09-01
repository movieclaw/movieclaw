"""IM 通道「入站图片入库 + 本轮 Agent 输入组装」（微信 / Telegram / Discord 共用）。

通道层负责「把图片字节取回来」（各平台协议不同，见各 adapter），到了这里
三家就完全一样了：压缩 → 落会话附件目录 → 组装成图文内容块。抽成一个函数
而不是各服务各写一遍，是因为限额、失败降级、提醒文案这些恰恰是最容易在两处
实现里写歪的部分。

**提醒优先于沉默**：图片处理链路上任何一处「用户预期落空」的情况——模型不支持
看图、图太多只看了前几张、坏图跳过——都返回一条中文提醒，由调用方原样发回
聊天窗口。IM 侧没有 Web 那样的模型选择器与视觉标记，用户唯一的反馈渠道就是
这句话，所以它必须是确定性的服务端文案，而不是「指望模型自己说清楚」。

与 Web 的分工：Web 由前端压图、由界面标注模型是否支持视觉；IM 两者都没有，
所以压缩（`shrink_for_attachment`）与提醒都在服务端做。落盘之后的链路
（转录只存引用、请求前水合、视觉门控降级）三端完全共用，见
docs/design/agent-image-input.md。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services.agent_attachments import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    AttachmentMeta,
    compose_user_content,
    get_agent_attachment_store,
    hydrate_images,
    shrink_for_attachment,
)
from movieclaw_channel.types import InboundImage, InboundMessage
from movieclaw_llm import ChatMessage, ContentPart, LlmError, LlmRouter, ModelInfo

logger = logging.getLogger("movieclaw_api.im_attachments")


@dataclass(slots=True)
class IngestResult:
    """入库结果：本轮 Agent 输入 + 要发回聊天窗口的提醒。

    ``content`` 为空串表示「纯图消息且图片全军覆没」——调用方应放弃本轮，
    不要往转录里写一条空的用户消息。
    """

    content: str | list[ContentPart]
    notices: list[str] = field(default_factory=list)


def _vision_notice(model_info: ModelInfo) -> str:
    """默认模型不支持看图时的确定性提醒（IM 侧唯一的反馈渠道）。"""
    return (
        f"当前模型「{model_info.id}」不支持看图，这张图我看不了"
        "（文字部分照常处理）。可在设置里把默认模型换成视觉模型"
        "（如 qwen3-vl-plus）；若它其实支持看图，在供应商设置的「补录模型」里"
        "为它勾上「视觉」后重发即可。"
    )


async def ingest_inbound_images(
    *,
    session_id: str,
    text: str,
    images: Sequence[InboundImage],
    model_info: ModelInfo | None,
) -> IngestResult:
    """把 IM 入站消息组装成本轮 Agent 输入，图片落进会话附件目录。

    规则（三通道一致）：

    1. **模型不支持视觉照样入库**：图片仍写进会话附件目录、消息里仍留引用，
       用户之后在 Web 里换个视觉模型 retry 同一条消息就能看图；本轮则由
       水合层的门控降级成占位文本，并配一条确定性提醒（见 `_vision_notice`）；
    2. 超过单条上限的图只取前 N 张，多出来的明确告知；
    3. 单张图失败（格式不认、压不动、绑定冲突）只跳过这一张，随图文字与
       其他图照常送进 Agent。
    """
    if not images:
        return IngestResult(content=text)

    store = get_agent_attachment_store()
    notices: list[str] = []
    if model_info is not None and "image" not in (model_info.modalities or []):
        notices.append(_vision_notice(model_info))

    kept = list(images)
    if len(kept) > MAX_ATTACHMENTS_PER_MESSAGE:
        notices.append(
            f"一次最多处理 {MAX_ATTACHMENTS_PER_MESSAGE} 张图片，"
            f"本轮只看前 {MAX_ATTACHMENTS_PER_MESSAGE} 张。"
        )
        kept = kept[:MAX_ATTACHMENTS_PER_MESSAGE]

    attachment_ids: list[str] = []
    for image in kept:
        try:
            # 压缩与校验都是 CPU/磁盘操作，放线程池避免卡住事件循环
            data = await asyncio.to_thread(shrink_for_attachment, image.data)
            meta = await asyncio.to_thread(store.save_staging, data, image.name)
        except BadRequestException as exc:
            logger.warning("IM 图片入库失败，已跳过：%s", exc.message)
            notices.append(f"有一张图片没能处理（{exc.message}）。")
            continue
        attachment_ids.append(meta.attachment_id)

    bound: list[AttachmentMeta] = []
    if attachment_ids:
        try:
            bound = await asyncio.to_thread(store.bind, session_id, attachment_ids)
        except BadRequestException as exc:
            logger.warning("IM 图片绑定会话失败：%s", exc.message)
            notices.append(exc.message)
    return IngestResult(content=compose_user_content(text, bound), notices=notices)


@dataclass(slots=True)
class PreparedInput:
    """一轮 IM 消息准备好的三件套。

    ``user_content`` 与 ``input_for_run`` 必须分开：前者是**引用态**（图片只有
    attachment_id），是落转录的形态；后者是水合后的请求投影（含 base64 字节与
    附件清单文本），只能进请求、不能进转录。
    """

    user_content: str | list[ContentPart]
    input_for_run: str | list[ContentPart]
    history_for_run: list[ChatMessage]


async def prepare_agent_input(
    *,
    llm_router: LlmRouter,
    session_id: str,
    msg: InboundMessage,
    history: list[ChatMessage],
    emit: Callable[[str], Awaitable[None]],
) -> PreparedInput | None:
    """把一条入站消息准备成可直接喂 runner 的输入（三通道唯一入口）。

    图片入库 → 提醒回执 → 请求水合（视觉门控 / 读字节 / 请求预算）。返回
    ``None`` 表示本轮该放弃（纯图消息且图片全部失败，提醒已经发出去了）。

    IM 侧不给用户选模型，一律按默认模型（``model=""``）判定视觉能力；路由解析
    失败时跳过水合，交给 runner 以 agent_error 呈现同一个错误，不在这里抢答。
    """
    try:
        model_info: ModelInfo | None = llm_router.get_model_info("")
    except LlmError:
        model_info = None

    ingested = await ingest_inbound_images(
        session_id=session_id, text=msg.text, images=msg.images, model_info=model_info
    )
    for notice in ingested.notices:
        await emit(f"⚠️ {notice}")
    if not ingested.content:
        return None

    if model_info is None:
        return PreparedInput(ingested.content, ingested.content, history)
    hydrated = await hydrate_images(
        [*history, ChatMessage(role="user", content=ingested.content)],
        session_id=session_id,
        model_info=model_info,
    )
    return PreparedInput(ingested.content, hydrated[-1].content, hydrated[:-1])
