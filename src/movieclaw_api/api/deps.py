from __future__ import annotations

import hmac

from fastapi import Cookie, Depends, Header

from movieclaw_api.exceptions import ForbiddenException, UnauthorizedException
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.auth import Principal
from movieclaw_api.settings.schemas import get_sync_setting


def _extract_bearer(authorization: str | None) -> str | None:
    """从 Authorization 头中取出 Bearer 令牌；格式不符返回 None。"""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


async def require_sync_token(authorization: str | None = Header(default=None)) -> None:
    """插件侧接口的鉴权依赖：校验请求头里的同步令牌。

    校验流程：
    1. 后端从未生成令牌（同步未启用）→ 401，提示先去后台生成令牌。
    2. 请求未带 Bearer 令牌或与后端不一致 → 401，提示令牌无效/已重置。

    比较使用 ``hmac.compare_digest`` 做常量时间比较，避免时序侧信道。
    错误信息为清晰中文，方便非开发者按提示操作。
    """
    setting = await get_sync_setting()
    if not setting.token:
        raise UnauthorizedException("后端未启用同步，请先在后台生成令牌")

    provided = _extract_bearer(authorization)
    if not provided or not hmac.compare_digest(provided, setting.token):
        raise UnauthorizedException("令牌无效或已重置，请重新填写")


async def optional_login(
    session_token: str | None = Cookie(default=None, alias=auth_service.SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> Principal | None:
    """解析可选登录主体；未携带凭据返回 None，携带无效凭据仍返回 401。

    仅用于少量“匿名可读、登录后按账号分流”的接口，例如背景图库。不能用它
    替代业务接口的 ``require_login``，否则会把默认拒绝边界改成匿名放行。
    """
    if session_token:
        return await auth_service.verify_session_token(session_token)
    if bearer := _extract_bearer(authorization):
        return await auth_service.verify_bearer_token(bearer)
    return None


async def require_login(
    principal: Principal | None = Depends(optional_login),
) -> Principal:
    """业务接口的登录鉴权依赖：会话 Cookie **或** Bearer 令牌，返回请求主体。

    两条通道（docs/design/cli.md §8.1）：
    - Web 端：会话 Cookie（超管或成员，由令牌负载区分）；
    - CLI / 产品内 Agent：``Authorization: Bearer <令牌>``——PAT 长期令牌
      或 Agent 短时效签名令牌，同一验签入口。

    全站默认拒绝的执行点——除公开白名单与插件侧接口外，所有路由都必须挂
    本依赖（api/router.py 按组挂载，tests 里有守护测试兜底防漏挂）。
    未登录 / 会话过期 / 令牌无效统一 401。授权（管理员/能力开关）不在
    这里判——挂 ``require_admin`` 或在服务层消费 Principal。

    唯一的例外是**转码 Worker 的形态上限**（docs/design/device-auth.md §4.3）：
    Worker 令牌只为转码链路签发，在这里直接拒绝。这样做是默认拒绝而不是白名单
    枚举——新增的任何业务路由只要照常挂本依赖，就自动把 Worker 令牌挡在外面，
    不需要记得给它加标注。放行 Worker 的白名单只有一处：``require_transcode_worker``。
    """
    if principal is None:
        raise UnauthorizedException("未登录，请先登录")
    if principal.client_type == "worker":
        raise ForbiddenException("转码 Worker 的凭证只能用于转码，不能访问业务接口")
    return principal


async def resolve_worker_principal(authorization: str | None) -> Principal | None:
    """从 Authorization 头解析转码 Worker 主体；不是 Worker 令牌一律返回 None。

    抽成普通函数是因为 WebSocket 与 HTTP 两条入口要共用它：WS 握手不能抛
    HTTPException（只能关连接并给关闭码），FastAPI 的依赖那套在那里用不上。
    判定逻辑只此一份，两边不会走偏。
    """
    token = _extract_bearer(authorization)
    if not token:
        return None
    try:
        principal = await auth_service.verify_bearer_token(token)
    except UnauthorizedException:
        return None
    return principal if principal.client_type == "worker" else None


async def require_transcode_worker(
    authorization: str | None = Header(default=None),
) -> Principal:
    """转码控制面专用：只接受 Worker 令牌。

    与 ``require_login`` 互补——业务接口拒绝 Worker，转码接口只认 Worker。
    两边都是显式判定，不存在「既能转码又能改订阅」的凭证。
    """
    principal = await resolve_worker_principal(authorization)
    if principal is None:
        raise UnauthorizedException("需要转码 Worker 凭证，请在 Worker 应用里完成配对")
    return principal


async def require_admin(principal: Principal = Depends(require_login)) -> Principal:
    """管理员鉴权依赖：在登录之上断言管理员身份，成员访问一律 403。

    与守护测试的契约（tests/api/test_member_auth.py）：不在成员白名单里的
    路由必须由本依赖（或服务层等价判定）挡住成员——新增管理路由挂本依赖
    即自动满足契约。
    """
    if not principal.is_admin:
        raise ForbiddenException("该操作需要管理员权限")
    return principal


async def require_admin_session(
    principal: Principal = Depends(require_admin),
) -> Principal:
    """在管理员之上再要求「这是人在浏览器里操作」——只接受会话 Cookie 主体。

    用途只有一个：**凭证的签发与吊销**（创建/吊销令牌、批准/拒绝设备接入）。

    为什么不能让 Bearer 令牌调这些接口：设备令牌一旦能签发新令牌，就能给自己
    造一枚备份，吊销原来那枚也止不住损——而吊销是这套设计唯一的事后止损手段
    （docs/design/device-auth.md §8）。把签发闸门收在「人 + 浏览器」上，
    泄漏的令牌就无法自我复制，也无法把别的机器拉进来。

    Agent 工作区令牌同样被挡住，这正是想要的：Agent 不该能给自己续命。
    """
    if principal.kind != "admin":
        raise ForbiddenException(
            "签发与吊销凭证只能在网页上完成，请用管理员账号登录 movieclaw 后操作"
        )
    return principal


async def require_search_capability(
    principal: Principal = Depends(require_login),
) -> Principal:
    """站点搜索能力依赖：管理员直通；成员须开启 ``allow_search`` 开关。

    搜索消耗站点配额、暴露站点存在，因此默认对成员关闭，由管理员在成员
    管理页逐人开启（docs/design/member-management.md §2.2）。
    """
    if principal.is_admin:
        return principal
    if principal.member is None or not principal.member.allow_search:
        raise ForbiddenException("管理员未对你开放站点搜索，请联系管理员开启")
    return principal


async def require_subscribe_capability(
    principal: Principal = Depends(require_login),
) -> Principal:
    """订阅能力依赖：管理员直通；成员须开启 ``allow_subscribe`` 开关。"""
    if principal.is_admin:
        return principal
    if principal.member is None or not principal.member.allow_subscribe:
        raise ForbiddenException("管理员未对你开放订阅功能，请联系管理员开启")
    return principal


async def require_direct_download_capability(
    principal: Principal = Depends(require_login),
) -> Principal:
    """一键下载能力依赖：管理员直通；成员须开启 ``allow_direct_download``。

    成员版一键下载还会在服务端强制自动路由（拒绝手选 save_path），
    见 routes/downloaders.py 的 submit 处理器。
    """
    if principal.is_admin:
        return principal
    if principal.member is None or not principal.member.allow_direct_download:
        raise ForbiddenException("管理员未对你开放一键下载，请联系管理员开启")
    return principal
