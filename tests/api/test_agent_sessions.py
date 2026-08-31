"""Agent 会话持久化：JSONL 存储、记录器收尾、索引重建与会话 API。

覆盖设计定案的四条关键约束：
1. append-only 线性链（uuid/parent_uuid 正确串联，坏行不毁会话）；
2. 只落定稿消息（assistant 带 model/usage 元数据，原样可回喂）；
3. 中断收尾补配对（tool_call 永远有回执）；
4. 索引可由文件整体重建（文件是事实源）。
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from tests.api.test_agent import _StreamProtocol, configure_provider

from movieclaw_agent import SUMMARY_PREFIX, CompactionResult
from movieclaw_agent.events import AgentEvent
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services.agent_sessions import (
    SESSION_FORMAT_VERSION,
    AgentSessionStore,
    SessionHandoffEntry,
    reset_agent_session_store,
)
from movieclaw_llm import ChatMessage, ChatResponse, TokenUsage, ToolCall
from movieclaw_llm.protocols import PROTOCOLS

# ---------------------------------------------------------------------------
# 存储层单元测试（纯文件，不依赖 DB / 应用）
# ---------------------------------------------------------------------------


def _assistant_with_tools() -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(id="call_1", name="read", arguments={"path": "a.txt"}),
            ToolCall(id="call_2", name="bash", arguments={"command": "ls"}),
        ],
    )


def test_store_roundtrip_linear_chain(tmp_path) -> None:
    store = AgentSessionStore(tmp_path)
    header = store.create()
    sid = header.session_id

    e1 = store.append(sid, ChatMessage(role="user", content="找沙丘 4K"))
    e2 = store.append(
        sid,
        ChatMessage(role="assistant", content="好的，找到了"),
        model="kimi-k2",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason="stop",
    )

    header_read, entries = store.read(sid)
    assert header_read.session_id == sid
    assert header_read.version == SESSION_FORMAT_VERSION
    assert [e.uuid for e in entries] == [e1.uuid, e2.uuid]
    # 线性链：首条 parent 为空，之后逐条回指
    assert entries[0].parent_uuid is None
    assert entries[1].parent_uuid == e1.uuid
    # assistant 信封元数据完整保留
    assert entries[1].model == "kimi-k2"
    assert entries[1].usage.total_tokens == 15
    assert entries[1].finish_reason == "stop"
    # 重建的 LLM 上下文就是原样消息
    history = store.build_history(sid)
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[1].text() == "好的，找到了"


def test_store_skips_corrupt_tail_and_chains_after_reload(tmp_path) -> None:
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    good = store.append(sid, ChatMessage(role="user", content="第一条"))
    # 模拟进程崩溃留下的半行
    with store.path(sid).open("a", encoding="utf-8") as f:
        f.write('{"type":"message","uuid":"trunc')

    # 新进程（新 store 实例，缓存为空）读取：坏行被跳过
    fresh = AgentSessionStore(tmp_path)
    _, entries = fresh.read(sid)
    assert [e.uuid for e in entries] == [good.uuid]
    # 继续追加：链尾接在最后一条合法 entry 上，而不是坏行
    e2 = fresh.append(sid, ChatMessage(role="user", content="第二条"))
    assert e2.parent_uuid == good.uuid


def test_seal_pending_tool_calls_completes_pairing(tmp_path) -> None:
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="执行任务"))
    store.append(sid, _assistant_with_tools())
    # 只有 call_1 有回执，call_2 因中断没有
    store.append(
        sid,
        ChatMessage(role="tool", content="文件内容", tool_call_id="call_1", name="read"),
    )

    assert store.seal_pending_tool_calls(sid) == 1
    _, entries = store.read(sid)
    sealed = entries[-1]
    assert sealed.message.role == "tool"
    assert sealed.message.tool_call_id == "call_2"
    assert "中断" in sealed.message.text()
    # 幂等：全部配对后再收尾不产生新行
    assert store.seal_pending_tool_calls(sid) == 0


def test_summarize_and_scan_all(tmp_path) -> None:
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="标题" * 100))
    store.append(sid, ChatMessage(role="assistant", content="答复"))
    store.append(sid, ChatMessage(role="user", content="第二轮提问"))

    summary = store.summarize(sid)
    assert summary.entry_count == 3
    assert len(summary.title) == 80  # 截断
    assert summary.last_prompt == "第二轮提问"
    assert summary.leaf_uuid == store.read(sid)[1][-1].uuid

    # 损坏文件（空文件）不拖垮全目录扫描
    (tmp_path / "broken.jsonl").write_text("", encoding="utf-8")
    summaries = store.scan_all()
    assert [s.session_id for s in summaries] == [sid]


def test_discard_from_user_message_drops_message_and_everything_after(tmp_path) -> None:
    """丢弃第二条用户消息后只剩首组问答，后续追加接回新链尾。"""
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    first = store.append(sid, ChatMessage(role="user", content="第一轮"))
    answer = store.append(sid, ChatMessage(role="assistant", content="第一轮答复"))
    second = store.append(sid, ChatMessage(role="user", content="第二轮"))
    store.append(sid, ChatMessage(role="assistant", content="第二轮答复"))

    assert store.discard_from_user_message(sid, second.uuid) == 2

    _, entries = store.read(sid)
    assert [e.uuid for e in entries] == [first.uuid, answer.uuid]
    # 上下文里不再有被丢弃的往返
    assert [m.text() for m in store.build_history(sid)] == ["第一轮", "第一轮答复"]
    # 链尾回到目标消息之前：新消息的 parent 指向保留下来的最后一条
    rewritten = store.append(sid, ChatMessage(role="user", content="第二轮（改写后）"))
    assert rewritten.parent_uuid == answer.uuid
    summary = store.summarize(sid)
    assert summary.entry_count == 3
    assert summary.last_prompt == "第二轮（改写后）"


def test_discard_from_user_message_rejects_non_user_anchor(tmp_path) -> None:
    """只能从 user message 丢弃：从 assistant 中间切会留下孤立工具轨迹。"""
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="执行任务"))
    assistant = store.append(sid, _assistant_with_tools())

    with pytest.raises(BadRequestException):
        store.discard_from_user_message(sid, assistant.uuid)
    with pytest.raises(NotFoundException):
        store.discard_from_user_message(sid, "不存在的uuid")
    # 失败不改动文件
    assert len(store.read(sid)[1]) == 2


def test_discard_from_first_message_empties_session(tmp_path) -> None:
    """从首条消息开始丢弃会清空会话：只剩头行，链尾回到 None。"""
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    first = store.append(sid, ChatMessage(role="user", content="唯一一轮"))
    store.append(sid, ChatMessage(role="assistant", content="答复"))

    assert store.discard_from_user_message(sid, first.uuid) == 2
    header, entries = store.read(sid)
    assert header.session_id == sid
    assert entries == []
    assert store.append(sid, ChatMessage(role="user", content="重新开始")).parent_uuid is None


def _compaction_result() -> CompactionResult:
    return CompactionResult(
        summary="交接摘要",
        replacement_history=[
            ChatMessage(role="user", content="执行任务"),
            ChatMessage(role="user", content=f"{SUMMARY_PREFIX}\n交接摘要"),
        ],
        tokens_before=100,
        tokens_after=10,
    )


def test_compaction_entry_chains_and_rebuilds_history(tmp_path) -> None:
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="执行任务"))
    store.append(sid, _assistant_with_tools())
    store.append(
        sid, ChatMessage(role="tool", content="文件内容", tool_call_id="call_1", name="read")
    )
    store.append(
        sid, ChatMessage(role="tool", content="目录清单", tool_call_id="call_2", name="bash")
    )

    result = _compaction_result()
    entry = store.append_compaction(sid, result)
    store.append(sid, ChatMessage(role="user", content="继续下一步"))

    # parent 链线性穿过压缩行
    _, entries = store.read(sid)
    assert [e.parent_uuid for e in entries] == [None, *[e.uuid for e in entries[:-1]]]
    assert entries[-2].uuid == entry.uuid
    assert entries[-2].summary == "交接摘要"

    # resume 上下文 = 替换历史 + 压缩行之后的增量消息；压缩前的原始消息不再进入
    history = store.build_history(sid)
    assert [m.text() for m in history] == [
        "执行任务",
        f"{SUMMARY_PREFIX}\n交接摘要",
        "继续下一步",
    ]
    assert all(m.role == "user" for m in history)


def test_seal_ignores_calls_before_compaction(tmp_path) -> None:
    """压缩行之前的未配对调用是死上下文，中断收尾不再给它们补回执。"""
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="执行任务"))
    store.append(sid, _assistant_with_tools())  # call_1 / call_2 均无回执
    store.append_compaction(sid, _compaction_result())

    assert store.seal_pending_tool_calls(sid) == 0

    # 压缩行之后的新调用仍正常补配对
    store.append(sid, _assistant_with_tools())
    assert store.seal_pending_tool_calls(sid) == 2


def test_compaction_entry_counts_in_summary_but_not_text(tmp_path) -> None:
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="执行任务"))
    entry = store.append_compaction(sid, _compaction_result())

    summary = store.summarize(sid)
    assert summary.entry_count == 2
    assert summary.leaf_uuid == entry.uuid
    # 标题/预览只看消息行：压缩摘要不会污染它们
    assert summary.title == "执行任务"
    assert summary.last_prompt == "执行任务"


def test_unknown_entry_type_skipped_as_bad_line(tmp_path) -> None:
    """未来格式（未知 type）按坏行跳过：老读者拿到未压缩全量，会话仍可打开。"""
    store = AgentSessionStore(tmp_path)
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="执行任务"))
    with store.path(sid).open("a", encoding="utf-8") as f:
        f.write('{"type": "future_thing", "uuid": "x"}\n')

    _, entries = store.read(sid)
    assert len(entries) == 1
    assert store.build_history(sid)[0].text() == "执行任务"


def test_fork_creates_independent_handoff_snapshot(tmp_path) -> None:
    """新会话持有源上下文快照，源文件之后删除也不影响续聊。"""
    store = AgentSessionStore(tmp_path)
    source_id = store.create().session_id
    store.append(source_id, ChatMessage(role="user", content="整理订阅"))
    store.append(source_id, ChatMessage(role="assistant", content="已完成第一步"))
    source_before = store.path(source_id).read_bytes()

    header, handoff = store.fork(source_id, source_title="订阅整理")

    assert header.session_id != source_id
    assert handoff.source_session_id == source_id
    assert handoff.source_leaf_uuid == store.read(source_id)[1][-1].uuid
    assert store.path(source_id).read_bytes() == source_before
    _, target_entries = store.read(header.session_id)
    assert target_entries == [handoff]
    assert isinstance(target_entries[0], SessionHandoffEntry)
    assert [message.text() for message in store.build_history(header.session_id)] == [
        "整理订阅",
        "已完成第一步",
    ]
    assert store.summarize(header.session_id).title == "续：订阅整理"

    store.delete(source_id)
    assert [message.text() for message in store.build_history(header.session_id)] == [
        "整理订阅",
        "已完成第一步",
    ]


def test_fork_uses_effective_compacted_history_and_repairs_tool_pairs(tmp_path) -> None:
    """只继承有效上下文，并在目标快照内修复硬崩留下的未配对调用。"""
    store = AgentSessionStore(tmp_path)
    source_id = store.create().session_id
    store.append(source_id, ChatMessage(role="user", content="不会进入快照的旧消息"))
    store.append_compaction(source_id, _compaction_result())
    store.append(source_id, _assistant_with_tools())
    store.append(
        source_id,
        ChatMessage(role="tool", content="已读文件", tool_call_id="call_1", name="read"),
    )
    source_count = len(store.read(source_id)[1])

    header, _ = store.fork(source_id, source_title="异常会话")
    history = store.build_history(header.session_id)

    assert [message.text() for message in history[:2]] == [
        "执行任务",
        f"{SUMMARY_PREFIX}\n交接摘要",
    ]
    assert "不会进入快照的旧消息" not in [message.text() for message in history]
    assert history[-2].tool_call_id == "call_1"
    assert history[-1].role == "tool"
    assert history[-1].tool_call_id == "call_2"
    assert "结果未知" in history[-1].text()
    # 修复只写进新会话的 handoff 快照，源会话保持原状。
    assert len(store.read(source_id)[1]) == source_count


# ---------------------------------------------------------------------------
# 记录器 + 索引（异步，真实 SQLite）
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from movieclaw_db.engine import dispose_db, get_database, init_db
    from movieclaw_db.migrations import run_migrations

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    reset_agent_session_store()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()
    reset_agent_session_store()


async def test_recorder_lifecycle_and_terminal_sealing(db) -> None:
    """完整运行生命周期：落用户输入 → 定稿回调 → 取消收尾 → 状态清空。"""
    from movieclaw_api.services.agent_session_recorder import AgentSessionRecorder
    from movieclaw_api.services.agent_sessions import get_agent_session_store
    from movieclaw_db.repositories.agent_session_repo import (
        AgentSessionRepository,
        is_running,
    )

    store = get_agent_session_store()
    sid = store.create().session_id
    async with db.session() as session:
        await AgentSessionRepository(session).create(sid, title=None)

    recorder = AgentSessionRecorder(store, sid, entry_count=0)
    await recorder.begin("run123")
    await recorder.record_user_message("帮我找资源")
    await recorder.on_message(
        _assistant_with_tools(),
        ChatResponse(model="kimi-k2", finish_reason="tool_calls"),
    )
    # 模拟只执行完第一个工具就被取消
    await recorder.on_message(
        ChatMessage(role="tool", content="ok", tool_call_id="call_1", name="read"), None
    )

    async with db.session() as session:
        row = await AgentSessionRepository(session).get(sid)
        assert row.active_run_id == "run123"
        assert is_running(row)
        assert row.title == "帮我找资源"
        assert row.entry_count == 3

    await recorder.on_terminal(AgentEvent(type="agent_cancelled", run_id="run123"))

    _, entries = store.read(sid)
    # user + assistant + tool(call_1) + 补配对的 tool(call_2)
    assert [e.message.role for e in entries] == ["user", "assistant", "tool", "tool"]
    assert entries[-1].message.tool_call_id == "call_2"
    async with db.session() as session:
        row = await AgentSessionRepository(session).get(sid)
        assert row.active_run_id is None
        assert not is_running(row)
        assert row.entry_count == 4
        assert row.leaf_uuid == entries[-1].uuid


async def test_terminal_before_begin_leaves_session_not_running(db) -> None:
    """极快的运行可能在 begin 落库前就进入终态（后台任务先于编排层调度）。

    回归保护：此时 begin 必须跳过 mark_running 与心跳，否则会话被重新标成
    「进行中」，且孤儿心跳任务不断续期，状态永远无法自愈（曾致偶发失败）。
    """
    from movieclaw_api.services.agent_session_recorder import AgentSessionRecorder
    from movieclaw_api.services.agent_sessions import get_agent_session_store
    from movieclaw_db.repositories.agent_session_repo import (
        AgentSessionRepository,
        is_running,
    )

    store = get_agent_session_store()
    sid = store.create().session_id
    async with db.session() as session:
        await AgentSessionRepository(session).create(sid, title=None)

    recorder = AgentSessionRecorder(store, sid, entry_count=0)
    await recorder.on_terminal(AgentEvent(type="agent_done", run_id="fast"))
    await recorder.begin("fast")

    async with db.session() as session:
        row = await AgentSessionRepository(session).get(sid)
        assert row.active_run_id is None
        assert not is_running(row)
    # 心跳任务不应被创建（无人取消它会永远续期运行状态）
    assert recorder._heartbeat_task is None


async def test_rebuild_restores_index_from_files(db) -> None:
    """索引丢行/多行都能从文件校准回来（文件是事实源）。"""
    from movieclaw_api.services.agent_session_recorder import rebuild_agent_session_index
    from movieclaw_api.services.agent_sessions import get_agent_session_store
    from movieclaw_db.models import AgentSession
    from movieclaw_db.repositories.agent_session_repo import AgentSessionRepository

    store = get_agent_session_store()
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="重建我"))
    # 场景 1：文件有、索引没有（崩在两步写入之间）
    # 场景 2：索引有、文件没有（用户手删了转录）
    async with db.session() as session:
        session.add(AgentSession(id="ghost", title="幽灵会话"))
        await session.commit()

    await rebuild_agent_session_index()

    async with db.session() as session:
        repo = AgentSessionRepository(session)
        restored = await repo.get(sid)
        assert restored is not None
        assert restored.title == "重建我"
        assert restored.entry_count == 1
        assert await repo.get("ghost") is None


# ---------------------------------------------------------------------------
# 会话 API 端到端（TestClient + 假协议）
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    reset_agent_session_store()
    from movieclaw_api.services.agent_attachments import reset_agent_attachment_store

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


def _send_message_and_wait(client, payload: dict) -> tuple[str, str]:
    """提交一条用户消息并等待终态，返回 (session_id, message_id)。"""
    body = dict(payload)
    started = client.post("/api/v1/sessions", json=body)
    assert started.status_code == 202
    data = started.json()["data"]
    with client.stream("GET", f"/api/v1/sessions/{data['session_id']}/events") as r:
        r.read()
    return data["session_id"], data["message_id"]


def _wait_not_running(client, session_id: str) -> dict:
    """等待终态收尾落库（on_terminal 与 SSE 收流并发，留一个短轮询窗）。"""
    for _ in range(50):
        item = client.get(f"/api/v1/sessions/{session_id}").json()["data"]["session"]
        if not item["running"]:
            return item
        time.sleep(0.1)
    pytest.fail("会话运行状态未在期限内清空")


def test_start_creates_session_and_persists_message(client) -> None:
    configure_provider(client)
    session_id, _ = _send_message_and_wait(client, {"content": "找沙丘 4K"})

    item = _wait_not_running(client, session_id)
    assert item["title"] == "找沙丘 4K"
    assert item["last_prompt"] == "找沙丘 4K"
    assert item["entry_count"] == 2  # user + 终答 assistant
    assert "active_run_id" not in item

    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    roles = [e["message"]["role"] for e in detail["entries"]]
    assert roles == ["user", "assistant"]
    assert detail["entries"][0]["message_id"]
    assert "id" not in detail["entries"][0]
    assistant = detail["entries"][1]
    # 定稿 assistant 带模型元数据；thinking 以内容块形式原样保留
    assert assistant["finish_reason"] == "stop"
    assert assistant["usage"]["total_tokens"] == 14
    parts = assistant["message"]["content"]
    assert [p["type"] for p in parts] == ["thinking", "text"]

    listing = client.get("/api/v1/sessions").json()["data"]
    assert [s["id"] for s in listing] == [session_id]


def test_followup_message_builds_history_from_transcript(client, monkeypatch) -> None:
    """后续消息的上下文来自服务端转录，而非前端回传。"""
    captured: dict = {}

    class _CaptureProtocol(_StreamProtocol):
        async def chat_stream(self, request, model_id):
            captured["roles"] = [m.role for m in request.messages]
            captured["last"] = request.messages[-1].text()
            async for e in super().chat_stream(request, model_id):
                yield e

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _CaptureProtocol)
    # 进程级 _runtime_router 按配置指纹缓存协议客户端；换一个 Key 使指纹
    # 变化，强制用本测试替换后的协议类重建（同 test_agent 的既有做法）
    client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": "sk-session-resume",
            "default_model": "qwen3.7-max",
        },
    )

    session_id, first_message_id = _send_message_and_wait(client, {"content": "第一轮"})
    _wait_not_running(client, session_id)
    second, _ = _send_message_and_wait(
        client,
        {
            "content": "第二轮",
            "session_id": session_id,
        },
    )
    assert second == session_id
    assert captured["roles"] == ["system", "user", "assistant", "user"]
    assert captured["last"] == "第二轮"

    item = _wait_not_running(client, session_id)
    assert item["entry_count"] == 4
    assert item["last_prompt"] == "第二轮"
    assert item["title"] == "第一轮"  # 标题保持首轮


def test_fork_api_creates_independent_session_and_resumes_snapshot(client, monkeypatch) -> None:
    """Fork 返回新 ID；删除源会话后，目标仍从快照上下文继续。"""
    captured: list[list[str]] = []

    class _CaptureProtocol(_StreamProtocol):
        async def chat_stream(self, request, model_id):
            captured.append([message.text() for message in request.messages])
            async for event in super().chat_stream(request, model_id):
                yield event

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _CaptureProtocol)
    client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": "sk-session-fork",
            "default_model": "qwen3.7-max",
        },
    )
    source_id, _ = _send_message_and_wait(client, {"content": "第一轮"})
    _wait_not_running(client, source_id)
    source_before = client.get(f"/api/v1/sessions/{source_id}").json()["data"]

    forked = client.post(f"/api/v1/sessions/{source_id}/fork")
    assert forked.status_code == 201
    fork_data = forked.json()["data"]
    target_id = fork_data["session"]["id"]
    assert target_id != source_id
    assert fork_data["session"]["running"] is False
    assert fork_data["entries"][0]["type"] == "handoff"
    assert fork_data["entries"][0]["source_session_id"] == source_id
    assert fork_data["entries"][0]["replacement_history"]
    # Fork 是只读快照，不改源轨迹。
    assert client.get(f"/api/v1/sessions/{source_id}").json()["data"] == source_before

    assert client.delete(f"/api/v1/sessions/{source_id}").status_code == 200
    fetched = client.get(f"/api/v1/sessions/{target_id}")
    assert fetched.status_code == 200
    # 常规轨迹读取不重复下发继承快照，只保留来源信息。
    target_entries = fetched.json()["data"]["entries"]
    assert target_entries[0]["type"] == "handoff"
    assert target_entries[0]["source_session_id"] == source_id
    assert target_entries[0]["replacement_history"] == []

    resumed_id, _ = _send_message_and_wait(
        client,
        {"content": "从这里继续", "session_id": target_id},
    )
    assert resumed_id == target_id
    _wait_not_running(client, target_id)
    assert captured[-1][1:] == ["第一轮", "已找到资源", "从这里继续"]


def test_fork_api_rejects_missing_running_and_empty_source(client, monkeypatch) -> None:
    configure_provider(client)
    assert client.post("/api/v1/sessions/missing/fork").status_code == 404

    # 存储层空会话虽然不会由普通 UI 产生，但 Fork 仍需给出明确业务错误。
    import asyncio
    import threading

    from movieclaw_api.services.agent_sessions import get_agent_session_store
    from movieclaw_db.engine import get_database
    from movieclaw_db.repositories.agent_session_repo import AgentSessionRepository

    store = get_agent_session_store()
    empty_id = store.create().session_id

    async def create_index() -> None:
        async with get_database().session() as db_session:
            await AgentSessionRepository(db_session).create(empty_id, title=None)

    asyncio.run(create_index())
    assert client.post(f"/api/v1/sessions/{empty_id}/fork").status_code == 400

    # 「运行中拒绝分叉」不能依赖调度时序：mock LLM 即时返回时，后台运行可能
    # 在 fork 请求发出前就已结束（会话不再运行中，fork 被合法接受）。用事件
    # 闸门扣住流式返回，保证断言窗口内会话必然运行中，断言后放行正常收尾。
    gate = threading.Event()

    class _GatedProtocol(_StreamProtocol):
        async def chat_stream(self, request, model_id):
            # 超时兜底：断言失败提前退出时闸门不会 set，避免工作线程悬死
            await asyncio.to_thread(gate.wait, 30)
            async for event in super().chat_stream(request, model_id):
                yield event

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _GatedProtocol)
    # 进程级 _runtime_router 按配置指纹缓存协议客户端；换一个 Key 强制重建
    client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": "sk-fork-running",
            "default_model": "qwen3.7-max",
        },
    )
    started = client.post("/api/v1/sessions", json={"content": "仍在执行"})
    running_id = started.json()["data"]["session_id"]
    assert client.post(f"/api/v1/sessions/{running_id}/fork").status_code == 400
    gate.set()
    with client.stream("GET", f"/api/v1/sessions/{running_id}/events") as response:
        response.read()


def test_send_message_to_unknown_session_returns_404(client) -> None:
    configure_provider(client)
    r = client.post(
        "/api/v1/sessions", json={"content": "x", "session_id": "missing"}
    )
    assert r.status_code == 404


def test_legacy_agent_and_turn_routes_are_removed(client) -> None:
    """破坏式重构不保留旧 agent/run/turn/truncate 协议入口。"""
    for path in (
        "/api/v1/agent/start",
        "/api/v1/sessions/missing/messages",
        "/api/v1/sessions/missing/rewind",
        "/api/v1/sessions/missing/turns",
        "/api/v1/sessions/missing/truncate-from-turn",
    ):
        assert client.post(path, json={}).status_code == 404


def test_rename_session_updates_index_title(client) -> None:
    configure_provider(client)
    session_id, _ = _send_message_and_wait(client, {"content": "起个名字"})
    _wait_not_running(client, session_id)

    r = client.patch(
        f"/api/v1/sessions/{session_id}", json={"title": "  我的追剧计划  "}
    )
    assert r.status_code == 200
    assert r.json()["data"]["title"] == "我的追剧计划"
    # 列表同步生效；转录文件不因改名而变化（标题只是索引元数据）
    items = client.get("/api/v1/sessions").json()["data"]
    assert items[0]["title"] == "我的追剧计划"

    assert client.patch(
        "/api/v1/sessions/missing", json={"title": "x"}
    ).status_code == 404
    assert client.patch(
        f"/api/v1/sessions/{session_id}", json={"title": "   "}
    ).status_code == 422


def _user_message_ids(client, session_id: str) -> list[str]:
    """会话里各条用户消息的公开 message_id。"""
    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    return [
        e["message_id"]
        for e in detail["entries"]
        if e["type"] == "message" and e["message"]["role"] == "user"
    ]


def test_retry_replaces_user_message_and_later_context(client, monkeypatch) -> None:
    """retry 一次完成问题替换，并为新消息生成稳定编号。"""
    captured: dict = {}

    class _CaptureProtocol(_StreamProtocol):
        async def chat_stream(self, request, model_id):
            captured["roles"] = [message.role for message in request.messages]
            captured["texts"] = [message.text() for message in request.messages]
            async for event in super().chat_stream(request, model_id):
                yield event

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _CaptureProtocol)
    client.put(
        "/api/v1/llm/provider",
        json={
            "provider_type": "bailian",
            "api_key": "sk-retry",
            "default_model": "qwen3.7-max",
        },
    )

    session_id, first_message_id = _send_message_and_wait(
        client, {"content": "第一轮"}
    )
    _wait_not_running(client, session_id)
    _, old_message_id = _send_message_and_wait(
        client, {"content": "第二轮", "session_id": session_id}
    )
    _wait_not_running(client, session_id)

    retried = client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"message_id": old_message_id, "content": "第二轮（改写后）"},
    )
    assert retried.status_code == 202
    new_message_id = retried.json()["data"]["message_id"]
    assert new_message_id != old_message_id
    with client.stream("GET", f"/api/v1/sessions/{session_id}/events") as response:
        response.read()
    _wait_not_running(client, session_id)

    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    user_entries = [
        entry
        for entry in detail["entries"]
        if entry["type"] == "message" and entry["message"]["role"] == "user"
    ]
    assert [entry["message"]["content"] for entry in user_entries] == [
        "第一轮",
        "第二轮（改写后）",
    ]
    assert [entry["message_id"] for entry in user_entries] == [first_message_id, new_message_id]
    assert old_message_id not in {entry["message_id"] for entry in detail["entries"]}
    assert captured["roles"] == ["system", "user", "assistant", "user"]
    assert "第二轮" not in captured["texts"]
    assert captured["texts"][-1] == "第二轮（改写后）"


def test_retry_first_message_replaces_session_title(client) -> None:
    """替换首条提问时，会话标题与最后提问同步更新。"""
    configure_provider(client)
    session_id, first_message_id = _send_message_and_wait(
        client, {"content": "写错了的第一句"}
    )
    _wait_not_running(client, session_id)

    retried = client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"message_id": first_message_id, "content": "改好的第一句"},
    )
    assert retried.status_code == 202
    with client.stream("GET", f"/api/v1/sessions/{session_id}/events") as response:
        response.read()
    _wait_not_running(client, session_id)
    item = client.get(f"/api/v1/sessions/{session_id}").json()["data"]["session"]
    assert item["title"] == "改好的第一句"
    assert item["last_prompt"] == "改好的第一句"


def test_retry_without_content_resubmits_original_message(client) -> None:
    configure_provider(client)
    session_id, old_message_id = _send_message_and_wait(client, {"content": "原问题"})
    _wait_not_running(client, session_id)

    retried = client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"message_id": old_message_id},
    )
    assert retried.status_code == 202
    assert retried.json()["data"]["message_id"] != old_message_id
    with client.stream("GET", f"/api/v1/sessions/{session_id}/events") as response:
        response.read()
    _wait_not_running(client, session_id)
    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    assert detail["entries"][0]["message"]["content"] == "原问题"


def test_retry_rejects_unknown_or_non_user_target(client) -> None:
    configure_provider(client)
    session_id, _ = _send_message_and_wait(client, {"content": "只有一轮"})
    _wait_not_running(client, session_id)

    assert client.post(
        "/api/v1/sessions/missing/retry", json={"message_id": "x"}
    ).status_code == 404
    assert client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"message_id": "不存在"},
    ).status_code == 404
    # assistant 回答不是合法的 retry 锚点
    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    assistant_id = detail["entries"][1]["message_id"]
    assert client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"message_id": assistant_id},
    ).status_code == 400


def test_start_returns_message_id_for_fresh_message(client) -> None:
    """新发出的 user message 立即返回稳定锚点，无需等待轨迹回放。"""
    configure_provider(client)
    started = client.post("/api/v1/sessions", json={"content": "刚发的消息"})
    data = started.json()["data"]
    with client.stream("GET", f"/api/v1/sessions/{data['session_id']}/events") as r:
        r.read()
    _wait_not_running(client, data["session_id"])
    assert data["message_id"] == _user_message_ids(client, data["session_id"])[0]


def test_delete_session_removes_file_and_index(client) -> None:
    from movieclaw_api.services.agent_sessions import get_agent_session_store

    configure_provider(client)
    session_id, _ = _send_message_and_wait(client, {"content": "删掉我"})
    _wait_not_running(client, session_id)

    assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 200
    assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404
    assert not get_agent_session_store().path(session_id).exists()
    assert client.get("/api/v1/sessions").json()["data"] == []
