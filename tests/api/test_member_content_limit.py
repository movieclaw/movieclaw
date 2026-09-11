"""儿童档案：内容分级的成员级强制约束（docs/design/library-filtering.md F5）。

**这个功能只要漏一处就是假的**——孩子能从搜索里搜到、从合集里点进去、
从电视端播到，那么海报墙上藏起来只是障眼法。所以这一组用例是按"面"写的：
每一面各来一条，少一面就红。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.content_rating import rating_age, ratings_at_or_below
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

#: 播种谱：(片名, 分级)。None = 未分级（大量中文影片就是这样）
CATALOG = [
    ("动画片", "G"),
    ("合家欢", "PG"),
    ("青春片", "PG-13"),
    ("成人片", "R"),
    ("没分级的", None),
]


# ---------------------------------------------------------------------------
# 折算表：认得出来的才认
# ---------------------------------------------------------------------------


def test_known_systems_map_to_ages() -> None:
    assert rating_age("G") == 0
    assert rating_age("PG-13") == 13
    assert rating_age("R") == 17
    assert rating_age("TV-MA") == 17
    assert rating_age("R15+") == 15
    assert rating_age("FSK 16") == 16  # 体系前缀剥掉之后是裸数字


def test_unknown_ratings_are_not_guessed() -> None:
    """认不出来就说不知道。把某国的符号猜成 0 可能把成人片放给小孩。"""
    assert rating_age(None) is None
    assert rating_age("未分级") is None
    assert rating_age("2016") is None  # 像年份的不当年龄用


def test_allowed_set_covers_the_writings_that_appear_in_libraries() -> None:
    allowed = set(ratings_at_or_below(13))
    assert {"G", "PG", "PG-13", "TV-PG", "12", "13"} <= allowed
    assert "R" not in allowed and "TV-MA" not in allowed


# ---------------------------------------------------------------------------
# 六个面
# ---------------------------------------------------------------------------


@pytest.fixture
def stack(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'kid.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            library = Library(name="电影", kind="movie", root_paths=[str(tmp_path / "media")])
            session.add(library)
            await session.flush()
            for index, (title, rating) in enumerate(CATALOG):
                item = MediaItem(
                    kind="movie",
                    tmdb_id=50_000 + index,
                    title=title,
                    original_title=title,
                    year=2020,
                )
                session.add(item)
                await session.flush()
                session.add_all(
                    [
                        MediaMetadata(
                            media_item_id=item.id,
                            genre_ids=[16],
                            release_date=date(2020, 1, 1),
                            content_rating=rating,
                            scraped_at=utcnow(),
                        ),
                        LibraryFile(
                            library_id=library.id,
                            media_item_id=item.id,
                            season_number=0,
                            episode_number=0,
                            file_path=str(tmp_path / "media" / f"{index}.mkv"),
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
    from movieclaw_db.models import Member

    app = create_app()
    state: dict[str, object] = {"member": None}

    def _principal() -> Principal:
        member = state["member"]
        if member is None:
            return Principal(kind="admin", name="admin")
        return Principal(kind="member", name=member.username, member=member)

    app.dependency_overrides[require_login] = _principal
    with TestClient(app) as client:
        # 建一个成员，默认不限
        resp = client.post(
            "/api/v1/members",
            json={"username": "kiddo", "password": "kiddo-pass-1", "nickname": "小孩"},
        )
        assert resp.status_code == 200, resp.text
        member_id = resp.json()["data"]["id"]

        def become_child(age: int | None, allow_unrated: bool = False) -> None:
            body: dict = {"allow_unrated": allow_unrated}
            body["content_age_limit"] = -1 if age is None else age
            assert client.put(f"/api/v1/members/{member_id}", json=body).status_code == 200

            async def _load():
                async with get_database().session() as session:
                    return await session.get(Member, member_id)

            state["member"] = asyncio.run(_load())

        def become_admin() -> None:
            state["member"] = None

        yield client, become_child, become_admin
    get_settings.cache_clear()


def _titles(rows: list[dict]) -> set[str]:
    return {row["title"] for row in rows}


def test_wall_hides_over_the_limit(stack) -> None:
    client, become_child, become_admin = stack
    become_admin()
    assert _titles(client.get("/api/v1/libraries/1/items").json()["data"]) == {
        title for title, _ in CATALOG
    }

    become_child(13)
    # PG-13 及以下留下；R 与**未分级**都不给看（allow_unrated 默认关）
    assert _titles(client.get("/api/v1/libraries/1/items").json()["data"]) == {
        "动画片",
        "合家欢",
        "青春片",
    }


def test_unrated_can_be_let_through_explicitly(stack) -> None:
    """大量中文影片没有分级信息，一刀切会让库几乎空掉——所以给一个开关。"""
    client, become_child, _ = stack
    become_child(13, allow_unrated=True)
    assert "没分级的" in _titles(client.get("/api/v1/libraries/1/items").json()["data"])
    assert "成人片" not in _titles(client.get("/api/v1/libraries/1/items").json()["data"])


def test_facets_count_what_the_wall_shows(stack) -> None:
    """面板说多少部，墙上就是多少部——分级约束不受 facet 的 skip 影响。"""
    client, become_child, _ = stack
    become_child(13)
    facets = client.get("/api/v1/libraries/1/facets?tier=all").json()["data"]
    wall = client.get("/api/v1/libraries/1/items").json()["data"]
    assert sum(row["count"] for row in facets["genres"]) == len(wall)


def test_search_cannot_find_it(stack) -> None:
    """搜得到就等于看得到（点进去是详情页）。"""
    client, become_child, become_admin = stack
    become_admin()
    groups = client.get("/api/v1/search/library-items?keyword=成人").json()["data"]
    assert groups and _titles(groups[0]["items"]) == {"成人片"}

    become_child(13)
    assert client.get("/api/v1/search/library-items?keyword=成人").json()["data"] == []


def test_collection_members_are_narrowed_too(stack) -> None:
    """合集是"存好的筛选"，绕过约束的话它就是一条后门。"""
    client, become_child, become_admin = stack
    become_admin()
    created = client.post(
        "/api/v1/collections",
        json={
            "name": "全部",
            "library_id": 1,
            "rules": [{"field": "genres", "op": "any_of", "values": []}],
        },
    ).json()["data"]
    assert created["item_count"] == len(CATALOG)

    become_child(13)
    listed = client.get("/api/v1/collections?library_id=1").json()["data"]
    row = next(r for r in listed if r["id"] == created["id"])
    assert row["item_count"] == 3
    members = client.get(f"/api/v1/collections/{created['id']}/items").json()["data"]
    assert _titles(members) == {"动画片", "合家欢", "青春片"}


def test_manual_collection_is_narrowed_too(stack) -> None:
    """手工挑进去的片更要挡——那正是"我给自己存的片单"最容易漏的一种。"""
    client, become_child, become_admin = stack
    become_admin()
    created = client.post(
        "/api/v1/collections",
        json={"name": "手挑", "library_id": 1, "item_ids": [1, 4]},
    ).json()["data"]
    assert created["item_count"] == 2

    become_child(13)
    members = client.get(f"/api/v1/collections/{created['id']}/items").json()["data"]
    assert _titles(members) == {"动画片"}


def test_detail_and_episodes_are_404(stack) -> None:
    """直接改地址栏也进不去：列表藏起来、详情点得进，那道约束就是障眼法。"""
    client, become_child, become_admin = stack
    become_admin()
    assert client.get("/api/v1/libraries/1/items/4").status_code == 200

    become_child(13)
    assert client.get("/api/v1/libraries/1/items/4").status_code == 404
    assert client.get("/api/v1/libraries/1/items/1").status_code == 200


def test_playback_is_blocked_too(stack) -> None:
    """列表藏起来、播放器照放，那道约束就只是障眼法。

    ``/playback/decide`` 此前只按**库范围**收窄，不看内容分级——儿童档案直链
    一个 ``media_item_id`` 过来就能拿到播放计划。收窄只做在浏览面上，挡住的
    是浏览，不是播放。
    """
    client, become_child, become_admin = stack
    body = {"media_item_id": 4, "capability": {}}
    become_admin()
    assert client.post("/api/v1/playback/decide", json=body).status_code != 404

    become_child(13)
    assert client.post("/api/v1/playback/decide", json=body).status_code == 404
    # 分级之内的那部照放（这里没有真文件，能走到"找不到可播放的文件"就说明
    # 它过了可见性这一关，而不是被约束挡在门外）
    allowed = client.post(
        "/api/v1/playback/decide", json={"media_item_id": 1, "capability": {}}
    )
    assert allowed.status_code != 403


def test_limit_can_be_cleared(stack) -> None:
    """取消上限传 -1；传 null 是"不改动"，两者不能混为一谈。"""
    client, become_child, _ = stack
    become_child(13)
    assert len(client.get("/api/v1/libraries/1/items").json()["data"]) == 3
    become_child(None)
    assert len(client.get("/api/v1/libraries/1/items").json()["data"]) == len(CATALOG)
