"""上下文压缩 —— codex 本地压缩策略的移植。

核心思路（与 codex-rs core/src/compact.rs 对齐，取舍见 docs/design/agent-context-compaction.md）：

1. **触发以服务端上报的 usage 为准**：每步响应带回真实 prompt_tokens，无需引入
   tokenizer；仅在没有服务端数据的场合（续聊冷启动、尚未发送的工具结果）用
   「UTF-8 字节数 ≈ 4 字节/token」的启发式补估。
2. **压缩就是一次普通模型调用**：把交接摘要指令作为 user 消息追加到全量历史
   末尾，不带任何工具定义——模型只能输出文本，天然保证这一轮只写摘要。
3. **重建历史「宽进严出」**：生成摘要时模型看得到全部现场（用户输入、assistant
   往返、工具调用与结果）；压缩落地后只保留「预算内最近的用户原话 + 摘要」，
   工具轨迹全部丢弃——用户原话永不经过有损摘要，防多次压缩后忘记原始意图。
4. **失败一律降级**：压缩失败只记日志、跳过本次压缩，绝不中断正在进行的运行
   （与 runner 的工具容错同一哲学）。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from movieclaw_agent.prompts import COMPACT_PROMPT, SUMMARY_PREFIX
from movieclaw_llm import (
    ChatMessage,
    ChatRequest,
    ImagePart,
    LlmError,
    LlmRouter,
    ModelSettings,
)

logger = logging.getLogger(__name__)

#: 自动压缩水位线：上下文占用 ≥ 窗口的 90% 时触发（codex 同款比例）
COMPACT_TRIGGER_RATIO = 0.9

#: 重建历史时保留近期用户消息的 token 预算（codex 同款 20k）
RETAINED_USER_TOKEN_BUDGET = 20_000

#: 无 tokenizer 时的估算系数：约 4 字节（UTF-8）≈ 1 token。对中文同样适用——
#: 一个汉字 3 字节 ≈ 0.75 token，与主流 tokenizer 的量级一致，偏保守即可
APPROX_BYTES_PER_TOKEN = 4

#: 每张图片的估算 token 数。qwen-vl 按 28×28 patch 计价（2048×1024 约
#: 2600 token），前端上传前已压到最长边 2048，1000 取中等图的量级。估算
#: 只服务 90% 水位的压缩触发时机，偏差可接受；按分辨率精算对各家供应商
#: 公式不一，收益配不上复杂度（docs/design/agent-image-input.md §7）。
IMAGE_TOKEN_ESTIMATE = 1000


class CompactionResult(BaseModel):
    """一次压缩的完整产物：摘要 + 重建后的历史（不含 system，system 每次运行重拼）。"""

    summary: str
    replacement_history: list[ChatMessage]
    #: 压缩前后的估算 token 数（bytes/4 启发式，展示与观测用，非精确值）
    tokens_before: int
    tokens_after: int


def estimate_tokens(target: list[ChatMessage] | ChatMessage | str) -> int:
    """按 UTF-8 字节数估算 token（约 4 字节/token）。

    消息计入正文文本与工具调用的 raw_arguments/name；图片块按固定
    IMAGE_TOKEN_ESTIMATE 计入（引用型与内联型同价——供应商按像素计费，
    与我们本地是否已水合无关）。
    """
    if isinstance(target, str):
        return len(target.encode("utf-8")) // APPROX_BYTES_PER_TOKEN
    if isinstance(target, ChatMessage):
        total = len(target.text().encode("utf-8"))
        for tc in target.tool_calls or []:
            total += len(tc.name.encode("utf-8"))
            total += len(tc.raw_arguments.encode("utf-8"))
        tokens = total // APPROX_BYTES_PER_TOKEN
        if isinstance(target.content, list):
            tokens += IMAGE_TOKEN_ESTIMATE * sum(
                1 for part in target.content if isinstance(part, ImagePart)
            )
        return tokens
    return sum(estimate_tokens(message) for message in target)


def should_compact(context_tokens: int, context_window: int | None) -> bool:
    """是否达到自动压缩水位线；模型未声明窗口时永不自动压缩（调用方负责日志）。"""
    if not context_window:
        return False
    return context_tokens >= int(context_window * COMPACT_TRIGGER_RATIO)


def is_summary_message(message: ChatMessage) -> bool:
    """识别历史里由压缩产生的摘要消息（user 角色 + 固定前缀）。"""
    return message.role == "user" and message.text().startswith(SUMMARY_PREFIX)


def _truncate_middle(text: str, max_tokens: int) -> str:
    """中部截断：头尾各留一半预算，中间以标记替代（工具输出头尾信息密度最高，
    用户消息同理——开头是诉求、结尾是最新补充）。按字节预算切，落在多字节
    字符中间时向后退到合法边界。"""
    budget_bytes = max_tokens * APPROX_BYTES_PER_TOKEN
    data = text.encode("utf-8")
    if len(data) <= budget_bytes:
        return text
    half = budget_bytes // 2
    head = data[:half].decode("utf-8", errors="ignore")
    tail = data[-half:].decode("utf-8", errors="ignore")
    omitted = (len(data) - budget_bytes) // APPROX_BYTES_PER_TOKEN
    return f"{head}\n…（中间约 {omitted} token 已截断）…\n{tail}"


def build_replacement_history(messages: list[ChatMessage], summary: str) -> list[ChatMessage]:
    """按 codex 策略重建历史：预算内逆序保留的用户原话 + 摘要收尾。

    - 只保留真实 user 消息（排除 system / 旧压缩摘要），从最新往旧装入预算，
      装不下的最旧一条中部截断；
    - assistant / tool / 思考内容全部丢弃，信息由摘要承载；
    - 摘要带固定前缀、作为最后一条 user 消息——下一轮模型看到的最后一项就是
      交接摘要。
    """
    retained: list[ChatMessage] = []
    remaining = RETAINED_USER_TOKEN_BUDGET
    for message in reversed(messages):
        if message.role != "user" or is_summary_message(message):
            continue
        if remaining <= 0:
            break
        tokens = estimate_tokens(message)
        if tokens <= remaining:
            retained.append(message)
            remaining -= tokens
        elif isinstance(message.content, list) and any(
            isinstance(part, ImagePart) for part in message.content
        ):
            # 带图消息不做中部截断：截断路径用 text() 重建 content 会把图丢掉。
            # 整条按预算取舍——装不下就整条丢弃，信息由摘要承载。
            break
        else:
            retained.append(
                ChatMessage(role="user", content=_truncate_middle(message.text(), remaining))
            )
            break
    retained.reverse()

    summary_message = ChatMessage(role="user", content=f"{SUMMARY_PREFIX}\n{summary}")
    return [*retained, summary_message]


async def compact(
    router: LlmRouter,
    model: str,
    messages: list[ChatMessage],
    settings: ModelSettings,
) -> CompactionResult | None:
    """执行一次压缩：全量历史 + 摘要指令 → 模型写交接摘要 → 重建历史。

    ``messages`` 是完整现场（含 system 与全部工具往返）；返回的
    replacement_history 不含 system。任何失败（流错误、异常、空摘要）都
    返回 None，由调用方决定降级方式——本函数绝不抛异常。
    """
    request = ChatRequest(
        model=model,
        # 摘要指令是普通 user 消息；不带工具定义，模型只能输出文本
        messages=[*messages, ChatMessage(role="user", content=COMPACT_PROMPT)],
        tools=None,
        settings=settings,
    )
    summary: str | None = None
    try:
        async for event in router.chat_stream(request):
            if event.type == "error":
                logger.warning("上下文压缩失败（模型调用出错）：%s", event.partial.error)
                return None
            if event.type == "done":
                summary = (event.partial.content or "").strip()
    except LlmError as exc:
        logger.warning("上下文压缩失败（请求异常）：%s", exc)
        return None
    if not summary:
        logger.warning("上下文压缩失败：模型返回了空摘要")
        return None

    replacement = build_replacement_history(messages, summary)
    system = [m for m in messages[:1] if m.role == "system"]
    return CompactionResult(
        summary=summary,
        replacement_history=replacement,
        tokens_before=estimate_tokens(messages),
        tokens_after=estimate_tokens([*system, *replacement]),
    )


__all__ = [
    "APPROX_BYTES_PER_TOKEN",
    "COMPACT_TRIGGER_RATIO",
    "RETAINED_USER_TOKEN_BUDGET",
    "CompactionResult",
    "build_replacement_history",
    "compact",
    "estimate_tokens",
    "is_summary_message",
    "should_compact",
]
