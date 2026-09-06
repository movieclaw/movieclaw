"""媒体库首页「我的收藏」接口（``/playback/favorites``）。

数据源是 ``playback_state.is_favorite``——网页详情页与 Jellyfin 客户端写的同一列，
所以这里一半收藏走网页接口、一半走 Jellyfin 协议，首页必须两边都列出来。
其余覆盖：按作品去重与层级翻译、最近收藏在前、成员隔离、不可见库不露出、
limit 截断时 total 仍是全量。
"""

from __future__ import annotations

import itertools
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaEpisode, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_jellyfin.ids import episode_guid, item_guid, season_guid

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'favorites.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    monkeypatch.setattr("movieclaw_api.api.routes.playback.available_backends", lambda: ())

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


_seed_counter = itertools.count(1)


async def _seed(tmp_path: Path) -> dict[str, int]:
    """两部电影 + 一部三集剧，各落在位文件。"""
    n = next(_seed_counter)
    root = tmp_path / f"media{n}"
    root.mkdir(exist_ok=True)

    def _file(item_id: int, library_id: int, season: int, episode: int, name: str) -> LibraryFile:
        path = root / name
        path.write_bytes(b"FAKE" * 16)
        return LibraryFile(
            library_id=library_id,
            media_item_id=item_id,
            season_number=season,
            episode_number=episode,
            file_path=str(path),
            size_bytes=path.stat().st_size,
            source=FileSource.SCANNED,
            state=FileState.IN_PLACE,
            duration_seconds=600,
        )

    async with get_database().session() as session:
        repo = LibraryRepository(session)
        movies = await repo.create(name=f"电影库{n}", kind="movie", root_paths=[str(root)])
        shows = await repo.create(name=f"剧集库{n}", kind="tv", root_paths=[str(root)])
        movie_a = MediaItem(kind="movie", tmdb_id=1000 + n, title="电影甲", original_title="A")
        movie_b = MediaItem(kind="movie", tmdb_id=3000 + n, title="电影乙", original_title="B")
        show = MediaItem(kind="tv", tmdb_id=2000 + n, title="剧", original_title="S")
        session.add_all([movie_a, movie_b, show])
        await session.flush()
        assert movie_a.id and movie_b.id and show.id and movies.id and shows.id
        session.add(_file(movie_a.id, movies.id, 0, 0, f"movie-a{n}.mkv"))
        session.add(_file(movie_b.id, movies.id, 0, 0, f"movie-b{n}.mkv"))
        for e in (1, 2, 3):
            session.add(_file(show.id, shows.id, 1, e, f"S01E0{e}-{n}.mkv"))
            session.add(MediaEpisode(media_item_id=show.id, season_number=1, episode_number=e))
        await session.commit()
        return {
            "movie_a": movie_a.id,
            "movie_b": movie_b.id,
            "show": show.id,
            "movies_library": movies.id,
            "shows_library": shows.id,
        }


def seed(client: TestClient, tmp_path: Path) -> dict[str, int]:
    return client.portal.call(partial(_seed, tmp_path))  # type: ignore[attr-defined]


def favorites(client: TestClient, **params) -> dict:
    resp = client.get(f"{_PB}/favorites", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def web_favorite(client: TestClient, **target) -> None:
    resp = client.post(f"{_PB}/marks", json={**target, "favorite": True})
    assert resp.status_code == 200, resp.text


def jf_auth(client: TestClient) -> dict:
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": _ADMIN["username"], "Pw": _ADMIN["password"]},
        headers={
            "Authorization": (
                'MediaBrowser Client="Infuse", Device="iPad", DeviceId="ipad-1", Version="7.0"'
            )
        },
    )
    assert resp.status_code == 200, resp.text
    return {"ApiKey": resp.json()["AccessToken"]}


def test_lists_web_and_jellyfin_favorites_newest_first(client, tmp_path):
    """网页收藏的电影与 Infuse 里收藏的单集同列；每部作品一格、最近收藏在前；
    层级由哨兵翻译：单集带季集号，电影不外泄 (0,0)。"""
    ids = seed(client, tmp_path)
    auth = jf_auth(client)

    assert favorites(client) == {"items": [], "total": 0}
    web_favorite(client, media_item_id=ids["movie_a"])
    # 同一部剧先收藏整季、再在 Infuse 里收藏一集：只出一格，层级取最近那次
    client.post(f"/UserFavoriteItems/{season_guid(ids['show'], 1)}", params=auth)
    client.post(f"/UserFavoriteItems/{episode_guid(ids['show'], 1, 2)}", params=auth)

    body = favorites(client)
    assert body["total"] == 2
    assert [i["media_item_id"] for i in body["items"]] == [ids["show"], ids["movie_a"]]
    show, movie = body["items"]
    assert (show["favorite_season_number"], show["favorite_episode_number"]) == (1, 2)
    assert show["library_id"] == ids["shows_library"]
    assert show["kind"] == "tv" and show["episode_count"] == 3  # 海报墙同一套库存口径
    assert (movie["favorite_season_number"], movie["favorite_episode_number"]) == (None, None)
    assert movie["library_id"] == ids["movies_library"]

    # 取消收藏立刻从首页消失
    client.request("DELETE", f"/UserFavoriteItems/{item_guid(ids['movie_a'])}", params=auth)
    assert [i["media_item_id"] for i in favorites(client)["items"]] == [ids["show"]]


def test_series_level_favorite_has_no_season(client, tmp_path):
    ids = seed(client, tmp_path)
    web_favorite(client, media_item_id=ids["show"])
    item = favorites(client)["items"][0]
    assert (item["favorite_season_number"], item["favorite_episode_number"]) == (None, None)


def test_limit_truncates_items_but_total_is_full(client, tmp_path):
    ids = seed(client, tmp_path)
    for key in ("movie_a", "movie_b", "show"):
        web_favorite(client, media_item_id=ids[key])
    body = favorites(client, limit=2)
    assert len(body["items"]) == 2 and body["total"] == 3
    assert [i["media_item_id"] for i in body["items"]] == [ids["show"], ids["movie_b"]]
    # 全部收藏页的滚动加载：offset 接着上一页，total 不变
    page2 = favorites(client, limit=2, offset=2)
    assert [i["media_item_id"] for i in page2["items"]] == [ids["movie_a"]]
    assert page2["total"] == 3
    assert favorites(client, limit=2, offset=3)["items"] == []


def test_favorites_are_per_member_and_hidden_library_excluded(client, tmp_path):
    """成员只看到自己的收藏；不可见库里的收藏既不列出也不计入总数。"""
    ids = seed(client, tmp_path)
    web_favorite(client, media_item_id=ids["movie_a"])

    created = client.post(
        "/api/v1/members",
        json={"username": "family", "password": "family-pass-1", "nickname": "家人"},
    )
    assert created.status_code == 200, created.text
    updated = client.put(
        f"/api/v1/members/{created.json()['data']['id']}",
        json={"all_libraries": False, "library_ids": [ids["shows_library"]]},
    )
    assert updated.status_code == 200, updated.text
    client.post("/api/v1/auth/logout")
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "family", "password": "family-pass-1"}
        ).status_code
        == 200
    )

    assert favorites(client) == {"items": [], "total": 0}
    web_favorite(client, media_item_id=ids["show"])
    body = favorites(client)
    assert body["total"] == 1 and body["items"][0]["media_item_id"] == ids["show"]

    # 超管回来：只有自己那一部，成员的收藏不混进来
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json=_ADMIN).status_code == 200
    assert [i["media_item_id"] for i in favorites(client)["items"]] == [ids["movie_a"]]
