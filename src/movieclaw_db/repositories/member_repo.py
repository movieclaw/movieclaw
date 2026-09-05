from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.member import Member, MemberLibraryAccess, MemberSiteAccess


class MemberRepository:
    """成员账号（``member`` 表）与其资源白名单的数据访问层。

    密码哈希由服务层（movieclaw_api.services.auth）负责生成与校验，本层只
    存取哈希串；会话失效语义（token_version）也由服务层驱动，本层提供 bump。

    白名单的写入采用整体覆盖式（set_*_access）：管理员在成员详情页勾选后
    整组保存，逐条 diff 不值得——家庭场景库与站点都是个位数。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 --------------------------------------------------------------

    async def get(self, member_id: int) -> Member | None:
        """按主键查询；不存在返回 None。"""
        return await self._session.get(Member, member_id)

    async def get_by_username(self, username: str) -> Member | None:
        """按用户名查询（大小写不敏感——登录与建号互斥校验共用）。"""
        result = await self._session.execute(
            select(Member).where(func.lower(Member.username) == username.strip().lower())
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Member]:
        """全部成员，按创建顺序（id 升序）——成员列表页的展示顺序。"""
        result = await self._session.execute(select(Member).order_by(Member.id))
        return list(result.scalars().all())

    # -- 写入 --------------------------------------------------------------

    async def create(self, *, username: str, password_hash: str, nickname: str = "") -> Member:
        """新增成员。用户名唯一性由数据库约束兜底，与超管的互斥由服务层校验。"""
        row = Member(
            username=username.strip(),
            password_hash=password_hash,
            nickname=nickname.strip(),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def save(self, member: Member) -> Member:
        """持久化对成员行的字段修改（昵称/开关/状态等），刷新 updated_at。"""
        member.updated_at = utcnow()
        self._session.add(member)
        await self._session.commit()
        await self._session.refresh(member)
        return member

    async def bump_token_version(self, member: Member) -> Member:
        """会话版本 +1：该成员所有已签发的会话令牌立即失效（只踢这一个人）。"""
        member.token_version += 1
        return await self.save(member)

    async def touch_last_login(self, member: Member) -> None:
        """登录成功后刷新最近登录时间（不动 updated_at 语义之外的字段）。"""
        member.last_login_at = utcnow()
        member.updated_at = utcnow()
        self._session.add(member)
        await self._session.commit()

    async def delete(self, member: Member) -> None:
        """删除成员。白名单行靠外键级联清理；其余个人数据（播放进度等）的
        清理与订阅归属转移由服务层在调用本方法前显式完成。"""
        await self._session.delete(member)
        await self._session.commit()

    # -- 库可见性白名单 ----------------------------------------------------

    async def get_library_ids(self, member_id: int) -> list[int]:
        """成员的可见库 id 白名单（仅 all_libraries=False 时有意义）。"""
        result = await self._session.execute(
            select(MemberLibraryAccess.library_id).where(
                MemberLibraryAccess.member_id == member_id
            )
        )
        return list(result.scalars().all())

    async def set_library_access(self, member_id: int, library_ids: list[int]) -> None:
        """整体覆盖成员的可见库白名单。"""
        await self._session.execute(
            sa_delete(MemberLibraryAccess).where(MemberLibraryAccess.member_id == member_id)
        )
        for library_id in dict.fromkeys(library_ids):
            self._session.add(MemberLibraryAccess(member_id=member_id, library_id=library_id))
        await self._session.commit()

    async def get_library_member_ids(self, library_id: int) -> list[int]:
        """某个库的显式授权成员 id（库设置页「可见成员」名单的反向读取）。"""
        result = await self._session.execute(
            select(MemberLibraryAccess.member_id).where(
                MemberLibraryAccess.library_id == library_id
            )
        )
        return list(result.scalars().all())

    async def set_library_member_ids(self, library_id: int, member_ids: list[int]) -> None:
        """整体覆盖某个库的显式授权成员（不提交事务，随库更新一起落盘）。

        与 ``set_library_access`` 写的是同一张表的同一种行——库设置页勾成员
        与成员管理页勾库互通，两处看到的名单永远一致。
        """
        await self._session.execute(
            sa_delete(MemberLibraryAccess).where(MemberLibraryAccess.library_id == library_id)
        )
        for member_id in dict.fromkeys(member_ids):
            self._session.add(MemberLibraryAccess(member_id=member_id, library_id=library_id))

    # -- 站点可用性白名单 --------------------------------------------------

    async def get_site_ids(self, member_id: int) -> list[str]:
        """成员的可用站点 id 白名单（仅 all_sites=False 时有意义）。"""
        result = await self._session.execute(
            select(MemberSiteAccess.site_id).where(MemberSiteAccess.member_id == member_id)
        )
        return list(result.scalars().all())

    async def set_site_access(self, member_id: int, site_ids: list[str]) -> None:
        """整体覆盖成员的可用站点白名单。"""
        await self._session.execute(
            sa_delete(MemberSiteAccess).where(MemberSiteAccess.member_id == member_id)
        )
        for site_id in dict.fromkeys(s.strip() for s in site_ids if s.strip()):
            self._session.add(MemberSiteAccess(member_id=member_id, site_id=site_id))
        await self._session.commit()
