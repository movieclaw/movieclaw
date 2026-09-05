"""library 增加可见范围两列：access_mode 与 admin_visible

每个库都有「谁能浏览」（docs/design/library-access.md）：

- ``access_mode``：``everyone``（默认，对全部成员自动开放，含以后新建的成员）
  / ``selected``（只对 ``member_library_access`` 里显式授权的成员开放）；
- ``admin_visible``：超管本人是否在浏览范围内。超管不是成员行，进不了
  白名单表，所以单独一列；默认 true，存量库对超管零变化。

向前兼容：两列都带默认值，旧代码回退后忽略它们，行为与现在一致。

Revision ID: b8d4e2f1a9c3
Revises: f7b3c9d2e814
Create Date: 2026-09-05 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d4e2f1a9c3"
down_revision = "f7b3c9d2e814"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("library") as batch:
        batch.add_column(
            sa.Column(
                "access_mode",
                sa.String(),
                nullable=False,
                server_default="everyone",
            )
        )
        batch.add_column(
            sa.Column(
                "admin_visible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("library") as batch:
        batch.drop_column("admin_visible")
        batch.drop_column("access_mode")
