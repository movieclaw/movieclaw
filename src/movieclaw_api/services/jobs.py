"""持久化后台任务：统一状态、恢复、取消、资源锁与事件时间线。

第一阶段刻意保持单体架构：SQLite 是事实源，当前 FastAPI 进程内的 dispatcher
只负责领取和执行。领取使用数据库租约、资源锁使用独立表，因此进程崩溃后任务
不会丢，也为以后多 worker 预留了安全边界；不引入 Redis/Celery，避免 NAS 部署
平白增加一个必须维护的基础设施。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    Job,
    JobEvent,
    JobLock,
    JobResource,
    JobStatus,
    utcnow,
)

logger = logging.getLogger("movieclaw_api.jobs")

JOB_LEASE_SECONDS = 45
JOB_HEARTBEAT_SECONDS = 15
JOB_RECOVERY_SECONDS = 15
DEFAULT_MAX_PARALLEL = 4
ACTIVE_STATUS_VALUES = tuple(status.value for status in ACTIVE_JOB_STATUSES)
_SECRET_INPUT_KEYS = {
    "apikey",
    "authorization",
    "apitoken",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "idtoken",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "sessiontoken",
    "token",
    "accesstoken",
}


@dataclass(frozen=True)
class ResourceRef:
    """领域资源中性引用；前端可据此把任务聚合到库、条目或文件。"""

    resource_type: str
    resource_id: str | int
    relation: str = "target"


@dataclass(frozen=True)
class CreateJobResult:
    job: Job
    created: bool


class JobControlError(Exception):
    """处理器主动给执行器的结构化结论，绝不把内部异常文本直接暴露。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        actions: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.actions = actions or []
        self.details = details or {}

    def as_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "actions": self.actions,
        }


class JobFailed(JobControlError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "JOB_FAILED",
        actions: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, actions=actions, details=details)


class JobBlocked(JobControlError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "JOB_BLOCKED",
        actions: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, actions=actions, details=details)


class JobRetry(JobControlError):
    def __init__(
        self,
        message: str,
        *,
        delay_seconds: int = 15,
        code: str = "JOB_RETRY",
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, code=code, actions=actions)
        self.delay_seconds = max(1, delay_seconds)


class JobCancelled(Exception):
    """处理器已在安全边界响应取消。"""


JobHandler = Callable[["JobContext", dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class RegisteredJobHandler:
    handler: JobHandler
    definition_versions: frozenset[int]


_handlers: dict[str, RegisteredJobHandler] = {}


def register_job_handler(
    job_type: str, *, definition_versions: Sequence[int] = (1,)
) -> Callable[[JobHandler], JobHandler]:
    """注册稳定任务类型的处理器；重复注册立即失败，避免升级后路由不确定。"""
    supported = frozenset(definition_versions)
    if not supported or any(version < 1 for version in supported):
        raise ValueError(f"后台任务定义版本不合法：{job_type}")

    def decorator(handler: JobHandler) -> JobHandler:
        existing = _handlers.get(job_type)
        if existing is not None and existing.handler is not handler:
            raise RuntimeError(f"后台任务处理器重复注册：{job_type}")
        _handlers[job_type] = RegisteredJobHandler(handler, supported)
        return handler

    return decorator


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def default_progress(message: str = "等待执行") -> dict[str, Any]:
    """所有任务共享的进度外壳；领域明细只放 details，不污染公共契约。"""
    return {
        "mode": "waiting",
        "phase": "queued",
        "message": message,
        "current": None,
        "total": None,
        "percent": None,
        "phase_index": None,
        "phase_count": None,
        "details": {},
    }


def _secret_input_path(value: object, path: str = "input_data") -> str | None:
    """拒绝把凭据固化进任务台账；任务只能保存配置引用。"""
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            child_path = f"{path}.{key}"
            if normalized in _SECRET_INPUT_KEYS:
                return child_path
            found = _secret_input_path(child, child_path)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found = _secret_input_path(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


async def create_job(
    session: AsyncSession,
    *,
    job_type: str,
    input_data: dict[str, Any],
    subject: str | None = None,
    resources: Sequence[ResourceRef] = (),
    dedupe_key: str | None = None,
    conflict_policy: str = "return_existing",
    definition_version: int = 1,
    handler_revision: str = "1",
    prompt_revision: str | None = None,
    provider_ref: str | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "system",
    correlation_id: str | None = None,
    parent_job_id: str | None = None,
    root_job_id: str | None = None,
    triggered_by_job_id: str | None = None,
    retry_of_job_id: str | None = None,
    progress: dict[str, Any] | None = None,
    commit: bool = True,
) -> CreateJobResult:
    """创建任务并写入首个事件；可复用调用方事务实现“业务写入 + 入队”原子化。"""
    if conflict_policy not in {"return_existing", "reject", "queue"}:
        raise ValueError(f"不支持的任务冲突策略：{conflict_policy}")
    secret_path = _secret_input_path(input_data)
    if secret_path is not None:
        raise ValueError(f"后台任务输入不能包含凭据字段：{secret_path}；请保存配置引用而不是密钥")
    if dedupe_key and conflict_policy != "queue":
        existing = (
            (
                await session.execute(
                    select(Job)
                    .where(Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUS_VALUES))
                    .order_by(Job.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            if conflict_policy == "reject":
                raise JobFailed("相同任务已在进行中", code="JOB_CONFLICT")
            return CreateJobResult(existing, False)

    job = Job(
        id=_new_job_id(),
        job_type=job_type,
        subject=subject,
        definition_version=definition_version,
        handler_revision=handler_revision,
        prompt_revision=prompt_revision,
        provider_ref=provider_ref,
        input_data=input_data,
        progress=progress or default_progress(),
        dedupe_key=dedupe_key,
        conflict_policy=conflict_policy,
        priority=priority,
        max_attempts=max(1, max_attempts),
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        correlation_id=correlation_id,
        parent_job_id=parent_job_id,
        root_job_id=root_job_id,
        triggered_by_job_id=triggered_by_job_id,
        retry_of_job_id=retry_of_job_id,
    )
    session.add(job)
    session.add(JobEvent(job_id=job.id, revision=1, event_type="created", payload=job.progress))
    seen: set[tuple[str, str, str]] = set()
    for resource in resources:
        key = (resource.resource_type, str(resource.resource_id), resource.relation)
        if key in seen:
            continue
        seen.add(key)
        session.add(
            JobResource(
                job_id=job.id,
                resource_type=key[0],
                resource_id=key[1],
                relation=key[2],
            )
        )
    try:
        if commit:
            await session.commit()
        else:
            await session.flush()
    except IntegrityError:
        await session.rollback()
        if not dedupe_key or conflict_policy == "queue":
            raise
        existing = (
            (
                await session.execute(
                    select(Job)
                    .where(Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUS_VALUES))
                    .order_by(Job.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            raise
        if conflict_policy == "reject":
            raise JobFailed("相同任务已在进行中", code="JOB_CONFLICT") from None
        return CreateJobResult(existing, False)
    await notify_job_change()
    return CreateJobResult(job, True)


async def get_job(session: AsyncSession, job_id: str) -> Job | None:
    return await session.get(Job, job_id)


async def resources_for_jobs(
    session: AsyncSession, job_ids: Sequence[str]
) -> dict[str, list[JobResource]]:
    if not job_ids:
        return {}
    rows = list(
        (
            await session.execute(
                select(JobResource).where(JobResource.job_id.in_(job_ids)).order_by(JobResource.id)
            )
        ).scalars()
    )
    out: dict[str, list[JobResource]] = {}
    for row in rows:
        out.setdefault(row.job_id, []).append(row)
    return out


async def list_jobs(
    session: AsyncSession,
    *,
    statuses: Sequence[JobStatus] = (),
    job_type: str | None = None,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    active_only: bool = False,
    limit: int = 50,
) -> list[Job]:
    statement = select(Job)
    if resource_type is not None or resource_id is not None:
        statement = statement.join(JobResource, JobResource.job_id == Job.id)
        if resource_type is not None:
            statement = statement.where(JobResource.resource_type == resource_type)
        if resource_id is not None:
            statement = statement.where(JobResource.resource_id == str(resource_id))
    if active_only:
        statement = statement.where(Job.status.in_(ACTIVE_STATUS_VALUES))
    elif statuses:
        statement = statement.where(Job.status.in_([status.value for status in statuses]))
    if job_type:
        statement = statement.where(Job.job_type == job_type)
    statement = statement.order_by(Job.created_at.desc()).limit(min(max(limit, 1), 200))
    return list((await session.execute(statement)).scalars().unique())


async def list_jobs_by_resource(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_ids: Sequence[str | int],
    job_type: str | None = None,
    limit_per_resource: int = 10,
) -> dict[str, list[Job]]:
    """一次取回**多个**资源各自最近的若干个 Job：``resource_id -> [Job, ...]``。

    存在的理由是列表页：媒体库列表要给每张库卡片显示扫描状态，逐库调用
    ``list_jobs`` 就是一次 N+1——79 个库 = 79 次 join 查询，而且随着 job
    台账增长而持续变慢（实测空表 70 ms、9 万行 131 ms）。

    每个资源内的顺序与 ``list_jobs`` 完全一致（``created_at`` 倒序），
    调用方拿到的切片因此可以直接顶替逐个查询的结果。用窗口函数在库内分组
    取前 N，不是取全部再在内存里切——后者在长期运行的部署上会把整张 job
    表读进内存。

    ``resource_ids`` 为空时直接返回空字典，不打库。
    """
    if not resource_ids:
        return {}
    keys = [str(resource_id) for resource_id in resource_ids]
    ranked = (
        select(
            JobResource.resource_id.label("resource_id"),
            Job.id.label("job_id"),
            func.row_number()
            .over(
                partition_by=JobResource.resource_id,
                order_by=Job.created_at.desc(),
            )
            .label("rank"),
        )
        .join(Job, Job.id == JobResource.job_id)
        .where(
            JobResource.resource_type == resource_type,
            JobResource.resource_id.in_(keys),
        )
    )
    if job_type:
        ranked = ranked.where(Job.job_type == job_type)
    ranked = ranked.subquery()

    rows = (
        await session.execute(
            select(ranked.c.resource_id, Job)
            .join(Job, Job.id == ranked.c.job_id)
            .where(ranked.c.rank <= max(limit_per_resource, 1))
            .order_by(ranked.c.resource_id, Job.created_at.desc())
        )
    ).all()

    grouped: dict[str, list[Job]] = {key: [] for key in keys}
    for resource_id, job in rows:
        grouped[resource_id].append(job)
    return grouped


async def latest_job_for_resource(
    session: AsyncSession,
    resource_type: str,
    resource_id: str | int,
    *,
    job_type: str | None = None,
) -> Job | None:
    rows = await list_jobs(
        session,
        resource_type=resource_type,
        resource_id=resource_id,
        job_type=job_type,
        limit=1,
    )
    return rows[0] if rows else None


async def resource_lock_owner(
    session: AsyncSession, resource_type: str, resource_id: str | int
) -> str | None:
    """返回当前持有领域资源租约的 Job；没有锁则返回 ``None``。

    少数仍由文件监听、定时调度器直接触发的轻量任务不进入 Job 历史，但它们
    也必须尊重统一作业的资源锁。对外只暴露持有者 id，领域代码不能自行改锁。
    """
    return (
        await session.execute(
            select(JobLock.job_id).where(
                JobLock.lock_key == f"{resource_type}:{resource_id}",
                JobLock.lease_expires_at >= utcnow(),
            )
        )
    ).scalar_one_or_none()


async def job_events(
    session: AsyncSession, job_id: str | None = None, *, after_id: int = 0, limit: int = 200
) -> list[JobEvent]:
    statement = select(JobEvent).where(JobEvent.id > after_id)
    if job_id is not None:
        statement = statement.where(JobEvent.job_id == job_id)
    statement = statement.order_by(JobEvent.id).limit(min(max(limit, 1), 500))
    return list((await session.execute(statement)).scalars())


async def latest_job_event_id(session: AsyncSession) -> int:
    """返回事件表尾游标；浏览器首次订阅不回放全部历史。"""
    value = (await session.execute(select(func.max(JobEvent.id)))).scalar_one_or_none()
    return int(value or 0)


async def _record_transition(
    session: AsyncSession,
    job: Job,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    # 取消请求、执行进度和心跳可能来自不同 session。revision 必须由数据库原子
    # 自增，不能依赖各 session 里可能过期的 ORM 值，否则会撞唯一事件序号。
    now = utcnow()
    await session.flush()
    await session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(revision=Job.revision + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(job, attribute_names=["revision", "updated_at"])
    session.add(
        JobEvent(job_id=job.id, revision=job.revision, event_type=event_type, payload=payload)
    )


async def request_cancel(
    session: AsyncSession, job_id: str, *, requested_by: str, reason: str | None = None
) -> tuple[Job | None, bool]:
    """请求取消任务。``reason`` 是面向用户的取消原因，会直接显示在任务中心；

    只在排队/挂起态的立即取消分支生效——运行中任务要等安全停止点，
    期间处理器仍会整体覆盖 progress，理由留不住。
    """
    job = await session.get(Job, job_id)
    if job is None:
        return None, False
    if job.status in TERMINAL_JOB_STATUSES:
        return job, False
    now = utcnow()
    job.cancel_requested_at = now
    job.cancel_requested_by = requested_by
    if job.status in {
        JobStatus.QUEUED,
        JobStatus.RETRY_WAIT,
        JobStatus.WAITING,
        JobStatus.BLOCKED,
    }:
        job.status = JobStatus.CANCELLED
        job.finished_at = now
        job.retention_until = now + timedelta(days=90)
        job.progress = {
            **job.progress,
            "mode": "paused",
            "phase": "cancelled",
            "message": reason or "任务已取消",
        }
        await _record_transition(session, job, "cancelled", job.progress)
    else:
        job.status = JobStatus.CANCELLING
        job.progress = {
            **job.progress,
            "mode": "waiting",
            "message": "已请求取消，正在等待安全停止点",
        }
        await _record_transition(session, job, "cancel_requested", job.progress)
    await session.commit()
    await notify_job_change()
    return job, True


async def dismiss_job(
    session: AsyncSession, job_id: str, *, dismissed_by: str
) -> tuple[Job | None, bool]:
    """忽略任务：用户明确表示"这件事我不打算处理了"。

    只对**失败**任务开放。理由是其余状态各有正确出口，忽略不该抢它们的活：
    活跃任务（含 ``blocked``）要真正终结得走取消——它们仍占着去重键与资源锁，
    只把提醒藏起来会留下一个谁也看不见、却挡着同名任务再次创建的幽灵；
    ``succeeded`` / ``cancelled`` 本来就不在「需要处理」里，无须忽略。

    忽略不改写 status（见 Job 类文档）：任务仍然是 failed，日志、事件时间线、
    重试链一概不动，用户随时还能重试或撤销忽略。这里只补一条保留期，让它跟
    已取消的任务一样最终被清理任务收走，而不是永久占着台账。

    返回 ``(job, 是否本次真的改变了状态)``；已经忽略过的返回 ``False`` 保持幂等。
    """
    job = await session.get(Job, job_id)
    if job is None:
        return None, False
    if job.status is not JobStatus.FAILED:
        raise JobFailed(
            "只有失败的任务可以忽略；进行中的任务请使用取消",
            code="JOB_NOT_DISMISSABLE",
        )
    if job.dismissed_at is not None:
        return job, False
    now = utcnow()
    job.dismissed_at = now
    job.dismissed_by = dismissed_by
    job.retention_until = now + timedelta(days=90)
    await _record_transition(session, job, "dismissed", {"dismissed_by": dismissed_by})
    await session.commit()
    await notify_job_change()
    return job, True


async def undismiss_job(session: AsyncSession, job_id: str) -> tuple[Job | None, bool]:
    """撤销忽略：任务回到「需要处理」。忽略是用户的判断，就必须能反悔。"""
    job = await session.get(Job, job_id)
    if job is None:
        return None, False
    if job.dismissed_at is None:
        return job, False
    job.dismissed_at = None
    job.dismissed_by = None
    job.retention_until = None
    await _record_transition(session, job, "undismissed", {})
    await session.commit()
    await notify_job_change()
    return job, True


async def dismiss_failed_jobs(
    session: AsyncSession, *, dismissed_by: str, job_type: str | None = None
) -> list[Job]:
    """批量忽略当前所有未忽略的失败任务。

    存在的理由是真实故障的形状：一次媒体库扫描收尾可能一口气建出几十个字幕
    任务，模型配置错了就几十条一起失败。逐条点忽略是灾难，逐条发请求同样是
    ——所以给一个"当前全部"的整体动作，而不是让前端循环调用单条接口。

    只收口**此刻**已经失败的任务；之后再失败的任务仍会正常提醒，这才是用户
    要的"把眼前这批清掉"，而不是从此对失败视而不见。
    """
    statement = select(Job).where(
        Job.status == JobStatus.FAILED.value,
        Job.dismissed_at.is_(None),
    )
    if job_type:
        statement = statement.where(Job.job_type == job_type)
    rows = list((await session.execute(statement)).scalars().unique())
    if not rows:
        return []
    now = utcnow()
    for job in rows:
        job.dismissed_at = now
        job.dismissed_by = dismissed_by
        job.retention_until = now + timedelta(days=90)
        await _record_transition(session, job, "dismissed", {"dismissed_by": dismissed_by})
    await session.commit()
    await notify_job_change()
    return rows


async def retry_job(
    session: AsyncSession,
    source: Job,
    *,
    actor_kind: str | None,
    actor_name: str | None,
    origin: str,
) -> CreateJobResult:
    if source.status == JobStatus.BLOCKED:
        # blocked 仍占用活跃 dedupe_key，不能另建一条相同任务。用户修复配置后
        # 原地重新入队，既保留稳定 job id 与完整事件历史，也不会绕过去重约束。
        source.status = JobStatus.QUEUED
        source.available_at = utcnow()
        source.error = None
        source.retention_until = None
        source.progress = {
            **source.progress,
            "mode": "waiting",
            "phase": "queued",
            "message": "已重新入队，正在等待执行",
        }
        await _record_transition(session, source, "unblocked", source.progress)
        await session.commit()
        await notify_job_change()
        return CreateJobResult(source, False)
    if source.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
        raise JobFailed("只有失败、已取消或需要处理的任务可以重试", code="JOB_NOT_RETRYABLE")
    resources = await resources_for_jobs(session, [source.id])
    return await create_job(
        session,
        job_type=source.job_type,
        subject=source.subject,
        input_data=source.input_data,
        resources=[
            ResourceRef(item.resource_type, item.resource_id, item.relation)
            for item in resources.get(source.id, [])
        ],
        dedupe_key=source.dedupe_key,
        conflict_policy=source.conflict_policy,
        definition_version=source.definition_version,
        handler_revision=source.handler_revision,
        prompt_revision=source.prompt_revision,
        provider_ref=source.provider_ref,
        priority=source.priority,
        max_attempts=source.max_attempts,
        actor_kind=actor_kind,
        actor_name=actor_name,
        origin=origin,
        correlation_id=source.correlation_id,
        parent_job_id=source.parent_job_id,
        # 首次重试把原任务立为根；后续重试沿用同一根，便于审计整条尝试链。
        root_job_id=source.root_job_id or source.id,
        triggered_by_job_id=source.triggered_by_job_id,
        retry_of_job_id=source.id,
    )


class JobContext:
    """领域处理器可用的窄接口，避免处理器自行篡改状态机字段。"""

    def __init__(
        self,
        job_id: str,
        *,
        lease_token: str,
        lease_lost: asyncio.Event,
    ) -> None:
        self.job_id = job_id
        self._lease_token = lease_token
        self._lease_lost = lease_lost

    async def update_progress(
        self,
        *,
        mode: str,
        phase: str,
        message: str,
        current: int | None = None,
        total: int | None = None,
        percent: float | None = None,
        phase_index: int | None = None,
        phase_count: int | None = None,
        details: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        progress = {
            "mode": mode,
            "phase": phase,
            "message": message,
            "current": current,
            "total": total,
            "percent": percent,
            "phase_index": phase_index,
            "phase_count": phase_count,
            "details": details or {},
        }
        db = get_database()
        async with db.session() as session:
            job = await session.get(Job, self.job_id)
            if job is None or job.status in TERMINAL_JOB_STATUSES:
                return
            if job.lease_owner != self._lease_token:
                self._lease_lost.set()
                return
            job.progress = progress
            if usage is not None:
                # 用量和进度共享同一事务，前端、CLI 与 Agent 读取任一 revision
                # 都不会看到“进度已完成但 token 仍滞后”的撕裂状态。
                job.usage = usage
            await _record_transition(session, job, "progress", progress)
            await session.commit()
        await notify_job_change()

    async def current_progress(self) -> dict[str, Any]:
        """读取本任务最近一次已持久化进度，供可重入处理器恢复检查点。

        处理器不能缓存 ORM ``Job``：进度、取消请求和租约由不同会话更新。
        每次恢复时重新读取事实源，才能在服务更新后从已确认的安全边界继续。
        """
        db = get_database()
        async with db.session() as session:
            job = await session.get(Job, self.job_id)
            if job is None or job.lease_owner != self._lease_token:
                self._lease_lost.set()
                return default_progress("任务执行租约已失效")
            return dict(job.progress or {})

    async def cancel_requested(self) -> bool:
        if self._lease_lost.is_set():
            return True
        db = get_database()
        async with db.session() as session:
            job = await session.get(Job, self.job_id)
            return bool(
                job is None
                or job.lease_owner != self._lease_token
                or job.cancel_requested_at is not None
            )

    async def raise_if_cancelled(self) -> None:
        if await self.cancel_requested():
            raise JobCancelled

    async def acquire_target_resource(self, resource_type: str, resource_id: str | int) -> bool:
        """给运行中的任务补充一个执行期才确定的目标资源并尝试加锁。

        自动路由类任务入队时还不知道最终媒体库，不能提前伪造资源。处理器在
        收敛出目标后调用本方法：资源关系先持久化，任务中心立即能建立归属；
        资源空闲时同时取得与当前任务租约同寿命的锁。若已被别的任务占用返回
        ``False``，调用方应保持当前阶段并稍后重试，绝不能越过统一资源锁。

        当前只允许追加单个目标资源。调用方不得用它按不同顺序获取一组资源，
        以免引入动态多锁死锁；需要多资源的任务仍应在入队时一次声明完整集合。
        """
        if self._lease_lost.is_set():
            return False
        key = f"{resource_type}:{resource_id}"
        db = get_database()
        changed = False
        async with db.session() as session:
            job = await session.get(Job, self.job_id)
            if job is None or job.lease_owner != self._lease_token:
                self._lease_lost.set()
                return False
            existing_resource = (
                await session.execute(
                    select(JobResource).where(
                        JobResource.job_id == self.job_id,
                        JobResource.resource_type == resource_type,
                        JobResource.resource_id == str(resource_id),
                        JobResource.relation == "target",
                    )
                )
            ).scalar_one_or_none()
            if existing_resource is None:
                session.add(
                    JobResource(
                        job_id=self.job_id,
                        resource_type=resource_type,
                        resource_id=str(resource_id),
                        relation="target",
                    )
                )
                changed = True

            now = utcnow()
            await session.execute(delete(JobLock).where(JobLock.lease_expires_at < now))
            lock = await session.get(JobLock, key)
            if lock is not None and lock.job_id != self.job_id:
                # 即使暂时拿不到锁，也先保存资源关系。任务下一次恢复领取时会
                # 从一开始就把它纳入原子锁集合，不再存在检查后被插队的窗口。
                await session.commit()
                if changed:
                    await notify_job_change()
                return False
            if lock is None:
                session.add(
                    JobLock(
                        lock_key=key,
                        job_id=self.job_id,
                        lease_expires_at=job.lease_expires_at
                        or now + timedelta(seconds=JOB_LEASE_SECONDS),
                    )
                )
                changed = True
            await session.commit()
        if changed:
            await notify_job_change()
        return True

    async def attach_resource(
        self,
        resource_type: str,
        resource_id: str | int,
        *,
        relation: str = "context",
    ) -> None:
        """给当前任务补充只用于聚合/导航的非锁定资源关系。"""
        if relation == "target":
            raise ValueError("目标资源必须通过 acquire_target_resource 获取")
        db = get_database()
        created = False
        async with db.session() as session:
            job = await session.get(Job, self.job_id)
            if job is None or job.lease_owner != self._lease_token:
                self._lease_lost.set()
                return
            existing = (
                await session.execute(
                    select(JobResource).where(
                        JobResource.job_id == self.job_id,
                        JobResource.resource_type == resource_type,
                        JobResource.resource_id == str(resource_id),
                        JobResource.relation == relation,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    JobResource(
                        job_id=self.job_id,
                        resource_type=resource_type,
                        resource_id=str(resource_id),
                        relation=relation,
                    )
                )
                await session.commit()
                created = True
        if created:
            await notify_job_change()


class JobDispatcher:
    """从 SQLite 领取任务的进程内执行器。

    每个运行实例有独立 owner；任务与资源锁均带租约。正常停机把本实例正在执行
    的任务退回队列，崩溃则由下一实例在租约过期后恢复。进度/状态/事件和锁释放
    始终在同一数据库事务内完成，再通知 SSE 观察者。
    """

    def __init__(self, *, max_parallel: int = DEFAULT_MAX_PARALLEL) -> None:
        self.owner = f"worker_{uuid.uuid4().hex}"
        self.max_parallel = max(1, max_parallel)
        self._wakeup = asyncio.Event()
        self._closing = False
        self._loop_task: asyncio.Task[None] | None = None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._last_cleanup = 0.0
        self._last_recovery = 0.0

    async def start(self) -> None:
        await self._recover_expired_jobs()
        await purge_expired_jobs()
        now = time.monotonic()
        self._last_cleanup = now
        self._last_recovery = now
        self._loop_task = asyncio.create_task(self._run_loop(), name="job-dispatcher")
        logger.info("持久化任务执行器已启动：owner=%s parallel=%d", self.owner, self.max_parallel)

    async def close(self) -> None:
        self._closing = True
        self._wakeup.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        running = list(self._running.values())
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._running.clear()
        logger.info("持久化任务执行器已关闭，未完成任务已退回队列")

    def wake(self) -> None:
        self._wakeup.set()

    async def _run_loop(self) -> None:
        while not self._closing:
            try:
                if time.monotonic() - self._last_cleanup >= 3600:
                    await purge_expired_jobs()
                    self._last_cleanup = time.monotonic()
                if time.monotonic() - self._last_recovery >= JOB_RECOVERY_SECONDS:
                    await self._recover_expired_jobs()
                    self._last_recovery = time.monotonic()
                while len(self._running) < self.max_parallel:
                    claimed = await self._claim_one()
                    if claimed is None:
                        break
                    job_id, lease_token = claimed
                    task = asyncio.create_task(
                        self._execute(job_id, lease_token), name=f"job-{job_id}"
                    )
                    self._running[job_id] = task
                    task.add_done_callback(lambda _task, jid=job_id: self._done(jid))
                self._wakeup.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- dispatcher 不能因单次 DB 抖动永久退出
                logger.exception("后台任务执行器调度失败，稍后自动重试")
                await asyncio.sleep(1)

    def _done(self, job_id: str) -> None:
        self._running.pop(job_id, None)
        self._wakeup.set()

    async def _recover_expired_jobs(self) -> None:
        now = utcnow()
        db = get_database()
        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(Job).where(
                            Job.status.in_([JobStatus.RUNNING.value, JobStatus.CANCELLING.value]),
                            Job.lease_expires_at.is_not(None),
                            Job.lease_expires_at < now,
                        )
                    )
                ).scalars()
            )
            recovered = 0
            for candidate in rows:
                progress = {
                    **candidate.progress,
                    "mode": "waiting",
                    "phase": "resuming",
                    "message": "服务已恢复，正在从保存进度继续",
                }
                # 多个存活实例可能同时发现同一过期租约。状态与过期时间条件让
                # 只有一个实例能完成接管，也不会覆盖刚被其他实例重新领取的任务。
                result = await session.execute(
                    update(Job)
                    .where(
                        Job.id == candidate.id,
                        Job.status.in_([JobStatus.RUNNING.value, JobStatus.CANCELLING.value]),
                        Job.lease_expires_at.is_not(None),
                        Job.lease_expires_at < now,
                    )
                    .values(
                        status=JobStatus.QUEUED,
                        available_at=now,
                        # 崩溃/强制更新后的恢复不是一次业务失败，不应消耗
                        # max_attempts。重新领取时会再 +1，用户看到的尝试次数
                        # 因此保持不变。
                        attempt=max(0, candidate.attempt - 1),
                        lease_owner=None,
                        lease_expires_at=None,
                        progress=progress,
                    )
                )
                if not result.rowcount:
                    continue
                job = await session.get(Job, candidate.id, populate_existing=True)
                assert job is not None
                await _record_transition(session, job, "recovered", progress)
                recovered += 1
            await session.execute(delete(JobLock).where(JobLock.lease_expires_at < now))
            await session.commit()
            if recovered:
                logger.info("已恢复 %d 个租约过期的后台任务", recovered)
                await notify_job_change()

    async def _claim_one(self) -> tuple[str, str] | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=JOB_LEASE_SECONDS)
        db = get_database()
        async with db.session() as session:
            candidates = list(
                (
                    await session.execute(
                        select(Job)
                        .where(
                            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value]),
                            Job.available_at <= now,
                        )
                        .order_by(Job.priority.desc(), Job.created_at)
                        .limit(10)
                    )
                ).scalars()
            )
            for candidate in candidates:
                lease_token = f"{self.owner}:{uuid.uuid4().hex}"
                resources = list(
                    (
                        await session.execute(
                            select(JobResource)
                            .where(
                                JobResource.job_id == candidate.id,
                                JobResource.relation == "target",
                            )
                            .order_by(JobResource.resource_type, JobResource.resource_id)
                        )
                    ).scalars()
                )
                lock_keys = sorted(
                    {f"{item.resource_type}:{item.resource_id}" for item in resources}
                )
                await session.execute(delete(JobLock).where(JobLock.lease_expires_at < now))
                if lock_keys:
                    occupied = (
                        (
                            await session.execute(
                                select(JobLock).where(JobLock.lock_key.in_(lock_keys))
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if occupied is not None:
                        continue
                claimed = await session.execute(
                    update(Job)
                    .where(
                        Job.id == candidate.id,
                        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value]),
                    )
                    .values(
                        status=JobStatus.RUNNING,
                        lease_owner=lease_token,
                        lease_expires_at=lease_until,
                        heartbeat_at=now,
                        started_at=candidate.started_at or now,
                        attempt=Job.attempt + 1,
                        updated_at=now,
                    )
                )
                if not claimed.rowcount:
                    await session.rollback()
                    continue
                try:
                    for key in lock_keys:
                        session.add(
                            JobLock(
                                lock_key=key,
                                job_id=candidate.id,
                                lease_expires_at=lease_until,
                            )
                        )
                    # 重新读取 update 后的 revision，状态事件与领取同一事务提交。
                    job = await session.get(Job, candidate.id, populate_existing=True)
                    assert job is not None
                    await _record_transition(
                        session,
                        job,
                        "started",
                        {"attempt": job.attempt, "owner": self.owner},
                    )
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                await notify_job_change()
                return candidate.id, lease_token
        return None

    async def _execute(self, job_id: str, lease_token: str) -> None:
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(job_id, lease_token, lease_lost), name=f"heartbeat-{job_id}"
        )
        try:
            db = get_database()
            async with db.session() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    return
                registration = _handlers.get(job.job_type)
                input_data = dict(job.input_data)
            if registration is None:
                raise JobBlocked(
                    "当前版本无法执行这类任务，请更新 MovieClaw 后重试",
                    code="JOB_HANDLER_UNAVAILABLE",
                    actions=[
                        {"type": "update_runtime", "label": "检查更新"},
                        {"type": "retry_job", "label": "更新后重试"},
                    ],
                )
            if job.definition_version not in registration.definition_versions:
                raise JobFailed(
                    "任务定义与当前版本不兼容，请从原功能入口重新发起",
                    code="JOB_DEFINITION_UNSUPPORTED",
                    actions=[{"type": "handoff_agent", "label": "交给 Agent"}],
                    details={
                        "definition_version": job.definition_version,
                        "supported_versions": sorted(registration.definition_versions),
                    },
                )
            result = await registration.handler(
                JobContext(job_id, lease_token=lease_token, lease_lost=lease_lost),
                input_data,
            )
            if lease_lost.is_set():
                logger.warning("任务执行租约已失效，丢弃本次执行结果：job=%s", job_id)
                return
            await self._finish_success(job_id, lease_token, result or {})
        except JobCancelled:
            await self._finish_cancelled(job_id, lease_token)
        except JobBlocked as exc:
            await self._finish_controlled(job_id, lease_token, JobStatus.BLOCKED, exc)
        except JobRetry as exc:
            await self._finish_retry(job_id, lease_token, exc)
        except JobFailed as exc:
            await self._finish_controlled(job_id, lease_token, JobStatus.FAILED, exc)
        except asyncio.CancelledError:
            await self._pause_for_shutdown(job_id, lease_token)
            raise
        except Exception:  # noqa: BLE001 -- 未知错误只进日志，对用户返回收敛后的描述
            logger.exception("后台任务执行失败：job=%s", job_id)
            await self._finish_unexpected(job_id, lease_token)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, job_id: str, lease_token: str, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(JOB_HEARTBEAT_SECONDS)
            now = utcnow()
            lease = now + timedelta(seconds=JOB_LEASE_SECONDS)
            try:
                db = get_database()
                async with db.session() as session:
                    renewed = await session.execute(
                        update(Job)
                        .where(Job.id == job_id, Job.lease_owner == lease_token)
                        .values(heartbeat_at=now, lease_expires_at=lease)
                    )
                    if not renewed.rowcount:
                        await session.rollback()
                        lease_lost.set()
                        logger.warning("任务执行租约已被接管：job=%s", job_id)
                        return
                    await session.execute(
                        update(JobLock)
                        .where(JobLock.job_id == job_id)
                        .values(lease_expires_at=lease)
                    )
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- 短暂 DB 抖动时继续争取在租约内续期
                logger.exception("后台任务心跳续租失败，将自动重试：job=%s", job_id)
                await asyncio.sleep(3)

    async def _load_owned(self, session: AsyncSession, job_id: str, lease_token: str) -> Job | None:
        job = await session.get(Job, job_id)
        if job is None or job.lease_owner != lease_token:
            return None
        return job

    async def _release_locks(self, session: AsyncSession, job_id: str) -> None:
        await session.execute(delete(JobLock).where(JobLock.job_id == job_id))

    async def _finish_success(self, job_id: str, lease_token: str, result: dict[str, Any]) -> None:
        db = get_database()
        async with db.session() as session:
            job = await self._load_owned(session, job_id, lease_token)
            if job is None:
                return
            if job.cancel_requested_at is not None:
                await self._set_cancelled(session, job)
            else:
                now = utcnow()
                job.status = JobStatus.SUCCEEDED
                job.result = result
                job.error = None
                job.finished_at = now
                job.retention_until = now + timedelta(days=30)
                job.progress = {
                    **job.progress,
                    "mode": "determinate",
                    "phase": "completed",
                    "message": result.get("message", "任务已完成"),
                    "percent": 100.0,
                }
                await _record_transition(session, job, "succeeded", result)
            job.lease_owner = None
            job.lease_expires_at = None
            await self._release_locks(session, job_id)
            await session.commit()
        await notify_job_change()

    async def _set_cancelled(self, session: AsyncSession, job: Job) -> None:
        now = utcnow()
        job.status = JobStatus.CANCELLED
        job.finished_at = now
        job.retention_until = now + timedelta(days=90)
        job.progress = {
            **job.progress,
            "mode": "paused",
            "phase": "cancelled",
            "message": "任务已取消，已保存可复用的中间进度",
        }
        await _record_transition(session, job, "cancelled", job.progress)

    async def _finish_cancelled(self, job_id: str, lease_token: str) -> None:
        db = get_database()
        async with db.session() as session:
            job = await self._load_owned(session, job_id, lease_token)
            if job is None:
                return
            await self._set_cancelled(session, job)
            job.lease_owner = None
            job.lease_expires_at = None
            await self._release_locks(session, job_id)
            await session.commit()
        await notify_job_change()

    async def _finish_controlled(
        self,
        job_id: str,
        lease_token: str,
        status: JobStatus,
        exc: JobControlError,
    ) -> None:
        db = get_database()
        async with db.session() as session:
            job = await self._load_owned(session, job_id, lease_token)
            if job is None:
                return
            now = utcnow()
            job.status = status
            job.error = exc.as_error()
            job.finished_at = now if status == JobStatus.FAILED else None
            job.retention_until = now + timedelta(days=90)
            job.progress = {
                **job.progress,
                "mode": "paused",
                "phase": status.value,
                "message": exc.message,
            }
            await _record_transition(session, job, status.value, job.error)
            job.lease_owner = None
            job.lease_expires_at = None
            await self._release_locks(session, job_id)
            await session.commit()
        await notify_job_change()

    async def _finish_retry(self, job_id: str, lease_token: str, exc: JobRetry) -> None:
        db = get_database()
        async with db.session() as session:
            job = await self._load_owned(session, job_id, lease_token)
            if job is None:
                return
            if job.attempt >= job.max_attempts:
                terminal = JobFailed(
                    f"{exc.message}；已达到自动重试上限",
                    code="JOB_RETRY_EXHAUSTED",
                    actions=exc.actions,
                )
                now = utcnow()
                job.status = JobStatus.FAILED
                job.error = terminal.as_error()
                job.finished_at = now
                job.retention_until = now + timedelta(days=90)
                event_type = "failed"
            else:
                job.status = JobStatus.RETRY_WAIT
                job.error = exc.as_error()
                job.available_at = utcnow() + timedelta(seconds=exc.delay_seconds)
                event_type = "retry_scheduled"
            job.progress = {
                **job.progress,
                "mode": "waiting",
                "phase": job.status.value,
                "message": job.error["message"],
            }
            await _record_transition(session, job, event_type, job.error)
            job.lease_owner = None
            job.lease_expires_at = None
            await self._release_locks(session, job_id)
            await session.commit()
        await notify_job_change()

    async def _finish_unexpected(self, job_id: str, lease_token: str) -> None:
        db = get_database()
        async with db.session() as session:
            job = await self._load_owned(session, job_id, lease_token)
            if job is None:
                return
            if job.attempt < job.max_attempts:
                job.status = JobStatus.RETRY_WAIT
                job.available_at = utcnow() + timedelta(seconds=min(60, 5 * 2**job.attempt))
                message = "任务遇到临时异常，稍后会自动重试"
                event_type = "retry_scheduled"
            else:
                now = utcnow()
                job.status = JobStatus.FAILED
                job.finished_at = now
                job.retention_until = now + timedelta(days=90)
                message = "任务未完成：发生未知错误，请查看任务详情或交给 Agent 处理"
                event_type = "failed"
            job.error = {
                "code": "JOB_UNEXPECTED_ERROR",
                "message": message,
                "details": {},
                "actions": [
                    {"type": "retry_job", "label": "重试"},
                    {"type": "inspect_logs", "label": "查看日志"},
                    {"type": "handoff_agent", "label": "交给 Agent"},
                ],
            }
            job.progress = {
                **job.progress,
                "mode": "waiting" if job.status == JobStatus.RETRY_WAIT else "paused",
                "phase": job.status.value,
                "message": message,
            }
            await _record_transition(session, job, event_type, job.error)
            job.lease_owner = None
            job.lease_expires_at = None
            await self._release_locks(session, job_id)
            await session.commit()
        await notify_job_change()

    async def _pause_for_shutdown(self, job_id: str, lease_token: str) -> None:
        db = get_database()
        async with db.session() as session:
            job = await self._load_owned(session, job_id, lease_token)
            if job is None:
                return
            job.status = JobStatus.QUEUED
            job.available_at = utcnow()
            # 优雅停机只是把同一次执行交还队列；下次领取会再 +1，先回退
            # 本次计数，避免多次应用更新耗尽真正留给异常重试的额度。
            job.attempt = max(0, job.attempt - 1)
            job.progress = {
                **job.progress,
                "mode": "waiting",
                "phase": "resuming",
                "message": "服务正在重启，启动后会自动继续",
            }
            await _record_transition(session, job, "paused", job.progress)
            job.lease_owner = None
            job.lease_expires_at = None
            await self._release_locks(session, job_id)
            await session.commit()
        await notify_job_change()


_dispatcher: JobDispatcher | None = None
_change_condition: asyncio.Condition | None = None


async def init_job_dispatcher(*, max_parallel: int = DEFAULT_MAX_PARALLEL) -> JobDispatcher:
    global _dispatcher, _change_condition
    _change_condition = asyncio.Condition()
    _dispatcher = JobDispatcher(max_parallel=max_parallel)
    await _dispatcher.start()
    return _dispatcher


def wake_job_dispatcher() -> None:
    if _dispatcher is not None:
        _dispatcher.wake()


async def close_job_dispatcher() -> None:
    global _dispatcher, _change_condition
    if _dispatcher is not None:
        await _dispatcher.close()
    _dispatcher = None
    _change_condition = None


async def notify_job_change() -> None:
    wake_job_dispatcher()
    condition = _change_condition
    if condition is not None:
        async with condition:
            condition.notify_all()


async def wait_for_job_change(timeout: float) -> None:
    """供 long-poll/SSE 共用；跨进程事件由短轮询兜底，本地变更即时唤醒。"""
    condition = _change_condition
    if condition is None:
        await asyncio.sleep(min(timeout, 0.5))
        return
    try:
        async with condition:
            await asyncio.wait_for(condition.wait(), timeout=min(timeout, 0.5))
    except TimeoutError:
        pass


async def dispatcher_health() -> dict[str, Any]:
    db = get_database()
    async with db.session() as session:
        active = list(
            (
                await session.execute(
                    select(Job.status).where(Job.status.in_(ACTIVE_STATUS_VALUES))
                )
            ).scalars()
        )
        oldest = (
            await session.execute(
                select(Job.created_at)
                .where(Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value]))
                .order_by(Job.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    counts = {status.value: 0 for status in JobStatus}
    for status in active:
        key = status.value if isinstance(status, JobStatus) else str(status)
        counts[key] = counts.get(key, 0) + 1
    return {
        "running": _dispatcher is not None and not _dispatcher._closing,
        "owner": _dispatcher.owner if _dispatcher else None,
        "active_workers": len(_dispatcher._running) if _dispatcher else 0,
        "counts": counts,
        "oldest_queued_seconds": (
            max(0, int((utcnow() - oldest).total_seconds())) if oldest is not None else None
        ),
    }


async def purge_expired_jobs() -> int:
    """清理到期任务历史；只删 Job 台账与检查点索引，不碰任何媒体产物。"""
    db = get_database()
    async with db.session() as session:
        expired_ids = list(
            (
                await session.execute(
                    select(Job.id).where(
                        Job.status.in_([status.value for status in TERMINAL_JOB_STATUSES]),
                        Job.retention_until.is_not(None),
                        Job.retention_until < utcnow(),
                    )
                )
            ).scalars()
        )
        if not expired_ids:
            return 0
        # SQLite 的首版 Job 表自引用采用 NO ACTION。删除祖先历史前主动断开
        # 子任务审计链引用，保留子任务自身状态/结果，也避免重建大表改约束。
        for column in (
            Job.parent_job_id,
            Job.root_job_id,
            Job.triggered_by_job_id,
            Job.retry_of_job_id,
        ):
            await session.execute(
                update(Job).where(column.in_(expired_ids)).values({column.key: None})
            )
        result = await session.execute(delete(Job).where(Job.id.in_(expired_ids)))
        await session.commit()
    count = result.rowcount or 0
    if count:
        logger.info("已清理 %d 条到期后台任务历史（媒体产物不受影响）", count)
    return count
