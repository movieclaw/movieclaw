"""add rule_set.match_rules（规则组适用范围）

规则组自述「适用于什么作品」（docs/design/rule-set-scope.md）：条件结构与
``library.match_rules`` 同构，额外支持 ``kind``（电影/剧集）字段。新订阅未
显式指定规则组时按适用范围自动选组，都不命中落默认规则组。

回退兼容：纯新增一个带默认值 ``[]`` 的 JSON 列。旧代码不认识它，回退后不读
不写——新订阅退回「一律用默认规则组」，已有订阅挂靠的规则组不受影响；再升级
回来适用范围配置原样还在。无运行时依赖变更，不 bump runtime-version。

Revision ID: b6e2d8f4a193
Revises: f3a9c7e1d428
Create Date: 2026-09-24 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e2d8f4a193"
down_revision: str | None = "f3a9c7e1d428"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("rule_set") as batch_op:
        batch_op.add_column(
            sa.Column("match_rules", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("rule_set") as batch_op:
        batch_op.drop_column("match_rules")
