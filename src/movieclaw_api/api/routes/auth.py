"""登录鉴权路由：首次初始化、登录、登出、会话查询、修改密码。

安全分区（与 api/router.py 的三分区对应）：
- 公开：GET/POST /auth/bootstrap、POST /auth/login、POST /auth/logout。
  其中 POST /auth/bootstrap 由服务层的一次性锁自我封闭（管理员已存在即 409），
  logout 只是清 Cookie，无需登录也无危害（会话过期后也能顺利登出）。
- 登录后：GET /auth/me、PUT /auth/password、PUT /auth/profile、
  POST/GET /auth/avatar（头像上传与读取；头像属于个人信息，读取也要求登录，
  同源部署下 <img> 自动携带会话 Cookie，前端零改造）、
  GET/POST/DELETE /auth/accounts*（多账号列表 / 切换 / 移除）。

会话凭证放 HttpOnly Cookie（同源部署下前端零改造自动携带；XSS 偷不走，
SameSite=Lax 挡跨站请求伪造）。

多账号（docs/design/account-switching.md）：除激活会话 Cookie 外，还有一个同样
HttpOnly 的"账号袋" Cookie 装着本浏览器登录过的全部会话令牌。登录总是并入袋子，
切换账号就是把袋子里的一枚令牌写回激活 Cookie——鉴权层只认激活 Cookie，零改动。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.client_address import client_address
from movieclaw_api.api.deps import require_admin_session, require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.auth import (
    AccountView,
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
    LogoutRequest,
    SessionCapabilities,
    SessionView,
    SwitchAccountRequest,
    UpdateProfileRequest,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services import avatar as avatar_media
from movieclaw_api.services import members as members_service
from movieclaw_api.services.auth import Principal, SavedAccount
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


async def _set_accounts_cookie(response: Response, accounts: list[SavedAccount]) -> None:
    """写账号袋 Cookie。空列表直接删掉 Cookie，不留一个签过名的空袋子。

    有效期取"记住我"的 30 天上限：袋子里每枚令牌自带过期时间，袋子本身活得
    久一点既无意义也无危害，但活得短会把仍然有效的令牌提前丢掉。
    """
    if not accounts:
        response.delete_cookie(auth_service.ACCOUNTS_COOKIE_NAME, path="/")
        return
    response.set_cookie(
        key=auth_service.ACCOUNTS_COOKIE_NAME,
        value=await auth_service.encode_saved_accounts(accounts),
        max_age=auth_service.SESSION_TTL_REMEMBER_SECONDS,
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
        path="/",
    )


async def _saved_accounts(request: Request) -> list[SavedAccount]:
    """解析本浏览器持有的全部有效账号（激活账号排第一）。"""
    return await auth_service.resolve_saved_accounts(
        request.cookies.get(auth_service.ACCOUNTS_COOKIE_NAME),
        request.cookies.get(auth_service.SESSION_COOKIE_NAME),
    )


async def _remember_login(
    request: Request, response: Response, token: str, max_age: int, principal: Principal
) -> None:
    """登录成功后的统一收尾：种激活 Cookie，并把新账号并入账号袋。

    普通登录与"添加账号"在后端没有区别——会话过期后重新登录，袋子里其余账号
    照样保留，这正是用户期望的行为。
    """
    _set_session_cookie(response, token, max_age)
    accounts = auth_service.merge_saved_account(await _saved_accounts(request), token, principal)
    await _set_accounts_cookie(response, accounts)


async def _activate(
    response: Response, accounts: list[SavedAccount], target: SavedAccount | None
) -> SessionView | None:
    """把 target 写成激活会话并回写袋子；target 为空表示袋子已空，清掉两个 Cookie。

    切换时激活 Cookie 的 max_age 统一给 30 天：真正的有效期由令牌内的过期
    时间戳决定（过期照样 401 → 登录页），Cookie 多活几天没有危害；而给短了
    会把"记住我"登录的账号提前踢掉。
    """
    if target is None:
        response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
        await _set_accounts_cookie(response, [])
        return None
    _set_session_cookie(response, target.token, auth_service.SESSION_TTL_REMEMBER_SECONDS)
    ordered = [target, *[a for a in accounts if a.token != target.token]]
    await _set_accounts_cookie(response, ordered)
    return await _principal_session_view(target.principal)


async def _account_view(saved: SavedAccount, *, active: bool) -> AccountView:
    """账号袋里的一个账号 → 列表项。头像地址带 account 参数，让 <img> 能读到
    非激活账号的头像（GET /auth/avatar?account=）。"""
    view = await _principal_session_view(saved.principal)
    avatar_url = view.avatar_url
    if avatar_url is not None:
        avatar_url = f"{avatar_url}&account={view.username}"
    return AccountView(
        username=view.username,
        nickname=view.nickname,
        avatar_url=avatar_url,
        role=view.role,
        active=active,
    )


def _find_account(accounts: list[SavedAccount], username: str) -> SavedAccount | None:
    """按用户名在袋子里找账号（用户名在超管与成员间全局唯一，大小写不敏感）。"""
    wanted = username.strip().lower()
    for saved in accounts:
        if saved.principal.name.lower() == wanted:
            return saved
    return None


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
    payload: BootstrapRequest, request: Request, response: Response
) -> ApiResponse[SessionView]:
    """创建管理员并自动登录。管理员已存在时一律 409，锁在服务端，不可绕过。"""
    account = await auth_service.create_admin(payload.username, payload.password)
    await mark_initialized()

    token, max_age = await auth_service.issue_session_token(account.username)
    await _remember_login(
        request, response, token, max_age, Principal(kind="admin", name=account.username)
    )
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
async def login(
    payload: LoginRequest, request: Request, response: Response
) -> ApiResponse[SessionView]:
    """校验账号密码并种下会话 Cookie（超管或成员）。连续失败触发限速（429）。

    登录成功的账号同时并入账号袋（docs/design/account-switching.md §3）：
    浏览器里其余已登录账号原样保留，用户菜单里可一键切换。
    """
    identity = await auth_service.authenticate(payload.username, payload.password)
    if isinstance(identity, Member):
        token, max_age = await auth_service.issue_member_session_token(
            identity, remember=payload.remember
        )
        principal = Principal(
            kind="member",
            name=identity.username,
            member_id=identity.id,
            is_admin=False,
            member=identity,
        )
        await _remember_login(request, response, token, max_age, principal)
        return ok(_member_session_view(identity), message="登录成功")

    token, max_age = await auth_service.issue_session_token(
        identity.username, remember=payload.remember
    )
    await _remember_login(
        request, response, token, max_age, Principal(kind="admin", name=identity.username)
    )
    return ok(_session_view(identity), message="登录成功")


@router.post(
    "/logout",
    response_model=ApiResponse[SessionView | None],
    summary="退出当前账号（自动切到浏览器里的下一个账号）；all=true 退出全部",
    operation_id="auth.logout",
    # CLI 侧登录/登出由精选命令 mclaw login/logout 负责（要持久化本地凭证），
    # 生成层隐藏本端点避免出现语义不完整的同名命令
    openapi_extra={"x-cli-hidden": True},
)
async def logout(
    request: Request, response: Response, payload: LogoutRequest | None = None
) -> ApiResponse[SessionView | None]:
    """退出当前账号。无需登录态即可调用（会话已过期时也能正常登出）。

    返回体是退出后浏览器所处的账号：袋子里还有别的账号就自动切过去并返回它，
    空则返回 null（前端据此决定回首页还是去登录页）。``all=true`` 清空全部。
    """
    if payload is not None and payload.all:
        await _activate(response, [], None)
        return ok(None, message="已退出全部账号")

    active_token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    remaining = [a for a in await _saved_accounts(request) if a.token != active_token]
    view = await _activate(response, remaining, remaining[0] if remaining else None)
    if view is None:
        return ok(None, message="已退出登录")
    return ok(view, message=f"已退出登录，已切换到 {view.nickname}")


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
async def read_avatar(
    request: Request,
    principal: Principal = Depends(require_login),
    account: str | None = Query(default=None, description="读取账号袋里某个账号的头像"),
) -> FileResponse:
    """直接返回当前主体的头像本体，供 <img> 加载；地址由会话视图的 avatar_url 给出。

    带 ``account`` 时读取的是账号袋里另一个账号的头像——只有本浏览器确实持有
    该账号的登录态才能读到，天然按持有者隔离，不会变成"按用户名查任何人头像"。
    """
    if account is not None:
        saved = _find_account(await _saved_accounts(request), account)
        if saved is None:
            raise NotFoundException("尚未上传头像")
        principal = saved.principal
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
    request: Request,
    response: Response,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionView]:
    """改密后旧会话失效，随即为当前会话重新签发 Cookie，操作者本人不被踢出。

    失效范围按身份不同（docs/design/member-management.md §3.3）：
    - 超管：轮换全局签名密钥，**所有端**全部下线（密钥可能泄露时的正确行为）；
    - 成员：token_version+1，只踢该成员自己的其他设备。

    账号袋同步重写（新令牌置顶）：成员改密不影响袋子里的其他账号；超管改密
    轮换了密钥，袋子里其余令牌随之失效，重写后只剩超管自己。
    """
    if principal.kind == "member" and principal.member_id is not None:
        member = await members_service.change_own_password(
            session,
            principal.member_id,
            old_password=payload.old_password,
            new_password=payload.new_password,
        )
        token, max_age = await auth_service.issue_member_session_token(member)
        refreshed = Principal(
            kind="member",
            name=member.username,
            member_id=member.id,
            is_admin=False,
            member=member,
        )
        await _remember_login(request, response, token, max_age, refreshed)
        return ok(_member_session_view(member), message="密码已修改，其他设备已全部下线")

    await auth_service.change_password(payload.old_password, payload.new_password)
    token, max_age = await auth_service.issue_session_token(str(principal))
    await _remember_login(
        request, response, token, max_age, Principal(kind="admin", name=str(principal))
    )
    return ok(
        _session_view(await auth_service.get_admin_account()),
        message="密码已修改，其他设备已全部下线",
    )


# ---------------------------------------------------------------------------
# 多账号：列表 / 切换 / 移除（docs/design/account-switching.md §3）
# ---------------------------------------------------------------------------
# 三个接口都只操作本浏览器的两个 Cookie，不碰数据库里的任何账号数据；成员
# 也可用（已登记进成员白名单守护测试）。


@router.get(
    "/accounts",
    response_model=ApiResponse[list[AccountView]],
    summary="列出本浏览器已登录的全部账号（激活账号排第一）",
    dependencies=[Depends(require_login)],
    operation_id="auth.accounts.list",
    openapi_extra={"x-cli-hidden": True},
)
async def list_accounts(request: Request) -> ApiResponse[list[AccountView]]:
    accounts = await _saved_accounts(request)
    return ok(
        [await _account_view(saved, active=index == 0) for index, saved in enumerate(accounts)]
    )


@router.post(
    "/accounts/switch",
    response_model=ApiResponse[SessionView],
    summary="切换到本浏览器已登录的另一个账号（无需再输密码）",
    dependencies=[Depends(require_login)],
    operation_id="auth.accounts.switch",
    openapi_extra={"x-cli-hidden": True},
)
async def switch_account(
    payload: SwitchAccountRequest, request: Request, response: Response
) -> ApiResponse[SessionView]:
    """把袋子里对应的令牌写回激活 Cookie。目标账号的登录态已失效（过期 /
    被停用 / 改密）时返回 404，前端引导用户重新登录该账号。"""
    accounts = await _saved_accounts(request)
    target = _find_account(accounts, payload.username)
    if target is None:
        raise NotFoundException("该账号的登录状态已失效，请重新登录该账号")
    view = await _activate(response, accounts, target)
    assert view is not None  # target 非空时 _activate 必有返回
    return ok(view, message=f"已切换到 {view.nickname}")


@router.delete(
    "/accounts/{username}",
    response_model=ApiResponse[SessionView | None],
    summary="从本浏览器移除一个已登录账号（移除的是当前账号时自动切到下一个）",
    dependencies=[Depends(require_login)],
    operation_id="auth.accounts.remove",
    # confirm：只是让本浏览器忘掉一个登录态，不删任何数据；契约测试要求所有 DELETE 都声明
    openapi_extra={"x-cli-dangerous": "confirm", "x-cli-hidden": True},
)
async def remove_account(
    username: str, request: Request, response: Response
) -> ApiResponse[SessionView | None]:
    """返回体语义与 /auth/logout 相同：移除后浏览器所处的账号，null 表示已全部退出。"""
    accounts = await _saved_accounts(request)
    target = _find_account(accounts, username)
    if target is None:
        raise NotFoundException("该账号不在本浏览器的已登录列表里")
    remaining = [a for a in accounts if a.token != target.token]
    view = await _activate(response, remaining, remaining[0] if remaining else None)
    return ok(view, message="已移除该账号")


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
