"""下载尝试：记录首次确认完成的时刻

主源从所有可达下载器中消失时，工单要退回 wanted 让常规搜索与人工介入接手
（issue #238）。但"曾经完成过"的任务文件可能已落盘等入库，退回只会重复
下载——而 status 会在任务消失后从 completed 翻回 active，事后分辨不出这一支，
因此把"完成过"这件事持久化下来。

向前兼容说明：加可空列，旧数据一律为 NULL（视为从未完成，符合绝大多数
存量在途任务的事实）；旧代码忽略该列即可，回退不丢数据。

Revision ID: a1f6c72b9d04
Revises: d9f4b1c73e85
Create Date: 2026-08-30 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1f6c72b9d04"
down_revision: str | None = "d9f4b1c73e85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.drop_column("completed_at")
