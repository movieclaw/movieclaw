"""内容型合集的成员缓存（docs/design/library-series-collections.md 5.5.2）。

设计文档要求的那条回归：**缓存计数 == count_members()**——把"记得刷缓存"这条
纪律变成 CI 能抓的东西。外加三条边界：观看态合集永远不缓存；分级受限的观看者
不读缓存；改规则后缓存就地重算。
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.api.routes.collections as routes_mod
from movieclaw_api.api.routes.collections import _views
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.access import ContentLimit
from movieclaw_api.services.library.collections import (
    cached_membership,
    count_members,
    ensure_builtin_collections,
    is_content_collection,
    refresh_collection_cache,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    Collection,
    FileSource,
    FileState,
    Library,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

SERIES = "tmdb:1241"


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _add_movie(session, library_id: int, tmdb_id: int, title: str, series: str) -> int:
    item = MediaItem(kind="movie", tmdb_id=tmdb_id, title=title, original_title=title, year=2001)
    session.add(item)
    await session.flush()
    session.add_all(
        [
            MediaMetadata(
                media_item_id=item.id,
                genre_ids=[14],
                release_date=date(2001, 11, 16),
                series_key=series,
                scraped_at=utcnow(),
            ),
            LibraryFile(
                library_id=library_id,
                media_item_id=item.id,
                season_number=0,
                episode_number=0,
                file_path=f"/media/{tmdb_id}.mkv",
                size_bytes=4096,
                source=FileSource.SCANNED,
                state=FileState.IN_PLACE,
            ),
        ]
    )
    await session.flush()
    return item.id


async def _seed(db) -> tuple[int, int]:
    """一个库、两部同系列的片、一个系列合集，外加内置的「我的收藏」。"""
    async with db.session() as session:
        library = Library(name="电影", kind="movie", root_paths=["/media"])
        session.add(library)
        await session.flush()
        await _add_movie(session, library.id, 671, "魔法石", SERIES)
        await _add_movie(session, library.id, 672, "密室", SERIES)
        series = Collection(
            name="哈利·波特（系列）",
            library_id=library.id,
            rules=[{"field": "series_key", "op": "any_of", "values": [SERIES]}],
            builtin=f"series:{SERIES}:{library.id}",
            sort="release_date_asc",
        )
        session.add(series)
        await ensure_builtin_collections(session, library.id)
        await session.commit()
        return library.id, series.id


async def _rows(db) -> dict[str, Collection]:
    async with db.session() as session:
        rows = (await session.execute(select(Collection))).scalars().all()
        return {("series" if r.builtin.startswith("series:") else r.builtin): r for r in rows}


@pytest.mark.asyncio
async def test_cached_count_equals_live_count_after_refresh_stats(db):
    """缓存计数 == count_members()，且随库内容变化一起刷新。"""
    library_id, _series_id = await _seed(db)
    assert cached_membership((await _rows(db))["series"]) is None  # 还没刷过

    async with db.session() as session:
        await LibraryRepository(session).refresh_stats([library_id])
    rows = await _rows(db)
    async with db.session() as session:
        live = await count_members(session, rows["series"])
    assert live == 2
    assert cached_membership(rows["series"])[0] == live
    # 封面头就是成员本身（两部片），按合集自己的序
    assert sorted(cached_membership(rows["series"])[1]) == [1, 2]
    # 「我的收藏」跟着看的人变：永远不缓存
    assert not is_content_collection(rows["favorites:1"])
    assert cached_membership(rows["favorites:1"]) is None

    # 又入了一部：refresh_stats 一到，缓存跟着变，仍与实时计数相等
    async with db.session() as session:
        await _add_movie(session, library_id, 673, "阿兹卡班", SERIES)
        await session.commit()
        await LibraryRepository(session).refresh_stats([library_id])
    rows = await _rows(db)
    async with db.session() as session:
        live = await count_members(session, rows["series"])
    assert live == 3 and cached_membership(rows["series"])[0] == 3
    assert len(cached_membership(rows["series"])[1]) == 3  # COVER_COUNT 张


@pytest.mark.asyncio
async def test_list_reads_cache_only_for_unrestricted_viewers(db, monkeypatch):
    """不限分级的观看者不为内容型合集跑任何成员判定；儿童档案照旧实时算。"""
    library_id, _series_id = await _seed(db)
    async with db.session() as session:
        await LibraryRepository(session).refresh_stats([library_id])

    calls: list[str] = []
    real = routes_mod.resolve_members

    async def spy(session, row, **kwargs):
        calls.append(row.name)
        return await real(session, row, **kwargs)

    monkeypatch.setattr(routes_mod, "resolve_members", spy)
    rows = await _rows(db)
    async with db.session() as session:
        views = await _views(
            session, list(rows.values()), member_id=0, visible=None, content_limit=ContentLimit()
        )
    by_name = {v.name: v for v in views}
    assert by_name["哈利·波特（系列）"].item_count == 2
    assert len(by_name["哈利·波特（系列）"].covers) <= 2
    assert calls == ["我的收藏"], calls  # 只有观看态合集现算

    calls.clear()
    async with db.session() as session:
        await _views(
            session,
            list(rows.values()),
            member_id=0,
            visible=None,
            content_limit=ContentLimit(max_age=12),
        )
    assert sorted(calls) == sorted(["哈利·波特（系列）", "我的收藏"])  # 受限：全部实时


@pytest.mark.asyncio
async def test_watch_rules_never_cache_and_rule_edits_recompute(db):
    """规则含观看状态的合集清空缓存；改规则后缓存就地重算。"""
    library_id, series_id = await _seed(db)
    async with db.session() as session:
        await LibraryRepository(session).refresh_stats([library_id])
        row = await session.get(Collection, series_id)
        assert row.member_count_cache == 2
        # 规则收窄到一部：重算后缓存跟着变
        row.rules = [{"field": "series_key", "op": "any_of", "values": ["tmdb:none"]}]
        await refresh_collection_cache(session, row)
        assert row.member_count_cache == 0 and row.cover_head_cache == []
        # 混进观看状态：不再是内容型，缓存清空
        row.rules = [{"field": "watch", "op": "any_of", "values": ["unwatched"]}]
        await refresh_collection_cache(session, row)
        assert row.member_count_cache is None and row.cover_head_cache is None
