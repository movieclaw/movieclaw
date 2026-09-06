"""media_metadata 加 poster_blur：主图的微缩占位图（渐进式加载）

图片库的相册墙一页 60 张、缩略图各几十 KB，网络慢时格子先空着再逐张蹦出来。
这里给每张主图记一个 16px 宽的 JPEG data URI（约 300 字节），随列表接口一起下发，
瓦片在缩略图到达前先铺一层模糊的色块（docs/design/library-photo-kind.md 3.4）。
本地来源条目生成缩略图时顺手算；TMDB 海报不记（卡片固定 2:3、图床有 CDN）。

向前兼容：纯新增可空列，旧代码回退后忽略它。

Revision ID: d1e2f3a4b5c6
Revises: d4e5f6a7b8c9
Create Date: 2026-09-06 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_metadata", sa.Column("poster_blur", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("media_metadata") as batch_op:
        batch_op.drop_column("poster_blur")
