"""库存台账：把原盘（BDMV / VIDEO_TS / ISO）的片源回填为 Disc（T6）

原盘此前只能落成 Blu-ray(T4) 或"片源未知"——而 Remux 是 T5，于是一个
2160p Remux 候选会被洗版判成升级，把原盘替换掉并送进回收站（默认
upgrade_keep_old=False）。Remux 本来就是从原盘剥出来的，这是降级。
片源阶梯已补上原盘档 T6（movieclaw_matcher.DISC_SOURCE），扫描与入库
两条写路径也已按结构落值，但**存量台账行不会自愈**：秒过行的增量刷新
不重写 media_source，只有文件本体变化才会重新落值（issue #163）。

判据取容器列，与写路径同源：BDMV 目录落 "bluray"、VIDEO_TS 落 "dvd"、
ISO 镜像按扩展名落 "iso"，三者都是整张盘。人工标注过的行不动
（media_source_manual = 1，保护位的语义就是"自动写入不得覆盖"）。

向前兼容说明：纯数据回填，不动表结构。回退不需要反向操作——旧代码读到
"Disc" 时片源档查表落空，退回"未知不可比"，即修复前对绝大多数原盘的
行为（安静、不参与洗版比较），不会误洗。

Revision ID: b2c7d84e5f16
Revises: a1f6c72b9d04
Create Date: 2026-08-31 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b2c7d84e5f16"
down_revision: str | None = "a1f6c72b9d04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE library_file SET media_source = 'Disc' "
        "WHERE container IN ('bluray', 'dvd', 'iso') "
        "AND NOT media_source_manual "
        "AND (media_source IS NULL OR media_source <> 'Disc')"
    )


def downgrade() -> None:
    # 不可逆：回填前的值（NULL 或名称解析出的 Blu-ray）没有保留，也没有
    # 保留的必要——见上方向前兼容说明，旧代码读到 'Disc' 即退回"未知"。
    pass
