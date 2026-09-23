"""add ingest_entry.unresolved_files / claimed_files / collection_item_ids（电影合集）

issue #438：一个监听条目目录里装着多部电影（合集）时，监听导入按识别结果分组、
逐部入库。台账仍是一次下载一行，合集里识别不出的文件、人工逐文件认领的结论、
已入库的各部作品记在这三列上（见 models/ingest_entry.py）。

回退兼容：纯新增三个可空 JSON 列。旧代码不认识它们，回退后不读不写——合集条目
退回旧行为（只入库最大的一部），已入库的文件不受影响；再升级回来下一轮处理
会重新填上。无运行时依赖变更，不 bump runtime-version。

Revision ID: f3a9c7e1d428
Revises: c4d8f2a6e913
Create Date: 2026-09-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a9c7e1d428"
down_revision: str | None = "c4d8f2a6e913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ingest_entry") as batch_op:
        batch_op.add_column(sa.Column("unresolved_files", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("claimed_files", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("collection_item_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ingest_entry") as batch_op:
        batch_op.drop_column("collection_item_ids")
        batch_op.drop_column("claimed_files")
        batch_op.drop_column("unresolved_files")
