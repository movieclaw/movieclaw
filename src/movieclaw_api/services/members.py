"""成员管理服务（docs/design/member-management.md §3.9.1）。

管理员侧：成员的建号/编辑/启停/重置密码/删除，以及库与站点白名单的整体
覆盖保存。成员侧：改自己的昵称与密码。

生命周期语义（与设计文档 §3.9.1 的表格一一对应）：
- 停用：token_version+1 即时踢下线，数据全部保留；
- 删除：清理个人数据（头像；P1 起还包括播放进度、订阅关注），公共资源
  （订阅与下载成果）绝不连带删除。
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
    UnauthorizedException,
)
from movieclaw_api.services import appearance as appearance_media
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services import avatar as avatar_media
from movieclaw_db.models.member import Member
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_db.repositories.member_repo import MemberRepository

logger = logging.getLogger("movieclaw_api.members")


def generate_password() -> str:
    """生成随机初始密码（重置密码用；明文只返回一次，落库只存哈希）。"""
    return secrets.token_urlsafe(12)


async def _assert_username_available(session: AsyncSession, username: str) -> None:
    """建号前校验用户名可用：与超管互斥（大小写不敏感）、与现有成员不重复。"""
    admin = await auth_service.get_admin_account()
    if admin.username and username.strip().lower() == admin.username.strip().lower():
        raise ConflictException("该用户名已被管理员使用，请换一个")
    if await MemberRepository(session).get_by_username(username) is not None:
        raise ConflictException("该用户名已存在，请换一个")


async def get_member(session: AsyncSession, member_id: int) -> Member:
    """按 id 取成员；不存在抛 404。"""
    member = await MemberRepository(session).get(member_id)
    if member is None:
        raise NotFoundException("成员不存在或已被删除")
    return member


async def create_member(
    session: AsyncSession, *, username: str, password: str, nickname: str = ""
) -> Member:
    """新建成员（默认能力开关见 Member 模型：可订阅、不可搜索、全库可见）。"""
    username = username.strip()
    await _assert_username_available(session, username)
    member = await MemberRepository(session).create(
        username=username,
        password_hash=auth_service.hash_password(password),
        nickname=nickname,
    )
    logger.info("已创建成员账号：%s（id=%d）", member.username, member.id)
    return member


async def update_member(
    session: AsyncSession,
    member_id: int,
    *,
    nickname: str | None = None,
    allow_subscribe: bool | None = None,
    allow_search: bool | None = None,
    allow_direct_download: bool | None = None,
    all_libraries: bool | None = None,
    library_ids: list[int] | None = None,
    all_sites: bool | None = None,
    site_ids: list[str] | None = None,
) -> Member:
    """编辑成员（None 字段不改动）。白名单为整体覆盖式保存。"""
    repo = MemberRepository(session)
    member = await get_member(session, member_id)

    # 全部校验先行，之后才落任何写——校验失败时不能留下半套权限
    # （比如 all_libraries 已切白名单模式、白名单却没存上 = 成员瞬间全库不可见）
    if library_ids is not None:
        # 校验库存在性给出友好错误（关联表有外键，无效 id 落库会炸 500）
        existing = {row.id for row in await LibraryRepository(session).list_all()}
        unknown = [i for i in library_ids if i not in existing]
        if unknown:
            raise BadRequestException(f"媒体库不存在：id={unknown}，请刷新后重试")

    if nickname is not None:
        member.nickname = nickname.strip()
    if allow_subscribe is not None:
        member.allow_subscribe = allow_subscribe
    if allow_search is not None:
        member.allow_search = allow_search
    if allow_direct_download is not None:
        member.allow_direct_download = allow_direct_download
    if all_libraries is not None:
        member.all_libraries = all_libraries
    if all_sites is not None:
        member.all_sites = all_sites
    member = await repo.save(member)

    if library_ids is not None:
        await repo.set_library_access(member_id, library_ids)
    if site_ids is not None:
        await repo.set_site_access(member_id, site_ids)
    return member


async def _drop_jellyfin_devices(session: AsyncSession, member_id: int) -> None:
    """删除该成员的全部 Jellyfin 设备凭据。

    协议侧的设备 token 长期有效且无版本机制（jellyfin_device 模型注释），
    停用/删除/改密时直接删行是让电视端即刻失效的唯一手段。
    """
    from sqlalchemy import delete as sa_delete

    from movieclaw_db.models import JellyfinDevice

    await session.execute(sa_delete(JellyfinDevice).where(JellyfinDevice.member_id == member_id))
    await session.commit()


async def set_member_status(session: AsyncSession, member_id: int, *, enabled: bool) -> Member:
    """启用/停用成员。停用即时踢下线（token_version+1 + 删 Jellyfin 设备），
    数据全部保留。"""
    repo = MemberRepository(session)
    member = await get_member(session, member_id)
    member.status = "active" if enabled else "disabled"
    member = await repo.bump_token_version(member)
    if not enabled:
        await _drop_jellyfin_devices(session, member_id)
    logger.info("成员 %s 已%s", member.username, "启用" if enabled else "停用")
    return member


async def reset_member_password(session: AsyncSession, member_id: int) -> tuple[Member, str]:
    """重置成员密码：生成随机新密码并返回明文（仅此一次），旧会话全部失效。"""
    repo = MemberRepository(session)
    member = await get_member(session, member_id)
    plaintext = generate_password()
    member.password_hash = auth_service.hash_password(plaintext)
    member = await repo.bump_token_version(member)
    await _drop_jellyfin_devices(session, member_id)
    logger.info("已重置成员 %s 的密码（旧会话与播放器凭据已全部下线）", member.username)
    return member, plaintext


async def delete_member(session: AsyncSession, member_id: int) -> None:
    """删除成员：清理个人数据，保留公共资源（§3.9.1 生命周期语义）。

    - 头像文件、个人背景图库、Jellyfin 设备：显式清理；
    - 全部**成员级表**（观看状态、搜索历史、下载保存位置记忆……）：遍历
      ``member_scoped_models()`` 注册表清理。这些表的 member_id 是哨兵值而非
      外键，级联指望不上；而漏清一张表的后果不是残留垃圾数据——SQLite 会复用
      已删成员的行 id，新成员建号后将**继承前一个人的数据**，是跨人隐私泄漏。
      过去靠逐行手写、靠"记得加一行"来防，现在靠登记：新增成员级表只要挂
      ``@register_member_scoped``（CI 守卫会拦住漏登记的），清理自动覆盖。
    - 库/站点白名单、订阅关注行：外键级联自动清理；
    - 其发起的订阅：外键 SET NULL 自动转为超管发起——绝不静默删除
      订阅与下载任务，已下载内容不受影响。
    """
    from sqlalchemy import delete as sa_delete

    from movieclaw_db.models import member_scoped_models

    member = await get_member(session, member_id)
    avatar_media.delete_avatar(avatar_media.member_stem(member_id))
    appearance_media.remove_member_gallery(member_id)
    await _drop_jellyfin_devices(session, member_id)
    for model in member_scoped_models():
        await session.execute(sa_delete(model).where(model.member_id == member_id))
    await MemberRepository(session).delete(member)
    logger.info(
        "已删除成员账号：%s（id=%d，个人数据已清理，订阅已转由管理员接管）",
        member.username,
        member_id,
    )


# ---------------------------------------------------------------------------
# 成员自助：改昵称 / 改密码（管理面之外，成员对自己的两个动作）
# ---------------------------------------------------------------------------


async def update_own_nickname(session: AsyncSession, member_id: int, nickname: str) -> Member:
    """成员修改自己的昵称。"""
    repo = MemberRepository(session)
    member = await get_member(session, member_id)
    member.nickname = nickname.strip()
    return await repo.save(member)


async def change_own_password(
    session: AsyncSession, member_id: int, *, old_password: str, new_password: str
) -> Member:
    """成员修改自己的密码：校验原密码，token_version+1 只踢自己的旧会话。

    调用方（路由层）随后须为本次会话重新签发 Cookie，保证操作者不被踢出。
    """
    if not new_password or len(new_password) < 8:
        raise BadRequestException("新密码至少 8 位")
    repo = MemberRepository(session)
    member = await get_member(session, member_id)
    if not auth_service.verify_password(old_password, member.password_hash):
        raise UnauthorizedException("原密码错误")
    member.password_hash = auth_service.hash_password(new_password)
    member = await repo.bump_token_version(member)
    await _drop_jellyfin_devices(session, member_id)
    logger.info("成员 %s 已修改密码，其他会话与播放器凭据已下线", member.username)
    return member
