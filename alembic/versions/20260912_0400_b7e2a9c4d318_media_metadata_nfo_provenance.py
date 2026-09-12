"""media_metadata 增加 NFO 吸收台账两列

配套「NFO 改为入库时吸收、读路径只读库」的改动（docs/design/metadata.md 第 5 节）。
在此之前，条目目录的 movie.nfo / tvshow.nfo 是**读时**才解析的：详情页每打开
一次就回媒体盘读一次，分集区每打开一次把一季每集都读一遍。改成刮削时读进
``media_metadata`` 的展示列（非空字段压过 TMDB），之后详情页只读库。

- ``nfo_name``：吸收来源的文件名，详情页仍要据此标注「信息来自 xxx.nfo」；
- ``nfo_fingerprint``：已吸收 NFO 的 ``"mtime_ns:大小"``。它同时是**存量回填
  的判据**（NULL = 从未吸收过）与「NFO 变没变」的判据。找不到 NFO 时写空串，
  区分「查过、确实没有」与「还没查过」。

向前兼容（CLAUDE.md 硬约束 3）：纯新增两列、均可空。旧版本回退后不认识它们，
读写都不碰——回退即回到「读时解析 NFO」的老行为，展示结果不变。

Revision ID: b7e2a9c4d318
Revises: a1c6d3f8b420
Create Date: 2026-09-12 04:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2a9c4d318"
down_revision: str | None = "a1c6d3f8b420"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_metadata", sa.Column("nfo_name", sa.String(), nullable=True))
    op.add_column("media_metadata", sa.Column("nfo_fingerprint", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_metadata", "nfo_fingerprint")
    op.drop_column("media_metadata", "nfo_name")
