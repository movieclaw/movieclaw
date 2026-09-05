"""认证与用户接口（设计文档 4.2/4.3）。"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.library.access import member_visible_ids
from movieclaw_api.settings.schemas import get_jellyfin_compat
from movieclaw_db.engine import get_database
from movieclaw_db.models import JellyfinDevice
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.member import Member
from movieclaw_db.repositories.member_repo import MemberRepository
from movieclaw_jellyfin.errors import (
    JellyfinError,
    bad_request_text,
    not_found_message,
)
from movieclaw_jellyfin.identity import session_info_dto, user_dto
from movieclaw_jellyfin.ids import (
    normalize_guid,
    user_guid_for,
)
from movieclaw_jellyfin.security import RequestIdentity, read_authorization, require_device

router = APIRouter()


@router.post("/Users/AuthenticateByName")
async def authenticate_by_name(request: Request) -> JSONResponse:
    auth = read_authorization(request)
    # 四键缺任一 → 400 text/plain（对齐 SessionManager 的 ArgumentException 链）
    if not auth.has_identity:
        raise bad_request_text()

    try:
        body = await request.json()
    except Exception:
        raise bad_request_text() from None
    if not isinstance(body, dict):
        raise bad_request_text()
    # 请求体键名大小写不敏感（MVC JsonSerializerDefaults.Web 行为）
    lowered = {str(k).lower(): v for k, v in body.items()}
    username = lowered.get("username") or ""
    password = lowered.get("pw") or ""
    if not username:
        raise bad_request_text()

    try:
        identity = await auth_service.authenticate(username, str(password))
    except Exception:
        # 密码错/账号不存在 → 401 text/plain "Error processing request."
        raise JellyfinError(401, text="Error processing request.") from None
    member = identity if isinstance(identity, Member) else None
    member_id = member.id if member is not None else 0

    setting = await get_jellyfin_compat()
    token = secrets.token_hex(16)
    async with get_database().session() as session:
        # 同 device_id 重登录：覆盖并换发 token（旧 token 即刻失效，防凭据累积）
        device = (
            await session.execute(
                select(JellyfinDevice).where(JellyfinDevice.device_id == auth.device_id)
            )
        ).scalar_one_or_none()
        if device is None:
            device = JellyfinDevice(token=token, device_id=auth.device_id)
            session.add(device)
        else:
            device.token = token
        # 设备绑定登录身份：换人登录同一设备 → 行覆盖 + 身份改写，
        # 之后该设备的 Policy、观看状态、可见库全部按新身份投影
        device.member_id = member_id
        device.client = auth.client
        device.device_name = auth.device
        device.version = auth.version
        device.last_seen_at = utcnow()
        device.updated_at = utcnow()
        await session.commit()

    # 超管与成员都按可浏览集投影 EnabledFolders（超管摘掉自己的库也不在电视端出现）
    async with get_database().session() as session:
        visible = await member_visible_ids(session, member_id)
    if member is not None:
        user_payload = await user_dto(setting.server_id, member, visible)
        user_name = member.username
    else:
        user_payload = await user_dto(setting.server_id, visible_library_ids=visible)
        user_name = (await auth_service.get_admin_account()).username
    return JSONResponse(
        {
            "User": user_payload,
            "SessionInfo": session_info_dto(
                setting.server_id,
                secrets.token_hex(16),
                client=auth.client,
                device_id=auth.device_id,
                device_name=auth.device,
                version=auth.version,
                user_name=user_name,
                member_id=member_id,
            ),
            "AccessToken": token,
            "ServerId": setting.server_id,
        }
    )


async def _member_user_dto(server_id: str, member: Member) -> dict:
    async with get_database().session() as session:
        visible = await member_visible_ids(session, member.id)
    return await user_dto(server_id, member, visible)


async def _identity_user_dto(server_id: str, member_id: int) -> dict:
    """按设备登录身份装配用户 DTO；成员行已不存在（竞态）按 404 处理。"""
    if member_id == 0:
        async with get_database().session() as session:
            visible = await member_visible_ids(session, 0)
        return await user_dto(server_id, visible_library_ids=visible)
    async with get_database().session() as session:
        member = await MemberRepository(session).get(member_id)
    if member is None:
        raise not_found_message("User not found")
    return await _member_user_dto(server_id, member)


@router.get("/Users/Public")
async def users_public() -> JSONResponse:
    """登录页用户列表：超管 + 启用中的成员（电视端出现多个头像）。"""
    setting = await get_jellyfin_compat()
    dtos = [await user_dto(setting.server_id)]
    async with get_database().session() as session:
        members = [
            m for m in await MemberRepository(session).list_all() if m.status == "active"
        ]
    for member in members:
        dtos.append(await _member_user_dto(setting.server_id, member))
    return JSONResponse(dtos)


@router.get("/Users/Me")
async def users_me(identity: RequestIdentity = Depends(require_device)) -> JSONResponse:
    setting = await get_jellyfin_compat()
    return JSONResponse(
        await _identity_user_dto(setting.server_id, identity.device.member_id)
    )


@router.get("/Users/{user_id}")
async def users_by_id(
    user_id: str, identity: RequestIdentity = Depends(require_device)
) -> JSONResponse:
    normalized = normalize_guid(user_id)
    if normalized is None:
        # 路由段解析失败是参数错误（400），绝不能 401——会触发客户端登录循环
        raise bad_request_text()
    # 只允许取自己的用户对象：GUID 必须与设备登录身份一致（协议里管理员可查
    # 他人，我们的超管即唯一管理员，同样按本人语义即可满足客户端需求）
    if normalized != user_guid_for(identity.device.member_id):
        raise not_found_message("User not found")
    setting = await get_jellyfin_compat()
    return JSONResponse(
        await _identity_user_dto(setting.server_id, identity.device.member_id)
    )
