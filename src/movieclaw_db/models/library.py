from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class Library(TimestampMixin, table=True):
    """媒体库——"我拥有哪些影视内容、放在哪里"的权威定义（docs/design/library.md）。

    L1 阶段的最小形态：库只是"类型 + 落盘根路径"的命名实体，职责是给
    订阅与手动下载提供**入库目标**（save_path 由主根推导）。入库管线的
    transfer_sources、扫描统计等字段随 L2/L3 的消费实现同期加列——
    不预留"存而不用"的配置（moviebot 稻草人配置的教训，见设计文档 1 节）。

    约定：
    - 每库单一类型（movie/tv），命名规范与订阅联通都按类型走；
    - ``root_paths`` 是字符串数组，**第一个为主根**（新入库落主根，
      其余为扩展根，供 L3 盘点对账）——库只有这一套目录体系，对目录的
      用途不做任何假设（它可以同时是下载目录，扫描的完整性检测兜底）；
      "把外部内容搬进库"是独立模块「监听导入」（import_watch）的职责；
    - 每 kind 至多一个默认库（``is_default``），订阅/手动下载不选库时用它。
      不变量由 Repository 维护：同 kind 第一个库自动成为默认；删除默认库时
      默认让给同 kind 剩下最早创建的一个；
    - ``match_rules`` 是库的**收藏范围声明**（docs/design/library-routing.md）：
      条件列表，路由（library_routing.route）据此在同 kind 的库里自动选目标。
      空列表 = 未声明——不参与自动命中，只作为显式指定或默认库兜底的目标。
    """

    __tablename__ = "library"

    id: int | None = Field(default=None, primary_key=True)
    # 展示名（如"电影库"/"剧集库"/"动漫库"），全局唯一
    name: str = Field(index=True, unique=True, description="库的展示名")
    # movie / tv——创建后不可改（订阅按 kind 挂库，改类型会让既有关联失义）
    kind: str = Field(index=True, description="媒体类型：movie / tv")
    # 根路径数组，第一个为主根；路径指 movieclaw 视角的绝对路径
    root_paths: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="根路径列表（绝对路径，第一个为主根）",
    )
    # 每 kind 至多一个默认库
    is_default: bool = Field(default=False, description="是否为该类型的默认库")
    # 展示顺序（越小越靠前），决定媒体库首页卡片区与「最近添加」分区的排列。
    # 新库置尾（max+1），用户在库卡片菜单里前移/后移调整；同值按 id 兜底
    sort_order: int = Field(default=0, index=True, description="展示顺序（升序）")
    # 收藏范围声明：条件列表，条件间 AND、条件内 any_of（交集即满足）。
    # 每条形如 {"field": "genres", "op": "any_of", "values": [16]}——
    # genres 存 TMDB genre **ID**（genre 名随刮削语言变化，存名字会在用户
    # 切换 tmdb_language 后静默失效）；origin_countries 存 ISO 3166-1 国家码。
    # 通用条件结构是有意为之：后续加字段（导演/公司/系列）零迁移零引擎改动
    match_rules: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="收藏范围条件列表；空=未声明（只作兜底目标）",
    )
    # 库级刮削偏好覆盖（docs/design/scrape-customization.md §1）：JSON 对象，
    # **只存显式覆盖的字段**，空/NULL = 全跟全局设置。动漫库要日文原名海报、
    # 电影库与剧集库各用一套命名模板，这类"按库口味"全局一套设置盖不住。
    # 只允许覆盖设计文档标注〔可库级〕的字段（选图、命名、目录写入细项）——
    # 语言不可库级：同一条目跨库共享一份 media_metadata，按库切语言会让两个
    # 库抢写同一行。合并读取见 services/scrape_config.merge_for_library
    scrape_overrides: dict | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="库级刮削偏好覆盖；NULL/空对象 = 全跟全局设置",
    )
    # 刮削成果镜像写入媒体目录（poster.jpg/fanart.jpg/分集 thumb + 完整 NFO，
    # Kodi/Emby 规范，只增不覆盖不删除——docs/design/metadata.md 6.2）。
    # 默认开：无破坏性且反哺播放器生态；不想污染目录的用户按库关闭
    write_media_assets: bool = Field(default=True, description="刮削图片/NFO 是否写入媒体目录")
    # 扫描结束后自动清理已确认丢失的库存记录（默认**关**）。
    # 关闭时台账保留 missing 行——它不是垃圾数据，而是三件事的输入：缺失清单
    # 的「重新下载」（把缺失单元交回订阅管线）、跨轮次改名归并的候选池（尺寸
    # 指纹匹配靠它，删了再出现就是新文件、人工认领的身份锚会丢），以及行上
    # 不可再生的介质规格与来源种子。开启则是另一种诉求：用户自己在管磁盘，
    # 删掉的文件就该从台账上消失，不想每次扫描后再手动清一遍。
    # 清理只在**本轮扫描可信**时执行（见 scan._auto_clear_missing），且只删
    # 台账、绝不动磁盘
    auto_clear_missing: bool = Field(
        default=False, description="扫描后自动清理已确认丢失的库存记录（不可恢复）"
    )
    # 实时监控（watchdog 文件事件 → 增量扫描）的库级开关，默认**开**。
    # 关闭动机是网络挂载（SMB/CIFS/NFS）：inotify 收不到远端变更、递归建
    # watch 还要对整棵目录树逐目录往返（issue #162 的启动拖死即由此而来），
    # 纯付成本无收益。关闭后该库不建监听、没有事件驱动的扫描；定期对账与
    # 手动扫描照常，新文件仍会被发现，只是不实时——与 Emby/Plex 的
    # real-time monitoring 开关同一语义
    realtime_watch: bool = Field(
        default=True, description="是否启用实时文件监控（关闭后靠定期对账与手动扫描）"
    )

    # —— 库存统计快照 ----------------------------------------------------
    # 媒体库首页是高频读路径，不能每次打开都扫描 library_file 全表再聚合。
    # 这些派生值随扫描、监听入库、转移、删除和人工认领等台账变更在事务收尾
    # 时统一重算；列表接口因此只读 library 的少量行，查询成本与文件数无关。
    # item/file/size 只统计在位文件（missing_since IS NULL）：“占用空间”必须
    # 反映当前磁盘内容；历史缺失记录另由 stats_missing_count 单独表达。
    stats_item_count: int = Field(default=0, description="在位且已识别的媒体条目数")
    # Jellyfin 库卡片与 /Items/Counts 还需要剧集的分集总数；与
    # 作品数一同预计算，避免兼容接口另外扫描整张 library_file。
    stats_episode_count: int = Field(default=0, description="在位且已识别的剧集分集数")
    stats_file_count: int = Field(default=0, description="在位文件数（含待识别）")
    stats_total_size_bytes: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, default=0),
        description="在位文件总大小（字节）",
    )
    stats_unidentified_count: int = Field(
        default=0, description="在位待识别文件数（不含已忽略）"
    )
    stats_missing_count: int = Field(default=0, description="标记 missing 的历史文件数")
    stats_ignored_count: int = Field(default=0, description="在位且已忽略的文件数")
    stats_refreshed_at: datetime | None = Field(
        default=None, description="库存统计最近一次重算时间；NULL=尚未扫描"
    )

    @property
    def primary_root(self) -> str | None:
        """主根路径（新入库的落点）；未配置任何根时为 None。"""
        return self.root_paths[0] if self.root_paths else None
