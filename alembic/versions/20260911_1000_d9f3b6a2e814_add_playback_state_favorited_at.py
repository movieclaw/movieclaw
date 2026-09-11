"""add playback_state.favorited_at

收藏列表原来按 ``updated_at`` 倒序，号称"最近收藏的在前"。但 ``updated_at``
是这一行**任何写入**都会动的——进度上报、标记已看、记忆音轨选择——于是
"两年前收藏、昨晚看过一遍"的片会排到最前面。那不是用户理解的"最近收藏"。

新增一列只在"由非收藏变收藏"时刷新的时间（写入点唯一：
``movieclaw_playback.state.set_favorite``，网页的心与 Jellyfin 的心都走它）。

存量回填成 ``updated_at``：那是当前能拿到的最好近似，且**不会比现在更差**
——今天的排序本来就是按它算的。

向前兼容（CLAUDE.md 硬约束 3）：纯新增一列、可空，旧版本回退后不认识它，
读写都不碰（回退即回到按 updated_at 排序，也就是今天的行为）。

Revision ID: d9f3b6a2e814
Revises: c8e2a5f7b431
Create Date: 2026-09-11 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9f3b6a2e814"
down_revision: str | None = "c8e2a5f7b431"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("playback_state", sa.Column("favorited_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_playback_state_favorited_at", "playback_state", ["favorited_at"], unique=False
    )
    # 只回填真的收藏着的行：没收藏过的行留 NULL，语义才对得上
    op.execute(
        "UPDATE playback_state SET favorited_at = updated_at "
        "WHERE is_favorite = 1 AND favorited_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_playback_state_favorited_at", table_name="playback_state")
    op.drop_column("playback_state", "favorited_at")
