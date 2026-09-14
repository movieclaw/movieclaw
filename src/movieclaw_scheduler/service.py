from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from movieclaw_db.engine import get_database
from movieclaw_db.models.scheduled_task import ScheduledTask, TriggerType
from movieclaw_db.repositories.scheduled_task_repo import (
    ScheduledTaskRepository,
    TaskRunRepository,
)
from movieclaw_scheduler.config import SchedulerConfig, set_scheduler_config
from movieclaw_scheduler.registry import TaskDefinition, get_task, iter_tasks
from movieclaw_scheduler.runner import run_task

logger = logging.getLogger("movieclaw_scheduler.service")


def _to_naive_utc(moment: datetime | None) -> datetime | None:
    """把 APScheduler 给出的带时区时间转成 UTC 朴素时间，符合本项目落库约定。"""
    if moment is None:
        return None
    return moment.astimezone(UTC).replace(tzinfo=None)


class SchedulerService:
    """调度器服务：把「数据库里的调度定义」翻译成「APScheduler 的内存 job」。

    生命周期（由 lifespan 驱动，见 ``start`` / ``shutdown``）：
      1. 确保内置任务已注册；
      2. 为代码里新注册、库中还没有的任务播种默认调度记录；
      3. 自愈上次遗留的 RUNNING 历史；
      4. 启动 APScheduler，并按数据库中「已启用」的定义逐个建立 job。

    只用内存 jobstore：调度定义的事实来源是数据库表，进程重启后从表重建即可，
    因此无需 APScheduler 自带的持久化（也就避开了它 pickle 代码的种种问题）。
    """

    def __init__(self, config: SchedulerConfig) -> None:
        self._config = config
        self._tz = ZoneInfo(config.timezone)
        # job_defaults 是「单机小规模周期任务」的稳妥默认：
        # - coalesce：错过多次触发时只补跑一次，避免积压后瞬间连发
        # - max_instances=1：同一任务不并发叠跑（一个慢抓取不会自己压自己）
        # - misfire_grace_time：停机后错过的触发，1 小时内仍允许补跑
        self._scheduler = AsyncIOScheduler(
            timezone=self._tz,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 3600,
            },
        )

    def _build_trigger(self, row: ScheduledTask) -> BaseTrigger:
        """把一条调度定义翻译成 APScheduler 触发器。"""
        if row.trigger_type == TriggerType.INTERVAL:
            if not row.interval_seconds or row.interval_seconds <= 0:
                raise ValueError(f"任务 {row.task_key} 的间隔秒数无效：{row.interval_seconds}")
            # 重启不清零节奏：以「上次实际执行 + 间隔」为序列起点，而非「启动
            # 时刻 + 间隔」。否则进程频繁重启（如开发环境热重载）会把下次触发
            # 无限顺延，任务被饿死；续排后等待上限恒为一个间隔。
            start_date = None
            if row.last_run_at is not None:
                start_date = row.last_run_at.replace(tzinfo=UTC) + timedelta(
                    seconds=row.interval_seconds
                )
            return IntervalTrigger(
                seconds=row.interval_seconds, timezone=self._tz, start_date=start_date
            )
        if row.trigger_type == TriggerType.CRON:
            if not row.cron_expr:
                raise ValueError(f"任务 {row.task_key} 缺少 cron 表达式")
            return CronTrigger.from_crontab(row.cron_expr, timezone=self._tz)
        raise ValueError(f"任务 {row.task_key} 的触发类型未知：{row.trigger_type}")

    async def _sync_registry_to_db(self) -> None:
        """为代码里已注册、但数据库还没有记录的任务播种默认调度定义。

        已存在的定义原样保留——用户此前对周期/启停的调整优先于代码默认值。
        """
        async with get_database().session() as session:
            repo = ScheduledTaskRepository(session)
            for defn in iter_tasks():
                created = await repo.create_if_absent(
                    task_key=defn.key,
                    trigger_type=defn.default_trigger_type,
                    interval_seconds=defn.default_interval_seconds,
                    cron_expr=defn.default_cron,
                    enabled=defn.default_enabled,
                )
                if created:
                    logger.info("已为新任务播种默认调度：%s（%s）", defn.key, defn.title)

    async def _reap_orphan_runs(self) -> None:
        """启动自愈：把上次遗留在 RUNNING 的历史标记为 FAILED。"""
        async with get_database().session() as session:
            count = await TaskRunRepository(session).reap_orphan_running()
            if count:
                logger.info("已将 %d 条中断的任务执行历史标记为失败", count)

    async def _load_jobs(self) -> None:
        """按数据库中「已启用」的定义建立 APScheduler job，并回写下次触发时间。"""
        async with get_database().session() as session:
            repo = ScheduledTaskRepository(session)
            rows = await repo.list_enabled()
            loaded = 0
            for row in rows:
                defn: TaskDefinition | None = get_task(row.task_key)
                if defn is None:
                    # 库里有定义但代码里没有对应处理器（如删了任务代码）——跳过并告警，
                    # 不删数据，避免误伤用户历史配置。
                    logger.warning("调度定义 %s 在代码中无对应处理器，已跳过", row.task_key)
                    continue
                try:
                    trigger = self._build_trigger(row)
                except ValueError as exc:
                    logger.error("跳过配置有误的任务 %s：%s", row.task_key, exc)
                    continue
                job = self._scheduler.add_job(
                    run_task,
                    trigger=trigger,
                    args=[defn],
                    id=row.task_key,
                    name=defn.title,
                    replace_existing=True,
                )
                await repo.update_next_run(row.task_key, _to_naive_utc(job.next_run_time))
                loaded += 1
                logger.info(
                    "已加载定时任务：%s，下次触发：%s",
                    row.task_key,
                    job.next_run_time,
                )
            logger.info("定时任务加载完成，共 %d 个", loaded)

    async def reschedule(self, task_key: str) -> datetime | None:
        """用户改了周期 / 启停后按库里的最新定义重排这一个任务，返回下次触发时间。

        停用 → 摘掉 job；启用 → 按新触发器替换（``replace_existing``）。不重启进程、
        不影响其它任务；正在执行中的那一轮照常跑完（runner 自己管互斥）。
        """
        async with get_database().session() as session:
            repo = ScheduledTaskRepository(session)
            row = await repo.get_by_key(task_key)
            defn = get_task(task_key)
            if row is None or defn is None:
                return None
            if not row.enabled or not self._scheduler.running:
                if self._scheduler.get_job(task_key) is not None:
                    self._scheduler.remove_job(task_key)
                await repo.update_next_run(task_key, None)
                return None
            job = self._scheduler.add_job(
                run_task,
                trigger=self._build_trigger(row),
                args=[defn],
                id=task_key,
                name=defn.title,
                replace_existing=True,
            )
            next_run = _to_naive_utc(job.next_run_time)
            await repo.update_next_run(task_key, next_run)
            logger.info("定时任务已重排：%s，下次触发：%s", task_key, job.next_run_time)
            return next_run

    async def start(self) -> None:
        """启动调度器。应在应用启动（lifespan）时调用一次。

        注意：调用前，各领域包的任务模块需已被 import（从而完成 @register_task 注册）。
        内置任务由本方法负责导入；领域业务任务应由 lifespan 在此之前导入。
        """
        # 确保内置系统任务（如历史清理）已注册；领域任务由调用方在 start 前导入
        import movieclaw_scheduler.tasks  # noqa: F401

        await self._sync_registry_to_db()
        await self._reap_orphan_runs()
        self._scheduler.start()
        await self._load_jobs()
        logger.info("调度器已启动（时区：%s）", self._config.timezone)

    async def shutdown(self) -> None:
        """关闭调度器。应用关闭时调用；wait=False 不阻塞进程退出。"""
        self._scheduler.shutdown(wait=False)
        logger.info("调度器已关闭")


# ---------------------------------------------------------------------------
# 模块级单例 + 初始化入口（与 movieclaw_db 的 init_db/get_database 风格一致）
# ---------------------------------------------------------------------------
_scheduler_service: SchedulerService | None = None


def init_scheduler(config: SchedulerConfig) -> SchedulerService:
    """初始化全局调度器单例。应在应用启动（lifespan）时调用一次。"""
    global _scheduler_service
    if _scheduler_service is not None:
        logger.warning("调度器已初始化，重复调用 init_scheduler 被忽略")
        return _scheduler_service
    set_scheduler_config(config)
    _scheduler_service = SchedulerService(config)
    return _scheduler_service


def try_get_scheduler() -> SchedulerService | None:
    """已初始化就给单例，没有就 None——设置接口在调度器关着时也要能改库里的定义。"""
    return _scheduler_service


def get_scheduler() -> SchedulerService:
    """获取全局调度器单例；未初始化时抛错，提示检查启动流程。"""
    if _scheduler_service is None:
        raise RuntimeError("调度器尚未初始化，请确认应用启动时已调用 init_scheduler()")
    return _scheduler_service
