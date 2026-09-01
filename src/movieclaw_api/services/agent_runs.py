"""进程内 Agent 运行注册表：后台执行、事件回放与广播订阅。

这里刻意不用 ``asyncio.Queue``：队列中的一条消息只会被一个消费者取走，
无法同时满足多个 SSE 订阅者，也无法让断线客户端回放历史。本模块为每次运行
维护一份只追加的事件日志，订阅者各自持有序号游标；``asyncio.Condition`` 只
负责在新事件到达时唤醒等待者，消息本身始终以日志为准。

注册表仅在单个 API 进程内有效。浏览器或 SSE 连接断开不会影响后台任务，但
进程重启会丢失所有运行；若未来支持多 worker，应把这一层替换为 Redis 等共享
存储，而不是改变 AgentRunner 或 SSE 事件协议。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from movieclaw_agent import AgentEvent, AgentRunner, AgentStartParams
from movieclaw_api.exceptions import BadRequestException, NotFoundException

logger = logging.getLogger("movieclaw_api.agent_runs")

TERMINAL_EVENT_TYPES = {"agent_done", "agent_error", "agent_cancelled"}
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
#: 停机时等待全部运行收尾的总超时（秒）；超时强制放行，交给启动自愈
CLOSE_TIMEOUT_SECONDS = 10
#: 取消看门狗：首次 cancel 后任务仍未停下时，每隔该秒数再投递一次取消。
#: 实测中偶发首次 CancelledError 被吞（深层 await 的取消竞态，如
#: wait_for/子进程管道；也防御第三方工具代码捕获后不抛），复投一次即可
#: 解卡——没有它，卡住的运行要等 30 秒心跳超时才对外显示结束，收尾
#: （补配对、清运行标记）则一直悬着。
CANCEL_RETRY_SECONDS = 3
CANCEL_RETRY_MAX = 3


@dataclass(frozen=True, slots=True)
class StoredAgentEvent:
    """带运行内递增序号的事件；序号直接用作 SSE ``id``。"""

    sequence: int
    event: AgentEvent


@dataclass(slots=True)
class _AgentRun:
    """一次运行的全部进程内状态，由 AgentRunRegistry 在同一事件循环中保护。"""

    run_id: str
    session_id: str
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    events: list[StoredAgentEvent] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    terminal: bool = False
    completed_at: float | None = None
    #: 应用停机触发的取消：终态钩子据此把补配对文案从「用户停止」切换为
    #: 「服务中断」（docs/design/agent-runtime-resilience.md §4.3）
    shutdown: bool = False
    #: 运行协程已停笔、进入持久化收尾（_finalize）。取消看门狗见此标志即
    #: 停止复投——看门狗的职责是解卡「吞掉取消的 runner」，合法的慢收尾
    #: （杀进程组、大转录 seal、DB 提交）不该被自家看门狗再补一刀
    finalizing: bool = False
    #: 终态钩子：**运行协程真正结束后**（_execute 的 finally）调用一次，
    #: 供会话持久化做收尾（停心跳、补配对、清运行标记）。刻意不挂在
    #: 「第一个终态事件」上：取消时事件先落、协程可能还在写盘，此刻收尾
    #: 会与 runner 的落盘竞态产生重复回执（§3 缺口 B）。
    on_terminal: Callable[[AgentEvent, str], Awaitable[None]] | None = None


class AgentRunRegistry:
    """管理后台 Agent 任务，并为每次运行提供可回放的广播事件日志。

    关键保证：
    1. 创建接口持有 task 强引用，因此 HTTP/SSE 请求结束不会取消 Agent；
    2. 发布时先追加日志、再唤醒全部订阅者，重连不会漏掉通知窗口内的事件；
    3. 每个订阅者按自己的 sequence 读取，慢客户端不会阻塞生产者；
    4. 所有退出路径都补齐终态事件，SSE 不会无限等待一个已消失的任务。
    """

    def __init__(
        self,
        *,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retention_seconds = retention_seconds
        self._clock = clock
        self._runs: dict[str, _AgentRun] = {}
        # 公开接口只接受 session_id；映射仅用于把会话动作解析到内部运行。
        self._latest_by_session: dict[str, str] = {}
        self._closing = False
        # 取消看门狗任务的强引用（fire-and-forget task 无引用会被 GC 掉）
        self._watchdogs: set[asyncio.Task[None]] = set()

    def start(
        self,
        runner: AgentRunner,
        params: AgentStartParams,
        *,
        session_id: str,
        on_terminal: Callable[[AgentEvent, str], Awaitable[None]] | None = None,
    ) -> str:
        """分配运行编号并把 runner 放入后台执行，立即返回编号。"""
        if self._closing:
            raise RuntimeError("Agent 运行注册表正在关闭，无法创建新运行")
        self._prune_expired()
        run_id = uuid.uuid4().hex[:12]
        run = _AgentRun(run_id=run_id, session_id=session_id, on_terminal=on_terminal)
        self._runs[run_id] = run
        self._latest_by_session[session_id] = run_id
        run.task = asyncio.create_task(
            self._execute(run, runner, params),
            name=f"agent-run-{run_id}",
        )
        logger.info("Agent 后台运行已创建 session=%s run=%s", session_id, run_id)
        return run_id

    async def get_session_events(
        self,
        session_id: str,
        after_sequence: int,
        *,
        timeout_seconds: float,
    ) -> tuple[list[StoredAgentEvent], bool]:
        """按公开会话编号读取当前（或最近一轮）事件。"""
        return await self.get_events(
            self._get_session_run(session_id).run_id,
            after_sequence,
            timeout_seconds=timeout_seconds,
        )

    async def get_events(
        self,
        run_id: str,
        after_sequence: int,
        *,
        timeout_seconds: float,
    ) -> tuple[list[StoredAgentEvent], bool]:
        """返回游标后的事件；暂无事件时等待通知，超时返回空列表供 SSE 发心跳。

        第二个返回值表示运行是否已进入终态。调用方应先发送本批事件，再在
        ``terminal=True`` 且批次已追平时关闭连接。
        """
        if after_sequence < 0:
            raise BadRequestException("SSE 事件游标不能为负数")
        run = self._get_run(run_id)
        async with run.condition:
            if after_sequence > len(run.events):
                raise BadRequestException(f"SSE 事件游标 {after_sequence} 超出当前事件范围")
            if after_sequence == len(run.events) and not run.terminal:
                try:
                    await asyncio.wait_for(
                        run.condition.wait_for(
                            lambda: after_sequence < len(run.events) or run.terminal
                        ),
                        timeout=timeout_seconds,
                    )
                except TimeoutError:
                    return [], False
            return list(run.events[after_sequence:]), run.terminal

    async def cancel(self, run_id: str) -> None:
        """幂等取消一次运行，并在取消 task 前先落下可回放的终态事件。

        先写事件很重要：创建接口刚返回、后台协程尚未获得调度时，直接
        ``task.cancel()`` 可能让协程一次都不执行，因而没有机会在 except 中
        补 ``agent_cancelled``，订阅者会永久等待。
        """
        run = self._get_run(run_id)
        if run.terminal:
            return
        await self._publish(
            run,
            AgentEvent(type="agent_cancelled", run_id=run.run_id),
        )
        if run.task is not None and not run.task.done():
            logger.info("用户请求取消 Agent 运行 run=%s", run_id)
            run.task.cancel()
            self._spawn_cancel_watchdog(run)

    async def cancel_session(self, session_id: str) -> None:
        """按公开会话编号幂等取消当前一轮。"""
        await self.cancel(self._get_session_run(session_id).run_id)

    async def close(self) -> None:
        """应用关闭时取消并等待全部活动任务（含各自的持久化收尾）。

        每个任务在自己的 finally 里补配对、清运行标记，等它们完成即保证
        优雅停机后转录配对完整、状态干净。总超时兜底：单个收尾卡死（如
        文件系统 hang）不能拖死整个停机——超时强制放行，遗留状态交给
        下次启动自愈（seal + 心跳超时），并记错误日志供排查。
        """
        self._closing = True
        pending = [run for run in self._runs.values() if run.task and not run.task.done()]
        for run in pending:
            run.shutdown = True  # 收尾文案按「服务中断」走，而非「用户停止」
            assert run.task is not None
            run.task.cancel()
        if pending:
            done, not_done = await asyncio.wait(
                [run.task for run in pending if run.task is not None],
                timeout=CLOSE_TIMEOUT_SECONDS,
            )
            if not_done:
                logger.error(
                    "停机时 %d 个 Agent 运行的收尾在 %d 秒内未完成，已强制放行"
                    "（转录配对与运行标记将由下次启动自愈修复）",
                    len(not_done),
                    CLOSE_TIMEOUT_SECONDS,
                )
        for watchdog in self._watchdogs:
            watchdog.cancel()
        self._watchdogs.clear()
        self._runs.clear()
        self._latest_by_session.clear()
        logger.info("Agent 运行注册表已关闭，活动任务均已回收")

    async def _execute(
        self,
        run: _AgentRun,
        runner: AgentRunner,
        params: AgentStartParams,
    ) -> None:
        """消费 runner 事件并写入日志，兜住取消、异常和异常断流三种出口。

        持久化收尾（补配对、清运行标记）在 finally 里由协程自己执行——
        此刻 runner 必然不再落盘，收尾读到的转录就是最终形态，不存在
        「seal 与真实回执同 id 并存」的竞态。取消路径的终态事件仍由
        cancel()/close() 先行写入（防 task 从未调度导致订阅者永久等待），
        这里只负责收尾，不重复发事件。
        """
        try:
            async for event in runner.start(params, run_id=run.run_id):
                await self._publish(run, event)
            if not run.terminal:
                await self._publish(
                    run,
                    AgentEvent(
                        type="agent_error",
                        run_id=run.run_id,
                        error="Agent 运行异常结束，未返回终态事件",
                    ),
                )
        except asyncio.CancelledError:
            if not run.terminal:
                await self._publish(
                    run,
                    AgentEvent(type="agent_cancelled", run_id=run.run_id),
                )
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务必须转成可见终态，不能静默消失
            logger.exception("Agent 后台运行发生未知错误 run=%s", run.run_id)
            if not run.terminal:
                await self._publish(
                    run,
                    AgentEvent(
                        type="agent_error",
                        run_id=run.run_id,
                        error=f"Agent 运行发生未知错误：{exc}",
                    ),
                )
        finally:
            run.finalizing = True
            run.task = None
            await self._finalize(run)

    def _spawn_cancel_watchdog(self, run: _AgentRun) -> None:
        """派出后台看门狗盯着被取消的任务真正停下（强引用挂在注册表上防 GC）。"""
        watchdog = asyncio.create_task(
            self._watch_cancelled(run),
            name=f"agent-cancel-watchdog-{run.run_id}",
        )
        self._watchdogs.add(watchdog)
        watchdog.add_done_callback(self._watchdogs.discard)

    async def _watch_cancelled(self, run: _AgentRun) -> None:
        """首次取消后若 runner 迟迟不停笔，按间隔复投取消，超过上限记错误日志。

        只盯「runner 是否停笔」（finalizing 标志），不盯任务整体结束：收尾
        （_finalize）本身可能合法地超过复投间隔，被复投的取消打断反而会让
        finish_run 被跳过、会话多挂 30 秒心跳窗。
        """
        task = run.task
        if task is None:
            return
        for _ in range(CANCEL_RETRY_MAX):
            done, _pending = await asyncio.wait([task], timeout=CANCEL_RETRY_SECONDS)
            if done or run.finalizing:
                return
            logger.warning(
                "Agent 运行取消后 %d 秒仍未停下，再次投递取消 run=%s",
                CANCEL_RETRY_SECONDS,
                run.run_id,
            )
            task.cancel()
        done, _pending = await asyncio.wait([task], timeout=CANCEL_RETRY_SECONDS)
        if not done and not run.finalizing:
            logger.error(
                "Agent 运行多次取消后仍未停下，放弃等待（收尾将由停机/启动自愈兜底）run=%s",
                run.run_id,
            )

    async def _finalize(self, run: _AgentRun) -> None:
        """运行协程末尾的持久化收尾；被取消的任务里 await 仍会执行完。

        再次被取消（停机窗口内二次 cancel）时也不放弃：收尾失败只记日志，
        遗留状态由启动自愈兜底。
        """
        if run.on_terminal is None:
            return
        terminal_event = next(
            (s.event for s in reversed(run.events) if s.event.type in TERMINAL_EVENT_TYPES),
            AgentEvent(type="agent_error", run_id=run.run_id, error="运行未留下终态事件"),
        )
        # 补配对文案：停机取消 → 服务中断；用户取消 → 用户停止；
        # 其余终态（done 无孤儿 / error 断流）按「服务中断」的中性文案兜底
        reason = (
            "user_cancelled"
            if terminal_event.type == "agent_cancelled" and not run.shutdown
            else "service_interrupted"
        )
        try:
            await run.on_terminal(terminal_event, reason)
        except asyncio.CancelledError:
            logger.error("Agent 运行收尾期间再次被取消，遗留状态待启动自愈 run=%s", run.run_id)
        except Exception:  # noqa: BLE001 - 收尾失败不能影响事件流的终态语义
            logger.exception("Agent 运行终态钩子执行失败 run=%s", run.run_id)

    async def _publish(self, run: _AgentRun, event: AgentEvent) -> None:
        """原子追加事件并广播通知；终态之后的迟到事件直接忽略。"""
        async with run.condition:
            if run.terminal:
                return
            run.events.append(StoredAgentEvent(sequence=len(run.events) + 1, event=event))
            if event.type in TERMINAL_EVENT_TYPES:
                run.terminal = True
                run.completed_at = self._clock()
                logger.info("Agent 后台运行已结束 run=%s status=%s", run.run_id, event.type)
            run.condition.notify_all()

    def _get_run(self, run_id: str) -> _AgentRun:
        self._prune_expired()
        run = self._runs.get(run_id)
        if run is None:
            raise NotFoundException("Agent 运行不存在或事件历史已过期")
        return run

    def _get_session_run(self, session_id: str) -> _AgentRun:
        """把公开会话编号解析为注册表内最近一轮运行。"""
        self._prune_expired()
        run_id = self._latest_by_session.get(session_id)
        run = self._runs.get(run_id) if run_id else None
        if run is None:
            raise NotFoundException("会话没有可跟随或停止的运行")
        return run

    def _prune_expired(self) -> None:
        """惰性清理超过保留期的终态运行；活动运行永不在这里删除。"""
        cutoff = self._clock() - self._retention_seconds
        expired = [
            run_id
            for run_id, run in self._runs.items()
            if run.completed_at is not None and run.completed_at <= cutoff
        ]
        for run_id in expired:
            run = self._runs.pop(run_id)
            if self._latest_by_session.get(run.session_id) == run_id:
                del self._latest_by_session[run.session_id]
        if expired:
            logger.info("已清理 %d 条过期 Agent 运行历史", len(expired))


_registry: AgentRunRegistry | None = None


def init_agent_run_registry() -> AgentRunRegistry:
    """为当前 FastAPI 生命周期初始化唯一的运行注册表。"""
    global _registry
    _registry = AgentRunRegistry()
    return _registry


def get_agent_run_registry() -> AgentRunRegistry:
    """取得已初始化的运行注册表；仅供 lifespan 启动后的请求使用。"""
    if _registry is None:
        raise RuntimeError("Agent 运行注册表尚未初始化")
    return _registry


async def close_agent_run_registry() -> None:
    """关闭并清空当前生命周期的注册表。"""
    global _registry
    if _registry is not None:
        await _registry.close()
        _registry = None
