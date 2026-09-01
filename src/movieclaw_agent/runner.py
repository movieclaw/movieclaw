"""AgentRunner —— Agent loop 执行器。

循环语义（与用户逐条确认的设计）：
- 每一步 = 一次流式模型调用；模型输出的工具调用**以内容为准**判断是否
  继续循环——finish_reason 只作参考，因为兼容端点存在「stop 但带调用」
  「tool_calls 但数组为空」两类在案怪癖（vLLM/Kimi 等）；
- 有工具调用 → 顺序执行每一个（参数先过 JSON Schema 校验），结果作为
  tool 消息回喂，进入下一步；无工具调用且有正文 → STOP，正常结束；
  无工具调用且正文为空 → 判为空响应，原样重试一次，仍空则以 agent_error
  收尾（详见循环内注释）；
- 工具的校验失败与执行异常都不中断循环：错误文本作为失败结果回喂，
  让模型自行修正（pi-agent / Claude Code 同款韧性设计）；
- max_steps（默认 200）防失控；错误一律以 agent_error 事件收尾，不抛异常。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from movieclaw_agent.compaction import (
    CompactionResult,
    compact,
    estimate_tokens,
    should_compact,
)
from movieclaw_agent.events import (
    AgentCompaction,
    AgentDone,
    AgentEvent,
    AgentStartParams,
    AgentToolResult,
)
from movieclaw_agent.prompts import build_system_prompt
from movieclaw_agent.toolkit import AgentTool
from movieclaw_llm import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LlmError,
    LlmRouter,
    TokenUsage,
    ToolCall,
    validate_tool_call,
)

logger = logging.getLogger(__name__)

#: tool_result 事件里输出的截断长度（完整输出仍进对话上下文喂给模型）
_EVENT_OUTPUT_LIMIT = 2000

#: 空响应的原样重试次数。模型偶发返回 0 token 的 STOP（供应商抖动、限流降级、
#: 安全过滤等），多为瞬时故障，原样重发一次通常就能拿到正常回复。
_MAX_EMPTY_RETRIES = 1


class AgentRunner:
    """绑定一个 LlmRouter 与一组工具的 Agent loop 执行器。"""

    def __init__(
        self,
        router: LlmRouter,
        tools: list[AgentTool] | None = None,
        *,
        max_steps: int = 200,
        on_message: Callable[[ChatMessage, ChatResponse | None], Awaitable[None]] | None = None,
        on_compaction: Callable[[CompactionResult], Awaitable[None]] | None = None,
    ) -> None:
        self._router = router
        self._tools = tools or []
        self._tools_by_name = {t.name: t for t in self._tools}
        self._max_steps = max_steps
        # 压缩定稿回调（转录落盘的挂点）：每次上下文压缩成功后调用一次，
        # 载荷含摘要与完整替换历史。与 on_message 同款降级：失败只告警。
        self._on_compaction = on_compaction
        # 定稿消息回调（会话持久化的挂点）：每当一条消息内容完全确定——
        # 中间步的 assistant（含 tool_calls）、每条 tool 结果、以及终答
        # assistant——各调用一次。流式 delta 不经过这里（只落定稿不落增量）。
        # assistant 消息附带所属 ChatResponse 供取 model/usage/finish_reason。
        self._on_message = on_message

    async def start(
        self,
        params: AgentStartParams,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """启动一次运行，产出 Agent 事件流。

        ``run_id`` 允许 API 编排层在创建后台任务前预先分配运行编号，这样
        创建接口返回的编号与随后所有事件中的编号完全一致。领域层直接调用
        时仍可省略，由 runner 自行生成，保持原有用法不变。
        """
        run_id = run_id or uuid.uuid4().hex[:12]
        started = time.monotonic()

        # 路由解析放在发事件之前：解析失败（没配供应商/模型不存在）
        # 也要以 agent_error 事件的形式告知前端，而不是断流
        try:
            provider, model_id = self._router.resolve(params.model)
        except LlmError as exc:
            yield AgentEvent(type="agent_error", run_id=run_id, error=str(exc))
            return

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=params.system_prompt or build_system_prompt())
        ]
        messages.extend(params.history)
        messages.append(ChatMessage(role="user", content=params.input))
        definitions = [t.definition for t in self._tools]

        # 自动压缩的窗口来源：模型目录声明的 context_window。未声明（目录外
        # 模型）时停用自动压缩——宁可放行到供应商报错，也不做瞎猜的预算
        context_window = self._router.get_model_info(params.model).context_window
        if not context_window:
            logger.info("模型未声明上下文窗口，自动压缩停用 model=%s", model_id)

        yield AgentEvent(type="agent_start", run_id=run_id, provider=provider.name, model=model_id)

        # pre-run 检查：续聊重建的历史可能已经逼近窗口（此时还没有服务端
        # usage 可用，用字节启发式估算），先压缩再进入循环
        compact_event = await self._maybe_compact(
            run_id, messages, estimate_tokens(messages), context_window, params
        )
        if compact_event is not None:
            yield compact_event

        usage = TokenUsage()
        #: 连续空响应次数，收到有效响应即清零
        empty_retries = 0
        #: 本步流式已累积的半截输出。取消打断流式时以 finish_reason=aborted
        #: 定稿落盘（pi 的 stopReason:"aborted" 同款语义）：用户屏幕上看到的
        #: 半截回答与转录一致，续聊时模型也知道自己说到哪被打断。
        partial_text = ""
        partial_thinking = ""
        try:
            for step in range(1, self._max_steps + 1):
                request = ChatRequest(
                    model=params.model,
                    messages=messages,
                    tools=definitions or None,
                    settings=params.settings,
                )

                final: ChatResponse | None = None
                partial_text = ""
                partial_thinking = ""
                async for event in self._router.chat_stream(request):
                    if event.type == "thinking_delta":
                        partial_thinking += event.delta or ""
                        yield AgentEvent(type="thinking_delta", run_id=run_id, delta=event.delta)
                    elif event.type == "text_delta":
                        partial_text += event.delta or ""
                        yield AgentEvent(type="text_delta", run_id=run_id, delta=event.delta)
                    elif event.type == "toolcall_start":
                        # 名称一确定就上报，前端从这一刻起即可展示「正在调用 xx 工具」
                        yield AgentEvent(
                            type="tool_call_start", run_id=run_id, tool_call=event.tool_call
                        )
                    elif event.type == "toolcall_delta":
                        # 参数 JSON 逐片上报；tool_call_id 让前端把增量归到正确的调用
                        yield AgentEvent(
                            type="tool_call_delta",
                            run_id=run_id,
                            delta=event.delta,
                            tool_call_id=event.tool_call.id if event.tool_call else None,
                        )
                    elif event.type == "toolcall_end":
                        yield AgentEvent(type="tool_call", run_id=run_id, tool_call=event.tool_call)
                    elif event.type == "error":
                        logger.warning("Agent 运行失败 run=%s：%s", run_id, event.partial.error)
                        yield AgentEvent(
                            type="agent_error",
                            run_id=run_id,
                            error=event.partial.error or "模型调用失败，原因未知",
                        )
                        return
                    elif event.type == "done":
                        final = event.partial

                if final is None:
                    yield AgentEvent(
                        type="agent_error", run_id=run_id, error="模型流异常终止，未返回结果"
                    )
                    return

                usage = _add_usage(usage, final.usage)

                # 空响应：既没有工具调用、正文也是空的。这不是「答完了」，而是模型
                # 或中转端点这一轮什么都没返回（0 token 的 STOP）。当成正常结束的话
                # 通道侧无话可发，微信/Telegram 上表现为对话毫无征兆地静默中断，
                # 日志里只留一行「运行已结束」，排查时极难定位。
                # 成因多为瞬时故障（供应商抖动、限流降级、安全过滤），所以先原样重发
                # 一次——messages 没动过，continue 即重试；连续空才判定失败。
                # 注意要在 _notify 之前处理：空的 assistant 消息不该写进会话历史，
                # 否则下一轮续聊会把这段空白也带上。
                if not final.tool_calls and not (final.content or "").strip():
                    if empty_retries < _MAX_EMPTY_RETRIES:
                        empty_retries += 1
                        logger.warning(
                            "Agent 收到空响应，原样重试第 %d 次 run=%s step=%d finish_reason=%s",
                            empty_retries,
                            run_id,
                            step,
                            final.finish_reason,
                        )
                        continue
                    logger.warning(
                        "Agent 连续空响应，判定失败 run=%s step=%d finish_reason=%s",
                        run_id,
                        step,
                        final.finish_reason,
                    )
                    yield AgentEvent(
                        type="agent_error",
                        run_id=run_id,
                        error="我连续两次都没能生成回复，可能是临时故障。请稍后再发一遍试试。",
                    )
                    return
                # 拿到了有效响应，重置重试计数
                empty_retries = 0

                # 循环判据：以 tool_calls 内容为准（finish_reason 只作参考）——
                # 覆盖「stop 但带调用」与「tool_calls 但数组为空」两类兼容怪癖
                if not final.tool_calls:
                    await self._notify(final.to_message(), final)
                    yield AgentEvent(
                        type="agent_done",
                        run_id=run_id,
                        result=AgentDone(
                            text=final.content,
                            thinking=final.thinking,
                            finish_reason=final.finish_reason,
                            usage=usage,
                            steps=step,
                            model=final.model,
                            provider=final.provider,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        ),
                    )
                    return

                # assistant 消息（含 tool_calls）入上下文，随后逐个执行工具
                messages.append(final.to_message())
                await self._notify(final.to_message(), final)
                # 本步 assistant 已定稿落盘，清空半截缓存：随后工具执行期间
                # 若被取消，不应把同样内容再以 aborted 重复落盘一次
                partial_text = ""
                partial_thinking = ""
                step_tool_messages: list[ChatMessage] = []
                for tc in final.tool_calls:
                    result = await self._execute_tool(tc, definitions)
                    yield AgentEvent(
                        type="tool_result",
                        run_id=run_id,
                        tool_result=AgentToolResult(
                            tool_call_id=result.tool_call_id,
                            name=result.name,
                            output=result.output[:_EVENT_OUTPUT_LIMIT],
                            is_error=result.is_error,
                            elapsed_ms=result.elapsed_ms,
                        ),
                    )
                    tool_message = ChatMessage(
                        role="tool",
                        content=result.output,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                    messages.append(tool_message)
                    step_tool_messages.append(tool_message)
                    await self._notify(tool_message, None)

                # mid-run 检查：本步全部工具结果已回喂、call/output 配对完整，
                # 此刻丢弃历史不会产生孤儿调用，是压缩的安全点。用量以服务端
                # 上报为准（模型实际看到的部分），仅对尚未发送的新工具结果补估
                step_usage = final.usage.prompt_tokens + final.usage.completion_tokens
                context_tokens = (
                    step_usage + estimate_tokens(step_tool_messages)
                    if step_usage > 0
                    # 供应商不回用量时退化为全量字节估算
                    else estimate_tokens(messages)
                )
                compact_event = await self._maybe_compact(
                    run_id, messages, context_tokens, context_window, params
                )
                if compact_event is not None:
                    yield compact_event

            logger.warning("Agent 达到最大步数上限 run=%s steps=%d", run_id, self._max_steps)
            yield AgentEvent(
                type="agent_error",
                run_id=run_id,
                error=f"已达到最大执行步数上限（{self._max_steps} 步）仍未完成，运行终止。"
                "请把任务拆小后重试，或检查是否陷入了循环。",
            )
        except (asyncio.CancelledError, GeneratorExit):
            # 取消打断流式时的半截定稿：用户屏幕上已经看到多少，转录里就落多少，
            # 以 finish_reason="aborted" 标记（pi 的 stopReason:"aborted" 同款
            # 语义），续聊时模型知道自己说到哪被打断。已定稿的步不会重复落盘
            # （定稿后 partial 缓存即清零）。
            # GeneratorExit：取消恰好落在 yield 悬停点时，本生成器收到的是
            # aclose 的 GeneratorExit 而非 CancelledError——清理期允许 await
            # （不允许再 yield），同样把半截定稿落盘后原样抛出。
            if partial_text or partial_thinking:
                aborted = ChatResponse(
                    content=partial_text or None,
                    thinking=partial_thinking or None,
                    finish_reason="aborted",
                    model=model_id,
                    provider=provider.name,
                )
                await self._notify(aborted.to_message(), aborted)
            raise

    async def _maybe_compact(
        self,
        run_id: str,
        messages: list[ChatMessage],
        context_tokens: int,
        context_window: int | None,
        params: AgentStartParams,
    ) -> AgentEvent | None:
        """达到水位线时压缩上下文并原地替换 messages，返回压缩事件。

        任何失败（compact 返回 None、意外异常）都降级为「本次不压缩」：
        记日志后照常返回 None，运行继续——靠工具层截断撑到下一个检查点，
        绝不因压缩失败中断任务。
        """
        if not should_compact(context_tokens, context_window):
            return None
        try:
            # 压缩是内部调用：思维链档位不随会话设置放大摘要成本，清掉再传
            compact_settings = params.settings.model_copy(update={"thinking_level": None})
            result = await compact(self._router, params.model, messages, compact_settings)
        except Exception:  # noqa: BLE001 - 压缩是优化路径，任何故障都不应拖垮运行
            logger.exception("上下文压缩发生意外错误，本次跳过压缩 run=%s", run_id)
            return None
        if result is None:
            # 具体原因 compact() 已记日志
            return None
        # 原地替换：system 保留在首位，其后是重建历史（保留的用户原话 + 摘要）
        messages[:] = [messages[0], *result.replacement_history]
        await self._notify_compaction(result)
        logger.info(
            "上下文已压缩 run=%s tokens=%d→%d", run_id, result.tokens_before, result.tokens_after
        )
        return AgentEvent(
            type="context_compacted",
            run_id=run_id,
            compaction=AgentCompaction(
                summary=result.summary,
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
            ),
        )

    async def _notify_compaction(self, result: CompactionResult) -> None:
        """调用压缩定稿回调；持久化失败只告警，不中断正在进行的运行。"""
        if self._on_compaction is None:
            return
        try:
            await self._on_compaction(result)
        except Exception:  # noqa: BLE001 - 落盘故障不应打断模型循环
            logger.exception("压缩记录持久化回调失败（本次压缩可能未落盘）")

    async def _notify(self, message: ChatMessage, response: ChatResponse | None) -> None:
        """调用定稿消息回调；持久化失败只告警，不中断正在进行的运行。"""
        if self._on_message is None:
            return
        try:
            await self._on_message(message, response)
        except Exception:  # noqa: BLE001 - 落盘故障不应打断模型循环
            logger.exception("Agent 消息持久化回调失败（本条消息可能未落盘）")

    async def _execute_tool(self, tc: ToolCall, definitions: list) -> AgentToolResult:
        """执行单个工具调用：校验 → 执行；任何失败都转为回喂文本，不抛异常。"""
        started = time.monotonic()
        args, err = validate_tool_call(definitions, tc)
        if err is not None:
            output, is_error = err, True
        else:
            tool = self._tools_by_name[tc.name]  # validate 已确认工具存在
            try:
                output, is_error = await tool.handler(args), False
            except Exception as exc:  # noqa: BLE001 - 工具异常回喂模型，不中断 loop
                logger.warning("工具执行失败 tool=%s：%s", tc.name, exc)
                output, is_error = f"工具执行失败：{exc}", True
        return AgentToolResult(
            tool_call_id=tc.id,
            name=tc.name,
            output=output,
            is_error=is_error,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _add_usage(total: TokenUsage, step: TokenUsage) -> TokenUsage:
    return TokenUsage(
        prompt_tokens=total.prompt_tokens + step.prompt_tokens,
        completion_tokens=total.completion_tokens + step.completion_tokens,
        total_tokens=total.total_tokens + step.total_tokens,
        cache_read_tokens=total.cache_read_tokens + step.cache_read_tokens,
    )
