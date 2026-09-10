"""作品系列（docs/design/library-series-collections.md）。

覆盖三条主线各自的落点：``series_key`` 怎么构造、NFO 的 ``<set>`` 怎么读写、
以及"系列合集就是一条规则驱动的合集"这件事在接口层是不是真的成立。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.nfo import (
    read_entry_metadata,
    read_local_sidecar,
    write_full_nfo,
)
from movieclaw_api.services.library.series import (
    SERIES_KEY_NONE,
    build_series_key,
    normalize_series_name,
    series_builtin,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    Library,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    utcnow,
)

#: 《哈利·波特》在 TMDB 上的系列 id，用作贯穿全文的例子
POTTER = 1241


# ---------------------------------------------------------------------------
# series_key：一个 key，不是两列
# ---------------------------------------------------------------------------


def test_tmdb_id_always_wins_over_the_name() -> None:
    """两者都有时只落 tmdb: 那一支。

    并存的话同一部片会同时落进两个几乎一样的合集——用户看到两个《哈利·波特》。
    """
    assert build_series_key(POTTER, "哈利·波特系列") == f"tmdb:{POTTER}"
    assert build_series_key(None, "哈利·波特系列") == "name:哈利·波特系列"
    # 两支天然不相交：name: 只出现在没有 TMDB 身份的条目上
    assert build_series_key(POTTER, None).startswith("tmdb:")


def test_no_series_is_an_empty_string_not_none() -> None:
    """"查过了，没有系列" 与 "还没查过" 必须分得开。

    分不开的话，存量回填每一轮都会把"本来就没有系列"的片重新问一遍 TMDB，
    永远跑不完。
    """
    assert build_series_key(None, None) == SERIES_KEY_NONE
    assert build_series_key(None, "   ") == SERIES_KEY_NONE


def test_name_normalisation_is_conservative() -> None:
    """只去空白 + 折叠大小写。繁简转换、删「系列」后缀这类都是猜。

    猜错了会把两个不同的系列并成一个，而并错了比分开更难发现。
    """
    assert normalize_series_name("Harry Potter") == normalize_series_name("harrypotter")
    assert normalize_series_name("哈利波特 系列") == "哈利波特系列"
    # 「系列」不是噪音词：真有叫法不同的两个系列靠它区分
    assert normalize_series_name("哈利波特") != normalize_series_name("哈利波特系列")


# ---------------------------------------------------------------------------
# NFO：写出去 Kodi/Emby 认得，读回来两种写法都认
# ---------------------------------------------------------------------------


def test_nfo_carries_the_set_tag(tmp_path: Path) -> None:
    """``<set>`` 是 Emby/Jellyfin/Kodi 认合集的唯一依据，写出去要有。"""
    entry = tmp_path / "哈利·波特与魔法石 (2001)"
    entry.mkdir()
    item = MediaItem(
        id=1, kind="movie", tmdb_id=671, title="哈利·波特与魔法石", original_title="Harry Potter"
    )
    meta = MediaMetadata(
        media_item_id=1,
        series_key=f"tmdb:{POTTER}",
        series_name="哈利·波特系列",
        scraped_at=utcnow(),
    )
    write_full_nfo(entry, item, meta)
    root = ET.fromstring((entry / "movie.nfo").read_text(encoding="utf-8"))
    assert root.findtext("set/name") == "哈利·波特系列"
    # 不发明非标写法：<set> 里不塞 tmdbid
    assert root.find("set/tmdbid") is None


def test_nfo_without_a_series_has_no_set_tag(tmp_path: Path) -> None:
    """不属于任何系列的片不该凭空多出一个空 ``<set>``。"""
    entry = tmp_path / "盗梦空间 (2010)"
    entry.mkdir()
    item = MediaItem(
        id=1, kind="movie", tmdb_id=27205, title="盗梦空间", original_title="Inception"
    )
    write_full_nfo(entry, item, MediaMetadata(media_item_id=1, scraped_at=utcnow()))
    root = ET.fromstring((entry / "movie.nfo").read_text(encoding="utf-8"))
    assert root.find("set") is None


@pytest.mark.parametrize(
    "xml",
    [
        "<movie><title>片</title><set>哈利·波特系列</set></movie>",  # Kodi v17-
        "<movie><title>片</title><set><name>哈利·波特系列</name></set></movie>",  # v18+ / TMM
    ],
)
def test_both_set_writings_are_understood(tmp_path: Path, xml: str) -> None:
    """两种写法都认——第三方整理过的库两种都见得到。"""
    nfo = tmp_path / "movie.nfo"
    nfo.write_text(xml, encoding="utf-8")
    assert read_entry_metadata(nfo).series_name == "哈利·波特系列"
    assert read_local_sidecar(nfo).series_name == "哈利·波特系列"


def test_missing_set_reads_as_none(tmp_path: Path) -> None:
    nfo = tmp_path / "movie.nfo"
    nfo.write_text("<movie><title>片</title></movie>", encoding="utf-8")
    assert read_entry_metadata(nfo).series_name is None


# ---------------------------------------------------------------------------
# 接口：系列合集就是一条规则驱动的合集
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """一个电影库 + 三部《哈利·波特》 + 一部无系列的片。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'series.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            library = Library(name="电影", kind="movie", root_paths=[str(tmp_path / "media")])
            other = Library(name="剧集", kind="tv", root_paths=[str(tmp_path / "tv")])
            session.add_all([library, other])
            await session.flush()
            rows = [
                (671, "哈利·波特与魔法石", 2001, f"tmdb:{POTTER}", "哈利·波特系列"),
                (672, "哈利·波特与密室", 2002, f"tmdb:{POTTER}", "哈利·波特系列"),
                (673, "哈利·波特与阿兹卡班的囚徒", 2004, f"tmdb:{POTTER}", "哈利·波特系列"),
                (27205, "盗梦空间", 2010, SERIES_KEY_NONE, None),
                # 只入库了一部的系列：< 2 不下发，否则合集页塞满单片盒子
                (1893, "星球大战前传1", 1999, "tmdb:10", "星球大战系列"),
            ]
            for tmdb_id, title, year, key, name in rows:
                item = MediaItem(
                    kind="movie", tmdb_id=tmdb_id, title=title, original_title=title, year=year
                )
                session.add(item)
                await session.flush()
                session.add_all(
                    [
                        MediaMetadata(
                            media_item_id=item.id,
                            genre_ids=[12],
                            release_date=date(year, 1, 1),
                            series_key=key,
                            series_name=name,
                            scraped_at=utcnow(),
                        ),
                        LibraryFile(
                            library_id=library.id,
                            media_item_id=item.id,
                            season_number=0,
                            episode_number=0,
                            file_path=str(tmp_path / "media" / f"{tmdb_id}.mkv"),
                            size_bytes=4096,
                            source=FileSource.SCANNED,
                            state=FileState.IN_PLACE,
                        ),
                    ]
                )
            await session.commit()
        await dispose_db()

    asyncio.run(_seed())

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


async def _ensure(library_id: int = 1) -> None:
    from movieclaw_api.services.library.series import ensure_series_collections_for_library

    async with get_database().session() as session:
        await ensure_series_collections_for_library(session, library_id)
        await session.commit()


def test_series_becomes_a_rule_driven_collection(client: TestClient) -> None:
    """一条 ``GROUP BY`` 补齐之后，系列就是合集列表里的一行——没有新实体。"""
    asyncio.run(_ensure())
    rows = client.get("/api/v1/collections?library_id=1").json()["data"]
    potter = next(row for row in rows if row["builtin"] == series_builtin(f"tmdb:{POTTER}", 1))
    assert potter["name"] == "哈利·波特系列"
    assert potter["kind"] == "series"
    assert potter["rule_driven"] is True
    assert potter["editable"] is False  # 自动生成的规则不可改
    assert potter["item_count"] == 3
    # 系列要按上映顺序看，不是按标题
    assert potter["sort"] == "release_date"


def test_a_single_film_is_not_a_series(client: TestClient) -> None:
    """成员 < 2 不下发——TMDB 的系列里常有只入库了一部的。"""
    asyncio.run(_ensure())
    rows = client.get("/api/v1/collections?library_id=1").json()["data"]
    assert all(row["builtin"] != series_builtin("tmdb:10", 1) for row in rows)
    # 行是建了的，只是不列：以后补齐第二部它自己就出现，不需要再 ensure
    admin = client.get("/api/v1/collections?library_id=1&include_empty=true").json()["data"]
    assert any(row["builtin"] == series_builtin("tmdb:10", 1) for row in admin)


def test_members_match_the_wall_exactly(client: TestClient) -> None:
    """合集成员 == 同一条规则在海报墙上的命中集。

    这是整条链路的地基：不成立的话后面每一层都会各写一套查询。
    """
    asyncio.run(_ensure())
    rows = client.get("/api/v1/collections?library_id=1").json()["data"]
    potter = next(row for row in rows if row["kind"] == "series")
    members = client.get(f"/api/v1/collections/{potter['id']}/items").json()["data"]
    wall = client.get(
        f"/api/v1/libraries/1/items?series_keys=tmdb:{POTTER}&sort=release_date"
    ).json()["data"]
    assert [row["media_item_id"] for row in members] == [row["media_item_id"] for row in wall]


def test_ensure_is_idempotent_and_does_not_revive_a_tombstone(client: TestClient) -> None:
    """跑第二遍不多建行；用户藏起来的系列不会被下一次扫描顶回来。"""
    asyncio.run(_ensure())
    rows = client.get("/api/v1/collections?library_id=1&include_empty=true").json()["data"]
    potter = next(row for row in rows if row["kind"] == "series")

    assert client.delete(f"/api/v1/collections/{potter['id']}").status_code == 200
    asyncio.run(_ensure())  # 再扫一次
    listed = client.get("/api/v1/collections?library_id=1&include_empty=true").json()["data"]
    assert all(row["kind"] != "series" or row["id"] != potter["id"] for row in listed)
    tombs = client.get(
        "/api/v1/collections?library_id=1&include_empty=true&include_hidden=true"
    ).json()["data"]
    assert sum(1 for row in tombs if row["id"] == potter["id"]) == 1


def test_the_switch_controls_display_only(client: TestClient) -> None:
    """关掉开关只是不建合集行——``series_key`` 与 NFO 不受影响。"""
    resp = client.put(
        "/api/v1/libraries/1",
        json={
            "name": "电影",
            "kind": "movie",
            "root_paths": ["/tmp/media"],
            "auto_series_collections": False,
        },
    )
    assert resp.status_code == 200, resp.text
    asyncio.run(_ensure_for_item())
    rows = client.get("/api/v1/collections?library_id=1&include_empty=true").json()["data"]
    assert all(row["kind"] != "series" for row in rows)
    # 数据照落：条目详情仍然说得出它属于哪个系列
    detail = client.get("/api/v1/libraries/1/items/1").json()["data"]
    assert detail["series_name"] == "哈利·波特系列"
    assert detail["series_collection_id"] is None  # 没生成合集就不给入口


async def _ensure_for_item(media_item_id: int = 1) -> None:
    from movieclaw_api.services.library.series import ensure_series_collections_for_item

    async with get_database().session() as session:
        await ensure_series_collections_for_item(session, media_item_id)
        await session.commit()


def test_item_detail_links_into_the_series(client: TestClient) -> None:
    """影片页给的是**本库真有的**那个合集 id，不是一个点了 404 的入口。"""
    asyncio.run(_ensure())
    detail = client.get("/api/v1/libraries/1/items/1").json()["data"]
    assert detail["series_name"] == "哈利·波特系列"
    assert detail["series_collection_id"] is not None
    # 藏起来之后入口就撤掉：同一件事不能说两句不一样的话
    client.delete(f"/api/v1/collections/{detail['series_collection_id']}")
    assert client.get("/api/v1/libraries/1/items/1").json()["data"]["series_collection_id"] is None
