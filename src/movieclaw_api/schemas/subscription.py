"""订阅接口的请求/响应模型。

命名沿用项目 API 惯例的 snake_case；时间字段输出前补 UTC 时区标记
（库内 naive UTC，理由见 schemas.site.ConfiguredSite）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_serializer

from movieclaw_api.schemas.base import BaseModel
from movieclaw_db.models import (
    MediaItem,
    MediaSeason,
    RuleSet,
    Subscription,
    SubscriptionActivity,
    WantedItem,
)
from movieclaw_media.library import ResolveCandidate
from movieclaw_media.models import MediaKind


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class SubscriptionTargetPreviewPayload(BaseModel):
    """订阅表单打开前的内部预览请求。

    ``title_ref`` 必须直接来自 Discover；服务端负责识别来源、解析豆瓣候选并
    建立 TMDB 锚点，Web 不再拼装 ``source/kind/external_id`` 组合。
    """

    title_ref: str = Field(
        min_length=1,
        max_length=160,
        description="Discover 返回的影视条目稳定引用",
    )


class MediaBrief(BaseModel):
    """弹层与列表共用的条目摘要。"""

    media_item_id: int
    kind: MediaKind
    tmdb_id: int
    douban_id: str | None
    title: str
    original_title: str
    year: int | None
    poster_url: str | None = Field(description="完整海报 URL（按配置的图床基址拼好）")
    status: str | None

    @classmethod
    def from_model(cls, item: MediaItem) -> MediaBrief:
        from movieclaw_api.core.config import get_settings

        poster_url = None
        if item.poster_path:
            base = get_settings().tmdb_image_base_url.rstrip("/")
            poster_url = f"{base}/w500{item.poster_path}"
        return cls(
            media_item_id=item.id,  # type: ignore[arg-type]  # 落库后必有主键
            kind=MediaKind(item.kind),
            tmdb_id=item.tmdb_id,
            douban_id=item.douban_id,
            title=item.title,
            original_title=item.original_title,
            year=item.year,
            poster_url=poster_url,
            status=item.status,
        )


class SeasonOverview(BaseModel):
    """弹层季选择器的一行：季号 + 播出进度 + 库存进度。"""

    season_number: int
    name: str
    air_date: date | None
    episode_count: int | None
    aired_count: int = Field(description="已播集数（air_date<=今天）")
    owned_count: int = Field(default=0, description="媒体库已有的集数（库存 H）")

    @classmethod
    def from_row(
        cls,
        season: MediaSeason,
        *,
        aired_count: int,
        owned_units: set[tuple[int, int]] | None = None,
    ) -> SeasonOverview:
        """``aired_count`` 由调用方从 media_episode 表统计（集数据唯一事实源）。"""
        owned = 0
        if owned_units:
            owned = sum(1 for s, _e in owned_units if s == season.season_number)
        return cls(
            season_number=season.season_number,
            name=season.name,
            air_date=season.air_date,
            episode_count=season.episode_count,
            aired_count=aired_count,
            owned_count=owned,
        )


class ResolveCandidateView(BaseModel):
    """豆瓣收敛歧义时的确认候选。"""

    tmdb_id: int
    title_ref: str = Field(description="选定候选后用于订阅的稳定引用")
    title: str
    original_title: str
    year: int | None
    poster_url: str | None

    @classmethod
    def from_model(cls, c: ResolveCandidate, *, kind: MediaKind) -> ResolveCandidateView:
        from movieclaw_api.core.config import get_settings

        poster_url = None
        if c.poster_path:
            base = get_settings().tmdb_image_base_url.rstrip("/")
            poster_url = f"{base}/w342{c.poster_path}"
        return cls(
            tmdb_id=c.tmdb_id,
            title_ref=f"tmdb:{kind.value}:{c.tmdb_id}",
            title=c.title,
            original_title=c.original_title,
            year=c.year,
            poster_url=poster_url,
        )


class PrepareView(BaseModel):
    """预检结果三态：ready 可直接渲染弹层；ambiguous 先让用户选候选；
    not_found 提示该条目暂无法订阅。"""

    status: Literal["ready", "ambiguous", "not_found"]
    media: MediaBrief | None = None
    seasons: list[SeasonOverview] = Field(default_factory=list)
    existing_subscription_id: int | None = Field(
        default=None, description="该条目已有订阅时给出，前端展示「已订阅」态"
    )
    movie_owned: bool = Field(
        default=False, description="电影：媒体库里已有本片（弹层提示，不拦订阅）"
    )
    suggested_seasons: list[int] = Field(
        default_factory=list,
        description=(
            "建议预勾选的季号。豆瓣把剧集按季拆条目，用户点进「中餐厅 第十季」"
            "要订的就是那一季；这里给出收敛通路用首播日期定案的 TMDB 季号"
            "（与豆瓣季号未必相同）。为空表示无可信结论，前端按原默认规则勾选"
        ),
    )
    candidates: list[ResolveCandidateView] = Field(default_factory=list)


class DispatchPreviewView(BaseModel):
    """投递路由预检（订阅弹窗选库时的即时提示）。

    与真实投递的三级兜底 + 映射守门同源判定，把"下载完成后能不能进库"
    这个问题在订阅那一刻就回答掉，而不是等投递失败/落点告警才暴露。
    """

    mode: Literal["watch", "inplace", "downloader_default"] = Field(
        description="投递路由：监听导入目录 / 直接下载进库 / 下载器默认目录"
    )
    path: str | None = Field(default=None, description="movieclaw 视角的投递基底目录")
    entry_dir: str | None = Field(
        default=None,
        description=(
            "条目目录的完整路径预览（按生效的命名模板渲染）。前端展示"
            "「直接下载到 …」时必须用它，不要自己拼「标题 (年份)」——"
            "命名模板可全局/按库自定义，自己拼会与真实落点不一致"
        ),
    )
    staging_path: str | None = Field(
        default=None,
        description=(
            "命中自定义目录规则时的整理落点：下载完成后整理到该目录（不直接入库），"
            "文件外部流转回库根后才入账"
        ),
    )
    library_id: int | None = Field(default=None, description="解析出的目标库（前端预选用）")
    library_name: str | None = None
    downloader_name: str | None = None
    route_matched: bool | None = Field(
        default=None,
        description="收藏范围路由结论：true=命中声明库 / false=默认库兜底；未走路由为 null",
    )
    route_reason: str | None = Field(
        default=None, description="路由理由（中文整句，弹窗徽标直接展示）"
    )
    ok: bool = Field(description="按当前配置投递能否顺利入库")
    warning: str | None = Field(default=None, description="不 ok 时的中文指引")


# ---------------------------------------------------------------------------
# 订阅 CRUD
# ---------------------------------------------------------------------------


class SubscriptionCreatePayload(BaseModel):
    """从 Discover 条目创建订阅的公开请求。

    调用方只传递上游返回的稳定引用；来源识别、豆瓣到 TMDB 的锚定、媒体
    建档和初始工单生成均由服务端完成。豆瓣发生歧义时，错误详情会返回可重试
    的 TMDB ``title_ref`` 候选。
    """

    title_ref: str = Field(
        min_length=1,
        max_length=160,
        description="Discover 搜索、片单或详情返回的影视条目稳定引用",
    )
    source_title_ref: str | None = Field(
        default=None,
        max_length=160,
        description=(
            "可选的原始来源引用；从豆瓣歧义候选改选 TMDB 条目时原样回传，用于保留豆瓣身份"
        ),
    )
    selected_seasons: list[int] = Field(
        default_factory=list, description="剧集要订阅的季号数组，如 [1,2]；空=全部缺失季"
    )
    follow_future: bool = Field(default=False, description="自动续订：未来新集与新季自动纳入订阅")
    rule_set_id: int | None = Field(default=None, description="缺省用默认规则组")
    library_id: int | None = Field(default=None, description="入库目标库；缺省用该类型默认库")


class SubscriptionUpdatePayload(BaseModel):
    """部分更新语义：不传的字段一律保持不变。"""

    selected_seasons: list[int] | None = Field(
        default=None, description="新的季选择，如 [1,2]；不传=不变"
    )
    follow_future: bool | None = Field(
        default=None, description="是否自动续订（未来新集与新季自动纳入）；不传=不变"
    )
    rule_set_id: int | None = Field(default=None, description="换绑规则组 id；不传=不变")
    library_id: int | None = Field(
        default=None,
        description="换入库目标库；显式传 null=清除指定、改回按默认库路由；不传=不变",
    )


class SubscriptionTrackingState(StrEnum):
    """用户可显式设置的追踪状态；完成态仍由工单自动派生。"""

    ACTIVE = "active"
    PAUSED = "paused"


class SubscriptionTrackingStatePayload(BaseModel):
    state: SubscriptionTrackingState = Field(
        description="目标追踪状态：active 恢复追踪，paused 暂停搜索与投递"
    )


class SubscriptionFollowFuturePayload(BaseModel):
    """自动续订是详情页上的独立动作，不与选季等批量调整耦合。"""

    enabled: bool = Field(description="是否持续追踪之后播出的新集与新一季")


class DownloadUnitView(BaseModel):
    """追踪单元（电影为 0/0）——下载快照与手动选种结果共用。"""

    season_number: int
    episode_number: int


class SearchNowView(BaseModel):
    """立即搜索缺失资源的结果。"""

    reset_count: int = Field(description="跳过冷却、重新排队的缺口工单数")


class GrabPayload(BaseModel):
    """人工选择种子下载：把搜索结果里的一条种子直接投给本订阅。

    字段即搜索结果行（TorrentHit）原样回传——交互式搜索现算现返、不落
    种子索引，只能由前端带回。attrs 同样回传（它本就是搜索链路里服务端
    enrich 的产物，用户按它筛选后选中了这条）；缺失时服务端重新推导兜底。
    """

    site_id: str
    torrent_id: str
    title: str
    subtitle: str = ""
    category: str | None = Field(default=None, description="站点分类（movie/tv/…）")
    attrs: dict | None = Field(
        default=None, description="搜索结果里的结构化属性（TorrentAttrs）；缺失时服务端重算"
    )
    download_url: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    is_free: bool | None = None
    hit_and_run: bool | None = None
    imdb_id: str | None = None
    douban_id: str | None = None
    publish_time: datetime | None = None


class GrabResultView(BaseModel):
    """手动选种的投递结果。"""

    units: list[DownloadUnitView] = Field(description="本次投递满足的追踪单元")


class ProgressView(BaseModel):
    """列表页进度：total = 工单总数，wanted 子集是缺口，imported 是已入库终态。"""

    total: int
    wanted: int
    grabbed: int
    downloaded: int
    imported: int
    # 已入库但仍在洗版中的单元数。详情视图随 _wanted_upgrades 计算；
    # 列表路由用 upgrading_counts 批量补算（海报墙青点），两处同口径
    upgrading: int = 0


class SubscriptionView(BaseModel):
    id: int
    media: MediaBrief
    status: str
    selected_seasons: list[int]
    follow_future: bool
    rule_set_id: int
    library_id: int | None = Field(description="入库目标库；null=该类型默认库")
    progress: ProgressView
    season_collection: list[SeasonOverview] = Field(
        default_factory=list,
        description="剧集按季收录统计；电影或无需展示时为空",
    )
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_model(
        cls,
        sub: Subscription,
        item: MediaItem,
        counts: dict[str, int],
        season_collection: list[SeasonOverview] | None = None,
    ) -> SubscriptionView:
        wanted = counts.get("wanted", 0)
        grabbed = counts.get("grabbed", 0)
        downloaded = counts.get("downloaded", 0)
        imported = counts.get("imported", 0)
        return cls(
            id=sub.id,  # type: ignore[arg-type]
            media=MediaBrief.from_model(item),
            status=sub.status,
            selected_seasons=list(sub.selected_seasons),
            follow_future=sub.follow_future,
            rule_set_id=sub.rule_set_id,
            library_id=sub.library_id,
            progress=ProgressView(
                total=wanted + grabbed + downloaded + imported,
                wanted=wanted,
                grabbed=grabbed,
                downloaded=downloaded,
                imported=imported,
            ),
            season_collection=season_collection or [],
            created_at=sub.created_at,
            updated_at=sub.updated_at,
        )


class TodayArrivalView(BaseModel):
    """订阅首页的单集待入库摘要；不携带海报和下载进度等重复信息。"""

    subscription_id: int
    wanted_id: int
    media_title: str
    media_kind: Literal["movie", "tv"]
    season_number: int
    episode_number: int
    status: Literal["wanted", "grabbed", "downloaded"]
    air_date: date | None
    expected_day: date = Field(description="预计入库/播出的站点日历日，用于展示日期")
    days_ahead: int = Field(
        description="expected_day 距今天几天（0=今天）；站点日历口径，前端据此切换今日/预告文案"
    )
    release_forecast: dict | None
    next_probe_at: datetime | None = Field(
        description="按站点游标与礼貌间隔换算后的下一次有效预测探测时间"
    )
    info_hash: str | None
    grabbed_at: datetime | None
    downloaded_at: datetime | None
    estimated_release_to_import_minutes: int = Field(
        description="预计出种后到入库的分钟数；优先使用本订阅历史中位数"
    )
    estimated_download_to_import_minutes: int = Field(
        description="下载完成后到入库的分钟数；优先使用本订阅历史中位数"
    )

    @field_serializer("next_probe_at", "grabbed_at", "downloaded_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_models(
        cls,
        sub: Subscription,
        item: MediaItem,
        wanted: WantedItem,
        *,
        next_probe_at: datetime | None,
        release_to_import_minutes: int,
        download_to_import_minutes: int,
        expected_day: date,
        days_ahead: int,
    ) -> TodayArrivalView:
        return cls(
            subscription_id=sub.id,  # type: ignore[arg-type]
            wanted_id=wanted.id,  # type: ignore[arg-type]
            media_title=item.title,
            media_kind=item.kind,  # type: ignore[arg-type]
            season_number=wanted.season_number,
            episode_number=wanted.episode_number,
            status=wanted.status,  # type: ignore[arg-type]
            air_date=wanted.air_date,
            expected_day=expected_day,
            days_ahead=days_ahead,
            release_forecast=wanted.release_forecast,
            next_probe_at=next_probe_at,
            info_hash=wanted.info_hash,
            grabbed_at=wanted.grabbed_at,
            downloaded_at=wanted.downloaded_at,
            estimated_release_to_import_minutes=release_to_import_minutes,
            estimated_download_to_import_minutes=download_to_import_minutes,
        )


def _elapsed_seconds(start: datetime | None, end: datetime | None) -> int | None:
    """计算非负整秒耗时；站点时间异常时不向用户展示误导性的负数。"""
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds())


class ResourceTimingView(BaseModel):
    """一集最近一次成功投递所使用资源的发布→发现→提交时间链。"""

    site_id: str
    torrent_id: str
    publish_time: datetime | None
    first_seen_at: datetime | None
    submitted_at: datetime
    publish_to_seen_seconds: int | None
    seen_to_submit_seconds: int | None
    publish_to_submit_seconds: int | None
    dry_run: bool = False

    @field_serializer("publish_time", "first_seen_at", "submitted_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, object]) -> ResourceTimingView | None:
        publish_time = snapshot.get("publish_time")
        first_seen_at = snapshot.get("first_seen_at")
        submitted_at = snapshot.get("submitted_at")
        if not isinstance(publish_time, datetime):
            publish_time = None
        if not isinstance(first_seen_at, datetime):
            first_seen_at = None
        if not isinstance(submitted_at, datetime):
            return None
        # 既没有发布时间也没有首次发现时间时无法回答“隔了多久”，不返回空壳。
        if publish_time is None and first_seen_at is None:
            return None
        return cls(
            site_id=str(snapshot["site_id"]),
            torrent_id=str(snapshot["torrent_id"]),
            publish_time=publish_time,
            first_seen_at=first_seen_at,
            submitted_at=submitted_at,
            publish_to_seen_seconds=_elapsed_seconds(publish_time, first_seen_at),
            seen_to_submit_seconds=_elapsed_seconds(first_seen_at, submitted_at),
            publish_to_submit_seconds=_elapsed_seconds(publish_time, submitted_at),
            dry_run=snapshot.get("dry_run") is True,
        )


class UpgradeRunPayload(BaseModel):
    """「一轮洗版」请求（docs/design/quality-upgrade.md §13.2）。"""

    rule_set_id: int | None = Field(
        default=None,
        description="可选：先换用该规则组再触发（组必须已配置洗版目标）；缺省用当前组",
    )


class UpgradeRunUnitView(BaseModel):
    """一轮洗版的逐集体检结果。"""

    season_number: int
    episode_number: int
    state: Literal["upgradable", "at_cutoff", "in_flight", "not_comparable", "missing"] = Field(
        description="可洗已排期 / 已达目标 / 洗版在途 / 无法识别当前版本 / 缺失走补缺"
    )
    current_label: str | None = Field(description="当前版本档位标签；未入库/无法识别为 null")
    target_label: str = Field(description="洗版目标档位标签")


class UpgradeRunView(BaseModel):
    """一轮洗版的体检报告（同步返回的一次性快照，不落库）。"""

    target_label: str
    rule_set_id: int = Field(description="本轮实际生效的规则组（换组后为新组）")
    summary: str = Field(description="中文摘要句，前端直接展示")
    counts: dict[str, int]
    units: list[UpgradeRunUnitView]


class WantedUpgradeView(BaseModel):
    """单元的洗版派生状态（docs/design/quality-upgrade.md §8.3/§9）。

    只在"已入库且规则组配置了洗版目标"的单元上出现；标签由后端用统一的
    档位阶梯生成，前端零拼接直接展示。
    """

    active: bool = Field(description="是否洗版中（可证明低于目标且未熔断）")
    current_label: str = Field(description="当前版本档位标签（如「1080p WEB-DL」）")
    target_label: str = Field(description="洗版目标档位标签（如「1080p Remux」）")
    search_attempts: int = Field(description="已洗版搜索次数")
    indeterminate: bool = Field(
        default=False,
        description="无法确认档位：证明不了低于目标也证明不了已达标——"
        "不参与自动洗版，可手动选种替换（§13.8）",
    )


class WantedView(BaseModel):
    id: int
    season_number: int
    episode_number: int
    status: str
    air_date: date | None
    priority: int
    # 在途工单锚定的种子 hash；前端据此把工单行与实时下载进度组对上
    info_hash: str | None
    next_search_at: datetime | None
    search_attempts: int
    last_search_at: datetime | None
    # 由历史种子发布时间推导的可解释调度快照；NULL=样本不足/不适用。
    release_forecast: dict | None
    # 实际拉取时间链；老记录或站点未提供发布时间时可为空。
    resource_timing: ResourceTimingView | None
    grabbed_at: datetime | None
    downloaded_at: datetime | None
    imported_at: datetime | None
    # 单集履历注解（里程碑链的叙事细节）：最近一次被拒原因 / 投递的种子名
    last_reject_reason: str | None
    grab_title: str | None
    # 洗版派生状态；规则组未配洗版目标或单元未入库时为 null
    upgrade: WantedUpgradeView | None = None

    @field_serializer(
        "next_search_at", "last_search_at", "grabbed_at", "downloaded_at", "imported_at"
    )
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_model(
        cls, w: WantedItem, resource_timing: dict[str, object] | None = None
    ) -> WantedView:
        return cls(
            id=w.id,  # type: ignore[arg-type]
            season_number=w.season_number,
            episode_number=w.episode_number,
            status=w.status,
            air_date=w.air_date,
            priority=w.priority,
            info_hash=w.info_hash,
            next_search_at=w.next_search_at,
            search_attempts=w.search_attempts,
            last_search_at=w.last_search_at,
            release_forecast=w.release_forecast,
            resource_timing=(
                ResourceTimingView.from_snapshot(resource_timing)
                if resource_timing is not None
                else None
            ),
            grabbed_at=w.grabbed_at,
            downloaded_at=w.downloaded_at,
            imported_at=w.imported_at,
            last_reject_reason=w.last_reject_reason,
            grab_title=w.grab_title,
        )


class SubscriptionDetailView(SubscriptionView):
    wanted: list[WantedView] = Field(default_factory=list)

    @classmethod
    def from_detail(
        cls,
        sub: Subscription,
        item: MediaItem,
        wanted_rows: list[WantedItem],
        resource_timings: dict[tuple[int, int], dict[str, object]] | None = None,
        rule_spec: object | None = None,
    ) -> SubscriptionDetailView:
        counts: dict[str, int] = {}
        for w in wanted_rows:
            counts[w.status] = counts.get(w.status, 0) + 1
        base = SubscriptionView.from_model(sub, item, counts)
        upgrades = _wanted_upgrades(wanted_rows, rule_spec)
        view = cls(
            **base.model_dump(),
            wanted=[
                WantedView.from_model(
                    w,
                    (resource_timings or {}).get((w.season_number, w.episode_number)),
                )
                for w in wanted_rows
            ],
        )
        for row in view.wanted:
            row.upgrade = upgrades.get((row.season_number, row.episode_number))
        view.progress.upgrading = sum(1 for u in upgrades.values() if u is not None and u.active)
        return view


def _wanted_upgrades(
    wanted_rows: list[WantedItem], rule_spec: object | None
) -> dict[tuple[int, int], WantedUpgradeView | None]:
    """按规则组 spec 派生每个已入库单元的洗版状态（后端统一生成标签）。

    规则组未配洗版目标 / spec 无法解析 → 全部 None（前端不显示洗版信息）。
    """
    from movieclaw_matcher import (
        QualitySnapshot,
        RuleSetSpec,
        provably_at_cutoff,
        provably_below_cutoff,
        quality_label,
        upgrade_target_label,
    )

    if not isinstance(rule_spec, RuleSetSpec) or rule_spec.upgrade_source is None:
        return {}
    # upgrade_ready 与调度口径同源（含熔断冷却）——否则详情页显示"洗版中"
    # 的同时 system_notice 却说该单元已暂停，两处互相打架
    from movieclaw_api.services.subscription import upgrade_ready
    from movieclaw_db.models import utcnow

    now = utcnow()
    target = upgrade_target_label(rule_spec) or ""
    result: dict[tuple[int, int], WantedUpgradeView | None] = {}
    for w in wanted_rows:
        if w.status != "imported" or w.quality is None:
            continue  # NULL=快照未回填（转瞬态），不展示半截结论
        if not w.quality:
            # {} 哨兵：完全无法识别。常驻如实展示（§13.8）——否则这类单元
            # 在追踪明细里与档位健康的单元毫无区别，用户以为它在洗版盘子里
            result[(w.season_number, w.episode_number)] = WantedUpgradeView(
                active=False,
                current_label="版本未识别",
                target_label=target,
                search_attempts=w.search_attempts,
                indeterminate=True,
            )
            continue
        snapshot = QualitySnapshot.model_validate(w.quality)
        result[(w.season_number, w.episode_number)] = WantedUpgradeView(
            active=w.in_scope and upgrade_ready(w, rule_spec, now=now),
            current_label=quality_label(snapshot, rule_spec),
            target_label=target,
            search_attempts=w.search_attempts,
            indeterminate=not provably_below_cutoff(snapshot, rule_spec)
            and not provably_at_cutoff(snapshot, rule_spec),
        )
    return result


class SubscriptionCreateView(BaseModel):
    """完整创建工作流结果：订阅本身以及管理员可见的下载路由预检。"""

    subscription: SubscriptionDetailView
    download_routing: DispatchPreviewView | None = Field(
        default=None,
        description="管理员可见的下载与入库路由预检；成员调用时为空",
    )


class SubscriptionDownloadView(BaseModel):
    """订阅在途种子的实时下载快照（详情页轮询展示）。

    state 词表与 TorrentStatus.state 一致，另加 missing——种子已不在任何
    可用下载器中（可能被手动删除，救援巡检稍后会退回工单重新找资源）。
    """

    info_hash: str
    name: str | None = Field(default=None, description="下载器中的任务名；missing 时为空")
    progress: float | None = Field(default=None, description="0.0~1.0；missing 时为空")
    size_bytes: int | None = None
    dlspeed_bytes: int | None = None
    eta_seconds: int | None = None
    state: str = Field(
        description="downloading / stalled / paused / completed / error / missing / unknown"
    )
    downloader_name: str | None = None
    units: list[DownloadUnitView] = Field(default_factory=list)


class ActivityView(BaseModel):
    """活动时间线的一条记录：message 已是完整中文句子，前端直接展示。"""

    id: int
    type: str
    message: str
    payload: dict
    created_at: datetime
    # 管线类活动挂在具体工单上（生命周期/搜索轮次等订阅级活动为 NULL）。
    # 详情页据此把「该工单最新一条活动仍是失败」翻译成里程碑链上的失败站，
    # 失败原因不再只躺在折叠的排查记录里
    wanted_item_id: int | None

    @field_serializer("created_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_model(cls, row: SubscriptionActivity) -> ActivityView:
        return cls(
            id=row.id,  # type: ignore[arg-type]
            type=row.type,
            message=row.message,
            payload=dict(row.payload),
            created_at=row.created_at,
            wanted_item_id=row.wanted_item_id,
        )


# ---------------------------------------------------------------------------
# 规则组
# ---------------------------------------------------------------------------


class RuleSetPayload(BaseModel):
    name: str = Field(description="规则组名称（订阅列表与选择器里的展示名）")
    spec: dict = Field(
        default_factory=dict,
        description=(
            "过滤规则 JSON（全部键可缺省=不限）："
            'resolutions 分辨率偏好序（如 ["2160p","1080p"]，顺序即优先级）、'
            "media_sources 片源档白名单兼偏好序（值域 remux/blu-ray/web-dl/rip/tv，"
            "顺序即优先级；与 resolutions 一样参与候选选优，空=不限片源）、"
            "video_codecs 编码白名单（按编码族匹配：写 x265 即接受 H.265/HEVC）、"
            "platforms/platforms_block 流媒体平台白/黑名单（规范值如 netflix、"
            "disney_plus、iqiyi；词表外的值读取时丢弃不报错）、"
            "release_groups_allow/release_groups_block "
            "制作组白/黑名单、hdr_levels/hdr_block HDR 白/黑名单（值域 "
            "DV/HDR10+/HDR10/HLG/SDR，SDR=资源未标注 HDR；白名单任一命中即过、"
            "顺序即偏好，黑名单命中即排除；旧的 hdr/dv 三态字段仍可写入，"
            "读取时自动换算并反向回填）、free_only 只要免费种、"
            "min_seeders 做种数下限、size_min_mb/size_max_mb 体积区间（整季包按每集均摊）、"
            "exclude_hr 排除 H&R、hr_unknown_policy 决定 H&R 状态未知时宽松/严格处理、"
            "未填写的条件均不限制。\n\n"
            "subtitle_languages_require：要求的字幕语言（BCP 47，任一命中即通过）。\n\n"
            "audio_languages_require：要求的音轨语言（BCP 47，任一命中即通过）。\n\n"
            "upgrade_source 洗版目标片源档（web-dl/blu-ray/remux，缺省=不洗版）、"
            "cutoff_resolution 洗版目标分辨率（缺省=resolutions 首选，"
            "必须在 resolutions 允许范围内；同理 upgrade_source 必须在 "
            "media_sources 允许范围内）、"
            "upgrade_ladder 参与洗版比较的维度及优先级（顺序即位次，值域 "
            "resolution/source/video_codec/platform，缺省 [resolution, source] "
            "即只比分辨率与片源；偏好列表为空的维度自动跳过）。\n\n"
            '示例：{"resolutions":["2160p"],"free_only":true,"upgrade_source":"remux"}'
        ),
    )


class HealthCheckView(BaseModel):
    """订阅链路体检里的一段检查结论。"""

    key: str = Field(
        description="downloader / dispatch_dir / mapping / transfer_disk / watch_active"
    )
    label: str = Field(description="段落名（如「下载器」「路径映射」）")
    status: Literal["ok", "warn", "error"] = Field(
        description="ok=正常 / warn=能转但降级 / error=会失败，必须修"
    )
    detail: str = Field(description="中文事实陈述，直接展示")
    fix_section: str | None = Field(
        default=None,
        description="修复去处：设置分区 id（sites/downloaders/import-watch）或 libraries",
    )


class LibraryPipelineView(BaseModel):
    """一个库的完整入库链路结论。"""

    library_id: int
    library_name: str
    kind: str
    is_default: bool
    mode: Literal["watch", "inplace", "downloader_default"]
    path: str | None = Field(default=None, description="投递基底目录（movieclaw 视角）")
    library_root: str | None = Field(default=None, description="库主根（入库节点的落点）")
    staging_path: str | None = Field(
        default=None,
        description="命中自定义目录规则时的整理落点（非空时转移段不直接入库，外部流转后回库根入账）",
    )
    status: Literal["ok", "warn", "error"] = Field(description="全链路最坏状态")
    narrative: str = Field(
        default="", description="「订阅命中本库会发生什么」的一句话叙事（正向可预期）"
    )
    checks: list[HealthCheckView]


class FixOptionView(BaseModel):
    """修复卡里的一个可选修法。"""

    title: str = Field(description="选项标题（如「补一条公共父目录映射」）")
    why: str = Field(default="", description="为什么这么做 / 适合谁（帮用户在多个选项间取舍）")
    steps: str = Field(description="具体做什么，含建议值")
    fix_section: str = Field(description="修复去处：设置分区 id 或 libraries（前端映射到路由）")
    fix_label: str = Field(description="跳转按钮文案")
    fix_params: dict[str, str] | None = Field(
        default=None, description="跳转预填参数（目标设置页读取后自动填表单）"
    )


class HealthIssueView(BaseModel):
    """按根因聚合的问题卡：一个根因 = 一张卡，不随受影响的库数膨胀。"""

    key: str = Field(description="与被聚合检查项的 HealthCheck.key 同词表")
    status: Literal["warn", "error"]
    title: str = Field(description="一句话根因")
    detail: str = Field(description="根因的事实陈述与后果")
    affected_libraries: list[str] = Field(default_factory=list, description="受影响的库名")
    options: list[FixOptionView] = Field(description="结构化修复选项（多个时由用户取舍）")


class PipelineHealthView(BaseModel):
    """订阅链路体检的整体结论（订阅设定页与订阅列表警示横幅共用）。"""

    status: Literal["ok", "warn", "error"] = Field(
        description="整体状态：库链路 + 全局段（站点/下载器）的最坏值"
    )
    error_count: int = Field(description="链路有 error 的库数")
    warn_count: int
    site_check: HealthCheckView = Field(description="资源搜索段（全局，链路第一环）")
    downloader_ok: bool = Field(description="是否有可用的默认下载器")
    sites_configured: bool = Field(
        description="是否配置过站点（无论当前可用与否）——开局清单只看它，"
        "配置过但失效的老用户看到的是体检红项而非新手清单"
    )
    downloaders_configured: bool = Field(description="是否配置过下载器（同上语义）")
    issues: list[HealthIssueView] = Field(
        default_factory=list,
        description="按根因聚合的问题卡（error 在前）——前端置顶展示为「需要处理 N 件事」",
    )
    libraries: list[LibraryPipelineView]


class RuleSetView(BaseModel):
    id: int
    name: str
    is_default: bool
    spec: dict
    reference_count: int = Field(default=0, description="正在引用本规则组的订阅数；>0 时不可删除")

    @classmethod
    def from_model(cls, row: RuleSet, *, reference_count: int = 0) -> RuleSetView:
        return cls(
            id=row.id,  # type: ignore[arg-type]
            name=row.name,
            is_default=row.is_default,
            spec=dict(row.spec),
            reference_count=reference_count,
        )
