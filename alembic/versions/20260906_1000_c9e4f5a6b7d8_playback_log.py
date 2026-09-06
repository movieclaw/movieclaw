"""新增 playback_log：每场播放一行的播放日志

``playback_state`` 是状态不是日志（同一集重看会覆盖），标不出哪台设备、什么
时候、看了多久。本表由网页与 Jellyfin 两条播放入口共用的上报服务写入，活动页
的「播放记录」与「观看统计」从这里出（docs/design/activity.md）。

条目与成员都只存数字锚、不做外键，并快照片名与形态：统计要在条目删除、
刮削改名之后仍然成立。

向前兼容：纯新增表，旧代码回退后忽略它。

Revision ID: c9e4f5a6b7d8
Revises: b8d4e2f1a9c3
Create Date: 2026-09-06 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9e4f5a6b7d8"
down_revision = "b8d4e2f1a9c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "playback_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="movie"),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("season_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("episode_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("device_id", sa.String(), nullable=False, server_default=""),
        sa.Column("client", sa.String(), nullable=False, server_default=""),
        sa.Column("device_name", sa.String(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("start_position_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("end_position_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("watched_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_playback_log_member_id", "playback_log", ["member_id"])
    op.create_index("ix_playback_log_media_item_id", "playback_log", ["media_item_id"])
    op.create_index("ix_playback_log_device_id", "playback_log", ["device_id"])
    op.create_index("ix_playback_log_started_at", "playback_log", ["started_at"])
    op.create_index("ix_playback_log_ended_at", "playback_log", ["ended_at"])


def downgrade() -> None:
    op.drop_index("ix_playback_log_ended_at", table_name="playback_log")
    op.drop_index("ix_playback_log_started_at", table_name="playback_log")
    op.drop_index("ix_playback_log_device_id", table_name="playback_log")
    op.drop_index("ix_playback_log_media_item_id", table_name="playback_log")
    op.drop_index("ix_playback_log_member_id", table_name="playback_log")
    op.drop_table("playback_log")
