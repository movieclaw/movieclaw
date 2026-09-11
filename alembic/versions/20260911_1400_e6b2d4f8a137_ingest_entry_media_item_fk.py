"""ingest_entry.media_item_id 补外键（删除作品时置空）

模型早就声明了 ``ForeignKey("media_item.id", ondelete="SET NULL")``，但加列的
d1e4b8f30527 只建了普通整数列、漏建约束。后果：作品被删（孤儿清理）后台账仍
指着旧 id；SQLite 的主键不带 AUTOINCREMENT，被删的最大 id 会复用给下一个新建
作品，于是台账静默改挂到一部毫不相干的作品上（NAS 实测：《恶人传》的监听台账
指向了一个 AV 条目）。

升级分两步：

1. 先清掉已经失效的引用——指向已不存在的作品；或指向的作品**建档晚于**这条台账
   最后一次写入（台账写入 media_item_id 时作品必然已存在，建档更晚只能是 id 被
   复用了）。media_item_id 只服务于清单汇总与诊断，置空的代价是那几行汇总不再
   归到某部作品下，远好于归错；
2. 再以 batch 模式重建表补上约束，此后删作品由数据库自动置空。

向前兼容（CLAUDE.md 硬约束 3）：只收紧约束并把本就错误的引用置空，列本身不变；
旧版本回退后读写这一列的方式完全一样。

Revision ID: e6b2d4f8a137
Revises: d9f3b6a2e814
Create Date: 2026-09-11 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6b2d4f8a137"
down_revision: str | None = "d9f3b6a2e814"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ingest_entry
        SET media_item_id = NULL
        WHERE media_item_id IS NOT NULL
          AND (
            NOT EXISTS (SELECT 1 FROM media_item WHERE media_item.id = ingest_entry.media_item_id)
            OR EXISTS (
              SELECT 1 FROM media_item
              WHERE media_item.id = ingest_entry.media_item_id
                AND media_item.created_at > ingest_entry.updated_at
            )
          )
        """
    )
    with op.batch_alter_table("ingest_entry", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_ingest_entry_media_item_id",
            "media_item",
            ["media_item_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("ingest_entry", schema=None) as batch_op:
        batch_op.drop_constraint("fk_ingest_entry_media_item_id", type_="foreignkey")
