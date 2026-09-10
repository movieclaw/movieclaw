"""库访问判定的单点收口（docs/design/library-access.md，前身为
docs/design/member-management.md §3.6）。

全系统只有这里回答"这个主体能不能**浏览**这个库"。消费面（库列表/详情/
条目、全局搜索、最近观看、Jellyfin 视图、图片资产）一律调用本模块，不自行
拼查询条件——访问规则演进只改这一个文件。

可见范围模型（两侧各一个开关，一张矩阵）：

- 库侧 ``access_mode``：``everyone`` 对全部成员自动开放；``selected`` 只对
  白名单里显式授权的成员开放。``admin_visible`` 说超管本人在不在范围内；
- 成员侧 ``all_libraries``：自动包含全部 ``everyone`` 库（含以后新建的）；
  ``member_library_access`` 白名单是显式授权行，对两种模式的库都有效。

可浏览集合::

    超管会话            = {admin_visible 为真的库}
    成员 all_libraries  = {everyone 库} ∪ 该成员的白名单
    成员 白名单          = 该成员的白名单
    PAT / Agent 令牌     = {everyone 库}
    Jellyfin member_id=0 = 同超管会话

约定：
- **总是返回具体集合**，超管也不例外——管理权是超管身份自带的（管理类
  路由挂 require_admin），浏览权对所有身份走同一条判定；
- 令牌主体只看 ``everyone`` 库：CLI 输出与 Agent 对话会进日志和会话记录，
  默认不带指定成员才能看的内容；
- 不可见按 404 处理（不泄露"存在但你不能看"）；
- Jellyfin 侧没有 Principal，用 ``member_visible_ids``（按 member_id 哨兵
  约定：0=超管）。

消费点的函数签名仍写 ``set[int] | None``：None 在那里表示「不受限」，只留给
扫描/封面渲染这类**内部管理流程**直接调用时使用，本模块不再产出 None。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.content_rating import rating_age
from movieclaw_db.models.library import Library
from movieclaw_db.models.library_file import LibraryFile
from movieclaw_db.models.media_item import MediaItem
from movieclaw_db.models.media_metadata import MediaMetadata
from movieclaw_db.models.member import Member
from movieclaw_db.repositories.member_repo import MemberRepository

ACCESS_MODE_EVERYONE = "everyone"
ACCESS_MODE_SELECTED = "selected"
ACCESS_MODES = frozenset({ACCESS_MODE_EVERYONE, ACCESS_MODE_SELECTED})


async def _library_flags(session: AsyncSession) -> list[tuple[int, str, bool]]:
    """全部库的 (id, access_mode, admin_visible)。库只有几个到几十个，整取即可。"""
    rows = await session.execute(
        select(Library.id, Library.access_mode, Library.admin_visible)  # type: ignore[call-overload]
    )
    return [(int(i), str(mode), bool(adm)) for i, mode, adm in rows.all() if i is not None]


async def admin_browsable_ids(session: AsyncSession) -> set[int]:
    """超管会话可浏览的库：admin_visible 为真的全部库。"""
    return {i for i, _, adm in await _library_flags(session) if adm}


async def everyone_library_ids(session: AsyncSession) -> set[int]:
    """对全部成员自动开放的库（access_mode=everyone）。"""
    return {i for i, mode, _ in await _library_flags(session) if mode == ACCESS_MODE_EVERYONE}


async def _member_browsable_ids(session: AsyncSession, member: Member) -> set[int]:
    granted = set(await MemberRepository(session).get_library_ids(member.id))  # type: ignore[arg-type]
    if member.all_libraries:
        return (await everyone_library_ids(session)) | granted
    return granted


async def member_visible_ids(session: AsyncSession, member_id: int) -> set[int]:
    """按成员 id（0=超管）返回可浏览库 id 集合。

    成员行不存在（被删除后残留的凭据竞态）按"什么都看不见"处理。
    """
    if member_id == 0:
        return await admin_browsable_ids(session)
    member = await MemberRepository(session).get(member_id)
    if member is None:
        return set()
    return await _member_browsable_ids(session, member)


async def visible_library_ids(session: AsyncSession, principal: Principal) -> set[int]:
    """请求主体可浏览的库 id 集合（见模块说明的矩阵）。"""
    if principal.share is not None:
        # 分享访客（docs/design/media-share.md §4.1）：只有分享出去的那一个库，
        # 且刻意不看库的可见范围——超管把这部片放出去就是决定了它对外可见。
        # 跨库合集的分享没有"那一个库"，范围由合集成员本身界定（_share_covers），
        # 库集合放开是安全的：条目一级的判定才是这条分享真正的闸门
        if principal.share.library_id is None:
            return await admin_browsable_ids(session)
        return {principal.share.library_id}
    if principal.kind == "admin":
        return await admin_browsable_ids(session)
    if principal.member is None:
        # PAT / Agent 令牌：等价管理员的**管理权**，但浏览只给对全员开放的库
        return await everyone_library_ids(session)
    return await _member_browsable_ids(session, principal.member)


@dataclass(frozen=True, slots=True)
class ContentLimit:
    """一个观看者的内容分级约束（``None`` 的那份等于不限）。

    与"可浏览库集合"是同一类东西：**强制的、观看者自带的收窄**，不是用户
    自己选的筛选条件。两者都在本模块产出、由消费面原样往下传，消费面不自己
    拼条件——这条纪律在库可见性上已经证明有效，分级只是第二个实例。

    为什么必须是"六处"而不是"给墙加个条件就完了"：一个孩子能从搜索里搜到、
    从合集里点进去、从电视端播到，那么墙上藏起来只是障眼法。列表少一处、
    起播少一处，这个功能就是假的。
    """

    #: 年龄上限（岁）；None = 不限
    max_age: int | None = None
    #: 未分级的作品给不给看（只在 max_age 非空时有意义）
    allow_unrated: bool = False

    @property
    def unrestricted(self) -> bool:
        """不限——消费面据此走与改造前逐字相同的老路径，零额外查询。"""
        return self.max_age is None

    def allows(self, rating: str | None) -> bool:
        """这条分级串给不给看。

        认不出来的分级串按"未分级"处理：**不猜比猜错安全**——把某国的 ``T``
        猜成 0 就可能把成人片放给小孩（见 content_rating.py 的分寸 1）。
        """
        if self.unrestricted:
            return True
        age = rating_age(rating)
        return self.allow_unrated if age is None else age <= (self.max_age or 0)


#: 不受限的那一份（超管、令牌主体、没设上限的成员共用一个实例）
NO_CONTENT_LIMIT = ContentLimit()


def _limit_of(member: Member | None) -> ContentLimit:
    if member is None or member.content_age_limit is None:
        return NO_CONTENT_LIMIT
    return ContentLimit(
        max_age=int(member.content_age_limit), allow_unrated=bool(member.allow_unrated)
    )


async def content_limit_for(session: AsyncSession, principal: Principal) -> ContentLimit:
    """请求主体的内容分级约束（**全系统唯一的产出点**）。

    分享访客不受这条约束：超管把某部片分享出去，就是已经决定了它对外可见，
    再拿分享者的分级去卡访客既讲不通、也无从取值（访客不是任何一个成员）。
    """
    if principal.share is not None or principal.kind == "admin":
        return NO_CONTENT_LIMIT
    return _limit_of(principal.member)


async def member_content_limit(session: AsyncSession, member_id: int) -> ContentLimit:
    """按成员 id（0=超管）取分级约束——Jellyfin 侧没有 Principal，用这一支。"""
    if member_id == 0:
        return NO_CONTENT_LIMIT
    return _limit_of(await MemberRepository(session).get(member_id))


async def assert_library_visible(
    session: AsyncSession, principal: Principal, library_id: int
) -> None:
    """断言主体可浏览该库；不可见抛 404（与"库不存在"不可区分）。"""
    if library_id not in await visible_library_ids(session, principal):
        raise NotFoundException(f"媒体库不存在：id={library_id}")


async def assert_item_visible(
    session: AsyncSession, principal: Principal, media_item_id: int
) -> None:
    """断言条目对主体可浏览：它至少有一份台账落在可浏览库里。

    没有任何台账行的条目（只被订阅、还没入库）不属于任何库，不受库可见范围
    约束，放行——发现页/订阅页的海报走的正是这条。

    分享访客只能看分享的那一个条目，其他条目一律 404。
    """
    if principal.share is not None and not await _share_covers(session, principal, media_item_id):
        raise NotFoundException("媒体条目不存在")
    library_ids = {
        int(lid)
        for lid in (
            await session.execute(
                select(LibraryFile.library_id).where(  # type: ignore[call-overload]
                    LibraryFile.media_item_id == media_item_id,
                    LibraryFile.library_id.is_not(None),  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
        if lid is not None
    }
    if not library_ids:
        # 本地来源条目（其他库）台账被清空后仍挂着刮削归属库，按归属库判
        item = await session.get(MediaItem, media_item_id)
        if item is None or item.scrape_library_id is None:
            return
        library_ids = {item.scrape_library_id}
    if library_ids.isdisjoint(await visible_library_ids(session, principal)):
        raise NotFoundException("媒体条目不存在")
    await _assert_within_content_limit(session, principal, media_item_id)


async def _share_covers(
    session: AsyncSession, principal: Principal, media_item_id: int
) -> bool:
    """这条分享的范围盖不盖得住这个条目。

    条目分享：就那一个。合集分享：**此刻的成员**——规则驱动的合集会自己长，
    所以每次访问现算，分享出去之后新入库的片也在里面。反过来，被移出合集
    （或不再命中规则）的片立刻失去访问权，不需要任何撤销动作。
    """
    grant = principal.share
    assert grant is not None
    if grant.collection_id is None:
        return media_item_id == grant.media_item_id
    from movieclaw_api.services.library.collections import resolve_members
    from movieclaw_db.models import Collection

    collection = await session.get(Collection, grant.collection_id)
    if collection is None:
        return False
    # 访客不是任何一个成员：可见库不受限（分享出去就是决定了它对外可见），
    # 分级也不受限（与 content_limit_for 里那条同源）
    members = await resolve_members(session, collection)
    return media_item_id in set(members)


async def _assert_within_content_limit(
    session: AsyncSession, principal: Principal, media_item_id: int
) -> None:
    """断言这个条目在观看者的分级约束之内；超出按 404。

    列表少一处、起播少一处，儿童档案这个功能就是假的：孩子从别处拿到 id
    （分享链接、历史记录、直接改地址栏）照样点得进详情、按得下播放。所以判定
    落在 ``assert_item_visible`` 这个已有的收口里——凡是要"这个条目给不给看"
    的地方都已经在调它，不必再记得多调一处。
    """
    limit = await content_limit_for(session, principal)
    if limit.unrestricted:
        return
    rating = (
        await session.execute(
            select(MediaMetadata.content_rating).where(
                MediaMetadata.media_item_id == media_item_id
            )
        )
    ).scalar_one_or_none()
    if not limit.allows(rating):
        raise NotFoundException("媒体条目不存在")
