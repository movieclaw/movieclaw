"""订阅接口的请求/响应模型。

命名沿用项目 API 惯例的 snake_case；时间字段输出前补 UTC 时区标记
（库内 naive UTC，理由见 schemas.site.ConfiguredSite）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, model_validator

from movieclaw_api.services.subscription.quality import public_policy
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


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


class PreparePayload(BaseModel):
    """订阅弹层打开时的预检请求。

    - source=tmdb：带 kind + tmdb_id（发现页/详情页入口）；
    - source=douban：带 kind + title（豆瓣入口，year/douban_id 尽量带上，
      收敛精度更高）。
    """

    source: Literal["tmdb", "douban"] = Field(
        default="tmdb", description="入口来源：tmdb（带 tmdb_id）/ douban（带 title，可配 year）"
    )
    kind: MediaKind
    tmdb_id: int | None = Field(default=None, description="source=tmdb 时必填的 TMDB 条目 id")
    title: str | None = Field(default=None, description="豆瓣入口：豆瓣标题")
    year: int | None = Field(default=None, description="豆瓣入口：年份（可缺）")
    douban_id: str | None = Field(default=None, description="豆瓣入口：豆瓣条目 ID")


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
    title: str
    original_title: str
    year: int | None
    poster_url: str | None

    @classmethod
    def from_model(cls, c: ResolveCandidate) -> ResolveCandidateView:
        from movieclaw_api.core.config import get_settings

        poster_url = None
        if c.poster_path:
            base = get_settings().tmdb_image_base_url.rstrip("/")
            poster_url = f"{base}/w342{c.poster_path}"
        return cls(
            tmdb_id=c.tmdb_id,
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


class QualityPolicyPayload(BaseModel):
    """订阅级连续性策略；目标规则组在保存时复制成稳定快照。"""

    mode: Literal["lock_first", "upgrade"] = Field(
        description="lock_first=首次入库后固定；upgrade=洗版达标后固定"
    )
    target_rule_set_id: int | None = Field(
        default=None, description="洗版目标规则组；mode=upgrade 时必填"
    )

    @model_validator(mode="after")
    def validate_target(self) -> QualityPolicyPayload:
        if self.mode == "upgrade" and self.target_rule_set_id is None:
            raise ValueError("自动洗版必须选择目标规则组")
        if self.mode == "lock_first" and self.target_rule_set_id is not None:
            self.target_rule_set_id = None
        return self


class SubscriptionCreatePayload(BaseModel):
    kind: MediaKind
    tmdb_id: int = Field(description="TMDB 条目 id（movie 用电影 id，tv 用剧集 id）")
    selected_seasons: list[int] = Field(
        default_factory=list, description="剧集要订阅的季号数组，如 [1,2]；空=全部缺失季"
    )
    follow_future: bool = Field(default=False, description="持续追新：未来新季自动纳入订阅")
    rule_set_id: int | None = Field(default=None, description="缺省用默认规则组")
    library_id: int | None = Field(default=None, description="入库目标库；缺省用该类型默认库")
    douban_id: str | None = Field(default=None, description="豆瓣入口时带上，留存来源身份")
    quality_policy: QualityPolicyPayload | None = Field(
        default=None, description="首个版本锁定或自动洗版策略；null=关闭"
    )


class SubscriptionUpdatePayload(BaseModel):
    """部分更新语义：不传的字段一律保持不变。"""

    selected_seasons: list[int] | None = Field(
        default=None, description="新的季选择，如 [1,2]；不传=不变"
    )
    follow_future: bool | None = Field(
        default=None, description="是否持续追新（新季自动纳入）；不传=不变"
    )
    rule_set_id: int | None = Field(default=None, description="换绑规则组 id；不传=不变")
    library_id: int | None = Field(
        default=None,
        description="换入库目标库；显式传 null=清除指定、改回按默认库路由；不传=不变",
    )
    quality_policy: QualityPolicyPayload | None = Field(
        default=None, description="首个版本锁定或自动洗版策略；显式 null=关闭；不传=不变"
    )


class SubscriptionPausePayload(BaseModel):
    paused: bool = Field(description="true=暂停（停止抓种与投递）；false=恢复追踪")


class DownloadUnitView(BaseModel):
    """追踪单元（电影为 0/0）——下载快照与手动选种结果共用。"""

    season_number: int
    episode_number: int


class SearchNowView(BaseModel):
    """立即搜索（sub.search-now）的结果。"""

    reset_count: int = Field(description="跳过冷却、重新排队的缺口工单数")


class GrabPayload(BaseModel):
    """手动选种（sub.grab）：把搜索结果里的一条种子直接投给本订阅。

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
    upgrading: int = Field(default=0, description="已有版本、正在寻找洗版目标的单元数")


class SubscriptionView(BaseModel):
    id: int
    media: MediaBrief
    status: str
    selected_seasons: list[int]
    follow_future: bool
    rule_set_id: int
    quality_policy: dict | None = Field(description="连续性/洗版策略及当前锁定状态")
    library_id: int | None = Field(description="入库目标库；null=该类型默认库")
    progress: ProgressView
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_model(
        cls, sub: Subscription, item: MediaItem, counts: dict[str, int]
    ) -> SubscriptionView:
        wanted = counts.get("wanted", 0)
        grabbed = counts.get("grabbed", 0)
        downloaded = counts.get("downloaded", 0)
        imported = counts.get("imported", 0)
        upgrading = counts.get("upgrading", 0)
        return cls(
            id=sub.id,  # type: ignore[arg-type]
            media=MediaBrief.from_model(item),
            status=sub.status,
            selected_seasons=list(sub.selected_seasons),
            follow_future=sub.follow_future,
            rule_set_id=sub.rule_set_id,
            quality_policy=public_policy(sub.quality_policy),
            library_id=sub.library_id,
            progress=ProgressView(
                total=wanted + grabbed + downloaded + imported + upgrading,
                wanted=wanted,
                grabbed=grabbed,
                downloaded=downloaded,
                imported=imported,
                upgrading=upgrading,
            ),
            created_at=sub.created_at,
            updated_at=sub.updated_at,
        )


class WantedView(BaseModel):
    id: int
    season_number: int
    episode_number: int
    status: str
    air_date: date | None
    priority: int
    # 在途工单锚定的种子 hash；前端据此把工单行与 sub.downloads 的进度组对上
    info_hash: str | None
    next_search_at: datetime | None
    search_attempts: int
    last_search_at: datetime | None
    grabbed_at: datetime | None
    downloaded_at: datetime | None
    imported_at: datetime | None

    @field_serializer(
        "next_search_at", "last_search_at", "grabbed_at", "downloaded_at", "imported_at"
    )
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_model(cls, w: WantedItem) -> WantedView:
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
            grabbed_at=w.grabbed_at,
            downloaded_at=w.downloaded_at,
            imported_at=w.imported_at,
        )


class SubscriptionDetailView(SubscriptionView):
    wanted: list[WantedView] = Field(default_factory=list)

    @classmethod
    def from_detail(
        cls,
        sub: Subscription,
        item: MediaItem,
        wanted_rows: list[WantedItem],
    ) -> SubscriptionDetailView:
        counts: dict[str, int] = {}
        for w in wanted_rows:
            counts[w.status] = counts.get(w.status, 0) + 1
        base = SubscriptionView.from_model(sub, item, counts)
        return cls(
            **base.model_dump(),
            wanted=[WantedView.from_model(w) for w in wanted_rows],
        )


class SubscriptionDownloadView(BaseModel):
    """订阅在途种子的实时下载快照（sub.downloads，详情页轮询展示）。

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
            "video_codecs 编码白名单、platforms_allow/platforms_block 平台白/黑名单、"
            "release_groups_allow/release_groups_block 制作组白/黑名单、"
            "source_match_mode 平台与制作组白名单的 any/all 关系、"
            "hdr 策略（any/require/forbid，判断整个 HDR 家族含 DV）、"
            "dv 策略（any/require/forbid，单独判断杜比视界，与 hdr 正交，"
            "如必须 HDR 但排除 DV = hdr:require + dv:forbid）、free_only 只要免费种、"
            "min_seeders 做种数下限、size_min_mb/size_max_mb 体积区间（整季包按每集均摊）、"
            "exclude_hr 排除 H&R。"
            '例：{"resolutions":["2160p"],"free_only":true}'
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
    reference_count: int = Field(
        default=0, description="正在引用本规则组的订阅数；>0 时不可删除"
    )

    @classmethod
    def from_model(cls, row: RuleSet, *, reference_count: int = 0) -> RuleSetView:
        return cls(
            id=row.id,  # type: ignore[arg-type]
            name=row.name,
            is_default=row.is_default,
            spec=dict(row.spec),
            reference_count=reference_count,
        )
