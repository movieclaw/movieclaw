"""登录鉴权相关的请求 / 响应模型。"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class BootstrapStatus(BaseModel):
    """首次初始化状态：前端据此决定进引导页（/setup）还是登录页（/login）。"""

    initialized: bool


class BootstrapRequest(BaseModel):
    """首次初始化：创建超级管理员账号。"""

    username: str = Field(min_length=3, max_length=32, description="管理员用户名")
    password: str = Field(min_length=8, max_length=128, description="管理员密码，至少 8 位")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = Field(default=False, description="记住我：会话有效期 7 天 → 30 天")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128, description="当前密码（校验身份）")
    new_password: str = Field(min_length=8, max_length=128, description="新密码，至少 8 位")


class UpdateProfileRequest(BaseModel):
    """修改个人信息（当前只有昵称；登录用户名不可改）。"""

    nickname: str = Field(min_length=1, max_length=32, description="展示昵称")


class ApiTokenCreateRequest(BaseModel):
    """创建 CLI API 令牌。"""

    name: str = Field(
        min_length=1, max_length=64, description="令牌名字，如 'nas-cron'，便于识别与吊销"
    )


class ApiTokenView(BaseModel):
    """令牌元信息（列表用；不含任何可用于认证的内容）。"""

    id: str
    name: str
    created_at: str
    client_type: str = Field(default="manual", description="客户端形态：worker / cli / manual")
    last_used_at: str | None = Field(
        default=None, description="最近一次使用时间；None 表示从未使用过"
    )


class ApiTokenCreatedView(ApiTokenView):
    """创建成功的返回体：token 明文仅此一次，请立即保存。"""

    token: str = Field(description="令牌明文；服务端只存哈希，之后无法再次查看")


class SessionCapabilities(BaseModel):
    """当前主体的能力开关快照（前端据此裁剪入口；安全边界仍在后端 403）。"""

    allow_subscribe: bool = True
    allow_search: bool = True
    allow_direct_download: bool = True


class SessionView(BaseModel):
    """当前登录状态（GET /auth/me 与登录成功后的返回体）。"""

    username: str
    nickname: str
    avatar_url: str | None = Field(
        default=None, description="头像相对 URL（含版本号）；未上传过头像时为空"
    )
    role: str = Field(default="admin", description="admin=超级管理员；member=成员")
    capabilities: SessionCapabilities = Field(
        default_factory=SessionCapabilities,
        description="能力开关快照；管理员恒为全开",
    )


# ---------------------------------------------------------------------------
# 设备授权（docs/design/device-auth.md §2）
# ---------------------------------------------------------------------------


class DeviceAuthorizeRequest(BaseModel):
    """客户端发起接入请求。

    刻意**没有权限字段**：客户端只声明自己是什么形态、叫什么名字，
    能做什么由批准者决定。
    """

    client_type: str = Field(
        description="客户端形态：worker（转码 Worker）或 cli（命令行 / Agent）"
    )
    client_name: str = Field(
        min_length=1,
        max_length=64,
        description="设备名，批准页上给人看的，如 'Yi的Mac-mini'",
    )


class DeviceAuthorizeView(BaseModel):
    """接入请求的回执。``user_code`` 给人看，``device_code`` 用于兑换。"""

    user_code: str = Field(description="配对码，客户端显示给用户，在网页上核对")
    device_code: str = Field(description="兑换凭据，仅客户端持有，不得展示给用户")
    verification_uri: str = Field(description="用户应当打开的网页地址")
    interval: int = Field(description="建议的轮询间隔（秒），不要比这更快")
    expires_in: int = Field(description="配对码有效期（秒），超时需重新发起")


class DeviceTokenRequest(BaseModel):
    """客户端轮询兑换令牌。"""

    device_code: str = Field(min_length=1, max_length=128)


class DeviceTokenView(BaseModel):
    """兑换成功的返回体：令牌明文仅此一次。"""

    token: str = Field(description="令牌明文；服务端只存哈希，之后无法再次查看")
    client_name: str
    client_type: str
    granted_by: str = Field(description="批准者身份，仅用于客户端回显「你现在是谁」")


class DeviceRequestView(BaseModel):
    """待批准的接入请求（网页审批卡的数据源）。"""

    user_code: str
    client_type: str
    client_name: str
    source_ip: str = Field(
        description=(
            "请求来源 IP，帮助用户判断这是不是自己那台机器；"
            "容器桥接网络会把源地址 NAT 掉，那种情况下为空串，界面应如实说无法确定"
        )
    )
    expires_in: int = Field(description="剩余有效秒数")
