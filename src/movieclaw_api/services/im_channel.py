"""IM 通道服务编排(Telegram / Discord 共用):配对码绑定 + Agent 装配 + 主动推送。

与微信服务(weixin_channel.py)的分工:微信有扫码/配对码/iLink 网关等
重量级绑定状态机,自成一体;TG/Discord 的绑定简单得多——面板填 bot token,
服务端生成 6 位配对码,用户私聊 bot 发码即完成绑定(谁发码谁是白名单)。
两个平台的差异全部收敛在「通道规格」(_ChannelSpec)里,服务本体只写一份。

Agent 装配与微信同款红线:受限工具集(仅 mclaw 产品操作),不开宿主 shell;
会话与 Web「最近会话」共用 agent_session 体系,/reset 换新会话。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
from movieclaw_channel.adapter import ChannelAdapter
from movieclaw_channel.discord import CHANNEL_ID as DISCORD_CHANNEL_ID
from movieclaw_channel.discord import DiscordAdapter, DiscordClient
from movieclaw_channel.dispatcher import make_dispatcher
from movieclaw_channel.telegram import CHANNEL_ID as TELEGRAM_CHANNEL_ID
from movieclaw_channel.telegram import TelegramAdapter, TelegramClient
from movieclaw_channel.types import OutboundEnvelope, ReplyContext
from movieclaw_db.engine import get_database
from movieclaw_db.models.channel_account import ChannelAccount, ChannelAccountStatus
from movieclaw_db.repositories.agent_session_repo import AgentSessionRepository
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository

logger = logging.getLogger("movieclaw_api.im_channel")

ImChannelId = Literal["telegram", "discord"]
IM_CHANNEL_IDS: tuple[ImChannelId, ...] = (TELEGRAM_CHANNEL_ID, DISCORD_CHANNEL_ID)

#: IM 侧 Agent 步数上限(与微信同口径)
_IM_MAX_STEPS = 40
_ACK_TEXT = "思考中💭"
#: 配对码有效期
_PAIR_TTL_S = 10 * 60
#: 配对码最大尝试次数:6 位数字码 + 配对期对所有人放行,不设上限就能被暴力猜码
_PAIR_MAX_ATTEMPTS = 5
#: 终态 challenge 在内存里的留存期(前端轮询还要能读到终态,不能立即删)
_CHALLENGE_LINGER_S = 10 * 60


# ---------------------------------------------------------------------------
# 通道规格:平台差异的唯一落点
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ChannelSpec:
    channel_id: str
    display_name: str
    #: token → (客户端, 关闭函数)
    make_client: Callable[[str], Any]
    make_adapter: Callable[[Any, str], ChannelAdapter]
    #: 校验 token 并返回 (bot 账号 id, bot 展示名)
    validate: Callable[[Any], Awaitable[tuple[str, str]]]


async def _tg_validate(client: TelegramClient) -> tuple[str, str]:
    me = await client.get_me()
    return str(me["id"]), str(me.get("username") or me.get("first_name") or me["id"])


async def _dc_validate(client: DiscordClient) -> tuple[str, str]:
    me = await client.get_me()
    return str(me["id"]), str(me.get("username") or me["id"])


_SPECS: dict[str, _ChannelSpec] = {
    TELEGRAM_CHANNEL_ID: _ChannelSpec(
        channel_id=TELEGRAM_CHANNEL_ID,
        display_name="Telegram",
        make_client=TelegramClient,
        make_adapter=TelegramAdapter,
        validate=_tg_validate,
    ),
    DISCORD_CHANNEL_ID: _ChannelSpec(
        channel_id=DISCORD_CHANNEL_ID,
        display_name="Discord",
        make_client=DiscordClient,
        make_adapter=DiscordAdapter,
        validate=_dc_validate,
    ),
}


# ---------------------------------------------------------------------------
# 配对码绑定
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PairChallenge:
    """一次进行中的配对绑定。

    生命周期:begin(token 校验通过)→ 临时账号已在收消息,等用户私聊 bot
    发配对码 → 命中后落库转正、绑定发码人为白名单 → confirmed。
    过期(10 分钟)自动停临时账号。
    """

    challenge_id: str
    channel_id: str
    pair_code: str
    bot_id: str
    bot_name: str
    status: Literal["pending", "confirmed", "expired", "failed"] = "pending"
    message: str = ""
    #: 已发生的错误配对码次数,达到 _PAIR_MAX_ATTEMPTS 即作废本次绑定
    attempts: int = 0
    expires_at: float = field(default_factory=lambda: time.monotonic() + _PAIR_TTL_S)


class ImChannelService:
    """进程级单例:Telegram 与 Discord 两个通道的账号管理。"""

    def __init__(self) -> None:
        self.manager = ChannelManager()
        self._clients: dict[str, Any] = {}  # "channel:account_id" → client
        self._challenges: dict[str, PairChallenge] = {}
        #: 配对期临时凭据:challenge_id → token(确认后才落库)
        self._pending_tokens: dict[str, str] = {}
        #: 配对期临时客户端:challenge_id → client(超时清理时按身份比对,
        #: 避免误停已被确认流程/新一轮绑定接管的账号)
        self._pairing_clients: dict[str, Any] = {}
        #: 主动推送地址簿:"channel:account_id" → ReplyContext(绑定用户)
        self._push_targets: dict[str, ReplyContext] = {}
        #: 后台任务(配对超时守护/绑定热切换):持有引用防 GC,异常统一落日志
        self._bg_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        async with get_database().session() as session:
            repo = ChannelAccountRepository(session)
            rows: list[tuple[ChannelAccount, str]] = []
            for channel_id in IM_CHANNEL_IDS:
                for row in await repo.list_by_channel(channel_id):
                    if row.status != ChannelAccountStatus.ACTIVE:
                        continue
                    try:
                        rows.append((row, ChannelAccountRepository.decrypted_token(row)))
                    except Exception:
                        # 单条坏记录(如密钥更换后的旧凭据)不能拖垮整个应用启动
                        logger.exception(
                            "IM 账号凭据解密失败,已跳过 channel=%s account=%s",
                            row.channel_id,
                            row.account_id,
                        )
        for row, token in rows:
            try:
                await self._start_account(row, token)
            except Exception:
                logger.exception(
                    "IM 账号启动失败 channel=%s account=%s", row.channel_id, row.account_id
                )
        if rows:
            logger.info("IM 通道已启动 %d 个账号", len(rows))

    async def stop(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        for task in list(self._bg_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._bg_tasks.clear()
        await self.manager.close()
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        self._pairing_clients.clear()
        self._push_targets.clear()

    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        """起后台任务并持有引用:防 GC 提前回收,异常落日志而不是无声丢失。"""
        task = asyncio.get_running_loop().create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("%s 后台任务失败", what, exc_info=t.exception())

        task.add_done_callback(_done)

    # ------------------------------------------------------------------
    # 账号装配
    # ------------------------------------------------------------------
    def _key(self, channel_id: str, account_id: str) -> str:
        return f"{channel_id}:{account_id}"

    async def _start_account(self, row: ChannelAccount, token: str) -> None:
        spec = _SPECS[row.channel_id]
        key = self._key(row.channel_id, row.account_id)
        old = self._clients.pop(key, None)
        if old is not None:
            await self.manager.stop_account(row.channel_id, row.account_id)
            await old.aclose()

        client = spec.make_client(token)
        self._clients[key] = client
        adapter = spec.make_adapter(client, row.account_id)

        bound_user = (row.bound_user_id or "").strip()
        if bound_user:
            self._push_targets[key] = ReplyContext(
                channel_id=row.channel_id,
                account_id=row.account_id,
                user_id=bound_user,
            )
        dispatcher = make_dispatcher(
            adapter,
            # 白名单 = 完成配对的用户本人;缺失(异常数据)时拒绝所有人
            is_allowed=lambda uid, _bound=bound_user: bool(_bound) and uid == _bound,
            run_agent=self._run_agent,
            reset_session=self._reset_session,
        )
        await self.manager.start_account(
            adapter,
            dispatcher,
            account_id=row.account_id,
            initial_cursor=row.cursor or "",
            save_cursor=self._make_cursor_saver(row.account_id),
            on_auth_error=self._make_auth_error_handler(row.channel_id),
        )

    @staticmethod
    def _make_cursor_saver(account_id: str) -> Callable[[str], Awaitable[None]]:
        async def save(cursor: str) -> None:
            async with get_database().session() as session:
                await ChannelAccountRepository(session).save_cursor(account_id, cursor)

        return save

    def _make_auth_error_handler(self, channel_id: str) -> Callable[[str], Awaitable[None]]:
        async def on_auth_error(account_id: str) -> None:
            async with get_database().session() as session:
                await ChannelAccountRepository(session).mark_stale(
                    account_id, f"{_SPECS[channel_id].display_name} bot 凭据已失效,请重新绑定"
                )
            key = self._key(channel_id, account_id)
            self._push_targets.pop(key, None)
            client = self._clients.pop(key, None)
            if client is not None:
                await client.aclose()

        return on_auth_error

    # ------------------------------------------------------------------
    # 配对码绑定流程
    # ------------------------------------------------------------------
    async def begin_binding(self, channel_id: ImChannelId, token: str) -> PairChallenge:
        """校验 token → 生成配对码 → 起临时账号等待用户发码。"""
        spec = _SPECS[channel_id]
        client = spec.make_client(token)
        try:
            bot_id, bot_name = await spec.validate(client)
        except Exception as exc:
            await client.aclose()
            raise ValueError(f"{spec.display_name} bot token 校验失败:{exc}") from exc

        pair_code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = PairChallenge(
            challenge_id=uuid.uuid4().hex[:16],
            channel_id=channel_id,
            pair_code=pair_code,
            bot_id=bot_id,
            bot_name=bot_name,
            message=f"请在 {spec.display_name} 上私聊 @{bot_name} 发送配对码 {pair_code}",
        )
        self._challenges[challenge.challenge_id] = challenge
        self._pending_tokens[challenge.challenge_id] = token

        # 配对期临时收发:任何人发来的消息只跟配对码比对,不进 Agent。
        # 命中即落库转正、以正式白名单重启账号。
        adapter = spec.make_adapter(client, bot_id)
        key = self._key(channel_id, bot_id)
        old = self._clients.pop(key, None)
        if old is not None:
            await self.manager.stop_account(channel_id, bot_id)
            await old.aclose()
        self._clients[key] = client
        self._pairing_clients[challenge.challenge_id] = client

        async def pair_agent(
            msg: InboundMessage, emit: Callable[[str], Awaitable[None]]
        ) -> None:
            ch = self._challenges.get(challenge.challenge_id)
            if ch is None or ch.status != "pending":
                return
            if time.monotonic() > ch.expires_at:
                ch.status = "expired"
                ch.message = "配对码已过期,请重新发起绑定"
                return
            if msg.text.strip() != ch.pair_code:
                ch.attempts += 1
                if ch.attempts >= _PAIR_MAX_ATTEMPTS:
                    ch.status = "failed"
                    ch.message = "配对码错误次数过多,绑定已作废,请重新发起"
                    # 终态回执直接同步发送(不走出站队列):随后的清理任务会
                    # 停账号并关闭发送泵,而 close 不排空队列,入队的回执会丢
                    await self._send_receipt(
                        adapter, msg, "配对码错误次数过多,本次绑定已作废。请回面板重新发起绑定。"
                    )
                    # 当前正在该账号的会话 worker 里,停账号须走后台任务避免自锁
                    self._spawn(self._teardown_pairing(ch), "配对作废清理")
                    return
                await emit("配对码不正确。请发送面板上显示的 6 位数字完成绑定。")
                return
            await self._confirm_binding(ch, msg, adapter)

        dispatcher = make_dispatcher(adapter, is_allowed=lambda _uid: True, run_agent=pair_agent)
        await self.manager.start_account(
            adapter,
            dispatcher,
            account_id=bot_id,
            initial_cursor="",
            save_cursor=self._make_cursor_saver(bot_id),
            on_auth_error=self._make_auth_error_handler(channel_id),
        )
        self._spawn(self._challenge_watchdog(challenge), "配对超时守护")
        return challenge

    async def _challenge_watchdog(self, challenge: PairChallenge) -> None:
        """配对超时守护:到期停临时账号(必要时恢复原正式账号),留存期后清内存。

        无论过期由谁先发现(本任务/轮询/入站消息),清理都收口在这里执行一次;
        已确认或被新一轮绑定接管的场景由 ``_teardown_pairing`` 的身份比对天然跳过。
        """
        await asyncio.sleep(max(0.0, challenge.expires_at - time.monotonic()))
        if challenge.status == "pending":
            challenge.status = "expired"
            challenge.message = "配对码已过期,请重新发起绑定"
        await self._teardown_pairing(challenge)
        # 终态留存一段时间供前端轮询读取,然后清内存(否则 challenge 只增不减)
        await asyncio.sleep(_CHALLENGE_LINGER_S)
        self._challenges.pop(challenge.challenge_id, None)

    async def _teardown_pairing(self, challenge: PairChallenge) -> None:
        """停掉配对期临时账号;若该 bot 之前有正式绑定,恢复其运行。

        幂等:确认流程或新一轮绑定已接管该 bot(client 被替换)时不做任何事。
        不恢复的话,对已激活 bot 重新发起绑定又中途放弃,原来的对话链路会
        一直停留在「只认配对码」的临时模式,直到进程重启。
        """
        self._pending_tokens.pop(challenge.challenge_id, None)
        pairing_client = self._pairing_clients.pop(challenge.challenge_id, None)
        key = self._key(challenge.channel_id, challenge.bot_id)
        if pairing_client is None or self._clients.get(key) is not pairing_client:
            return
        await self.manager.stop_account(challenge.channel_id, challenge.bot_id)
        self._clients.pop(key, None)
        await pairing_client.aclose()

        async with get_database().session() as session:
            row = await ChannelAccountRepository(session).get(challenge.bot_id)
            token = ""
            if row is not None and row.channel_id == challenge.channel_id:
                if row.status == ChannelAccountStatus.ACTIVE:
                    try:
                        token = ChannelAccountRepository.decrypted_token(row)
                    except Exception:
                        logger.exception("恢复原绑定失败:凭据解密错误 account=%s", row.account_id)
            else:
                row = None
        if row is not None and token:
            try:
                await self._start_account(row, token)
                logger.info(
                    "配对未完成,已恢复原正式账号 channel=%s bot=%s",
                    challenge.channel_id,
                    challenge.bot_id,
                )
            except Exception:
                logger.exception(
                    "配对未完成后恢复原账号失败 channel=%s account=%s",
                    challenge.channel_id,
                    challenge.bot_id,
                )

    @staticmethod
    async def _send_receipt(adapter: ChannelAdapter, msg: InboundMessage, text: str) -> None:
        """配对终态回执:绕过出站队列直接发送。

        终态之后紧跟账号热切换/清理,``dispatcher.close()`` 会取消发送泵且
        不排空队列,走队列的回执大概率丢失,所以必须在触发切换前同步发出;
        发送失败只记日志,绝不能影响绑定状态流转。
        """
        try:
            await adapter.send_text(msg.reply, text)
        except Exception:  # noqa: BLE001
            logger.exception("配对回执发送失败(不影响绑定结果)user=%s", msg.user_id)

    async def _confirm_binding(
        self, challenge: PairChallenge, msg: InboundMessage, adapter: ChannelAdapter
    ) -> None:
        """配对码命中:凭据落库、发码人即白名单,重启为正式账号。"""
        token = self._pending_tokens.pop(challenge.challenge_id, "")
        if not token:
            challenge.status = "failed"
            challenge.message = "绑定状态已丢失,请重新发起"
            return
        async with get_database().session() as session:
            row = await ChannelAccountRepository(session).upsert(
                channel_id=challenge.channel_id,
                account_id=challenge.bot_id,
                token=token,
                base_url="",
                bound_user_id=msg.user_id,
            )
        challenge.status = "confirmed"
        challenge.message = "绑定完成,通道已启动"
        logger.info(
            "IM 绑定完成 channel=%s bot=%s user=%s",
            challenge.channel_id,
            challenge.bot_id,
            msg.user_id,
        )
        # 成功回执必须在触发热切换之前直接发出:热切换会停掉当前临时账号
        # 并关闭其发送泵,出站队列里未发送的消息会被丢弃
        await self._send_receipt(
            adapter, msg, "绑定成功!我是 movieclaw 助手,现在可以直接发消息让我干活了。"
        )
        # 配对客户端交由 _start_account 热替换,超时守护不再需要处理它
        self._pairing_clients.pop(challenge.challenge_id, None)
        # 从配对模式切到正式模式(热替换;当前正在配对回调里,起后台任务避免自锁)
        self._spawn(self._start_account(row, token), "绑定确认后的通道热切换")

    def get_challenge(self, challenge_id: str) -> PairChallenge | None:
        ch = self._challenges.get(challenge_id)
        if ch is not None and ch.status == "pending" and time.monotonic() > ch.expires_at:
            # 只翻状态供前端展示;临时账号的停止与恢复统一由超时守护任务执行
            ch.status = "expired"
            ch.message = "配对码已过期,请重新发起绑定"
        return ch

    # ------------------------------------------------------------------
    # 账号管理
    # ------------------------------------------------------------------
    async def unbind(self, channel_id: str, account_id: str) -> bool:
        async with get_database().session() as session:
            row = await ChannelAccountRepository(session).get(account_id)
        # 只删本通道的账号:防止经 im 路由误删其他通道(如微信)的绑定
        if row is None or row.channel_id != channel_id:
            return False
        await self.manager.stop_account(channel_id, account_id)
        key = self._key(channel_id, account_id)
        self._push_targets.pop(key, None)
        client = self._clients.pop(key, None)
        if client is not None:
            await client.aclose()
        async with get_database().session() as session:
            return await ChannelAccountRepository(session).delete(account_id)

    # ------------------------------------------------------------------
    # 主动推送
    # ------------------------------------------------------------------
    async def push_text(self, text: str, photo: bytes | None = None) -> int:
        """把一条文本(可附图)推给所有已绑定且在运行的账号;返回送达队列的账号数。"""
        count = 0
        # 快照迭代:推送中途账号凭据失效/解绑会并发修改地址簿,直接迭代会炸
        for key, reply in list(self._push_targets.items()):
            channel_id, account_id = key.split(":", 1)
            dispatcher = self.manager.get_dispatcher(channel_id, account_id)
            if dispatcher is None:
                continue
            await dispatcher.push_outbound(
                OutboundEnvelope(reply=reply, text=text, origin="push", photo=photo)
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # 会话持久化与 Agent 执行(与微信同一套体系)
    # ------------------------------------------------------------------
    async def _ensure_agent_session(self, msg: InboundMessage) -> str:
        store = get_agent_session_store()
        display = _SPECS[msg.channel_id].display_name
        async with get_database().session() as session:
            account_repo = ChannelAccountRepository(session)
            row = await account_repo.get(msg.account_id)
            existing = (row.agent_session_id or "").strip() if row else ""
            if existing:
                session_row = await AgentSessionRepository(session).get(existing)
                if session_row is not None and store.path(existing).exists():
                    return existing
                logger.warning(
                    "IM 会话已失效,将新建 account=%s session=%s", msg.account_id, existing
                )
            header = store.create()
            await AgentSessionRepository(session).create(
                header.session_id, title=f"{display} · {msg.user_id}"
            )
            await account_repo.set_agent_session(msg.account_id, header.session_id)
            return header.session_id

    async def _reset_session(self, session_key: str) -> None:
        account_id = session_key.split(":", 2)[1]
        async with get_database().session() as session:
            await ChannelAccountRepository(session).set_agent_session(account_id, None)

    async def _run_agent(self, msg: InboundMessage, emit: Callable[[str], Awaitable[None]]) -> None:
        from movieclaw_channel.pusher import StepReplyPusher

        await emit(_ACK_TEXT)
        try:
            async with get_database().session() as session:
                llm_router = await acquire_llm_router(session)
        except NotFoundException as exc:
            await emit(f"⚠️ {exc.message}")
            return

        store = get_agent_session_store()
        session_id = await self._ensure_agent_session(msg)
        history = store.build_history(session_id)
        recorder = AgentSessionRecorder(store, session_id, entry_count=len(history))
        await recorder.record_user_message(msg.text)

        runner = AgentRunner(
            llm_router,
            tools=await self._restricted_tools(session_id, msg.channel_id),
            max_steps=_IM_MAX_STEPS,
            on_message=recorder.on_message,
            on_compaction=recorder.on_compaction,
        )
        run_id = uuid.uuid4().hex[:12]
        await recorder.begin(run_id)

        pusher = StepReplyPusher(emit)
        last_event: AgentEvent | None = None
        try:
            async for ev in runner.start(
                AgentStartParams(input=msg.text, history=history), run_id=run_id
            ):
                last_event = ev
                await pusher.feed(ev)
        finally:
            terminal = (
                last_event
                if last_event is not None
                and last_event.type in ("agent_done", "agent_error", "agent_cancelled")
                else AgentEvent(type="agent_cancelled", run_id=run_id)
            )
            # IM 通道没有用户主动停止入口，运行被取消只可能来自停机——
            # 合成回执按「服务中断」文案走（引导模型先核实工具实际结果）
            await recorder.on_terminal(terminal, reason="service_interrupted")

    async def _restricted_tools(self, session_id: str, channel_id: str):
        """IM 侧受限工具集:仅 mclaw 产品操作工具,与微信同一安全红线。"""
        settings = get_settings()
        workdir = Path(settings.agent_workspace_dir).resolve() / channel_id
        workdir.mkdir(parents=True, exist_ok=True)
        token = await auth_service.issue_agent_token(session_id)
        cli_env = {
            "MOVIECLAW_SERVER": f"http://127.0.0.1:{settings.port}",
            "MOVIECLAW_TOKEN": token,
        }
        return [make_mclaw_tool(workdir, cli_env, render_service_map())]


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------

_service: ImChannelService | None = None


async def init_im_channels() -> ImChannelService:
    global _service
    _service = ImChannelService()
    await _service.start()
    return _service


def get_im_channels() -> ImChannelService:
    if _service is None:
        raise RuntimeError("IM 通道服务尚未初始化")
    return _service


async def close_im_channels() -> None:
    global _service
    if _service is not None:
        await _service.stop()
        _service = None
