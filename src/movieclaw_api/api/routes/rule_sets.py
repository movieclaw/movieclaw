from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.subscription import RuleSetPayload, RuleSetView
from movieclaw_api.services.rule_sets import RuleSetService
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/rule-sets", tags=["rule-sets"])


@router.get(
    "",
    response_model=ApiResponse[list[RuleSetView]],
    summary="规则组列表（首次访问自动创建默认组）",
    operation_id="rules.list",
)
async def list_rule_sets(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[RuleSetView]]:
    service = RuleSetService(session)
    rows = await service.list_all()
    references = await service.reference_counts()
    return ok(
        [RuleSetView.from_model(r, reference_count=references.get(r.id, 0)) for r in rows]
    )


@router.post(
    "",
    response_model=ApiResponse[RuleSetView],
    summary="创建规则组",
    operation_id="rules.create",
)
async def create_rule_set(
    payload: RuleSetPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RuleSetView]:
    service = RuleSetService(session)
    row = await service.create(payload.name, payload.spec, payload.match_rules)
    return ok(RuleSetView.from_model(row), message="规则组已创建")


@router.put(
    "/{rule_set_id}",
    response_model=ApiResponse[RuleSetView],
    summary="更新规则组（只影响之后的匹配评估）",
    operation_id="rules.update",
)
async def update_rule_set(
    rule_set_id: int,
    payload: RuleSetPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RuleSetView]:
    service = RuleSetService(session)
    row = await service.update(
        rule_set_id, name=payload.name, spec=payload.spec, match_rules=payload.match_rules
    )
    references = await service.count_references(rule_set_id)
    return ok(
        RuleSetView.from_model(row, reference_count=references), message="规则组已更新"
    )


@router.post(
    "/{rule_set_id}/default",
    response_model=ApiResponse[RuleSetView],
    summary="设为默认规则组（新订阅未指定规则组时使用）",
    operation_id="rules.default",
)
async def set_default_rule_set(
    rule_set_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RuleSetView]:
    """只换"谁是默认"，不改任何已有订阅的挂靠。幂等。"""
    service = RuleSetService(session)
    row = await service.set_default(rule_set_id)
    references = await service.count_references(rule_set_id)
    return ok(
        RuleSetView.from_model(row, reference_count=references),
        message=f"「{row.name}」已设为默认规则组",
    )


@router.delete(
    "/{rule_set_id}",
    response_model=ApiResponse[dict],
    summary="删除规则组（默认组与被引用的组禁删）",
    operation_id="rules.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_rule_set(
    rule_set_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = RuleSetService(session)
    await service.delete(rule_set_id)
    return ok({}, message="已删除")
