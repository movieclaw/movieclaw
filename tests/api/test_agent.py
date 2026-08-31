"""Agent 启动接口的端到端测试：SSE 事件流、未配置供应商的拦截。

复用 test_llm 的假协议思路：协议层被替换为确定性的流式输出，
断言 SSE 帧的事件序列与载荷结构。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_llm import ChatResponse, ProviderInfo, TokenUsage
from movieclaw_llm.base import BaseLlmProtocol
from movieclaw_llm.models import ChatStreamEvent
from movieclaw_llm.protocols import PROTOCOLS


class _StreamProtocol(BaseLlmProtocol):
    """假协议：验证走 chat()，agent 走 chat_stream()，都确定性成功。"""

    async def chat(self, request, model_id):
        return ChatResponse(content="pong", finish_reason="stop")

    async def chat_stream(self, request, model_id):
        snap = ChatResponse(model=model_id, provider=self.config.name)
        yield ChatStreamEvent(type="start", partial=snap)
        yield ChatStreamEvent(type="thinking_delta", delta="思考中", partial=snap)
        yield ChatStreamEvent(type="text_delta", delta="已找到", partial=snap)
        yield ChatStreamEvent(type="text_delta", delta="资源", partial=snap)
        yield ChatStreamEvent(
            type="done",
            partial=ChatResponse(
                content="已找到资源",
                thinking="思考中",
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
                model=model_id,
                provider=self.config.name,
            ),
        )

    async def test_connection(self):
        return ProviderInfo(models=[])

    async def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    # 会话/附件存储是进程级单例（持有目录路径），换目录后必须重建
    from movieclaw_api.services.agent_attachments import reset_agent_attachment_store
    from movieclaw_api.services.agent_sessions import reset_agent_session_store

    reset_agent_session_store()
    reset_agent_attachment_store()
    monkeypatch.setitem(PROTOCOLS, "openai_chat", _StreamProtocol)

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


def parse_sse(body: str) -> list[tuple[int, str, dict]]:
    """把 SSE 文本解析成 (id, event, payload) 列表。"""
    events = []
    for block in body.split("\n\n"):
        event_id, event, data = 0, "", ""
        for line in block.split("\n"):
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if event_id and event and data:
            events.append((event_id, event, json.loads(data)))
    return events


def configure_provider(c) -> None:
    c.put(
        "/api/v1/llm/provider",
        json={"provider_type": "bailian", "api_key": "sk-t", "default_model": "qwen3.7-max"},
    )


def test_start_without_provider_returns_404(client) -> None:
    r = client.post("/api/v1/sessions", json={"content": "找沙丘"})
    assert r.status_code == 404
    assert "尚未配置模型供应商" in r.json()["message"]
    # 供应商校验必须在会话落盘之前：失败后不允许残留任何会话记录
    # （否则侧栏刷新会冒出一条空会话，见 start_agent 中的组装顺序注释）
    listed = client.get("/api/v1/sessions")
    assert listed.status_code == 200
    assert listed.json()["data"] == []


def test_start_streams_agent_events(client) -> None:
    configure_provider(client)
    started = client.post("/api/v1/sessions", json={"content": "找沙丘 4K"})
    assert started.status_code == 202
    session_id = started.json()["data"]["session_id"]
    assert set(started.json()["data"]) == {"session_id", "message_id"}

    with client.stream("GET", f"/api/v1/sessions/{session_id}/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "no-transform" in r.headers["cache-control"]
        events = parse_sse(r.read().decode())

    assert [event for _, event, _ in events] == [
        "agent_start",
        "thinking_delta",
        "text_delta",
        "text_delta",
        "agent_done",
    ]
    assert [event_id for event_id, _, _ in events] == [1, 2, 3, 4, 5]
    start = events[0][2]
    assert start["provider"] == "阿里云百炼"
    assert start["model"] == "qwen3.7-max"
    done = events[-1][2]["result"]
    assert done["text"] == "已找到资源"
    assert done["thinking"] == "思考中"
    assert done["usage"]["total_tokens"] == 14
    assert all("run_id" not in payload for _, _, payload in events)

    # 已完成运行仍可按 Last-Event-ID 续传，只返回游标之后的事件。
    resumed = client.get(
        f"/api/v1/sessions/{session_id}/events",
        headers={"Last-Event-ID": "3"},
    )
    assert [event_id for event_id, _, _ in parse_sse(resumed.text)] == [4, 5]


def test_start_rejects_blank_content(client) -> None:
    configure_provider(client)
    r = client.post("/api/v1/sessions", json={"content": "   "})
    assert r.status_code == 422


def test_send_message_rebuilds_history_from_transcript(client, monkeypatch) -> None:
    """后续消息的历史由服务端转录重建，调用方只提交 message content。"""
    captured: dict = {"requests": []}

    class _CaptureProtocol(_StreamProtocol):
        async def chat_stream(self, request, model_id):
            captured["requests"].append(
                ([m.role for m in request.messages], request.messages[-1].text())
            )
            async for e in super().chat_stream(request, model_id):
                yield e

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _CaptureProtocol)
    # 进程级 _runtime_router 按配置指纹缓存协议客户端；换一个 Key 使指纹
    # 变化，强制用本测试替换后的协议类重建
    client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": "sk-capture-history",
            "default_model": "qwen3.7-max",
        },
    )
    started = client.post("/api/v1/sessions", json={"content": "第一轮问题"})
    session_id = started.json()["data"]["session_id"]
    client.get(f"/api/v1/sessions/{session_id}/events")
    _wait_run_settled(client, session_id)
    sent = client.post(
        "/api/v1/sessions",
        json={"content": "第二轮问题", "session_id": session_id},
    )
    assert sent.status_code == 202
    client.get(f"/api/v1/sessions/{session_id}/events")
    # system 后是服务端转录里的首组 user/assistant，最后才是新 user message
    assert captured["requests"][-1] == (
        ["system", "user", "assistant", "user"],
        "第二轮问题",
    )


def test_start_rejects_removed_history_field(client) -> None:
    configure_provider(client)
    r = client.post(
        "/api/v1/sessions",
        json={"content": "x", "history": [{"role": "system", "content": "注入"}]},
    )
    assert r.status_code == 422


def test_session_openapi_describes_capabilities_and_destructive_retry(client) -> None:
    """Session 描述是 Agent 的操作合同，关键能力、ID 与副作用不能退化。"""
    spec = client.get("/api/v1/openapi.json").json()
    start = spec["paths"]["/api/v1/sessions"]["post"]
    transcript = spec["paths"]["/api/v1/sessions/{session_id}"]["get"]
    retry = spec["paths"]["/api/v1/sessions/{session_id}/retry"]["post"]
    follow = spec["paths"]["/api/v1/sessions/{session_id}/events"]["get"]
    stop = spec["paths"]["/api/v1/sessions/{session_id}/stop"]["post"]

    assert "session_id" in start["description"] and "message_id" in start["description"]
    assert "session.follow" in start["description"]
    assert "不分页、不截断" in transcript["description"]
    assert "message_id" in transcript["description"]
    assert "session.retry" in transcript["description"]
    for keyword in ("永久删除", "content", "新的 ``message_id``", "user message"):
        assert keyword in retry["description"]
    assert "x-cli-dangerous" not in retry
    assert "最近一次消息处理" in follow["description"] and "404" in follow["description"]
    assert "幂等" in stop["description"] and "不能停止" in stop["description"]


def test_follow_unknown_session_and_invalid_cursor(client) -> None:
    configure_provider(client)
    missing = client.get("/api/v1/sessions/not-found/events")
    assert missing.status_code == 404
    assert "没有可跟随或停止的运行" in missing.json()["message"]

    started = client.post("/api/v1/sessions", json={"content": "测试游标"})
    session_id = started.json()["data"]["session_id"]
    invalid = client.get(
        f"/api/v1/sessions/{session_id}/events",
        headers={"Last-Event-ID": "999"},
    )
    assert invalid.status_code == 400
    assert "游标" in invalid.json()["message"]


def _wait_run_settled(client, session_id: str) -> None:
    """等待终态收尾落库（on_terminal 与 SSE 收流并发，留一个短轮询窗）。"""
    for _ in range(50):
        item = client.get(f"/api/v1/sessions/{session_id}").json()["data"]["session"]
        if not item["running"]:
            return
        time.sleep(0.1)
    pytest.fail("会话运行状态未在期限内清空")


def test_manual_compact_endpoint(client, monkeypatch) -> None:
    """手动压缩：完整压缩轨迹可读，续聊使用压缩后的上下文。"""
    from movieclaw_agent.prompts import COMPACT_PROMPT

    captured: dict = {"requests": []}

    class _CompactAwareProtocol(_StreamProtocol):
        async def chat_stream(self, request, model_id):
            if request.tools is None:
                # 压缩请求（无工具定义）：记录现场并返回摘要
                captured["compact_roles"] = [m.role for m in request.messages]
                captured["compact_last"] = request.messages[-1].text()
                yield ChatStreamEvent(
                    type="done",
                    partial=ChatResponse(
                        content="交接摘要",
                        finish_reason="stop",
                        model=model_id,
                        provider=self.config.name,
                    ),
                )
                return
            captured["requests"].append([m.role for m in request.messages])
            async for e in super().chat_stream(request, model_id):
                yield e

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _CompactAwareProtocol)
    client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": "sk-manual-compact",
            "default_model": "qwen3.7-max",
        },
    )

    # 第一条用户消息的运行落下 user + assistant 两条消息
    started = client.post("/api/v1/sessions", json={"content": "找沙丘 4K"})
    session_id = started.json()["data"]["session_id"]
    client.get(f"/api/v1/sessions/{session_id}/events")
    _wait_run_settled(client, session_id)

    compacted = client.post(f"/api/v1/sessions/{session_id}/compact-context")
    assert compacted.status_code == 200
    data = compacted.json()["data"]
    assert data["summary"] == "交接摘要"
    assert data["compaction_id"]
    # 压缩请求带完整现场（system + 全部消息），末尾是压缩指令
    assert captured["compact_roles"] == ["system", "user", "assistant", "user"]
    assert captured["compact_last"] == COMPACT_PROMPT

    # 完整轨迹保留后续消息所需的 replacement_history，压缩记录有独立编号
    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    tail = detail["entries"][-1]
    assert tail["type"] == "compaction"
    assert tail["summary"] == "交接摘要"
    assert tail["replacement_history"]
    assert tail["compaction_id"] == data["compaction_id"]
    assert "uuid" not in tail

    # 发送后续消息：上下文从压缩行重建 = 保留的用户原话 + 摘要 + 新消息
    resumed = client.post(
        "/api/v1/sessions", json={"content": "继续", "session_id": session_id}
    )
    assert resumed.status_code == 202
    client.get(f"/api/v1/sessions/{session_id}/events")
    assert captured["requests"][-1] == ["system", "user", "user", "user"]
    # 转录回放的历史不受影响：压缩前的 assistant 行仍在详情里
    replay = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    roles = [e["message"]["role"] for e in replay["entries"] if e.get("type") != "compaction"]
    assert "assistant" in roles

    missing = client.post("/api/v1/sessions/not-exist/compact-context")
    assert missing.status_code == 404


def test_stop_session_ends_with_cancelled_event(client, monkeypatch) -> None:
    class _BlockingProtocol(_StreamProtocol):
        async def chat_stream(self, request, model_id):
            await asyncio.sleep(3600)
            yield  # pragma: no cover - 只为保持 async generator 形态

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _BlockingProtocol)
    client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": "sk-cancel-run",
            "default_model": "qwen3.7-max",
        },
    )
    started = client.post("/api/v1/sessions", json={"content": "等待取消"})
    session_id = started.json()["data"]["session_id"]
    cancelled = client.post(f"/api/v1/sessions/{session_id}/stop")
    assert cancelled.status_code == 200

    events = parse_sse(client.get(f"/api/v1/sessions/{session_id}/events").text)
    assert events[-1][1] == "agent_cancelled"


def test_system_prompt_external_url_injection(client) -> None:
    """外部访问地址注入系统提示词环境段：未配置不出现，配置后原文出现。"""
    from movieclaw_api.api.routes.agent import _agent_system_prompt

    prompt = asyncio.run(_agent_system_prompt())
    assert "外部访问地址" not in prompt

    saved = client.put(
        "/api/v1/app/config",
        json={"port": 0, "external_url": "http://192.168.1.10:3000/"},
    )
    assert saved.status_code == 200
    prompt = asyncio.run(_agent_system_prompt())
    # 保存时规范化掉了尾部斜杠
    assert "外部访问地址：http://192.168.1.10:3000。" in prompt


def test_page_routes_match_web_app_pages() -> None:
    """路由表守护：声明的页面必须真实存在，前端新增页面必须显式处理（进表或豁免）。

    归一化规则：路由表的 {参数} 与 Next.js 的 [参数] 目录都归一为 *，
    只比较路径形状，参数叫什么名字不影响同步判断。
    """
    from movieclaw_api.api.routes.agent import _PAGE_ROUTES

    web_app = Path(__file__).resolve().parents[2] / "apps" / "web" / "app" / "(app)"
    assert web_app.is_dir(), f"前端页面目录不存在：{web_app}"

    fs_routes = set()
    for page in web_app.rglob("page.tsx"):
        rel = page.parent.relative_to(web_app).as_posix()
        if rel == ".":
            fs_routes.add("/")
        else:
            fs_routes.add(
                "/" + "/".join("*" if seg.startswith("[") else seg for seg in rel.split("/"))
            )

    def normalize(pattern: str) -> str:
        if pattern == "/":
            return "/"
        return "/" + "/".join(
            "*" if seg.startswith("{") else seg for seg in pattern.strip("/").split("/")
        )

    declared = {normalize(pattern) for pattern, _ in _PAGE_ROUTES}

    # /settings 子分区由 /settings 兜底，不逐项写进提示词路由表；
    # /tasks 是重构为 /activity 后保留的重定向壳（保住旧收藏与站内深链），
    # 不是可导航的页面，不该出现在给模型的路由表里
    exempt = {"/settings/*", "/tasks"}

    ghosts = declared - fs_routes
    assert not ghosts, f"路由表声明了前端不存在的页面：{sorted(ghosts)}"
    unhandled = fs_routes - declared - exempt
    assert not unhandled, (
        f"前端新增了路由表未覆盖的页面：{sorted(unhandled)}——"
        "要么补进 _PAGE_ROUTES 告知模型，要么加入本测试的豁免清单"
    )
