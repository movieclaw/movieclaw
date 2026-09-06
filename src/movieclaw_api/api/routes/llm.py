from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.llm import (
    LlmDefaultsPayload,
    LlmDefaultsView,
    LlmModelOptionView,
    LlmPresetView,
    LlmProviderPayload,
    LlmProviderView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.llm_config import LlmConfigService, verify_llm_provider
from movieclaw_db.engine import get_session
from movieclaw_llm.providers import list_presets

router = APIRouter(prefix="/llm", tags=["llm"])


@router.get(
    "/presets",
    response_model=ApiResponse[list[LlmPresetView]],
    summary="列出可接入的供应商类型及其模型目录",
    operation_id="llm.presets",
)
async def list_llm_presets() -> ApiResponse[list[LlmPresetView]]:
    """返回内置供应商预设（OpenAI / 阿里云百炼 / 通用兼容端点），
    设置页据此渲染类型选项、端点默认值与模型选择提示。"""
    return ok([LlmPresetView.from_preset(p) for p in list_presets()])


@router.get(
    "/models",
    response_model=ApiResponse[list[LlmModelOptionView]],
    summary="列出对话框可选的全部模型（跨实例）",
    operation_id="llm.models",
)
async def list_llm_models(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[LlmModelOptionView]]:
    """已接入实例目录里的全部模型（按实例添加顺序）。同一模型 id 只在一个实例里
    有时 ref 是裸 id；出现在多个实例里时 ref 为「实例名/模型id」、label 带括号
    标注实例名。把 ref 原样填进 session.start 的 model 或 AI 设定即可；
    is_default 标记智能体默认模型。"""
    return ok(await LlmConfigService(session).list_model_options())


@router.get(
    "/defaults",
    response_model=ApiResponse[LlmDefaultsView],
    summary="查看 AI 设定（各用途的默认模型）",
    operation_id="llm.defaults.show",
)
async def get_llm_defaults(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LlmDefaultsView]:
    """agent_model / subtitle_model 首次接入供应商时自动设为其目录第一个模型，之后
    由用户改；一个实例都没有时为 null。effective_* 为实际生效值，正常与之一致。"""
    return ok(await LlmConfigService(session).get_defaults())


@router.put(
    "/defaults",
    response_model=ApiResponse[LlmDefaultsView],
    summary="保存 AI 设定（各用途的默认模型）",
    operation_id="llm.defaults.update",
)
async def update_llm_defaults(
    payload: LlmDefaultsPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LlmDefaultsView]:
    """引用取自 llm.models 的 ref；传 null 则回到自动推荐（最早实例的第一个模型）。"""
    view = await LlmConfigService(session).update_defaults(
        agent_model=payload.agent_model, subtitle_model=payload.subtitle_model
    )
    return ok(view, message="AI 设定已保存")


@router.get(
    "/providers",
    response_model=ApiResponse[list[LlmProviderView]],
    summary="列出已接入的模型供应商实例",
    operation_id="llm.providers.list",
)
async def list_llm_providers(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[LlmProviderView]]:
    """按添加顺序；一个都没接入时 data 为空数组，设置页据此渲染空态。"""
    rows = await LlmConfigService(session).list_all()
    return ok([LlmProviderView.from_model(r) for r in rows])


@router.post(
    "/providers",
    response_model=ApiResponse[LlmProviderView],
    summary="接入一个模型供应商实例（保存后异步测试连接）",
    operation_id="llm.providers.create",
)
async def create_llm_provider(
    payload: LlmProviderPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LlmProviderView]:
    """保存后状态置 pending 并在后台用连接测试模型（目录里第一个）发一次最小
    对话验证；调用方可轮询实例（CLI：mclaw llm providers show）观察
    status：pending → verifying → active / failed（见 last_error）。"""
    service = LlmConfigService(session)
    row = await service.create(
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=payload.base_url,
        api_key=payload.api_key,
        default_model=payload.default_model,
        extra_models=payload.extra_models,
        user_agent=payload.user_agent,
    )
    assert row.id is not None
    row = await service.start_verification(row.id)
    background_tasks.add_task(verify_llm_provider, row.id)
    return ok(LlmProviderView.from_model(row), message="已保存，正在测试模型连接")


@router.get(
    "/providers/{provider_id}",
    response_model=ApiResponse[LlmProviderView],
    summary="查看一个模型供应商实例",
    operation_id="llm.providers.show",
)
async def get_llm_provider(
    provider_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LlmProviderView]:
    row = await LlmConfigService(session).get(provider_id)
    return ok(LlmProviderView.from_model(row))


@router.put(
    "/providers/{provider_id}",
    response_model=ApiResponse[LlmProviderView],
    summary="修改一个模型供应商实例（保存后异步测试连接）",
    operation_id="llm.providers.update",
)
async def update_llm_provider(
    provider_id: int,
    payload: LlmProviderPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LlmProviderView]:
    """整体覆盖接入配置（API Key 需重新填写）。"""
    service = LlmConfigService(session)
    await service.update(
        provider_id,
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=payload.base_url,
        api_key=payload.api_key,
        default_model=payload.default_model,
        extra_models=payload.extra_models,
        user_agent=payload.user_agent,
    )
    row = await service.start_verification(provider_id)
    background_tasks.add_task(verify_llm_provider, provider_id)
    return ok(LlmProviderView.from_model(row), message="已保存，正在测试模型连接")


@router.post(
    "/providers/{provider_id}/verify",
    response_model=ApiResponse[LlmProviderView],
    summary="手动重新测试一个实例的模型连接",
    operation_id="llm.providers.verify",
)
async def reverify_llm_provider(
    provider_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LlmProviderView]:
    """行为：不存在 → 404；已在测试中 → 409；否则同步占位为 VERIFYING
    并在后台重新测试。"""
    row = await LlmConfigService(session).start_verification(provider_id)
    background_tasks.add_task(verify_llm_provider, provider_id)
    return ok(LlmProviderView.from_model(row), message="已重新发起连接测试")


@router.delete(
    "/providers/{provider_id}",
    response_model=ApiResponse[dict],
    summary="删除一个模型供应商实例",
    operation_id="llm.providers.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_llm_provider(
    provider_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """AI 设定里指向它的默认模型会自动改指最早剩下的实例；全部删除时清空。"""
    await LlmConfigService(session).delete(provider_id)
    return ok({}, message="已删除")
