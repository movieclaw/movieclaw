"""add media_metadata.series_key / series_name 与 collection 的系列档案快照

系列合集（docs/design/library-series-collections.md）：TMDB 的
``belongs_to_collection`` 与 NFO 的 ``<set>`` 落到 ``media_metadata`` 的两列上，
合集本身仍然是**规则驱动的合集**（规则就是「series_key = X」），不新开实体——
"这个抽象要吃掉既有特例"的第二次兑现（第一次是「我的收藏」）。

``series_key`` 是唯一的判定依据，写入时定死优先级：有 TMDB id 就是
``tmdb:{id}``，否则才是 ``name:{规范化名}``。两列并存会让同一部片落进两个
几乎一样的合集，一个规范化 key 从构造上杜绝这件事。

``collection.series_parts`` 是 TMDB 系列档案的**快照**（整个系列共几部、每部的
id/标题/上映日/海报），用来算「缺哪几部」并一键订阅；懒加载，脏了重拉即可。
``series_image`` 是同一次响应里白拿的系列官方海报。

向前兼容（CLAUDE.md 硬约束 3）：纯新增四列且全部可空，旧版本回退后不认识
这些列，读写都不碰。

Revision ID: a3c8e5d2f691
Revises: f2a6d3b8c471
Create Date: 2026-09-10 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c8e5d2f691"
down_revision: str | None = "f2a6d3b8c471"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_metadata", sa.Column("series_key", sa.String(), nullable=True))
    op.add_column("media_metadata", sa.Column("series_name", sa.String(), nullable=True))
    # 成员判定天天走这一列（规则驱动的合集每次求值都是一条 series_key = ?）
    op.create_index("ix_media_metadata_series_key", "media_metadata", ["series_key"])

    op.add_column("collection", sa.Column("series_parts", sa.JSON(), nullable=True))
    op.add_column("collection", sa.Column("series_image", sa.String(), nullable=True))

    # 展示开关（默认开）：关掉只是不自动建合集行，series_key 与 NFO 的 <set>
    # 照常写——可逆的展示偏好可以关，不可逆的数据缺失不能
    op.add_column(
        "library",
        sa.Column(
            "auto_series_collections", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column("library", "auto_series_collections")
    op.drop_column("collection", "series_image")
    op.drop_column("collection", "series_parts")
    op.drop_index("ix_media_metadata_series_key", table_name="media_metadata")
    op.drop_column("media_metadata", "series_name")
    op.drop_column("media_metadata", "series_key")
