"""LlmRouter —— 模型路由与请求分发（小网关）。

未来所有 LLM 调用的唯一入口。职责：

1. 路由解析：ChatRequest.model 支持三种写法——
   - ``实例名/模型id``（如 ``我的百炼/qwen-max``）：精确路由，模型不要求在目录里；
   - 裸模型 id（如 ``qwen-max``）：在启用实例的模型目录里查归属；
   - 空串 / ``default``：默认实例 + 其配置的默认模型；
2. 实例缓存：按配置构建协议客户端并缓存，配置变更后重建；
3. 调用审计：每次调用记录实例/模型/耗时/token 用量的中文日志。

明确不做：负载均衡、多供应商自动故障转移、语义缓存。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from movieclaw_llm.base import BaseLlmProtocol
from movieclaw_llm.exceptions import LlmError, LlmRoutingError
from movieclaw_llm.models import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    LlmProviderConfig,
    ModelInfo,
)
from movieclaw_llm.protocols import PROTOCOLS
from movieclaw_llm.providers import get_preset

logger = logging.getLogger(__name__)


class LlmRouter:
    def __init__(self, providers: list[LlmProviderConfig] | None = None) -> None:
        self._providers: list[LlmProviderConfig] = list(providers or [])
        # 实例名 → (配置指纹, 协议客户端)；指纹变了说明配置被改过，需重建
        self._clients: dict[str, tuple[str, BaseLlmProtocol]] = {}

    # -- 配置管理 -----------------------------------------------------------

    async def update_providers(self, providers: list[LlmProviderConfig]) -> None:
        """整体替换供应商配置（上层在 DB 配置变更后调用）。"""
        self._providers = list(providers)
        alive = {p.name for p in providers}
        for name in list(self._clients):
            if name not in alive:
                _, client = self._clients.pop(name)
                await client.close()

    def _catalog(self, config: LlmProviderConfig) -> list[ModelInfo]:
        """预设目录 + 用户补录，用户条目按 id 覆盖预设。"""
        merged = {m.id: m for m in get_preset(config.provider_type).models}
        merged.update({m.id: m for m in config.extra_models})
        return list(merged.values())

    def _enabled_providers(self) -> list[LlmProviderConfig]:
        # 默认实例排最前：裸模型 id 在多实例都有时优先命中默认实例
        return sorted(
            (p for p in self._providers if p.enabled),
            key=lambda p: not p.is_default,
        )

    def _default_provider(self) -> LlmProviderConfig:
        enabled = self._enabled_providers()
        if not enabled:
            raise LlmRoutingError("没有任何已启用的模型供应商，请先在设置中接入")
        return enabled[0]

    # -- 路由解析 -----------------------------------------------------------

    def resolve(self, model_ref: str) -> tuple[LlmProviderConfig, str]:
        """解析 model 引用 → (供应商实例配置, 具体模型 id)。"""
        ref = (model_ref or "").strip()
        if not ref or ref == "default":
            provider = self._default_provider()
            if not provider.default_model:
                raise LlmRoutingError(
                    f"供应商「{provider.name}」未配置默认模型，"
                    "请求需显式指定 model 或补全配置"
                )
            return provider, provider.default_model

        if "/" in ref:
            name, _, model_id = ref.partition("/")
            for provider in self._providers:
                if provider.name == name:
                    if not provider.enabled:
                        raise LlmRoutingError(f"供应商「{name}」已被停用")
                    # 显式指定实例时信任用户，模型不要求在目录里
                    return provider, model_id
            raise LlmRoutingError(f"找不到名为「{name}」的供应商实例")

        owners = [
            p for p in self._enabled_providers() if any(m.id == ref for m in self._catalog(p))
        ]
        if owners:
            if len(owners) > 1:
                # 多实例目录里都有这个 id：按默认实例优先的顺序命中。对话框选模型
                # 时前端会对冲突 id 改用「实例名/模型id」，走不到这里；API / CLI
                # 直接传裸 id 才会，记日志让用户知道实际走了哪家
                logger.info(
                    "模型「%s」在 %d 个供应商实例目录中都存在，按默认优先路由到「%s」",
                    ref, len(owners), owners[0].name,
                )
            return owners[0], ref
        raise LlmRoutingError(
            f"模型「{ref}」不在任何已启用供应商的目录中；"
            "如确认端点支持该模型，请用「实例名/模型id」显式指定"
        )

    def get_model_info(self, model_ref: str) -> ModelInfo:
        """取模型元数据（上下文窗口、能力标志），供 agent 做预算与决策。"""
        provider, model_id = self.resolve(model_ref)
        for m in self._catalog(provider):
            if m.id == model_id:
                return m
        # 显式指定的目录外模型：返回只含 id 的最小条目
        return ModelInfo(id=model_id)

    # -- 客户端构建 ---------------------------------------------------------

    def _client_for(self, config: LlmProviderConfig) -> BaseLlmProtocol:
        fingerprint = config.model_dump_json()
        cached = self._clients.get(config.name)
        if cached and cached[0] == fingerprint:
            return cached[1]
        preset = get_preset(config.provider_type)
        protocol_cls = PROTOCOLS.get(preset.protocol)
        if protocol_cls is None:
            raise LlmRoutingError(f"预设「{preset.id}」引用了未实现的协议「{preset.protocol}」")
        client = protocol_cls(config, preset)
        self._clients[config.name] = (fingerprint, client)
        return client

    # -- 调用入口 -----------------------------------------------------------

    async def chat(self, request: ChatRequest) -> ChatResponse:
        provider, model_id = self.resolve(request.model)
        client = self._client_for(provider)
        started = time.monotonic()
        try:
            response = await client.chat(request, model_id)
        except LlmError as exc:
            logger.warning(
                "LLM 调用失败 provider=%s model=%s 耗时=%.2fs：%s",
                provider.name, model_id, time.monotonic() - started, exc,
            )
            raise
        self._audit(provider.name, model_id, started, response)
        return response

    async def chat_stream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        provider, model_id = self.resolve(request.model)
        client = self._client_for(provider)
        started = time.monotonic()
        async for event in client.chat_stream(request, model_id):
            yield event
            if event.type in ("done", "error"):
                self._audit(provider.name, model_id, started, event.partial)

    async def aclose(self) -> None:
        for _, client in self._clients.values():
            await client.close()
        self._clients.clear()

    @staticmethod
    def _audit(provider: str, model: str, started: float, response: ChatResponse) -> None:
        logger.info(
            "LLM 调用完成 provider=%s model=%s 耗时=%.2fs tokens=%d/%d 结束原因=%s",
            provider,
            model,
            time.monotonic() - started,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            response.finish_reason,
        )
