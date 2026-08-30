"""add media_item.scrape_library_id

条目的**刮削归属库**（docs/design/scrape-customization.md §14 / P4）：
元数据与图片的产物挂全局条目（一部片一份档案、图片按条目 id 存一份），
所以"按库配不同的语言/选图"必须先回答一个问题——**这条条目按哪套配置刮**。
本列就是那个答案：归属库定了，刮削与后台刷新读同一列，口味不会被洗回全局。

列语义：NULL = 未定（读取时按"在位文件所属库 → 订阅目标库 → 该类型默认库"
惰性推断并回填固化；都没有则跟全局设置）。库删除时置 NULL，下次读取重新推断。

向前兼容：纯加列且可空，旧代码回退后忽略该列，不丢数据；存量条目取 NULL，
靠惰性推断自愈，**不需要数据迁移脚本**，升级后未改任何库设置的部署行为不变。

Revision ID: d9f4b1c73e85
Revises: c8e3fa5b7d12
Create Date: 2026-08-30 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9f4b1c73e85"
down_revision: str | None = "c8e3fa5b7d12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch 模式：SQLite 不支持 ALTER 加带外键的列，batch 会重建表带上约束
    with op.batch_alter_table("media_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scrape_library_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_media_item_scrape_library_id", ["scrape_library_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_media_item_scrape_library",
            "library",
            ["scrape_library_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("media_item", schema=None) as batch_op:
        batch_op.drop_constraint("fk_media_item_scrape_library", type_="foreignkey")
        batch_op.drop_index("ix_media_item_scrape_library_id")
        batch_op.drop_column("scrape_library_id")
