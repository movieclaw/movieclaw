"""Agent 会话的 JSONL 持久化存储（事实源）。

设计定案（调研 pi / codex / Claude Code 三家后的共识 + 项目取舍）：

1. **一个会话一个 append-only JSONL 文件**：首行是会话头，之后每行一条
   消息 entry。历史行永不改写——中断、重启都只会追加，不会破坏已有内容。
   唯一例外是 ``discard_from_user_message``（retry 的历史替换），见该方法
   的说明；除它以外，任何写入路径都只准 append。
2. **只落定稿消息，不落流式 delta**：SSE 增量属于 UI 通道；文件里的
   ``message`` 就是 LLM API 原样格式（ChatMessage），resume 重建上下文
   零转换。
3. **entry 带 uuid / parent_uuid**：v1 是纯线性链（parent 永远指向上一
   条），字段先留好，将来做历史分支时无需迁移文件格式。
4. **SQLite 的 agent_session 表只是查询索引**：任何时候都能由本目录的
   文件整体重建（见 repository 层的 rebuild），因此写入顺序固定为
   「先 append 文件、后更新 DB」，两步之间崩溃只会让索引落后，不会产生
   幽灵会话。
5. **读取容错**：进程崩溃可能留下半行，逐行解析时静默跳过坏行并计数，
   绝不让整个会话打不开。
6. **v2 新增压缩行**（``type: "compaction"``）：上下文压缩时追加一条含摘要与
   完整替换历史的记录，``build_history`` 从最后一条压缩行起重建（codex 的
   Compacted 记录同款思路）。老版本读端会把压缩行当坏行跳过，重建出未压缩的
   全量历史——更大但仍是合法上下文，属可接受的降级。
7. **v3 新增交接行**（``type: "handoff"``）：从旧会话创建独立新会话时，
   把源会话当时的有效上下文完整快照进新文件。新会话不依赖源文件，也不继承
   运行状态；``build_history`` 把交接行与压缩行同样视为上下文替换边界。
"""

from __future__ import annotations

import json
import logging
import os
import uuid as uuid_mod
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from movieclaw_agent import CompactionResult, strip_skill_blocks
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_llm import ChatMessage, ImagePart, TextPart, TokenUsage, ToolCall

logger = logging.getLogger("movieclaw_api.agent_sessions")

#: 文件格式版本；未来结构变化时 +1，读取端按版本做迁移
#: v3：新增 type="handoff" 的跨会话交接行；v1/v2 文件继续原样读取
SESSION_FORMAT_VERSION = 3

#: 会话标题 / 最后提示预览的截断长度（DB 索引列用，全文始终在文件里）
PREVIEW_MAX_CHARS = 80

#: 中断收尾的来源分级（决定合成回执的文案，见 seal_pending_tool_calls）
SealReason = Literal["user_cancelled", "service_interrupted"]

_SEAL_TEXTS: dict[str, str] = {
    "user_cancelled": (
        "用户停止了本次运行，此工具调用被中断。它可能已产生部分效果；"
        "如需确认实际结果，请先用查询类操作核实，不要盲目重发。"
    ),
    "service_interrupted": (
        "运行被中断（服务重启或异常），此工具调用的结果未知。"
        "继续任务前请先查询相关状态确认它是否已生效，避免重复执行有副作用的操作。"
    ),
}


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 串（带 +00:00，文件格式统一用 aware 时间）。"""
    return datetime.now(UTC).isoformat()


class SessionHeader(BaseModel):
    """JSONL 文件首行：会话身份与创建信息。

    标题、活跃时间等会变化的元数据不放这里——那些是 DB 索引的职责，
    头一旦写入就不再变化（append-only 原则）。
    """

    type: Literal["session"] = "session"
    version: int = SESSION_FORMAT_VERSION
    session_id: str
    created_at: str


class SessionMessageEntry(BaseModel):
    """JSONL 消息行：信封 + LLM API 原样消息。

    ``model / usage / finish_reason`` 仅 assistant 消息携带（运行元数据，
    不属于 API message 本身，故放信封层）。``finish_reason`` 约定含
    ``"aborted"``：运行被取消时由收尾逻辑写入。
    """

    type: Literal["message"] = "message"
    uuid: str
    parent_uuid: str | None = None
    timestamp: str
    message: ChatMessage
    model: str | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    #: 仅 user 消息携带：这条消息生效的思维链档位（None = 默认）。会话的
    #: 「当前档位」= 最近一条 user 行的值（与压缩接口沿用最近模型同一先例），
    #: 不给 agent_session 表加列。老读端忽略该字段（向前兼容）。
    thinking_level: str | None = None


class SessionCompactionEntry(BaseModel):
    """JSONL 压缩行：摘要 + 完整替换历史（codex Compacted 记录同款）。

    ``replacement_history`` 是压缩后模型上下文的精确内容（不含 system——
    system 从不入库，每次运行重拼）。resume 时以它为起点、追加其后的增量
    消息即可精确重建，不存在「按摘要重算」的歧义。
    """

    type: Literal["compaction"] = "compaction"
    uuid: str
    parent_uuid: str | None = None
    timestamp: str
    summary: str
    replacement_history: list[ChatMessage]
    #: 压缩前后的估算 token 数（展示与观测用，非精确值）
    tokens_before: int | None = None
    tokens_after: int | None = None


class SessionHandoffEntry(BaseModel):
    """跨会话交接行：源会话有效上下文的一份独立快照。

    新会话必须在源文件被删除后仍可续聊，因此这里保存 ``replacement_history``
    而不是只记一个外键。``source_leaf_uuid`` 标记快照时点：源会话之后即使继续
    写入，也不会悄悄改变已经创建的新会话。运行编号、心跳与 SSE 事件从不继承。
    """

    type: Literal["handoff"] = "handoff"
    uuid: str
    parent_uuid: str | None = None
    timestamp: str
    source_session_id: str
    source_leaf_uuid: str | None = None
    source_title: str | None = None
    replacement_history: list[ChatMessage]


#: 消息、压缩与交接行的联合类型；_read 逐行按 type 判别解析
SessionTranscriptEntry = Annotated[
    SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry,
    Field(discriminator="type"),
]
_entry_adapter: TypeAdapter[SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry] = (
    TypeAdapter(SessionTranscriptEntry)
)


class SessionSummary(BaseModel):
    """扫描一个会话文件得到的索引摘要（rebuild 与列表回填用）。"""

    session_id: str
    created_at: str
    entry_count: int
    leaf_uuid: str | None
    #: 首条 user 消息的截断文本；作为无自定义标题时的会话标题
    title: str | None
    #: 最后一条 user 消息的截断文本（列表页副标题）
    last_prompt: str | None
    #: 最后一条 entry 的时间戳（文件为空时取头的 created_at）
    last_timestamp: str


def dehydrate_message(message: ChatMessage) -> ChatMessage:
    """落库脱水（引用化的不变量守门员，docs/design/agent-image-input.md §6）。

    发请求前的水合会给引用型 ImagePart 补上 base64 字节、给带图 user 消息
    追加附件清单文本；两者都只属于请求投影。压缩行的 replacement_history
    来自运行内已水合的消息，不在这里剥掉就会把几 MB base64 写进转录。
    规则：

    - 带 attachment_id 的 ImagePart 一律置 data=None（字节的事实源在
      assets 目录，转录只存引用）；
    - 附件清单文本（固定前缀，见 agent_attachments.ATTACHMENT_NOTE_PREFIX）
      仅在消息**同时含图**时剥除——用户自己打出前缀文字的纯文本消息不受
      影响。

    应用在本 store 的全部写入口（append / append_compaction / append_handoff），
    store 是唯一写盘口，守在这里才覆盖手动压缩等不经 recorder 的路径。
    """
    from movieclaw_api.services.agent_attachments import ATTACHMENT_NOTE_PREFIX

    if not isinstance(message.content, list):
        return message
    has_image = any(isinstance(p, ImagePart) for p in message.content)
    changed = False
    parts = []
    for part in message.content:
        if isinstance(part, ImagePart) and part.attachment_id and part.data is not None:
            parts.append(part.model_copy(update={"data": None}))
            changed = True
            continue
        if (
            has_image
            and isinstance(part, TextPart)
            and part.text.startswith(ATTACHMENT_NOTE_PREFIX)
        ):
            changed = True
            continue
        parts.append(part)
    return message.model_copy(update={"content": parts}) if changed else message


def latest_user_thinking_level(
    entries: list[SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry],
) -> str | None:
    """会话当前生效的思维链档位：最近一条 user 行的信封值（None = 默认）。

    续聊未显式传档位时沿用它——与手动压缩「沿用会话最近一次使用的模型」
    同一口径。没有任何 user 行（新会话/纯交接会话）即默认。
    """
    for entry in reversed(entries):
        if isinstance(entry, SessionMessageEntry) and entry.message.role == "user":
            return entry.thinking_level
    return None


def message_preview(message: ChatMessage) -> str:
    """消息的列表预览文本：正文优先，纯图消息用「[图片]」占位。

    显式技能调用的展开块（<skill> 开头的 user 消息）替换为「[技能]」占位，
    预览只留用户自己的话——与图片占位同一思路。

    供会话标题 / last_prompt 使用（summarize 与 recorder 共用同一口径，
    重建索引与实时写入才不会出现两种预览）。
    """
    text = message.text().strip()
    if text:
        skill_names, rest = strip_skill_blocks(text)
        if skill_names:
            return f"[技能] {rest}".strip()
        return text
    if isinstance(message.content, list):
        images = sum(1 for p in message.content if isinstance(p, ImagePart))
        if images == 1:
            return "[图片]"
        if images > 1:
            return f"[图片 ×{images}]"
    return ""


def _repair_unpaired_tool_messages(
    session_id: str, messages: list[ChatMessage]
) -> list[ChatMessage]:
    """读取侧最后防线：把未配对的 tool_call / tool 回执修复成协议完整的历史。

    复用交接快照的修复逻辑（补「结果未知」回执 / 降级孤立回执），只在
    内存里的投影上生效，不回写文件。命中即记错误日志——正常部署下写入侧
    seal 保证这里永远是直通路径（docs/design/agent-runtime-resilience.md §4.2）。
    """
    repaired = _repair_handoff_history(messages)
    if repaired != messages:
        logger.error(
            "会话 %s 重建上下文时发现未配对的工具消息，已在内存中修复"
            "（正常情况下写入侧收尾应保证配对完整，请检查日志中的中断收尾记录）",
            session_id,
        )
    return repaired


def _last_context_boundary_index(
    entries: list[SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry],
) -> int:
    """最后一条上下文替换边界（压缩或交接）的下标；没有时返回 -1。"""
    for i in range(len(entries) - 1, -1, -1):
        if isinstance(entries[i], (SessionCompactionEntry, SessionHandoffEntry)):
            return i
    return -1


def _messages_after_last_compaction(
    entries: list[SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry],
) -> list[ChatMessage]:
    """最后一个上下文边界之后的消息（没有边界时即全部消息）。"""
    last = _last_context_boundary_index(entries)
    return [e.message for e in entries[last + 1 :] if isinstance(e, SessionMessageEntry)]


def _repair_handoff_history(history: list[ChatMessage]) -> list[ChatMessage]:
    """复制一份协议完整的历史，不修改源数据（交接快照与读取侧兜底共用）。

    硬崩可能留下 assistant tool_call 却没有 tool 回执；直接回喂时部分
    供应商会因此拒绝整次请求。在每个缺口处补一条「结果未知」，提醒
    Agent 先查询真实状态，不能武断地把外部操作判成未执行。孤立 tool 回执
    则降级成普通历史说明，既保住信息，也不把非法 tool 消息喂给供应商。
    无缺口时逐条原样返回（调用方可用相等比较判断是否发生过修复）。
    """
    repaired: list[ChatMessage] = []
    pending: list[ToolCall] = []

    def seal_pending() -> None:
        for tool_call in pending:
            repaired.append(
                ChatMessage(
                    role="tool",
                    content=(
                        f"会话在此处异常中断，工具「{tool_call.name}」的结果未知。"
                        "继续前请先查询实际状态，避免重复操作。"
                    ),
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                )
            )
        pending.clear()

    for message in history:
        # system 随当前代码版本重新生成，不从旧会话继承。
        if message.role == "system":
            continue
        if message.role == "tool":
            match = next(
                (call for call in pending if call.id == message.tool_call_id),
                None,
            )
            if match is not None:
                repaired.append(message)
                pending.remove(match)
            else:
                repaired.append(
                    ChatMessage(
                        role="user",
                        content=(
                            f"【历史中的孤立工具回执：{message.name or '未知工具'}】\n"
                            f"{message.text()}"
                        ),
                    )
                )
            continue

        if pending:
            seal_pending()
        repaired.append(message)
        if message.role == "assistant" and message.tool_calls:
            pending.extend(message.tool_calls)

    if pending:
        seal_pending()
    return repaired


class AgentSessionStore:
    """会话 JSONL 文件的读写入口。

    同一会话同一时刻只有一个运行在追加（路由层用 active_run_id 挡并发），
    因此这里不做文件锁；写入用同步 IO——单行 append 是微秒级操作，不值得
    为它引入线程池调度。
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        #: session_id → 最后一条 entry 的 uuid（避免每次 append 都重读文件）
        self._leaf_cache: dict[str, str | None] = {}

    @property
    def root(self) -> Path:
        """转录目录（启动自愈遍历用）。"""
        return self._root

    def path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create(self, session_id: str | None = None) -> SessionHeader:
        """新建会话文件并写入头行，返回头信息。"""
        header = SessionHeader(
            session_id=session_id or uuid_mod.uuid4().hex,
            created_at=_now_iso(),
        )
        path = self.path(header.session_id)
        self._root.mkdir(parents=True, exist_ok=True)
        # "x" 模式：会话 id 冲突（几乎不可能）时宁可报错也不覆盖已有文件
        with path.open("x", encoding="utf-8") as f:
            f.write(header.model_dump_json() + "\n")
        self._leaf_cache[header.session_id] = None
        return header

    def append(
        self,
        session_id: str,
        message: ChatMessage,
        *,
        model: str | None = None,
        usage: TokenUsage | None = None,
        finish_reason: str | None = None,
        thinking_level: str | None = None,
    ) -> SessionMessageEntry:
        """追加一条定稿消息，自动接到当前链尾，返回写入的 entry。"""
        path = self.path(session_id)
        if not path.is_file():
            raise NotFoundException("Agent 会话不存在或转录文件已被删除")
        if session_id not in self._leaf_cache:
            _, entries, _ = self._read(path)
            self._leaf_cache[session_id] = entries[-1].uuid if entries else None
        entry = SessionMessageEntry(
            uuid=uuid_mod.uuid4().hex[:12],
            parent_uuid=self._leaf_cache[session_id],
            timestamp=_now_iso(),
            message=dehydrate_message(message),
            model=model,
            usage=usage,
            finish_reason=finish_reason,
            thinking_level=thinking_level,
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json(exclude_none=True) + "\n")
        self._leaf_cache[session_id] = entry.uuid
        return entry

    def append_compaction(
        self, session_id: str, result: CompactionResult
    ) -> SessionCompactionEntry:
        """追加一条压缩行，与 append 同款接到当前链尾（parent 链线性穿过压缩行）。"""
        path = self.path(session_id)
        if not path.is_file():
            raise NotFoundException("Agent 会话不存在或转录文件已被删除")
        if session_id not in self._leaf_cache:
            _, entries, _ = self._read(path)
            self._leaf_cache[session_id] = entries[-1].uuid if entries else None
        entry = SessionCompactionEntry(
            uuid=uuid_mod.uuid4().hex[:12],
            parent_uuid=self._leaf_cache[session_id],
            timestamp=_now_iso(),
            summary=result.summary,
            replacement_history=[dehydrate_message(m) for m in result.replacement_history],
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json(exclude_none=True) + "\n")
        self._leaf_cache[session_id] = entry.uuid
        return entry

    def append_handoff(
        self,
        session_id: str,
        *,
        source_session_id: str,
        source_leaf_uuid: str | None,
        source_title: str | None,
        replacement_history: list[ChatMessage],
    ) -> SessionHandoffEntry:
        """给新建的空会话写入首条交接快照。"""
        path = self.path(session_id)
        if not path.is_file():
            raise NotFoundException("Agent 会话不存在或转录文件已被删除")
        if session_id not in self._leaf_cache:
            _, entries, _ = self._read(path)
            self._leaf_cache[session_id] = entries[-1].uuid if entries else None
        entry = SessionHandoffEntry(
            uuid=uuid_mod.uuid4().hex[:12],
            parent_uuid=self._leaf_cache[session_id],
            timestamp=_now_iso(),
            source_session_id=source_session_id,
            source_leaf_uuid=source_leaf_uuid,
            source_title=source_title,
            replacement_history=[dehydrate_message(m) for m in replacement_history],
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json(exclude_none=True) + "\n")
        self._leaf_cache[session_id] = entry.uuid
        return entry

    def fork(
        self, source_session_id: str, *, source_title: str | None = None
    ) -> tuple[SessionHeader, SessionHandoffEntry]:
        """从源会话的有效上下文创建一个完全独立的新会话。

        读取与修复均先完成，再创建目标文件；源会话为空或不可读时不会留下空的
        新会话。这里只复制模型上下文，不复制标题索引、运行状态或事件流。
        """
        _, source_entries = self.read(source_session_id)
        history = _repair_handoff_history(self.build_history(source_session_id))
        if not history:
            raise BadRequestException("源会话没有可继承的上下文")
        source_leaf_uuid = source_entries[-1].uuid if source_entries else None
        header = self.create()
        handoff = self.append_handoff(
            header.session_id,
            source_session_id=source_session_id,
            source_leaf_uuid=source_leaf_uuid,
            source_title=source_title,
            replacement_history=history,
        )
        return header, handoff

    def seal_pending_tool_calls(
        self, session_id: str, *, reason: SealReason = "user_cancelled"
    ) -> int:
        """中断收尾：给没有结果的 tool_call 补写错误回执，返回补写条数。

        保证文件里 assistant 的 tool_calls 与 tool 消息任何时刻都配对完整，
        resume 直接回喂 API 不需要修复逻辑（Claude Code 是吃到 400 再反应式
        修复，我们在写入侧一次做对更省事）。

        文案按中断来源分级（docs/design/agent-runtime-resilience.md §4.3）：
        两种场景下工具都**可能已产生副作用**（提交下载、创建订阅），合成
        回执必须表达「结果未知、先查询核实」，绝不能断言「未执行」——
        否则模型会盲目重发，副作用工具被重复执行。

        只检查最后一条压缩行之后的消息：更早的往返已被压缩挡在上下文之外，
        给死上下文补回执毫无意义。幂等：无孤儿时零写入。
        """
        _, entries, _ = self._read(self.path(session_id))
        messages = _messages_after_last_compaction(entries)
        answered = {m.tool_call_id for m in messages if m.role == "tool"}
        sealed = 0
        for message in messages:
            for tc in message.tool_calls or []:
                if tc.id in answered:
                    continue
                self.append(
                    session_id,
                    ChatMessage(
                        role="tool",
                        content=_SEAL_TEXTS[reason],
                        tool_call_id=tc.id,
                        name=tc.name,
                    ),
                )
                sealed += 1
        return sealed

    def discard_from_user_message(self, session_id: str, message_id: str) -> int:
        """删除指定 user message 及其之后的全部 entry，返回删除条数。

        retry 的落盘动作（前端二次确认后调用）：把目标消息及其后的所有往返
        从事实源上抹掉，会话链尾回到该消息之前，下一次
        ``append`` 自然接在那里，重建出的 LLM 上下文里也不再有被丢弃的内容。

        **这是本模块唯一改写历史行的方法**，与顶部「append-only」的约定相悖，
        属于刻意的例外：用户要的就是「这些记录不该再存在」，而不是「藏起来」。
        另一条路是按 parent_uuid 另开分支（格式早已留好字段），但那要求
        build_history / summarize / 回放 / 收尾全部改成沿父链回溯，为一个
        「用户明确要求丢弃」的场景付出的代价过大。

        整文件重写，写临时文件后 ``os.replace`` 原子换入：中途崩溃要么是旧文件
        完好、要么是新文件完整，不存在写坏一半的转录。

        已知副作用：重写会顺带丢掉文件里无法解析的坏行（异常退出的残留半行）
        ——它们本就不参与任何读取路径，清掉无损。
        """
        header, entries = self.read(session_id)
        index = next((i for i, e in enumerate(entries) if e.uuid == message_id), None)
        if index is None:
            raise NotFoundException("会话中没有这条记录，可能已被改写")
        target = entries[index]
        # 只允许从 user message 重试：从 assistant/tool 中间切一刀会留下没有
        # 回执的 tool_call，重建出的上下文喂回模型直接 400
        if not isinstance(target, SessionMessageEntry) or target.message.role != "user":
            raise BadRequestException("只能重试用户消息")

        kept = entries[:index]
        path = self.path(session_id)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(header.model_dump_json() + "\n")
            for entry in kept:
                f.write(entry.model_dump_json(exclude_none=True) + "\n")
        os.replace(tmp, path)
        self._leaf_cache[session_id] = kept[-1].uuid if kept else None
        return len(entries) - len(kept)

    def delete(self, session_id: str) -> None:
        """删除会话文件（幂等：文件不存在不报错）。"""
        self.path(session_id).unlink(missing_ok=True)
        self._leaf_cache.pop(session_id, None)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def read(
        self, session_id: str
    ) -> tuple[
        SessionHeader,
        list[SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry],
    ]:
        """读取整个会话（头 + 全部 entry，含压缩/交接行），坏行静默跳过。"""
        path = self.path(session_id)
        if not path.is_file():
            raise NotFoundException("Agent 会话不存在或转录文件已被删除")
        header, entries, bad = self._read(path)
        if bad:
            logger.warning(
                "会话文件存在 %d 行无法解析的记录，已跳过（可能来自异常退出）：%s",
                bad,
                path.name,
            )
        return header, entries

    def build_history(self, session_id: str) -> list[ChatMessage]:
        """把会话重建成 LLM 上下文消息列表（resume 喂回模型用）。

        文件里存的就是 API 原样消息，这里只做投影不做转换；system 提示词
        不入库（随代码版本演进），由 runner 每次运行时重新拼装。

        有压缩或交接行时，从**最后一个**上下文边界的替换历史起步、只追加
        其后的增量消息。交接因此只在新会话文件里保存一次快照，之后与普通
        会话完全相同，不再读取源文件。

        末端过一道读取侧防线（maka 回放层的成对丢弃）：写入侧 seal 的
        双保险生效后这里理论上永远命中 0，但绝不把必被供应商拒绝的
        非法配对发出去——防御纵深的最后一层。
        """
        _, entries = self.read(session_id)
        last = _last_context_boundary_index(entries)
        if last < 0:
            messages = [e.message for e in entries if isinstance(e, SessionMessageEntry)]
        else:
            messages = [
                *entries[last].replacement_history,
                *(e.message for e in entries[last + 1 :] if isinstance(e, SessionMessageEntry)),
            ]
        return _repair_unpaired_tool_messages(session_id, messages)

    def summarize(self, session_id: str) -> SessionSummary:
        """扫描单个会话文件生成索引摘要。"""
        header, entries = self.read(session_id)
        # 交接会话的标题固定为「续：源标题」——与 DB 索引只在 title 为空时
        # 回填的口径一致，用户后续发言不覆盖标题，重建索引时也保持同一语义。
        # 压缩/交接行都计入 entry_count 与链尾，但不伪造 last_prompt。
        user_texts = [
            message_preview(e.message)
            for e in entries
            if isinstance(e, SessionMessageEntry)
            and e.message.role == "user"
            and message_preview(e.message)
        ]
        handoff_title = next(
            (
                (e.source_title if e.source_title.startswith("续：") else f"续：{e.source_title}")[
                    :PREVIEW_MAX_CHARS
                ]
                for e in entries
                if isinstance(e, SessionHandoffEntry) and e.source_title
            ),
            None,
        )
        return SessionSummary(
            session_id=header.session_id,
            created_at=header.created_at,
            entry_count=len(entries),
            leaf_uuid=entries[-1].uuid if entries else None,
            title=handoff_title or (user_texts[0][:PREVIEW_MAX_CHARS] if user_texts else None),
            last_prompt=user_texts[-1][:PREVIEW_MAX_CHARS] if user_texts else None,
            last_timestamp=entries[-1].timestamp if entries else header.created_at,
        )

    def scan_all(self) -> list[SessionSummary]:
        """遍历目录下全部会话文件生成摘要（DB 索引整体重建用）。

        单个文件损坏（连头都解析不出）只告警跳过，不阻断其它会话重建。
        """
        if not self._root.is_dir():
            return []
        summaries: list[SessionSummary] = []
        for path in sorted(self._root.glob("*.jsonl")):
            try:
                summaries.append(self.summarize(path.stem))
            except Exception:  # noqa: BLE001 - 重建是自愈路径，单文件坏不拖垮全局
                logger.warning("会话文件无法解析，重建索引时已跳过：%s", path.name)
        return summaries

    def _read(
        self, path: Path
    ) -> tuple[
        SessionHeader,
        list[SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry],
        int,
    ]:
        """逐行解析文件；返回（头、entry 列表、坏行数）。

        首行必须是合法会话头（否则整个文件视为损坏抛错）；其余行坏了只跳过。
        """
        entries: list[SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry] = []
        bad = 0
        header: SessionHeader | None = None
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                if line_no == 0:
                    header = SessionHeader.model_validate_json(line)
                    continue
                try:
                    entries.append(_entry_adapter.validate_json(line))
                except (ValidationError, json.JSONDecodeError):
                    bad += 1
        if header is None:
            raise NotFoundException("Agent 会话文件为空或头记录损坏")
        return header, entries, bad


_store: AgentSessionStore | None = None


def get_agent_session_store() -> AgentSessionStore:
    """进程级单例：按配置目录构建会话存储。"""
    global _store
    if _store is None:
        from movieclaw_api.core.config import get_settings

        _store = AgentSessionStore(Path(get_settings().agent_sessions_dir).resolve())
    return _store


def reset_agent_session_store() -> None:
    """重置单例（测试隔离用：换目录后重新构建）。"""
    global _store
    _store = None
