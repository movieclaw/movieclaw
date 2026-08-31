"""思维链档位的 API 链路：三态传参、信封持久化、续聊/retry 沿用。

对应 docs/design/agent-thinking-level.md §4.3：
- 显式档位 / "default" 清回默认 / 未传沿用最近一条 user 消息；
- 生效值存转录信封（零迁移），transcript 透出供前端初始化选择器；
- 档位进入每次模型调用的 ModelSettings。
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from tests.api.test_agent import _StreamProtocol

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.agent_attachments import reset_agent_attachment_store
from movieclaw_api.services.agent_sessions import reset_agent_session_store
from movieclaw_llm.protocols import PROTOCOLS

#: 每次模型调用的 settings.thinking_level 记录（按调用顺序）
captured_levels: list[str | None] = []


class _CapturingProtocol(_StreamProtocol):
    async def chat_stream(self, request, model_id):
        captured_levels.append(request.settings.thinking_level)
        async for event in super().chat_stream(request, model_id):
            yield event


def configure_provider(client) -> None:
    """接入供应商。api_key 每次唯一：LlmRouter 是进程级单例、协议客户端按
    配置指纹缓存，复用其它测试文件的指纹会拿到用旧协议类构建的缓存客户端，
    本文件的捕获协议就不会生效。"""
    r = client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": f"sk-think-{uuid.uuid4().hex[:8]}",
            "default_model": "qwen3.7-max",
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    reset_agent_session_store()
    reset_agent_attachment_store()
    captured_levels.clear()
    monkeypatch.setitem(PROTOCOLS, "openai_chat", _CapturingProtocol)

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    reset_agent_session_store()
    reset_agent_attachment_store()


def send_and_finish(client, payload: dict) -> tuple[str, str]:
    started = client.post("/api/v1/sessions", json=payload)
    assert started.status_code == 202, started.text
    data = started.json()["data"]
    with client.stream("GET", f"/api/v1/sessions/{data['session_id']}/events") as r:
        r.read()
    for _ in range(50):
        item = client.get(f"/api/v1/sessions/{data['session_id']}").json()["data"]["session"]
        if not item["running"]:
            break
        time.sleep(0.1)
    return data["session_id"], data["message_id"]


def user_entry_levels(client, session_id: str) -> list[str | None]:
    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    return [
        e.get("thinking_level")
        for e in detail["entries"]
        if e["type"] == "message" and e["message"]["role"] == "user"
    ]


def test_explicit_then_inherit_then_default(client) -> None:
    configure_provider(client)
    # 第 1 条：显式 high
    sid, _ = send_and_finish(client, {"content": "第一问", "thinking_level": "high"})
    # 第 2 条：不传 → 沿用 high
    send_and_finish(client, {"content": "第二问", "session_id": sid})
    # 第 3 条：显式 default → 清回模型默认
    send_and_finish(client, {"content": "第三问", "session_id": sid, "thinking_level": "default"})
    # 第 4 条：不传 → 沿用「默认」
    send_and_finish(client, {"content": "第四问", "session_id": sid})

    assert user_entry_levels(client, sid) == ["high", "high", None, None]
    assert captured_levels == ["high", "high", None, None]


def test_retry_inherits_original_level(client) -> None:
    configure_provider(client)
    sid, mid = send_and_finish(client, {"content": "推理题", "thinking_level": "low"})
    captured_levels.clear()

    r = client.post(
        f"/api/v1/sessions/{sid}/retry", json={"message_id": mid, "content": "换个问法"}
    )
    assert r.status_code == 202, r.text
    with client.stream("GET", f"/api/v1/sessions/{sid}/events") as s:
        s.read()
    for _ in range(50):
        if not client.get(f"/api/v1/sessions/{sid}").json()["data"]["session"]["running"]:
            break
        time.sleep(0.1)

    assert user_entry_levels(client, sid) == ["low"]  # 重试后的新消息沿用 low
    assert captured_levels == ["low"]


def test_invalid_level_rejected(client) -> None:
    r = client.post("/api/v1/sessions", json={"content": "问", "thinking_level": "turbo"})
    assert r.status_code == 422
    assert "思维链档位" in r.text
