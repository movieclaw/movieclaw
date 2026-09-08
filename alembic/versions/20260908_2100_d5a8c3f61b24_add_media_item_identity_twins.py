"""add identity_twins to media_item

同名同年孪生条目的探测缓存（docs/design/identity-confidence.md §9）：
[{tmdb_id, title, year, imdb_id, runtime_minutes}]。存清单而不是布尔——孪生的
imdb_id 与 runtime 都是判别器要用的证据（"站点标的编号是不是那一部""这个体积
更像谁的片长"）。

三态语义：NULL=未探测（探测失败也留 NULL，网络抖动不能把订阅卡成待确认）、
[]=探测过且没有孪生、非空=有孪生，自动投递门槛升一档。

向前兼容：纯新增可空列、无回填。旧版本回退后不认识这一列，读写都不碰。

Revision ID: d5a8c3f61b24
Revises: c9f4b1e7a352
Create Date: 2026-09-08 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5a8c3f61b24"
down_revision: str | None = "c9f4b1e7a352"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_item") as batch:
        batch.add_column(sa.Column("identity_twins", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("media_item") as batch:
        batch.drop_column("identity_twins")
