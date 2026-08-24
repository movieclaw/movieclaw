from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, field_serializer

from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.services.auth_factory import required_fields
from movieclaw_db.models.site_credential import AuthType, ConfigStatus, SiteCredential
from movieclaw_db.models.site_torrent import SiteSyncCursor
from movieclaw_db.models.site_user_profile import SiteUserProfile
from movieclaw_tracker.registry import SiteConfig

# ---------------------------------------------------------------------------
# 目录（可选项）相关
# ---------------------------------------------------------------------------


class AuthTypeRequirement(BaseModel):
    """某授权类型及其要求用户填写的字段，供前端渲染表单。"""

    auth_type: AuthType
    required_fields: list[str] = Field(description="该授权类型需要填写的字段名")


class CatalogItem(BaseModel):
    """目录项：一个系统支持的可配置站点。"""

    site_id: str
    display_name: str
    base_url: str
    supported_auth_types: list[AuthTypeRequirement] = Field(
        description="支持的授权类型及各自的必填字段"
    )

    @classmethod
    def from_config(cls, config: SiteConfig) -> CatalogItem:
        """从 registry 的 SiteConfig 构造目录项，附带每种授权类型的必填字段。"""
        reqs = [
            AuthTypeRequirement(
                auth_type=AuthType(t),
                required_fields=list(required_fields(AuthType(t))),
            )
            for t in config.supported_auth_types
        ]
        return cls(
            site_id=config.site_id,
            display_name=config.display_name,
            base_url=config.base_url,
            supported_auth_types=reqs,
        )


# ---------------------------------------------------------------------------
# 已配置站点相关
# ---------------------------------------------------------------------------


class SiteUserProfileView(BaseModel):
    """站点用户资料快照的对外视图（数据来源见 SiteUserProfile 模型）。

    上传/下载量只回传字节数，由前端统一格式化；``ratio`` 为 None 表示站点
    未提供（与 0.0 —— 真实无上传 —— 含义不同，前端应显示为"—"）。
    """

    username: str
    user_class: str = ""
    uploaded_bytes: int = 0
    downloaded_bytes: int = 0
    ratio: float | None = None
    bonus: float | None = None
    seeding_count: int = 0
    leeching_count: int = 0
    fetched_at: datetime

    @field_serializer("fetched_at")
    def _serialize_utc(self, value: datetime) -> str:
        """库内 naive UTC 补时区标记再输出，理由见 ConfiguredSite._serialize_utc。"""
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()

    @classmethod
    def from_model(cls, row: SiteUserProfile) -> SiteUserProfileView:
        return cls(
            username=row.username,
            user_class=row.user_class,
            uploaded_bytes=row.uploaded_bytes,
            downloaded_bytes=row.downloaded_bytes,
            ratio=row.ratio,
            bonus=row.bonus,
            seeding_count=row.seeding_count,
            leeching_count=row.leeching_count,
            fetched_at=row.fetched_at,
        )


class ConfiguredSite(BaseModel):
    """已配置站点的对外视图（**脱敏**：绝不回传 cookie/api_key/密码）。"""

    site_id: str
    auth_type: AuthType
    enabled: bool
    status: ConfigStatus
    usable: bool = Field(description="是否可用 = 已启用且验证通过（status=active）")
    protected: bool = Field(
        default=False, description="站点保护开关：订阅链路绕开该站，手动搜索/下载不受影响"
    )
    boost_enabled: bool = Field(default=False, description="自动刷分享率开关")
    boost_paused: bool = Field(
        default=False,
        description="刷流暂停中：做种压到极低上传限速，停止汰换与拉新种（任务保留）",
    )
    boost_budget_bytes: int = Field(default=0, description="刷流存储预算（字节）")
    boost_hold_days: int = Field(default=3, description="刷流汰换最低保留天数；0=不保护")
    last_verified_at: datetime | None = Field(default=None, description="最近验证成功时间")
    last_checked_at: datetime | None = Field(default=None, description="最近验证尝试时间")
    last_error: str | None = Field(default=None, description="最近验证失败原因（清晰中文）")
    profile: SiteUserProfileView | None = Field(
        default=None, description="站点用户资料快照；从未验证成功过则为 null"
    )
    created_at: datetime
    updated_at: datetime

    @field_serializer("last_verified_at", "last_checked_at", "created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        """把库内的 naive UTC 时间序列化为**带 UTC 时区标记**的 ISO 串。

        全库时间统一存 naive UTC（见 movieclaw_db.models.base.utcnow）。若原样输出，
        得到的是无时区后缀的串（如 ``2026-07-09T10:00:00``）。前端 ``new Date()``
        会按 ECMAScript 规范把它当作**本地时间**解析，东八区用户于是看到
        "刚验证完却显示 8 小时前"的错位。这里补上 ``+00:00``，让前端正确按 UTC 解析。
        """
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()

    @classmethod
    def from_model(
        cls, row: SiteCredential, profile: SiteUserProfile | None = None
    ) -> ConfiguredSite:
        """从 ORM 记录构造脱敏视图。只挑选可公开字段，天然屏蔽敏感信息。

        ``profile`` 为该站点的用户资料快照（可选）——从未验证成功过的站点没有。
        """
        return cls(
            site_id=row.site_id,
            auth_type=row.auth_type,
            enabled=row.enabled,
            status=row.status,
            usable=row.enabled and row.status == ConfigStatus.ACTIVE,
            protected=row.protected,
            boost_enabled=row.boost_enabled,
            boost_paused=row.boost_paused,
            boost_budget_bytes=row.boost_budget_bytes,
            boost_hold_days=row.boost_hold_days,
            last_verified_at=row.last_verified_at,
            last_checked_at=row.last_checked_at,
            last_error=row.last_error,
            profile=SiteUserProfileView.from_model(profile) if profile else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SiteSyncStatsView(BaseModel):
    """站点种子缓存与同步节奏的对外视图（数据来源见 SiteTorrent / SiteSyncCursor）。

    供站点配置页展示「本地缓存了多少、上次/下次什么时候同步」。可空语义：
    - ``last_sync_at`` 为 None = 从未同步过；
    - ``next_sync_at`` 为 None = 立即到期（新站等待首刷）；
    - ``last_error`` 为 None = 上次同步成功。
    """

    torrent_count: int = Field(description="该站点已缓存的种子数")
    tracking_since: datetime | None = Field(default=None, description="开始跟踪时间(t0)")
    last_sync_at: datetime | None = Field(default=None, description="上次同步完成时间")
    last_success_at: datetime | None = Field(
        default=None, description="上次同步成功时间；None=从未成功"
    )
    next_sync_at: datetime | None = Field(
        default=None, description="下次同步到期时刻；None=立即到期"
    )
    sync_interval_seconds: int | None = Field(default=None, description="当前自适应轮询间隔（秒）")
    last_new_count: int | None = Field(default=None, description="上次同步新增种子数")
    last_error: str | None = Field(default=None, description="上次同步失败原因；成功为 None")
    consecutive_failures: int = Field(default=0, description="连续同步失败次数；成功清零")

    @field_serializer("tracking_since", "last_sync_at", "last_success_at", "next_sync_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        """库内 naive UTC 补时区标记再输出，理由见 ConfiguredSite._serialize_utc。"""
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()

    @classmethod
    def from_parts(cls, count: int, cursor: SiteSyncCursor | None) -> SiteSyncStatsView:
        """由「缓存计数 + 同步游标」组装视图。二者可能只有其一：
        刚加站还没同步过只有游标，游标被清但快照残留则只有计数。"""
        return cls(
            torrent_count=count,
            tracking_since=cursor.tracking_since if cursor else None,
            last_sync_at=cursor.last_sync_at if cursor else None,
            last_success_at=cursor.last_success_at if cursor else None,
            next_sync_at=cursor.next_sync_at if cursor else None,
            sync_interval_seconds=cursor.sync_interval_seconds if cursor else None,
            last_new_count=cursor.last_new_count if cursor else None,
            last_error=cursor.last_error if cursor else None,
            consecutive_failures=cursor.consecutive_failures if cursor else 0,
        )


class SiteConfigCreate(BaseModel):
    """配置/更新站点的请求体。

    按所选 auth_type 填写对应字段；未用到的字段留空即可（服务端会校验必填项）：
    - cookie      → cookie 字符串
    - apikey      → api_key
    - credential  → username + password
    """

    site_id: str = Field(
        description="要配置的站点标识，须来自站点目录（CLI：mclaw site catalog）"
    )
    auth_type: AuthType = Field(description="选用的授权类型，须在该站点 supported 列表内")
    cookie: str | None = Field(default=None, description="COOKIE 模式：浏览器 cookie 字符串")
    api_key: str | None = Field(default=None, description="APIKEY 模式：API 密钥")
    username: str | None = Field(default=None, description="CREDENTIAL 模式：用户名")
    password: str | None = Field(default=None, description="CREDENTIAL 模式：密码")
    enabled: bool = Field(default=True, description="是否启用（默认启用）")


class SiteConfigUpdate(BaseModel):
    """更新站点授权信息的请求体（site_id 走路径参数，故此处不含）。"""

    auth_type: AuthType = Field(description="选用的授权类型，须在该站点 supported 列表内")
    cookie: str | None = Field(default=None, description="COOKIE 模式：浏览器 cookie 字符串")
    api_key: str | None = Field(default=None, description="APIKEY 模式：API 密钥")
    username: str | None = Field(default=None, description="CREDENTIAL 模式：用户名")
    password: str | None = Field(default=None, description="CREDENTIAL 模式：密码")
    enabled: bool = Field(default=True, description="true=启用 / false=停用")


class SiteStatusUpdate(BaseModel):
    """启用/停用请求体。"""

    enabled: bool = Field(description="true=启用 / false=停用（停用后不再参与搜索与同步）")


class SiteProtectionUpdate(BaseModel):
    """站点保护开关请求体（语义见 docs/design/site-protection-ratio-boost.md）。"""

    protected: bool = Field(
        description="true=保护（订阅链路绕开该站，手动搜索/下载不受影响）/ false=取消保护"
    )


class SiteRatioBoostUpdate(BaseModel):
    """自动刷分享率设置请求体。"""

    enabled: bool = Field(description="true=开启自动刷分享率 / false=关闭")
    budget_bytes: int | None = Field(
        default=None, description="刷流存储预算（字节，≥1 GiB）；None=不修改现有预算"
    )
    hold_days: int | None = Field(
        default=None,
        description="汰换最低保留天数（0～30；0=站点无 H&R、不设保护）；None=不修改",
    )


class SiteBoostPauseUpdate(BaseModel):
    """刷流暂停/恢复请求体。"""

    paused: bool = Field(
        description=(
            "true=暂停（做种压到极低上传限速、停止汰换与拉新种，任务与数据保留）"
            "/ false=恢复（解除限速，回到正常刷流节奏）"
        )
    )


class SiteBoostStatsView(BaseModel):
    """单个站点的刷流运行统计（数据来源见 RatioBoostTask 台账）。"""

    active_count: int = Field(default=0, description="在池刷流任务数")
    used_bytes: int = Field(default=0, description="在池任务占用的预算（字节）")
    budget_bytes: int = Field(default=0, description="当前预算（字节）")
    uploaded_bytes_total: int = Field(
        default=0, description="刷流累计上传量（含已汰换任务的历史贡献，字节）"
    )
    evicted_count: int = Field(default=0, description="累计汰换任务数")
    # 近期窗口指标（来自小时桶统计）：支撑"近 24 小时用 X GB 种子贡献 Y GB 上传"
    uploaded_bytes_24h: int = Field(default=0, description="近 24 小时上传量（字节）")
    avg_used_bytes_24h: int = Field(default=0, description="近 24 小时平均在池体积（字节）")
    uploaded_bytes_7d: int = Field(default=0, description="近 7 天上传量（字节）")
    avg_used_bytes_7d: int = Field(default=0, description="近 7 天平均在池体积（字节）")
