"""media_share 支持分享一个合集

一条链接 = 一段可看范围。范围此前只能是"一个条目"，现在也可以是"一个合集"
（docs/design/library-filtering.md F4）。

**不新开一张表**：链接、密码、有效期、访问计数、解锁 Cookie 这一整套机制两者
逐字相同，分表只会把它抄两遍。分成两列之后，两者真正的区别只剩"范围"这一处。

``media_item_id`` 与 ``library_id`` 因此改为可空（存量行两列都有值，语义不变），
新增可空的 ``collection_id``。SQLite 改列要重建表，用 batch_alter_table。

向前兼容（CLAUDE.md 硬约束 3）：旧版本回退后不认识 collection_id，会把合集
分享当成缺 media_item_id 的坏行——所以回退前请先取消合集分享。列本身放宽
约束不影响旧代码读写既有的条目分享。

Revision ID: c8e2a5f7b431
Revises: b7d4f1a9c352
Create Date: 2026-09-10 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2a5f7b431"
down_revision: str | None = "b7d4f1a9c352"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_share") as batch:
        batch.alter_column("media_item_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("library_id", existing_type=sa.Integer(), nullable=True)
        # batch 模式下加外键必须给约束名（SQLite 重建表时要按名字挂回去）
        batch.add_column(sa.Column("collection_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_media_share_collection_id",
            "collection",
            ["collection_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index("ix_media_share_collection_id", "media_share", ["collection_id"])


def downgrade() -> None:
    op.drop_index("ix_media_share_collection_id", table_name="media_share")
    with op.batch_alter_table("media_share") as batch:
        batch.drop_constraint("fk_media_share_collection_id", type_="foreignkey")
        batch.drop_column("collection_id")
        batch.alter_column("library_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("media_item_id", existing_type=sa.Integer(), nullable=False)
