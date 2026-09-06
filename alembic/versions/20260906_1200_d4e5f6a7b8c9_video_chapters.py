"""视频章节与场景图：library_file 两列 JSON，library 一列开关

docs/design/video-chapters.md：章节是文件的属性（同条目两个版本章节可以
不同，剧集每集各有各的），所以挂 ``library_file``：

- ``chapters``：ffprobe ``-show_chapters`` 的探测事实。NULL=未探测（旧行，
  由抓图作业顺带补探），[]=探测过但没有章节；
- ``chapter_images``：抓图状态。NULL=没抓过，[]=抓过无产物。与探测事实分列，
  合成策略调档、抓图失败都不动探测事实；
- ``library.extract_chapter_images``：库级开关，默认开。

向前兼容：三列都可空或带默认值，旧代码回退后忽略它们，行为与现在一致。

Revision ID: d4e5f6a7b8c9
Revises: c9e4f5a6b7d8
Create Date: 2026-09-06 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c9e4f5a6b7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("library_file") as batch:
        batch.add_column(sa.Column("chapters", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("chapter_images", sa.JSON(), nullable=True))
    with op.batch_alter_table("library") as batch:
        batch.add_column(
            sa.Column(
                "extract_chapter_images",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("library") as batch:
        batch.drop_column("extract_chapter_images")
    with op.batch_alter_table("library_file") as batch:
        batch.drop_column("chapter_images")
        batch.drop_column("chapters")
