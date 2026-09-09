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


# ---------------------------------------------------------------------------
# facet 计数（docs/design/library-filtering.md 3.3）
# ---------------------------------------------------------------------------


async def _facets(session, library_id, **kw):
    from movieclaw_api.services.library.items import build_library_facets

    return await build_library_facets(session, library_id, "movie", member_id=_ME, **kw)


def _by_value(view_list) -> dict[str, int]:
    return {row.value: row.count for row in view_list}


async def test_facets_count_the_whole_library_when_unfiltered(db) -> None:
    """无条件时：每个候选值的计数就是它自己的条目数，总数是全库。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        facets = await _facets(session, library_id)

        assert facets.total == len(ids)
        assert _by_value(facets.genres)["16"] == 2  # 动画两部
        assert _by_value(facets.genres)["18"] == 2  # 剧情两部
        assert _by_value(facets.countries)["JP"] == 2
        assert _by_value(facets.decades)["2010s"] == 3


async def test_facet_labels_use_the_builtin_mapping(db) -> None:
    """类型/地区的展示名走内置映射表，不是把 id 原样丢给前端。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        facets = await _facets(session, library_id)

        assert {row.value: row.label for row in facets.genres}["16"] == "动画"
        assert {row.value: row.label for row in facets.countries}["JP"] == "日本"
        assert {row.value: row.label for row in facets.decades}["earlier"] == "更早"


async def test_facet_excludes_its_own_dimension(db) -> None:
    """这是整个 facet 的关键：算某一维时**排除该维自身**的条件。

    勾了「动画」之后，「剧情」的计数不能变成 0——否则多选就废了，
    用户永远只能一次选一个类型。
    """
    async with db.session() as session:
        library_id, _ = await _seed(session)
        facets = await _facets(session, library_id, filters=LibraryFilter(genres=(16,)))

        genres = _by_value(facets.genres)
        assert genres["16"] == 2, "已选值自身的计数照常"
        assert genres["18"] == 2, "同维其他值不受本维条件影响——勾上它就能看到这两部"
        # 但**其他维度**要受影响：只剩两部日本动画，所以韩国/美国都归零
        countries = _by_value(facets.countries)
        assert countries["JP"] == 2
        assert countries.get("KR", 0) == 0 and countries.get("US", 0) == 0
        assert facets.total == 2


async def test_zero_values_are_still_returned(db) -> None:
    """为 0 的候选值照常返回——前端置灰不可点，是"永不空货架"的第一道闸。

    直接不返回的话，用户会看到选项凭空消失，比置灰更让人困惑。
    """
    async with db.session() as session:
        library_id, _ = await _seed(session)
        facets = await _facets(session, library_id, filters=LibraryFilter(countries=("JP",)))

        decades = _by_value(facets.decades)
        assert set(decades) == {"2020s", "2010s", "2000s", "1990s", "earlier"}
        assert decades["1990s"] == 0  # 日本片里没有 90 年代的，但这一档仍然在


async def test_facets_and_wall_agree(db) -> None:
    """面板上显示多少部，点下去墙上就是多少部——两者共用同一个 filters。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        base = LibraryFilter(countries=("JP", "KR"))
        facets = await _facets(session, library_id, filters=base)

        for row in facets.genres:
            picked = LibraryFilter(countries=base.countries, genres=(int(row.value),))
            wall = await build_library_wall(session, library_id, member_id=_ME, filters=picked)
            assert len(wall) == row.count, f"类型 {row.label} 的计数与墙对不上"


async def test_watch_facet_sums_to_total(db) -> None:
    """未看/在看/已看完三档相加等于总数（favorite 与它们正交，不参与求和）。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        session.add(
            PlaybackState(member_id=_ME, media_item_id=ids["寄生虫"], played=True, position_ms=0)
        )
        await session.flush()

        facets = await _facets(session, library_id)
        watch = _by_value(facets.watch)
        assert watch["unwatched"] + watch["watching"] + watch["played"] == facets.total


# ---------------------------------------------------------------------------
# 接口装配：查询参数解析与三个接口的同参约定
# ---------------------------------------------------------------------------


def test_filter_params_parsing() -> None:
    """逗号分隔、大小写、脏取值的解析口径。

    解析不出的取值静默丢弃而不是 422——筛选条件常出现在分享出去的链接里，
    老链接里的一个废值不该让整页打不开。
    """
    from movieclaw_api.api.routes.libraries import _filter_params

    got = _filter_params(g="16, 878 ,x", c="jp,kr", d="2010s,2020s", w="unwatched")
    assert got.genres == (16, 878)
    assert got.countries == ("JP", "KR")
    assert got.decades == ("2010s", "2020s")
    assert got.watch == "unwatched"

    empty = _filter_params()
    assert empty.is_empty and empty.genres == () and empty.watch is None


def test_facets_endpoint_is_wired(tmp_path, monkeypatch) -> None:
    """走一遍真实 HTTP：路由注册、依赖装配、响应形状。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'http.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.api.routes import libraries as library_routes
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    async def _skip_scan(*_a, **_kw) -> None:
        """建库后的初次扫描与本用例无关。"""

    monkeypatch.setattr(library_routes, "enqueue_scan_job", _skip_scan)
    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/libraries",
            json={"name": "电影库", "kind": "movie", "root_paths": [str(tmp_path / "movies")]},
        )
        assert created.status_code == 200, created.text
        library_id = created.json()["data"]["id"]

        resp = client.get(f"/api/v1/libraries/{library_id}/facets?g=16&c=JP&w=unwatched")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 0
        # 空库也回全部年代档与全部观看档（置灰用），只有类型/地区是数据里长出来的
        assert [row["value"] for row in data["decades"]] == [
            "2020s",
            "2010s",
            "2000s",
            "1990s",
            "earlier",
        ]
        assert [row["value"] for row in data["watch"]] == [
            "unwatched",
            "watching",
            "played",
            "favorite",
        ]

        # /items 与 /item-index 吃同一组参数，不能因为多带筛选就 422
        assert client.get(f"/api/v1/libraries/{library_id}/items?g=16&c=JP").status_code == 200
        assert client.get(f"/api/v1/libraries/{library_id}/item-index?g=16&c=JP").status_code == 200

    get_settings.cache_clear()
