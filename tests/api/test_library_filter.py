"""海报墙筛选：维内 OR、维间 AND，以及观看状态的三档划分。

对应 docs/design/library-filtering.md 3.1/3.2。重点验证两件容易出错的事：
① ``genre_ids`` / ``origin_countries`` 是 JSON 数组，SQLite 侧靠 ``json_each``
   展开后 IN——关联子查询写错会静默筛出全库或空集；
② 未看 / 在看 / 已看完是一个**划分**，三档计数之和必须等于总数，
   否则 facet 计数会对不上，用户点进去就是空墙。
"""

from __future__ import annotations

from datetime import date

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.items import (
    LibraryFilter,
    build_library_index,
    build_library_wall,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    PlaybackState,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

#: 观看者哨兵：0 = 超管（与 playback_state 的成员维度约定一致）
_ME = 0


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'filter.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _file(library_id: int, item_id: int) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=0,
        episode_number=0,
        file_path=f"/movies/{item_id}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
    )


async def _seed(session):
    """一个 5 部电影的小库，覆盖类型/地区/年代三个维度的交叉。

    返回 (library_id, {标题: media_item_id})。
    """
    library = await LibraryRepository(session).create(
        name="电影库", kind="movie", root_paths=["/movies"]
    )
    spec = [
        # 标题,        tmdb, genre_ids, 国家,   上映日
        ("千与千寻", 401, [16, 14], ["JP"], "2001-07-20"),
        ("你的名字", 402, [16, 10749], ["JP"], "2016-08-26"),
        ("寄生虫", 403, [18, 53], ["KR"], "2019-05-30"),
        ("盗梦空间", 404, [878, 28], ["US"], "2010-07-16"),
        ("霸王别姬", 405, [18, 10749], ["CN"], "1993-01-01"),
    ]
    ids: dict[str, int] = {}
    for title, tmdb_id, genres, countries, released in spec:
        item = MediaItem(
            kind="movie",
            tmdb_id=tmdb_id,
            title=title,
            original_title=title,
            year=int(released[:4]),
        )
        session.add(item)
        await session.flush()
        assert item.id is not None
        ids[title] = item.id
        session.add(
            MediaMetadata(
                media_item_id=item.id,
                genre_ids=genres,
                origin_countries=countries,
                release_date=date.fromisoformat(released),
            )
        )
        session.add(_file(library.id, item.id))
    await session.flush()
    assert library.id is not None
    return library.id, ids


async def _titles(session, library_id, **kw) -> set[str]:
    rows = await build_library_wall(session, library_id, member_id=_ME, **kw)
    return {r.title for r in rows}


async def test_no_filter_returns_whole_library(db) -> None:
    """不给筛选条件时与改造前逐字等价：整库都在。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        assert await _titles(session, library_id) == set(ids)
        assert await _titles(session, library_id, filters=LibraryFilter()) == set(ids)


async def test_genres_are_or_within_dimension(db) -> None:
    """维内 OR：勾动画(16)再勾科幻(878)是两者都要看到，不是取交集。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        assert await _titles(session, library_id, filters=LibraryFilter(genres=(16,))) == {
            "千与千寻",
            "你的名字",
        }
        assert await _titles(session, library_id, filters=LibraryFilter(genres=(16, 878))) == {
            "千与千寻",
            "你的名字",
            "盗梦空间",
        }


async def test_dimensions_are_and_across(db) -> None:
    """维间 AND：动画 + 日本 = 只剩两部日本动画；换成韩国就一部不剩。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        both = LibraryFilter(genres=(16,), countries=("JP",))
        assert await _titles(session, library_id, filters=both) == {"千与千寻", "你的名字"}
        assert (
            await _titles(
                session, library_id, filters=LibraryFilter(genres=(16,), countries=("KR",))
            )
            == set()
        )


async def test_decades_use_release_date(db) -> None:
    """年代按上映日期分档；`earlier` 是 1989 年及以前。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        assert await _titles(session, library_id, filters=LibraryFilter(decades=("2010s",))) == {
            "你的名字",
            "寄生虫",
            "盗梦空间",
        }
        assert await _titles(
            session, library_id, filters=LibraryFilter(decades=("1990s", "2000s"))
        ) == {"霸王别姬", "千与千寻"}
        assert (
            await _titles(session, library_id, filters=LibraryFilter(decades=("earlier",))) == set()
        )


async def test_unknown_year_falls_out_of_every_decade(db) -> None:
    """既没有 release_date 也没有 year 的条目不属于任何年代档。

    「未知年份」不是一个年代，硬塞进「更早」是编数据；它只在不筛年代时出现。
    """
    async with db.session() as session:
        library_id, _ = await _seed(session)
        orphan = MediaItem(kind="movie", tmdb_id=499, title="年份不明", original_title="X")
        session.add(orphan)
        await session.flush()
        assert orphan.id is not None
        session.add(_file(library_id, orphan.id))
        await session.flush()

        assert "年份不明" in await _titles(session, library_id)
        every_decade = LibraryFilter(decades=("2020s", "2010s", "2000s", "1990s", "earlier"))
        assert "年份不明" not in await _titles(session, library_id, filters=every_decade)


async def test_watch_states_partition_the_library(db) -> None:
    """未看 / 在看 / 已看完是一个划分：三档不相交，且加起来是全库。

    这条一旦不成立，facet 计数就会对不上——用户看到「已看完 3」点进去只有 2 部。
    """
    async with db.session() as session:
        library_id, ids = await _seed(session)
        session.add_all(
            [
                # 看完了
                PlaybackState(
                    member_id=_ME, media_item_id=ids["千与千寻"], played=True, position_ms=0
                ),
                # 看到一半
                PlaybackState(
                    member_id=_ME, media_item_id=ids["寄生虫"], played=False, position_ms=600_000
                ),
                # 看完过、但又开了个头没看完 → 归「在看」，不能两档都算
                PlaybackState(
                    member_id=_ME, media_item_id=ids["盗梦空间"], played=True, position_ms=0
                ),
                PlaybackState(
                    member_id=_ME,
                    media_item_id=ids["盗梦空间"],
                    season_number=1,
                    played=False,
                    position_ms=90_000,
                ),
            ]
        )
        await session.flush()

        played = await _titles(session, library_id, filters=LibraryFilter(watch="played"))
        watching = await _titles(session, library_id, filters=LibraryFilter(watch="watching"))
        unwatched = await _titles(session, library_id, filters=LibraryFilter(watch="unwatched"))

        assert played == {"千与千寻"}
        assert watching == {"寄生虫", "盗梦空间"}
        assert unwatched == {"你的名字", "霸王别姬"}
        assert played | watching | unwatched == set(ids)
        assert not (played & watching) and not (played & unwatched) and not (watching & unwatched)


async def test_favorite_uses_item_level_sentinel(db) -> None:
    """收藏只认条目级哨兵单元（电影 (0,0)），单集上的收藏不算整部收藏。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        session.add_all(
            [
                PlaybackState(member_id=_ME, media_item_id=ids["你的名字"], is_favorite=True),
                # 单集收藏：季集不是哨兵，不该让整部作品出现在「我收藏的」里
                PlaybackState(
                    member_id=_ME,
                    media_item_id=ids["寄生虫"],
                    season_number=1,
                    episode_number=2,
                    is_favorite=True,
                ),
            ]
        )
        await session.flush()

        assert await _titles(session, library_id, filters=LibraryFilter(watch="favorite")) == {
            "你的名字"
        }


async def test_watch_is_scoped_to_the_viewer(db) -> None:
    """观看状态按人隔离：别人看完的，在我这儿仍是未看。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        session.add(
            PlaybackState(member_id=7, media_item_id=ids["千与千寻"], played=True, position_ms=0)
        )
        await session.flush()

        mine = await build_library_wall(
            session, library_id, member_id=_ME, filters=LibraryFilter(watch="unwatched")
        )
        theirs = await build_library_wall(
            session, library_id, member_id=7, filters=LibraryFilter(watch="unwatched")
        )
        assert "千与千寻" in {r.title for r in mine}
        assert "千与千寻" not in {r.title for r in theirs}


async def test_index_follows_the_same_filter(db) -> None:
    """索引条与墙读同一份有序名单：筛完之后档位里的条目数必须对得上。

    两者口径分叉的话，点字母跳过去会落在错误的 offset 上。
    """
    async with db.session() as session:
        library_id, _ = await _seed(session)
        filters = LibraryFilter(genres=(16,))

        wall = await build_library_wall(session, library_id, member_id=_ME, filters=filters)
        buckets = await build_library_index(session, library_id, "title", filters=filters)

        assert sum(count for _, count, _ in buckets) == len(wall) == 2
        # 起始 offset 连续，且第一档从 0 开始（前端点档名直接当 offset 用）
        assert [offset for _, _, offset in buckets][0] == 0


async def test_pagination_holds_under_filter(db) -> None:
    """筛选态下翻页仍然稳定：两页拼起来正好是全集，无重复无漏项。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        filters = LibraryFilter(countries=("JP", "KR", "US", "CN"))

        first = await build_library_wall(
            session, library_id, member_id=_ME, limit=2, offset=0, filters=filters
        )
        second = await build_library_wall(
            session, library_id, member_id=_ME, limit=2, offset=2, filters=filters
        )
        rest = await build_library_wall(
            session, library_id, member_id=_ME, limit=2, offset=4, filters=filters
        )
        seen = [r.media_item_id for r in (*first, *second, *rest)]
        assert len(seen) == len(set(seen)) == 5
