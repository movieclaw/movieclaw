"""add member.content_age_limit / allow_unrated

儿童档案（docs/design/library-filtering.md F5）：把分级从一个"用户自己选的筛选
维度"升级成**成员级强制约束**——海报墙、搜索、合集、Jellyfin、条目详情与起播
六处一律收窄，判定收口在 services/library/access.content_limit_for()。

``content_age_limit`` NULL = 不限（现存成员与超管都是这个值，行为零变化）。
``allow_unrated`` 默认 false：大量中文影片在 TMDB 上没有分级信息，设了年龄上限
之后未分级的片一并隐藏——"我不确定的一律不给看"才是家长要的默认值。

向前兼容（CLAUDE.md 硬约束 3）：纯新增两列且有默认值，旧版本回退后不认识
它们，读写都不碰（回退即等于取消约束，不会把库锁死）。

Revision ID: b7d4f1a9c352
Revises: a3c8e5d2f691
Create Date: 2026-09-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d4f1a9c352"
down_revision: str | None = "a3c8e5d2f691"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("member", sa.Column("content_age_limit", sa.Integer(), nullable=True))
    op.add_column(
        "member",
        sa.Column("allow_unrated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("member", "allow_unrated")
    op.drop_column("member", "content_age_limit")
