"""飞书 Webhook 通道:凭据解析、签名、响应解析与「即绑即用」流程测试。

用假的 FeishuClient 驱动真实的 ImChannelService + ChannelManager +
dispatcher 链路,验证绑定即推送的全链路;客户端本身用 MockTransport
验证报文形状与飞书响应的错误分类。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable

import httpx
import pytest
from sqlmodel import SQLModel

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.feishu import FeishuAdapter, FeishuApiError, FeishuClient
from movieclaw_channel.feishu.client import (
    feishu_account_id,
    normalize_webhook_url,
    parse_credentials,
)
from movieclaw_channel.types import ReplyContext
from movieclaw_db.engine import dispose_db, init_db

_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abc-123"


@pytest.fixture
async def db(tmp_path):
    """临时 SQLite 库 + 临时密钥的加密器(通道凭据落库前会加密)。"""
    from movieclaw_db.crypto import init_secret_box, reset_secret_box

    reset_secret_box()
    init_secret_box(None, tmp_path / ".secret_key")
    database = init_db(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with database.engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield database
    await dispose_db()
    reset_secret_box()


async def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待条件超时")


# ---------------------------------------------------------------------------
# Webhook 地址与凭据解析
# ---------------------------------------------------------------------------


class TestWebhookUrl:
    def test_accepts_official_hosts(self) -> None:
        assert (
            normalize_webhook_url(f" {_URL} ") == _URL
        )  # 首尾空白归一化(粘贴常见)
        assert (
            normalize_webhook_url("https://open.larksuite.com/open-apis/bot/v2/hook/x1")
            == "https://open.larksuite.com/open-apis/bot/v2/hook/x1"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "http://open.feishu.cn/open-apis/bot/v2/hook/x1",  # 非 https
            "https://evil.example.com/open-apis/bot/v2/hook/x1",  # 非官方域(SSRF)
            "https://open.feishu.cn/other/path",  # 路径不是机器人 hook
            "https://open.feishu.cn/open-apis/bot/v2/hook/",  # 缺 hook 令牌
            "open.feishu.cn/open-apis/bot/v2/hook/x1",  # 缺协议
            "",
        ],
    )
    def test_rejects_bad_urls(self, bad: str) -> None:
        with pytest.raises(ValueError):
            normalize_webhook_url(bad)

    def test_account_id_extracts_hook_token(self) -> None:
        assert feishu_account_id(_URL) == "abc-123"

    def test_parse_credentials(self) -> None:
        assert parse_credentials(_URL) == (_URL, "")
        assert parse_credentials(json.dumps({"webhook_url": _URL, "secret": "s"})) == (_URL, "s")
        assert parse_credentials(json.dumps({"webhook_url": _URL, "secret": ""}))[1] == ""


# ---------------------------------------------------------------------------
# 客户端:报文形状与响应错误分类
# ---------------------------------------------------------------------------


def _expected_sign(timestamp: str, secret: str) -> str:
    # 飞书口径:string_to_sign(时间戳+换行+密钥)整体作 HMAC-SHA256 密钥,消息为空
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


def _client_with(token: str) -> FeishuClient:
    return FeishuClient(token)


async def _swap_transport(
    client: FeishuClient, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_send_text_payload_without_secret() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    client = _client_with(_URL)  # 裸 URL = 无签名
    await _swap_transport(client, handler)
    try:
        await client.send_text("hello")
    finally:
        await client.aclose()
    body = json.loads(captured["req"].content)
    assert body["msg_type"] == "text"
    assert body["content"]["text"] == "hello"
    assert "sign" not in body and "timestamp" not in body


async def test_send_text_signs_when_secret_present() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    client = _client_with(json.dumps({"webhook_url": _URL, "secret": "s3cret"}))
    await _swap_transport(client, handler)
    try:
        await client.send_text("hello")
    finally:
        await client.aclose()
    body = json.loads(captured["req"].content)
    assert body["sign"] == _expected_sign(body["timestamp"], "s3cret")


@pytest.mark.parametrize(
    ("status", "payload", "match", "auth_failed"),
    [
        (200, {"code": 19021, "msg": "Sign Match Fail"}, "签名校验失败", True),
        (200, {"code": 19024, "msg": "Key Words Not Found"}, "自定义关键词", False),
        (200, {"code": 9499, "msg": "Bad Request"}, "拒收消息", False),
        (404, {"msg": "not found"}, "机器人不存在", True),
        (502, None, "接口异常", False),
    ],
)
async def test_send_text_error_classification(
    status: int, payload: dict | None, match: str, auth_failed: bool
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if payload is None:
            return httpx.Response(status, text="<html>Bad Gateway</html>")
        return httpx.Response(status, json=payload)

    client = _client_with(json.dumps({"webhook_url": _URL, "secret": "s3cret"}))
    await _swap_transport(client, handler)
    try:
        with pytest.raises(FeishuApiError) as exc_info:
            await client.send_text("hello")
    finally:
        await client.aclose()
    assert match in str(exc_info.value)
    assert exc_info.value.auth_failed is auth_failed


# ---------------------------------------------------------------------------
# 适配器:纯推送语义
# ---------------------------------------------------------------------------


class RecordingClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def aclose(self) -> None:
        pass


async def test_adapter_send_text_delegates_to_client() -> None:
    client = RecordingClient()
    adapter = FeishuAdapter(client, "bot1")
    reply = ReplyContext(channel_id="feishu", account_id="bot1", user_id="group")
    await adapter.send_text(reply, "hi")
    assert client.sent == ["hi"]


async def test_adapter_run_idles_until_stop() -> None:
    adapter = FeishuAdapter(RecordingClient(), "bot1")
    stop = asyncio.Event()

    async def on_inbound(msg) -> None: ...
    async def save_cursor(cursor: str) -> None: ...

    ctx = ChannelContext(
        account_id="bot1", on_inbound=on_inbound, save_cursor=save_cursor, stop=stop
    )
    task = asyncio.create_task(adapter.run(ctx))
    await asyncio.sleep(0.02)
    assert not task.done()  # 无收消息循环,空转挂起
    stop.set()
    await asyncio.wait_for(task, timeout=2)


# ---------------------------------------------------------------------------
# 服务层:即绑即用全链路
# ---------------------------------------------------------------------------


class FakeFeishuClient:
    """替身客户端:bind_feishu 与 _start_account 各建一个,发送统一记到类列表。"""

    sent: list[str] = []
    instances: list[FakeFeishuClient] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.closed = False
        FakeFeishuClient.instances.append(self)

    async def send_text(self, text: str) -> None:
        FakeFeishuClient.sent.append(text)

    async def aclose(self) -> None:
        self.closed = True

    @classmethod
    def reset(cls) -> None:
        cls.sent = []
        cls.instances = []


@pytest.fixture
def _fake_feishu(monkeypatch):
    """假客户端要同时补到模块名与通道规格两处:bind_feishu 走前者,
    _start_account 走 _SPECS 里注册的 make_client(与 lifecycle 测试同款)。"""
    from movieclaw_api.services import im_channel

    FakeFeishuClient.reset()
    monkeypatch.setattr(im_channel, "FeishuClient", FakeFeishuClient)
    monkeypatch.setitem(
        im_channel._SPECS,
        "feishu",
        im_channel._ChannelSpec(
            channel_id="feishu",
            display_name="飞书",
            make_client=FakeFeishuClient,
            make_adapter=im_channel.FeishuAdapter,
            validate=im_channel._feishu_reject_pairing,
        ),
    )
    yield


async def test_bind_feishu_end_to_end(db, _fake_feishu) -> None:
    from movieclaw_api.services.im_channel import ImChannelService
    from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository

    service = ImChannelService()
    try:
        row = await service.bind_feishu(_URL, "s3cret")
        assert row.channel_id == "feishu"
        assert row.account_id == "abc-123"
        # 欢迎消息在绑定过程中已发出(连通性校验,群里当场可见)
        assert any("已接入本群" in text for text in FakeFeishuClient.sent)
        # 通道运行中,且无绑定用户也注册为推送目标
        await _wait_for(lambda: service.manager.is_running("feishu", "abc-123"))
        assert "feishu:abc-123" in service._push_targets
        # 推送经发送泵送达群机器人
        await service.push_text("hello")
        await _wait_for(lambda: "hello" in FakeFeishuClient.sent)
        # 落库凭据为 JSON 且加密可逆
        async with db.session() as session:
            stored = await ChannelAccountRepository(session).get("abc-123")
        assert stored is not None
        creds = json.loads(ChannelAccountRepository.decrypted_token(stored))
        assert creds == {"webhook_url": _URL, "secret": "s3cret"}
    finally:
        await service.stop()


async def test_bind_feishu_same_bot_rebind_overwrites(db, _fake_feishu) -> None:
    from movieclaw_api.services.im_channel import ImChannelService

    service = ImChannelService()
    try:
        await service.bind_feishu(_URL, "old")
        await service.bind_feishu(_URL, "new")
        rows = await _list_feishu()
        assert [r.account_id for r in rows] == ["abc-123"]
    finally:
        await service.stop()


async def _list_feishu():
    from movieclaw_db.engine import get_database
    from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository

    async with get_database().session() as session:
        rows = await ChannelAccountRepository(session).list_by_channel("feishu")
    return rows


async def test_bind_feishu_then_unbind(db, _fake_feishu) -> None:
    from movieclaw_api.services.im_channel import ImChannelService

    service = ImChannelService()
    try:
        await service.bind_feishu(_URL, "")
        assert await service.unbind("feishu", "abc-123") is True
        assert "feishu:abc-123" not in service._push_targets
        assert not service.manager.is_running("feishu", "abc-123")
    finally:
        await service.stop()


async def test_bind_feishu_rejects_bad_url() -> None:
    from movieclaw_api.services.im_channel import ImChannelService

    service = ImChannelService()
    # 内网地址必须在进入任何网络/落库动作前被拒(SSRF 防线)
    with pytest.raises(ValueError, match="不合法"):
        await service.bind_feishu("https://192.168.1.1/open-apis/bot/v2/hook/x1")
    await service.stop()


async def test_begin_binding_rejects_feishu() -> None:
    from movieclaw_api.services.im_channel import ImChannelService

    service = ImChannelService()
    with pytest.raises(ValueError, match="配对码"):
        await service.begin_binding("feishu", "tok-123456")
    await service.stop()
