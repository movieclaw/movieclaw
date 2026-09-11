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
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.items import (
    LibraryFilter,
    _wall_count,
    _wall_page_ids,
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
    utcnow,
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
                # 真实刮削过的条目一定有 scraped_at；不设的话「没刮到档案」
                # 这一档会把整库都算进去
                scraped_at=utcnow(),
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


# ---------------------------------------------------------------------------
# 新增排序（docs/design/library-filtering.md 3.1「排序」）
# ---------------------------------------------------------------------------


async def _order(session, library_id, sort, **kw) -> list[str]:
    rows = await build_library_wall(session, library_id, member_id=_ME, sort=sort, **kw)
    return [r.title for r in rows]


async def _seed_metrics(session):
    """在基础库上补齐评分/片长/体积/观看时间，供四档排序验证。"""
    library_id, ids = await _seed(session)
    metrics = {
        "千与千寻": (8.7, 125),
        "你的名字": (8.4, 106),
        "寄生虫": (8.6, 132),
        "盗梦空间": (9.4, 148),
        "霸王别姬": (9.6, 171),
    }
    for title, (score, runtime) in metrics.items():
        row = (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id == ids[title])
            )
        ).scalar_one()
        row.vote_average = score
        row.runtime_minutes = runtime
    await session.flush()
    return library_id, ids


async def test_sort_by_rating_puts_the_best_first(db) -> None:
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        assert (await _order(session, library_id, "rating"))[:2] == ["霸王别姬", "盗梦空间"]


async def test_sort_by_runtime_is_ascending(db) -> None:
    """片长升序：「今晚只有 90 分钟」是真实诉求，「最长的在前」几乎没人要。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        assert (await _order(session, library_id, "runtime"))[:2] == ["你的名字", "千与千寻"]


async def test_missing_metric_sinks_to_the_bottom(db) -> None:
    """度量为空的条目一律沉底，不随排序方向在头尾之间跳。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        blank = MediaItem(kind="movie", tmdb_id=498, title="没有档案", original_title="Y")
        session.add(blank)
        await session.flush()
        assert blank.id is not None
        session.add(_file(library_id, blank.id))
        await session.flush()

        assert (await _order(session, library_id, "rating"))[-1] == "没有档案"
        assert (await _order(session, library_id, "runtime"))[-1] == "没有档案"


async def test_sort_by_size_uses_this_library_only(db) -> None:
    """体积按本库在位文件求和：同一部片散在两个库时，这面墙显示的是它在**本库**占多少。"""
    async with db.session() as session:
        library_id, ids = await _seed_metrics(session)
        other = await LibraryRepository(session).create(
            name="备份库", kind="movie", root_paths=["/backup"]
        )
        assert other.id is not None
        # 「你的名字」在本库补一个大文件；「寄生虫」的大文件落在另一个库，不该算进来
        big = _file(library_id, ids["你的名字"])
        big.file_path = "/movies/big.mkv"
        big.size_bytes = 50_000
        elsewhere = _file(other.id, ids["寄生虫"])
        elsewhere.file_path = "/backup/huge.mkv"
        elsewhere.size_bytes = 999_999
        session.add_all([big, elsewhere])
        await session.flush()

        assert (await _order(session, library_id, "size"))[0] == "你的名字"


async def test_sort_by_last_played_is_per_viewer(db) -> None:
    """最近观看按人算：别人的观看记录不该影响我的墙。"""
    from datetime import datetime

    async with db.session() as session:
        library_id, ids = await _seed_metrics(session)
        session.add_all(
            [
                PlaybackState(
                    member_id=_ME,
                    media_item_id=ids["寄生虫"],
                    last_played_at=datetime(2026, 9, 1, 12, 0),
                ),
                PlaybackState(
                    member_id=99,
                    media_item_id=ids["霸王别姬"],
                    last_played_at=datetime(2026, 9, 8, 12, 0),
                ),
            ]
        )
        await session.flush()

        assert (await _order(session, library_id, "last_played"))[0] == "寄生虫"
        theirs = await build_library_wall(session, library_id, member_id=99, sort="last_played")
        assert theirs[0].title == "霸王别姬"


async def test_rating_index_buckets_match_the_wall(db) -> None:
    """评分档与墙读同一份有序名单：各档条目数之和等于墙长，起点从 0 开始。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        wall = await _order(session, library_id, "rating")
        buckets = await build_library_index(session, library_id, "rating", member_id=_ME)

        assert [label for label, _, _ in buckets][:2] == ["9+", "8+"]
        assert sum(count for _, count, _ in buckets) == len(wall)
        assert buckets[0][2] == 0


async def test_new_sorts_respect_filters(db) -> None:
    """筛选与排序正交：先收窄候选集，再按新排序排——两件事互不知道对方存在。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        got = await _order(session, library_id, "rating", filters=LibraryFilter(countries=("JP",)))
        assert got == ["千与千寻", "你的名字"]


# ---------------------------------------------------------------------------
# 正倒序切换（2026-09-11）
# ---------------------------------------------------------------------------

#: 自然方向是升序的档；其余都是「大的 / 新的 / 近的在前」
_ASC_BY_NATURE = {"title", "runtime"}

#: 可以切方向的全部排序档（补探序是临时接管，不在其列）
_DIRECTIONAL_SORTS = (
    "title",
    "added_at",
    "release_date",
    "rating",
    "runtime",
    "size",
    "last_played",
)


async def test_reversed_order_is_the_natural_order_backwards(db) -> None:
    """反向后的墙恰好是自然方向倒过来——同分、同为空的片也倒过来。

    只有这样，翻页、索引、「回到上次位置」的 offset 才能共用一套口径；显式传
    自然方向则必须与不传逐字相同（老调用方、合集的 sort 都不带方向）。
    """
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        for sort in _DIRECTIONAL_SORTS:
            natural = await _order(session, library_id, sort)
            same, flipped = ("asc", "desc") if sort in _ASC_BY_NATURE else ("desc", "asc")
            assert await _order(session, library_id, sort, order=flipped) == natural[::-1], sort
            assert await _order(session, library_id, sort, order=same) == natural, sort


async def test_missing_metric_sinks_in_both_directions(db) -> None:
    """没数据的条目不是"最小值"：反过来排，它也不该跑到墙最前面。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        blank = MediaItem(kind="movie", tmdb_id=497, title="没有档案", original_title="Z")
        session.add(blank)
        await session.flush()
        assert blank.id is not None
        session.add(_file(library_id, blank.id))
        await session.flush()

        assert (await _order(session, library_id, "rating", order="asc"))[-1] == "没有档案"
        assert (await _order(session, library_id, "runtime", order="desc"))[-1] == "没有档案"


async def test_reversed_index_is_the_natural_index_backwards(db) -> None:
    """索引跟着墙一起反：档的先后倒过来，每档起点仍指向该档在反向墙里的第一格。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        for sort, flipped in (("title", "desc"), ("rating", "asc")):
            natural = await build_library_index(session, library_id, sort, member_id=_ME)
            backwards = await build_library_index(
                session, library_id, sort, member_id=_ME, order=flipped
            )
            total = sum(count for _, count, _ in natural)
            assert [(label, count) for label, count, _ in backwards] == [
                (label, count) for label, count, _ in reversed(natural)
            ], sort
            assert [start for _, _, start in backwards] == [
                total - start - count for _, count, start in reversed(natural)
            ], sort


# ---------------------------------------------------------------------------
# 放宽建议（铁律 2：永不空货架）
# ---------------------------------------------------------------------------


async def _relax(session, library_id, filters):
    from movieclaw_api.services.library.items import build_library_relax

    return await build_library_relax(
        session, library_id, "movie", filters=filters, member_id=_ME
    )


async def test_relax_suggests_the_condition_that_saves_the_most(db) -> None:
    """筛空时给出路：去掉哪一条能救回多少部，按能救回的数量倒序。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        # 动画 + 韩国 = 0 部；去掉韩国还剩 2 部动画，去掉动画还剩 1 部韩国片
        got = await _relax(session, library_id, LibraryFilter(genres=(16,), countries=("KR",)))

        assert got.total == 0
        assert [(s.dim, s.label, s.count) for s in got.suggestions] == [
            ("countries", "韩国", 2),
            ("genres", "动画", 1),
        ]


async def test_relax_only_lists_conditions_that_actually_help(db) -> None:
    """「去掉它还是 0 部」是噪音不是建议，一条都不列。

    纪录片 + 日韩 + 已看完：三条里去掉任意一条仍然是 0（本库没有纪录片，
    也没有日韩的已看完影片），所以建议为空，前端只留「清空全部条件」。
    """
    async with db.session() as session:
        library_id, _ = await _seed(session)
        dead = LibraryFilter(genres=(99,), countries=("JP", "KR"), watch="played")
        got = await _relax(session, library_id, dead)

        assert got.total == 0
        assert got.suggestions == []


async def test_selected_values_never_vanish_from_their_own_dimension(db) -> None:
    """选中的取值一定还在本维的候选里，哪怕被别的维度收窄到 0 部。

    不这样的话：勾了「动画 + 科幻」再勾「日本」，本库的日本片里没有科幻，
    科幻就从类型下拉里整个消失——用户既取消不掉它，条件行也查不到它的中文名，
    只能把裸的 TMDB id「878」印在界面上。
    """
    async with db.session() as session:
        library_id, _ = await _seed(session)
        got = await _facets(
            session, library_id, filters=LibraryFilter(genres=(16, 878), countries=("JP",))
        )
        by_value = {row.value: row for row in got.genres}
        assert "878" in by_value, [r.value for r in got.genres]
        assert by_value["878"].count == 0
        assert by_value["878"].label != "878", "补出来的那条也要有展示名"


async def test_relax_covers_secondary_dimensions_too(db) -> None:
    """二级维度也要给建议——不给的话界面会说一句假话。

    只看一级四维时，「4K + 评分≥9」这种全靠二级维度筛空的组合一条建议都
    给不出，空态却会写「去掉任意一条也救不回来」——而去掉评分明明就救得回来。
    """
    async with db.session() as session:
        library_id, ids = await _seed(session)
        # 唯一的 4K 是《寄生虫》（8.6 分），所以「4K + 评分≥9」是 0 部，
        # 而去掉评分就能救回它
        row = _file(library_id, ids["寄生虫"])
        row.file_path = "/movies/parasite-4k.mkv"
        row.resolution = "2160p"
        session.add(row)
        await session.flush()

        got = await _relax(session, library_id, LibraryFilter(resolutions=("2160p",), rating_gte=9))

        assert got.total == 0
        assert [(s.dim, s.label, s.count) for s in got.suggestions] == [("rating_gte", "≥ 9", 1)]


async def test_relax_caps_at_three(db) -> None:
    """最多三条：再多就不是建议，是又一份要读的清单。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        many = LibraryFilter(
            genres=(16, 18, 878), countries=("JP",), decades=("1990s",), watch="unwatched"
        )
        got = await _relax(session, library_id, many)
        assert len(got.suggestions) <= 3
        assert all(s.count > 0 for s in got.suggestions)


async def test_relax_labels_are_human(db) -> None:
    """建议文案直接可读：维度名 + 取值展示名，不给前端留翻译活。"""
    async with db.session() as session:
        library_id, _ = await _seed(session)
        got = await _relax(session, library_id, LibraryFilter(genres=(16,), countries=("KR",)))
        first = got.suggestions[0]
        assert first.dim_label in {"类型", "地区", "年代", "观看"}
        assert first.label and not first.label.isdigit()


# ---------------------------------------------------------------------------
# 二级筛选：找片（刮削档案）与查库（库存台账）
# ---------------------------------------------------------------------------


async def test_rating_and_runtime_are_second_tier_find_filters(db) -> None:
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        assert await _titles(session, library_id, filters=LibraryFilter(rating_gte=9)) == {
            "盗梦空间",
            "霸王别姬",
        }
        # 片长档是左开右闭：106 分钟落在 90to120，125 与 132 落在 gt120
        assert await _titles(session, library_id, filters=LibraryFilter(runtimes=("90to120",))) == {
            "你的名字",
        }
        assert await _titles(session, library_id, filters=LibraryFilter(runtimes=("gt120",))) == {
            "千与千寻",
            "寄生虫",
            "盗梦空间",
            "霸王别姬",
        }


async def test_quality_filters_are_scoped_to_this_library(db) -> None:
    """画质是**文件级**条件，必须限定本库。

    同一部片散在两个库时，「本库有没有 4K」问的是这个库，不是全世界——
    否则海报墙会显示一部在本库其实只有 1080p 的片。
    """
    async with db.session() as session:
        library_id, ids = await _seed(session)
        other = await LibraryRepository(session).create(
            name="备份库", kind="movie", root_paths=["/backup"]
        )
        assert other.id is not None
        here = _file(library_id, ids["寄生虫"])
        here.file_path = "/movies/here-4k.mkv"
        here.resolution = "2160p"
        elsewhere = _file(other.id, ids["盗梦空间"])
        elsewhere.file_path = "/backup/there-4k.mkv"
        elsewhere.resolution = "2160p"
        session.add_all([here, elsewhere])
        await session.flush()

        got = await _titles(session, library_id, filters=LibraryFilter(resolutions=("2160p",)))
        assert got == {"寄生虫"}, "另一个库里的 4K 不该让这面墙认为本库有 4K"


async def test_hdr_filter_has_both_directions(db) -> None:
    """HDR 是三态里的两问：只看 HDR / 只看 SDR。不给条件才是「都看」。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        row = _file(library_id, ids["千与千寻"])
        row.file_path = "/movies/hdr.mkv"
        row.hdr = "HDR10"
        session.add(row)
        await session.flush()

        assert await _titles(session, library_id, filters=LibraryFilter(hdr=True)) == {"千与千寻"}
        sdr = await _titles(session, library_id, filters=LibraryFilter(hdr=False))
        assert "千与千寻" not in sdr and len(sdr) == 4


async def test_stock_state_finds_what_needs_attention(db) -> None:
    """查库维度回答的是「哪些要处理」：文件失联、没刮到档案。"""
    async with db.session() as session:
        library_id, ids = await _seed(session)
        lost = _file(library_id, ids["霸王别姬"])
        lost.file_path = "/movies/lost.mkv"
        lost.missing_since = utcnow()
        blank = MediaItem(kind="movie", tmdb_id=497, title="没刮到", original_title="Z")
        session.add_all([lost, blank])
        await session.flush()
        assert blank.id is not None
        session.add(_file(library_id, blank.id))
        await session.flush()

        assert await _titles(session, library_id, filters=LibraryFilter(stock=("missing",))) == {
            "霸王别姬"
        }
        assert await _titles(session, library_id, filters=LibraryFilter(stock=("unscraped",))) == {
            "没刮到"
        }


async def test_second_tier_combines_with_first_tier(db) -> None:
    """二级维度与一级维度同样是维间 AND——它们只是摆在不同的面板上。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)
        got = await _titles(
            session,
            library_id,
            filters=LibraryFilter(countries=("JP",), rating_gte=8.5),
        )
        assert got == {"千与千寻"}


async def test_second_tier_facets_only_when_asked(db) -> None:
    """二级维度是十几条 COUNT，默认不算——常用路径不该为没打开的面板买单。"""
    async with db.session() as session:
        library_id, _ = await _seed_metrics(session)

        primary = await _facets(session, library_id)
        assert primary.genres and primary.ratings == [] and primary.resolutions == []

        full = await _facets(session, library_id, tier="all")
        assert {r.value: r.count for r in full.ratings}["9"] == 2
        assert {r.value: r.count for r in full.runtimes}["gt120"] == 4
        assert {r.value: r.count for r in full.stock}["unscraped"] == 0
        assert {r.value: r.count for r in full.hdr}["0"] == 5


async def test_wall_count_never_disagrees_with_the_page(db) -> None:
    """``_wall_count`` 与 ``_wall_page_ids`` 必须逐档一致。

    合集列表为了不把成员整批取出来（几十个系列合集时那是道悬崖），数数走的是
    一条 ``COUNT(DISTINCT)``。两处收窄条件共用 ``_narrow`` + ``_wall_scope``，
    但"共用"是纪律、这条用例才是保证：口径一旦分叉，合集卡片会说 8 部、
    点进去只有 7 部。
    """
    async with db.session() as session:
        library_id, ids = await _seed_metrics(session)
        cases = [
            LibraryFilter(),
            LibraryFilter(genres=(16,)),
            LibraryFilter(countries=("JP",), rating_gte=8.5),
            LibraryFilter(decades=("2010s",)),
            LibraryFilter(genres=(16, 878), languages=("ja",)),
        ]
        for filters in cases:
            for sort in ("title", "added_at", "release_date", "rating"):
                page = await _wall_page_ids(
                    session, library_id, sort, None, 0, "confirmed", filters, _ME
                )
                assert await _wall_count(session, library_id, "confirmed", filters, _ME) == len(
                    page
                ), f"{sort} / {filters}"
        assert len(ids) > 0


async def test_trashed_items_leave_the_wall(db) -> None:
    """文件进回收站后条目就不在墙上了——与库卡片的作品数同一口径。

    在此之前墙的成员查询完全不看文件状态：用户把最后一个文件移进回收站，
    库卡片的作品数减一、墙上那部片还摆着，同一个库两个数字。
    """
    from movieclaw_db.models import FileState, LibraryFile

    async with db.session() as session:
        library_id, ids = await _seed(session)
        before = await _titles(session, library_id)
        target = sorted(before)[0]
        row = (
            await session.execute(
                select(LibraryFile)
                .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)
                .where(LibraryFile.library_id == library_id, MediaItem.title == target)
            )
        ).scalars().first()
        row.state = FileState.TRASHED
        session.add(row)
        await session.flush()
        assert target not in await _titles(session, library_id)

        # 失联的片**仍在架**：用户要看得见它才知道该去插硬盘，而「有文件失联」
        # 那一档筛选找的正是这些片
        row.state = FileState.MISSING
        session.add(row)
        await session.flush()
        assert target in await _titles(session, library_id)
        assert len(ids) > 1
