"""成员维度 P1 的行为验证（docs/design/member-management.md §3.4–§3.6）。

覆盖三块：
1. 播放状态按成员隔离：进度/已看/收藏各是各的，互不覆盖；
2. 订阅归属与关注：重复订阅转关注、成员列表过滤、取消的三种语义
   （取关 / 发起人转移 / 真删）、非发起人改参数被拒；
3. 库可见性：白名单成员的库列表/详情按可见性过滤（走真实 HTTP，
   见 test_member_auth.py 的守护测试；此处补服务层的判定语义）。
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import ForbiddenException
from movieclaw_api.services.library.access import member_visible_ids
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.repositories.member_repo import MemberRepository
from movieclaw_db.repositories.subscription_repo import SubscriptionRepository
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient
from movieclaw_playback import state as playback_state

_KEY = "0123456789abcdef0123456789abcdef"

_ROUTES = {
    "/3/movie/100": {
        "id": 100,
        "title": "测试电影",
        "original_title": "Test Movie",
        "release_date": "2024-01-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
}


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _ROUTES.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=payload)

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'member.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _service(session) -> SubscriptionService:
    return SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))


async def _create_member(session, username: str) -> int:
    member = await MemberRepository(session).create(
        username=username, password_hash="x", nickname=username
    )
    assert member.id is not None
    return member.id


async def _create_media_item(session) -> int:
    """播放状态表外键要求条目真实存在；建一条最小的电影条目。"""
    from movieclaw_db.models import MediaItem

    item = MediaItem(kind="movie", tmdb_id=100, title="测试电影", original_title="Test Movie")
    session.add(item)
    await session.commit()
    await session.refresh(item)
    assert item.id is not None
    return item.id


# ---------------------------------------------------------------------------
# 播放状态隔离
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playback_state_isolated_per_member(db) -> None:
    """两个观看者对同一单元各有一行：进度互不覆盖、收藏互不影响。"""
    async with db.session() as session:
        item_id = await _create_media_item(session)
        unit = (item_id, 0, 0)
        # 超管（0）看到 10 分钟，成员 7 看到 20 分钟
        # 与协议层调用流一致：每次写入后 commit（会话工厂关闭了 autoflush，
        # 同一事务内连续 upsert 会看不到彼此的待写行）
        await playback_state.record_playback_progress(
            session, unit, member_id=0, position_ms=600_000, runtime_ms=7_200_000
        )
        await session.commit()
        await playback_state.record_playback_progress(
            session, unit, member_id=7, position_ms=1_200_000, runtime_ms=7_200_000
        )
        await session.commit()
        await playback_state.set_favorite(session, unit, member_id=7, favorite=True)
        await session.commit()

        admin_states = await playback_state.get_states(session, [item_id], member_id=0)
        member_states = await playback_state.get_states(session, [item_id], member_id=7)
        assert admin_states[unit].position_ms == 600_000
        assert member_states[unit].position_ms == 1_200_000
        assert admin_states[unit].is_favorite is False
        assert member_states[unit].is_favorite is True

        # 成员标记已看，超管的进度与已看不受影响
        await playback_state.mark_played(session, [unit], member_id=7)
        await session.commit()
        states_admin = await playback_state.get_states(session, [item_id], member_id=0)
        states_member = await playback_state.get_states(session, [item_id], member_id=7)
        assert states_admin[unit].played is False
        assert states_member[unit].played is True


# ---------------------------------------------------------------------------
# 订阅归属与关注
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_subscribe_becomes_follow_and_cancel_semantics(db) -> None:
    """A 订 → B 再订转关注；B 取关不影响订阅；A 退出转移给 B；B 再退真删。"""
    async with db.session() as session:
        a = await _create_member(session, "alice")
        b = await _create_member(session, "bob")
        service = _service(session)
        repo = SubscriptionRepository(session)

        sub = await service.create(MediaKind.MOVIE, 100, member_id=a)
        assert sub.created_by_member_id == a

        # B 再订同一部：幂等返回同一订阅 + 生成关注行
        again = await service.create(MediaKind.MOVIE, 100, member_id=b)
        assert again.id == sub.id
        assert await repo.follower_member_ids(sub.id) == [b]

        # 成员列表过滤：A 与 B 都看得到，无关成员看不到
        c = await _create_member(session, "carol")
        assert len(await service.list_with_progress(member_id=a)) == 1
        assert len(await service.list_with_progress(member_id=b)) == 1
        assert await service.list_with_progress(member_id=c) == []

        # 非发起人不能改参数（可读中文错误引导去取关）
        with pytest.raises(ForbiddenException):
            await service.assert_can_manage(sub.id, b)
        await service.assert_can_manage(sub.id, a)  # 发起人可以
        await service.assert_can_manage(sub.id, None)  # 超管直通

        # B 取消 = 只删关注行，订阅还在
        message = await service.unsubscribe(sub.id, member_id=b)
        assert "关注" in message
        assert await repo.get(sub.id) is not None
        assert await repo.follower_member_ids(sub.id) == []

        # B 重新关注后 A 退出：发起人转移给 B，订阅继续活着
        await service.create(MediaKind.MOVIE, 100, member_id=b)
        message = await service.unsubscribe(sub.id, member_id=a)
        assert "接管" in message or "继续" in message
        transferred = await repo.get(sub.id)
        assert transferred is not None and transferred.created_by_member_id == b

        # 新发起人 B 退出且无人关注：真删
        await service.unsubscribe(sub.id, member_id=b)
        assert await repo.get(sub.id) is None


@pytest.mark.asyncio
async def test_member_deletion_transfers_subscription_to_admin(db) -> None:
    """删除成员：其发起的订阅经外键 SET NULL 转为超管发起，绝不连带删。"""
    from movieclaw_api.services import members as members_service
    from movieclaw_db.repositories.search_history_repo import SearchHistoryRepository

    async with db.session() as session:
        a = await _create_member(session, "alice")
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 100, member_id=a)
        sub_id = sub.id
        history_repo = SearchHistoryRepository(session)
        await history_repo.record("敏感搜索", member_id=a)

        await members_service.delete_member(session, a)

        # 搜索历史必须跟人清：SQLite 复用行 id 时新成员会"继承"旧历史
        assert await history_repo.list_recent_groups(member_id=a) == []

        # SET NULL 发生在数据库侧；同一 session 的身份映射里还是旧对象，先失效
        session.expire_all()
        survived = await SubscriptionRepository(session).get(sub_id)
        assert survived is not None
        assert survived.created_by_member_id is None  # NULL = 超管发起


# ---------------------------------------------------------------------------
# 库可见性判定（单点收口的语义）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_history_isolated_per_member(db) -> None:
    """搜索历史（P2）：各人只看/只删自己的；越权删除按不存在处理。"""
    from movieclaw_db.repositories.search_history_repo import SearchHistoryRepository

    async with db.session() as session:
        repo = SearchHistoryRepository(session)
        admin_row = await repo.record("沙丘", member_id=0)
        member_row = await repo.record("沙丘", member_id=7)
        assert admin_row != member_row  # 同关键词各归各的行，互不去重

        assert [r.id for r in await repo.list_recent_groups(member_id=0)] == [admin_row]
        assert [r.id for r in await repo.list_recent_groups(member_id=7)] == [member_row]

        # 成员删不到别人的行（按不存在处理），删自己的成功
        assert await repo.delete_by_id(admin_row, member_id=7) is False
        assert await repo.delete_by_id(member_row, member_id=7) is True
        # 清空只清本人：超管的行还在
        assert await repo.clear(member_id=7) == 0
        assert [r.id for r in await repo.list_recent_groups(member_id=0)] == [admin_row]


@pytest.mark.asyncio
async def test_usable_site_ids_semantics(db) -> None:
    """站点白名单判定（P2）：None=不受限；白名单成员返回集合。"""
    from movieclaw_api.services.auth import Principal
    from movieclaw_api.services.site_visibility import usable_site_ids

    async with db.session() as session:
        repo = MemberRepository(session)
        member_id = await _create_member(session, "dave")
        member = await repo.get(member_id)

        admin = Principal(kind="admin", name="admin")
        assert await usable_site_ids(session, admin) is None

        principal = Principal(
            kind="member", name="dave", member_id=member_id, is_admin=False, member=member
        )
        assert await usable_site_ids(session, principal) is None  # all_sites 默认

        member.all_sites = False
        await repo.save(member)
        await repo.set_site_access(member_id, ["mteam"])
        assert await usable_site_ids(session, principal) == {"mteam"}


@pytest.mark.asyncio
async def test_member_visible_ids_semantics(db) -> None:
    """总是返回具体集合（docs/design/library-access.md）：超管 = admin_visible
    的库；all_libraries 成员 = everyone 库 ∪ 白名单；白名单成员只有白名单；
    成员行不存在（凭据竞态）按空集合处理。"""
    from movieclaw_db.repositories.library_repo import LibraryRepository

    async with db.session() as session:
        repo = MemberRepository(session)
        libraries = LibraryRepository(session)
        shared = await libraries.create(name="共享", kind="movie", root_paths=["/m/shared"])
        selected = await libraries.create(
            name="指定", kind="movie", root_paths=["/m/selected"], access_mode="selected"
        )
        member_id = await _create_member(session, "alice")

        assert await member_visible_ids(session, 0) == {shared.id, selected.id}  # 超管默认可浏览
        assert await member_visible_ids(session, member_id) == {shared.id}  # 默认只含 everyone 库

        await repo.set_library_access(member_id, [selected.id])
        assert await member_visible_ids(session, member_id) == {shared.id, selected.id}

        member = await repo.get(member_id)
        member.all_libraries = False
        await repo.save(member)
        assert await member_visible_ids(session, member_id) == {selected.id}  # 只剩白名单

        await libraries.update(
            selected.id, name="指定", root_paths=["/m/selected"], admin_visible=False
        )
        assert await member_visible_ids(session, 0) == {shared.id}  # 超管把自己摘掉

        assert await member_visible_ids(session, 99999) == set()  # 行不存在
