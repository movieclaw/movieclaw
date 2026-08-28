"""为监听入库保存下载器真实名称与落点

Revision ID: c8e3f0b56d42
Revises: b7d2e9a45c31
Create Date: 2026-08-27 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e3f0b56d42"
down_revision: str | None = "b7d2e9a45c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("manual_download_intent", schema=None) as batch_op:
        batch_op.add_column(sa.Column("downloader_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("download_name", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("save_path", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_manual_download_intent_downloader_id",
            "downloader_client",
            ["downloader_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_manual_download_intent_downloader_id", ["downloader_id"], unique=False
        )
        batch_op.create_index(
            "ix_manual_download_intent_download_name", ["download_name"], unique=False
        )

    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.add_column(sa.Column("download_name", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("save_path", sa.Text(), nullable=True))
        batch_op.create_index(
            "ix_subscription_download_attempt_download_name", ["download_name"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.drop_index("ix_subscription_download_attempt_download_name")
        batch_op.drop_column("save_path")
        batch_op.drop_column("download_name")

    with op.batch_alter_table("manual_download_intent", schema=None) as batch_op:
        batch_op.drop_index("ix_manual_download_intent_download_name")
        batch_op.drop_index("ix_manual_download_intent_downloader_id")
        batch_op.drop_constraint("fk_manual_download_intent_downloader_id", type_="foreignkey")
        batch_op.drop_column("save_path")
        batch_op.drop_column("download_name")
        batch_op.drop_column("downloader_id")
