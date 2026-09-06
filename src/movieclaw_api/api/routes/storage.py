"""缓存管理接口（「设置 → 更新与维护 → 缓存管理」标签的后端）。

- GET  /app/storage             —— 各登记目录的占用快照（?refresh=1 强制重算）；
- POST /app/storage/{key}/clean —— 清理某个目录（all / orphans）。

目录清单、用途与能否清理都来自 services/storage/registry.py 的登记表，这里
不写死任何目录名。挂在管理员路由组（router.py 的 _ADMIN_ROUTERS）。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query

from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.storage import CleanPayload, CleanResultView, StorageUsageView
from movieclaw_api.services.storage import service

router = APIRouter(prefix="/app/storage", tags=["app"])


@router.get(
    "",
    response_model=ApiResponse[StorageUsageView],
    summary="运行期数据目录的占用统计",
    operation_id="app.storage.usage",
)
async def get_storage_usage(
    refresh: bool = Query(default=False, description="忽略缓存快照，立即重新统计"),
) -> ApiResponse[StorageUsageView]:
    snapshot = await service.usage(refresh=refresh)
    return ok(StorageUsageView.model_validate(asdict(snapshot)))


@router.post(
    "/{key}/clean",
    response_model=ApiResponse[CleanResultView],
    summary="清理某个缓存目录",
    operation_id="app.storage.clean",
)
async def clean_storage(key: str, payload: CleanPayload) -> ApiResponse[CleanResultView]:
    result = await service.clean(key, payload.mode)
    return ok(CleanResultView.model_validate(asdict(result)), message="清理完成")
