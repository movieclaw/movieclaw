"""Agent 运行时稳定性：中断收尾、恢复语义与竞态防御的模拟测试。

覆盖 docs/design/agent-runtime-resilience.md 的三场景验收：
1. 用户主动停止 → 转录配对完整（user_cancelled 文案），可立即续聊；
2. 优雅停机（registry.close）→ service_interrupted 文案，收尾在停机窗内完成；
3. 硬崩（kill -9 / 断电）→ 启动自愈补配对，续聊不再被供应商 400 拒绝。
外加取消竞态压测（同一 tool_call 永远恰好一条回执）与终态钩子时序。
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from movieclaw_agent import AgentEvent, AgentStartParams
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.agent_runs import AgentRunRegistry
from movieclaw_api.services.agent_sessions import AgentSessionStore
from movieclaw_llm import ChatMessage, ChatResponse, ProviderInfo, ToolCall
from movieclaw_llm.base import BaseLlmProtocol
from movieclaw_llm.models import ChatStreamEvent
from movieclaw_llm.protocols import PROTOCOLS

# ---------------------------------------------------------------------------
# 存储层：seal 文案分级 / 幂等 / 读取侧内存修复
# ---------------------------------------------------------------------------


def _orphan_session(store: AgentSessionStore) -> str:
    """构造一个「工具调用没有回执」的中断现场，返回会话 id。"""
    sid = store.create().session_id
    store.append(sid, ChatMessage(role="user", content="执行任务"))
    store.append(
        sid,
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_x", name="bash", arguments={"command": "ls"})],
        ),
    )
    return sid


def test_seal_reason_selects_text_and_is_idempotent(tmp_path) -> None:
    """文案分级：用户停止 vs 服务中断两种回执；重复 seal 零写入。"""
    store = AgentSessionStore(tmp_path)

    sid = _orphan_session(store)
    assert store.seal_pending_tool_calls(sid, reason="user_cancelled") == 1
    receipt = store.read(sid)[1][-1].message
    assert receipt.tool_call_id == "call_x"
    assert "用户停止了本次运行" in receipt.text()
    assert "不要盲目重发" in receipt.text()
    # 幂等：已配对完整，再 seal 不写入
    assert store.seal_pending_tool_calls(sid, reason="user_cancelled") == 0
    assert store.seal_pending_tool_calls(sid, reason="service_interrupted") == 0

    sid2 = _orphan_session(store)
    assert store.seal_pending_tool_calls(sid2, reason="service_interrupted") == 1
    receipt2 = store.read(sid2)[1][-1].message
    assert "服务重启或异常" in receipt2.text()
    assert "结果未知" in receipt2.text()


def test_build_history_repairs_unpaired_in_memory_only(tmp_path) -> None:
    """读取侧最后防线：写入侧 seal 没跑到时，投影仍配对完整且不回写文件。"""
    store = AgentSessionStore(tmp_path)
    sid = _orphan_session(store)
    entry_count_before = len(store.read(sid)[1])

    history = store.build_history(sid)

    # 修复后的历史协议完整：assistant 的 call_x 有了合成回执
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[-1].tool_call_id == "call_x"
    assert "结果未知" in history[-1].text()
    # 只修投影，文件保持原样（回写属于写入侧 seal 的职责）
    assert len(store.read(sid)[1]) == entry_count_before


# ---------------------------------------------------------------------------
# 注册表：终态钩子的时序与 reason 判定
# ---------------------------------------------------------------------------


class _CleanupOrderRunner:
    """取消时先做一段自己的收尾写入，用于断言 on_terminal 在其之后执行。"""

    def __init__(self, journal: list[str]) -> None:
        self.journal = journal
        self.started = asyncio.Event()

    async def start(self, params, *, run_id=None):
        yield AgentEvent(type="agent_start", run_id=run_id, provider="测试", model="m")
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 模拟 runner 在取消路径上的落盘（半截 aborted 定稿等）
            await asyncio.sleep(0.05)
            self.journal.append("runner-cleanup")
            raise
        yield  # pragma: no cover - 只为保持 async generator 形态


async def test_on_terminal_runs_after_runner_cleanup_with_user_cancelled() -> None:
    """收尾时序（修缺口 B）：终态钩子必须等运行协程彻底停笔后才执行。

    旧实现挂在「第一个终态事件」上，会与 runner 的取消清理并发，产生
    「seal 与真实回执同 id 并存」的竞态；现在挂在协程 finally，天然串行。
    """
    journal: list[str] = []
    finalized = asyncio.Event()

    async def on_terminal(event: AgentEvent, reason: str) -> None:
        journal.append(f"terminal:{event.type}:{reason}")
        finalized.set()

    registry = AgentRunRegistry()
    runner = _CleanupOrderRunner(journal)
    registry.start(
        runner, AgentStartParams(input="x"), session_id="s1", on_terminal=on_terminal
    )
    try:
        await runner.started.wait()
        await registry.cancel_session("s1")
        await asyncio.wait_for(finalized.wait(), timeout=5)
        assert journal == ["runner-cleanup", "terminal:agent_cancelled:user_cancelled"]
    finally:
        await registry.close()


async def test_cancel_watchdog_spares_slow_finalize(monkeypatch) -> None:
    """看门狗只解卡「不停笔的 runner」：合法的慢收尾不能被复投的取消打断。

    收尾（杀进程组、大转录 seal、DB 提交）超过复投间隔时，若看门狗对整个
    任务计时就会把 CancelledError 打进 on_terminal，finish_run 被跳过、
    会话多挂 30 秒心跳窗——finalizing 标志让看门狗在 runner 停笔后收手。
    """
    from movieclaw_api.services import agent_runs

    monkeypatch.setattr(agent_runs, "CANCEL_RETRY_SECONDS", 0.1)
    journal: list[str] = []
    finalized = asyncio.Event()

    async def slow_on_terminal(event: AgentEvent, reason: str) -> None:
        await asyncio.sleep(0.5)  # 远超复投间隔的合法慢收尾
        journal.append(f"terminal:{reason}")
        finalized.set()

    registry = AgentRunRegistry()
    runner = _CleanupOrderRunner(journal)
    registry.start(
        runner, AgentStartParams(input="x"), session_id="s1", on_terminal=slow_on_terminal
    )
    try:
        await runner.started.wait()
        await registry.cancel_session("s1")
        await asyncio.wait_for(finalized.wait(), timeout=5)
        # 收尾完整执行过一次；没有被看门狗二次取消而放弃
        assert journal == ["runner-cleanup", "terminal:user_cancelled"]
    finally:
        await registry.close()


async def test_close_finalizes_with_service_interrupted_reason() -> None:
    """停机取消：同为 agent_cancelled 终态，reason 必须切到 service_interrupted。"""
    journal: list[str] = []
    finalized = asyncio.Event()

    async def on_terminal(event: AgentEvent, reason: str) -> None:
        journal.append(f"terminal:{event.type}:{reason}")
        finalized.set()

    registry = AgentRunRegistry()
    runner = _CleanupOrderRunner(journal)
    registry.start(
        runner, AgentStartParams(input="x"), session_id="s1", on_terminal=on_terminal
    )
    await runner.started.wait()
    await registry.close()
    await asyncio.wait_for(finalized.wait(), timeout=5)
    assert journal == ["runner-cleanup", "terminal:agent_cancelled:service_interrupted"]


# ---------------------------------------------------------------------------
# API 端到端：三场景恢复语义
# ---------------------------------------------------------------------------


class _StreamProtocol(BaseLlmProtocol):
    """基础假协议：直接给终答。"""

    async def chat(self, request, model_id):  # pragma: no cover
        return ChatResponse(content="pong", finish_reason="stop")

    async def chat_stream(self, request, model_id):
        snap = ChatResponse(model=model_id, provider=self.config.name)
        yield ChatStreamEvent(type="start", partial=snap)
        yield ChatStreamEvent(type="text_delta", delta="完成", partial=snap)
        yield ChatStreamEvent(
            type="done",
            partial=ChatResponse(
                content="完成", finish_reason="stop", model=model_id, provider=self.config.name
            ),
        )

    async def test_connection(self):  # pragma: no cover
        return ProviderInfo(models=[])

    async def close(self):
        pass


_MARKER = "movieclaw-resilience-hang"


class _HangingBashProtocol(_StreamProtocol):
    """首步让 Agent 执行一条挂起的 bash（sleep），续聊时给终答。

    真实工具真实子进程：停止/停机路径连带验证进程组收割。
    """

    async def chat_stream(self, request, model_id):
        captured = getattr(type(self), "captured", None)
        if captured is not None:
            captured.append([m.role for m in request.messages])
        snap = ChatResponse(model=model_id, provider=self.config.name)
        yield ChatStreamEvent(type="start", partial=snap)
        if any(m.role == "tool" for m in request.messages):
            yield ChatStreamEvent(type="text_delta", delta="已确认状态", partial=snap)
            yield ChatStreamEvent(
                type="done",
                partial=ChatResponse(
                    content="已确认状态",
                    finish_reason="stop",
                    model=model_id,
                    provider=self.config.name,
                ),
            )
            return
        tc = ToolCall(
            id="call_hang", name="bash", arguments={"command": f"sleep 987654 # {_MARKER}"}
        )
        yield ChatStreamEvent(type="toolcall_end", tool_call=tc, partial=snap)
        yield ChatStreamEvent(
            type="done",
            partial=ChatResponse(
                tool_calls=[tc],
                finish_reason="tool_calls",
                model=model_id,
                provider=self.config.name,
            ),
        )


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """会话/DB/密钥全部指向临时目录；调用方自建 TestClient（可多次启停模拟重启）。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    from movieclaw_api.services.agent_attachments import reset_agent_attachment_store
    from movieclaw_api.services.agent_sessions import reset_agent_session_store

    reset_agent_session_store()
    reset_agent_attachment_store()
    yield tmp_path
    get_settings.cache_clear()
    reset_agent_session_store()
    reset_agent_attachment_store()


def _make_client(monkeypatch, protocol_cls) -> TestClient:
    monkeypatch.setitem(PROTOCOLS, "openai_chat", protocol_cls)
    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    return TestClient(app)


def _configure_provider(client: TestClient, api_key: str) -> None:
    # api_key 进路由器缓存指纹：每个测试用独立 Key，强制用本测试的协议类重建
    client.put(
        "/api/v1/llm/provider",
        json={"provider_type": "bailian", "api_key": api_key, "default_model": "qwen3.7-max"},
    )


def _wait_marker_process(present: bool, timeout: float = 8.0) -> None:
    """等待标记子进程出现/消失（bash 工具真实起了 sleep 子进程）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = subprocess.run(["pgrep", "-f", _MARKER], capture_output=True, text=True)
        if bool(probe.stdout.strip()) == present:
            return
        time.sleep(0.05)
    pytest.fail(f"标记进程未在期限内{'出现' if present else '消失'}")


def _wait_run_settled(client: TestClient, session_id: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = client.get(f"/api/v1/sessions/{session_id}").json()["data"]["session"]
        if not item["running"]:
            return
        time.sleep(0.05)
    import faulthandler
    import sys

    faulthandler.dump_traceback(all_threads=True, file=sys.stderr)
    pytest.fail("会话运行状态未在期限内清空")


def _transcript_entries(client: TestClient, session_id: str) -> list[dict]:
    return client.get(f"/api/v1/sessions/{session_id}").json()["data"]["entries"]


def test_user_stop_seals_pairing_and_resend_continues(agent_env, monkeypatch) -> None:
    """场景 3（用户主动停止）：工具执行中点停止 → 回执补齐（用户停止文案）、
    子进程整组收割、立即续聊不被拒绝且上下文配对完整。"""
    captured: list[list[str]] = []
    _HangingBashProtocol.captured = captured
    with _make_client(monkeypatch, _HangingBashProtocol) as client:
        _configure_provider(client, "sk-resilience-stop")
        started = client.post("/api/v1/sessions", json={"content": "执行长任务"})
        assert started.status_code == 202
        session_id = started.json()["data"]["session_id"]
        _wait_marker_process(present=True)

        assert client.post(f"/api/v1/sessions/{session_id}/stop").status_code == 200
        _wait_run_settled(client, session_id)
        _wait_marker_process(present=False)

        entries = _transcript_entries(client, session_id)
        roles = [e["message"]["role"] for e in entries]
        assert roles == ["user", "assistant", "tool"]
        receipt = entries[-1]["message"]
        assert receipt["tool_call_id"] == "call_hang"
        assert "用户停止了本次运行" in receipt["content"]

        # 立即续聊：不被 400/409 拒绝，模型收到的历史 call/output 配对完整
        resent = client.post(
            "/api/v1/sessions", json={"content": "继续", "session_id": session_id}
        )
        assert resent.status_code == 202
        client.get(f"/api/v1/sessions/{session_id}/events")
        _wait_run_settled(client, session_id)
        assert captured[-1] == ["system", "user", "assistant", "tool", "user"]
        assert _transcript_entries(client, session_id)[-1]["message"]["role"] == "assistant"
    _HangingBashProtocol.captured = None


def test_graceful_shutdown_seals_with_service_interrupted(agent_env, monkeypatch) -> None:
    """场景 1（停机升级）：工具执行中关停应用 → 停机窗内完成收尾，
    转录带服务中断回执，子进程一并收割。"""
    client = _make_client(monkeypatch, _HangingBashProtocol)
    with client:
        _configure_provider(client, "sk-resilience-shutdown")
        started = client.post("/api/v1/sessions", json={"content": "执行长任务"})
        session_id = started.json()["data"]["session_id"]
        _wait_marker_process(present=True)
    # 离开 with 即触发 lifespan 停机：registry.close() 取消并等待收尾完成

    _wait_marker_process(present=False)
    transcript = (agent_env / "agent-sessions" / f"{session_id}.jsonl").read_text()
    assert "call_hang" in transcript
    assert "服务重启或异常" in transcript
    assert "结果未知" in transcript


def test_crash_recovery_startup_selfheal_and_resend(agent_env, monkeypatch) -> None:
    """场景 2（异常停机）：kill -9 没有收尾机会，孤儿 tool_call 留在转录里。
    重启后启动自愈补配对（幂等），续聊立即可用（修缺口 A 的「永久 400」）。"""
    # 直接构造硬崩现场：assistant 带 tool_call、没有回执，进程已不在
    store = AgentSessionStore(agent_env / "agent-sessions")
    session_id = _orphan_session(store)

    # 第一次重启：启动自愈补配对
    with _make_client(monkeypatch, _StreamProtocol) as client:
        _configure_provider(client, "sk-resilience-crash")
        entries = _transcript_entries(client, session_id)
        roles = [e["message"]["role"] for e in entries]
        assert roles == ["user", "assistant", "tool"]
        receipt = entries[-1]["message"]
        assert receipt["tool_call_id"] == "call_x"
        assert "服务重启或异常" in receipt["content"]

        # 直接续聊：历史配对完整，正常运行到终态
        resent = client.post(
            "/api/v1/sessions", json={"content": "继续", "session_id": session_id}
        )
        assert resent.status_code == 202
        client.get(f"/api/v1/sessions/{session_id}/events")
        _wait_run_settled(client, session_id)
        entry_count = len(_transcript_entries(client, session_id))
        assert _transcript_entries(client, session_id)[-1]["message"]["role"] == "assistant"

    # 第二次重启：自愈幂等，零写入
    with _make_client(monkeypatch, _StreamProtocol) as client:
        assert len(_transcript_entries(client, session_id)) == entry_count


def test_cancel_race_every_tool_call_gets_exactly_one_receipt(agent_env, monkeypatch) -> None:
    """取消竞态压测：发起长工具运行后在随机时点取消，反复多轮，
    同一 tool_call_id 永远恰好一条回执（不重复 seal、不遗漏）。"""
    with _make_client(monkeypatch, _HangingBashProtocol) as client:
        _configure_provider(client, "sk-resilience-race")
        for round_no in range(15):
            started = client.post("/api/v1/sessions", json={"content": f"第 {round_no} 轮"})
            assert started.status_code == 202
            session_id = started.json()["data"]["session_id"]
            # 随机竞态窗：流式前 / 流式中 / 工具执行中都可能被取消命中
            time.sleep(0.02 * (round_no % 5))
            assert client.post(f"/api/v1/sessions/{session_id}/stop").status_code == 200
            _wait_run_settled(client, session_id)

            entries = _transcript_entries(client, session_id)
            call_ids: list[str] = []
            receipt_ids: list[str] = []
            for entry in entries:
                message = entry["message"]
                if message["role"] == "assistant":
                    call_ids.extend(tc["id"] for tc in message.get("tool_calls") or [])
                elif message["role"] == "tool":
                    receipt_ids.append(message["tool_call_id"])
            assert sorted(call_ids) == sorted(receipt_ids), (
                f"第 {round_no} 轮配对失衡：calls={call_ids} receipts={receipt_ids}"
            )
            assert len(receipt_ids) == len(set(receipt_ids)), (
                f"第 {round_no} 轮出现重复回执：{receipt_ids}"
            )
    _wait_marker_process(present=False)
