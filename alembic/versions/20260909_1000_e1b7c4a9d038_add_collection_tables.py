"""add collection / collection_item

合集：「一组 media_item」的持久化定义（docs/design/library-collections.md）。
规则与 library.match_rules 同构，所以"筛完存为合集"是纯粹的形状转换。

没有 mode 列——形态由 rules 是否为空、collection_item 是否有行、builtin 是否为
NULL 三个独立事实推导。builtin 是"这个抽象要吃掉既有特例"的落点（「我的收藏」
登记为 builtin='favorites' 后自动出现在合集列表与 Jellyfin BoxSet 里）。

向前兼容（CLAUDE.md 硬约束 3）：纯新增两张表，不动任何既有表。旧版本回退后
不认识这两张表，读写都不碰；应用内更新的一键回退安全。

Revision ID: e1b7c4a9d038
Revises: d5a8c3f61b24
Create Date: 2026-09-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1b7c4a9d038"
down_revision: str | None = "d5a8c3f61b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        # 库删除时级联删除：库没了，"这个库里的一批片"这个定义也就没意义
        sa.Column(
            "library_id",
            sa.Integer(),
            sa.ForeignKey("library.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("sort", sa.String(), nullable=False, server_default="title"),
        sa.Column("visibility", sa.String(), nullable=False, server_default="household"),
        # 成员级数据（MemberScopedMixin）：private 时是归属成员，household 时为 0。
        # 登记在 member_scoped 注册表里，删成员时随之清理
        sa.Column("member_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("builtin", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cover_item_id",
            sa.Integer(),
            sa.ForeignKey("media_item.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("builtin", name="uq_collection_builtin"),
    )
    op.create_index("ix_collection_library_id", "collection", ["library_id"])
    op.create_index("ix_collection_member_id", "collection", ["member_id"])
    op.create_index("ix_collection_visibility", "collection", ["visibility"])

    op.create_table(
        "collection_item",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("collection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("collection_id", "media_item_id", name="uq_collection_item"),
    )
    op.create_index("ix_collection_item_collection_id", "collection_item", ["collection_id"])
    op.create_index("ix_collection_item_media_item_id", "collection_item", ["media_item_id"])


def downgrade() -> None:
    op.drop_table("collection_item")
    op.drop_table("collection")
