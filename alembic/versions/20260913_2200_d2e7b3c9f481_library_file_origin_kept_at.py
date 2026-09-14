"""add library_file origin / kept_at（来源快照与「都留着」标记）

docs/design/library-duplicate-files.md §2 / §3.4：

- ``origin``：文件是怎么进库的（订阅投递 / 手动下载 / 监听识别 / 存量扫描），
  入库或扫描时一次成型的展示用 JSON 快照 {kind, label, detail}，不外键。
  NULL = 本次升级前的旧行，展示时按既有的 source / site_id / torrent_id 读时推导，
  不做数据回填；
- ``kept_at``：用户在「重复文件」页对某个单元点「都留着」的时间，单元内每个文件
  各盖一个；NULL = 未标记。

回退兼容：两列都可空、无默认值要求，旧代码不认识它们，插入落 NULL、读取忽略；
回退期间新入库的行没有来源快照，升级回来后按旧行同样读时推导——不丢数据只丢
一段文案。无运行时依赖变更，不 bump runtime-version。

Revision ID: d2e7b3c9f481
Revises: d4e7f2a9c631
Create Date: 2026-09-13 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2e7b3c9f481"
down_revision: str | None = "d4e7f2a9c631"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("origin", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("kept_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("kept_at")
        batch_op.drop_column("origin")
