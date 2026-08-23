"""wanted_item 加单集履历注解两列：最近被拒原因 / 投递种子名

订阅详情页改为「一集一条履历」的里程碑链后，工单自身的六站时间戳已够
渲染链条，唯一缺的叙事细节是「最近为什么被拒 / 投的是哪个种子」——
此前散在活动流水里，按季集捞既慢又受条数上限。落成工单上的冗余快照，
写入点顺手更新（拒绝：matching._log_rejection；投递：dispatch）。

向前兼容说明：两列均 nullable、无默认值语义，旧版本模型不感知这两列，
SELECT/INSERT 均按模型列名进行，回退后新列被忽略、数据保留不丢。

Revision ID: e4a7b0d3f569
Revises: d3f6a9c2e458
Create Date: 2026-08-23 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7b0d3f569"
down_revision: str | None = "d3f6a9c2e458"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_reject_reason", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("grab_title", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.drop_column("grab_title")
        batch_op.drop_column("last_reject_reason")
