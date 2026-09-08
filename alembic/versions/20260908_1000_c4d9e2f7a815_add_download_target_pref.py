"""add download_target_pref table

搜索结果「下载」的保存位置记忆（docs/design/download-target-memory.md）。
每人每种子分类一行，桶键是 TorrentCategory。

向前兼容：纯新增表、无回填。旧版本回退后忽略该表，行为退回「每次弹窗」，
不会异常——这正是不去改 app_setting 唯一约束的原因（那个改动会让旧代码读
配置时抛 MultipleResultsFound）。

Revision ID: c4d9e2f7a815
Revises: f3a4b5c6d7e8
Create Date: 2026-09-08 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9e2f7a815"
down_revision: str | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "download_target_pref",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("save_path", sa.Text(), nullable=True),
        sa.Column("downloader_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", "category", name="uq_download_target_pref"),
    )
    with op.batch_alter_table("download_target_pref", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_download_target_pref_member_id"), ["member_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("download_target_pref", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_download_target_pref_member_id"))
    op.drop_table("download_target_pref")
