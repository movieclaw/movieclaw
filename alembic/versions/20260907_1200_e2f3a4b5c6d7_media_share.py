"""新增 media_share：影片分享（一条链接 = 一个条目对外可看）

docs/design/media-share.md §3。分享范围是一个条目（剧集整部），一部影片
同一时间只有一条有效分享（服务层保证）。密码 Fernet 可逆加密（创建者要能
回显），``password_version`` 变更即作废旧的解锁 Cookie。过期 / 取消的行保留。

条目与库删除时行级联删除。向前兼容：纯新增表，旧代码回退后忽略它。

Revision ID: e2f3a4b5c6d7
Revises: d1a2b3c4e5f6
Create Date: 2026-09-07 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_share",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "library_id",
            sa.Integer(),
            sa.ForeignKey("library.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by_member_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("password_encrypted", sa.String(), nullable=True),
        sa.Column("password_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_media_share_slug", "media_share", ["slug"], unique=True)
    op.create_index("ix_media_share_media_item_id", "media_share", ["media_item_id"])
    op.create_index("ix_media_share_expires_at", "media_share", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_media_share_expires_at", table_name="media_share")
    op.drop_index("ix_media_share_media_item_id", table_name="media_share")
    op.drop_index("ix_media_share_slug", table_name="media_share")
    op.drop_table("media_share")
