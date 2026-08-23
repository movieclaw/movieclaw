"""Agent 会话的 JSONL 持久化存储（事实源）。

设计定案（调研 pi / codex / Claude Code 三家后的共识 + 项目取舍）：

1. **一个会话一个 append-only JSONL 文件**：首行是会话头，之后每行一条
   消息 entry。历史行永不改写——中断、重启都只会追加，不会破坏已有内容。
   唯一例外是 ``discard_from_user_message``（retry 的历史替换），见该方法
   的说明；除它以外，任何写入路径都只准 append。
2. **只落定稿消息，不落流式 delta**：SSE 增量属于 UI 通道；文件里的
   ``message`` 保留事实发生顺序，resume 投影时只修复进程重启可能造成的
   工具回执迟到/缺失，不改写完整轨迹。
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

from movieclaw_agent import CompactionResult
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_llm import ChatMessage, TokenUsage

logger = logging.getLogger("movieclaw_api.agent_sessions")

#: 文件格式版本；未来结构变化时 +1，读取端按版本做迁移
#: v2：新增 type="compaction" 的压缩行（读端向后兼容 v1，无需迁移）
SESSION_FORMAT_VERSION = 2

#: 会话标题 / 最后提示预览的截断长度（DB 索引列用，全文始终在文件里）
PREVIEW_MAX_CHARS = 80

#: 进程退出时给尚未完成的工具调用回填的统一结果文本。
_INTERRUPTED_TOOL_RESULT = "操作已被中断，工具未执行完成。"


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


#: 消息行与压缩行的联合类型；_read 逐行按 type 判别解析
SessionTranscriptEntry = Annotated[
    SessionMessageEntry | SessionCompactionEntry,
    Field(discriminator="type"),
]
_entry_adapter: TypeAdapter[SessionMessageEntry | SessionCompactionEntry] = TypeAdapter(
    SessionTranscriptEntry
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


def _last_compaction_index(
    entries: list[SessionMessageEntry | SessionCompactionEntry],
) -> int:
    """最后一条压缩行的下标；没有压缩行时返回 -1。"""
    for i in range(len(entries) - 1, -1, -1):
        if isinstance(entries[i], SessionCompactionEntry):
            return i
    return -1


def _messages_after_last_compaction(
    entries: list[SessionMessageEntry | SessionCompactionEntry],
) -> list[ChatMessage]:
    """最后一条压缩行之后的消息（无压缩行时即全部消息）。"""
    last = _last_compaction_index(entries)
    return [e.message for e in entries[last + 1 :] if isinstance(e, SessionMessageEntry)]


def _normalize_tool_call_history(
    messages: list[ChatMessage],
) -> tuple[list[ChatMessage], int, int, int]:
    """把工具结果投影到对应 assistant 调用之后，返回修复后的模型上下文。

    OpenAI 工具协议不只要求 call id 存在，还要求每组 assistant tool_calls 在
    下一条普通消息之前收齐结果。应用重启可能留下「assistant → user → tool」的
    迟到回执；完整转录仍保留事实发生顺序，这里只修复发送给模型的投影：

    - 迟到回执移动到对应调用之后；
    - 完全缺失的回执生成中断结果，避免坏历史永久阻断续聊；
    - 找不到对应调用的孤立 tool 消息不进入模型上下文。

    额外返回（补写数、迟到数、孤立数），供调用方输出可理解的诊断日志。
    """
    outputs_by_id: dict[str, list[tuple[int, ChatMessage]]] = {}
    tool_indexes: set[int] = set()
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        tool_indexes.add(index)
        if message.tool_call_id:
            outputs_by_id.setdefault(message.tool_call_id, []).append((index, message))

    normalized: list[ChatMessage] = []
    used_tool_indexes: set[int] = set()
    synthesized = 0
    late = 0
    for call_index, message in enumerate(messages):
        if message.role == "tool":
            continue
        normalized.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        for tool_call in message.tool_calls:
            matched: tuple[int, ChatMessage] | None = None
            for output_index, output in outputs_by_id.get(tool_call.id, []):
                if output_index > call_index and output_index not in used_tool_indexes:
                    matched = (output_index, output)
                    break
            if matched is None:
                normalized.append(
                    ChatMessage(
                        role="tool",
                        content=_INTERRUPTED_TOOL_RESULT,
                        tool_call_id=tool_call.id,
                        name=tool_call.name,
                    )
                )
                synthesized += 1
                continue
            output_index, output = matched
            used_tool_indexes.add(output_index)
            if any(messages[index].role != "tool" for index in range(call_index + 1, output_index)):
                late += 1
            normalized.append(output)

    orphaned = len(tool_indexes - used_tool_indexes)
    return normalized, synthesized, late, orphaned


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
            message=message,
            model=model,
            usage=usage,
            finish_reason=finish_reason,
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
            replacement_history=result.replacement_history,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json(exclude_none=True) + "\n")
        self._leaf_cache[session_id] = entry.uuid
        return entry

    def seal_pending_tool_calls(self, session_id: str) -> int:
        """中断收尾：给没有结果的 tool_call 补写错误回执，返回补写条数。

        正常终态保证文件里的 assistant tool_calls 都有 tool 回执。进程硬退出
        无法运行本方法，续聊时由 prepare_history 再补；若旧版已经把新 user
        写在回执前面，build_history 会在模型上下文投影中恢复协议顺序。

        只检查最后一条压缩行之后的消息：更早的往返已被压缩挡在上下文之外，
        给死上下文补回执毫无意义。
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
                        content=_INTERRUPTED_TOOL_RESULT,
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
    ) -> tuple[SessionHeader, list[SessionMessageEntry | SessionCompactionEntry]]:
        """读取整个会话（头 + 全部 entry，含压缩行），坏行静默跳过。"""
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

        文件里保存完整事实轨迹；system 提示词不入库（随代码版本演进），由
        runner 每次运行时重新拼装。投影到模型上下文时会修复重启窗口产生的
        迟到/缺失工具回执，避免一条坏顺序让整个会话永久无法续聊。

        有压缩行时，从**最后一条**压缩行的替换历史起步、只追加其后的增量
        消息——压缩前的原始消息仍完整留在文件里（回放展示用），但不再进入
        模型上下文。
        """
        _, entries = self.read(session_id)
        last = _last_compaction_index(entries)
        if last < 0:
            messages = [e.message for e in entries if isinstance(e, SessionMessageEntry)]
        else:
            messages = [
                *entries[last].replacement_history,
                *(e.message for e in entries[last + 1 :] if isinstance(e, SessionMessageEntry)),
            ]
        history, synthesized, late, orphaned = _normalize_tool_call_history(messages)
        if synthesized or late or orphaned:
            logger.warning(
                "会话历史工具回执顺序已在模型上下文中修复 "
                "session=%s 补回执=%d 迟到=%d 孤立=%d",
                session_id,
                synthesized,
                late,
                orphaned,
            )
        return history

    def prepare_history(self, session_id: str) -> list[ChatMessage]:
        """续聊前补齐尾部未完成调用，再构建协议合法的模型上下文。"""
        sealed = self.seal_pending_tool_calls(session_id)
        if sealed:
            logger.info("续聊前已补齐中断的工具调用 session=%s 数量=%d", session_id, sealed)
        return self.build_history(session_id)

    def summarize(self, session_id: str) -> SessionSummary:
        """扫描单个会话文件生成索引摘要。"""
        header, entries = self.read(session_id)
        # 标题/预览只看消息行；压缩行计入 entry_count 与链尾但不产生文本
        user_texts = [
            e.message.text().strip()
            for e in entries
            if isinstance(e, SessionMessageEntry)
            and e.message.role == "user"
            and e.message.text().strip()
        ]
        return SessionSummary(
            session_id=header.session_id,
            created_at=header.created_at,
            entry_count=len(entries),
            leaf_uuid=entries[-1].uuid if entries else None,
            title=user_texts[0][:PREVIEW_MAX_CHARS] if user_texts else None,
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
    ) -> tuple[SessionHeader, list[SessionMessageEntry | SessionCompactionEntry], int]:
        """逐行解析文件；返回（头、entry 列表、坏行数）。

        首行必须是合法会话头（否则整个文件视为损坏抛错）；其余行坏了只跳过。
        """
        entries: list[SessionMessageEntry | SessionCompactionEntry] = []
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
