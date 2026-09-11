"""add media_disproven_source

"这个发布被实测证明满足不了这个单元"此前只写在
``subscription_download_attempt.content_missing`` 里，而那张表的
``subscription_id`` 是 ON DELETE CASCADE——订阅一删，负面记忆全没。真实教训：
用户抓到一条 113 MB 的假「正片」，手动删掉文件、删掉订阅重建，系统把同一条
种子原样又抓了一遍。证据产生过，只是挂在了一个比它短命的东西上。

记忆的正确归属是条目：「这个发布里没有这部电影」是关于内容的事实，与用户订
没订无关。挂到 media_item 上，删订阅重建、换规则组、洗版重订都带得走。

不做数据迁移：选种阶段**两边都读**，存量安装的旧记忆继续从 content_missing
生效，新证据同时落到这张表。

向前兼容（CLAUDE.md 硬约束 3）：纯新增一张表，旧版本回退后不认识它，读写都
不碰（回退即回到今天的行为——负面记忆随订阅生灭）。

Revision ID: f7c3a9e2d481
Revises: e4a7c2b9d165
Create Date: 2026-09-11 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c3a9e2d481"
down_revision: str | None = "e4a7c2b9d165"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_disproven_source",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("torrent_id", sa.String(), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_item_id",
            "site_id",
            "torrent_id",
            "season_number",
            "episode_number",
            name="uq_media_disproven_source_unit",
        ),
    )
    op.create_index(
        "ix_media_disproven_source_media_item_id",
        "media_disproven_source",
        ["media_item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_disproven_source_media_item_id", table_name="media_disproven_source"
    )
    op.drop_table("media_disproven_source")
