"""add library_duplicate_unit（重复扫描结论表）

docs/design/library-duplicate-files.md §9：重复检测从「打开页面现算」改成
「用户触发扫描、结论落库、页面只读库」。这张表存一轮扫描的结论，一个多文件
单元一行：属于哪一堆、要用户花多少心思（tier）、建议留哪个、依据、清掉能腾
多少空间。表整体由每轮扫描重建，不做增量维护。

回退兼容：纯新增表，旧代码不认识它，回退后表留在库里不被读写（旧版本仍按
请求线现算，功能不丢只是慢）；再升级回来重新扫描一次即可。无运行时依赖变更，
不 bump runtime-version。

Revision ID: e8b1d5a7c204
Revises: d2e7b3c9f481
Create Date: 2026-09-14 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b1d5a7c204"
down_revision: str | None = "d2e7b3c9f481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_duplicate_unit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("review_kind", sa.Text(), nullable=True),
        sa.Column("file_ids", sa.JSON(), nullable=False),
        sa.Column("suggested_file_id", sa.Integer(), nullable=False),
        sa.Column("suggest_reason", sa.Text(), nullable=True),
        sa.Column("extra_files", sa.Integer(), nullable=False),
        sa.Column("extra_bytes", sa.Integer(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_library_duplicate_unit_library_id", "library_duplicate_unit", ["library_id"]
    )
    op.create_index(
        "ix_library_duplicate_unit_scope", "library_duplicate_unit", ["library_id", "tier"]
    )
    op.create_index(
        "ix_library_duplicate_unit_unit",
        "library_duplicate_unit",
        ["media_item_id", "season_number", "episode_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_library_duplicate_unit_unit", table_name="library_duplicate_unit")
    op.drop_index("ix_library_duplicate_unit_scope", table_name="library_duplicate_unit")
    op.drop_index("ix_library_duplicate_unit_library_id", table_name="library_duplicate_unit")
    op.drop_table("library_duplicate_unit")
