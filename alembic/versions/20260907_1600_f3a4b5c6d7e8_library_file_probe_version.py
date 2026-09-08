"""library_file 新增 probe_version：识别「用旧版字段集探测过」的行

此前台账行的新鲜度只有一个标记——``audio_streams IS NULL`` 表示「从没探测
成功过」。它认不出另一类行：用旧版字段集探测**成功**过、但缺后来新增字段的行。

2026-09 新增 ``frame_rate`` / ``color_space`` 与 Dolby Vision 识别后，早于那一版
入库的行永远拿不到这几项（补探判据把它们排除在外），于是 DV 片永远识别不出来，
播放链读台账也就永远不做色调映射——issue #331 里 DV P5 画面发绿的直接成因。

向前兼容：纯新增可空列。旧代码回退后忽略它，补探退回原来的判据，不会报错。
存量行一律为 NULL，下一次**手动扫描**会补探一遍（限量自愈不碰，避免把真正
需要重试的失败行挤出那 10 个名额）。

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-09-07 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("library_file", sa.Column("probe_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    # SQLite 不支持 ALTER TABLE DROP COLUMN，必须走 batch（重建表）——
    # 与仓库既有迁移同款做法
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("probe_version")
