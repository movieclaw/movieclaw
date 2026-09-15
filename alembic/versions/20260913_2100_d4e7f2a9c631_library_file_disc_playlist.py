"""add disc_playlist to library_file

原盘（BDMV）主播放列表的剪辑清单（docs/design/disc-playback.md §3.2）：播放
链路据此构造 ffmpeg concat 输入与关键帧索引，Jellyfin 兼容层据此判断单剪辑
（直出 m2ts）还是多剪辑（HLS remux），都不必回盘上读成百上千个 MPLS。

向前兼容：纯新增可空列、无回填。旧版本回退后不认识这一列，读写都不碰；
存量原盘行由补探填充（container=bluray 且此列为空即进入补探）。

Revision ID: d4e7f2a9c631
Revises: c9f4a1e6b573
Create Date: 2026-09-13 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e7f2a9c631"
down_revision: str | None = "c9f4a1e6b573"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file") as batch:
        batch.add_column(sa.Column("disc_playlist", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file") as batch:
        batch.drop_column("disc_playlist")
