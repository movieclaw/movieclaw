"""任务忽略标记与字幕自动生成静音台账

issue #221：失败任务只有重试/看日志/交给 Agent 三条路，用户"不想再处理"时
没有出口，于是永远挂在「需要处理」里，侧栏红角标永不熄灭。

本迁移落两件事：

1. ``job.dismissed_at`` / ``job.dismissed_by``——用户忽略。刻意不做成新的
   JobStatus：状态是执行语义（终态集合、重试链、日志溯源都建立在它上面），
   忽略是用户态度，两者正交，多一个终态会让每一处状态判定都要跟着改。
2. ``subtitle_auto_mute``——「不再自动生成」台账。入库后自动生成字幕的
   "本进程只自动试一次"是内存集合，重启即清零；不落库的话用户忽略掉的
   失败任务会在下次扫描时原地复活。

向前兼容说明：两列均可空、新表独立，旧代码忽略即可，回退不丢既有数据
（回退后已忽略的任务会重新出现在「需要处理」中，这是可接受的降级）。

Revision ID: b7d2e9a45c31
Revises: 1cde1f667494
Create Date: 2026-08-27 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d2e9a45c31"
down_revision: str | None = "1cde1f667494"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dismissed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("dismissed_by", sa.String(), nullable=True))
        batch_op.create_index("ix_job_dismissed_at", ["dismissed_at"], unique=False)

    op.create_table(
        "subtitle_auto_mute",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("library_file_id", sa.Integer(), nullable=False),
        sa.Column("target_language", sa.String(), nullable=False),
        sa.Column("muted_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["library_file_id"], ["library_file.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_file_id", "target_language", name="uq_subtitle_auto_mute_file_language"
        ),
    )
    op.create_index(
        "ix_subtitle_auto_mute_library_file_id",
        "subtitle_auto_mute",
        ["library_file_id"],
        unique=False,
    )
    op.create_index(
        "ix_subtitle_auto_mute_target_language",
        "subtitle_auto_mute",
        ["target_language"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subtitle_auto_mute_target_language", table_name="subtitle_auto_mute")
    op.drop_index("ix_subtitle_auto_mute_library_file_id", table_name="subtitle_auto_mute")
    op.drop_table("subtitle_auto_mute")
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_index("ix_job_dismissed_at")
        batch_op.drop_column("dismissed_by")
        batch_op.drop_column("dismissed_at")
