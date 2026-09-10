"""成员管理路由（管理员专属，挂载于 api/router.py 的管理区）。

页面形态见 docs/design/member-management.md §3.9.1：列表 → 详情两层，
权限（能力开关 + 库/站点白名单）的写入口唯一在这里——库页面零改动。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.schemas.member import (
    MemberCreateRequest,
    MemberPasswordResetView,
    MemberStatusRequest,
    MemberUpdateRequest,
    MemberView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import avatar as avatar_media
from movieclaw_api.services import members as members_service
from movieclaw_db.engine import get_session
from movieclaw_db.models.member import Member
from movieclaw_db.repositories.member_repo import MemberRepository

router = APIRouter(prefix="/members", tags=["members"])


def _member_avatar_url(member_id: int) -> str | None:
    """成员头像的带版本号相对地址；未上传时 None（前端显示首字徽标）。"""
    version = avatar_media.avatar_version(avatar_media.member_stem(member_id))
    if version is None:
        return None
    return f"{get_settings().api_v1_prefix}/members/{member_id}/avatar?v={version}"


async def _member_view(session: AsyncSession, member: Member) -> MemberView:
    repo = MemberRepository(session)
    return MemberView(
        id=member.id,
        username=member.username,
        nickname=member.nickname or member.username,
        avatar_url=_member_avatar_url(member.id),
        status=member.status,
        last_login_at=member.last_login_at,
        allow_subscribe=member.allow_subscribe,
        allow_search=member.allow_search,
        allow_direct_download=member.allow_direct_download,
        all_libraries=member.all_libraries,
        library_ids=await repo.get_library_ids(member.id),
        all_sites=member.all_sites,
        site_ids=await repo.get_site_ids(member.id),
        content_age_limit=member.content_age_limit,
        allow_unrated=member.allow_unrated,
        created_at=member.created_at,
    )


@router.get(
    "",
    response_model=ApiResponse[list[MemberView]],
    summary="成员列表（含能力开关与白名单摘要）",
    operation_id="members.list",
)
async def list_members(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[MemberView]]:
    members = await MemberRepository(session).list_all()
    return ok([await _member_view(session, m) for m in members])


@router.post(
    "",
    response_model=ApiResponse[MemberView],
    summary="新建成员（把用户名和初始密码发给对方，登录后可自行修改）",
    operation_id="members.create",
)
async def create_member(
    payload: MemberCreateRequest, session: AsyncSession = Depends(get_session)
) -> ApiResponse[MemberView]:
    member = await members_service.create_member(
        session,
        username=payload.username,
        password=payload.password,
        nickname=payload.nickname,
    )
    return ok(await _member_view(session, member), message="成员已创建，请把账号密码发给对方")


@router.get(
    "/{member_id}",
    response_model=ApiResponse[MemberView],
    summary="成员详情",
    operation_id="members.show",
)
async def get_member(
    member_id: int, session: AsyncSession = Depends(get_session)
) -> ApiResponse[MemberView]:
    member = await members_service.get_member(session, member_id)
    return ok(await _member_view(session, member))


@router.put(
    "/{member_id}",
    response_model=ApiResponse[MemberView],
    summary="编辑成员（昵称 / 能力开关 / 可见库与可用站点白名单）",
    operation_id="members.update",
)
async def update_member(
    member_id: int,
    payload: MemberUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[MemberView]:
    member = await members_service.update_member(
        session,
        member_id,
        nickname=payload.nickname,
        allow_subscribe=payload.allow_subscribe,
        allow_search=payload.allow_search,
        allow_direct_download=payload.allow_direct_download,
        all_libraries=payload.all_libraries,
        library_ids=payload.library_ids,
        all_sites=payload.all_sites,
        site_ids=payload.site_ids,
        content_age_limit=payload.content_age_limit,
        allow_unrated=payload.allow_unrated,
    )
    return ok(await _member_view(session, member), message="成员设置已保存")


@router.put(
    "/{member_id}/status",
    response_model=ApiResponse[MemberView],
    summary="启用 / 停用成员（停用即时踢下线，数据全部保留）",
    operation_id="members.status.set",
)
async def set_member_status(
    member_id: int,
    payload: MemberStatusRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[MemberView]:
    member = await members_service.set_member_status(session, member_id, enabled=payload.enabled)
    message = "成员已启用" if payload.enabled else "成员已停用，其所有设备已下线"
    return ok(await _member_view(session, member), message=message)


@router.post(
    "/{member_id}/reset-password",
    response_model=ApiResponse[MemberPasswordResetView],
    summary="重置成员密码（新密码明文仅返回这一次）",
    operation_id="members.password.reset",
)
async def reset_member_password(
    member_id: int, session: AsyncSession = Depends(get_session)
) -> ApiResponse[MemberPasswordResetView]:
    member, plaintext = await members_service.reset_member_password(session, member_id)
    return ok(
        MemberPasswordResetView(id=member.id, username=member.username, password=plaintext),
        message="密码已重置，请立即复制发给成员；旧会话已全部下线",
    )


@router.delete(
    "/{member_id}",
    response_model=ApiResponse[None],
    summary="删除成员（清理其个人数据；订阅与已下载内容保留）",
    operation_id="members.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_member(
    member_id: int, session: AsyncSession = Depends(get_session)
) -> ApiResponse[None]:
    await members_service.delete_member(session, member_id)
    return ok(None, message="成员已删除；其个人数据已清理，公共资源不受影响")


@router.get(
    "/{member_id}/avatar",
    summary="读取成员头像（成员列表展示用）",
    operation_id="members.avatar.download",
)
async def read_member_avatar(member_id: int) -> FileResponse:
    """返回成员头像本体。挂在管理区，仅管理员可读（成员本人经 /auth/avatar 读取）。"""
    path = avatar_media.find_avatar(avatar_media.member_stem(member_id))
    if path is None:
        raise NotFoundException("该成员尚未上传头像")
    return FileResponse(
        path,
        media_type=avatar_media.content_type_for(path),
        headers={"Cache-Control": "private, max-age=31536000"},
    )
