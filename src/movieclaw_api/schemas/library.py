"""媒体库接口的请求/响应模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_serializer

from movieclaw_api.schemas.base import BaseModel
from movieclaw_db.models.library import Library
from movieclaw_media.models import MediaKind


class LibraryPayload(BaseModel):
    """创建/更新库的请求体。kind 仅创建时生效，更新时忽略（创建后不可改）。"""

    name: str = Field(description="库的展示名（全局唯一）")
    kind: MediaKind = Field(description="媒体类型：movie / tv")
    root_paths: list[str] = Field(
        description="根路径列表（绝对路径），第一个为主根——新入库落在这里"
    )
    match_rules: list[dict] = Field(
        default_factory=list,
        description=(
            "收藏范围条件（条件间 AND、条件内任一匹配）；"
            "genres 存 TMDB 类型 ID，origin_countries 存国家码；空=未声明"
        ),
    )
    # 不传 = 不改动（更新时保持原值，创建时为关）。这是个不可恢复的操作开关，
    # 不能因为某个客户端没带这个字段就被静默改掉
    auto_clear_missing: bool | None = Field(
        default=None,
        description=(
            "扫描后自动清理已确认丢失的库存记录（只删台账不动磁盘，不可恢复）；"
            "不传表示不改动，新建时默认关闭"
        ),
    )
    # 同为"不传 = 不改动"：老客户端的请求体没带该字段，不能把用户关掉的
    # 监控悄悄打开（或反过来）
    scrape_overrides: dict | None = Field(
        default=None,
        description=(
            "库级刮削偏好覆盖（语言/选图/命名/目录写入，即「刮削与整理」的全部字段）；"
            "不传=不改动，空对象=清空覆盖回到全跟全局。"
            "语言与选图这类产物挂全局条目的设置，按条目的**刮削归属库**生效"
        ),
    )
    realtime_watch: bool | None = Field(
        default=None,
        description=(
            "是否启用实时文件监控（关闭后该库不建 watchdog 监听，"
            "靠定期对账与手动扫描发现新文件——SMB/NFS 网络挂载建议关闭）；"
            "不传表示不改动，新建时默认开启"
        ),
    )


class LibraryReorderPayload(BaseModel):
    """媒体库重排的请求体：必须一次给全所有库的 id（漏/多/重复都拒绝）。"""

    ordered_ids: list[int] = Field(description="全部媒体库 id 的目标顺序（越靠前展示越靠前）")


class LibraryStats(BaseModel):
    """库存统计快照（台账变化时重算，查询时直接读取 library 表）。"""

    item_count: int = Field(default=0, description="在位且已识别的媒体条目数")
    file_count: int = Field(default=0, description="在位文件总数（含待识别）")
    total_size_bytes: int = Field(default=0, description="在位文件总大小（字节）")
    unidentified_count: int = Field(
        default=0, description="在位待识别文件数（不含已忽略）"
    )
    missing_count: int = Field(default=0, description="标记 missing 的文件数（缺失清单入口）")
    ignored_count: int = Field(
        default=0,
        description="在位且被用户忽略的文件数（不再参与识别，可在已忽略清单恢复）",
    )


class LastScanView(BaseModel):
    """最近一次扫描的结论——扫描常毫秒级结束，前端靠它给用户"点了有反应"的反馈。"""

    finished_at: datetime
    scanned: int = Field(description="本轮新入账文件数")
    identified: int
    unidentified: int
    marked_missing: int = Field(description="本轮标记丢失的文件数")
    cleared_missing: int = Field(
        default=0, description="本轮自动清理出台账的丢失记录数（库开了自动清理才非 0）"
    )
    removed_root_marked_missing: int = Field(
        default=0, description="本轮因已移除根路径而标记缺失的旧台账数"
    )
    removed_root_cleared: int = Field(
        default=0, description="本轮因已移除根路径而自动清理的旧台账数"
    )
    removed_root_conflicts: int = Field(
        default=0, description="本轮已移除根路径台账的身份冲突数（需人工处理）"
    )
    deferred: int = Field(default=0, description="疑似写入中暂缓入账的文件数（稍后自动补扫）")
    retried: int = Field(
        default=0, description="识别重试数：在位但待识别的文件重走识别链（不算新入账）"
    )
    cancelled: bool = Field(default=False, description="本轮扫描被用户手动停止（未扫完）")
    errors: list[str] = Field(default_factory=list)

    @field_serializer("finished_at")
    def _serialize_utc(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()


class ScanProgressView(BaseModel):
    """进行中扫描/整理的实时进度（前端在库封面上画进度环，两种任务共用）。

    ``phase`` 是必填的：一次"扫描"内部分好几段（盘点 → 逐文件入账 →
    补齐图片资产），分子分母各段各算。前端**必须**按阶段选文案，否则
    进度走完文件数后还要跑几分钟资产，界面就会僵在"已处理 = 总数"上
    对用户撒谎（见 library_scan.ScanPhase）。
    """

    phase: str = Field(description="进行中的阶段，取值见 library_scan.ScanPhase / organizing")
    processed: int
    total: int = Field(description="总数；0 表示分母未知，前端画不确定态转圈")


class LastOrganizeView(BaseModel):
    """最近一次整理的结论——给用户"整理完成了什么"的反馈。"""

    finished_at: datetime
    renamed: int = Field(description="改名归位的主文件数")
    sidecars_renamed: int = Field(description="跟随改名的附属文件数（字幕、分集剧照等）")
    entry_assets_moved: int = Field(
        default=0, description="跟随条目目录改名的镜像资产数（海报/背景/季海报/条目 NFO）"
    )
    already_ok: int = Field(description="本就符合规范、无需动作的文件数")
    skipped: int = Field(description="计划阶段跳过的文件数（原因见预览）")
    removed_dirs: int = Field(description="搬空后清理掉的目录数")
    errors: list[str] = Field(default_factory=list)

    @field_serializer("finished_at")
    def _serialize_utc(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()


class RefreshActiveView(BaseModel):
    """整库刷新中正在处理的一部片（并发若干路，故是列表）。"""

    media_item_id: int
    title: str
    phase: str = Field(description="当前阶段：拉取 TMDB 档案 / 写入元数据 / 下载图片 / …")


class MetadataRefreshView(BaseModel):
    """整库刷新的实时状态——全量重刷很慢，用户要看到"到哪部了、在做什么"。

    随库列表一并返回（见 LibraryView.metadata_refresh），媒体库首页的库卡片
    因此不必额外请求就能显示刷新进度；单库页另有专用端点做 2 秒级的阶段
    刷新（首页 10 秒一轮的节奏跟不上阶段变化）。
    """

    refreshing: bool
    processed: int = Field(default=0, description="已完成条目数（含失败）")
    total: int = 0
    failed: int = Field(default=0, description="刮削失败的条目数（多为 TMDB 不可达）")
    stopping: bool = Field(default=False, description="已请求停止，正在收尾")
    active: list[RefreshActiveView] = Field(
        default_factory=list, description="正在处理的条目及其阶段"
    )


class LibraryView(BaseModel):
    id: int
    name: str
    kind: MediaKind
    root_paths: list[str]
    primary_root: str | None = Field(description="主根路径（root_paths 第一项）")
    is_default: bool
    match_rules: list[dict] = Field(default_factory=list, description="收藏范围条件")
    auto_clear_missing: bool = Field(
        default=False, description="扫描后自动清理已确认丢失的库存记录"
    )
    realtime_watch: bool = Field(default=True, description="是否启用实时文件监控")
    scrape_overrides: dict = Field(
        default_factory=dict, description="库级刮削偏好覆盖；空对象 = 全跟全局设置"
    )
    stats: LibraryStats = Field(default_factory=LibraryStats)
    scanning: bool = Field(default=False, description="是否正在扫描")
    scan_progress: ScanProgressView | None = Field(default=None, description="扫描实时进度")
    last_scan: LastScanView | None = Field(default=None, description="最近一次扫描结论")
    organizing: bool = Field(default=False, description="是否正在整理文件名")
    organize_progress: ScanProgressView | None = Field(
        default=None, description="整理实时进度（与扫描进度同构）"
    )
    last_organize: LastOrganizeView | None = Field(default=None, description="最近一次整理结论")
    metadata_refresh: MetadataRefreshView | None = Field(
        default=None, description="整库元数据刷新状态；没在刷为 null"
    )
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()

    @classmethod
    def from_model(
        cls,
        row: Library,
        *,
        scanning: bool = False,
        scan_progress: ScanProgressView | None = None,
        last_scan: LastScanView | None = None,
        organizing: bool = False,
        organize_progress: ScanProgressView | None = None,
        last_organize: LastOrganizeView | None = None,
        metadata_refresh: MetadataRefreshView | None = None,
    ) -> LibraryView:
        return cls(
            id=row.id,  # type: ignore[arg-type]  # 落库后必有主键
            name=row.name,
            kind=MediaKind(row.kind),
            root_paths=list(row.root_paths),
            primary_root=row.primary_root,
            is_default=row.is_default,
            match_rules=list(row.match_rules),
            auto_clear_missing=row.auto_clear_missing,
            realtime_watch=row.realtime_watch,
            scrape_overrides=dict(row.scrape_overrides or {}),
            stats=LibraryStats(
                item_count=row.stats_item_count,
                file_count=row.stats_file_count,
                total_size_bytes=row.stats_total_size_bytes,
                unidentified_count=row.stats_unidentified_count,
                missing_count=row.stats_missing_count,
                ignored_count=row.stats_ignored_count,
            ),
            scanning=scanning,
            scan_progress=scan_progress,
            last_scan=last_scan,
            organizing=organizing,
            organize_progress=organize_progress,
            last_organize=last_organize,
            metadata_refresh=metadata_refresh,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# TMDB status 原值 → 播出状态两分类（剧集海报悬浮操作的判断依据）。
# 映射外的未知值不猜——返回 None，前端降级为静态「已入库」标识，
# 不给出错误的「订阅追新 / 补齐缺集」入口。
_AIRING_STATUSES = frozenset({"Returning Series", "In Production", "Planned", "Pilot"})
_ENDED_STATUSES = frozenset({"Ended", "Canceled"})


def derive_air_status(status: str | None) -> Literal["airing", "ended"] | None:
    """剧集 TMDB status 原值收敛为 airing（还会有新集）/ ended（不会再有）。"""
    if status in _AIRING_STATUSES:
        return "airing"
    if status in _ENDED_STATUSES:
        return "ended"
    return None


class LibraryIndexEntryView(BaseModel):
    """海报墙 A-Z 索引条的一档（按标题排序下的首字母分组）。"""

    initial: str = Field(description="首字母档：A-Z，落不进的（数字/符号/假名等）为 #")
    count: int = Field(description="该档的条目数")
    offset: int = Field(
        description="该档第一格在按标题排序中的位置——即 /items?sort=title&offset= 的取值"
    )


class LibraryRecentAdditionView(BaseModel):
    """让条目进入「最近添加」的最后一批剧集的紧凑摘要。"""

    season_count: int
    episode_count: int
    season_number: int | None = Field(description="仅涉及一季时的季号；跨季为 NULL")
    first_episode_number: int | None = Field(description="同季连续批次的起始集；否则 NULL")
    last_episode_number: int | None = Field(description="同季连续批次的结束集；否则 NULL")
    complete_season: bool = Field(description="本批是否完整覆盖该季 TMDB 已知集数")


class LibraryInventorySummaryView(BaseModel):
    """剧集库海报 hover 的在位库存完整度摘要。"""

    season_count: int = Field(description="在位正季数；仅特别篇时为 0")
    episode_count: int = Field(description="摘要覆盖的在位去重集数")
    season_number: int | None = Field(
        description="只覆盖一季时的季号（0=特别篇）；多季为 NULL"
    )
    total_episode_count: int | None = Field(
        description="摘要所覆盖季的 TMDB 已知总集数；任一季未知时为 NULL"
    )
    all_seasons_owned: bool = Field(description="是否覆盖 TMDB 已知的全部正季")
    all_episodes_owned: bool = Field(description="摘要所覆盖的每一季是否都已收齐")


class LibraryItemView(BaseModel):
    """库内一个媒体条目的库存聚合（单库海报墙的一格）。"""

    media_item_id: int
    kind: MediaKind
    tmdb_id: int
    title: str
    year: int | None
    poster_url: str | None
    file_count: int
    total_size_bytes: int
    # 在库的季号列表（电影为空）；集数 = 去重的 (季,集) 单元数
    seasons: list[int]
    episode_count: int
    # 去重的介质规格标签（如 ["2160p","1080p"]），探测不到为空
    resolutions: list[str]
    missing_count: int = Field(description="标记 missing 的文件数（>0 时前端提示）")
    # 剧集海报悬浮操作的两个判断依据（前端三分支：在播→订阅追新 /
    # 完结缺集→补齐缺集 / 完结齐全或电影→已入库）
    air_status: Literal["airing", "ended"] | None = Field(
        default=None,
        description="剧集播出状态：airing=在播 / ended=完结；电影或状态未知为 NULL",
    )
    missing_episode_count: int = Field(
        default=0,
        description="已播出但所有媒体库里都没有的正季集数（电影恒 0）——「补齐缺集」的依据",
    )
    added_at: datetime | None = Field(
        default=None, description="最近一次文件入账时间（首页「最近添加」排序依据）"
    )
    recent_addition: LibraryRecentAdditionView | None = Field(
        default=None,
        description="最近一次可追溯入库批次的剧集摘要；NULL=电影或迁移前旧台账",
    )
    inventory_summary: LibraryInventorySummaryView | None = Field(
        default=None,
        description="本库在位剧集相对 TMDB 季集结构的完整度摘要；电影或无有效集号为 NULL",
    )
    probe_pending_count: int = Field(
        default=0,
        description="在位但尚未探出介质规格的文件数——扫描补探阶段前端据此"
        "把「还在处理」的条目排到海报墙前面并点亮标记",
    )

    @field_serializer("added_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()


class LibrarySearchGroupView(BaseModel):
    """媒体库搜索结果的一组：一个库内命中关键词的条目（组内按标题拼音排序）。"""

    library_id: int
    library_name: str
    kind: MediaKind
    items: list[LibraryItemView]


class AudioStreamView(BaseModel):
    """一条音轨（ffprobe 探测；字段 None=该项探不出）。"""

    codec: str | None = None
    profile: str | None = Field(
        default=None, description="编码档次（如 DTS-HD MA），比 codec 更贴近用户认知"
    )
    channels: int | None = None
    channel_layout: str | None = Field(default=None, description="声道布局（如 5.1(side)）")
    language: str | None = Field(default=None, description="语言标签（ISO 639，如 chi/eng）")
    title: str | None = None
    default: bool = False


class SubtitleStreamView(BaseModel):
    """一条字幕：内封轨（ffprobe）或外挂文件（目录发现）。"""

    codec: str | None = Field(
        default=None, description="内封轨编码（subrip/ass/pgs…）；外挂为文件扩展名"
    )
    language: str | None = None
    title: str | None = None
    forced: bool = False
    default: bool = False
    external: bool = Field(default=False, description="是否外挂字幕文件")
    file_name: str | None = Field(default=None, description="外挂字幕的文件名")


class SubtitleCueView(BaseModel):
    """字幕预览中的一条对白；时间统一使用毫秒，前端只负责格式化。"""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str


class SubtitlePreviewView(BaseModel):
    """详情页字幕预览：格式元数据 + 已去样式的时间轴对白。"""

    track: str = Field(description="本次预览的中性轨引用")
    format: str | None = None
    event_count: int = Field(ge=0)
    cues: list[SubtitleCueView] = Field(default_factory=list)


class SubtitleDeleteResultView(BaseModel):
    """删除一个外挂字幕文件后的回执（路径回显，便于用户核对删的是哪一个）。"""

    path: str = Field(description="已删除的字幕文件完整路径")
    freed_bytes: int = Field(ge=0, description="释放的磁盘空间")


class LibraryFileView(BaseModel):
    """条目详情页的一个物理文件（一个版本 / 一集）。"""

    id: int
    file_path: str
    file_name: str
    size_bytes: int
    container: str | None
    resolution: str | None
    video_codec: str | None
    hdr: str | None
    bit_depth: int | None
    duration_seconds: int | None
    bit_rate: int | None
    frame_rate: float | None
    color_space: str | None
    media_source: str | None
    media_source_manual: bool = Field(
        default=False, description="片源为人工标注（含 user-lowest 哨兵）"
    )
    release_group: str | None
    source: str = Field(description="imported（入库管线）/ scanned（存量扫描）")
    season_number: int
    episode_number: int
    missing: bool = Field(description="文件当前不在磁盘（missing 标记）")
    # -- 生命周期第三态（docs/design/library-file-recycle.md §7）--------------
    state: str = Field(
        default="in_place",
        description="生命周期：in_place 在位 / missing 缺失 / trashed 待回收",
    )
    purge_after: datetime | None = Field(
        default=None,
        description="待回收的预计自动清理时间；null 且 trashed = 做种保护，不自动删",
    )
    trash_note: str | None = Field(
        default=None,
        description="待回收原因（中文整句，含触发方），文件区直接展示",
    )
    audio_streams: list[AudioStreamView] | None = Field(
        default=None, description="音轨列表；null=尚未探测（ffprobe 缺失或文件不可达）"
    )
    subtitle_streams: list[SubtitleStreamView] = Field(
        default_factory=list, description="字幕列表：内封轨 + 外挂文件"
    )
    added_at: datetime

    @field_serializer("added_at")
    def _serialize_utc(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()

    @field_serializer("purge_after")
    def _serialize_purge_after(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()


class ActorView(BaseModel):
    """本地刮削（NFO）的一位演员。"""

    name: str
    role: str | None = None
    thumb_url: str | None = Field(default=None, description="头像地址（NFO 里的图床 URL）")
    tmdb_person_id: int | None = Field(
        default=None, description="TMDB 影人 ID；有值时前端把这一格链到人物页"
    )


class DirectorView(BaseModel):
    """库内人物关系表中的一位导演。"""

    name: str
    thumb_url: str | None = Field(default=None, description="头像地址（TMDB 图床 URL）")
    tmdb_person_id: int = Field(description="TMDB 影人 ID；用于人物页链接")


class LocalMetaView(BaseModel):
    """条目的展示元数据：本地 NFO > 库内刮削档案 > TMDB 实时兜底；
    三个来源都拉不到时整体为 null（docs/design/metadata.md 第 5 节）。"""

    plot: str | None = None
    rating: float | None = None
    runtime_minutes: int | None = None
    genres: list[str] = Field(default_factory=list)
    directors: list[str] = Field(default_factory=list)
    director_credits: list[DirectorView] = Field(
        default_factory=list,
        description="从 person 关系表读取的结构化导演；空列表时前端回退 directors 姓名",
    )
    actors: list[ActorView] = Field(default_factory=list)
    nfo_name: str = Field(default="", description="来源 NFO 文件名（source=nfo 时给出）")
    source: Literal["nfo", "db", "tmdb"] = Field(
        default="nfo", description="信息出处：nfo=本地刮削 / db=库内档案 / tmdb=实时兜底"
    )


class LibraryItemDetailView(BaseModel):
    """条目详情页的完整数据：基本信息 + 本地刮削元数据 + 逐文件真实规格。

    图片优先级：条目目录里的本地美术图（poster.jpg/fanart.jpg，走
    /libraries/.../artwork 接口的相对路径）优先，其次 TMDB 图床绝对地址
    ——前端按"是否 http 开头"区分两种加载方式。
    """

    media_item_id: int
    kind: MediaKind
    tmdb_id: int
    imdb_id: str | None
    douban_id: str | None
    title: str
    original_title: str
    year: int | None
    poster_url: str | None
    backdrop_url: str | None
    local_meta: LocalMetaView | None = Field(
        default=None, description="NFO 本地刮削元数据；目录里没有可用 NFO 时为 null"
    )
    entry_dirs: list[str] = Field(
        default_factory=list, description="条目在磁盘上的目录（删除确认时展示）"
    )
    files: list[LibraryFileView]
    file_count: int
    total_size_bytes: int
    # 剧集分集区的季选择器数据：库内实有 ∪ 元数据季集结构（电影恒空）。
    # 特别季 0 只在库里真有文件时出现——元数据里的特别季默认不参与展示
    seasons: list[int] = Field(default_factory=list, description="季号列表（电影为空）")
    # 刮削是后台长任务（TMDB + 图片下载），状态放服务端：用户离开页面、
    # 刷新浏览器、甚至换台设备打开，都能看到"这部还在刮"并等到它结束。
    # 阶段文案与整库刷新同一套（拉取 TMDB 档案 / 写入元数据 / 下载图片 / …）
    scraping: bool = Field(default=False, description="该条目正在后台刮削元数据")
    scraping_phase: str | None = Field(default=None, description="刮削当前阶段；没在刮为 null")
    # 刮削归属库（docs/design/scrape-customization.md §14）：元数据与图片的
    # 产物挂全局条目，一条目只能有一套语言/选图口味，由归属库决定。同一条目
    # 的文件散在两个库时，这里显示的就是"哪个库说了算"——不摆出来用户无法
    # 解释"为什么这部片没跟我的动漫库设置"
    scrape_library_id: int | None = Field(
        default=None, description="刮削归属库 id；null=无归属，跟全局设置"
    )
    scrape_library_name: str | None = Field(
        default=None, description="刮削归属库名；null=无归属，跟全局设置"
    )


class EpisodeView(BaseModel):
    """剧集分集区的一集：季集结构 + 本地分集刮削 + TMDB 兜底的合并结果。"""

    episode_number: int
    name: str | None = None
    overview: str | None = Field(default=None, description="分集简介")
    air_date: str | None = None
    still_url: str | None = Field(
        default=None,
        description="分集剧照：本地缩略图接口相对路径或 TMDB 图床地址；无为 null",
    )
    owned: bool = Field(description="该集有在位文件；false=缺集或文件缺失（前端置灰）")
    file_ids: list[int] = Field(default_factory=list, description="该集的台账文件 id")
    # 当前观看者的进度：分集卡的进度条与已看对勾，视觉与数据口径都与首页
    # 「最近观看」一致（同一张 playback_state 表，Jellyfin 客户端写的也算）
    position_ms: int = Field(default=0, description="当前观看者上次看到的位置（毫秒）")
    played: bool = Field(default=False, description="当前观看者已看完该集")
    progress_percent: int | None = Field(
        default=None, description="观看进度 1~99；已看完由 played 表达，不给百分比"
    )


class SeasonEpisodesView(BaseModel):
    """一季的分集清单（分集横滚区数据源）。"""

    season_number: int
    episodes: list[EpisodeView]


class ArtworkCandidateView(BaseModel):
    """「更换图片」弹层里的一张候选（docs/design/metadata.md 6.3）。"""

    file_path: str = Field(description="TMDB 图片路径（选定时原样回传）")
    preview_url: str = Field(description="缩略预览地址（TMDB 图床，前端经代理加载）")
    width: int | None = None
    height: int | None = None
    language: str | None = Field(
        default=None, description="图上文字的语言；null=无文字（背景首选这类）"
    )
    vote_average: float | None = None
    vote_count: int | None = None


class ArtworkCandidatesView(BaseModel):
    """条目的全部候选图，按与自动选图一致的规则排序。

    ``current_*`` 是**实际在用**的图路径（前端据此标「当前」）——不能用
    "列表第一张"推断：策略升级前刮的条目、手动锁定的条目、TMDB 新增更高票
    的图，三种情况下第一张都不是在用的那张。
    """

    posters: list[ArtworkCandidateView] = Field(default_factory=list)
    backdrops: list[ArtworkCandidateView] = Field(default_factory=list)
    current_poster: str | None = Field(default=None, description="当前在用的海报路径")
    current_backdrop: str | None = Field(default=None, description="当前在用的背景路径")
    poster_locked: bool = Field(default=False, description="海报已手动选定，刷新不覆盖")
    backdrop_locked: bool = Field(default=False, description="背景已手动选定，刷新不覆盖")


class ArtworkSelectPayload(BaseModel):
    """选图请求：kind 指海报还是背景；file_path 为 null 表示恢复自动选图。"""

    kind: Literal["poster", "backdrop"] = Field(description="poster=海报 / backdrop=背景图")
    file_path: str | None = Field(
        default=None, description="TMDB 图片路径；null=解锁并恢复自动选图"
    )


class ScrapeLibraryPayload(BaseModel):
    """改条目的刮削归属库；``target_library_id=null`` 表示恢复自动（清空后重新推断）。

    字段不叫 ``library_id``：路径上已经有一个同名参数（条目所在的库），重名会
    让生成式 CLI 的两个参数互相覆盖（click 会直接告警）。
    """

    target_library_id: int | None = Field(
        default=None, description="新的刮削归属库 id；null=清空，由系统按文件/订阅重新推断"
    )


class ReidentifyResultView(BaseModel):
    """单条目重新识别的结论。"""

    total: int = Field(description="参与重识别的在位文件数")
    identified: int
    unidentified: int
    skipped_missing: int = Field(description="missing 文件数（无磁盘实体，保持原身份）")
    kept_on_error: int = Field(
        default=0, description="TMDB 网络类失败、保留原身份的文件数（修复网络后可重试）"
    )
    changed: bool = Field(description="识别结果与原身份是否不同")
    new_media_item_id: int | None = Field(
        default=None, description="全部文件收敛到的新条目；识别失败或分裂为多个条目时为 null"
    )
    new_title: str | None = None
    pinned_identity: bool = Field(
        default=False,
        description="身份由目录名 tmdbid 标记或 NFO 钉死——结果不满意需先改标记/NFO 或人工认领",
    )
    message: str = Field(description="面向用户的结论文案")


class TransferPayload(BaseModel):
    """条目转移的请求体：只需要目标库。"""

    target_library_id: int = Field(description="转移目标库 id（必须与当前库同类型）")


class TransferMoveView(BaseModel):
    """转移预览里的一个搬运单元。"""

    source_path: str
    target_path: str
    is_dir: bool = Field(description="true=整个条目目录搬走；false=只搬这一个文件")
    size_bytes: int
    file_count: int = Field(description="本单元随迁的台账行数")


class TransferSkipView(BaseModel):
    """预览里的一条跳过说明：哪个路径、为什么不搬它。"""

    file_path: str
    reason: str


class TransferPreviewView(BaseModel):
    """转移预览：完整的「将要发生什么」，用户确认后才执行。"""

    target_library_id: int
    target_library_name: str
    target_root: str = Field(description="目标库主根——条目目录会搬到这里")
    moves: list[TransferMoveView]
    skips: list[TransferSkipView] = Field(description="不参与转移的路径与中文原因")
    total_bytes: int
    missing_count: int = Field(description="缺失文件数（磁盘无实体，只随迁台账归属）")
    cross_device: bool = Field(
        description="目标与源不在同一块盘——将退化为完整复制（耗时，且断开与做种目录的硬链接）"
    )
    blocked: list[str] = Field(
        default_factory=list, description="阻断性问题（如目标已有同名目录）；非空则不给执行"
    )


class TransferStartView(BaseModel):
    """转移启动响应。"""

    started: bool
    message: str
    job_id: str = Field(description="持久化后台作业 ID，可在活动页继续观察")
    created: bool = Field(default=True, description="false 表示复用了仍在进行的同一作业")


class TransferStatusView(BaseModel):
    """转移进度 / 最近一次结论（前端弹窗轮询这一个接口收尾）。"""

    running: bool
    media_item_id: int | None = None
    title: str | None = None
    target_library_id: int | None = None
    processed: int = 0
    total: int = 0
    # 以下为最近一次转移的结论（running=false 且转移过才有）
    finished_at: datetime | None = None
    target_library_name: str | None = None
    moved_paths: list[str] = Field(default_factory=list)
    files_relocated: int = 0
    bytes_moved: int = 0
    removed_dirs: int = 0
    subscription_moved: bool = Field(
        default=False, description="该片的订阅是否一并改挂到目标库（后续剧集直接投新库）"
    )
    errors: list[str] = Field(default_factory=list)

    @field_serializer("finished_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        # 后端存的是 naive UTC，补上 +00:00 前端才不会按本地时区解读
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()


class ItemDeleteResultView(BaseModel):
    """条目真实删除的结论。"""

    removed_paths: list[str] = Field(description="实际从磁盘删除的目录/文件")
    rows_deleted: int
    freed_bytes: int
    errors: list[str] = Field(default_factory=list)


class UnidentifiedCandidateView(BaseModel):
    """收敛器判不了时留下的一个候选（用户点一下即可认领）。"""

    tmdb_id: int
    title: str
    year: int | None = None
    episode_count: int | None = Field(
        default=None, description="该候选对应季的集数——同名双版本靠它一眼区分"
    )
    reasons: list[str] = Field(
        default_factory=list, description="本地证据对它的佐证（年份相同/本地 N 集吻合…）"
    )


class UnidentifiedFileView(BaseModel):
    """待识别清单的一行。"""

    id: int
    library_id: int
    library_name: str
    file_path: str
    size_bytes: int
    season_number: int
    episode_number: int
    reason: str | None = Field(
        default=None, description="识别失败原因整句（展开/悬停查看；清单上只显示标签）"
    )
    code: str | None = Field(
        default=None,
        description="失败分类：unparsable / tmdb_unreachable / ambiguous / no_match",
    )
    candidates: list[UnidentifiedCandidateView] = Field(default_factory=list)


class UnidentifiedGroupView(BaseModel):
    """待识别清单的一组：同一条目目录下的文件聚成一条。

    一部剧几十集全认不出时，逐集列出来会把清单刷爆、也让人无从下手；
    按条目目录聚合后一次认领整组（各文件沿用自己已解析的季集号）。
    """

    key: str = Field(description="分组键：条目目录绝对路径；裸文件用文件自身路径")
    label: str = Field(description="展示名：条目目录名（裸文件为文件名）")
    library_id: int
    library_name: str
    file_count: int
    total_size_bytes: int
    reason: str | None = Field(default=None, description="组内共同的识别失败原因整句")
    code: str | None = Field(default=None, description="组内共同的失败分类（决定标签与配色）")
    candidates: list[UnidentifiedCandidateView] = Field(
        default_factory=list, description="组内共同的候选：点一下整组认领"
    )
    files: list[UnidentifiedFileView]


class ClaimPayload(BaseModel):
    """人工指定文件身份：把文件关联到 Discover 返回的影视条目。"""

    title_ref: str = Field(
        min_length=1,
        max_length=160,
        description="Discover 返回的 TMDB 影视条目稳定引用，如 tmdb:tv:1396",
    )
    season_number: int = Field(default=0, description="季号；电影固定 0")
    episode_number: int = Field(default=0, description="集号；电影固定 0")


class ClaimBatchPayload(BaseModel):
    """整组认领：一次把多个待识别文件挂到同一个 TMDB 条目。

    季集号不在这里指定——每个文件沿用扫描时已从文件名解析出的季集号，
    这正是"一部剧几十集一次认领"能成立的前提。
    """

    file_ids: list[int] = Field(
        min_length=1, description="待识别文件 id 数组（来自待识别清单接口），如 [101,102]"
    )
    title_ref: str = Field(
        min_length=1,
        max_length=160,
        description="Discover 返回的 TMDB 影视条目稳定引用，如 tmdb:tv:1396",
    )


class MediaSourceAnnotationPayload(BaseModel):
    """整季片源人工标注（docs/design/media-source-annotation.md §4）。

    值域与洗版片源档阶梯对齐；``user-lowest`` 是「不确定，按最低档处理」
    的显式哨兵（T0，会触发整季自动洗版重下）。
    """

    media_item_id: int = Field(description="媒体条目 id")
    season_number: int = Field(ge=0, description="季号；电影固定 0")
    media_source: Literal["Remux", "Blu-ray", "WEB-DL", "WEBRip", "HDTV", "user-lowest"] = (
        Field(description="标注的片源档")
    )


class MediaSourceAnnotationCandidateView(BaseModel):
    """标注弹窗预览的一行：将被标注的文件（片源未知或此前人工标注）。"""

    file_id: int
    file_name: str
    episode_number: int
    size_bytes: int
    media_source: str | None = Field(description="当前片源；null=未知")
    media_source_manual: bool = Field(description="当前值是否为此前的人工标注")


class ReviewItemView(BaseModel):
    """身份复核里的一方（现身份 / 建议身份）的条目信息。"""

    media_item_id: int
    tmdb_id: int | None = None
    title: str
    year: int | None = None
    poster_url: str | None = None


class ReviewGroupView(BaseModel):
    """身份复核清单的一组：同一条目目录下、现身份与建议都一致的文件聚成一条。

    识别器升级后扫描复核发现新旧结论不一致的行进入本清单——身份未被改动，
    由用户拍板：采纳建议（改挂新条目）或维持现状。两种拍板都转为人工身份，
    此后对账永不再打扰。
    """

    key: str = Field(description="分组键：条目目录路径 + 现身份 + 建议身份")
    label: str = Field(description="展示名：条目目录名（裸文件为文件名）")
    library_id: int
    library_name: str
    file_count: int
    total_size_bytes: int
    file_ids: list[int]
    current: ReviewItemView = Field(description="现挂身份")
    suggestion: ReviewItemView = Field(description="新识别器给出的建议身份")


class IdentityReviewDecision(StrEnum):
    """身份复核的明确决策，避免调用方猜测布尔值含义。"""

    ACCEPT_SUGGESTION = "accept_suggestion"
    KEEP_CURRENT = "keep_current"


class ReviewResolvePayload(BaseModel):
    """复核拍板：采纳识别器建议，或明确维持当前身份。

    两种拍板都把这些文件的身份来源转为 manual——用户已经看过并做了决定，
    后续识别器升级不再对它们提复核建议。
    """

    file_ids: list[int] = Field(min_length=1, description="同一复核组内的文件 id 数组")
    decision: IdentityReviewDecision = Field(
        description="accept_suggestion=采纳建议；keep_current=维持当前身份"
    )


class ReidentifyOutcomeView(BaseModel):
    """预览里一组文件的识别结论：命中某条目，或没命中（带原因与候选）。"""

    media_item_id: int | None = None
    tmdb_id: int | None = None
    title: str | None = None
    year: int | None = None
    poster_url: str | None = None
    source: str | None = Field(
        default=None, description="身份来源：path_tag=目录名标记 / nfo / resolved=名称收敛"
    )
    same_as_current: bool = Field(default=False, description="结论与条目现有身份一致")
    reason: str | None = Field(default=None, description="没命中时的中文原因")
    code: str | None = Field(default=None, description="没命中时的失败分类")
    candidates: list[UnidentifiedCandidateView] = Field(default_factory=list)


class ReidentifyGroupView(BaseModel):
    """预览的一组：识别结论相同的文件聚成一条，用户按组拍板。"""

    key: str
    outcome: ReidentifyOutcomeView
    file_ids: list[int]
    file_count: int
    total_size_bytes: int
    sample_names: list[str] = Field(default_factory=list, description="前几个文件名")


class ReidentifyPreviewView(BaseModel):
    """「修正识别结果」第一阶段：重跑识别链给出的结论，**尚未落库**。

    用户在此拍板：采纳某组结论 / 自己搜一个条目 / 把这些文件标为非独立
    作品；关掉面板则台账零改动。
    """

    current: ReviewItemView = Field(description="条目当前挂着的身份")
    movie: bool = Field(description="本库是电影库（搜索与文案按类型走）")
    groups: list[ReidentifyGroupView]
    skipped_missing: int = Field(default=0, description="missing 文件数（无磁盘实体，不参与）")
    pinned_identity: bool = Field(
        default=False, description="身份被目录名 tmdbid 标记或 NFO 钉死——改了还会被扫描改回去"
    )
    unreachable: bool = Field(default=False, description="有文件因 TMDB 不通而无结论，此刻不宜拍板")
    search_seed: str = Field(default="", description="「自己搜」的预填词（解析出的片名）")


class DetachPayload(BaseModel):
    """「这不是独立作品」：摘掉身份锚并忽略，不动磁盘。

    花絮/预告/片段被高置信错挂到别的影片时用它——用户要表达的不是"改挂
    到条目 Y"，而是"它根本不该是个条目"。可在「已忽略」里恢复。
    """

    file_ids: list[int] = Field(min_length=1, description="要摘掉身份的台账行 id 数组")


class MissingFileView(BaseModel):
    """缺失清单里的一个文件。"""

    id: int
    file_path: str
    season_number: int
    episode_number: int
    size_bytes: int


class MissingItemView(BaseModel):
    """缺失清单的一行：按媒体条目聚合（一个条目可能缺多个文件）。"""

    media_item_id: int
    kind: MediaKind
    tmdb_id: int
    title: str
    year: int | None
    poster_url: str | None
    subscription_id: int | None = Field(
        default=None, description="该条目已有订阅时给出——清理记录前提示用户（订阅可能重新下回来）"
    )
    files: list[MissingFileView]


class MissingClearPayload(BaseModel):
    """清理缺失记录（只删台账行，绝不动磁盘）。media_item_id 缺省 = 清整库。"""

    library_id: int = Field(description="所属媒体库 id")
    media_item_id: int | None = Field(
        default=None, description="只清理该条目的缺失记录；不传=清理整库"
    )


class PathReconcilePayload(BaseModel):
    """历史根路径迁移修复的范围：旧前缀与当前配置中的目标前缀。"""

    old_root: str = Field(description="已移除、需要收口的旧根路径（绝对路径）")
    new_root: str = Field(description="当前媒体库配置中的目标根路径（绝对路径）")


class PathReconcilePreviewView(BaseModel):
    """路径迁移修复预览：所有数字均只涉及数据库台账，磁盘文件永不删除。"""

    library_id: int
    old_root: str
    new_root: str
    same_path_candidates: int
    safe_merges: int
    marked_missing: int
    conflicts: list[str] = Field(default_factory=list)
    unconfirmed: list[str] = Field(default_factory=list)
    old_rows_to_delete_from_ledger: int
    disk_files_to_delete: int = Field(default=0)


class RedownloadPayload(BaseModel):
    """重新下载：把某条目的缺失单元交回订阅管线。"""

    library_id: int = Field(description="所属媒体库 id")
    media_item_id: int = Field(description="要重新下载缺失内容的条目 id")


class UnidentifiedClearPayload(BaseModel):
    """批量忽略整库的待识别文件（只打忽略标记，绝不动磁盘）。"""

    library_id: int = Field(description="要批量忽略待识别文件的媒体库 id")


class RestorePayload(BaseModel):
    """恢复已忽略的文件：清掉忽略标记，重新参与识别。"""

    file_ids: list[int] = Field(min_length=1, description="要恢复识别的已忽略文件 id 数组")


class ScanResultView(BaseModel):
    """扫描启动响应。"""

    started: bool
    message: str
    job_id: str = Field(description="持久化后台作业 id，可用于等待、取消和查看时间线")
    created: bool = Field(description="本次是否新建作业；false 表示复用同库进行中的扫描")


class OrganizeSidecarView(BaseModel):
    """跟随主文件改名的附属文件（字幕等）。"""

    source_path: str
    target_path: str


class OrganizeRenameView(BaseModel):
    """预览里的一条改名计划：旧路径 → 规范路径。"""

    file_id: int
    media_item_id: int = Field(description="所属条目——前端按条目分组展示")
    title: str
    year: int | None
    source_path: str
    target_path: str
    source_rel: str = Field(description="相对所在库根的旧路径（展示用）")
    target_rel: str = Field(description="相对所在库根的规范路径（展示用）")
    size_bytes: int
    sidecars: list[OrganizeSidecarView] = Field(default_factory=list)


class OrganizeSkipView(BaseModel):
    """预览里的一条跳过说明：哪个文件、为什么不动它。"""

    file_path: str
    reason: str


class OrganizePreviewView(BaseModel):
    """整理预览：完整的「将要发生什么」清单，用户确认后才执行。"""

    total: int = Field(description="台账在位文件总数（= 改名 + 已规范 + 跳过）")
    already_ok: int = Field(description="已符合规范命名的文件数")
    renames: list[OrganizeRenameView]
    skips: list[OrganizeSkipView]
    entry_assets: list[OrganizeSidecarView] = Field(
        default_factory=list,
        description=(
            "条目目录改名时跟着搬的镜像资产（poster.jpg / fanart.jpg / "
            "seasonNN-poster.jpg / movie.nfo / tvshow.nfo）——不搬走旧目录就清不掉"
        ),
    )


class OrganizeStartView(BaseModel):
    """整理启动响应。"""

    started: bool
    message: str
    job_id: str = Field(description="持久化后台作业 ID，可在活动页继续观察")
    created: bool = Field(default=True, description="false 表示复用了仍在进行的同一作业")
