"""LLM 供应商配置服务：多实例配置的读写、连接验证与对话框的模型清单。

与下载器配置（downloader_config）同构：可接入多个实例、有且仅有一个默认。
差异只在验证判据：用 default_model 发一次最小对话（max_tokens=1）——比只调
/models 列表更真实，能一次性证明 key、端点、模型 id 三者都有效；
模型列表另行 best-effort 拉取，仅用于设置页的补录提示，失败不影响结论。

模型选择（对话框「模型」入口）的口径：
- 一个实例接入后，它目录里的全部模型都可选：预设目录 ∪ 用户补录
  （extra_models，按 id 覆盖预设），与 LlmRouter._catalog 同口径；
- 端点上报的 available_models 不进清单——它们没有上下文窗口 / 思考能力
  等元数据，选了之后自动压缩与思维链菜单都会失效；
- 同一模型 id 只在一个实例里有 → 引用就是裸 id、展示也是裸 id；出现在多个
  实例 → 引用用「实例名/模型id」精确路由，展示加括号「模型id（实例名）」。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
)
from movieclaw_api.schemas.llm import LlmModelOptionView
from movieclaw_db.engine import get_database
from movieclaw_db.models.llm_provider import LlmProvider
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.llm_provider_repo import LlmProviderRepository
from movieclaw_llm import ChatMessage, ChatRequest, LlmError, LlmRouter, ModelInfo, ModelSettings
from movieclaw_llm.models import LlmProviderConfig, ProviderPreset
from movieclaw_llm.protocols import PROTOCOLS
from movieclaw_llm.providers import get_preset, list_presets

logger = logging.getLogger("movieclaw_api.llm_config")

# 验证用较短超时：只回答"通不通"，没必要等 SDK 默认的十分钟
_TEST_TIMEOUT = 30.0


def to_domain_config(row: LlmProvider, api_key: str) -> LlmProviderConfig:
    """ORM 记录 → movieclaw_llm 领域配置（LlmRouter 按它构建协议客户端）。"""
    return LlmProviderConfig(
        name=row.name,
        provider_type=row.provider_type,
        api_key=api_key,
        base_url=row.base_url,
        default_model=row.default_model,
        extra_models=[ModelInfo.model_validate(m) for m in row.extra_models or []],
        is_default=row.is_default,
        user_agent=row.user_agent,
    )


def provider_catalog(row: LlmProvider) -> list[ModelInfo]:
    """一个实例的可选模型目录：预设目录 + 用户补录，补录按 id 覆盖预设。

    与 LlmRouter._catalog 同口径——清单里出现的模型，路由一定能解析。
    """
    merged = {m.id: m for m in get_preset(row.provider_type).models}
    extras = [ModelInfo.model_validate(m) for m in row.extra_models or []]
    merged.update({m.id: m for m in extras})
    return list(merged.values())


def build_model_options(rows: list[LlmProvider]) -> list[LlmModelOptionView]:
    """把全部实例的目录拍平成对话框的模型清单（rows 须默认实例在前）。

    重复 id 的处理策略：同一模型 id 只在一个实例里有，引用与展示都是裸 id；
    出现在多个实例里，引用改用「实例名/模型id」精确路由（LlmRouter 的显式
    分支），展示加括号「模型id（实例名）」让用户分得清走哪家。
    """
    catalogs = [(row, provider_catalog(row)) for row in rows]
    owners: dict[str, int] = {}
    for _, catalog in catalogs:
        for model in catalog:
            owners[model.id] = owners.get(model.id, 0) + 1
    options: list[LlmModelOptionView] = []
    for row, catalog in catalogs:
        for model in catalog:
            ambiguous = owners[model.id] > 1
            options.append(
                LlmModelOptionView(
                    ref=f"{row.name}/{model.id}" if ambiguous else model.id,
                    label=f"{model.id}（{row.name}）" if ambiguous else model.id,
                    model_id=model.id,
                    provider_id=row.id or 0,
                    provider_name=row.name,
                    is_default=row.is_default and model.id == row.default_model,
                    thinking_levels=model.thinking_levels,
                )
            )
    return options


async def resolve_provider_endpoint(session: AsyncSession) -> tuple[str, str, str | None]:
    """解析默认实例的接入参数，返回 ``(base_url, api_key, user_agent)``。

    端点取值：显式配置 → 预设默认；两者皆空抛 BadRequest。
    user_agent 未配置时为 None（调用方保持自己的默认 UA）。
    这是端点/密钥/UA 解析的唯一判据来源——网络连通性测试（routes/network）
    复用本函数，不允许在别处再实现一遍取值顺序。
    """
    repo = LlmProviderRepository(session)
    row = await repo.get_default()
    if row is None:
        raise BadRequestException("尚未配置 AI 模型供应商，无法测试")
    base = row.base_url or (get_preset(row.provider_type).base_url or "")
    if not base:
        raise BadRequestException(f"供应商「{row.name}」未配置 API 端点地址")
    return base, repo.decrypted_api_key(row) or "", row.user_agent


class LlmConfigService:
    """LLM 供应商实例配置的业务服务。绑定一个数据库会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = LlmProviderRepository(session)

    @staticmethod
    def _assert_not_verifying(row: LlmProvider) -> None:
        """若正在测试连接，拒绝当前操作（409）。"""
        if row.status == ConfigStatus.VERIFYING:
            raise ConflictException(f"「{row.name}」正在测试模型连接，请等待完成后再操作")

    # -- 查询 --------------------------------------------------------------

    async def list_all(self) -> list[LlmProvider]:
        """全部实例，默认实例在前。"""
        return await self._repo.list_all()

    async def get(self, provider_id: int) -> LlmProvider:
        """按 id 取实例；不存在抛 404。"""
        row = await self._repo.get(provider_id)
        if row is None:
            raise NotFoundException("模型供应商实例不存在")
        return row

    async def list_model_options(self) -> list[LlmModelOptionView]:
        """对话框的模型清单（口径见模块说明）。"""
        return build_model_options(await self._repo.list_all())

    # -- 写入 --------------------------------------------------------------

    @staticmethod
    def _assert_default_model_configured(
        preset: ProviderPreset,
        extra_models: list[ModelInfo],
        default_model: str,
    ) -> None:
        """默认模型的严格校验，按供应商类型分两条规则：

        - 有内置目录的供应商（官方渠道）：默认模型必须在目录内，自定义
          模型不参与——官方渠道的模型集合以预设目录为准；
        - 无目录的自定义端点：模型必须在 extra_models 里带完整参数。
          其中「借用」自其它预设目录的模型（按 id 识别）参数随目录，
          豁免手填规则——共享窗口类模型（如 Kimi）本就没有独立输出上限。

        agent 做上下文预算、思考预算、并发决策都依赖这些参数，
        缺参数会让下游全部退化成瞎猜，所以在入口就拦住。
        """
        if preset.models:
            if any(m.id == default_model for m in preset.models):
                return
            raise BadRequestException(
                f"模型「{default_model}」不在「{preset.display_name}」的模型目录中，"
                "请从下拉列表中选择"
            )
        custom = next((m for m in extra_models if m.id == default_model), None)
        if custom is None:
            raise BadRequestException(
                f"模型「{default_model}」不在预设目录中，请先补全它的参数配置"
                "（上下文长度、最大输出等）后再保存"
            )
        # 借用目录模型：任一预设目录里有同 id 条目即豁免手填参数规则
        if any(m.id == custom.id for p in list_presets() for m in p.models):
            return
        if not custom.context_window or not custom.max_output_tokens:
            raise BadRequestException(
                f"自定义模型「{default_model}」缺少必要参数：上下文长度与最大输出为必填"
            )
        if custom.supports_thinking and not custom.max_thinking_tokens:
            raise BadRequestException(
                f"自定义模型「{default_model}」开启了思考模式，必须填写思考预算上限"
            )

    async def _validate(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        default_model: str,
        extra_models: list[ModelInfo],
        exclude_id: int | None = None,
    ) -> None:
        """新增与编辑共用的入参校验：类型存在、端点必填、默认模型合法、实例名唯一。"""
        try:
            preset = get_preset(provider_type)
        except LlmError as exc:
            # 未知供应商类型：领域错误翻译成 400（错误信息本身已含可选项提示）
            raise BadRequestException(str(exc)) from exc
        if preset.requires_base_url and not base_url:
            raise BadRequestException(f"接入「{preset.display_name}」必须填写 API 端点地址")
        self._assert_default_model_configured(preset, extra_models, default_model)
        existing = await self._repo.get_by_name(name)
        if existing is not None and existing.id != exclude_id:
            raise ConflictException(f"实例名「{name}」已被使用，请换一个名字")

    async def create(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        api_key: str,
        default_model: str,
        extra_models: list[ModelInfo] | None = None,
        user_agent: str | None = None,
    ) -> LlmProvider:
        """新增实例。状态置 PENDING，等待后台验证；第一个实例自动成为默认。"""
        extras = extra_models or []
        await self._validate(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            default_model=default_model,
            extra_models=extras,
        )
        return await self._repo.create(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            default_model=default_model,
            extra_models=[m.model_dump() for m in extras] or None,
            user_agent=user_agent,
        )

    async def update(
        self,
        provider_id: int,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        api_key: str,
        default_model: str,
        extra_models: list[ModelInfo] | None = None,
        user_agent: str | None = None,
    ) -> LlmProvider:
        """整体覆盖一个实例的接入配置。不存在抛 404，正在验证中抛 409。"""
        row = await self.get(provider_id)
        self._assert_not_verifying(row)
        extras = extra_models or []
        await self._validate(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            default_model=default_model,
            extra_models=extras,
            exclude_id=provider_id,
        )
        updated = await self._repo.update(
            provider_id,
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            default_model=default_model,
            extra_models=[m.model_dump() for m in extras] or None,
            user_agent=user_agent,
        )
        assert updated is not None  # 上面 get 已确认存在
        return updated

    async def start_verification(self, provider_id: int) -> LlmProvider:
        """同步占位为 VERIFYING 并返回，随后由调用方排队后台测试任务。

        并发守卫原理见 SiteConfigService.start_verification。
        """
        row = await self.get(provider_id)
        self._assert_not_verifying(row)
        await self._repo.update_status(provider_id, ConfigStatus.VERIFYING)
        return await self.get(provider_id)

    async def set_default(self, provider_id: int) -> LlmProvider:
        """把某实例设为全局默认；不存在抛 404。"""
        await self.get(provider_id)
        await self._repo.set_default(provider_id)
        return await self.get(provider_id)

    async def delete(self, provider_id: int) -> None:
        """删除实例；不存在抛 404，正在验证中抛 409。默认让位规则见 Repository。"""
        row = await self.get(provider_id)
        self._assert_not_verifying(row)
        await self._repo.delete(provider_id)


# ---------------------------------------------------------------------------
# 进程级 LlmRouter 单例：所有 LLM 调用（agent 等）的运行时入口
# ---------------------------------------------------------------------------

_runtime_router = LlmRouter()


async def acquire_llm_router(session: AsyncSession) -> LlmRouter:
    """读取全部实例配置并组装进程级 LlmRouter。

    每次取用都重新读配置并 update_providers：配置指纹未变时底层客户端
    缓存直接复用（零开销），变了则自动重建——不需要「配置已修改」的
    显式通知链路。一个实例都没有时抛 404（中文提示引导去设置页）。
    """
    repo = LlmProviderRepository(session)
    rows = await repo.list_all()
    if not rows:
        raise NotFoundException("尚未配置模型供应商，请先在「设置 → AI 模型」中接入")
    configs = [to_domain_config(row, repo.decrypted_api_key(row)) for row in rows]
    await _runtime_router.update_providers(configs)
    return _runtime_router


# ---------------------------------------------------------------------------
# 后台连接测试（与 verify_downloader 同构的背景任务）
# ---------------------------------------------------------------------------


async def verify_llm_provider(provider_id: int) -> None:
    """异步验证一个 LLM 供应商实例，并把结论写回状态字段。

    验证判据：用 default_model 发一次 max_tokens=1 的最小对话，能收到
    响应即证明 key、端点、模型 id 均有效。可用模型列表 best-effort
    拉取（部分兼容端点不提供 /models），失败只记日志不影响结论。

    前置约定：调用前状态已被 start_verification 置为 VERIFYING。
    作为背景任务：自开独立数据库会话，绝不向外抛异常 ——
    任何失败都转成 FAILED + last_error。
    """
    async with get_database().session() as session:
        repo = LlmProviderRepository(session)
        row = await repo.get(provider_id)
        if row is None:
            logger.warning("测试连接时 LLM 供应商实例（id=%s）已被删除", provider_id)
            return

        config = to_domain_config(row, repo.decrypted_api_key(row))
        config.timeout_seconds = _TEST_TIMEOUT
        preset = get_preset(row.provider_type)
        protocol = PROTOCOLS[preset.protocol](config, preset)
        try:
            await protocol.chat(
                ChatRequest(
                    messages=[ChatMessage(role="user", content="ping")],
                    settings=ModelSettings(max_tokens=1),
                ),
                row.default_model,
            )
        except LlmError as exc:
            # LlmError 的 message 本身已是清晰中文，直接展示
            logger.info("LLM 连接测试失败「%s」：%s", row.name, exc)
            await repo.update_status(provider_id, ConfigStatus.FAILED, last_error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- 背景任务兜底，绝不外抛
            logger.exception("LLM 连接测试发生未知错误「%s」", row.name)
            await repo.update_status(
                provider_id,
                ConfigStatus.FAILED,
                last_error=f"测试时发生未知错误（{type(exc).__name__}）：{exc}",
            )
            return
        else:
            # 对话验证已通过；模型列表仅是设置页的补录提示，拉不到不影响结论
            available: list[str] | None = None
            try:
                info = await protocol.test_connection()
                available = sorted(info.models)
            except Exception:  # noqa: BLE001
                logger.info("端点未提供模型列表接口，跳过（不影响验证结论）")
            logger.info("LLM 连接测试通过：%s / %s", row.name, row.default_model)
            await repo.update_status(provider_id, ConfigStatus.ACTIVE, available_models=available)
        finally:
            await protocol.close()
