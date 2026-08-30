"""登录鉴权路由：首次初始化、登录、登出、会话查询、修改密码。

安全分区（与 api/router.py 的三分区对应）：
- 公开：GET/POST /auth/bootstrap、POST /auth/login、POST /auth/logout。
  其中 POST /auth/bootstrap 由服务层的一次性锁自我封闭（管理员已存在即 409），
  logout 只是清 Cookie，无需登录也无危害（会话过期后也能顺利登出）。
- 登录后：GET /auth/me、PUT /auth/password、PUT /auth/profile、
  POST/GET /auth/avatar（头像上传与读取；头像属于个人信息，读取也要求登录，
  同源部署下 <img> 自动携带会话 Cookie，前端零改造）。

会话凭证放 HttpOnly Cookie（同源部署下前端零改造自动携带；XSS 偷不走，
SameSite=Lax 挡跨站请求伪造）。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.client_address import client_address
from movieclaw_api.api.deps import require_admin_session, require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.auth import (
    ApiTokenCreatedView,
    ApiTokenCreateRequest,
    ApiTokenView,
    BootstrapRequest,
    BootstrapStatus,
    ChangePasswordRequest,
    DeviceAuthorizeRequest,
    DeviceAuthorizeView,
    DeviceRequestView,
    DeviceTokenRequest,
    DeviceTokenView,
    LoginRequest,
    SessionCapabilities,
    SessionView,
    UpdateProfileRequest,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services import avatar as avatar_media
from movieclaw_api.services import members as members_service
from movieclaw_api.services.auth import Principal
from movieclaw_api.settings import (
    AdminAccountSetting,
    AppServerSetting,
    get_setting_store,
    mark_initialized,
)
from movieclaw_db.engine import get_session
from movieclaw_db.models.member import Member

logger = logging.getLogger("movieclaw_api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def _avatar_url(stem: str | None = None) -> str | None:
    """构造头像的带版本号相对地址；未上传过头像时返回 None（前端显示首字徽标）。

    版本号取文件 mtime 纳秒值：换头像 → URL 变化，绕开浏览器 <img> 缓存。
    超管与成员共用 GET /auth/avatar 端点（各读各的槽位），URL 形态一致。
    """
    version = (
        avatar_media.avatar_version(stem) if stem else avatar_media.avatar_version()
    )
    if version is None:
        return None
    return f"{get_settings().api_v1_prefix}/auth/avatar?v={version}"


def _session_view(account: AdminAccountSetting) -> SessionView:
    """超管账号 → 会话视图。老账号可能没存过昵称（字段后加的），回退到用户名。"""
    return SessionView(
        username=account.username,
        nickname=account.nickname or account.username,
        avatar_url=_avatar_url(),
        role="admin",
        capabilities=SessionCapabilities(),
    )


def _member_session_view(member: Member) -> SessionView:
    """成员行 → 会话视图（能力开关快照供前端裁剪入口，安全边界仍在后端）。"""
    return SessionView(
        username=member.username,
        nickname=member.nickname or member.username,
        avatar_url=_avatar_url(avatar_media.member_stem(member.id)),
        role="member",
        capabilities=SessionCapabilities(
            allow_subscribe=member.allow_subscribe,
            allow_search=member.allow_search,
            allow_direct_download=member.allow_direct_download,
        ),
    )


async def _principal_session_view(principal: Principal) -> SessionView:
    """请求主体 → 会话视图。成员主体已携带成员行；其余（含 PAT/Agent）按超管展示。"""
    if principal.kind == "member" and principal.member is not None:
        return _member_session_view(principal.member)
    return _session_view(await auth_service.get_admin_account())


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    """统一的会话 Cookie 写入口，安全属性集中在这一处维护。"""
    response.set_cookie(
        key=auth_service.SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,  # JS 不可读，XSS 无法窃取会话
        samesite="lax",  # 跨站发起的 POST 不携带，天然防 CSRF
        # 自托管常见 LAN 内 http 直连，Secure 默认关闭；公网 https 部署时开启
        secure=get_settings().session_cookie_secure,
        path="/",
    )


@router.get(
    "/bootstrap",
    response_model=ApiResponse[BootstrapStatus],
    summary="查询系统是否已完成首次初始化",
    operation_id="auth.bootstrap.status",
)
async def bootstrap_status() -> ApiResponse[BootstrapStatus]:
    """公开接口：仅返回布尔状态，供前端决定进 /setup 还是 /login。"""
    return ok(BootstrapStatus(initialized=await auth_service.is_admin_initialized()))


@router.post(
    "/bootstrap",
    response_model=ApiResponse[SessionView],
    summary="首次初始化：创建超级管理员（全生命周期仅一次）",
    operation_id="auth.bootstrap.create",
)
async def bootstrap_create(
    payload: BootstrapRequest, response: Response
) -> ApiResponse[SessionView]:
    """创建管理员并自动登录。管理员已存在时一律 409，锁在服务端，不可绕过。"""
    account = await auth_service.create_admin(payload.username, payload.password)
    await mark_initialized()

    token, max_age = await auth_service.issue_session_token(account.username)
    _set_session_cookie(response, token, max_age)
    return ok(_session_view(account), message="初始化完成，已自动登录")


@router.post(
    "/login",
    response_model=ApiResponse[SessionView],
    summary="管理员登录",
    operation_id="auth.login",
    # CLI 侧登录/登出由精选命令 mclaw login/logout 负责（要持久化本地凭证），
    # 生成层隐藏本端点避免出现语义不完整的同名命令
    openapi_extra={"x-cli-hidden": True},
)
async def login(payload: LoginRequest, response: Response) -> ApiResponse[SessionView]:
    """校验账号密码并种下会话 Cookie（超管或成员）。连续失败触发限速（429）。"""
    identity = await auth_service.authenticate(payload.username, payload.password)
    if isinstance(identity, Member):
        token, max_age = await auth_service.issue_member_session_token(
            identity, remember=payload.remember
        )
        _set_session_cookie(response, token, max_age)
        return ok(_member_session_view(identity), message="登录成功")

    token, max_age = await auth_service.issue_session_token(
        identity.username, remember=payload.remember
    )
    _set_session_cookie(response, token, max_age)
    return ok(_session_view(identity), message="登录成功")


@router.post(
    "/logout",
    response_model=ApiResponse[None],
    summary="退出登录",
    operation_id="auth.logout",
    # CLI 侧登录/登出由精选命令 mclaw login/logout 负责（要持久化本地凭证），
    # 生成层隐藏本端点避免出现语义不完整的同名命令
    openapi_extra={"x-cli-hidden": True},
)
async def logout(response: Response) -> ApiResponse[None]:
    """清除会话 Cookie。无需登录态即可调用（会话已过期时也能正常登出）。"""
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
    return ok(None, message="已退出登录")


@router.get(
    "/me",
    response_model=ApiResponse[SessionView],
    summary="查询当前登录状态",
    operation_id="auth.me",
)
async def me(principal: Principal = Depends(require_login)) -> ApiResponse[SessionView]:
    return ok(await _principal_session_view(principal))


@router.put(
    "/profile",
    response_model=ApiResponse[SessionView],
    summary="修改个人信息（昵称）",
    operation_id="auth.profile.update",
)
async def update_profile(
    payload: UpdateProfileRequest,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionView]:
    """昵称只影响界面展示；登录用户名与会话均不受影响。按身份分流到
    超管配置域或成员表。"""
    if principal.kind == "member" and principal.member_id is not None:
        member = await members_service.update_own_nickname(
            session, principal.member_id, payload.nickname
        )
        return ok(_member_session_view(member), message="个人信息已更新")
    account = await auth_service.update_nickname(payload.nickname.strip())
    return ok(_session_view(account), message="个人信息已更新")


def _avatar_stem_for(principal: Principal) -> str | None:
    """当前主体的头像槽位；超管（含 PAT/Agent）用默认槽位（返回 None）。"""
    if principal.kind == "member" and principal.member_id is not None:
        return avatar_media.member_stem(principal.member_id)
    return None


@router.post(
    "/avatar",
    response_model=ApiResponse[SessionView],
    summary="上传（替换）头像",
    operation_id="auth.avatar.upload",
)
async def upload_avatar(
    file: UploadFile = File(...),
    principal: Principal = Depends(require_login),
) -> ApiResponse[SessionView]:
    """接收一张图片存为头像；已有头像直接替换（按主体分槽位，不保留历史）。

    校验：只接受常见位图格式（拒绝可内嵌脚本的 SVG）、大小有上限。
    错误信息为中文，方便非开发者按提示处理。
    """
    if not avatar_media.is_supported_content_type(file.content_type):
        raise BadRequestException("不支持的图片格式，请上传 JPG / PNG / WebP / GIF / AVIF 图片")

    data = await file.read()
    if not data:
        raise BadRequestException("上传的图片为空，请重新选择")
    if len(data) > avatar_media.MAX_AVATAR_BYTES:
        limit_mb = avatar_media.MAX_AVATAR_BYTES // (1024 * 1024)
        raise BadRequestException(f"图片过大，请控制在 {limit_mb}MB 以内")

    stem = _avatar_stem_for(principal)
    # 已在上面校验过 content_type 属于受支持集合，此处必定命中
    if stem is None:
        avatar_media.save_avatar(data, file.content_type)  # type: ignore[arg-type]
    else:
        avatar_media.save_avatar(data, file.content_type, stem)  # type: ignore[arg-type]
    return ok(await _principal_session_view(principal), message="头像已更新")


@router.get(
    "/avatar",
    summary="读取头像文件",
    response_class=Response,
    operation_id="auth.avatar.download",
)
async def read_avatar(principal: Principal = Depends(require_login)) -> FileResponse:
    """直接返回当前主体的头像本体，供 <img> 加载；地址由会话视图的 avatar_url 给出。"""
    stem = _avatar_stem_for(principal)
    path = avatar_media.find_avatar(stem) if stem else avatar_media.find_avatar()
    if path is None:
        raise NotFoundException("尚未上传头像")
    return FileResponse(
        path,
        media_type=avatar_media.content_type_for(path),
        # URL 带版本号做缓存键，这里可放心让浏览器长期缓存，换头像时 URL 会变。
        headers={"Cache-Control": "private, max-age=31536000"},
    )


@router.put(
    "/password",
    response_model=ApiResponse[SessionView],
    summary="修改密码（本人其余会话强制下线）",
    operation_id="auth.password.update",
)
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionView]:
    """改密后旧会话失效，随即为当前会话重新签发 Cookie，操作者本人不被踢出。

    失效范围按身份不同（docs/design/member-management.md §3.3）：
    - 超管：轮换全局签名密钥，**所有端**全部下线（密钥可能泄露时的正确行为）；
    - 成员：token_version+1，只踢该成员自己的其他设备。
    """
    if principal.kind == "member" and principal.member_id is not None:
        member = await members_service.change_own_password(
            session,
            principal.member_id,
            old_password=payload.old_password,
            new_password=payload.new_password,
        )
        token, max_age = await auth_service.issue_member_session_token(member)
        _set_session_cookie(response, token, max_age)
        return ok(_member_session_view(member), message="密码已修改，其他设备已全部下线")

    await auth_service.change_password(payload.old_password, payload.new_password)
    token, max_age = await auth_service.issue_session_token(str(principal))
    _set_session_cookie(response, token, max_age)
    return ok(
        _session_view(await auth_service.get_admin_account()),
        message="密码已修改，其他设备已全部下线",
    )


# ---------------------------------------------------------------------------
# CLI API 令牌（PAT）管理。管理员专属——PAT 与管理员完全同权，若允许成员
# 创建即完成提权（docs/design/member-management.md §3.8）。
# ---------------------------------------------------------------------------


@router.post(
    "/tokens",
    response_model=ApiResponse[ApiTokenCreatedView],
    summary="创建 CLI API 令牌（明文仅返回这一次，请立即保存）",
    dependencies=[Depends(require_admin_session)],
    operation_id="auth.tokens.create",
    # 签发凭证只能是人在浏览器里的动作，CLI 调不动，也就不该出现在命令树里
    openapi_extra={"x-cli-hidden": True},
)
async def create_api_token(payload: ApiTokenCreateRequest) -> ApiResponse[ApiTokenCreatedView]:
    plaintext, record = await auth_service.create_api_token(payload.name.strip())
    return ok(
        ApiTokenCreatedView(
            id=record.id, name=record.name, created_at=record.created_at, token=plaintext
        ),
        message="令牌已创建；明文不会再次显示，请立即保存",
    )


@router.get(
    "/tokens",
    response_model=ApiResponse[list[ApiTokenView]],
    summary="列出已创建的 CLI API 令牌（仅元信息，不含明文）",
    dependencies=[Depends(require_admin_session)],
    operation_id="auth.tokens.list",
    openapi_extra={"x-cli-hidden": True},
)
async def list_api_tokens() -> ApiResponse[list[ApiTokenView]]:
    records = await auth_service.list_api_tokens()
    return ok(
        [
            ApiTokenView(
                id=r.id,
                name=r.name,
                created_at=r.created_at,
                client_type=r.client_type,
                last_used_at=r.last_used_at,
            )
            for r in records
        ]
    )


@router.delete(
    "/tokens/{token_id}",
    response_model=ApiResponse[None],
    summary="吊销一枚 CLI API 令牌（立即失效，不影响其他令牌）",
    dependencies=[Depends(require_admin_session)],
    operation_id="auth.tokens.revoke",
    openapi_extra={"x-cli-dangerous": "confirm", "x-cli-hidden": True},
)
async def revoke_api_token(token_id: str) -> ApiResponse[None]:
    if not await auth_service.revoke_api_token(token_id):
        raise NotFoundException("令牌不存在或已被吊销")
    return ok(None, message="令牌已吊销")


# ---------------------------------------------------------------------------
# 设备授权：客户端出示配对码，人在网页上批准
# ---------------------------------------------------------------------------
#
# 两个匿名端点是本次唯一新增的匿名可达面（已在 tests/api/test_auth.py 的
# 公开白名单里登记）。它们必须匿名——设备在拿到令牌之前无凭可用；防滥用靠
# 服务层的三道约束：单 IP 未决请求上限、轮询退避、挑战全程不落库。
# 设计见 docs/design/device-auth.md §2。


async def _verification_uri(request: Request) -> str:
    """用户应当打开的网页地址。

    优先用配置好的「外部访问地址」——那是用户平时访问 movieclaw 的地址，
    也是他浏览器里已经登录着的那个源。没配置时回落到本次请求的地址：
    设备既然能连上这里，同一局域网的浏览器多半也能。
    """
    setting = await get_setting_store().get(AppServerSetting)
    base = (setting.external_url or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/settings/devices"


@router.post(
    "/device/authorize",
    response_model=ApiResponse[DeviceAuthorizeView],
    summary="设备发起接入请求，取得配对码（匿名）",
    operation_id="auth.device.authorize",
    openapi_extra={"x-cli-hidden": True},
)
async def authorize_device(
    payload: DeviceAuthorizeRequest, request: Request
) -> ApiResponse[DeviceAuthorizeView]:
    """受理一次接入请求。客户端不声明权限，能做什么由批准者决定。

    x-cli-hidden：这是客户端之间的协议端点，不该出现在 CLI 命令树里——
    用户面对的是 ``mclaw login``，而不是手工拼装配对流程。
    """
    device_code, challenge = auth_service.authorize_device(
        client_type=payload.client_type,
        client_name=payload.client_name,
        source_ip=client_address(request),
    )
    return ok(
        DeviceAuthorizeView(
            user_code=challenge.user_code,
            device_code=device_code,
            verification_uri=await _verification_uri(request),
            interval=auth_service.DEVICE_POLL_INTERVAL_SECONDS,
            expires_in=auth_service.DEVICE_CODE_TTL_SECONDS,
        ),
        message="请在浏览器里核对配对码并批准",
    )


@router.post(
    "/device/token",
    # data 可空：202「等待批准」与 429「轮询过快」都是成功响应，只是还没有令牌
    response_model=ApiResponse[DeviceTokenView | None],
    summary="设备轮询兑换令牌（匿名）",
    operation_id="auth.device.token",
    openapi_extra={"x-cli-hidden": True},
)
async def redeem_device_token(
    payload: DeviceTokenRequest, response: Response
) -> ApiResponse[DeviceTokenView | None]:
    """轮询兑换。四种结论各自对应明确的 HTTP 语义，客户端据此决定继续还是停止。

    - 202 尚未批准，按 interval 继续轮询；
    - 429 轮询过快，退避后再来（挑战不作废，正常重试不该被当成攻击）；
    - 200 已批准，令牌明文仅此一次，挑战立即作废；
    - 400 已拒绝 / 已过期 / 不存在——**停止轮询**，重新发起。
    """
    result = await auth_service.redeem_device_code(payload.device_code)

    if result.status == "pending":
        response.status_code = 202
        return ok(None, code="AUTHORIZATION_PENDING", message="等待用户在浏览器里批准")
    if result.status == "slow_down":
        response.status_code = 429
        return ok(None, code="SLOW_DOWN", message="轮询过快，请按 interval 退避后重试")
    if result.status == "denied":
        raise BadRequestException("接入请求已被拒绝，请重新发起配对")
    if result.status == "expired":
        raise BadRequestException("配对码已过期或不存在，请重新发起配对")

    assert result.token is not None and result.record is not None  # status == "granted"
    return ok(
        DeviceTokenView(
            token=result.token,
            client_name=result.record.name,
            client_type=result.record.client_type,
            granted_by=result.record.owner_kind,
        ),
        message="配对成功；令牌明文不会再次显示，请立即保存",
    )


@router.get(
    "/devices/requests",
    response_model=ApiResponse[list[DeviceRequestView]],
    summary="列出待批准的设备接入请求",
    dependencies=[Depends(require_admin_session)],
    operation_id="auth.devices.requests",
    openapi_extra={"x-cli-hidden": True},
)
async def list_device_requests() -> ApiResponse[list[DeviceRequestView]]:
    """网页审批卡的数据源。当前只有超管能看、能批准（device-auth.md §4.4）。"""
    now = time.monotonic()
    return ok(
        [
            DeviceRequestView(
                user_code=ch.user_code,
                client_type=ch.client_type,
                client_name=ch.client_name,
                source_ip=ch.source_ip,
                expires_in=max(0, int(ch.expires_at - now)),
            )
            for ch in auth_service.list_device_requests()
        ]
    )


@router.post(
    "/devices/requests/{user_code}/approve",
    response_model=ApiResponse[None],
    summary="批准一台设备接入（此刻才签发令牌）",
    dependencies=[Depends(require_admin_session)],
    operation_id="auth.devices.approve",
    openapi_extra={"x-cli-hidden": True},
)
async def approve_device_request(user_code: str) -> ApiResponse[None]:
    """批准前请核对配对码与设备上显示的一致——这是防钓鱼的唯一一道人工闸。"""
    challenge = await auth_service.approve_device_request(user_code)
    return ok(None, message=f"已批准「{challenge.client_name}」接入")


@router.post(
    "/devices/requests/{user_code}/deny",
    response_model=ApiResponse[None],
    summary="拒绝一台设备接入",
    dependencies=[Depends(require_admin_session)],
    operation_id="auth.devices.deny",
    openapi_extra={"x-cli-hidden": True},
)
async def deny_device_request(user_code: str) -> ApiResponse[None]:
    """拒绝不生成任何令牌，也不在磁盘上留痕。"""
    challenge = auth_service.deny_device_request(user_code)
    return ok(None, message=f"已拒绝「{challenge.client_name}」的接入请求")
