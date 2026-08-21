"""给 job.created_at 建索引：作业列表一直在按它倒序取前 N

Revision ID: d3f6a9c2e458
Revises: b9c2d5e8f014
Create Date: 2026-08-21 10:00:00.000000

``jobs.list_jobs`` 与新增的 ``jobs.list_jobs_by_resource`` 都以
``ORDER BY job.created_at DESC LIMIT n`` 取「最近几条」，而 job 表上一直
没有 created_at 索引——每次都要把候选集全排一遍。作业台账保留 90 天、
随使用持续增长，媒体库列表又每 30 秒轮询一次，这个排序会一直变贵。

纯加索引，不动任何数据与列定义，向前兼容（回退到旧版本应用只是少了这条
索引，查询照常工作）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f6a9c2e458"
down_revision: str | None = "b9c2d5e8f014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_job_created_at", "job", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_job_created_at", table_name="job")
