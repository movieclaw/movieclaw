from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, ForeignKey, Integer, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin, utcnow


class IngestStatus(StrEnum):
    """ingest_entry.status 的取值。

    「待处理」与「失败」的分界是能否靠重试自愈：识别不出是**信息不足**，
    重试一百次 TMDB 也不会改主意，唯一有效的信号是输入变化（用户改名 →
    指纹变化 → 自动重试）或人来拍板（认领/忽略），所以它安静地待在清单里、
    不告警不重试；探测失败/搬运出错/TMDB 不可达是**环境故障**，时间驱动
    的重试有意义，才走退避重试 + 告警。
    """

    IMPORTED = "imported"  # 已搬进库（可能带部分文件的跳过说明）
    PENDING = "pending"  # 识别不出，等人工认领或忽略；不告警、不定时重试
    FAILED = "failed"  # 环境故障（探测失败/搬运出错/TMDB 不可达），退避重试
    SKIPPED = "skipped"  # 非影视内容（无视频文件/原盘），永久跳过
    IGNORED = "ignored"  # 用户忽略或「跳过存量」基线，永久跳过（指纹变化也不复活）


class IngestEntry(TimestampMixin, table=True):
    """下载监听目录条目的处理台账。

    条目 = 监听目录下的一个顶层文件/目录（一次下载任务的产物）。台账的
    职责是**幂等与可追溯**：源文件搬运后仍留在监听目录（硬链保种/复制
    留档），没有这张表每轮扫描都会重复处理同一批条目。

    重试语义靠 ``fingerprint``（条目全树的 大小:文件数:mtime 摘要）：
    - 指纹不变：imported/skipped/pending 不再处理（pending 等的是人，
      不是时间），failed 按时间退避重试；
    - 指纹变化：重新处理——覆盖"下载在继续（此前误判）"、"季包边下边
      补集"与"用户改名后重识别"三种场景，已入库的文件靠目标去重天然幂等；
    - ignored 例外：用户拍板（或「跳过存量」基线）是永久决定，指纹变化
      也不复活，恢复处理走接口显式操作。
    """

    __tablename__ = "ingest_entry"

    id: int | None = Field(default=None, primary_key=True)

    library_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        description="归属库；NULL=自动路由规则的条目在选定目标库之前失败（识别不出等）",
    )
    entry_path: str = Field(
        sa_column=Column(Text, nullable=False, unique=True, index=True),
        description="条目绝对路径（监听目录的顶层文件/目录）",
    )
    fingerprint: str = Field(description="处理时的条目指纹（大小:文件数:mtime）")
    status: str = Field(description="imported / pending / failed / skipped / ignored")
    message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="处理结论（中文，含跳过/失败原因）",
    )
    imported_count: int = Field(default=0, description="本条目累计入库的文件数")
    media_item_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description=(
            "本条目识别/认领到的作品；NULL=尚未定身份（待处理、非影视跳过等）。"
            "一部剧的多个版本（不同发布组、DV/HDR 各一个种子）是多个条目但同一"
            "作品，摘要行的「已入库」按这一列去重才是用户认知的「几部作品」"
        ),
    )
    attempted_at: datetime = Field(
        default_factory=utcnow, description="最近一次处理时间（failed 的退避重试依据）"
    )
    claimed_tmdb_id: int | None = Field(
        default=None,
        description=(
            "人工认领钉住的 TMDB id；NULL=未认领。用户拍板是最高权威：认领当轮"
            "环境故障失败后，自动重试凭它还原身份，不回退重新识别"
        ),
    )
    claimed_kind: str | None = Field(
        default=None,
        description="认领时的媒体类型（movie/tv），与 claimed_tmdb_id 一起还原认领身份",
    )
    # —— 电影合集（issue #438）：一个条目目录里装着多部电影 ——————————————
    # 台账仍是「一次下载一行」（清单、统计、认领都以顶层条目为单位），合集里
    # 各部电影的身份按文件记在下面两列：识别不出的文件列出来供人工逐个认领，
    # 认领结论按文件钉住（与 claimed_tmdb_id 同理，重试时直接还原，不再重识别）
    unresolved_files: list[str] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description=(
            "合集条目里识别不出身份的正片文件（条目内相对路径）；NULL/空=没有。"
            "每轮处理结论重写：不再是合集或都已识别时清空"
        ),
    )
    claimed_files: dict[str, int] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="合集条目的逐文件人工认领：条目内相对路径 → TMDB id（类型同规则先验）",
    )
    collection_item_ids: list[int] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description=(
            "合集条目已入库的各部作品（media_item.id）；NULL=不是合集。合集没有单一"
            "身份（media_item_id 为 NULL），摘要行的「已入库 N 部」靠这一列按部计数"
        ),
    )
