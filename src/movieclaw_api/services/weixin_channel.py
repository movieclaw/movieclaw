"""微信通道服务编排:绑定注册表 + 通道管理器 + Agent 装配的粘合层。

职责边界:
- movieclaw_channel 只管「收发字节与事件调度」,不知道 LLM/工具/历史;
- 本服务把三者拼起来:绑定确认 → 落库 + 热启动;入站消息 → 装配受限
  工具集的 AgentRunner → StepReplyPusher → 出站队列。

安全红线(与设计讨论一致):微信侧 Agent 用**受限工具集**——只挂 mclaw
产品操作工具,不开 bash/read/write/edit 工作区工具。IM 是远程入口,
即使有白名单,也不该让它拿到宿主机 shell。

会话持久化:微信对话与 Web「最近会话」共用同一套 Agent 会话体系
(agent_session 索引 + JSONL 转录)。每个绑定账号持有一个「当前会话」
(channel_account.agent_session_id),首条消息时创建、/reset 后换新;
历史从转录重建,重启不丢,且在 Web 侧栏可见、可回放。

体验:收到消息立即回执「思考中💭」,随后开启微信「正在输入」状态
(getconfig 换 typing_ticket,sendtyping 每 5 秒保活),生成结束取消。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from movieclaw_agent import AgentRunner, AgentStartParams
from movieclaw_agent.events import AgentEvent
from movieclaw_agent.tools import make_mclaw_tool
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.agent_session_recorder import AgentSessionRecorder
from movieclaw_api.services.agent_sessions import get_agent_session_store
from movieclaw_api.services.llm_config import acquire_llm_router
from movieclaw_api.services.mclaw_tool import render_service_map
from movieclaw_channel import ChannelManager, InboundMessage
from movieclaw_channel.dispatcher import make_dispatcher
from movieclaw_channel.weixin import (
    BindingResult,
    WeixinAdapter,
    WeixinBindingRegistry,
    WeixinClient,
)
from movieclaw_channel.weixin.adapter import CHANNEL_ID
from movieclaw_db.engine import get_database
from movieclaw_db.models.channel_account import ChannelAccount, ChannelAccountStatus
from movieclaw_db.repositories.agent_session_repo import AgentSessionRepository
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository

logger = logging.getLogger("movieclaw_api.weixin_channel")

#: 微信侧 Agent 的步数上限:IM 对话不该跑出超长循环,收紧防失控
_WEIXIN_MAX_STEPS = 40
#: 收到消息后的即时回执文案(先让用户知道已收到,再慢慢生成)
_ACK_TEXT = "思考中💭"
#: 「正在输入」保活间隔(秒),对齐 openclaw 的 5s 续期
_TYPING_KEEPALIVE_S = 5.0
#: typing_ticket 缓存时长(秒):票据由服务端按用户发放,12 小时后重取
_TYPING_TICKET_TTL_S = 12 * 60 * 60


class WeixinChannelService:
    """进程级单例:随 lifespan 启停。"""

    def __init__(self) -> None:
        self.manager = ChannelManager()
        self.binding = WeixinBindingRegistry(
            on_confirmed=self._on_binding_confirmed,
            get_local_tokens=self._local_tokens,
        )
        #: 每账号一个持有连接池的 iLink 客户端,停账号时关闭
        self._clients: dict[str, WeixinClient] = {}
        #: "account:user" → (typing_ticket, 过期时刻);失败不缓存,下条消息重试
        self._typing_tickets: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动时拉起所有已绑定且未失效的账号。"""
        async with get_database().session() as session:
            rows = await ChannelAccountRepository(session).list_by_channel(CHANNEL_ID)
            accounts = [
                (row, ChannelAccountRepository.decrypted_token(row))
                for row in rows
                if row.status == ChannelAccountStatus.ACTIVE
            ]
        for row, token in accounts:
            try:
                await self._start_account(row, token)
            except Exception:
                logger.exception("微信账号启动失败 account=%s", row.account_id)
        if accounts:
            logger.info("微信通道已启动 %d 个账号", len(accounts))

    async def stop(self) -> None:
        await self.binding.close()
        await self.manager.close()
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------------
    # 账号装配
    # ------------------------------------------------------------------
    async def _start_account(self, row: ChannelAccount, token: str) -> None:
        account_id = row.account_id
        # 重新绑定热替换时,先关旧客户端
        old = self._clients.pop(account_id, None)
        if old is not None:
            await self.manager.stop_account(CHANNEL_ID, account_id)
            await old.aclose()

        client = WeixinClient(row.base_url, token)
        self._clients[account_id] = client
        bound_user = (row.bound_user_id or "").strip()
        adapter = WeixinAdapter(
            client,
            account_id,
            # 主动推送要靠会话令牌定位会话:只记绑定人的,重启后从库里读回
            bound_user_id=bound_user,
            initial_context_token=row.context_token or "",
            on_context_token=self._make_context_token_saver(account_id),
        )

        dispatcher = make_dispatcher(
            adapter,
            # P0 白名单 = 扫码人本人;bound_user 缺失(异常数据)时拒绝所有人
            is_allowed=lambda uid, _bound=bound_user: bool(_bound) and uid == _bound,
            run_agent=self._run_agent,
            reset_session=self._reset_session,
        )

        await self.manager.start_account(
            adapter,
            dispatcher,
            account_id=account_id,
            initial_cursor=row.cursor or "",
            save_cursor=self._make_cursor_saver(account_id),
            on_auth_error=self._on_auth_error,
        )

    @staticmethod
    def _make_cursor_saver(account_id: str) -> Callable[[str], Awaitable[None]]:
        async def save(cursor: str) -> None:
            async with get_database().session() as session:
                await ChannelAccountRepository(session).save_cursor(account_id, cursor)

        return save

    @staticmethod
    def _make_context_token_saver(account_id: str) -> Callable[[str], Awaitable[None]]:
        async def save(context_token: str) -> None:
            async with get_database().session() as session:
                await ChannelAccountRepository(session).save_context_token(
                    account_id, context_token
                )

        return save

    async def _on_auth_error(self, account_id: str) -> None:
        """凭据失效回调:落库标记 stale,并关闭该账号的连接池。

        绑定页据 stale 状态引导用户重新扫码;重新绑定成功后会构造新客户端。
        """
        async with get_database().session() as session:
            await ChannelAccountRepository(session).mark_stale(
                account_id, "微信 bot 凭据已失效,请重新扫码绑定"
            )
        client = self._clients.pop(account_id, None)
        if client is not None:
            await client.aclose()

    # ------------------------------------------------------------------
    # 绑定流程回调
    # ------------------------------------------------------------------
    async def _local_tokens(self) -> list[str]:
        """已绑定账号的 token 列表(服务端判断「是否已绑定过本实例」用)。"""
        async with get_database().session() as session:
            rows = await ChannelAccountRepository(session).list_by_channel(CHANNEL_ID)
            return [ChannelAccountRepository.decrypted_token(r) for r in rows]

    async def _on_binding_confirmed(self, result: BindingResult) -> None:
        """扫码确认:凭据落库 → 热启动收发循环(成功后前端才看到 confirmed)。"""
        async with get_database().session() as session:
            row = await ChannelAccountRepository(session).upsert(
                channel_id=CHANNEL_ID,
                account_id=result.bot_id,
                token=result.bot_token,
                base_url=result.base_url,
                bound_user_id=result.user_id or None,
            )
        await self._start_account(row, result.bot_token)

    # ------------------------------------------------------------------
    # 账号管理(API 层调用)
    # ------------------------------------------------------------------
    async def unbind(self, account_id: str) -> bool:
        """解绑:停收发循环、关客户端、删凭据行。

        历史 Agent 会话保留在「最近会话」里(只删通道凭据,不删对话记录)。
        """
        await self.manager.stop_account(CHANNEL_ID, account_id)
        client = self._clients.pop(account_id, None)
        if client is not None:
            await client.aclose()
        async with get_database().session() as session:
            return await ChannelAccountRepository(session).delete(account_id)

    # ------------------------------------------------------------------
    # 会话持久化(与 Web「最近会话」同一套体系)
    # ------------------------------------------------------------------
    async def _ensure_agent_session(self, msg: InboundMessage) -> str:
        """取该账号当前的 Agent 会话 id;缺失或已失效则新建并落库。"""
        store = get_agent_session_store()
        async with get_database().session() as session:
            account_repo = ChannelAccountRepository(session)
            row = await account_repo.get(msg.account_id)
            existing = (row.agent_session_id or "").strip() if row else ""
            if existing:
                # 索引行与转录文件都在才算有效;任一缺失(被手工删除等)则重建
                session_row = await AgentSessionRepository(session).get(existing)
                if session_row is not None and store.path(existing).exists():
                    return existing
                logger.warning(
                    "微信会话已失效,将新建 account=%s session=%s", msg.account_id, existing
                )

            header = store.create()
            await AgentSessionRepository(session).create(
                header.session_id, title=f"微信 · {msg.user_id}"
            )
            await account_repo.set_agent_session(msg.account_id, header.session_id)
            logger.info("微信会话已创建 account=%s session=%s", msg.account_id, header.session_id)
            return header.session_id

    async def _reset_session(self, session_key: str) -> None:
        """/reset:清空账号的当前会话指针,下条消息自动新建。

        旧会话及其转录完整保留在「最近会话」列表中,只是不再续写。
        """
        account_id = session_key.split(":", 2)[1]
        async with get_database().session() as session:
            await ChannelAccountRepository(session).set_agent_session(account_id, None)

    # ------------------------------------------------------------------
    # 「正在输入」状态
    # ------------------------------------------------------------------
    async def _get_typing_ticket(self, msg: InboundMessage) -> str:
        """取该用户的 typing_ticket(带 12h 缓存);失败返回空串并降级。"""
        key = f"{msg.account_id}:{msg.user_id}"
        cached = self._typing_tickets.get(key)
        now = time.monotonic()
        if cached is not None and now < cached[1]:
            return cached[0]
        client = self._clients.get(msg.account_id)
        if client is None:
            return ""
        try:
            ticket = await client.get_config(msg.user_id, msg.reply.token.get("context_token"))
        except Exception as exc:  # noqa: BLE001 -- typing 是锦上添花,失败只降级
            logger.warning("getconfig 失败,本轮无「正在输入」状态:%s", exc)
            return ""
        if ticket:
            self._typing_tickets[key] = (ticket, now + _TYPING_TICKET_TTL_S)
        return ticket

    async def _start_typing(self, msg: InboundMessage) -> Callable[[], Awaitable[None]]:
        """开启「正在输入」并每 5s 保活;返回停止函数(取消保活并发送 CANCEL)。

        任一环节失败都只记日志降级——typing 绝不能影响消息主链路。
        """
        ticket = await self._get_typing_ticket(msg)
        client = self._clients.get(msg.account_id)
        if not ticket or client is None:

            async def noop() -> None:
                return None

            return noop

        async def keepalive() -> None:
            while True:
                try:
                    await client.send_typing(msg.user_id, ticket, typing=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("sendtyping 保活失败(忽略):%s", exc)
                await asyncio.sleep(_TYPING_KEEPALIVE_S)

        task = asyncio.create_task(keepalive(), name=f"weixin-typing-{msg.user_id}")

        async def stop() -> None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            try:
                await client.send_typing(msg.user_id, ticket, typing=False)
            except Exception as exc:  # noqa: BLE001
                logger.debug("sendtyping 取消失败(忽略):%s", exc)

        return stop

    # ------------------------------------------------------------------
    # Agent 装配与执行(dispatcher 的 run_agent 回调)
    # ------------------------------------------------------------------
    async def _run_agent(self, msg: InboundMessage, emit: Callable[[str], Awaitable[None]]) -> None:
        from movieclaw_channel.pusher import StepReplyPusher

        # 1) 即时回执:先让用户知道消息已收到,再慢慢生成
        await emit(_ACK_TEXT)

        try:
            async with get_database().session() as session:
                llm_router = await acquire_llm_router(session)
        except NotFoundException as exc:
            await emit(f"⚠️ {exc.message}")
            return

        # 2) 会话持久化:历史从转录重建;定稿消息经 recorder 持续追加
        store = get_agent_session_store()
        session_id = await self._ensure_agent_session(msg)
        history = store.build_history(session_id)
        recorder = AgentSessionRecorder(store, session_id, entry_count=len(history))
        await recorder.record_user_message(msg.text)

        runner = AgentRunner(
            llm_router,
            tools=await self._restricted_tools(session_id),
            max_steps=_WEIXIN_MAX_STEPS,
            on_message=recorder.on_message,
            on_compaction=recorder.on_compaction,
        )
        run_id = uuid.uuid4().hex[:12]
        await recorder.begin(run_id)

        # 3) 「正在输入」状态:生成期间每 5s 保活,结束取消
        stop_typing = await self._start_typing(msg)
        pusher = StepReplyPusher(emit)
        last_event: AgentEvent | None = None
        try:
            async for ev in runner.start(
                AgentStartParams(input=msg.text, history=history), run_id=run_id
            ):
                last_event = ev
                await pusher.feed(ev)
        finally:
            await stop_typing()
            # 任务被取消（服务停机）时事件流没有终态事件,合成 cancelled 供收尾统一处理
            terminal = (
                last_event
                if last_event is not None
                and last_event.type in ("agent_done", "agent_error", "agent_cancelled")
                else AgentEvent(type="agent_cancelled", run_id=run_id)
            )
            # 微信通道没有用户主动停止入口，取消只可能来自停机——按「服务中断」收尾
            await recorder.on_terminal(terminal, reason="service_interrupted")

    async def _restricted_tools(self, session_id: str):
        """微信侧的受限工具集:仅 mclaw 产品操作工具,不开工作区 shell。"""
        settings = get_settings()
        workdir = Path(settings.agent_workspace_dir).resolve() / "weixin"
        workdir.mkdir(parents=True, exist_ok=True)
        token = await auth_service.issue_agent_token(session_id)
        cli_env = {
            "MOVIECLAW_SERVER": f"http://127.0.0.1:{settings.port}",
            "MOVIECLAW_TOKEN": token,
        }
        return [make_mclaw_tool(workdir, cli_env, render_service_map())]


# ---------------------------------------------------------------------------
# 进程级单例(与 agent_runs 同款 init/get/close 约定)
# ---------------------------------------------------------------------------

_service: WeixinChannelService | None = None


async def init_weixin_channel() -> WeixinChannelService:
    """lifespan 启动时调用:创建单例并拉起已绑定账号。"""
    global _service
    _service = WeixinChannelService()
    await _service.start()
    return _service


def get_weixin_channel() -> WeixinChannelService:
    if _service is None:
        raise RuntimeError("微信通道服务尚未初始化")
    return _service


async def close_weixin_channel() -> None:
    global _service
    if _service is not None:
        await _service.stop()
        _service = None
