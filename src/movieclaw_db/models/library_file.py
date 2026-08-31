from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Index, Integer, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin

# 三态 JSON 列专用：Python None 落成**SQL NULL**，而不是 JSON 文本 'null'。
#
# SQLAlchemy 的 JSON 类型默认把 None 序列化成 JSON 的 null（因为 JSON 文档里
# null 是个合法值，与"这一列没有值"不是一回事）。对本表的三态列来说这个默认
# 是灾难性的：`WHERE audio_streams IS NULL` 永远匹配不到任何行，`IS NOT NULL`
# 则匹配到每一行——身份复核清单曾因此把整个库都列了出来。
#
# 只有语义上真·三态的列才该用它（NULL=没有 / []=有但为空）。默认 [] 的
# 列表列（aliases、genres…）不受影响，也不需要改。
_NullableJson = JSON(none_as_null=True)


# 原盘（完整盘）在台账里的判别依据：容器列。扫描/入库落 BDMV 目录时写
# "bluray"、VIDEO_TS 写 "dvd"，ISO 镜像按扩展名落 "iso"——三者都是**整张盘**
# 而不是从盘里剥出来的单个文件（.m2ts 单流、Remux 单 mkv 都不算）。
# 片源阶梯据此判 T6（movieclaw_matcher.DISC_SOURCE），见 issue #163。
DISC_CONTAINERS: frozenset[str] = frozenset({"bluray", "dvd", "iso"})


class FileSource(StrEnum):
    """library_file.source 的取值。"""

    IMPORTED = "imported"  # 入库管线产出（订阅/手动下载 → 整理器硬链）
    SCANNED = "scanned"  # 存量扫描发现（部署前就在库根路径下的文件）


class FileState(StrEnum):
    """library_file.state 的取值——文件生命周期的**唯一判别式**
    （docs/design/library-file-recycle.md §2）。

    在此之前"在位/缺失"靠 ``missing_since`` 判空推导，每个消费方各自手写
    条件；状态显式化后查询一律看 ``state``，时间戳降级为状态附属属性。
    状态迁移只允许经三个写路径：扫描对账（in_place ⇄ missing）、回收服务
    （in_place ⇄ trashed）、清理任务（trashed → 删行）。
    """

    IN_PLACE = "in_place"  # 在位：文件在 file_path 上，正常参与一切口径
    MISSING = "missing"  # 缺失：对账发现文件意外消失（missing_since 记时间）
    TRASHED = "trashed"  # 待回收：主动移除，等待清理/恢复（trashed_at 记时间）


class UnidentifiedCode(StrEnum):
    """识别失败的**分类**（``unidentified_reason`` 是给人看的整句，这是给
    机器用的分类）。

    两者分开是因为职责不同：文案会随体验迭代反复改写，分类要稳定——待识别
    清单靠它决定标签措辞、配色与可用动作（如"TMDB 不可达"是系统故障、
    重扫即可自愈，该跟"需要你拍板"的项目明确区分开，不该混用同一种警示色）。
    """

    UNPARSABLE = "unparsable"  # 文件名/目录名里根本解析不出片名
    TMDB_UNREACHABLE = "tmdb_unreachable"  # TMDB 网络/接口故障——修好重扫即可
    AMBIGUOUS = "ambiguous"  # 有候选但机器不敢拍板（同名双版本等）
    NO_MATCH = "no_match"  # TMDB 里找不到对得上的条目
    # 文件是剧集却在电影库（或反之）：库类型选错，认领单个文件没有意义——
    # 得换库。不识别是**故意**的：TMDB 的 movie/tv 是两套 id 空间，按错误
    # 类型拉档会静默拿到一部毫不相干的作品（见 library_scan._kind_conflict）
    KIND_MISMATCH = "kind_mismatch"


class IdentitySource(StrEnum):
    """library_file.identity_source 的取值——身份是怎么来的。

    对账机制据此分级：``MANUAL``（人工认领/复核确认）永不被自动翻案；
    其余三种是机器结论，识别器升级后允许复核。NULL = 特性上线前的旧数据，
    视同机器结论参与复核。
    """

    MANUAL = "manual"  # 人工认领，或复核清单里用户拍板过（采纳/维持均算）
    PATH_TAG = "path_tag"  # 目录名 [tmdbid=N] 显式标记
    NFO = "nfo"  # NFO 里的条目级 tmdb id
    RESOLVED = "resolved"  # 名称解析 + TMDB 证据收敛


class LibraryFile(TimestampMixin, table=True):
    """库存台账——"我实际拥有哪个文件"的物理真相源（docs/design/library.md 2.2）。

    设计要点：
    - ``media_item_id`` 可空：**NULL = 未识别**，进"待识别"清单等人工认领
      （宁可待确认，不静默错挂——与订阅低置信度同哲学）；
    - 季/集号沿用 wanted 的约定：电影 (0,0) 哨兵，NOT NULL 保证唯一性可用；
    - 介质规格来自 **ffprobe 对文件本体的探测**（不来自种子名）；探测失败
      保持 NULL（三态铁律）；
    - ``file_path`` 用 Text + 唯一索引（真实媒体路径经常超 255，且它是
      核心去重键——moviebot 的反面教训）；
    - 同条目多版本（1080p 与 2160p 并存）天然支持多行，去重/洗版是 P6 议题；
    - 文件消失不删记录：``missing_since`` 标记，对账任务维护。
    """

    __tablename__ = "library_file"
    __table_args__ = (
        # 库存查询与 wanted 跳过判定的热路径
        Index(
            "ix_library_file_media_unit",
            "media_item_id",
            "season_number",
            "episode_number",
        ),
        # Jellyfin 首页浏览热路径：Latest 按库过滤在位文件，再按媒体单元聚合
        # 最新入库时间；电影库默认分页也会按库/条目检查文件是否在位。把这些
        # 纯标量键放进同一棵索引，SQLite 可只扫描索引而不读取音轨/字幕 JSON。
        Index(
            "ix_library_file_browse_unit",
            "library_id",
            "state",
            "media_item_id",
            "season_number",
            "episode_number",
            "created_at",
        ),
        # 改名归并的候选池查询（同库同尺寸）。缺了它，首次扫描每落一个新文件
        # 都要扫一遍本库全部台账行，整轮就是 O(文件数²)
        Index("ix_library_file_library_size", "library_id", "size_bytes"),
    )

    id: int | None = Field(default=None, primary_key=True)

    def is_disc(self) -> bool:
        """本行是否是一张完整原盘（BDMV / VIDEO_TS / ISO）。

        判据取容器列而不是重新探路径：台账行在文件所在存储卸载时也要能
        回答这个问题（洗版比较发生在数据库里，不碰磁盘）。
        """
        return (self.container or "") in DISC_CONTAINERS

    @classmethod
    def in_place(cls):
        """库存默认口径（library-file-recycle.md §6）：只算在位文件。

        全站共享的唯一判别——将来再加状态，所有消费方零改动。
        """
        return cls.state == FileState.IN_PLACE

    library_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="归属库",
    )
    # 全局身份锚；NULL = 未识别（待识别清单）
    media_item_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("media_item.id", ondelete="SET NULL"), nullable=True),
        description="媒体条目身份锚；NULL=未识别",
    )
    season_number: int = Field(default=0, description="季号；电影=0（哨兵）")
    episode_number: int = Field(default=0, description="集号；电影=0（哨兵）")

    # -- 文件本体 ------------------------------------------------------------
    file_path: str = Field(
        sa_column=Column(Text, nullable=False, unique=True, index=True),
        description="绝对路径（movieclaw 视角）",
    )
    size_bytes: int = Field(default=0, description="文件大小（字节）")
    # 文件 mtime（纳秒）：扫描/入库时随 stat 顺手落库，播放接口的 ETag 直接
    # 由它派生——浏览类请求不再对媒体文件本体做任何文件系统调用（云盘挂载
    # 上一次 stat 就是一次网络往返，还会唤醒休眠盘）。NULL=旧数据未回填，
    # 下次扫描自动补齐，期间 MediaSource 省略 ETag（纯缓存语义，无功能损失）
    file_mtime_ns: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
        description="文件 mtime（纳秒）；NULL=未回填（下次扫描补齐）",
    )
    container: str | None = Field(default=None, description="容器格式（mkv/mp4/…）")

    # -- ffprobe 介质规格（探测失败保持 NULL）-------------------------------
    resolution: str | None = Field(default=None, description="归一化分辨率：2160p/1080p/…")
    video_codec: str | None = Field(default=None, description="视频编码：hevc/h264/av1/…")
    hdr: str | None = Field(default=None, description="HDR 格式：HDR10/HLG/…；SDR 为 NULL")
    bit_depth: int | None = Field(default=None, description="位深：8/10/12")
    duration_seconds: int | None = Field(default=None, description="时长（秒）")
    bit_rate: int | None = Field(default=None, description="总码率（bps）")
    frame_rate: float | None = Field(default=None, description="视频帧率（fps）")
    color_space: str | None = Field(
        default=None,
        description="用户可识别的色彩空间：BT.2020/BT.709/Display P3/…",
    )
    # 音轨/内封字幕轨（ffprobe 全量流信息，条目详情页展示）。
    # 三态：NULL=未探测（旧数据/探测失败，详情页按需补探），[]=探测过但没有该类流。
    # 元素结构见 media_probe 的 _audio_stream_info / _subtitle_stream_info
    audio_streams: list | None = Field(
        default=None,
        sa_column=Column(_NullableJson, nullable=True),
        description="音轨列表 JSON；NULL=未探测",
    )
    subtitle_streams: list | None = Field(
        default=None,
        sa_column=Column(_NullableJson, nullable=True),
        description="内封字幕轨列表 JSON；NULL=未探测",
    )
    # 外挂字幕台账（docs/design/jellyfin-subtitle.md §2.3）。与内封
    # subtitle_streams 平行分列：两者数据来源与失效键完全不同（内封=视频
    # 本体 ffprobe，外挂=同目录 sidecar 文件集），分列各自刷新互不牵连。
    # 三态：NULL=未发现过（旧行，重扫回填），[]=发现过但没有。
    # 元素结构见 library/subtitles.py 的 discover_external_subtitles
    external_subtitles: list | None = Field(
        default=None,
        sa_column=Column(_NullableJson, nullable=True),
        description="外挂字幕清单 JSON；NULL=未发现过",
    )

    # -- 发布信息（来自文件名解析，enrich 复用）------------------------------
    media_source: str | None = Field(default=None, description="片源：WEB-DL/Blu-ray/…")
    # 人工标注保护位（docs/design/media-source-annotation.md §3）：True 表示
    # media_source 是用户手工判定的（含「按最低档」哨兵 user-lowest），自动
    # 名称解析不得覆盖——upsert 保留人工值、重复行合并时人工值优先。
    media_source_manual: bool = Field(
        default=False, description="片源为人工标注；自动解析不得覆盖"
    )
    release_group: str | None = Field(default=None, description="发布组")

    # -- 来源与追溯 ----------------------------------------------------------
    source: str = Field(index=True, description="imported（入库管线）/ scanned（存量扫描）")
    site_id: str | None = Field(default=None, description="入库来源站点；scanned 为 NULL")
    torrent_id: str | None = Field(default=None, description="入库来源种子；scanned 为 NULL")
    # 同一次监听入库或存量扫描新发现的文件共享批次号。「最近添加」据此只展示
    # 让条目本次置顶的季集变化，而不是把条目名下全部历史库存误写成新增内容。
    # NULL 是迁移前旧台账；旧应用回退后新增的行也自然落 NULL，由界面退回时间。
    added_batch_id: str | None = Field(
        default=None,
        description="首次入账批次；NULL=旧数据或旧版本写入",
    )

    # -- 生命周期（docs/design/library-file-recycle.md）----------------------
    # state 是唯一判别式；missing_since / trashed_at 是状态附属时间戳，
    # 只在对应状态下有意义。file_path 恒为**当前物理位置**（全站不变量：
    # 缺失检测、ETag、播放都依赖它）——移入回收站时 file_path 更新为回收站
    # 内路径，原路径存 trash_original_path 供恢复。
    state: str = Field(
        default=FileState.IN_PLACE,
        index=True,
        description="生命周期：in_place 在位 / missing 缺失 / trashed 待回收",
    )
    missing_since: datetime | None = Field(
        default=None, description="对账发现文件消失的时间；仅 state=missing 时有意义"
    )
    trashed_at: datetime | None = Field(
        default=None, description="进入待回收的时间；仅 state=trashed 时有意义"
    )
    # NULL 且 state=trashed = 做种保护形态：文件仍在原位（file_path 未变）
    trash_original_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="移入回收站前的原路径（恢复用）；NULL=文件未被移动（原地待回收）",
    )
    purge_after: datetime | None = Field(
        default=None, description="预计自动删除时间；NULL=不自动删（做种保护/等联动）"
    )
    trash_context: dict | None = Field(
        default=None,
        sa_column=Column(_NullableJson, nullable=True),
        description="待回收审计快照：{reason, trigger:{kind,id,label}, note}",
    )

    def mark_missing(self, since: datetime | None = None) -> None:
        """对账标缺失：只作用于在位行——待回收行的文件去留由清理任务收敛，
        缺失检测不得覆盖待回收状态（复活防线）。"""
        from movieclaw_db.models.base import utcnow

        if self.state == FileState.IN_PLACE:
            self.state = FileState.MISSING
            self.missing_since = since or utcnow()

    def revive(self) -> None:
        """对账发现文件回来了：只把缺失行复位为在位——待回收行保持待回收
        （扫描按路径命中它时只更新探测属性，不复活状态）。"""
        if self.state == FileState.MISSING:
            self.state = FileState.IN_PLACE
            self.missing_since = None

    # 用户在待识别清单点过「忽略」的时间；NULL=没忽略过。
    # 语义是**永久**的"别再问我这个文件"：花絮/预告/自录内容在 TMDB 本就
    # 没有对应条目，永远认不出来。带标记的行扫描时直接秒过（不重走识别链、
    # 不进清单、不计入待识别统计），行本身保留——「已忽略」清单里可一键恢复
    ignored_at: datetime | None = Field(
        default=None, index=True, description="用户忽略该文件的时间；NULL=未忽略"
    )

    # 待识别的原因（扫描识别链失败时记录，给用户"为什么认不出"的解释，
    # 如 TMDB 无法访问 / 解析不出片名）；已识别或人工认领后为 NULL
    unidentified_reason: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="识别失败原因（给人看的整句）；NULL=已识别或未记录",
    )
    # 失败分类（UnidentifiedCode）：清单据此给标签措辞/配色/可用动作，
    # 与上面那句面向用户的文案解耦——文案改写不该动摇分类
    unidentified_code: str | None = Field(
        default=None, index=True, description="识别失败分类；NULL=已识别或旧数据"
    )
    # 收敛放弃时 TMDB 那边的候选（[{tmdb_id,title,year,episode_count,reasons}]）：
    # 机器判不了的歧义（如《风筝》正片 46 集 vs 送审版 51 集）人一眼就能认，
    # 待识别清单直接列出来点选，省得用户自己去 TMDB 查 id 手输
    unidentified_candidates: list | None = Field(
        default=None,
        sa_column=Column(_NullableJson, nullable=True),
        description="待识别文件的 TMDB 候选清单；NULL=没有候选可选",
    )

    # -- 身份对账（识别器升级后的存量复核）----------------------------------
    identity_source: str | None = Field(
        default=None,
        description="身份来源（IdentitySource）；NULL=特性上线前的旧数据",
    )
    resolved_version: int | None = Field(
        default=None,
        description="识别时的识别器版本；落后于 RESOLVER_VERSION 且非人工则待复核",
    )
    # 复核发现新旧结论不一致时的建议（{media_item_id,tmdb_id,title,year,poster_path}）：
    # 不直接改身份——宁可待确认，不静默翻案；用户在复核清单里拍板
    review_suggestion: dict | None = Field(
        default=None,
        sa_column=Column(_NullableJson, nullable=True),
        description="身份复核建议；NULL=无待复核建议",
    )
