"""技能清单注入 API 链路（docs/design/agent-skills.md §3.2）。

覆盖：有技能时 system prompt 含 <available_skills> 清单、空目录不含；
技能改动后无需重启、下一轮运行即生效（每次运行现扫）。
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

#: 每次模型调用的 system 消息内容（按调用顺序）
captured_system_prompts: list[str] = []


class _CapturingProtocol(_StreamProtocol):
    async def chat_stream(self, request, model_id):
        for message in request.messages:
            if message.role == "system":
                captured_system_prompts.append(message.text())
        async for event in super().chat_stream(request, model_id):
            yield event


def configure_provider(client) -> None:
    """接入供应商。api_key 每次唯一：LlmRouter 是进程级单例、协议客户端按
    配置指纹缓存，复用其它测试文件的指纹会拿到用旧协议类构建的缓存客户端。"""
    r = client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": f"sk-skill-{uuid.uuid4().hex[:8]}",
            "default_model": "qwen3.7-max",
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def skills_dir(tmp_path):
    d = tmp_path / "agent-skills"
    d.mkdir()
    return d


@pytest.fixture
def client(tmp_path, skills_dir, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path / "agent-workspace"))
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(skills_dir))
    get_settings.cache_clear()
    reset_agent_session_store()
    reset_agent_attachment_store()
    captured_system_prompts.clear()
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


def write_skill(root, dir_name: str, description: str) -> None:
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\ndescription: {description}\n---\n\n# 指令\n")


def send_and_finish(client, payload: dict) -> str:
    started = client.post("/api/v1/sessions", json=payload)
    assert started.status_code == 202, started.text
    session_id = started.json()["data"]["session_id"]
    with client.stream("GET", f"/api/v1/sessions/{session_id}/events") as r:
        r.read()
    for _ in range(50):
        item = client.get(f"/api/v1/sessions/{session_id}").json()["data"]["session"]
        if not item["running"]:
            break
        time.sleep(0.1)
    return session_id


def test_catalog_injected_and_updates_take_effect(client, skills_dir) -> None:
    configure_provider(client)

    # 空目录：system prompt 不含清单段
    send_and_finish(client, {"content": "第一问"})
    assert captured_system_prompts, "未捕获到 system 消息"
    assert "<available_skills>" not in captured_system_prompts[0]

    # 放入技能：下一轮运行即生效，无需重启
    write_skill(skills_dir, "subtitle-workflow", "字幕整理流程")
    captured_system_prompts.clear()
    send_and_finish(client, {"content": "第二问"})
    prompt = captured_system_prompts[0]
    assert "<available_skills>" in prompt
    assert "<name>subtitle-workflow</name>" in prompt
    assert "<description>字幕整理流程</description>" in prompt
    assert str(skills_dir / "subtitle-workflow" / "SKILL.md") in prompt

    # 改描述：再下一轮同样生效（每次运行现扫）
    write_skill(skills_dir, "subtitle-workflow", "改过的描述")
    captured_system_prompts.clear()
    send_and_finish(client, {"content": "第三问"})
    assert "<description>改过的描述</description>" in captured_system_prompts[0]
