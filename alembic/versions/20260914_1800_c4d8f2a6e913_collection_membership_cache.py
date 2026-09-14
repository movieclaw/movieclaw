"""add collection.member_count_cache / cover_head_cache（内容型合集的成员缓存）

docs/design/library-series-collections.md 5.5.2：合集列表页每次打开都要给每个
合集各跑一次成员判定（一次完整的海报墙查询），自动生成几十上百个系列合集之后
它就是首屏最重的一条读——而成员数与卡片封面只在「库里有什么」变化时才变。
对成员只取决于库内容的合集（规则驱动、规则不含观看状态），把这两个数落在行上，
跟着 refresh_stats 一起刷。「我的收藏」这类跟着看的人变的永远是 NULL。

回退兼容：纯新增两个可空列。旧代码不认识它们，回退后不读不写（退回实时判定，
功能不丢只是慢）；再升级回来下一次 refresh_stats 会重新填上。无运行时依赖变更，
不 bump runtime-version。

Revision ID: c4d8f2a6e913
Revises: a7c3e9d1b5f2
Create Date: 2026-09-14 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d8f2a6e913"
down_revision: str | None = "a7c3e9d1b5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("collection") as batch_op:
        batch_op.add_column(sa.Column("member_count_cache", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cover_head_cache", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("collection") as batch_op:
        batch_op.drop_column("cover_head_cache")
        batch_op.drop_column("member_count_cache")
