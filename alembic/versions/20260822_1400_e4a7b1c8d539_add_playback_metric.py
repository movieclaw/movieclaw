"""add playback_metric

网页播放的质量快照（docs/design/web-player.md §8）。没有它就答不出这个
播放器最重要的那个问题——**直通率**（档 0 + 档 1 占全部播放的比例）。

向前兼容说明：纯加表，不动任何既有表；旧代码回退后忽略本表，不丢数据。
遥测只落本地，绝不外发（硬边界 3）。

Revision ID: e4a7b1c8d539
Revises: e4a7b0d3f569
Create Date: 2026-08-22 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7b1c8d539"
down_revision: str | None = "e4a7b0d3f569"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playback_metric",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("library_file_id", sa.Integer(), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("degraded_from", sa.Integer(), nullable=True),
        sa.Column("engine", sa.String(), nullable=False, server_default=""),
        sa.Column("hw_backend", sa.String(), nullable=False, server_default=""),
        sa.Column("ttff_ms", sa.Integer(), nullable=True),
        sa.Column("rebuffer_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rebuffer_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seek_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dropped_frames", sa.Integer(), nullable=True),
        sa.Column("total_frames", sa.Integer(), nullable=True),
        sa.Column("watched_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_playback_metric_member_id", "playback_metric", ["member_id"], unique=False
    )
    op.create_index(
        "ix_playback_metric_library_file_id",
        "playback_metric", ["library_file_id"], unique=False,
    )
    op.create_index("ix_playback_metric_tier", "playback_metric", ["tier"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_playback_metric_tier", table_name="playback_metric")
    op.drop_index("ix_playback_metric_library_file_id", table_name="playback_metric")
    op.drop_index("ix_playback_metric_member_id", table_name="playback_metric")
    op.drop_table("playback_metric")
