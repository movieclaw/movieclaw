"""downloader_client 增加 path_health：路径映射的可达性体检结果

「测试连接」原本只验证 API 通不通，但 movieclaw 与下载器常常分处不同容器、
两边看同一块盘的路径不同（``path_mappings`` 为此存在）。**API 通、路径瞎**
是真实发生过的组合：容器 bind mount 解析失败，docker 兜底建了个空目录，
配置看起来完全正常，下载器状态一直是绿的，而所有下载都无法入库。

``path_health`` 存放每条映射的体检结论（``downloader_paths.PathProbe`` 的扁平
形态列表），让下载器的"可用"从"API 通"升级为"API 通且路径可达"。

向前兼容：新增可空列，存量行为 NULL（= 尚未体检，不影响任何既有判定）；
旧代码回退后忽略该列，行为与现在一致。

Revision ID: f7b3c9d2e814
Revises: e5a1b2c3d4f6
Create Date: 2026-09-05 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7b3c9d2e814"
down_revision = "e5a1b2c3d4f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "downloader_client",
        sa.Column("path_health", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("downloader_client", "path_health")
