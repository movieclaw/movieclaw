"""add collection.hidden

自动生成的合集（内置的「我的收藏」、后面的系列合集）不能真删——真删了下一次
ensure 又会把它建回来，用户会觉得"删不掉"。所以那颗「删除」按钮对它们落成
**墓碑**：``hidden = 1``，行留着，列表不再下发。

留行还有推导不出来的东西要承载：稳定 id（网页深链 ``/library/{id}/c/{cid}``
与 Jellyfin 的 BoxSet GUID 都由它派生）、用户改过的名字、封面、顺序。

隐藏是**可逆**的：接口侧 ``include_hidden`` 与既有的 ``include_empty`` 同形，
界面上给一个"显示已隐藏的合集"把它放回来。不可逆的隐藏就是单向黑洞。

向前兼容（CLAUDE.md 硬约束 3）：纯新增一列且有默认值，旧版本回退后不认识
这一列，读写都不碰。

Revision ID: f2a6d3b8c471
Revises: e1b7c4a9d038
Create Date: 2026-09-10 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a6d3b8c471"
down_revision: str | None = "e1b7c4a9d038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collection",
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_collection_hidden", "collection", ["hidden"])


def downgrade() -> None:
    op.drop_index("ix_collection_hidden", table_name="collection")
    op.drop_column("collection", "hidden")
