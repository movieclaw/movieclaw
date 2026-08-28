"""AgentRunner loop：工具执行循环、兼容怪癖判据、错误回喂、步数上限、自动压缩。"""

from __future__ import annotations

import json

from movieclaw_agent import SUMMARY_PREFIX, AgentRunner, AgentStartParams, AgentTool
from movieclaw_agent.prompts import COMPACT_PROMPT
from movieclaw_llm import (
    ChatMessage,
    ChatResponse,
    LlmProviderConfig,
    LlmRouter,
    ModelInfo,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from movieclaw_llm.base import BaseLlmProtocol
from movieclaw_llm.models import ChatStreamEvent
from movieclaw_llm.protocols import PROTOCOLS

SEARCH_TOOL_DEF = ToolDefinition(
    name="search",
    description="搜索资源",
    parameters={
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    },
)

# 各测试用例共享的探针：记录工具入参与每步请求
_probe: dict = {}


def make_runner(
    protocol_cls, monkeypatch, *, handler=None, max_steps=200, on_message=None
) -> AgentRunner:
    monkeypatch.setitem(PROTOCOLS, "openai_chat", protocol_cls)
    _probe.clear()
    _probe["requests"] = []

    async def default_handler(args: dict) -> str:
        _probe["tool_args"] = args
        return "找到 3 条资源：A / B / C"

    router = LlmRouter(
        [
            LlmProviderConfig(
                name="测试百炼",
                provider_type="bailian",
                api_key="sk-x",
                default_model="qwen3.7-max",
                is_default=True,
            )
        ]
    )
    tools = [AgentTool(definition=SEARCH_TOOL_DEF, handler=handler or default_handler)]
    return AgentRunner(router, tools=tools, max_steps=max_steps, on_message=on_message)


class ToolLoopProtocol(BaseLlmProtocol):
    """两步流：首步发起工具调用，见到 tool 消息后给最终答复。"""

    #: 首步 done 的 finish_reason（子类覆盖以模拟兼容怪癖）
    first_finish = "tool_calls"
    #: 首步的工具调用（子类覆盖以模拟坏参数/未知工具）
    first_calls = [ToolCall(id="c1", name="search", arguments={"q": "沙丘"})]
    #: 首步的正文（子类覆盖；无工具调用时决定是「有话说」还是空响应）
    first_content = ""

    async def chat(self, request, model_id):  # pragma: no cover
        raise NotImplementedError

    async def chat_stream(self, request, model_id):
        _probe["requests"].append(request)
        snap = ChatResponse(model=model_id, provider=self.config.name)
        has_tool_msg = any(m.role == "tool" for m in request.messages)
        yield ChatStreamEvent(type="start", partial=snap)
        if not has_tool_msg:
            for tc in self.first_calls:
                # 与真实协议层一致的三段式：start（仅 id/name）→ delta 分片 → end
                yield ChatStreamEvent(
                    type="toolcall_start",
                    tool_call=ToolCall(id=tc.id, name=tc.name),
                    partial=snap,
                )
                args_json = json.dumps(tc.arguments, ensure_ascii=False)
                mid = len(args_json) // 2
                for piece in (args_json[:mid], args_json[mid:]):
                    yield ChatStreamEvent(
                        type="toolcall_delta",
                        delta=piece,
                        tool_call=ToolCall(id=tc.id, name=tc.name, raw_arguments=args_json),
                        partial=snap,
                    )
                yield ChatStreamEvent(type="toolcall_end", tool_call=tc, partial=snap)
            yield ChatStreamEvent(
                type="done",
                partial=ChatResponse(
                    content=self.first_content or None,
                    tool_calls=self.first_calls or None,
                    finish_reason=self.first_finish,
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                    model=model_id,
                    provider=self.config.name,
                ),
            )
        else:
            yield ChatStreamEvent(type="text_delta", delta="最终答复", partial=snap)
            yield ChatStreamEvent(
                type="done",
                partial=ChatResponse(
                    content="最终答复",
                    finish_reason="stop",
                    usage=TokenUsage(prompt_tokens=20, completion_tokens=7, total_tokens=27),
                    model=model_id,
                    provider=self.config.name,
                ),
            )

    async def test_connection(self):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass


async def collect(runner: AgentRunner, params: AgentStartParams):
    return [e async for e in runner.start(params)]


async def test_tool_loop_two_steps(monkeypatch):
    runner = make_runner(ToolLoopProtocol, monkeypatch)
    events = await collect(runner, AgentStartParams(input="找沙丘"))
    assert [e.type for e in events] == [
        "agent_start",
        "tool_call_start",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call",
        "tool_result",
        "text_delta",
        "agent_done",
    ]
    # 三段式工具调用事件：start 只带名称，delta 归属正确且拼出完整参数 JSON
    start = events[1]
    assert start.tool_call.name == "search" and start.tool_call.arguments == {}
    deltas = [e for e in events if e.type == "tool_call_delta"]
    assert all(e.tool_call_id == "c1" for e in deltas)
    assert "".join(e.delta for e in deltas) == '{"q": "沙丘"}'
    # 工具收到校验后的参数
    assert _probe["tool_args"] == {"q": "沙丘"}
    # 工具回执
    tr = next(e.tool_result for e in events if e.type == "tool_result")
    assert tr.name == "search" and not tr.is_error
    assert "找到 3 条资源" in tr.output
    # 终态：两步、usage 累计、最终正文
    done = events[-1].result
    assert done.steps == 2
    assert done.text == "最终答复"
    assert done.usage.total_tokens == 42  # 15 + 27
    # 第二步请求的上下文形态：system + user + assistant(带调用) + tool 结果
    round2 = _probe["requests"][1]
    assert [m.role for m in round2.messages] == ["system", "user", "assistant", "tool"]
    assert round2.messages[0].text().startswith("你是 MovieClaw 影音助理")
    assert "必须在当前轮次重新调用工具查询最新接口数据" in round2.messages[0].text()
    tool_msg = round2.messages[-1]
    assert tool_msg.tool_call_id == "c1"
    assert "找到 3 条资源" in tool_msg.text()


async def test_stop_finish_reason_with_tool_calls_still_loops(monkeypatch):
    """兼容怪癖①：finish_reason=stop 但带工具调用 → 以内容为准，照常执行。"""

    class QuirkProtocol(ToolLoopProtocol):
        first_finish = "stop"

    runner = make_runner(QuirkProtocol, monkeypatch)
    events = await collect(runner, AgentStartParams(input="x"))
    assert events[-1].type == "agent_done"
    assert events[-1].result.steps == 2


async def test_tool_calls_finish_reason_with_empty_calls_ends(monkeypatch):
    """兼容怪癖②：finish_reason=tool_calls 但数组为空 → 安全终止，不空转。"""

    class EmptyCallsProtocol(ToolLoopProtocol):
        first_calls = []
        first_content = "不用查了，直接回答你"

    runner = make_runner(EmptyCallsProtocol, monkeypatch)
    events = await collect(runner, AgentStartParams(input="x"))
    assert events[-1].type == "agent_done"
    assert events[-1].result.steps == 1


async def test_empty_response_retries_then_ends_with_agent_error(monkeypatch):
    """连续空响应（无工具调用且正文为空）→ 重试一次后 agent_error。

    伪装成 agent_done 的话，通道侧拿到空文本会静默丢弃，用户看到的是对话
    毫无征兆地中断。
    """
    recorded: list = []

    async def record(message, response):
        recorded.append(message)

    class EmptyResponseProtocol(ToolLoopProtocol):
        first_calls = []
        first_content = ""

    runner = make_runner(EmptyResponseProtocol, monkeypatch, on_message=record)
    events = await collect(runner, AgentStartParams(input="x"))

    assert events[-1].type == "agent_error"
    # 面向终端用户的文案：不出现「工具调用」「中转端点」这类开发者术语
    assert events[-1].error == "我连续两次都没能生成回复，可能是临时故障。请稍后再发一遍试试。"
    # 判定失败前原样重发过一次：共两次模型调用
    assert len(_probe["requests"]) == 2
    # 重试用的是同一份上下文，没有被空响应污染
    assert [m.role for m in _probe["requests"][1].messages] == ["system", "user"]
    # 空的 assistant 消息不能写进会话历史，否则续聊会把空白带上
    assert not [m for m in recorded if m.role == "assistant"]


async def test_empty_response_recovers_on_retry(monkeypatch):
    """空响应多为瞬时故障：重试拿到正常回复就照常收尾，用户无感。"""

    class FlakyProtocol(ToolLoopProtocol):
        """首次调用空回，重试给出正文。"""

        first_calls = []

        async def chat_stream(self, request, model_id):
            _probe["requests"].append(request)
            snap = ChatResponse(model=model_id, provider=self.config.name)
            yield ChatStreamEvent(type="start", partial=snap)
            first_call = len(_probe["requests"]) == 1
            content = "" if first_call else "重试之后的答复"
            if content:
                yield ChatStreamEvent(type="text_delta", delta=content, partial=snap)
            yield ChatStreamEvent(
                type="done",
                partial=ChatResponse(
                    content=content or None,
                    finish_reason="stop",
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=0 if first_call else 6),
                    model=model_id,
                    provider=self.config.name,
                ),
            )

    runner = make_runner(FlakyProtocol, monkeypatch)
    events = await collect(runner, AgentStartParams(input="x"))

    assert events[-1].type == "agent_done"
    assert events[-1].result.text == "重试之后的答复"
    assert len(_probe["requests"]) == 2


async def test_invalid_tool_args_fed_back_as_error(monkeypatch):
    """参数校验失败：不中断循环，错误描述作为失败结果回喂模型。"""

    class BadArgsProtocol(ToolLoopProtocol):
        first_calls = [ToolCall(id="c1", name="search", arguments={"q": 123})]

    runner = make_runner(BadArgsProtocol, monkeypatch)
    events = await collect(runner, AgentStartParams(input="x"))
    tr = next(e.tool_result for e in events if e.type == "tool_result")
    assert tr.is_error
    assert "不符合定义" in tr.output
    # 循环继续走完并正常结束
    assert events[-1].type == "agent_done"
    # 错误文本确实进了第二步的上下文
    assert "不符合定义" in _probe["requests"][1].messages[-1].text()


async def test_tool_handler_exception_fed_back(monkeypatch):
    """工具执行抛异常：转为失败结果回喂，不中断循环。"""

    async def broken_handler(args: dict) -> str:
        raise RuntimeError("站点连接超时")

    runner = make_runner(ToolLoopProtocol, monkeypatch, handler=broken_handler)
    events = await collect(runner, AgentStartParams(input="x"))
    tr = next(e.tool_result for e in events if e.type == "tool_result")
    assert tr.is_error
    assert "站点连接超时" in tr.output
    assert events[-1].type == "agent_done"


async def test_max_steps_guard(monkeypatch):
    """模型永远要求调工具 → 达到步数上限后以 agent_error 明确终止。"""

    class ForeverProtocol(ToolLoopProtocol):
        async def chat_stream(self, request, model_id):
            _probe["requests"].append(request)
            snap = ChatResponse(model=model_id, provider=self.config.name)
            tc = ToolCall(id=f"c{len(_probe['requests'])}", name="search", arguments={"q": "x"})
            yield ChatStreamEvent(type="toolcall_end", tool_call=tc, partial=snap)
            yield ChatStreamEvent(
                type="done",
                partial=ChatResponse(
                    tool_calls=[tc], finish_reason="tool_calls",
                    model=model_id, provider=self.config.name,
                ),
            )

    runner = make_runner(ForeverProtocol, monkeypatch, max_steps=3)
    events = await collect(runner, AgentStartParams(input="x"))
    assert events[-1].type == "agent_error"
    assert "最大执行步数上限（3 步）" in events[-1].error
    assert sum(1 for e in events if e.type == "tool_result") == 3


async def test_stream_error_ends_with_agent_error(monkeypatch):
    class BrokenProtocol(ToolLoopProtocol):
        async def chat_stream(self, request, model_id):
            snap = ChatResponse(model=model_id, provider=self.config.name)
            yield ChatStreamEvent(type="start", partial=snap)
            yield ChatStreamEvent(type="text_delta", delta="部分", partial=snap)
            yield ChatStreamEvent(
                type="error",
                partial=ChatResponse(
                    content="部分", finish_reason="error", error="连接模型服务失败"
                ),
            )

    runner = make_runner(BrokenProtocol, monkeypatch)
    events = await collect(runner, AgentStartParams(input="x"))
    assert [e.type for e in events] == ["agent_start", "text_delta", "agent_error"]
    assert "连接模型服务失败" in events[-1].error


async def test_routing_failure_yields_agent_error_without_start():
    events = [
        e
        async for e in AgentRunner(LlmRouter([])).start(AgentStartParams(input="x"))
    ]
    assert [e.type for e in events] == ["agent_error"]
    assert "没有任何已启用" in events[0].error


# ---------------------------------------------------------------------------
# 自动压缩（触发判定的纯函数见 test_compaction.py，这里测 loop 集成）
# ---------------------------------------------------------------------------


def make_compact_runner(protocol_cls, monkeypatch, *, model_info: ModelInfo) -> AgentRunner:
    """带小上下文窗口自定义模型的 runner；压缩产物记入 _probe["compactions"]。"""
    monkeypatch.setitem(PROTOCOLS, "openai_chat", protocol_cls)
    _probe.clear()
    _probe["requests"] = []
    _probe["compactions"] = []

    async def handler(args: dict) -> str:
        # 大体积工具输出：压缩后被丢弃，tokens_before 因此显著大于 tokens_after
        return "找到 3 条资源：" + "A" * 4000

    async def on_compaction(result) -> None:
        _probe["compactions"].append(result)

    router = LlmRouter(
        [
            LlmProviderConfig(
                name="测试百炼",
                provider_type="bailian",
                api_key="sk-x",
                default_model=model_info.id,
                extra_models=[model_info],
                is_default=True,
            )
        ]
    )
    tools = [AgentTool(definition=SEARCH_TOOL_DEF, handler=handler)]
    return AgentRunner(router, tools=tools, on_compaction=on_compaction)


class CompactionProtocol(ToolLoopProtocol):
    """三段流：首步高用量的工具调用 → 压缩请求回摘要 → 见到摘要给终答。"""

    #: 首步上报的 prompt 用量（窗口 1000、阈值 900：默认触发 mid-run 压缩）
    first_prompt_tokens = 950

    async def chat_stream(self, request, model_id):
        _probe["requests"].append(request)
        snap = ChatResponse(model=model_id, provider=self.config.name)
        yield ChatStreamEvent(type="start", partial=snap)
        if request.tools is None:
            # 压缩请求（无工具定义）：回一份交接摘要
            yield ChatStreamEvent(
                type="done",
                partial=ChatResponse(
                    content="这是交接摘要",
                    finish_reason="stop",
                    model=model_id,
                    provider=self.config.name,
                ),
            )
            return
        has_summary = any(
            m.role == "user" and m.text().startswith(SUMMARY_PREFIX) for m in request.messages
        )
        has_tool_msg = any(m.role == "tool" for m in request.messages)
        if has_summary or has_tool_msg:
            yield ChatStreamEvent(type="text_delta", delta="最终答复", partial=snap)
            yield ChatStreamEvent(
                type="done",
                partial=ChatResponse(
                    content="最终答复",
                    finish_reason="stop",
                    usage=TokenUsage(prompt_tokens=20, completion_tokens=7, total_tokens=27),
                    model=model_id,
                    provider=self.config.name,
                ),
            )
            return
        tc = ToolCall(id="c1", name="search", arguments={"q": "沙丘"})
        yield ChatStreamEvent(type="toolcall_end", tool_call=tc, partial=snap)
        yield ChatStreamEvent(
            type="done",
            partial=ChatResponse(
                tool_calls=[tc],
                finish_reason="tool_calls",
                usage=TokenUsage(
                    prompt_tokens=self.first_prompt_tokens,
                    completion_tokens=5,
                    total_tokens=self.first_prompt_tokens + 5,
                ),
                model=model_id,
                provider=self.config.name,
            ),
        )


async def test_mid_run_auto_compact(monkeypatch):
    """mid-run 压缩：工具结果回喂后超水位 → 压缩 → 用重建历史继续循环。"""
    runner = make_compact_runner(
        CompactionProtocol, monkeypatch, model_info=ModelInfo(id="tiny", context_window=1000)
    )
    events = await collect(runner, AgentStartParams(input="找沙丘"))
    types = [e.type for e in events]
    # 压缩事件出现在工具回执之后、终态之前
    assert types.index("tool_result") < types.index("context_compacted") < types.index("agent_done")
    compacted = next(e for e in events if e.type == "context_compacted")
    assert compacted.compaction.summary == "这是交接摘要"
    assert compacted.compaction.tokens_before > compacted.compaction.tokens_after

    # 请求序列：步 1（带工具）→ 压缩（无工具、末尾是压缩指令、含完整现场）→ 步 2
    step1, compact_req, step2 = _probe["requests"]
    assert compact_req.tools is None
    assert compact_req.messages[-1].text() == COMPACT_PROMPT
    assert any(m.role == "tool" for m in compact_req.messages)
    # 步 2 的上下文已重建：system + 保留的用户原话 + 摘要，零 assistant/tool
    assert [m.role for m in step2.messages] == ["system", "user", "user"]
    assert step2.messages[1].text() == "找沙丘"
    assert step2.messages[2].text() == f"{SUMMARY_PREFIX}\n这是交接摘要"
    # 压缩回调收到的替换历史与步 2 的上下文（去掉 system）一致
    assert len(_probe["compactions"]) == 1
    assert _probe["compactions"][0].replacement_history == step2.messages[1:]


async def test_pre_run_compact_on_oversized_history(monkeypatch):
    """pre-run 压缩：续聊历史冷启动即超水位（字节估算）→ 首个请求就是压缩。"""
    runner = make_compact_runner(
        CompactionProtocol, monkeypatch, model_info=ModelInfo(id="tiny", context_window=1000)
    )
    huge = ChatMessage(role="user", content="x" * 4000)  # ≈1000 token ≥ 900 水位
    events = await collect(runner, AgentStartParams(input="继续", history=[huge]))
    assert [e.type for e in events][:2] == ["agent_start", "context_compacted"]
    assert _probe["requests"][0].tools is None
    # 压缩后模型看到摘要，直接给出终答
    assert events[-1].type == "agent_done"


async def test_compact_failure_degrades_and_run_continues(monkeypatch):
    """压缩请求失败 → 跳过压缩继续运行，任务照常完成。"""

    class FailingCompactProtocol(CompactionProtocol):
        async def chat_stream(self, request, model_id):
            if request.tools is None:
                _probe["requests"].append(request)
                yield ChatStreamEvent(
                    type="error",
                    partial=ChatResponse(finish_reason="error", error="模型服务不可用"),
                )
                return
            async for event in super().chat_stream(request, model_id):
                yield event

    runner = make_compact_runner(
        FailingCompactProtocol, monkeypatch, model_info=ModelInfo(id="tiny", context_window=1000)
    )
    events = await collect(runner, AgentStartParams(input="找沙丘"))
    assert all(e.type != "context_compacted" for e in events)
    assert events[-1].type == "agent_done"
    assert _probe["compactions"] == []


async def test_no_context_window_disables_auto_compact(monkeypatch):
    """模型未声明窗口：高用量也不发压缩请求（宁可放行到供应商报错）。"""
    runner = make_compact_runner(
        CompactionProtocol, monkeypatch, model_info=ModelInfo(id="nowin")
    )
    events = await collect(runner, AgentStartParams(input="找沙丘"))
    assert all(e.type != "context_compacted" for e in events)
    assert all(request.tools is not None for request in _probe["requests"])
    assert events[-1].type == "agent_done"


async def test_system_prompt_override_and_history(monkeypatch):
    """system_prompt 覆盖生效；history 按序插在 system 与本轮 input 之间。"""
    runner = make_runner(ToolLoopProtocol, monkeypatch)
    params = AgentStartParams(
        input="本轮问题",
        system_prompt="自定义系统词",
        history=[
            ChatMessage(role="user", content="上轮问题"),
            ChatMessage(role="assistant", content="上轮回答"),
        ],
    )
    await collect(runner, params)
    first = _probe["requests"][0]
    assert [m.role for m in first.messages] == ["system", "user", "assistant", "user"]
    assert first.messages[0].text() == "自定义系统词"
    assert first.messages[-1].text() == "本轮问题"
    # 工具声明随请求下发
    assert first.tools[0].name == "search"
