"""add identity_confidence / matched_alias to subscription_download_attempt

投递时的身份证据强度落台账——"当初凭什么认定这个种子就是这部片"
（docs/design/identity-confidence.md §5.3）。入库时 info_hash 认领会继承它，
据此决定要不要再做一次反证体检。

向前兼容：两列纯新增且可空，无回填（旧数据留 NULL，语义是"特性上线前，
证据强度未知"）。旧版本回退后完全不认识这两列，读写都不碰，不会异常。

Revision ID: b7e3a9c1d240
Revises: c4d9e2f7a815
Create Date: 2026-09-08 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3a9c1d240"
down_revision: str | None = "c4d9e2f7a815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt") as batch:
        batch.add_column(sa.Column("identity_confidence", sa.String(), nullable=True))
        batch.add_column(sa.Column("matched_alias", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt") as batch:
        batch.drop_column("matched_alias")
        batch.drop_column("identity_confidence")
