"""LLM 供应商配置服务：实例接入、AI 设定（各用途默认模型）、连接验证与模型清单。

接入与设定是两件事：
- **接入**（llm_provider 表，多实例）只回答「怎么连上」：实例名、类型、端点、
  Key、自定义模型目录。连接测试用目录里第一个模型发一次 max_tokens=1 的
  最小对话——比只调 /models 列表更真实，能一次性证明 key、端点、模型三者
  都有效；模型列表另行 best-effort 拉取，仅作设置页补录提示；
- **设定**（LlmDefaultsSetting）回答「什么场景用哪个模型」：智能体默认模型、
  字幕处理默认模型，值是对话框同款的模型引用。不变量：**只要还有实例，两个
  默认就都已设置且可解析**——首次接入时自动设为该实例目录里的第一个模型，
  被引用的实例删除时自动改指最早剩下的一家，全部删除时清空（见
  reconcile_defaults）。设置页显示的就是真实存的值，不靠运行时隐式兜底；
  resolve_defaults 里的兜底只是预设目录变动等漂移场景的安全网。

模型清单（对话框「模型」入口与 AI 设定的选项）的口径：
- 一个实例接入后，它目录里的全部模型都可选：预设目录 ∪ 用户补录
  （extra_models，按 id 覆盖预设），与 LlmRouter._catalog 同口径；
- 端点上报的 available_models 不进清单——它们没有上下文窗口 / 思考能力
  等元数据，选了之后自动压缩与思维链菜单都会失效；
- 同一模型 id 只在一个实例里有 → 引用就是裸 id、展示也是裸 id；出现在多个
  实例 → 引用用「实例名/模型id」精确路由，展示加括号「模型id（实例名）」；
- 模型 id 本身含斜杠（SiliconFlow / OpenRouter 风格的 ``org/model``）时引用
  也必须限定：路由层按第一个斜杠拆「实例名/模型id」，裸引用会把 org 当实例名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
)
from movieclaw_api.schemas.llm import LlmDefaultsView, LlmModelOptionView
from movieclaw_api.settings import LlmDefaultsSetting, get_setting_store
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

#: 有默认模型设定的用途；新增用途时同步扩展 LlmDefaultsSetting 与 resolve_defaults
Purpose = Literal["agent", "subtitle"]


def to_domain_config(row: LlmProvider, api_key: str) -> LlmProviderConfig:
    """ORM 记录 → movieclaw_llm 领域配置（LlmRouter 按它构建协议客户端）。"""
    return LlmProviderConfig(
        name=row.name,
        provider_type=row.provider_type,
        api_key=api_key,
        base_url=row.base_url,
        default_model=row.default_model,
        extra_models=[ModelInfo.model_validate(m) for m in row.extra_models or []],
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


def build_model_options(
    rows: list[LlmProvider], agent_default: str | None = None
) -> list[LlmModelOptionView]:
    """把全部实例的目录拍平成模型清单（rows 按添加顺序）。

    重复 id 的处理策略：同一模型 id 只在一个实例里有，引用与展示都是裸 id；
    出现在多个实例里，引用改用「实例名/模型id」精确路由（LlmRouter 的显式
    分支），展示加括号「模型id（实例名）」让用户分得清走哪家。
    ``agent_default`` 是智能体默认模型的引用，命中的选项标 is_default。
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
            # 冲突或 id 含斜杠都要限定（见模块说明）；展示只在冲突时加括号
            qualified = ambiguous or "/" in model.id
            ref = f"{row.name}/{model.id}" if qualified else model.id
            options.append(
                LlmModelOptionView(
                    ref=ref,
                    label=f"{model.id}（{row.name}）" if ambiguous else model.id,
                    model_id=model.id,
                    provider_id=row.id or 0,
                    provider_name=row.name,
                    is_default=ref == agent_default,
                    thinking_levels=model.thinking_levels,
                )
            )
    return options


@dataclass(frozen=True)
class ResolvedDefaults:
    """各用途实际生效的模型引用（None = 一个实例都没有）。"""

    agent: str | None
    subtitle: str | None

    def ref(self, purpose: Purpose) -> str | None:
        return self.agent if purpose == "agent" else self.subtitle


def recommended_default(rows: list[LlmProvider], options: list[LlmModelOptionView]) -> str | None:
    """自动设定的默认模型：最早接入实例目录里的第一个模型（即它的连接测试模型）。

    预设目录按 yaml 顺序排列、旗舰在前，所以「第一个」就是该供应商的推荐
    主力模型；自定义端点则是用户补录的第一个。一个实例都没有时为 None。
    ``options`` 是 build_model_options(rows) 的结果，由调用方构建一次传入。
    """
    if not rows:
        return None
    first = rows[0]
    return next(
        (o.ref for o in options if o.provider_id == first.id and o.model_id == first.default_model),
        options[0].ref if options else None,
    )


def resolve_defaults(
    rows: list[LlmProvider],
    setting: LlmDefaultsSetting,
    options: list[LlmModelOptionView] | None = None,
) -> ResolvedDefaults:
    """设定 → 实际生效：设定的引用仍能在清单里找到就用它，否则按推荐默认兜底。

    正常情况下 reconcile_defaults 已保证设定可解析，这里的兜底只是安全网
    （如预设目录升级后删掉了某个模型 id）。热路径调用方把已构建的 options 传进来。
    """
    options = build_model_options(rows) if options is None else options
    refs = {o.ref for o in options}
    fallback = recommended_default(rows, options)

    def pick(configured: str | None) -> str | None:
        return configured if configured in refs else fallback

    return ResolvedDefaults(agent=pick(setting.agent_model), subtitle=pick(setting.subtitle_model))


async def reconcile_defaults(
    rows: list[LlmProvider], previous: list[LlmModelOptionView]
) -> None:
    """实例增删改后维护不变量：有实例则两个默认都已设置且可解析，无实例则清空。

    ``previous`` 是改动前的模型清单。引用的拼写会随实例集合变化（同 id 出现
    在第二家后裸 id 变成「实例名/模型id」、实例改名后前半段变了），所以不能
    按字符串判断设定是否还有效——先用改动前的清单把设定还原成
    (实例 id, 模型 id) 这个稳定身份，再到新清单里找同身份的新引用：
    - 身份仍在：改写成新拼写（用户的选择原样保留）；
    - 身份不在（实例被删、模型从目录移除）：改指最早剩下实例的第一个模型；
    - 设定不在改动前清单里（漂移遗留）：字符串仍可解析就保留，否则同上兜底；
    - 首次接入：两个默认都设为该实例目录里的第一个模型；全部删除：清空。
    """
    store = get_setting_store()
    setting = await store.get(LlmDefaultsSetting)
    options = build_model_options(rows)
    refs = {o.ref for o in options}
    by_identity = {(o.provider_id, o.model_id): o.ref for o in options}
    fallback = recommended_default(rows, options)

    def carry(stored: str | None) -> str | None:
        if stored is None:
            return fallback
        before = next((o for o in previous if o.ref == stored), None)
        if before is not None:
            return by_identity.get((before.provider_id, before.model_id), fallback)
        return stored if stored in refs else fallback

    agent, subtitle = carry(setting.agent_model), carry(setting.subtitle_model)
    if (agent, subtitle) != (setting.agent_model, setting.subtitle_model):
        await store.set(LlmDefaultsSetting(agent_model=agent, subtitle_model=subtitle))
        logger.info("AI 设定已自动调整：智能体默认模型=%s，字幕处理默认模型=%s", agent, subtitle)


async def default_model_ref(session: AsyncSession, purpose: Purpose) -> str:
    """某用途实际生效的默认模型引用；一个实例都没有时返回空串（交给路由报错）。"""
    rows = await LlmProviderRepository(session).list_all()
    setting = await get_setting_store().get(LlmDefaultsSetting)
    return resolve_defaults(rows, setting).ref(purpose) or ""


def _owner_of(
    rows: list[LlmProvider], ref: str | None, options: list[LlmModelOptionView]
) -> tuple[LlmProvider, str] | None:
    """模型引用 → (所属实例, 模型 id)；引用为空或解析不到返回 None。"""
    if not ref:
        return None
    for option in options:
        if option.ref == ref:
            row = next((r for r in rows if r.id == option.provider_id), None)
            return (row, option.model_id) if row is not None else None
    return None


async def resolve_provider_endpoint(session: AsyncSession) -> tuple[str, str, str | None]:
    """解析智能体默认模型所属实例的接入参数，返回 ``(base_url, api_key, user_agent)``。

    端点取值：显式配置 → 预设默认；两者皆空抛 BadRequest。
    user_agent 未配置时为 None（调用方保持自己的默认 UA）。
    这是端点/密钥/UA 解析的唯一判据来源——网络连通性测试（routes/network）
    复用本函数，不允许在别处再实现一遍取值顺序。
    """
    repo = LlmProviderRepository(session)
    rows = await repo.list_all()
    if not rows:
        raise BadRequestException("尚未配置 AI 模型供应商，无法测试")
    setting = await get_setting_store().get(LlmDefaultsSetting)
    options = build_model_options(rows)
    owner = _owner_of(rows, resolve_defaults(rows, setting, options).agent, options)
    row = owner[0] if owner else rows[0]
    base = row.base_url or (get_preset(row.provider_type).base_url or "")
    if not base:
        raise BadRequestException(f"供应商「{row.name}」未配置 API 端点地址")
    return base, repo.decrypted_api_key(row) or "", row.user_agent


class LlmConfigService:
    """LLM 供应商实例接入与 AI 设定的业务服务。绑定一个数据库会话。"""

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
        """全部实例，按添加顺序。"""
        return await self._repo.list_all()

    async def get(self, provider_id: int) -> LlmProvider:
        """按 id 取实例；不存在抛 404。"""
        row = await self._repo.get(provider_id)
        if row is None:
            raise NotFoundException("模型供应商实例不存在")
        return row

    async def list_model_options(self) -> list[LlmModelOptionView]:
        """对话框与 AI 设定的模型清单（口径见模块说明），智能体默认项标 is_default。"""
        rows = await self._repo.list_all()
        setting = await get_setting_store().get(LlmDefaultsSetting)
        agent = resolve_defaults(rows, setting).agent
        # 设置页 / 对话框入口，不在热路径；再构建一次只为标记 is_default
        return build_model_options(rows, agent)

    # -- AI 设定 -----------------------------------------------------------

    async def get_defaults(self) -> LlmDefaultsView:
        rows = await self._repo.list_all()
        setting = await get_setting_store().get(LlmDefaultsSetting)
        effective = resolve_defaults(rows, setting)
        return LlmDefaultsView(
            agent_model=setting.agent_model,
            subtitle_model=setting.subtitle_model,
            effective_agent_model=effective.agent,
            effective_subtitle_model=effective.subtitle,
        )

    async def update_defaults(
        self, *, agent_model: str | None, subtitle_model: str | None
    ) -> LlmDefaultsView:
        """保存各用途默认模型；引用必须能在当前清单里找到。

        传 null 表示「回到自动推荐」（最早实例的第一个模型），不会把设定清成
        空——只要还有实例，两个默认就始终有值，设置页显示的就是真实生效值。
        """
        rows = await self._repo.list_all()
        refs = {o.ref for o in build_model_options(rows)}
        for label, ref in (("智能体", agent_model), ("字幕处理", subtitle_model)):
            if ref is not None and ref not in refs:
                raise BadRequestException(
                    f"{label}默认模型「{ref}」不在已接入供应商的模型清单中，请重新选择"
                )
        recommended = recommended_default(rows, build_model_options(rows))
        await get_setting_store().set(
            LlmDefaultsSetting(
                agent_model=agent_model or recommended,
                subtitle_model=subtitle_model or recommended,
            )
        )
        return await self.get_defaults()

    # -- 接入写入 ----------------------------------------------------------

    @staticmethod
    def _assert_model_configured(
        preset: ProviderPreset,
        extra_models: list[ModelInfo],
        model_id: str,
    ) -> None:
        """连接测试模型的严格校验，按供应商类型分两条规则：

        - 有内置目录的供应商（官方渠道）：模型必须在目录内，自定义模型不
          参与——官方渠道的模型集合以预设目录为准；
        - 无目录的自定义端点：模型必须在 extra_models 里带完整参数。
          其中「借用」自其它预设目录的模型（按 id 识别）参数随目录，
          豁免手填规则——共享窗口类模型（如 Kimi）本就没有独立输出上限。

        agent 做上下文预算、思考预算、并发决策都依赖这些参数，
        缺参数会让下游全部退化成瞎猜，所以在入口就拦住。
        """
        if preset.models:
            if any(m.id == model_id for m in preset.models):
                return
            raise BadRequestException(
                f"模型「{model_id}」不在「{preset.display_name}」的模型目录中，请从下拉列表中选择"
            )
        custom = next((m for m in extra_models if m.id == model_id), None)
        if custom is None:
            raise BadRequestException(
                f"模型「{model_id}」不在预设目录中，请先补全它的参数配置"
                "（上下文长度、最大输出等）后再保存"
            )
        # 借用目录模型：任一预设目录里有同 id 条目即豁免手填参数规则
        if any(m.id == custom.id for p in list_presets() for m in p.models):
            return
        if not custom.context_window or not custom.max_output_tokens:
            raise BadRequestException(
                f"自定义模型「{model_id}」缺少必要参数：上下文长度与最大输出为必填"
            )
        if custom.supports_thinking and not custom.max_thinking_tokens:
            raise BadRequestException(
                f"自定义模型「{model_id}」开启了思考模式，必须填写思考预算上限"
            )

    async def _validate(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        default_model: str | None,
        extra_models: list[ModelInfo],
        exclude_id: int | None = None,
    ) -> str:
        """新增与编辑共用的入参校验，返回实际使用的连接测试模型 id。

        校验：类型存在、端点必填、实例名唯一；测试模型未指定时取目录第一个
        （预设目录 → 自定义目录），兼容端点一个模型都没补录时拒绝——没有
        目录的实例接入了也无模型可用。自定义目录里的每个模型都要参数齐全。
        """
        try:
            preset = get_preset(provider_type)
        except LlmError as exc:
            # 未知供应商类型：领域错误翻译成 400（错误信息本身已含可选项提示）
            raise BadRequestException(str(exc)) from exc
        if preset.requires_base_url and not base_url:
            raise BadRequestException(f"接入「{preset.display_name}」必须填写 API 端点地址")
        if not preset.models:
            if not extra_models:
                raise BadRequestException(
                    f"接入「{preset.display_name}」至少要补录一个模型（含上下文长度、最大输出等参数）"
                )
            for model in extra_models:
                self._assert_model_configured(preset, extra_models, model.id)
        test_model = default_model or (
            preset.models[0].id if preset.models else extra_models[0].id
        )
        self._assert_model_configured(preset, extra_models, test_model)
        existing = await self._repo.get_by_name(name)
        if existing is not None and existing.id != exclude_id:
            raise ConflictException(f"实例名「{name}」已被使用，请换一个名字")
        return test_model

    async def create(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        api_key: str,
        default_model: str | None = None,
        extra_models: list[ModelInfo] | None = None,
        user_agent: str | None = None,
    ) -> LlmProvider:
        """新增实例。状态置 PENDING，等待后台验证。"""
        extras = extra_models or []
        test_model = await self._validate(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            default_model=default_model,
            extra_models=extras,
        )
        before = build_model_options(await self._repo.list_all())
        row = await self._repo.create(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            default_model=test_model,
            extra_models=[m.model_dump() for m in extras] or None,
            user_agent=user_agent,
        )
        await reconcile_defaults(await self._repo.list_all(), before)
        return row

    async def update(
        self,
        provider_id: int,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        api_key: str,
        default_model: str | None = None,
        extra_models: list[ModelInfo] | None = None,
        user_agent: str | None = None,
    ) -> LlmProvider:
        """整体覆盖一个实例的接入配置。不存在抛 404，正在验证中抛 409。"""
        row = await self.get(provider_id)
        self._assert_not_verifying(row)
        extras = extra_models or []
        test_model = await self._validate(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            default_model=default_model,
            extra_models=extras,
            exclude_id=provider_id,
        )
        before = build_model_options(await self._repo.list_all())
        updated = await self._repo.update(
            provider_id,
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            default_model=test_model,
            extra_models=[m.model_dump() for m in extras] or None,
            user_agent=user_agent,
        )
        assert updated is not None  # 上面 get 已确认存在
        # 实例改名或目录改动会改变引用的拼写，按稳定身份改写设定
        await reconcile_defaults(await self._repo.list_all(), before)
        return updated

    async def start_verification(self, provider_id: int) -> LlmProvider:
        """同步占位为 VERIFYING 并返回，随后由调用方排队后台测试任务。

        并发守卫原理见 SiteConfigService.start_verification。
        """
        row = await self.get(provider_id)
        self._assert_not_verifying(row)
        await self._repo.update_status(provider_id, ConfigStatus.VERIFYING)
        return await self.get(provider_id)

    async def delete(self, provider_id: int) -> None:
        """删除实例；不存在抛 404，正在验证中抛 409。"""
        row = await self.get(provider_id)
        self._assert_not_verifying(row)
        before = build_model_options(await self._repo.list_all())
        await self._repo.delete(provider_id)
        await reconcile_defaults(await self._repo.list_all(), before)


# ---------------------------------------------------------------------------
# 进程级 LlmRouter 单例：所有 LLM 调用（agent 等）的运行时入口
# ---------------------------------------------------------------------------

_runtime_router = LlmRouter()


async def acquire_llm_router(session: AsyncSession) -> LlmRouter:
    """读取全部实例配置并组装进程级 LlmRouter。

    每次取用都重新读配置并 update_providers：配置指纹未变时底层客户端
    缓存直接复用（零开销），变了则自动重建——不需要「配置已修改」的
    显式通知链路。一个实例都没有时抛 404（中文提示引导去设置页）。

    路由层的 ``default`` / 空引用 = 智能体默认模型：把它所属的实例标为
    is_default 并把该实例的 default_model 设成它，所有不显式选模型的调用
    （IM 通道、会话续聊、手动压缩、CLI）就都跟着 AI 设定走，不必逐处传参。
    """
    repo = LlmProviderRepository(session)
    rows = await repo.list_all()
    if not rows:
        raise NotFoundException("尚未配置模型供应商，请先在「设置 → 模型接入」中接入")
    setting = await get_setting_store().get(LlmDefaultsSetting)
    # 每条消息都经过这里：清单只构建一次，解析默认与找归属都复用它
    options = build_model_options(rows)
    owner = _owner_of(rows, resolve_defaults(rows, setting, options).agent, options)
    configs = []
    for row in rows:
        config = to_domain_config(row, repo.decrypted_api_key(row))
        if owner is not None and owner[0].id == row.id:
            config.is_default = True
            config.default_model = owner[1]
        configs.append(config)
    await _runtime_router.update_providers(configs)
    return _runtime_router


# ---------------------------------------------------------------------------
# 后台连接测试（与 verify_downloader 同构的背景任务）
# ---------------------------------------------------------------------------


async def verify_llm_provider(provider_id: int) -> None:
    """异步验证一个 LLM 供应商实例，并把结论写回状态字段。

    验证判据：用连接测试模型（default_model）发一次 max_tokens=1 的最小
    对话，能收到响应即证明 key、端点、模型 id 均有效。可用模型列表
    best-effort 拉取（部分兼容端点不提供 /models），失败只记日志不影响结论。

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
