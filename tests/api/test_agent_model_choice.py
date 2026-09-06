"""对话框模型选择的 API 链路：三态传参、信封持久化、续聊沿用、跨实例路由。

- 显式引用（裸 id 或「实例名/模型id」）/ "default" 清回默认 / 未传沿用最近一条
  user 消息的引用；
- 生效值存转录信封的 user 行（零迁移），transcript 透出供前端初始化选择器；
- 同 id 在两个实例里都有时，「实例名/模型id」精确路由到指定实例；
- 不选模型时走 AI 设定的智能体默认模型（未设定时兜底到第一个实例）。
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
from movieclaw_api.settings import reset_setting_store
from movieclaw_llm.protocols import PROTOCOLS

#: 每次模型调用实际路由到的 (实例名, 模型 id)
captured_routes: list[tuple[str, str]] = []


class _CapturingProtocol(_StreamProtocol):
    async def chat_stream(self, request, model_id):
        captured_routes.append((self.config.name, model_id))
        async for event in super().chat_stream(request, model_id):
            yield event


def configure_two_providers(client) -> None:
    """百炼（默认）+ 一个借用了 qwen3.7-max 的兼容端点，制造同 id 冲突。
    api_key 每次唯一：LlmRouter 是进程级单例、协议客户端按配置指纹缓存。"""
    salt = uuid.uuid4().hex[:8]
    r = client.post(
        "/api/v1/llm/providers",
        json={
            "name": "百炼",
            "provider_type": "bailian",
            "api_key": f"sk-model-{salt}",
            "default_model": "qwen3.7-max",
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/v1/llm/providers",
        json={
            "name": "中转",
            "provider_type": "openai_compat",
            "base_url": "http://relay.local/v1",
            "api_key": f"sk-relay-{salt}",
            "default_model": "qwen3.7-max",
            "extra_models": [{"id": "qwen3.7-max", "context_window": 131072}],
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    # 配置存储是进程级单例且带缓存，用例间必须重置，否则上一个用例的 AI 设定会串进来
    reset_setting_store()
    reset_agent_session_store()
    reset_agent_attachment_store()
    captured_routes.clear()
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


def send_and_finish(client, payload: dict) -> str:
    started = client.post("/api/v1/sessions", json=payload)
    assert started.status_code == 202, started.text
    session_id = started.json()["data"]["session_id"]
    with client.stream("GET", f"/api/v1/sessions/{session_id}/events") as r:
        r.read()
    for _ in range(50):
        item = client.get(f"/api/v1/sessions/{session_id}").json()["data"]["session"]
        if not item["running"]:
            return session_id
        time.sleep(0.1)
    raise AssertionError("会话未在预期时间内结束")


def user_models(client, session_id: str) -> list[str | None]:
    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    return [
        e.get("model")
        for e in detail["entries"]
        if e["type"] == "message" and e["message"]["role"] == "user"
    ]


def test_explicit_ref_routes_to_named_instance_and_is_inherited(client) -> None:
    configure_two_providers(client)
    # 「实例名/模型id」精确路由到中转（裸 id 会按默认优先落到百炼）
    session_id = send_and_finish(client, {"content": "你好", "model": "中转/qwen3.7-max"})
    assert captured_routes[-1] == ("中转", "qwen3.7-max")
    # 续聊不传 model → 沿用会话上一条的引用
    send_and_finish(client, {"content": "继续", "session_id": session_id})
    assert captured_routes[-1] == ("中转", "qwen3.7-max")
    # "default" 显式清回默认实例的默认模型
    send_and_finish(client, {"content": "换回默认", "session_id": session_id, "model": "default"})
    assert captured_routes[-1] == ("百炼", "qwen3.7-max")
    # 信封记录：显式引用 / 沿用 / 默认（None）
    assert user_models(client, session_id) == ["中转/qwen3.7-max", "中转/qwen3.7-max", None]


def test_bare_id_prefers_default_instance(client) -> None:
    configure_two_providers(client)
    send_and_finish(client, {"content": "你好", "model": "qwen3.7-max"})
    assert captured_routes[-1] == ("百炼", "qwen3.7-max")


def test_new_session_without_model_uses_default(client) -> None:
    configure_two_providers(client)
    # 首次接入时自动设定为第一个实例目录里的第一个模型（这里显式指定了 qwen3.7-max）
    session_id = send_and_finish(client, {"content": "你好"})
    assert captured_routes[-1] == ("百炼", "qwen3.7-max")
    assert user_models(client, session_id) == [None]
    # 设定智能体默认模型后，不选模型的会话（含续聊）都跟着走
    r = client.put("/api/v1/llm/defaults", json={"agent_model": "中转/qwen3.7-max"})
    assert r.status_code == 200, r.text
    send_and_finish(client, {"content": "继续", "session_id": session_id})
    assert captured_routes[-1] == ("中转", "qwen3.7-max")
    assert user_models(client, session_id) == [None, None]
