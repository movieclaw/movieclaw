from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, ForeignKey, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel

from movieclaw_db.models.base import TimestampMixin, utcnow


class JobStatus(StrEnum):
    """用户可见后台任务的持久化状态。

    状态值是 API、CLI、前端和执行器共同依赖的稳定协议，不能拿临时协程状态
    代替。``retry_wait`` 表示系统会自动重试，``blocked`` 表示必须由用户修复
    配置或补充输入；二者都不是普通失败。

    「用户忽略」刻意**不做成状态**：忽略只表达"我不打算处理了"，不改写
    "这件事失败过"这个事实。日志溯源、重试链（``retry_of_job_id``）与终态
    集合都建立在 status 上，多一个终态会让每一处判定都要跟着改。忽略因此
    记在 ``dismissed_at`` 这一维上，与 status 正交。
    """

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    CANCELLING = "cancelling"
    WAITING = "waiting"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.RETRY_WAIT,
    JobStatus.CANCELLING,
    JobStatus.WAITING,
    JobStatus.BLOCKED,
}
TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


class Job(TimestampMixin, table=True):
    """可恢复后台任务的唯一事实来源。

    表中只保存可序列化的任务定义、状态与小型进度/结果；字幕断点等大文件继续
    放在 ``data/jobs/<job_id>/`` 或领域缓存目录。处理器由 ``job_type`` 在代码
    注册表中解析，数据库绝不保存函数引用，避免升级重构后反序列化旧代码。
    """

    __tablename__ = "job"

    id: str = Field(primary_key=True, description="稳定任务 ID（job_<uuid>）")
    job_type: str = Field(index=True, description="任务类型，对应代码注册的处理器")
    subject: str | None = Field(default=None, description="用户可读的任务对象名称")
    definition_version: int = Field(default=1, description="任务输入定义版本")
    handler_revision: str = Field(default="1", description="执行逻辑修订号")
    prompt_revision: str | None = Field(default=None, description="AI 提示词修订号")
    provider_ref: str | None = Field(default=None, description="供应商/模型中性引用，不含密钥")
    # SQLAlchemy Enum 默认把成员名（QUEUED）写进数据库，但 API 与部分唯一索引
    # 使用稳定协议值（queued）。显式按 value 持久化，避免跨进程并发去重索引
    # 因大小写不一致而形同虚设。
    status: JobStatus = Field(
        default=JobStatus.QUEUED,
        sa_column=Column(
            SAEnum(
                JobStatus,
                values_callable=lambda members: [member.value for member in members],
                native_enum=False,
            ),
            nullable=False,
            index=True,
        ),
    )
    input_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="已确认的任务输入（不含密钥）",
    )
    progress: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="统一进度外壳",
    )
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    error: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    usage: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="资源消耗台账（token/OCR 图片/请求数等）",
    )

    # 语义去重与冲突策略分开：前者说明“是不是同一件事”，后者说明遇到同一件
    # 正在做时该复用、拒绝还是排队。当前首批任务使用 return_existing。
    dedupe_key: str | None = Field(default=None, index=True)
    conflict_policy: str = Field(default="return_existing")
    priority: int = Field(default=0, index=True)
    available_at: datetime = Field(default_factory=utcnow, index=True)
    attempt: int = Field(default=0)
    max_attempts: int = Field(default=3)

    actor_kind: str | None = Field(default=None)
    actor_name: str | None = Field(default=None)
    actor_id: str | None = Field(default=None)
    origin: str = Field(default="system", index=True)
    correlation_id: str | None = Field(default=None, index=True)

    parent_job_id: str | None = Field(default=None, foreign_key="job.id", index=True)
    root_job_id: str | None = Field(default=None, foreign_key="job.id", index=True)
    triggered_by_job_id: str | None = Field(default=None, foreign_key="job.id", index=True)
    retry_of_job_id: str | None = Field(default=None, foreign_key="job.id", index=True)

    lease_owner: str | None = Field(default=None, index=True)
    lease_expires_at: datetime | None = Field(default=None, index=True)
    heartbeat_at: datetime | None = Field(default=None)
    cancel_requested_at: datetime | None = Field(default=None)
    cancel_requested_by: str | None = Field(default=None)
    # 用户忽略：失败任务的"我不再处理了"。与 status 正交（见类文档），只影响
    # 任务中心把它算进「需要处理」还是「已结束」，不影响任何执行语义。
    dismissed_at: datetime | None = Field(default=None, index=True)
    dismissed_by: str | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None, index=True)
    retention_until: datetime | None = Field(default=None, index=True)
    revision: int = Field(default=1, nullable=False)


class JobResource(SQLModel, table=True):
    """任务与产品资源的多对多索引，供媒体墙/详情页一次查询聚合状态。"""

    __tablename__ = "job_resource"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "resource_type", "resource_id", "relation", name="uq_job_resource"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(
        sa_column=Column(
            Text,
            ForeignKey("job.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    resource_type: str = Field(index=True)
    resource_id: str = Field(index=True)
    relation: str = Field(default="target")


class JobEvent(SQLModel, table=True):
    """任务状态时间线；既用于排错，也作为 SSE 的可续传游标。"""

    __tablename__ = "job_event"
    __table_args__ = (UniqueConstraint("job_id", "revision", name="uq_job_event_revision"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(
        sa_column=Column(
            Text,
            ForeignKey("job.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    revision: int = Field(index=True)
    event_type: str = Field(index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow, index=True)


class JobLock(SQLModel, table=True):
    """执行期资源锁；与语义去重分离，任务等待时不提前占住真实资源。"""

    __tablename__ = "job_lock"

    lock_key: str = Field(primary_key=True)
    job_id: str = Field(
        sa_column=Column(
            Text,
            ForeignKey("job.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    lease_expires_at: datetime = Field(index=True)
