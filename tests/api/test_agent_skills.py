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
#: 每次模型调用的 user 消息文本（验证显式调用展开后的实际请求体）
captured_user_messages: list[str] = []


class _CapturingProtocol(_StreamProtocol):
    async def chat_stream(self, request, model_id):
        for message in request.messages:
            if message.role == "system":
                captured_system_prompts.append(message.text())
            elif message.role == "user":
                captured_user_messages.append(message.text())
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
    captured_user_messages.clear()
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

    # 用户目录为空：清单只含内置技能（skill-creator 随源码打包），
    # 且创建引导指向用户技能目录
    send_and_finish(client, {"content": "第一问"})
    assert captured_system_prompts, "未捕获到 system 消息"
    first = captured_system_prompts[0]
    assert "<available_skills>" in first
    assert "<name>skill-creator</name>" in first
    assert f"用户技能目录 {skills_dir}" in first

    # 放入用户技能：下一轮运行即生效，无需重启
    write_skill(skills_dir, "subtitle-workflow", "字幕整理流程")
    captured_system_prompts.clear()
    send_and_finish(client, {"content": "第二问"})
    prompt = captured_system_prompts[0]
    assert "<name>subtitle-workflow</name>" in prompt
    assert "<description>字幕整理流程</description>" in prompt
    assert str(skills_dir / "subtitle-workflow" / "SKILL.md") in prompt

    # 改描述：再下一轮同样生效（每次运行现扫）
    write_skill(skills_dir, "subtitle-workflow", "改过的描述")
    captured_system_prompts.clear()
    send_and_finish(client, {"content": "第三问"})
    assert "<description>改过的描述</description>" in captured_system_prompts[0]


def test_user_skill_overrides_builtin(client, skills_dir) -> None:
    """用户目录放同名 skill-creator：清单里 location 指向用户目录。"""
    configure_provider(client)
    write_skill(skills_dir, "skill-creator", "用户定制版技能创建器")
    send_and_finish(client, {"content": "问一句"})
    prompt = captured_system_prompts[0]
    assert "<description>用户定制版技能创建器</description>" in prompt
    assert str(skills_dir / "skill-creator" / "SKILL.md") in prompt
    assert "builtin-skills" not in prompt


def test_skills_endpoint_lists_merged_layers(client, skills_dir) -> None:
    """GET /skills：内置 + 用户合并清单（composer 加号菜单的数据源）。"""
    write_skill(skills_dir, "douban-picks", "按类型推荐豆瓣高分电影")
    r = client.get("/api/v1/skills")
    assert r.status_code == 200, r.text
    by_name = {s["name"]: s for s in r.json()["data"]}
    assert by_name["douban-picks"]["scope"] == "user"
    assert by_name["douban-picks"]["description"] == "按类型推荐豆瓣高分电影"
    assert by_name["skill-creator"]["scope"] == "builtin"


def test_invocation_expands_into_transcript_and_llm_request(client, skills_dir) -> None:
    """/skill:名字 占位符：服务端展开、入转录、发给模型的是展开文，预览打 [技能] 占位。"""
    configure_provider(client)
    skill_dir = skills_dir / "douban-picks"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: douban-picks\ndescription: 推荐高分电影\n---\n\n"
        "# 推荐流程\n先拉榜单再核对库存\n"
    )

    sid = send_and_finish(client, {"content": "/skill:douban-picks 来点科幻片"})

    # 转录里的 user 消息是展开后的全文（内容冻结在调用时刻）
    detail = client.get(f"/api/v1/sessions/{sid}").json()["data"]
    user_entries = [
        e for e in detail["entries"] if e["type"] == "message" and e["message"]["role"] == "user"
    ]
    stored = user_entries[0]["message"]["content"]
    assert stored.startswith('<skill name="douban-picks" location="')
    assert "先拉榜单再核对库存" in stored
    assert stored.endswith("来点科幻片")
    assert "/skill:douban-picks" not in stored

    # 发给模型的 user 消息与转录一致（capture 协议记录的请求消息）
    assert captured_user_messages, "未捕获到 user 消息"
    assert captured_user_messages[-1] == stored

    # 侧栏预览：技能块折叠为 [技能] 占位
    items = client.get("/api/v1/sessions").json()["data"]
    item = next(i for i in items if i["id"] == sid)
    assert item["title"].startswith("[技能] 来点科幻片")


def test_unknown_token_passthrough_to_transcript(client, skills_dir) -> None:
    configure_provider(client)
    sid = send_and_finish(client, {"content": "/skill:no-such-skill 你好"})
    detail = client.get(f"/api/v1/sessions/{sid}").json()["data"]
    user = next(
        e for e in detail["entries"] if e["type"] == "message" and e["message"]["role"] == "user"
    )
    assert user["message"]["content"] == "/skill:no-such-skill 你好"
