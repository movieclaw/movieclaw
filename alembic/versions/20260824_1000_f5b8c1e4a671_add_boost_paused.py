"""站点凭据：刷流暂停开关列

刷流做种上行占满带宽会影响用户前台使用（看视频等），此前只能整个关闭
刷流。新增 boost_paused 暂停闸：暂停期间引擎把该站在池做种批量压到极低
上传限速，并停止汰换与拉新种；恢复时解除限速、回到正常节奏。

向前兼容说明：加列 NOT NULL + server_default false（旧数据一律视为未暂停），
旧代码忽略该列即可，回退不丢数据。

Revision ID: f5b8c1e4a671
Revises: e4a7b0d3f569
Create Date: 2026-08-24 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b8c1e4a671"
down_revision: str | None = "e4a7b0d3f569"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("site_credential", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "boost_paused",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("site_credential", schema=None) as batch_op:
        batch_op.drop_column("boost_paused")
