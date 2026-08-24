from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class AuthType(StrEnum):
    """站点认证方式。与 ``movieclaw_tracker`` 的三种 AuthProvider 一一对应。

    - ``COOKIE``：用户直接粘贴浏览器 cookie（最简单，见 CookieAuthProvider）
    - ``APIKEY``：走站点 API 的密钥认证（如 M-Team，见 ApiKeyAuthProvider）
    - ``CREDENTIAL``：用户名 + 密码，由程序模拟登录（见 CredentialAuthProvider）
    """

    COOKIE = "cookie"
    APIKEY = "apikey"
    CREDENTIAL = "credential"


class ConfigStatus(StrEnum):
    """站点配置的验证状态机。

    用户填入授权信息后并不立刻可用，需异步验证通过才算数。状态流转：

        PENDING ──► VERIFYING ──► ACTIVE   （验证成功，可用）
                          └─────► FAILED   （验证失败，见 last_error）

    - ``PENDING``：已保存，等待验证（刚配置或刚更新后的初始态）。
    - ``VERIFYING``：验证进行中（异步任务已接手）。
    - ``ACTIVE``：验证通过，凭据真实有效。
    - ``FAILED``：验证失败（密码错误、cookie 过期、网络不通等，原因见 last_error）。

    注意：「一个站点是否可用」= ``enabled=True`` 且 ``status=ACTIVE``。
    ``enabled`` 是用户的启用开关（意图），``status`` 是系统的验证结果，二者正交。
    """

    PENDING = "pending"
    VERIFYING = "verifying"
    ACTIVE = "active"
    FAILED = "failed"


class SiteCredential(TimestampMixin, table=True):
    """站点授权凭据表：保存用户为每个站点配置的登录信息。

    这是应用的核心配置数据。每个站点一条记录（``site_id`` 唯一），
    根据 ``auth_type`` 使用不同的字段组合：
    - COOKIE 模式 → 使用 ``cookie``
    - APIKEY 模式 → 使用 ``api_key``
    - CREDENTIAL 模式 → 使用 ``username`` + ``password``

    安全说明：cookie / api_key / password 属于敏感信息，由 ``CredentialRepository``
    在写入时用 SecretBox 加密落库（``enc::`` 前缀密文），读取明文须经该层的
    ``decrypted_*`` 方法；加密内核上线前的存量明文在应用启动时一次性转密文。
    """

    __tablename__ = "site_credential"

    id: int | None = Field(default=None, primary_key=True)
    # 站点标识，对应 registry 里注册的 site_id，一个站点仅一套凭据
    site_id: str = Field(index=True, unique=True, description="站点标识，如 mteam、ttg")
    auth_type: AuthType = Field(description="认证方式")

    # 以下敏感字段按 auth_type 选择性填写，未使用的保持 None
    cookie: str | None = Field(default=None, description="COOKIE 模式：原始 cookie 字符串")
    api_key: str | None = Field(default=None, description="APIKEY 模式：API 密钥")
    username: str | None = Field(default=None, description="CREDENTIAL 模式：用户名")
    password: str | None = Field(default=None, description="CREDENTIAL 模式：密码")

    # 是否启用该站点；停用后不参与聚合搜索等操作，但保留凭据便于随时恢复
    enabled: bool = Field(default=True, description="用户启用开关")

    # ------------------------------------------------------------------
    # 站点保护与刷流（设计见 docs/design/site-protection-ratio-boost.md）
    # ------------------------------------------------------------------
    # 保护开关：打开后订阅链路（被动匹配/缺口搜索/换源/洗版）不再选中该站点，
    # 但主动搜索、手动下载、种子同步照常。用于分享率尚未养起来的新站点。
    # 与 enabled 正交：enabled 是"能不能访问"，protected 是"订阅能不能自动拉"。
    protected: bool = Field(default=False, description="站点保护开关：订阅链路绕开该站")
    # 自动刷分享率开关：盯本地索引的免费种第一时间抢下做种，预算内自动汰换
    boost_enabled: bool = Field(default=False, description="自动刷分享率开关")
    # 刷流暂停开关：上行带宽被刷流占满影响前台使用（如看视频）时的临时闸——
    # 暂停期间在池做种全部压到极低上传限速，且停止汰换与拉新种；恢复时解除
    # 限速、引擎回到正常节奏。与 boost_enabled 正交：暂停是"临时让路"，
    # 关闭是"不再刷流"（关闭刷流会自动清掉暂停态）。
    boost_paused: bool = Field(default=False, description="刷流暂停开关：做种限速+停止汰换拉新")
    # 刷流存储预算（字节）：该站刷流任务占用磁盘的上限，默认 100 GiB
    boost_budget_bytes: int = Field(
        default=100 * 1024**3, description="刷流存储预算（字节）"
    )
    # 刷流汰换的最低保留天数（H&R 安全垫）：任务入池满该天数才可被汰换。
    # 默认 3 天覆盖多数站点的考核时长；无 H&R 考核的站可调 0 = 自由汰换
    #（判定成熟度仍由引擎的测量窗保证，见 ratio_boost.evictable）
    boost_hold_days: int = Field(default=3, description="刷流汰换最低保留天数；0=不保护")

    # ------------------------------------------------------------------
    # 验证状态机（见 ConfigStatus）
    # ------------------------------------------------------------------
    status: ConfigStatus = Field(
        default=ConfigStatus.PENDING,
        index=True,
        description="验证状态：pending/verifying/active/failed",
    )
    # 最近一次验证成功的时间；None 表示从未验证成功
    last_verified_at: datetime | None = Field(default=None, description="最近验证成功时间")
    # 最近一次验证尝试的时间（无论成败都刷新），供页面显示"上次检查于何时"
    last_checked_at: datetime | None = Field(default=None, description="最近验证尝试时间")
    # 最近一次验证失败的原因，已归类为清晰中文，直接展示给用户帮助非开发者排查
    last_error: str | None = Field(default=None, description="最近验证失败原因")
