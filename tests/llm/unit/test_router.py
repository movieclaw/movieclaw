"""LlmRouter：路由解析、目录合并、经假协议的完整调用链。"""

import pytest

from movieclaw_llm import (
    ChatRequest,
    ChatResponse,
    LlmProviderConfig,
    LlmRouter,
    LlmRoutingError,
    ModelInfo,
)
from movieclaw_llm.base import BaseLlmProtocol
from movieclaw_llm.protocols import PROTOCOLS


def make_router() -> LlmRouter:
    return LlmRouter(
        [
            LlmProviderConfig(
                name="官方OpenAI",
                provider_type="openai",
                api_key="sk-a",
                default_model="gpt-4o-mini",
                is_default=True,
            ),
            LlmProviderConfig(
                name="我的百炼",
                provider_type="bailian",
                api_key="sk-b",
                default_model="qwen-plus",
                extra_models=[ModelInfo(id="qwen3-235b", context_window=131072)],
            ),
            LlmProviderConfig(
                name="停用实例", provider_type="openai_compat", api_key="sk-c", enabled=False
            ),
        ]
    )


def test_resolve_explicit_provider_and_model():
    provider, model = make_router().resolve("我的百炼/qwen-max")
    assert provider.name == "我的百炼"
    assert model == "qwen-max"


def test_resolve_explicit_allows_uncataloged_model():
    provider, model = make_router().resolve("我的百炼/some-new-model")
    assert model == "some-new-model"


def test_resolve_bare_model_by_catalog_ownership():
    provider, model = make_router().resolve("qwen3.7-max")
    assert provider.name == "我的百炼"


def test_resolve_bare_model_finds_extra_models():
    provider, model = make_router().resolve("qwen3-235b")
    assert provider.name == "我的百炼"


def test_resolve_default():
    provider, model = make_router().resolve("")
    assert provider.name == "官方OpenAI"
    assert model == "gpt-4o-mini"


def test_resolve_disabled_provider_rejected():
    with pytest.raises(LlmRoutingError, match="已被停用"):
        make_router().resolve("停用实例/whatever")


def test_resolve_unknown_bare_model_suggests_explicit_form():
    with pytest.raises(LlmRoutingError, match="实例名/模型id"):
        make_router().resolve("no-such-model")


def test_resolve_no_providers():
    with pytest.raises(LlmRoutingError, match="没有任何已启用"):
        LlmRouter([]).resolve("")


def test_get_model_info_extra_overrides_and_fallback():
    router = make_router()
    assert router.get_model_info("qwen3-235b").context_window == 131072
    # 目录外显式指定：返回只含 id 的最小条目
    assert router.get_model_info("我的百炼/mystery").context_window is None


class FakeProtocol(BaseLlmProtocol):
    """记录收到的 model_id，返回固定响应。"""

    async def chat(self, request, model_id):
        return ChatResponse(
            content=f"echo:{model_id}", finish_reason="stop", provider=self.config.name
        )

    async def chat_stream(self, request, model_id):  # pragma: no cover - 本测试不用
        yield None

    async def test_connection(self):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass


async def test_chat_dispatches_to_resolved_provider(monkeypatch):
    monkeypatch.setitem(PROTOCOLS, "openai_chat", FakeProtocol)
    router = make_router()
    response = await router.chat(ChatRequest(model="qwen3.7-max", messages=[]))
    assert response.content == "echo:qwen3.7-max"
    assert response.provider == "我的百炼"
    await router.aclose()


async def test_update_providers_drops_removed_instances(monkeypatch):
    monkeypatch.setitem(PROTOCOLS, "openai_chat", FakeProtocol)
    router = make_router()
    await router.chat(ChatRequest(model="qwen3.7-max", messages=[]))
    assert "我的百炼" in router._clients
    await router.update_providers([])
    assert router._clients == {}


def test_resolve_explicit_ref_with_slash_in_model_id():
    """实例名不含斜杠，所以按第一个斜杠拆分：模型 id 自己带斜杠也能精确路由。"""
    provider, model_id = make_router().resolve("我的百炼/deepseek-ai/DeepSeek-V3")
    assert provider.name == "我的百炼"
    assert model_id == "deepseek-ai/DeepSeek-V3"
