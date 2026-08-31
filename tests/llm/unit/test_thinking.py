"""思维链强度：菜单推导、三种方言翻译、回落语义与 extra_body 优先级。

对应 docs/design/agent-thinking-level.md 的核心规则：
- 菜单按模型声明裁剪，词汇表外/off 不可声明；
- effort 直传 / budget 按 max_thinking_tokens 分段 / toggle 仅关；
- 菜单不含所选档位 → 回落默认（不发参数），绝不就近取整；
- 用户显式 extra_body 覆盖翻译结果（逃生舱优先）。
"""

from __future__ import annotations

from movieclaw_llm import (
    ChatMessage,
    ChatRequest,
    LlmProviderConfig,
    ModelInfo,
    ModelSettings,
    ThinkingControl,
)
from movieclaw_llm.protocols.openai_chat import OpenAIChatProtocol
from movieclaw_llm.providers import get_preset
from movieclaw_llm.thinking import resolve_thinking_level, thinking_body_fragment

EFFORT_MODEL = ModelInfo(
    id="kimi-k3",
    thinking_control=ThinkingControl(kind="effort", levels=["low", "high", "max"]),
)
BUDGET_MODEL = ModelInfo(
    id="qwen3.7-max",
    max_thinking_tokens=80_000,
    thinking_control=ThinkingControl(kind="budget", supports_off=True),
)
TOGGLE_MODEL = ModelInfo(
    id="glm-5",
    thinking_control=ThinkingControl(kind="toggle", supports_off=True),
)
PLAIN_MODEL = ModelInfo(id="deepseek-chat", supports_thinking=True)


# ---------------------------------------------------------------------------
# 菜单推导
# ---------------------------------------------------------------------------


def test_menu_per_dialect() -> None:
    assert EFFORT_MODEL.thinking_levels == ["low", "high", "max"]  # 无 off：未声明关闭协议
    assert BUDGET_MODEL.thinking_levels == ["off", "low", "medium", "high"]
    assert TOGGLE_MODEL.thinking_levels == ["off"]
    # supports_thinking 只表示会输出思考内容，不兼职暗示可控性
    assert PLAIN_MODEL.thinking_levels == []


def test_budget_without_max_tokens_degrades_to_toggle() -> None:
    degraded = ModelInfo(
        id="m", thinking_control=ThinkingControl(kind="budget", supports_off=True)
    )
    assert degraded.thinking_levels == ["off"]  # 没有预算可分段


def test_declaration_drops_unknown_and_off_and_reorders() -> None:
    control = ThinkingControl(kind="effort", levels=["max", "turbo", "off", "low"])
    assert control.levels == ["low", "max"]  # 词汇表固定顺序；turbo/off 丢弃


# ---------------------------------------------------------------------------
# 翻译与回落
# ---------------------------------------------------------------------------


def test_effort_translates_verbatim() -> None:
    assert thinking_body_fragment("max", EFFORT_MODEL, "kimi-k3") == {"reasoning_effort": "max"}


def test_budget_translates_ratio_segments() -> None:
    assert thinking_body_fragment("low", BUDGET_MODEL, "q") == {
        "enable_thinking": True,
        "thinking_budget": 20_000,
    }
    assert thinking_body_fragment("medium", BUDGET_MODEL, "q")["thinking_budget"] == 40_000
    assert thinking_body_fragment("high", BUDGET_MODEL, "q")["thinking_budget"] == 80_000
    assert thinking_body_fragment("off", BUDGET_MODEL, "q") == {"enable_thinking": False}


def test_toggle_translates_off_only() -> None:
    assert thinking_body_fragment("off", TOGGLE_MODEL, "glm-5") == {
        "thinking": {"type": "disabled"}
    }


def test_unsupported_level_falls_back_to_default_not_nearest() -> None:
    # kimi-k3 菜单无 medium：回落默认（空 dict），绝不就近映射为 low/high
    assert resolve_thinking_level("medium", EFFORT_MODEL) is None
    assert thinking_body_fragment("medium", EFFORT_MODEL, "kimi-k3") == {}
    # 目录外模型 / 未声明控制的模型同样 fail-closed
    assert thinking_body_fragment("high", None, "unknown") == {}
    assert thinking_body_fragment("high", PLAIN_MODEL, "deepseek-chat") == {}


# ---------------------------------------------------------------------------
# 协议层集成：extra_body 通道与用户优先级
# ---------------------------------------------------------------------------


def make_protocol(extra_models: list[ModelInfo]) -> OpenAIChatProtocol:
    config = LlmProviderConfig(
        name="测试实例", provider_type="openai_compat", api_key="sk-t",
        base_url="https://example.com/v1", extra_models=extra_models,
    )
    return OpenAIChatProtocol(config, get_preset("openai_compat"))


def payload_for(protocol: OpenAIChatProtocol, settings: ModelSettings, model_id: str) -> dict:
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="你好")], settings=settings
    )
    return protocol._build_payload(request, model_id, stream=False)


def test_payload_carries_translated_thinking_in_extra_body() -> None:
    p = make_protocol([EFFORT_MODEL])
    payload = payload_for(p, ModelSettings(thinking_level="high"), "kimi-k3")
    assert payload["extra_body"] == {"reasoning_effort": "high"}


def test_user_extra_body_overrides_translation() -> None:
    p = make_protocol([BUDGET_MODEL])
    settings = ModelSettings(
        thinking_level="low",
        extra_body={"thinking_budget": 12345, "enable_search": True},
    )
    payload = payload_for(p, settings, "qwen3.7-max")
    # 用户显式值优先；翻译补上用户没写的键
    assert payload["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 12345,
        "enable_search": True,
    }


def test_no_level_no_extra_body() -> None:
    p = make_protocol([EFFORT_MODEL])
    assert "extra_body" not in payload_for(p, ModelSettings(), "kimi-k3")


def test_unknown_model_level_dropped_from_payload() -> None:
    p = make_protocol([])
    payload = payload_for(p, ModelSettings(thinking_level="high"), "mystery-model")
    assert "extra_body" not in payload
