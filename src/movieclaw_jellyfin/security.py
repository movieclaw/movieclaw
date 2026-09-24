"""Jellyfin 兼容层的认证：token 提取与设备凭据校验（设计文档 4.1/4.3）。

token 的全部合法位置（一律无条件支持；上游 12.x 默认关掉了旧式写法，
我们照收以兼容老播放器）：
Authorization / X-Emby-Authorization 头的 ``Token=``、X-Emby-Token、
X-MediaBrowser-Token、``?ApiKey=``、``?api_key=``。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import JellyfinDevice
from movieclaw_db.models.base import utcnow
from movieclaw_jellyfin.authhdr import AuthorizationInfo, parse_authorization_header
from movieclaw_jellyfin.errors import unauthorized


@dataclass(frozen=True)
class RequestIdentity:
    """一次请求解析出的客户端身份（token 已验证）。"""

    device: JellyfinDevice
    auth: AuthorizationInfo


def read_authorization(request: Request) -> AuthorizationInfo:
    """解析客户端标识头；两个头都没有/不合法时返回空信息（不报错）。"""
    info = parse_authorization_header(request.headers.get("Authorization"))
    if info is None:
        info = parse_authorization_header(request.headers.get("X-Emby-Authorization"))
    return info or AuthorizationInfo()


def extract_token(request: Request) -> str:
    """按 Jellyfin 的优先级顺序从各位置提取 token；找不到返回空串。"""
    info = read_authorization(request)
    if info.token:
        return info.token
    for header in ("X-Emby-Token", "X-MediaBrowser-Token"):
        value = request.headers.get(header)
        if value:
            return value
    # query 的 ApiKey：Starlette QueryParams 区分大小写，两种拼写都要认
    for key in ("ApiKey", "api_key"):
        value = request.query_params.get(key)
        if value:
            return value
    return ""


async def require_device(request: Request) -> RequestIdentity:
    """认证依赖：token 无效/缺失 → 401 空 body（认证管道形态）。"""
    token = extract_token(request)
    if not token:
        raise unauthorized()
    async with get_database().session() as session:
        device = (
            await session.execute(
                select(JellyfinDevice).where(JellyfinDevice.token == token)
            )
        ).scalar_one_or_none()
        if device is None:
            raise unauthorized()
        device.last_seen_at = utcnow()
        await session.commit()
    return RequestIdentity(device=device, auth=read_authorization(request))
