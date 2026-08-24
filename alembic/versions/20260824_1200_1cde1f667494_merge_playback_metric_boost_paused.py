"""merge playback_metric and boost_paused

把分叉的两条迁移线并回一条：网页播放器分支加的 `playback_metric`
（e4a7b1c8d539）与主干加的站点刷流暂停开关列（f5b8c1e4a671）都挂在
e4a7b0d3f569 底下，合并分支后 alembic 出现两个 head，升级会直接报错。

**为什么用 merge 节点而不是把 down_revision 改成串行**：串行改法会让
已经跑过 e4a7b1c8d539 的库（本分支的测试部署就是）永远漏掉主干那条列
——alembic 只认 alembic_version 里那一个 revision，链一改就把主干迁移
当成"已应用"跳过，表里缺列。merge 节点则会让这类库先补上主干那条、
再落到本节点。

本节点自身不改任何 schema（两条迁移互不相干，纯粹是拓扑合流）。

Revision ID: 1cde1f667494
Revises: e4a7b1c8d539, f5b8c1e4a671
Create Date: 2026-08-24 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "1cde1f667494"
down_revision: str | Sequence[str] | None = ("e4a7b1c8d539", "f5b8c1e4a671")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空操作：两条线各自的 upgrade 已经做完了，这里只负责合流。"""


def downgrade() -> None:
    """空操作：回退时拓扑重新分叉，两条线各自的 downgrade 负责真正的回退。"""
