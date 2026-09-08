"""add identity_doubt to library_file

入库时长体检的存疑记录（docs/design/identity-confidence.md §8）：实测片长与
影片信息严重不符时留痕，但**照常入库**——踩线更常见的原因是导演剪辑版/加长版，
拦下来的代价大于收益。

不复用 review_suggestion：它的语义是"我觉得应该改成那个"，结构里必须有
media_item_id；时长反证给不出替代条目，硬塞会让复核清单里混进一批点不动
"采纳建议"的行。

向前兼容：纯新增可空列、无回填。旧版本回退后不认识这一列，读写都不碰。

Revision ID: c9f4b1e7a352
Revises: b7e3a9c1d240
Create Date: 2026-09-08 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f4b1e7a352"
down_revision: str | None = "b7e3a9c1d240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file") as batch:
        batch.add_column(sa.Column("identity_doubt", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file") as batch:
        batch.drop_column("identity_doubt")
