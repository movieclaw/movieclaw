"""llm_provider 从单例改为多实例：加 name（实例名）

以前全表至多一行、只能接入一家供应商；现在可同时接入多家（官方 + 中转 +
自建），对话框里可以在全部实例的模型间切换。``name`` 是实例名，全局唯一、
不含斜杠，是「实例名/模型id」精确路由的键。

「哪个模型是默认」不在本表：智能体 / 字幕处理各自的默认模型是 AI 设定
（app_setting 的 llm.defaults 域），接入与设定分开。

存量回填：唯一的那一行实例名取预设显示名（与迁移前对话记录里显示的
provider 名一致）。

向前兼容：只加列不改列。旧版本回退后 ``select … limit 1`` 仍能读到
一行；多出的列被忽略。

Revision ID: d1a2b3c4e5f6
Revises: d1e2f3a4b5c6
Create Date: 2026-09-07 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1a2b3c4e5f6"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# provider_type → 预设显示名（与 movieclaw_llm/providers/presets/*.yaml 一致）。
# 迁移不 import 应用代码（预设目录以后会变，迁移必须冻结在写下时的事实）。
_DISPLAY_NAMES = {
    "openai": "OpenAI",
    "bailian": "阿里云百炼",
    "deepseek": "DeepSeek 官方",
    "kimi": "Kimi 官方（月之暗面）",
    "glm": "智谱 GLM 官方",
    "openai_compat": "OpenAI 兼容端点",
}


def upgrade() -> None:
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        # 先允许为空以便回填，回填完再收紧为 NOT NULL + 唯一索引
        batch_op.add_column(sa.Column("name", sa.String(), nullable=True))

    # 存量回填：实例名取预设显示名；未知类型（理论上不存在）退回 provider_type 本身
    case = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in _DISPLAY_NAMES.items())
    op.execute(
        f"UPDATE llm_provider SET name = CASE provider_type {case} ELSE provider_type END "
        "WHERE name IS NULL"
    )
    # 单例时代理论上只有一行；万一有多行，同名会撞唯一索引，给后面的行加序号
    op.execute(
        "UPDATE llm_provider SET name = name || ' ' || id "
        "WHERE id NOT IN (SELECT MIN(id) FROM llm_provider GROUP BY name)"
    )

    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.alter_column("name", existing_type=sa.String(), nullable=False)
        batch_op.create_index(batch_op.f("ix_llm_provider_name"), ["name"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_provider_name"))
        batch_op.drop_column("name")
