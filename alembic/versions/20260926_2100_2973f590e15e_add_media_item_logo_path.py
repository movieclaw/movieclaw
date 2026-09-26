"""add media_item.logo_path（片名 Logo）

订阅首页改成流媒体式沉浸 Hero 后，片名用 TMDB 的透明底片名字标（Logo）
代替纯文字。Logo 与海报、背景图同在一次详情请求的 images 里，建档与定时
刷新顺手挑出来落库，不增加 TMDB 请求（挑选规则见
``movieclaw_media.library.pick_logo``）。

三态：NULL=还没从 TMDB 取过；空串=取过但该片没有可用 Logo。

存量回填：老条目本来就会被元数据刷新任务按档期自然回填（同 english_title
加列时的做法），但完结剧/已上映电影一周才刷一次，订阅首页要等很久才有
Logo。这里只把**有订阅**的条目的下一次刷新提前到「立即到期」——不写一次性
回填任务，刷新任务照常每 tick 处理几条，几小时内补齐；没有订阅的条目不动。

回退兼容：纯新增一个可空列。旧代码不认识它，回退后不读不写；提前的刷新
时间只是让刷新任务早点跑，对旧代码同样成立。无运行时依赖变更，不 bump
runtime-version。

Revision ID: 2973f590e15e
Revises: b6e2d8f4a193
Create Date: 2026-09-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2973f590e15e"
down_revision: str | None = "b6e2d8f4a193"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_item", sa.Column("logo_path", sa.String(), nullable=True))
    op.execute(
        "UPDATE media_item SET next_refresh_at = NULL "
        "WHERE source = 'tmdb' AND id IN (SELECT media_item_id FROM subscription)"
    )


def downgrade() -> None:
    op.drop_column("media_item", "logo_path")
