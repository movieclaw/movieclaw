"""add media_item.english_title

主动搜索的召回词原来首选 ``original_title``，理由写的是"种子多为英文命名"。
但 ``original_title`` 是 TMDB 的**原始语言标题**，只在影片原语言就是英语时
才等于英文名：韩语片是「악인전」、日语片是「万引き家族」——恰恰是中文 PT 站
最不可能用来命名的那个写法。真实教训：一部韩语片用原名召回 4 条（全被规则
拒），用中文名召回 70 条正片，而那 70 条从未进入过候选池。

英文名此前只以无标签文本躺在 ``media_item.aliases`` 里（``_build_aliases``
把 title / original_title / 地区别名 / zh+en 译名拍进同一个扁平列表，语言标签
在那一步就丢了），搜索时无法把它认出来。单独存一列，让最重要的召回通道不必
依赖"挑第一个 ASCII 串"这种猜测。

存量条目不写一次性回填任务：下次元数据刷新时由 ``merge_identity_fields``
自然补上（``services/media_scrape.py``）。回填前该列为 NULL，召回词退回
"中文名 + 原名"两个，也就是今天的行为，不会更差。

向前兼容（CLAUDE.md 硬约束 3）：纯新增一列、可空，旧版本回退后不认识它，
读写都不碰（回退即回到今天的召回策略）。

Revision ID: e4a7c2b9d165
Revises: e6b2d4f8a137
Create Date: 2026-09-11 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7c2b9d165"
down_revision: str | None = "e6b2d4f8a137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_item", sa.Column("english_title", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_item", "english_title")
