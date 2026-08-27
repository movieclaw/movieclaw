"""统一后台任务查询、等待、取消、重试、忽略与事件流。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_admin
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.base import utc_isoformat
from movieclaw_api.schemas.jobs import (
    JobCancelView,
    JobDismissAllRequest,
    JobDismissAllView,
    JobDismissRequest,
    JobDismissView,
    JobEventListView,
    JobEventView,
    JobListView,
    JobResourceView,
    JobRetryView,
    JobView,
    JobWaitView,
    JobWorkerHealthView,
)
from movieclaw_api.schemas.response import ApiResponse
from movieclaw_api.services import jobs
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.subtitle_gen import auto as subtitle_auto
from movieclaw_db.engine import get_database, get_session
from movieclaw_db.models import Job, JobResource, JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _origin(principal: Principal, client_name: str | None) -> str:
    if principal.kind == "agent":
        return "agent"
    if client_name and client_name.strip().lower() in {"cli", "web", "agent", "scheduler"}:
        return client_name.strip().lower()
    return "cli" if principal.kind == "pat" else "web"


def _job_view(job: Job, resources: list[JobResource]) -> JobView:
    progress = jobs.default_progress()
    progress.update(job.progress or {})
    return JobView(
        id=job.id,
        job_type=job.job_type,
        subject=job.subject,
        definition_version=job.definition_version,
        handler_revision=job.handler_revision,
        prompt_revision=job.prompt_revision,
        provider_ref=job.provider_ref,
        status=job.status,
        input_data=job.input_data,
        progress=progress,  # type: ignore[arg-type]
        result=job.result,
        error=job.error,
        usage=job.usage,
        resources=[
            JobResourceView(
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                relation=item.relation,
            )
            for item in resources
        ],
        origin=job.origin,
        actor_kind=job.actor_kind,
        actor_name=job.actor_name,
        parent_job_id=job.parent_job_id,
        root_job_id=job.root_job_id,
        retry_of_job_id=job.retry_of_job_id,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        revision=job.revision,
        cancel_requested_at=job.cancel_requested_at,
        cancel_requested_by=job.cancel_requested_by,
        dismissed_at=job.dismissed_at,
        dismissed_by=job.dismissed_by,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def job_view(session: AsyncSession, job: Job) -> JobView:
    resource_map = await jobs.resources_for_jobs(session, [job.id])
    return _job_view(job, resource_map.get(job.id, []))


async def job_views(session: AsyncSession, rows: list[Job]) -> list[JobView]:
    """列表只批量读取一次资源索引，避免任务中心产生 N+1 查询。"""
    resource_map = await jobs.resources_for_jobs(session, [row.id for row in rows])
    return [_job_view(row, resource_map.get(row.id, [])) for row in rows]


def _parse_statuses(value: str | None) -> list[JobStatus]:
    if not value:
        return []
    try:
        return [JobStatus(token.strip()) for token in value.split(",") if token.strip()]
    except ValueError as exc:
        allowed = "、".join(status.value for status in JobStatus)
        raise BadRequestException(f"任务状态不合法，可选值：{allowed}") from exc


@router.get(
    "",
    response_model=ApiResponse[JobListView],
    summary="查询后台任务（可按状态、类型或资源聚合）",
    operation_id="jobs.list",
)
async def list_job_rows(
    status: str | None = Query(default=None, description="状态，多个用逗号分隔"),
    job_type: str | None = Query(default=None, description="任务类型"),
    resource_type: str | None = Query(default=None, description="资源类型，如 library_file"),
    resource_id: str | None = Query(default=None, description="资源 ID"),
    active_only: bool = Query(default=False, description="只返回活跃任务"),
    limit: int = Query(default=50, ge=1, le=200, description="返回数量"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobListView]:
    rows = await jobs.list_jobs(
        session,
        statuses=_parse_statuses(status),
        job_type=job_type,
        resource_type=resource_type,
        resource_id=resource_id,
        active_only=active_only,
        limit=limit,
    )
    return ApiResponse(data=JobListView(items=await job_views(session, rows)))


@router.get(
    "/stream",
    summary="后台任务全局事件流",
    operation_id="jobs.stream",
    # 浏览器 Provider 的基础设施端点；CLI 已有 jobs wait/events，不重复生成
    # 一个永不自然终止的全局流命令。
    openapi_extra={"x-cli-hidden": True},
)
async def stream_jobs(
    request: Request,
    after: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _principal: Principal = Depends(require_admin),
) -> StreamingResponse:
    cursor = after or 0
    resume_requested = after is not None
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
        resume_requested = True

    async def generate():  # type: ignore[no-untyped-def]
        nonlocal cursor
        if not resume_requested:
            # 首次连接前端已经加载任务快照，只订阅此刻之后的新事件；浏览器
            # 断线重连会自动携带 Last-Event-ID，届时才精确续传缺失事件。
            db = get_database()
            async with db.session() as session:
                cursor = await jobs.latest_job_event_id(session)
            # 客户端收到 ready 后再校准一次快照，覆盖“首次列表查询完成到 SSE
            # 尾游标建立”之间的窄竞态窗口；ready 不带 id，不影响断线续传游标。
            yield f"event: ready\ndata: {cursor}\n\n"
        idle = 0
        while not await request.is_disconnected():
            db = get_database()
            async with db.session() as session:
                events = await jobs.job_events(session, after_id=cursor, limit=200)
            if events:
                idle = 0
                for event in events:
                    assert event.id is not None
                    cursor = event.id
                    payload = {
                        "id": event.id,
                        "job_id": event.job_id,
                        "revision": event.revision,
                        "event_type": event.event_type,
                        "payload": event.payload,
                        "created_at": utc_isoformat(event.created_at),
                    }
                    event_data = json.dumps(payload, ensure_ascii=False)
                    yield f"id: {event.id}\nevent: job\ndata: {event_data}\n\n"
                continue
            idle += 1
            if idle >= 30:
                idle = 0
                yield ": heartbeat\n\n"
            await jobs.wait_for_job_change(1.0)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/health",
    response_model=ApiResponse[JobWorkerHealthView],
    summary="查看后台任务执行器健康状态",
    operation_id="jobs.health",
)
async def job_worker_health() -> ApiResponse[JobWorkerHealthView]:
    return ApiResponse(data=JobWorkerHealthView.model_validate(await jobs.dispatcher_health()))


@router.post(
    "/dismiss-all",
    response_model=ApiResponse[JobDismissAllView],
    summary="忽略当前所有失败任务",
    operation_id="jobs.dismiss-all",
)
async def dismiss_all_failed_jobs(
    payload: JobDismissAllRequest | None = None,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobDismissAllView]:
    """一次收口眼前这批失败任务。

    存在的理由是故障的形状：一次扫描收尾可能建出几十个字幕任务，模型配置
    错了就几十条一起失败。让前端循环调用单条接口既慢又可能中途半死不活，
    整体动作在服务端一次事务完成。

    只影响**此刻**已经失败的任务；之后再失败的仍会照常提醒。
    """
    rows = await jobs.dismiss_failed_jobs(
        session,
        dismissed_by=str(principal),
        job_type=payload.job_type if payload else None,
    )
    return ApiResponse(
        data=JobDismissAllView(dismissed=len(rows), jobs=await job_views(session, rows)),
        message=f"已忽略 {len(rows)} 个失败任务" if rows else "当前没有需要忽略的失败任务",
    )


@router.get(
    "/{job_id}",
    response_model=ApiResponse[JobView],
    summary="查看后台任务详情",
    operation_id="jobs.show",
)
async def show_job(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> ApiResponse[JobView]:
    row = await jobs.get_job(session, job_id)
    if row is None:
        raise NotFoundException("后台任务不存在")
    return ApiResponse(data=await job_view(session, row))


@router.get(
    "/{job_id}/wait",
    response_model=ApiResponse[JobWaitView],
    summary="等待任务状态变化（CLI 长轮询）",
    operation_id="jobs.wait",
)
async def wait_job(
    job_id: str,
    after_revision: int = Query(default=0, ge=0, description="已看到的 revision"),
    wait_seconds: float = Query(default=25.0, ge=0.1, le=30.0, description="最长等待秒数"),
) -> ApiResponse[JobWaitView]:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        db = get_database()
        async with db.session() as session:
            row = await jobs.get_job(session, job_id)
            if row is None:
                raise NotFoundException("后台任务不存在")
            if row.revision > after_revision:
                return ApiResponse(data=JobWaitView(changed=True, job=await job_view(session, row)))
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return ApiResponse(
                    data=JobWaitView(changed=False, job=await job_view(session, row))
                )
        await jobs.wait_for_job_change(remaining)


@router.get(
    "/{job_id}/events",
    response_model=ApiResponse[JobEventListView],
    summary="查看后台任务事件时间线",
    operation_id="jobs.events",
)
async def list_job_events(
    job_id: str,
    after: int = Query(default=0, ge=0, description="事件游标"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobEventListView]:
    if await jobs.get_job(session, job_id) is None:
        raise NotFoundException("后台任务不存在")
    rows = await jobs.job_events(session, job_id, after_id=after, limit=limit)
    return ApiResponse(
        data=JobEventListView(
            items=[
                JobEventView(
                    id=row.id,  # type: ignore[arg-type]
                    job_id=row.job_id,
                    revision=row.revision,
                    event_type=row.event_type,
                    payload=row.payload,
                    created_at=row.created_at,
                )
                for row in rows
            ]
        )
    )


@router.post(
    "/{job_id}/cancel",
    response_model=ApiResponse[JobCancelView],
    summary="取消后台任务（在安全边界生效）",
    operation_id="jobs.cancel",
)
async def cancel_job(
    job_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobCancelView]:
    row, cancelled = await jobs.request_cancel(session, job_id, requested_by=str(principal))
    if row is None:
        raise NotFoundException("后台任务不存在")
    return ApiResponse(
        data=JobCancelView(cancelled=cancelled, job=await job_view(session, row)),
        message="已请求取消" if cancelled else "任务已经结束",
    )


@router.post(
    "/{job_id}/retry",
    response_model=ApiResponse[JobRetryView],
    summary="重试失败、已取消或需要处理的后台任务",
    operation_id="jobs.retry",
)
async def retry_job_row(
    job_id: str,
    principal: Principal = Depends(require_admin),
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobRetryView]:
    source = await jobs.get_job(session, job_id)
    if source is None:
        raise NotFoundException("后台任务不存在")
    was_blocked = source.status == JobStatus.BLOCKED
    try:
        created = await jobs.retry_job(
            session,
            source,
            actor_kind=principal.kind,
            actor_name=principal.name,
            origin=_origin(principal, client_name),
        )
    except jobs.JobFailed as exc:
        raise BadRequestException(exc.message) from exc
    return ApiResponse(
        data=JobRetryView(
            created=created.created,
            job=await job_view(session, created.job),
        ),
        message=(
            "任务已恢复并重新入队"
            if was_blocked
            else "任务已重新入队"
            if created.created
            else "相同任务已在进行中"
        ),
    )


@router.post(
    "/{job_id}/dismiss",
    response_model=ApiResponse[JobDismissView],
    summary="忽略失败的后台任务（不再计入需要处理）",
    operation_id="jobs.dismiss",
)
async def dismiss_job_row(
    job_id: str,
    payload: JobDismissRequest | None = None,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobDismissView]:
    """用户对一条失败任务说"不处理了"。

    任务仍然是失败——日志、事件时间线与重试入口原样保留，只是不再占着
    「需要处理」。想反悔走 ``jobs.undismiss``。

    ``mute_source`` 是这个动作真正的分量所在：不勾它，下次自动扫描还会为
    同一个对象重建同样的任务、再失败一次（issue #221）。勾上则连自动来源
    一起静音，手动触发不受影响。
    """
    try:
        row, dismissed = await jobs.dismiss_job(session, job_id, dismissed_by=str(principal))
    except jobs.JobFailed as exc:
        raise BadRequestException(exc.message) from exc
    if row is None:
        raise NotFoundException("后台任务不存在")

    muted = False
    if payload is not None and payload.mute_source:
        muted = await subtitle_auto.mute_from_job(session, row, muted_by=str(principal))

    if not dismissed:
        message = "任务已经忽略过了"
    elif muted:
        message = "已忽略，并且不再自动重建同类任务"
    else:
        message = "已忽略，不再计入需要处理"
    return ApiResponse(
        data=JobDismissView(
            dismissed=dismissed, muted=muted, job=await job_view(session, row)
        ),
        message=message,
    )


@router.post(
    "/{job_id}/undismiss",
    response_model=ApiResponse[JobDismissView],
    summary="撤销忽略，任务回到需要处理",
    operation_id="jobs.undismiss",
)
async def undismiss_job_row(
    job_id: str,
    _principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobDismissView]:
    """撤销忽略。连带解除自动来源静音——否则"撤销"只撤了一半。"""
    row, restored = await jobs.undismiss_job(session, job_id)
    if row is None:
        raise NotFoundException("后台任务不存在")
    if restored:
        await subtitle_auto.unmute_from_job(session, row)
    return ApiResponse(
        data=JobDismissView(dismissed=False, muted=False, job=await job_view(session, row)),
        message="已撤销忽略" if restored else "任务并未被忽略",
    )
