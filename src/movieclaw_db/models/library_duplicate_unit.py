from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin, utcnow


class LibraryDuplicateUnit(TimestampMixin, table=True):
    """一次重复扫描的结论：一个多文件单元一行（docs/design/library-duplicate-files.md §9）。

    为什么要落库：检测本身不贵，贵的是**为了检测要摸的东西**——单元内每个文件
    各 stat 一次（网络盘上就是几千次往返）、每个文件重跑一遍发布名解析 enrich
    才排得出「建议保留」。第一版把这些放在 ``GET`` 的请求线上现算，页面每 30 秒
    还轮询一次，万级媒体库打开即卡。现在改成：**扫描算一次、落这张表，页面只读
    这张表**，与「扫描媒体库」是同一种东西——用户按一次，任务跑一会儿，看结果。

    一行 = 一个单元 ``(media_item_id, season_number, episode_number)``，存的是
    「当时那一轮的结论」：属于哪一堆、要用户花多少心思（``tier``）、建议留哪个、
    依据是什么。**不存展示文案**（规格标签、来源 label 都从台账现拼），因为那些
    只在翻到的那一页才需要，几十行的成本可以忽略。

    结论会过期：扫描之后跑过入库 / 洗版 / 另一轮扫描，文件集合就变了。读的时候
    按 ``file_ids`` 与台账现状**逐单元比对**，对不上的不显示也不清理（等下一轮
    扫描修正），绝不按过期结论删文件。表整体由每轮扫描重建，不做增量维护——
    维护一份随时可能失真的增量索引，比重算一遍更容易出错。
    """

    __tablename__ = "library_duplicate_unit"
    __table_args__ = (
        # 列表与摘要的热路径：按库筛 + 按分档分组
        Index("ix_library_duplicate_unit_scope", "library_id", "tier"),
        # 条目详情页入口（?item=）与决定后的定点删除
        Index(
            "ix_library_duplicate_unit_unit",
            "media_item_id",
            "season_number",
            "episode_number",
            unique=True,
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    library_id: int = Field(index=True, description="单元所在库（跨库重叠时取第一份，展示用）")
    media_item_id: int = Field(description="条目 id")
    season_number: int = Field(description="季号；电影为 0（哨兵）")
    episode_number: int = Field(description="集号；电影为 0（哨兵）")

    bucket: str = Field(description="identical（一模一样）/ versions（不同版本）")
    tier: str = Field(
        description=(
            "要用户花多少心思：safe（放心清，机器确定没区别）/ "
            "suggested（建议清，有一个明显更好）/ review（要你决定，各有各的好）"
        )
    )
    review_kind: str | None = Field(
        default=None,
        description=(
            "tier=review 时这个单元的取舍类型，同一种取舍聚成一组一次决定："
            "resolution（分辨率不同）/ hdr（HDR 与 SDR）/ unknown（片源未知比不了）/ "
            "same_tier（同档不同版本）；其余档为 NULL"
        ),
    )

    file_ids: list[int] = Field(
        sa_column=Column(JSON, nullable=False),
        description="扫描时这个单元的在位文件 id（升序）；读时与台账现状比对，对不上即过期",
    )
    suggested_file_id: int = Field(description="建议保留的文件 id")
    suggest_reason: str | None = Field(
        sa_column=Column(Text, nullable=True),
        description="建议依据：档位最高 / 同档，实测码率更高 / 档位无法比较，按实测码率建议 …",
    )
    extra_files: int = Field(description="按建议清理会清掉几个文件（非建议保留、非「都留着」）")
    extra_bytes: int = Field(description="按建议清理能腾出多少字节")

    scanned_at: datetime = Field(
        default_factory=utcnow, description="算出这条结论的那一轮扫描的时间"
    )
