"""吸收台账拆出「镜像自己写的那份」指纹

配套 b7e2a9c4d318。此前只有 ``media_metadata.nfo_fingerprint`` 一列，同时承担
两件事：**存量回填的判据**（NULL=从未查过）与**「这份 NFO 别再吸收」的判据**。
两者合一在两个方向上都出了问题：

1. 分集 NFO 根本没有台账。镜像每次刷新都按 ``media_episode`` 重写
   ``<视频名>.nfo``，下一次刷新的吸收又把它读回来（``absorb_episode_nfos``
   对集名/简介是无条件覆盖）——集名与单集简介于是**永久冻结在第一次镜像时
   的内容**，TMDB 后补的真标题（新播剧集常见：先占位"第 N 集"，几天后才补）
   永远显示不出来。
2. 条目级把"我们自己写的"与"用户的、没改过的"当成了同一回事：指纹一致就
   跳过吸收，而每次刷新 ``apply_display_profile`` 都无条件把 TMDB 值写回
   展示列。对关掉了媒体目录镜像的库（``write_media_assets`` / ``mirror_nfo``），
   磁盘上一直是用户那份、指纹不变，于是用户的 NFO 在刷新后静默失效。

拆开之后语义各归各位：``nfo_fingerprint`` 只答"这个条目查过 NFO 没有"（回填
用），``nfo_mirror_fingerprint`` 只答"磁盘上这份是不是我们自己写出去的"。

两列的存量取值刻意不同
----------------------
- **条目级照抄** ``nfo_fingerprint``：条目级的旧行为本来就是对的（镜像写完
  把指纹记进 ``nfo_fingerprint``，下一次刷新据此跳过）。不照抄的话，升级后
  第一次刷新会把上一轮镜像出去的旧内容当用户 NFO 吸收一次，反而把刚拉回来
  的 TMDB 新数据盖掉——凭空造一次回归。代价是 2. 里那批用户要等自己下次改动
  NFO 才享受到修复，可接受：那正是他们会注意到的时刻。
- **分集级留 NULL**：分集的旧行为本来就是坏的（每次刷新都吸收自家副本），
  留 NULL 意味着升级后第一次刷新与今天等价，之后镜像记上指纹就恢复正常。
  单调变好，不会比今天更差。

向前兼容（CLAUDE.md 硬约束 3）：纯新增两列、均可空。旧版本回退后不认识它们，
读写都不碰——回退即回到旧行为。

Revision ID: c9f4a1e6b573
Revises: b7e2a9c4d318
Create Date: 2026-09-12 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f4a1e6b573"
down_revision: str | None = "b7e2a9c4d318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_metadata", sa.Column("nfo_mirror_fingerprint", sa.String(), nullable=True))
    op.add_column("media_episode", sa.Column("nfo_mirror_fingerprint", sa.String(), nullable=True))
    # 条目级照抄（空串是"查过、确实没有"，不是指纹，不抄）
    op.execute(
        "UPDATE media_metadata SET nfo_mirror_fingerprint = nfo_fingerprint "
        "WHERE nfo_fingerprint IS NOT NULL AND nfo_fingerprint != ''"
    )


def downgrade() -> None:
    op.drop_column("media_episode", "nfo_mirror_fingerprint")
    op.drop_column("media_metadata", "nfo_mirror_fingerprint")
