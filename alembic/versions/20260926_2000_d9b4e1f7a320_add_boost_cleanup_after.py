"""刷流台账：用户清理后最早可删除的时刻

- ratio_boost_task.cleanup_after：用户在活动页 / 关闭刷流时请求清理残留刷流种子，
  但任务还在保留期内（提前删可能被记 H&R）——记下保留期到期时刻，由刷流引擎到点
  连数据自动删除；下载器暂时不可达没删成的记请求时刻，下一轮重试。

向前兼容说明：nullable、无默认值。旧代码忽略该列即可（回退后已请求清理的任务
只是不再自动删除，照常做种，不丢数据）。

Revision ID: d9b4e1f7a320
Revises: b6e2d8f4a193
Create Date: 2026-09-26 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9b4e1f7a320"
down_revision: str | None = "b6e2d8f4a193"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cleanup_after", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.drop_column("cleanup_after")
