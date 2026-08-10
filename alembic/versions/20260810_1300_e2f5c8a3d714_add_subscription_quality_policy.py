"""add subscription quality policy

订阅的首个版本锁定、洗版目标与少量运行态放在一个可空 JSON 列中。旧版本
忽略该列即可正常回退；NULL 表示完全沿用既有订阅行为。

Revision ID: e2f5c8a3d714
Revises: d1e4b8f30527
Create Date: 2026-08-10 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f5c8a3d714"
down_revision: str | None = "d1e4b8f30527"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.add_column(sa.Column("quality_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.drop_column("quality_policy")
