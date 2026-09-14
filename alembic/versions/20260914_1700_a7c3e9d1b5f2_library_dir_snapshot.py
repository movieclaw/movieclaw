"""add library_dir_snapshot（目录 mtime 快照）与 library.dir_snapshot_full_at

定期对账原来每轮把每个根路径下的每个目录都 readdir 一遍——网络挂载上万级
媒体库一轮一两分钟，而绝大多数目录根本没变。现在把上一轮完整遍历时每个目录
的 mtime 落这张表，对账只重列 mtime 变过的目录；``library.dir_snapshot_full_at``
记最近一次全量遍历的时间，过期（一周）强制全量一轮作保险。

回退兼容：纯新增表 + 一个可空列。旧代码不认识它们，回退后表与列留在库里
不被读写（旧版本对账退回全量遍历，功能不丢只是慢）；再升级回来第一轮对账
会因快照过期自动重建。无运行时依赖变更，不 bump runtime-version。

Revision ID: a7c3e9d1b5f2
Revises: e8b1d5a7c204
Create Date: 2026-09-14 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9d1b5f2"
down_revision: str | None = "e8b1d5a7c204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_dir_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("leaf", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_library_dir_snapshot_library_id", "library_dir_snapshot", ["library_id"])
    op.create_index(
        "ix_library_dir_snapshot_library_path",
        "library_dir_snapshot",
        ["library_id", "path"],
        unique=True,
    )
    with op.batch_alter_table("library") as batch_op:
        batch_op.add_column(sa.Column("dir_snapshot_full_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library") as batch_op:
        batch_op.drop_column("dir_snapshot_full_at")
    op.drop_index("ix_library_dir_snapshot_library_path", table_name="library_dir_snapshot")
    op.drop_index("ix_library_dir_snapshot_library_id", table_name="library_dir_snapshot")
    op.drop_table("library_dir_snapshot")
