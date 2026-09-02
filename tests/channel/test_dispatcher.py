"""ChannelDispatcher:分片、去重、鉴权、同会话串行、/stop 与 /reset、图片聚合。"""

from __future__ import annotations

import asyncio

from movieclaw_channel import dispatcher as dispatcher_mod
from movieclaw_channel.dispatcher import make_dispatcher, split_message
from movieclaw_channel.types import (
    InboundImage,
    InboundMessage,
    OutboundEnvelope,
    ReplyContext,
)

# ---------------------------------------------------------------------------
# split_message(纯函数)
# ---------------------------------------------------------------------------


def test_split_short_text_untouched():
    assert split_message("你好", 100) == ["你好"]


def test_split_empty_text():
    assert split_message("  \n ", 100) == []


def test_split_prefers_paragraph_boundary():
    text = "第一段。\n\n第二段。\n\n第三段。"
    chunks = split_message(text, 12)
    assert all(len(c) <= 12 for c in chunks)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


def test_split_hard_cuts_oversized_paragraph():
    text = "字" * 25
    chunks = split_message(text, 10)
    assert chunks == ["字" * 10, "字" * 10, "字" * 5]


# ---------------------------------------------------------------------------
# dispatcher 行为(用假 adapter 驱动)
# ---------------------------------------------------------------------------


class FakeAdapter:
    channel_id = "weixin"
    max_text_len = 50

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def run(self, ctx) -> None:  # pragma: no cover - dispatcher 测试不走收循环
        raise NotImplementedError

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        self.sent.append((reply.user_id, text))


def _msg(
    text: str,
    *,
    msg_id: str,
    user: str = "u1@im.wechat",
    images: tuple[InboundImage, ...] = (),
) -> InboundMessage:
    reply = ReplyContext(channel_id="weixin", account_id="bot1", user_id=user)
    return InboundMessage(
        channel_id="weixin",
        account_id="bot1",
        user_id=user,
        text=text,
        reply=reply,
        provider_message_id=msg_id,
        images=images,
    )


async def _drain(dispatcher, adapter, *, expect: int, timeout: float = 2.0) -> None:
    """等发送泵把 expect 条消息发完(轮询,避免依赖内部实现)。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(adapter.sent) < expect:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"等待发送超时:已发 {len(adapter.sent)}/{expect}")
        await asyncio.sleep(0.01)


async def test_serial_processing_and_reply():
    """同会话消息按序处理,回复经出站泵送达。"""
    adapter = FakeAdapter()
    order: list[str] = []

    async def run_agent(msg, emit):
        order.append(msg.text)
        await emit(f"回复:{msg.text}")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("一", msg_id="m1"))
        await d.submit_inbound(_msg("二", msg_id="m2"))
        await _drain(d, adapter, expect=2)
        assert order == ["一", "二"]
        assert [t for _, t in adapter.sent] == ["回复:一", "回复:二"]
    finally:
        await d.close()


async def test_dedup_and_auth():
    adapter = FakeAdapter()
    handled: list[str] = []

    async def run_agent(msg, emit):
        handled.append(msg.provider_message_id)

    d = make_dispatcher(
        adapter,
        is_allowed=lambda u: u == "owner@im.wechat",
        run_agent=run_agent,
    )
    d.start()
    try:
        await d.submit_inbound(_msg("hi", msg_id="dup", user="owner@im.wechat"))
        await d.submit_inbound(_msg("hi", msg_id="dup", user="owner@im.wechat"))  # 重复:丢
        await d.submit_inbound(_msg("hi", msg_id="other", user="stranger@im.wechat"))  # 未授权:丢
        await asyncio.sleep(0.1)
        assert handled == ["dup"]
    finally:
        await d.close()


async def test_reset_command():
    adapter = FakeAdapter()
    resets: list[str] = []

    async def run_agent(msg, emit):  # pragma: no cover - 命令不进 Agent
        raise AssertionError("命令消息不应进入 Agent")

    async def reset_session(key: str) -> None:
        resets.append(key)

    d = make_dispatcher(
        adapter,
        is_allowed=lambda _u: True,
        run_agent=run_agent,
        reset_session=reset_session,
    )
    d.start()
    try:
        await d.submit_inbound(_msg("/reset", msg_id="c1"))
        await _drain(d, adapter, expect=1)
        assert resets == ["weixin:bot1:u1@im.wechat"]
        assert "会话已重置" in adapter.sent[0][1]
    finally:
        await d.close()


async def test_stop_cancels_running_agent():
    adapter = FakeAdapter()
    started = asyncio.Event()

    async def run_agent(msg, emit):
        started.set()
        await asyncio.sleep(30)  # 模拟长任务,等着被 /stop 取消

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("跑个长任务", msg_id="m1"))
        await asyncio.wait_for(started.wait(), timeout=2)
        await d.submit_inbound(_msg("/stop", msg_id="m2"))
        # /stop 只回执一条,worker 侧不再重复发「已取消」
        await _drain(d, adapter, expect=1)
        await asyncio.sleep(0.05)
        texts = [t for _, t in adapter.sent]
        assert texts == ["已取消当前处理。"]
    finally:
        await d.close()


async def test_dead_worker_is_revived():
    """worker 意外死亡后,下一条消息触发自动复活,会话不永久卡死。"""
    adapter = FakeAdapter()
    calls: list[str] = []

    async def run_agent(msg, emit):
        calls.append(msg.text)
        await emit(f"回复:{msg.text}")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("一", msg_id="m1"))
        await _drain(d, adapter, expect=1)
        # 模拟 worker 被意外终止(等价于内部未预期崩溃后任务结束)
        session = d._sessions["weixin:bot1:u1@im.wechat"]  # noqa: SLF001 -- 测试内部状态
        assert session.worker is not None
        session.worker.cancel()
        await asyncio.sleep(0.05)
        assert session.worker.done()

        await d.submit_inbound(_msg("还活着吗", msg_id="m2"))
        await _drain(d, adapter, expect=2)
        assert calls == ["一", "还活着吗"]
        assert adapter.sent[-1][1] == "回复:还活着吗"
    finally:
        await d.close()


async def test_long_reply_is_chunked():
    adapter = FakeAdapter()

    async def run_agent(msg, emit):
        await emit("段落甲" * 10 + "\n\n" + "段落乙" * 10)  # 60 字,上限 50

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("hi", msg_id="m1"))
        await _drain(d, adapter, expect=2)
        assert all(len(t) <= 50 for _, t in adapter.sent)
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# 图文推送(send_photo 可选能力与纯文本回退)
# ---------------------------------------------------------------------------


class FakePhotoAdapter(FakeAdapter):
    """带 send_photo 能力的假 adapter;fail_photo 置位时模拟发图失败。"""

    def __init__(self, *, fail_photo: bool = False) -> None:
        super().__init__()
        self.sent_photos: list[tuple[str, bytes, str]] = []
        self._fail_photo = fail_photo

    async def send_photo(self, reply: ReplyContext, photo: bytes, caption: str) -> None:
        if self._fail_photo:
            raise RuntimeError("模拟发图失败")
        self.sent_photos.append((reply.user_id, photo, caption))


def _push_env(text: str, photo: bytes | None) -> OutboundEnvelope:
    reply = ReplyContext(channel_id="weixin", account_id="bot1", user_id="u1@im.wechat")
    return OutboundEnvelope(reply=reply, text=text, origin="push", photo=photo)


async def _drain_photos(adapter: FakePhotoAdapter, *, expect: int, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while len(adapter.sent_photos) < expect:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"等待发图超时:已发 {len(adapter.sent_photos)}/{expect}")
        await asyncio.sleep(0.01)


async def _noop_agent(msg, emit):  # pragma: no cover - 图文测试不走 Agent
    raise AssertionError("不应进入 Agent")


async def test_photo_sent_with_caption():
    """有发图能力时图文一条发出,不再走文本路径。"""
    adapter = FakePhotoAdapter()
    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=_noop_agent)
    d.start()
    try:
        await d.push_outbound(_push_env("已入库:《某片》", b"\x89PNG"))
        await _drain_photos(adapter, expect=1)
        assert adapter.sent_photos == [("u1@im.wechat", b"\x89PNG", "已入库:《某片》")]
        assert adapter.sent == []
    finally:
        await d.close()


async def test_photo_falls_back_without_capability():
    """adapter 无 send_photo 时(微信),照常发纯文本。"""
    adapter = FakeAdapter()
    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=_noop_agent)
    d.start()
    try:
        await d.push_outbound(_push_env("已入库:《某片》", b"\x89PNG"))
        await _drain(d, adapter, expect=1)
        assert adapter.sent == [("u1@im.wechat", "已入库:《某片》")]
    finally:
        await d.close()


async def test_photo_send_failure_falls_back_to_text():
    """发图抛错时退回纯文本,文字不丢。"""
    adapter = FakePhotoAdapter(fail_photo=True)
    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=_noop_agent)
    d.start()
    try:
        await d.push_outbound(_push_env("已入库:《某片》", b"\x89PNG"))
        await _drain(d, adapter, expect=1)
        assert adapter.sent == [("u1@im.wechat", "已入库:《某片》")]
        assert adapter.sent_photos == []
    finally:
        await d.close()


async def test_oversized_caption_skips_photo():
    """文案超 caption 上限(1000 字符)时放弃随图发送,走文本分片。"""
    adapter = FakePhotoAdapter()
    adapter.max_text_len = 3900
    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=_noop_agent)
    d.start()
    try:
        await d.push_outbound(_push_env("字" * 1001, b"\x89PNG"))
        await _drain(d, adapter, expect=1)
        assert adapter.sent_photos == []
        assert adapter.sent[0][1] == "字" * 1001
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# 图片消息(docs/design/agent-image-input.md §10)
# ---------------------------------------------------------------------------


def _image(tag: bytes = b"\x89PNG") -> InboundImage:
    return InboundImage(data=tag, name="微信图片1")


async def test_unsupported_message_type_gets_hint():
    """既无文字也无图片(视频/文件等还没接):明确回执,不静默丢弃。"""
    adapter = FakeAdapter()

    async def run_agent(msg, emit):  # pragma: no cover - 不该进 Agent
        raise AssertionError("空内容消息不应进入 Agent")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("", msg_id="m1"))
        await _drain(d, adapter, expect=1)
        assert adapter.sent[0][1] == "暂时只支持文字和图片消息哦。"
    finally:
        await d.close()


async def test_pure_image_message_reaches_agent(monkeypatch):
    """纯图消息(用户只甩了张图)照常触发 Agent。"""
    monkeypatch.setattr(dispatcher_mod, "_IMAGE_AGGREGATE_WINDOW_S", 0.05)
    adapter = FakeAdapter()
    seen: list[InboundMessage] = []

    async def run_agent(msg, emit):
        seen.append(msg)
        await emit("收到")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("", msg_id="m1", images=(_image(),)))
        await _drain(d, adapter, expect=1)
        assert len(seen) == 1
        assert seen[0].images == (_image(),)
    finally:
        await d.close()


async def test_image_then_text_merged_into_one_run(monkeypatch):
    """「先甩图、再打字」合并成一轮:不白跑一次「请问要做什么」。"""
    monkeypatch.setattr(dispatcher_mod, "_IMAGE_AGGREGATE_WINDOW_S", 1.0)
    adapter = FakeAdapter()
    seen: list[InboundMessage] = []

    async def run_agent(msg, emit):
        seen.append(msg)
        await emit("收到")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("", msg_id="m1", images=(_image(b"a"),)))
        await d.submit_inbound(_msg("", msg_id="m2", images=(_image(b"b"),)))
        await d.submit_inbound(_msg("这两张有什么区别", msg_id="m3"))
        await _drain(d, adapter, expect=1)
        await asyncio.sleep(0.1)
        assert len(seen) == 1
        assert seen[0].text == "这两张有什么区别"
        assert [i.data for i in seen[0].images] == [b"a", b"b"]
    finally:
        await d.close()


async def test_text_message_is_not_delayed(monkeypatch):
    """纯文字对话不进聚合窗口:一秒都不等。"""
    monkeypatch.setattr(dispatcher_mod, "_IMAGE_AGGREGATE_WINDOW_S", 30.0)
    adapter = FakeAdapter()

    async def run_agent(msg, emit):
        await emit(f"回复:{msg.text}")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("在吗", msg_id="m1"))
        await _drain(d, adapter, expect=1, timeout=2.0)
        assert adapter.sent[0][1] == "回复:在吗"
    finally:
        await d.close()
