"""下载保存位置记忆的行为验证（docs/design/download-target-memory.md）。

覆盖四块：
1. 记忆的读写与幂等（目标没变就不刷 updated_at）；
2. 成员隔离：各人各一份，超管是哨兵 0，互相读不到；
3. 删除成员时的清理走注册表——本期把 delete_member 从逐行手写改成遍历
   ``member_scoped_models()``，这里验证新表和既有的两张表都被覆盖到；
4. 注册表守卫：带 member_id 的表必须登记，否则删成员时会漏清，而 SQLite
   复用行 id 会让新成员继承前一个人的数据（跨人隐私泄漏）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlmodel import SQLModel, select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.members import delete_member
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadTargetPref,
    MediaItem,
    MemberScopedMixin,
    PlaybackLog,
    PlaybackState,
    SearchHistory,
    member_scoped_models,
)
from movieclaw_db.repositories.download_target_pref_repo import DownloadTargetPrefRepository
from movieclaw_db.repositories.member_repo import MemberRepository

ADMIN = 0  # 超管哨兵

# 有 member_id 但**不该**进成员级注册表的表，逐条写明谁负责清理。
# 这是白名单而不是"忘了就算了"：每加一条都要说清替代的清理路径，
# 评审时才看得出是深思熟虑还是漏写。
_CLEANED_ELSEWHERE = {
    # 外键 ondelete=CASCADE，数据库自己清
    "member_library_access": "外键级联",
    "member_site_access": "外键级联",
    "subscription_follower": "外键级联",
    # 单独清理：停用/改密/重置密码时也要吊销设备，不止删除成员时
    "jellyfin_device": "_drop_jellyfin_devices 显式清理（三个时机都要）",
    # 播放质量遥测（档位、卡顿率），不含观看内容。删掉会改写历史直通率统计，
    # 故意保留；member_id 在此只作分组维度。若日后判定应随人清，登记即可。
    "playback_metric": "质量遥测，故意保留（会影响历史统计口径）",
}


def _MEMBER_ID_TABLES() -> set[str]:
    """metadata 里所有带 member_id 列的表名。"""
    return {
        name for name, table in SQLModel.metadata.tables.items() if "member_id" in table.columns
    }


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pref.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_upsert_and_read_back(db):
    """写入 → 读回；同一分类再写覆盖，不新增行。"""
    async with db.session() as session:
        repo = DownloadTargetPrefRepository(session)
        await repo.upsert(ADMIN, "anime", kind="dir", save_path="/media/anime", downloader_id=None)
        row = await repo.get(ADMIN, "anime")
        assert row is not None
        assert (row.kind, row.save_path) == ("dir", "/media/anime")

        # 换目标：覆盖同一行
        await repo.upsert(ADMIN, "anime", kind="smart", save_path=None, downloader_id=3)
        rows = await repo.list_for_member(ADMIN)
        assert len(rows) == 1
        assert (rows[0].kind, rows[0].save_path, rows[0].downloader_id) == ("smart", None, 3)


@pytest.mark.asyncio
async def test_upsert_is_noop_when_target_unchanged(db):
    """目标没变就不写：批量下载同一分类连点十几次不该每次刷盘。"""
    async with db.session() as session:
        repo = DownloadTargetPrefRepository(session)
        first = await repo.upsert(
            ADMIN, "movie", kind="dir", save_path="/media/movies", downloader_id=None
        )
        stamp = first.updated_at
        again = await repo.upsert(
            ADMIN, "movie", kind="dir", save_path="/media/movies", downloader_id=None
        )
        assert again.updated_at == stamp


@pytest.mark.asyncio
async def test_buckets_are_per_category(db):
    """分类之间互不干扰：动漫和电影各记各的。"""
    async with db.session() as session:
        repo = DownloadTargetPrefRepository(session)
        await repo.upsert(ADMIN, "anime", kind="dir", save_path="/media/anime", downloader_id=None)
        await repo.upsert(ADMIN, "movie", kind="smart", save_path=None, downloader_id=None)
        assert {r.category for r in await repo.list_for_member(ADMIN)} == {"anime", "movie"}


@pytest.mark.asyncio
async def test_delete_is_idempotent(db):
    """「不再记住」幂等：本来就没有也不算失败。"""
    async with db.session() as session:
        repo = DownloadTargetPrefRepository(session)
        assert await repo.delete(ADMIN, "anime") is False
        await repo.upsert(ADMIN, "anime", kind="default", save_path=None, downloader_id=None)
        assert await repo.delete(ADMIN, "anime") is True
        assert await repo.get(ADMIN, "anime") is None


@pytest.mark.asyncio
async def test_member_isolation(db):
    """成员之间、成员与超管之间互相读不到。"""
    async with db.session() as session:
        member = await MemberRepository(session).create(
            username="alice", password_hash="x", nickname=""
        )
        assert member.id is not None
        repo = DownloadTargetPrefRepository(session)
        await repo.upsert(ADMIN, "anime", kind="dir", save_path="/media/anime", downloader_id=None)
        await repo.upsert(
            member.id, "anime", kind="dir", save_path="/media/alice", downloader_id=None
        )

        assert (await repo.get(ADMIN, "anime")).save_path == "/media/anime"
        assert (await repo.get(member.id, "anime")).save_path == "/media/alice"
        assert len(await repo.list_for_member(ADMIN)) == 1
        assert len(await repo.list_for_member(member.id)) == 1


@pytest.mark.asyncio
async def test_delete_member_clears_all_member_scoped_tables(db):
    """删除成员时，注册表里的每一张表都要被清空——漏一张就是跨人隐私泄漏。

    这里同时放了新表和既有的两张（观看状态、搜索历史），验证 delete_member 从
    逐行手写改成遍历注册表之后没有回退。
    """
    async with db.session() as session:
        member = await MemberRepository(session).create(
            username="bob", password_hash="x", nickname=""
        )
        mid = member.id
        assert mid is not None

        await DownloadTargetPrefRepository(session).upsert(
            mid, "anime", kind="dir", save_path="/media/bob", downloader_id=None
        )
        # playback_state 对 media_item 有真外键，得先有条目
        item = MediaItem(
            kind="movie",
            external_id="tmdb:1",
            tmdb_id=1,
            title="测试电影",
            original_title="Test",
            aliases=[],
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)

        session.add(SearchHistory(member_id=mid, keyword="凡人修仙传"))
        session.add(PlaybackState(member_id=mid, media_item_id=item.id, position_ms=1000))
        # playback_log 本期新接入注册表：它的 docstring 一直写着「删成员时由服务层
        # 清理」，但 delete_member 从来没清过——这行就是那个漏洞的回归测试
        session.add(PlaybackLog(member_id=mid, media_item_id=item.id))
        # 超管的同类数据必须原样留下（只清这个人的）
        await DownloadTargetPrefRepository(session).upsert(
            ADMIN, "anime", kind="dir", save_path="/media/anime", downloader_id=None
        )
        session.add(SearchHistory(member_id=ADMIN, keyword="奥本海默"))
        await session.commit()

        await delete_member(session, mid)

        for model in member_scoped_models():
            left = (
                await session.execute(select(model).where(model.member_id == mid))
            ).scalars().all()
            assert left == [], f"{model.__name__} 未在删除成员时清理"

        assert len(await DownloadTargetPrefRepository(session).list_for_member(ADMIN)) == 1
        admin_history = (
            await session.execute(select(SearchHistory).where(SearchHistory.member_id == ADMIN))
        ).scalars().all()
        assert len(admin_history) == 1


def test_every_member_scoped_table_is_registered():
    """CI 守卫：带 member_id 列的表必须挂 MemberScopedMixin 并登记。

    漏登记不会报错、也不会有明显症状——直到某个成员被删除、SQLite 复用了他的
    行 id，新成员建号后继承了前一个人的数据。这条断言是唯一能提前发现它的地方。

    按 ``SQLModel.metadata`` 里的**表**比对而不是模型类：模型类的
    ``__subclasses__()`` 只看得到一层，够不着全部表定义。
    """
    import movieclaw_db.models  # noqa: F401  # 触发全部表注册进 metadata

    registered = {m.__tablename__ for m in member_scoped_models()}
    missing = sorted(_MEMBER_ID_TABLES() - registered - set(_CLEANED_ELSEWHERE))
    assert missing == [], (
        f"这些表有 member_id 却没有登记为成员级数据：{missing}；"
        "请混入 MemberScopedMixin 并加 @register_member_scoped，否则删除成员时会漏清"
        "（SQLite 复用行 id，新成员会继承前一个人的数据）"
    )


def test_exemptions_still_exist():
    """豁免名单不能腐烂：表改名或删掉后，名单里的死条目要被发现。"""
    import movieclaw_db.models  # noqa: F401

    stale = sorted(set(_CLEANED_ELSEWHERE) - _MEMBER_ID_TABLES())
    assert stale == [], f"豁免名单里这些表已不存在，请删掉对应条目：{stale}"


def test_registry_rejects_unmixed_model():
    """没混入 mixin 就登记会被当场拒绝，避免注册表里出现清理不了的表。"""
    from movieclaw_db.models.member_scoped import register_member_scoped

    class NotScoped(SQLModel):
        pass

    with pytest.raises(TypeError):
        register_member_scoped(NotScoped)

    assert issubclass(DownloadTargetPref, MemberScopedMixin)
