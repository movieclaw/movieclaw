"""应用内更新接口（「设置 → 应用」页的后端）。

端点（实现全部在 services/app_update.py，本文件只做参数进出）：
- GET  /app/update/status    —— 当前版本/来源/是否支持更新/可否回退；
- POST /app/update/check     —— 查 GitHub 最新 Release 并比对（结果落快照）；
- GET  /app/update/pending   —— 读快照（不触网）：侧栏更新徽标与进页自动提示的数据源；
- POST /app/update/apply     —— 发起更新（后台执行，进度轮询下方接口）；
- GET  /app/update/progress  —— 更新执行进度；
- POST /app/update/rollback  —— 回退到上一版本（或镜像基线）；
- POST /app/update/last-exit/dismiss —— 确认异常退出告警（清除记录）；
- POST /app/update/model/check / /app/update/model/apply —— NER 模型独立更新。
"""

from __future__ import annotations

from fastapi import APIRouter

from movieclaw_api.schemas.app_update import (
    ModelUpdateCheckView,
    PendingUpdateView,
    RollbackOptionsView,
    RollbackPayload,
    UpdateCheckView,
    UpdateProgressView,
    UpdateRetentionPayload,
    UpdateStatusView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import app_update

router = APIRouter(prefix="/app/update", tags=["app"])


@router.get(
    "/status",
    response_model=ApiResponse[UpdateStatusView],
    summary="读取应用版本与更新能力状态",
    operation_id="app.update.status",
)
async def get_update_status() -> ApiResponse[UpdateStatusView]:
    return ok(app_update.build_status())


@router.post(
    "/check",
    response_model=ApiResponse[UpdateCheckView],
    summary="检查是否有新版本（比对 GitHub 最新 Release）",
    operation_id="app.update.check",
)
async def check_update() -> ApiResponse[UpdateCheckView]:
    return ok(await app_update.check_update_now())


@router.get(
    "/pending",
    response_model=ApiResponse[PendingUpdateView],
    summary="读取待更新快照（最近一次检查的结论，不触网）",
    operation_id="app.update.pending",
)
async def get_pending_update() -> ApiResponse[PendingUpdateView]:
    return ok(await app_update.read_pending())


@router.post(
    "/apply",
    response_model=ApiResponse[UpdateProgressView],
    summary="应用最新版本（下载校验后自动重启生效）",
    operation_id="app.update.apply",
)
async def apply_update() -> ApiResponse[UpdateProgressView]:
    view = await app_update.start_update()
    return ok(view, message="更新已开始，可通过进度接口跟踪")


@router.get(
    "/progress",
    response_model=ApiResponse[UpdateProgressView],
    summary="读取更新执行进度",
    operation_id="app.update.progress",
)
async def get_update_progress() -> ApiResponse[UpdateProgressView]:
    return ok(app_update.get_progress())


@router.post(
    "/last-exit/dismiss",
    response_model=ApiResponse[None],
    summary="确认上一次异常退出告警（清除记录，不再展示）",
    operation_id="app.update.last-exit-dismiss",
)
async def dismiss_last_abnormal_exit() -> ApiResponse[None]:
    app_update.dismiss_last_abnormal_exit()
    return ok(None, message="已确认，该告警不再展示")


@router.post(
    "/model/check",
    response_model=ApiResponse[ModelUpdateCheckView],
    summary="检查 NER 模型是否有新版本",
    operation_id="app.update.model-check",
)
async def check_model_update() -> ApiResponse[ModelUpdateCheckView]:
    return ok(await app_update.check_model_update_now())


@router.post(
    "/model/apply",
    response_model=ApiResponse[UpdateProgressView],
    summary="更新 NER 模型（下载校验后重启后端生效）",
    operation_id="app.update.model-apply",
)
async def apply_model_update() -> ApiResponse[UpdateProgressView]:
    view = await app_update.start_model_update()
    return ok(view, message="模型更新已开始，可通过进度接口跟踪")


@router.get(
    "/rollback/options",
    response_model=ApiResponse[RollbackOptionsView],
    summary="回退选择器数据：本地保留的历史版本、数据兼容判定与保留策略",
    operation_id="app.update.rollback-options",
)
async def get_rollback_options() -> ApiResponse[RollbackOptionsView]:
    return ok(await app_update.rollback_options())


@router.post(
    "/rollback",
    response_model=ApiResponse[None],
    summary="回退到指定版本/镜像内置版本（不带 target 时回退上一版本）",
    operation_id="app.update.rollback",
)
async def rollback_update(payload: RollbackPayload | None = None) -> ApiResponse[None]:
    if payload is not None and payload.target:
        message = app_update.rollback_to(payload.target, payload.restore_backup)
    else:
        # 旧版兼容语义：previous 互换，无 previous 回落基线
        message = app_update.rollback()
    return ok(None, message=message)


@router.put(
    "/retention",
    response_model=ApiResponse[None],
    summary="设置本地保留的版本目录数（立即按新策略清理）",
    operation_id="app.update.retention",
)
async def set_update_retention(payload: UpdateRetentionPayload) -> ApiResponse[None]:
    value = await app_update.save_update_retention(payload.keep_versions)
    return ok(None, message=f"本地将保留最近 {value} 个版本")
