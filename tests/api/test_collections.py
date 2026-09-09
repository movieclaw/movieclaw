"""合集：规则求值只能有一个实现，以及三层可见性收口。

对应 docs/design/library-collections.md。最要紧的一条是第 0 节那个约束——
**合集根本没有自己的查询**：``resolve_members()`` 是 ``_wall_page_ids()`` 的
薄适配。这里有一条回归测试直接压它：同一组条件下，海报墙与合集必须返回
同一批 id。两端一旦分叉，同一个智能合集会在网页里 42 部、电视上 39 部。
"""

from __future__ import annotations

from datetime import date

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.collections import (
    count_members,
    effective_rules,
    ensure_builtin_collections,
    is_rule_driven,
    resolve_members,
    rules_to_filter,
    visible_collections,
)
from movieclaw_api.services.library.items import LibraryFilter, build_library_wall
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    Collection,
    CollectionItem,
    FileSource,
    FileState,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    PlaybackState,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

_ME = 0


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'col.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(session):
    library = await LibraryRepository(session).create(
        name="电影库", kind="movie", root_paths=["/movies"]
    )
    spec = [
        ("千与千寻", 401, [16], ["JP"], "2001-07-20"),
        ("你的名字", 402, [16], ["JP"], "2016-08-26"),
        ("寄生虫", 403, [18], ["KR"], "2019-05-30"),
        ("盗梦空间", 404, [878], ["US"], "2010-07-16"),
    ]
    ids: dict[str, int] = {}
    for title, tmdb_id, genres, countries, released in spec:
        item = MediaItem(
            kind="movie", tmdb_id=tmdb_id, title=title, original_title=title, year=int(released[:4])
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
                scraped_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                season_number=0,
                episode_number=0,
                file_path=f"/movies/{item.id}.mkv",
                size_bytes=1,
                source=FileSource.SCANNED,
                state=FileState.IN_PLACE,
            )
        )
    await session.flush()
    assert library.id is not None
    return library.id, ids


def _rules(**kw) -> list[dict]:
    return [{"field": k, "op": "any_of", "values": v} for k, v in kw.items()]


async def test_rules_and_filter_are_the_same_language(db) -> None:
    """规则 → 筛选条件是一次纯粹的形状转换，没有第二套语义。"""
    got = rules_to_filter(_rules(genres=[16], origin_countries=["JP"], rating_gte=[8]))
    assert got.genres == (16,) and got.countries == ("JP",) and got.rating_gte == 8


async def test_unknown_rule_fields_are_ignored_not_fatal(db) -> None:
    """未知字段保守忽略：新版本写的规则被旧代码读到时，宁可少收窄也不能误收窄。"""
    got = rules_to_filter([{"field": "从未见过的字段", "op": "any_of", "values": ["x"]}])
    assert got.is_empty


async def test_collection_matches_the_wall_exactly(db) -> None:
    """**本文件最重要的一条**：同一组条件，海报墙与合集返回同一批 id。

    两端一旦分叉，同一个智能合集会在网页里 42 部、电视上 39 部——这类不一致
    查起来极贵，用户会直接认定功能坏了。之所以能保证，是因为合集根本没有
    自己的查询：resolve_members 只是给 _wall_page_ids 传参。
    """
    async with db.session() as session:
        library_id, _ = await _seed(session)
        col = Collection(name="日本动画", library_id=library_id, rules=_rules(genres=[16]))
        session.add(col)
        await session.flush()

        from_wall = [
            row.media_item_id
            for row in await build_library_wall(
                session, library_id, member_id=_ME, filters=LibraryFilter(genres=(16,))
            )
        ]
        from_collection = await resolve_members(session, col, member_id=_ME)
        assert from_wall == from_collection
        assert len(from_wall) == 2


async def test_shape_is_derived_not_stored(db) -> None:
    """没有 mode 列：规则非空就是规则驱动，有名单行就是名单驱动。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        smart = Collection(name="动画", library_id=library_id, rules=_rules(genres=[16]))
        manual = Collection(name="手挑", library_id=library_id, rules=[])
        session.add_all([smart, manual])
        await session.flush()
        session.add(
            CollectionItem(collection_id=manual.id or 0, media_item_id=ids["寄生虫"], position=0)
        )
        await session.flush()

        assert is_rule_driven(smart) is True
        assert is_rule_driven(manual) is False
        assert await resolve_members(session, manual, member_id=_ME) == [ids["寄生虫"]]


async def test_manual_list_keeps_its_own_order(db) -> None:
    """名单驱动按 position——那是用户拖出来的顺序，不能被任何默认序洗掉。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        col = Collection(name="周末陪娃看", library_id=library_id, rules=[])
        session.add(col)
        await session.flush()
        order = ["盗梦空间", "千与千寻", "寄生虫"]
        session.add_all(
            CollectionItem(collection_id=col.id or 0, media_item_id=ids[t], position=i)
            for i, t in enumerate(order)
        )
        await session.flush()

        assert await resolve_members(session, col, member_id=_ME) == [ids[t] for t in order]


async def test_manual_list_drops_members_that_left_the_library(db) -> None:
    """名单里的行还在，但作品已经不在库里了——它不该出现在墙上。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        ghost = MediaItem(kind="movie", tmdb_id=499, title="已移走", original_title="G")
        session.add(ghost)
        await session.flush()
        assert ghost.id is not None
        col = Collection(name="名单", library_id=library_id, rules=[])
        session.add(col)
        await session.flush()
        session.add_all(
            [
                CollectionItem(collection_id=col.id or 0, media_item_id=ids["寄生虫"], position=0),
                # 这一部从来没有在位文件
                CollectionItem(collection_id=col.id or 0, media_item_id=ghost.id, position=1),
            ]
        )
        await session.flush()

        assert await resolve_members(session, col, member_id=_ME) == [ids["寄生虫"]]


async def test_builtin_favorites_is_a_collection_like_any_other(db) -> None:
    """「我的收藏」登记为内置合集后，走的是和用户合集完全一样的那条路。

    这个抽象要吃掉既有特例，而不是摆在它旁边。
    """
    async with db.session() as session:
        library_id, ids = await _seed(session)
        await ensure_builtin_collections(session, library_id)
        rows = await visible_collections(session, library_id=library_id, member_id=_ME)
        fav = next(r for r in rows if (r.builtin or "").startswith("favorites"))

        assert is_rule_driven(fav), "内置合集的规则来自内置表，不是存在 rules 列里"
        assert effective_rules(fav)
        assert await resolve_members(session, fav, member_id=_ME) == []

        session.add(
            PlaybackState(member_id=_ME, media_item_id=ids["千与千寻"], is_favorite=True)
        )
        await session.flush()
        assert await resolve_members(session, fav, member_id=_ME) == [ids["千与千寻"]]


async def test_builtin_is_idempotent(db) -> None:
    async with db.session() as session:
        library_id, _ = await _seed(session)
        await ensure_builtin_collections(session, library_id)
        await ensure_builtin_collections(session, library_id)
        rows = await visible_collections(session, library_id=library_id, member_id=_ME)
        assert len([r for r in rows if r.builtin]) == 1


async def test_member_scoped_collection_counts_differ_per_viewer(db) -> None:
    """成员相关的合集按人算——这也正是成员数不能缓存的原因。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        await ensure_builtin_collections(session, library_id)
        fav = next(
            r
            for r in await visible_collections(session, library_id=library_id, member_id=_ME)
            if r.builtin
        )
        session.add(
            PlaybackState(member_id=7, media_item_id=ids["寄生虫"], is_favorite=True)
        )
        await session.flush()

        assert await count_members(session, fav, member_id=_ME) == 0
        assert await count_members(session, fav, member_id=7) == 1


async def test_private_collections_are_invisible_to_others(db) -> None:
    """可见性第一层：私有合集只对归属成员下发。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        session.add(
            Collection(
                name="只有我",
                library_id=library_id,
                rules=_rules(genres=[16]),
                visibility="private",
                member_id=7,
            )
        )
        await session.flush()

        mine = await visible_collections(session, library_id=library_id, member_id=7)
        theirs = await visible_collections(session, library_id=library_id, member_id=_ME)
        assert [r.name for r in mine] == ["只有我"]
        assert theirs == []


async def test_collections_in_invisible_libraries_are_dropped(db) -> None:
    """可见性第一层的另一半：所属库对该成员不可见，合集也不下发。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        session.add(Collection(name="动画", library_id=library_id, rules=_rules(genres=[16])))
        await session.flush()

        assert await visible_collections(session, member_id=_ME, visible_library_ids={library_id})
        assert (
            await visible_collections(session, member_id=_ME, visible_library_ids=set()) == []
        )


async def test_members_are_narrowed_by_visible_libraries(db) -> None:
    """可见性第二层：解析结果必须过可见库，不能靠调用方自己再滤一遍。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        col = Collection(name="动画", library_id=library_id, rules=_rules(genres=[16]))
        session.add(col)
        await session.flush()

        assert (
            await resolve_members(
                session, col, member_id=_ME, visible_library_ids={library_id}
            )
            != []
        )
        assert (
            await resolve_members(session, col, member_id=_ME, visible_library_ids=set()) == []
        )
