"""会话记录器：把一次 Agent 运行的定稿消息接进 JSONL 存储与 DB 索引。

一次运行一个实例，由 `POST /sessions` 编排层创建并挂到两个钩子上：
- ``AgentRunner(on_message=...)``——每条定稿消息（assistant / tool）落盘
  并刷新索引（先文件后 DB，见 agent_sessions 模块的写入顺序约定）；
- ``AgentRunRegistry.start(on_terminal=...)``——运行终态时收尾：停心跳、
  给未配对的 tool_call 补错误回执、清空运行标记（列表立即显示已结束）。

心跳独立于消息追加：长工具执行期间可能几十秒没有新消息，心跳任务保证
``last_heartbeat_at`` 持续刷新，前端才能正确区分「还在跑」和「已崩溃」。
进程硬崩时心跳自然停止，超时窗过后状态自愈为已结束——这正是不持久化
静态 status 字段的原因。
"""

from __future__ import annotations

import asyncio
import logging

from movieclaw_agent import CompactionResult
from movieclaw_agent.events import AgentEvent
from movieclaw_api.services.agent_sessions import (
    PREVIEW_MAX_CHARS,
    AgentSessionStore,
    SealReason,
    message_preview,
)
from movieclaw_db.engine import get_database
from movieclaw_db.repositories.agent_session_repo import AgentSessionRepository
from movieclaw_llm import ChatMessage, ChatResponse, ContentPart

logger = logging.getLogger("movieclaw_api.agent_session_recorder")

#: 心跳间隔（秒）；超时窗见 repo.HEARTBEAT_TIMEOUT_SECONDS（须大于本值）
HEARTBEAT_INTERVAL_SECONDS = 10


class AgentSessionRecorder:
    """单次运行的持久化协调者（消息落盘 + 索引维护 + 心跳）。"""

    def __init__(
        self,
        store: AgentSessionStore,
        session_id: str,
        *,
        entry_count: int,
    ) -> None:
        self._store = store
        self._session_id = session_id
        # 运行开始时会话已有的 entry 数；之后每次落盘递增，避免反复重读文件
        self._entry_count = entry_count
        self._heartbeat_task: asyncio.Task[None] | None = None
        # begin 与 on_terminal 分别由编排层和后台任务并发调用：极快（或启动
        # 即失败）的运行可能在 begin 落库前就进入终态。若不串行，finish_run
        # 会先读到 mark_running 提交前的旧行，「清空 active_run_id」被 ORM 判
        # 定为无变更而丢弃，随后 mark_running 才提交，会话从此永远显示运行中
        # （孤儿心跳任务还会不断续期）。同一把锁 + 终态标志保证两者有序。
        self._lifecycle_lock = asyncio.Lock()
        self._terminated = False

    # ------------------------------------------------------------------
    # 运行生命周期
    # ------------------------------------------------------------------
    async def begin(self, run_id: str) -> None:
        """运行启动：标记 active_run_id 并开启心跳任务。

        运行已先一步进入终态时跳过：此时再标记会把已结束的运行写回「进行
        中」，且新起的心跳任务永远无人取消（on_terminal 已经执行过了）。
        """
        async with self._lifecycle_lock:
            if self._terminated:
                logger.info(
                    "Agent 运行在标记开始前已进入终态，跳过运行标记 session=%s run=%s",
                    self._session_id,
                    run_id,
                )
                return
            async with get_database().session() as session:
                await AgentSessionRepository(session).mark_running(self._session_id, run_id)
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"agent-session-heartbeat-{self._session_id}",
            )

    async def record_user_message(
        self,
        content: str | list[ContentPart],
        *,
        thinking_level: str | None = None,
    ) -> str:
        """落盘 user message，刷新标题（仅首条）与最后提示预览，返回 message id。

        内部仍以 uuid 存储，API 将它作为 message_id 返回；刚提交的消息还没
        经历 transcript 回放，调用方需要该编号作为 retry 锚点。
        带图消息以内容块形态传入（图片是引用型 ImagePart，落盘不含字节）；
        thinking_level 是本条消息生效的思维链档位（信封元数据，续聊沿用）。
        """
        message = ChatMessage(role="user", content=content)
        entry = self._store.append(self._session_id, message, thinking_level=thinking_level)
        self._entry_count += 1
        preview = message_preview(message)[:PREVIEW_MAX_CHARS]
        async with get_database().session() as session:
            await AgentSessionRepository(session).touch_after_append(
                self._session_id,
                leaf_uuid=entry.uuid,
                entry_count=self._entry_count,
                last_prompt=preview,
                title=preview,
            )
        return entry.uuid

    async def on_message(self, message: ChatMessage, response: ChatResponse | None) -> None:
        """runner 定稿消息回调：assistant 带响应元数据，tool 结果不带。"""
        entry = self._store.append(
            self._session_id,
            message,
            model=response.model if response else None,
            usage=response.usage if response else None,
            finish_reason=response.finish_reason if response else None,
        )
        self._entry_count += 1
        async with get_database().session() as session:
            await AgentSessionRepository(session).touch_after_append(
                self._session_id,
                leaf_uuid=entry.uuid,
                entry_count=self._entry_count,
            )

    async def on_compaction(self, result: CompactionResult) -> None:
        """runner 压缩定稿回调：压缩行落盘并刷新索引（与 on_message 同一节奏）。"""
        entry = self._store.append_compaction(self._session_id, result)
        self._entry_count += 1
        async with get_database().session() as session:
            await AgentSessionRepository(session).touch_after_append(
                self._session_id,
                leaf_uuid=entry.uuid,
                entry_count=self._entry_count,
            )

    async def on_terminal(self, event: AgentEvent, reason: str = "user_cancelled") -> None:
        """运行终态收尾（done / error / cancelled 统一路径）。

        由运行协程在自身 finally 里调用（registry._finalize）——此刻 runner
        必然不再落盘，补配对读到的就是转录终态。``reason`` 决定合成回执的
        文案：用户停止 vs 服务中断（停机/异常），见 agent_sessions 的
        SealReason。

        与 begin 持同一把锁：保证 finish_run 一定在 mark_running 提交之后
        读行（清空才会真正落库），或者 begin 尚未执行时由终态标志令其跳过。
        """
        seal_reason: SealReason = (
            "service_interrupted" if reason == "service_interrupted" else "user_cancelled"
        )
        async with self._lifecycle_lock:
            self._terminated = True
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            # 取消/出错的运行可能留下没有回执的 tool_call，补齐保证配对完整
            try:
                sealed = self._store.seal_pending_tool_calls(
                    self._session_id, reason=seal_reason
                )
            except Exception:  # noqa: BLE001 - 收尾尽力而为，文件被删等异常不再连锁
                logger.exception("会话中断收尾失败 session=%s", self._session_id)
                sealed = 0
            self._entry_count += sealed
            async with get_database().session() as session:
                repo = AgentSessionRepository(session)
                if sealed:
                    _, entries = self._store.read(self._session_id)
                    await repo.touch_after_append(
                        self._session_id,
                        leaf_uuid=entries[-1].uuid if entries else None,
                        entry_count=len(entries),
                    )
                await repo.finish_run(self._session_id)
        logger.info(
            "Agent 会话运行收尾完成 session=%s status=%s 补配对=%d",
            self._session_id,
            event.type,
            sealed,
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                async with get_database().session() as session:
                    await AgentSessionRepository(session).heartbeat(self._session_id)
            except Exception:  # noqa: BLE001 - 单拍心跳失败不终止循环
                logger.warning("会话心跳刷新失败 session=%s", self._session_id)


async def rebuild_agent_session_index() -> None:
    """启动自愈：先补上次异常停机的孤儿工具调用，再把索引校准到与文件一致。

    补配对（seal）必须在索引重建**之前**：seal 会追加 entry，先重建的话
    索引里的 entry_count / leaf 立即过期。覆盖三种失步：进程在「写文件、
    更 DB」之间崩溃（索引落后）、用户手工增删转录文件、索引库整个丢失。
    文件是事实源，扫描结果单向覆盖索引。
    失败只告警不阻断启动——索引暂时不准不影响会话内容安全。
    """
    from movieclaw_api.services.agent_sessions import get_agent_session_store

    try:
        _seal_interrupted_sessions(get_agent_session_store())
    except Exception:  # noqa: BLE001 - 自愈路径自身出错不能拖垮应用启动
        logger.exception("启动自愈补配对失败（不影响启动，接受路径仍有兜底）")
    try:
        summaries = get_agent_session_store().scan_all()
        async with get_database().session() as session:
            changed = await AgentSessionRepository(session).rebuild(
                [
                    (
                        s.session_id,
                        s.created_at,
                        s.entry_count,
                        s.leaf_uuid,
                        s.title,
                        s.last_prompt,
                    )
                    for s in summaries
                ]
            )
        if changed:
            logger.info("Agent 会话索引重建完成：校准了 %d 行", changed)
    except Exception:  # noqa: BLE001 - 自愈路径自身出错不能拖垮应用启动
        logger.exception("Agent 会话索引重建失败（不影响启动，会话文件仍完好）")


def _seal_interrupted_sessions(store: AgentSessionStore) -> None:
    """对上次异常停机遗留孤儿工具调用的会话补配对（幂等，正常时命中恒为 0）。

    优雅停机/用户取消在运行收尾里已经补过；这里只兜 kill -9 / 断电这类
    没有收尾机会的死法——不补的话，孤儿 tool_call 会让该会话此后每次
    发消息都被供应商以 400 拒绝（docs/design/agent-runtime-resilience.md
    缺口 A）。日志记明会话与条数，排查上次停机原因时不用翻转录。
    """
    if not store.root.is_dir():
        return
    for path in sorted(store.root.glob("*.jsonl")):
        session_id = path.stem
        try:
            sealed = store.seal_pending_tool_calls(session_id, reason="service_interrupted")
        except Exception:  # noqa: BLE001 - 单个坏文件不阻断其它会话自愈
            logger.exception("会话补配对自愈失败，已跳过 session=%s", session_id)
            continue
        if sealed:
            logger.warning(
                "上次停机时会话 %s 有 %d 个工具调用在执行中被中断，已补写回执"
                "（模型将被引导先核实这些操作的实际结果）",
                session_id,
                sealed,
            )
