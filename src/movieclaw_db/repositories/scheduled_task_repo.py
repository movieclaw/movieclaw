from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.scheduled_task import (
    ScheduledTask,
    TaskRun,
    TaskRunStatus,
    TriggerType,
)


class ScheduledTaskRepository:
    """定时任务调度定义（``scheduled_task`` 表）的数据访问层。

    上层调度器通过本层读取「有哪些任务、怎么调度、是否启用」，并回写运行台账
    （上次/下次执行时间）。用户对周期与启停的修改也经由本层落库。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_key(self, task_key: str) -> ScheduledTask | None:
        """按任务标识查询调度定义；不存在返回 None。"""
        result = await self._session.execute(
            select(ScheduledTask).where(ScheduledTask.task_key == task_key)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[ScheduledTask]:
        """返回所有调度定义（含已停用），按 task_key 排序，便于管理界面展示。"""
        result = await self._session.execute(select(ScheduledTask).order_by(ScheduledTask.task_key))
        return list(result.scalars().all())

    async def list_enabled(self) -> list[ScheduledTask]:
        """返回所有已启用的调度定义，供调度器启动时构建 job。"""
        result = await self._session.execute(
            select(ScheduledTask)
            .where(ScheduledTask.enabled == True)  # noqa: E712 -- SQL 表达式需用 ==
            .order_by(ScheduledTask.task_key)
        )
        return list(result.scalars().all())

    async def create_if_absent(
        self,
        *,
        task_key: str,
        trigger_type: TriggerType,
        interval_seconds: int | None,
        cron_expr: str | None,
        enabled: bool,
    ) -> bool:
        """任务不存在时按默认值建一条调度定义，返回是否新建。

        用于「代码里新注册的任务」首次启动时自动补一行；已存在的定义原样保留，
        确保用户此前对周期/启停的调整不会被代码默认值覆盖（用户意图优先）。
        """
        existing = await self.get_by_key(task_key)
        if existing is not None:
            return False
        self._session.add(
            ScheduledTask(
                task_key=task_key,
                enabled=enabled,
                trigger_type=trigger_type,
                interval_seconds=interval_seconds,
                cron_expr=cron_expr,
            )
        )
        await self._session.commit()
        return True

    async def update_schedule(
        self,
        task_key: str,
        *,
        enabled: bool,
        trigger_type: TriggerType,
        interval_seconds: int | None,
        cron_expr: str | None,
    ) -> ScheduledTask | None:
        """用户改周期 / 启停：只改定义列，运行台账（上次 / 下次）由调度器回写。"""
        row = await self.get_by_key(task_key)
        if row is None:
            return None
        row.enabled = enabled
        row.trigger_type = trigger_type
        row.interval_seconds = interval_seconds
        row.cron_expr = cron_expr
        row.updated_at = utcnow()
        await self._session.commit()
        return row

    async def update_next_run(self, task_key: str, next_run_at: datetime | None) -> None:
        """回填下次预计触发时间（调度器建立/刷新 job 后调用）。"""
        row = await self.get_by_key(task_key)
        if row is None:
            return
        row.next_run_at = next_run_at
        row.updated_at = utcnow()
        await self._session.commit()

    async def mark_run(self, task_key: str, last_run_at: datetime) -> None:
        """记录最近一次执行时间（任务执行结束时调用）。"""
        row = await self.get_by_key(task_key)
        if row is None:
            return
        row.last_run_at = last_run_at
        row.updated_at = utcnow()
        await self._session.commit()


class TaskRunRepository:
    """定时任务执行历史（``task_run`` 表）的数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start_run(self, task_key: str, started_at: datetime) -> int:
        """写入一条 RUNNING 记录，返回其主键 id，供结束时回填结论。"""
        run = TaskRun(
            task_key=task_key,
            status=TaskRunStatus.RUNNING,
            started_at=started_at,
        )
        self._session.add(run)
        await self._session.commit()
        await self._session.refresh(run)
        assert run.id is not None  # 落库后主键必然存在
        return run.id

    async def finish_run(
        self,
        run_id: int,
        *,
        status: TaskRunStatus,
        finished_at: datetime,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        """把 RUNNING 记录收尾为最终状态（SUCCESS / FAILED）。"""
        row = await self._session.get(TaskRun, run_id)
        if row is None:
            return
        row.status = status
        row.finished_at = finished_at
        row.duration_ms = duration_ms
        row.error = error
        await self._session.commit()

    async def reap_orphan_running(self) -> int:
        """把残留在 RUNNING 的历史记录标记为 FAILED，返回处理条数。

        用途：进程若在某次任务执行中被重启，这些记录会永久卡在 RUNNING。
        调度器启动时调用一次即可自愈，与站点验证的 reset_stale_verifying 同理。
        """
        result = await self._session.execute(
            select(TaskRun).where(TaskRun.status == TaskRunStatus.RUNNING)
        )
        rows = list(result.scalars().all())
        now = utcnow()
        for row in rows:
            row.status = TaskRunStatus.FAILED
            row.finished_at = now
            row.error = "进程重启导致本次执行中断（启动自愈标记）"
        if rows:
            await self._session.commit()
        return len(rows)

    async def purge_older_than(self, cutoff: datetime) -> int:
        """删除 started_at 早于 cutoff 的历史记录，返回删除条数。"""
        result = await self._session.execute(sa_delete(TaskRun).where(TaskRun.started_at < cutoff))
        await self._session.commit()
        return result.rowcount or 0

    @staticmethod
    def cutoff_before(days: int) -> datetime:
        """计算「N 天前」的 UTC 时间点，作为清理历史的分界线。"""
        return utcnow() - timedelta(days=days)
