"""add library.scrape_overrides

媒体库级的刮削偏好覆盖（docs/design/scrape-customization.md §1 / P3）：
动漫库要日文原名海报、纪录片库要无字背景、电影库与剧集库各用一套命名
模板——这些"按库口味"是收藏玩家的真实场景，全局一套设置盖不住。

列语义：JSON 对象，**只存显式覆盖的字段**（空对象 = 全跟全局）。读取端
按 `全局设置 → 库覆盖` 的顺序合并（services/scrape_config.merge_for_library）。

向前兼容：纯加列且可空，旧代码回退后忽略该列，不丢数据；已有库行取
NULL，合并读取按"全跟全局"处理，行为与升级前完全一致。

Revision ID: c8e3fa5b7d12
Revises: b7d2e9a45c31
Create Date: 2026-08-27 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e3fa5b7d12"
down_revision: str | None = "c8e3f0b56d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scrape_overrides", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.drop_column("scrape_overrides")
