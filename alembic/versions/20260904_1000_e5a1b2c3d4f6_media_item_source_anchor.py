"""media_item 身份锚泛化：source + external_id；library 加 source/缩略图/首页排除开关

docs/design/library-other-kind.md 第 2、3 节：媒体库要能容纳没有 TMDB 身份的
内容（「其他」库的家庭录像、影视库里 TMDB 尚未建条的 T0 剧集），而全站的
展示、播放、进度、Jellyfin 都吊在 ``media_item`` 上。把「身份来源」做成
条目的一个维度，本地内容就能成为真条目，不必另开一条文件寻址通道：

- ``media_item.source``：tmdb / local（存量回填 tmdb）；
- ``media_item.external_id``：来源内的 id，TMDB 回填为 ``tmdb_id`` 的字符串；
- ``media_item.tmdb_id`` 改可空，CHECK 保证与 source 同进退；
- 唯一键从 ``(kind, tmdb_id)`` 改为 ``(source, kind, external_id)``；
- ``library.source``（与 kind 一起定位能力档案，存量回填 tmdb）、
  ``library.generate_thumbnails``（默认开）、``library.exclude_from_home``（默认关）。

向前兼容：迁移单向（发布规范第 3 条，回退靠更新前备份）。存量 TMDB 条目的
锚与 id 全部保留；旧代码回退后读到的 ``tmdb_id`` 仍是原值。

Revision ID: e5a1b2c3d4f6
Revises: b2c7d84e5f16
Create Date: 2026-09-04 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1b2c3d4f6"
down_revision: str | None = "b2c7d84e5f16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- media_item：加列、回填、改约束（SQLite 只能 batch 重建）----------
    with op.batch_alter_table("media_item", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source", sa.String(), nullable=False, server_default="tmdb")
        )
        batch_op.add_column(sa.Column("external_id", sa.String(), nullable=True))
    # 回填：存量条目全部来自 TMDB
    op.execute("UPDATE media_item SET external_id = CAST(tmdb_id AS TEXT) WHERE external_id IS NULL")
    with op.batch_alter_table("media_item", schema=None) as batch_op:
        batch_op.alter_column("external_id", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("tmdb_id", existing_type=sa.Integer(), nullable=True)
        batch_op.drop_constraint("uq_media_item_kind_tmdb", type_="unique")
        batch_op.create_unique_constraint(
            "uq_media_item_anchor", ["source", "kind", "external_id"]
        )
        batch_op.create_check_constraint(
            "ck_media_item_tmdb_anchor", "(source = 'tmdb') = (tmdb_id IS NOT NULL)"
        )
        batch_op.create_index("ix_media_item_source", ["source"], unique=False)

    # ---- library：加三列 -----------------------------------------------------
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source", sa.String(), nullable=False, server_default="tmdb")
        )
        batch_op.create_index("ix_library_source", ["source"], unique=False)
        batch_op.add_column(
            sa.Column(
                "generate_thumbnails",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "exclude_from_home",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    # 本地来源条目在旧结构里没有位置（tmdb_id NOT NULL），降级前先删掉它们；
    # 它们的文件行外键 SET NULL 回到"未识别"，与旧版本语义一致
    op.execute("DELETE FROM media_item WHERE source <> 'tmdb'")
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.drop_column("exclude_from_home")
        batch_op.drop_column("generate_thumbnails")
        batch_op.drop_index("ix_library_source")
        batch_op.drop_column("source")
    with op.batch_alter_table("media_item", schema=None) as batch_op:
        batch_op.drop_index("ix_media_item_source")
        batch_op.drop_constraint("ck_media_item_tmdb_anchor", type_="check")
        batch_op.drop_constraint("uq_media_item_anchor", type_="unique")
        batch_op.alter_column("tmdb_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint("uq_media_item_kind_tmdb", ["kind", "tmdb_id"])
        batch_op.drop_column("external_id")
        batch_op.drop_column("source")
