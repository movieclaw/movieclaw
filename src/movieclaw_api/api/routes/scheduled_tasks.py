"""定时任务的周期与启停（设置 → 应用 → 定时任务）。

调度定义一直落在 ``scheduled_task`` 表里（代码里的注册只是首次播种的默认值），
但此前没有任何入口能改它——「媒体库对账」写死每 6 小时，网络挂载的库明明只能
靠它发现新文件，用户也调不了。这里补上读与改：改完直接让调度器按新定义重排
这一个任务，不重启、不影响其它任务。

只暴露代码里仍有处理器的任务：库里残留的、代码已删的定义不列（调度器也不加载它）。
"""

from __future__ import annotations

from datetime import datetime

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_db.engine import get_session
from movieclaw_db.models.scheduled_task import ScheduledTask, TriggerType
from movieclaw_db.repositories.scheduled_task_repo import ScheduledTaskRepository
from movieclaw_scheduler.registry import TaskDefinition, get_task
from movieclaw_scheduler.service import try_get_scheduler

router = APIRouter(prefix="/scheduled-tasks", tags=["system"])

#: 间隔下限：再短就是在轮询，而这些任务（对账、同步、巡检）没有一个需要分钟级
MIN_INTERVAL_SECONDS = 60


class ScheduledTaskView(BaseModel):
    key: str
    title: str
    description: str
    enabled: bool
    trigger_type: TriggerType
    interval_seconds: int | None
    cron_expr: str | None
    last_run_at: datetime | None
    next_run_at: datetime | None


class ScheduledTaskUpdate(BaseModel):
    enabled: bool
    trigger_type: TriggerType
    interval_seconds: int | None = None
    cron_expr: str | None = None


def _view(row: ScheduledTask, defn: TaskDefinition) -> ScheduledTaskView:
    return ScheduledTaskView(
        key=row.task_key,
        title=defn.title,
        description=defn.description,
        enabled=row.enabled,
        trigger_type=row.trigger_type,
        interval_seconds=row.interval_seconds,
        cron_expr=row.cron_expr,
        last_run_at=row.last_run_at,
        next_run_at=row.next_run_at,
    )


@router.get(
    "",
    response_model=ApiResponse[list[ScheduledTaskView]],
    summary="定时任务：周期、启停与最近/下次执行",
    operation_id="app.tasks.list",
)
async def list_scheduled_tasks(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ScheduledTaskView]]:
    rows = await ScheduledTaskRepository(session).list_all()
    views = []
    for row in rows:
        defn = get_task(row.task_key)
        if defn is not None:
            views.append(_view(row, defn))
    views.sort(key=lambda v: v.title)
    return ok(views)


@router.put(
    "/{task_key}",
    response_model=ApiResponse[ScheduledTaskView],
    summary="改一个定时任务的周期 / 启停（立即重排，不用重启）",
    operation_id="app.tasks.update",
)
async def update_scheduled_task(
    task_key: str,
    payload: ScheduledTaskUpdate,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ScheduledTaskView]:
    defn = get_task(task_key)
    if defn is None:
        raise NotFoundException("没有这个定时任务")
    interval: int | None = None
    cron: str | None = None
    if payload.trigger_type == TriggerType.INTERVAL:
        if payload.interval_seconds is None or payload.interval_seconds < MIN_INTERVAL_SECONDS:
            raise BadRequestException(f"间隔至少 {MIN_INTERVAL_SECONDS} 秒")
        interval = int(payload.interval_seconds)
    else:
        cron = (payload.cron_expr or "").strip()
        if not cron:
            raise BadRequestException("按固定时刻触发需要 cron 表达式（分 时 日 月 周）")
        try:
            CronTrigger.from_crontab(cron)
        except ValueError as exc:
            raise BadRequestException(f"cron 表达式无效：{exc}") from exc

    repo = ScheduledTaskRepository(session)
    row = await repo.update_schedule(
        task_key,
        enabled=payload.enabled,
        trigger_type=payload.trigger_type,
        interval_seconds=interval,
        cron_expr=cron,
    )
    if row is None:
        raise NotFoundException("没有这个定时任务")
    # 调度器在跑就立刻按新定义重排；没在跑（关闭了调度、测试环境）只落库，
    # 下次启动按新定义加载
    scheduler = try_get_scheduler()
    if scheduler is not None:
        await scheduler.reschedule(task_key)
        await session.refresh(row)
    return ok(_view(row, defn), message="已更新，按新周期生效")
