"""openai_chat 协议：消息互转、流式累积、错误翻译（全部 mock，不打真实 API）。"""

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from movieclaw_llm import (
    ChatMessage,
    ChatRequest,
    ImagePart,
    LlmAuthError,
    LlmConnectError,
    LlmContentFilterError,
    LlmProviderConfig,
    LlmRateLimitError,
    ModelSettings,
    TextPart,
    ThinkingPart,
    ToolCall,
    ToolDefinition,
)
from movieclaw_llm.protocols.openai_chat import OpenAIChatProtocol
from movieclaw_llm.providers import get_preset


def make_protocol(provider_type: str = "bailian") -> OpenAIChatProtocol:
    config = LlmProviderConfig(name="测试实例", provider_type=provider_type, api_key="sk-test")
    return OpenAIChatProtocol(config, get_preset(provider_type))


# -- 客户端构建 -------------------------------------------------------------


def test_default_user_agent_is_sdk_builtin():
    """不配 user_agent → 保持 openai SDK 自带 UA，不做任何干预。"""
    p = make_protocol()
    assert p._client.default_headers["User-Agent"].startswith("AsyncOpenAI/Python")


def test_custom_user_agent_overrides_sdk_default():
    """配了 user_agent → 覆盖 SDK 自带 UA（自建网关按 UA 放行的场景）。"""
    config = LlmProviderConfig(
        name="自建网关",
        provider_type="openai_compat",
        api_key="sk-test",
        base_url="http://192.168.1.5:8000/v1",
        user_agent="movieclaw/1.0",
    )
    p = OpenAIChatProtocol(config, get_preset("openai_compat"))
    assert p._client.default_headers["User-Agent"] == "movieclaw/1.0"


# -- 请求转换 ---------------------------------------------------------------


def test_convert_plain_text_messages():
    p = make_protocol()
    payload = p._build_payload(
        ChatRequest(messages=[
            ChatMessage(role="system", content="你是助手"),
            ChatMessage(role="user", content="你好"),
        ]),
        "qwen-plus",
        stream=False,
    )
    assert payload["messages"] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
    ]


def test_convert_multimodal_user_message():
    p = make_protocol()
    msg = ChatMessage(
        role="user",
        content=[TextPart(text="识别海报"), ImagePart(data="QUJD", media_type="image/png")],
    )
    converted = p._convert_message(msg)
    assert converted["content"] == [
        {"type": "text", "text": "识别海报"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]


def test_convert_all_text_parts_flattened_to_string():
    p = make_protocol()
    msg = ChatMessage(role="user", content=[TextPart(text="a"), TextPart(text="b")])
    assert p._convert_message(msg)["content"] == "ab"


def test_convert_unhydrated_image_ref_degrades_to_placeholder_text():
    """只有 attachment_id 的引用块（水合层漏了）：占位文本兜底，绝不发内部引用。"""
    p = make_protocol()
    msg = ChatMessage(
        role="user",
        content=[
            TextPart(text="看这张图"),
            ImagePart(attachment_id="a" * 32, name="截图.png", media_type="image/png"),
        ],
    )
    # 占位后不再有图片块，整条回落为纯字符串形态
    assert p._convert_message(msg)["content"] == "看这张图[图片未能加载，已略过]"


def test_convert_hydrated_image_ref_sends_data_url():
    """已水合的引用块（attachment_id 与 data 并存）：正常按 data URL 发送。"""
    p = make_protocol()
    msg = ChatMessage(
        role="user",
        content=[ImagePart(attachment_id="a" * 32, data="QUJD", media_type="image/jpeg")],
    )
    assert p._convert_message(msg)["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
    ]


def test_convert_assistant_history_drops_thinking_keeps_raw_tool_args():
    p = make_protocol()
    msg = ChatMessage(
        role="assistant",
        content=[ThinkingPart(text="内心戏"), TextPart(text="结论")],
        tool_calls=[ToolCall(id="c1", name="search", raw_arguments='{"q": "x"}')],
    )
    converted = p._convert_message(msg)
    assert converted["content"] == "结论"
    assert converted["tool_calls"][0]["function"]["arguments"] == '{"q": "x"}'


def test_convert_tool_result_message():
    p = make_protocol()
    msg = ChatMessage(role="tool", content="执行结果", tool_call_id="c1", name="search")
    assert p._convert_message(msg) == {
        "role": "tool",
        "content": "执行结果",
        "tool_call_id": "c1",
    }


def test_build_payload_settings_tools_and_stream_options():
    p = make_protocol()
    payload = p._build_payload(
        ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            tools=[ToolDefinition(name="f", description="d", parameters={"type": "object"})],
            tool_choice="f",
            settings=ModelSettings(
                temperature=0.2,
                max_tokens=100,
                response_format="json_object",
                extra_body={"enable_search": True},
            ),
        ),
        "qwen-plus",
        stream=True,
    )
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 100
    assert "top_p" not in payload  # None 不下发
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["extra_body"] == {"enable_search": True}
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "f"}}
    assert payload["stream_options"] == {"include_usage": True}


# -- 响应解析 ---------------------------------------------------------------


def completion_fixture() -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-r1",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "结论",
                        "reasoning_content": "推理过程",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "search", "arguments": '{"q": "沙丘"}'},
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "search", "arguments": "{broken"},
                            },
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        }
    )


class FakeClient:
    """替身客户端：chat.completions.create 返回预置结果。"""

    class _Completions:
        def __init__(self, result):
            self._result = result

        async def create(self, **kwargs):
            if isinstance(self._result, Exception):
                raise self._result
            if callable(self._result):
                return self._result()
            return self._result

    def __init__(self, result):
        self.chat = type("C", (), {})()
        self.chat.completions = self._Completions(result)

    async def close(self):
        pass


async def test_chat_parses_thinking_tool_calls_and_usage():
    p = make_protocol()
    p._client = FakeClient(completion_fixture())
    request = ChatRequest(messages=[ChatMessage(role="user", content="q")])
    resp = await p.chat(request, "deepseek-r1")
    assert resp.content == "结论"
    assert resp.thinking == "推理过程"
    assert resp.finish_reason == "tool_calls"
    assert resp.provider == "测试实例"
    assert resp.tool_calls[0].arguments == {"q": "沙丘"}
    assert resp.tool_calls[1].parse_error is not None
    assert resp.tool_calls[1].raw_arguments == "{broken"
    assert resp.usage.total_tokens == 15
    assert resp.usage.cache_read_tokens == 3


def test_openai_preset_ignores_reasoning_field():
    p = make_protocol("openai")
    completion = completion_fixture()
    assert p._parse_response(completion, "gpt-4o").thinking is None


# -- 流式 -------------------------------------------------------------------


def chunk(payload: dict) -> ChatCompletionChunk:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    return ChatCompletionChunk.model_validate(base | payload)


def stream_chunks():
    yield chunk({"choices": [{"index": 0, "delta": {"reasoning_content": "想想"}}]})
    yield chunk({"choices": [{"index": 0, "delta": {"content": "你好"}}]})
    yield chunk({"choices": [{"index": 0, "delta": {"content": "！"}}]})
    yield chunk(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "search", "arguments": '{"q"'},
                            }
                        ]
                    },
                }
            ]
        }
    )
    yield chunk(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "x"}'}}]},
                }
            ]
        }
    )
    yield chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    yield chunk(
        {
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
        }
    )


async def collect_events(p, gen_factory):
    async def agen():
        for c in gen_factory():
            yield c

    p._client = FakeClient(agen)
    events = []
    async for e in p.chat_stream(
        ChatRequest(messages=[ChatMessage(role="user", content="q")]), "qwen-plus"
    ):
        events.append(e)
    return events


async def test_stream_event_sequence_and_final_partial():
    events = await collect_events(make_protocol(), stream_chunks)
    assert [e.type for e in events] == [
        "start",
        "thinking_delta",
        "text_delta",
        "text_delta",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]
    final = events[-1].partial
    assert final.content == "你好！"
    assert final.thinking == "想想"
    assert final.finish_reason == "tool_calls"
    assert final.tool_calls[0].arguments == {"q": "x"}
    assert final.usage.total_tokens == 11
    # 每个事件的 partial 都是可用快照：中途任一时刻已含累积内容
    assert events[3].partial.content == "你好！"
    # toolcall_delta 带所属调用的快照，消费方靠它归属增量
    deltas = [e for e in events if e.type == "toolcall_delta"]
    assert all(e.tool_call.id == "call_1" and e.tool_call.name == "search" for e in deltas)
    assert "".join(e.delta for e in deltas) == '{"q": "x"}'


async def test_stream_error_ends_with_error_event_preserving_partial():
    def broken():
        yield chunk({"choices": [{"index": 0, "delta": {"content": "部分"}}]})
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

    events = await collect_events(make_protocol(), broken)
    assert events[-1].type == "error"
    assert events[-1].partial.finish_reason == "error"
    assert events[-1].partial.content == "部分"
    assert "连接模型服务失败" in events[-1].partial.error


# -- 错误翻译 ---------------------------------------------------------------


def http_error(cls, status: int, body=None, headers=None):
    request = httpx.Request("POST", "http://x")
    response = httpx.Response(status, request=request, headers=headers or {})
    return cls("boom", response=response, body=body)


def test_error_translation():
    p = make_protocol()
    assert isinstance(
        p._translate_error(http_error(openai.AuthenticationError, 401)), LlmAuthError
    )
    rate = p._translate_error(http_error(openai.RateLimitError, 429, headers={"retry-after": "2"}))
    assert isinstance(rate, LlmRateLimitError)
    assert rate.retryable and rate.retry_after == 2.0
    assert isinstance(
        p._translate_error(openai.APIConnectionError(request=httpx.Request("POST", "http://x"))),
        LlmConnectError,
    )
    filtered = p._translate_error(
        http_error(openai.BadRequestError, 400, body={"code": "data_inspection_failed"})
    )
    assert isinstance(filtered, LlmContentFilterError)


def test_error_message_contains_provider_and_chinese_hint():
    p = make_protocol()
    err = p._translate_error(http_error(openai.AuthenticationError, 401))
    assert "测试实例" in str(err)
    assert "API Key" in str(err)


@pytest.mark.parametrize("provider_type", ["openai", "bailian", "openai_compat"])
def test_protocol_constructs_for_all_presets(provider_type):
    # openai_compat 无预设 base_url 时也能构造（用户覆盖前 SDK 用官方默认）
    make_protocol(provider_type)


# -- Harmony 残留告警 -------------------------------------------------------


def test_content_with_harmony_residue_logs_warning(caplog):
    """正文泄漏 Harmony 标记（后端解析失败的特征）→ 打中文告警引导修后端。"""
    from movieclaw_llm.protocols.openai_chat import _warn_harmony_residue_in_content

    with caplog.at_level("WARNING"):
        _warn_harmony_residue_in_content("<|channel|>analysis<|message|>思考内容……")
    assert any("Harmony" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level("WARNING"):
        _warn_harmony_residue_in_content("正常的回答文本")
        _warn_harmony_residue_in_content(None)
    assert not caplog.records
