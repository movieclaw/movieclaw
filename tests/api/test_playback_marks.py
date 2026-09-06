"""网页端已看 / 收藏标记接口（``/playback/marks``）。

核心断言只有一条：**与 Jellyfin 客户端点的是同一份数据**——网页上点的心与对勾
落在 ``playback_state`` 的同一行、同一哨兵约定上，Jellyfin 的 ``/Items`` UserData
读出来必须一致，反过来 Jellyfin 点的网页也必须看到。其余覆盖整剧级联、
成员隔离、不可见条目 404、webhook 事件产生。
"""

from __future__ import annotations

import itertools
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.playback import marks
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    PlaybackState,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_jellyfin.ids import item_guid

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'marks.db'}")
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


@pytest.fixture
def emitted(monkeypatch) -> list:
    captured: list = []
    monkeypatch.setattr(marks, "emit_events", captured.extend)
    return captured


_seed_counter = itertools.count(1)


async def _seed(tmp_path: Path) -> dict[str, int]:
    """一部电影 + 一部三集剧，各落在位文件。"""
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
        movie = MediaItem(kind="movie", tmdb_id=1000 + n, title="电影", original_title="M")
        show = MediaItem(kind="tv", tmdb_id=2000 + n, title="剧", original_title="S")
        session.add_all([movie, show])
        await session.flush()
        assert movie.id and show.id and movies.id and shows.id
        session.add(_file(movie.id, movies.id, 0, 0, f"movie{n}.mkv"))
        for e in (1, 2, 3):
            session.add(_file(show.id, shows.id, 1, e, f"S01E0{e}-{n}.mkv"))
            session.add(MediaEpisode(media_item_id=show.id, season_number=1, episode_number=e))
        await session.commit()
        return {"movie": movie.id, "show": show.id, "shows_library": shows.id}


def seed(client: TestClient, tmp_path: Path) -> dict[str, int]:
    return client.portal.call(partial(_seed, tmp_path))  # type: ignore[attr-defined]


def get_marks(client: TestClient, **params) -> dict:
    resp = client.get(f"{_PB}/marks", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def set_marks(client: TestClient, **body) -> dict:
    resp = client.post(f"{_PB}/marks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _rows(item_id: int) -> dict[tuple[int, int, int], PlaybackState]:
    async with get_database().session() as session:
        rows = (
            await session.execute(
                select(PlaybackState).where(PlaybackState.media_item_id == item_id)
            )
        ).scalars()
        return {(r.member_id, r.season_number, r.episode_number): r for r in rows}


def rows(client: TestClient, item_id: int) -> dict:
    return client.portal.call(partial(_rows, item_id))  # type: ignore[attr-defined]


def jf_auth(client: TestClient) -> dict:
    """走 Jellyfin 协议登录，拿设备 token（超管 → member_id=0，与会话同一人）。"""
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


# ---------------------------------------------------------------------------
# 与 Jellyfin 同一份数据
# ---------------------------------------------------------------------------


def test_web_favorite_visible_in_jellyfin_and_back(client, tmp_path):
    """网页点心 → Jellyfin 的 UserData.IsFavorite 为真；Infuse 取消 → 网页读到假。"""
    ids = seed(client, tmp_path)
    auth = jf_auth(client)
    movie_guid = item_guid(ids["movie"])

    assert get_marks(client, media_item_id=ids["movie"]) == {
        "played": False,
        "is_favorite": False,
        "unplayed_count": None,
    }
    assert set_marks(client, media_item_id=ids["movie"], favorite=True)["is_favorite"] is True
    assert client.get(f"/Items/{movie_guid}", params=auth).json()["UserData"]["IsFavorite"] is True

    # Jellyfin 端取消收藏，网页立刻看到
    assert (
        client.request("DELETE", f"/UserFavoriteItems/{movie_guid}", params=auth).json()[
            "IsFavorite"
        ]
        is False
    )
    assert get_marks(client, media_item_id=ids["movie"])["is_favorite"] is False


def test_series_favorite_uses_folder_sentinel(client, tmp_path):
    """整剧收藏落在 (-1,-1) 哨兵行——与 Jellyfin 兼容层读 Series.UserData 的
    约定一致，且绝不污染 S00E00 / 任何真实单元。"""
    ids = seed(client, tmp_path)
    auth = jf_auth(client)
    show_guid = item_guid(ids["show"])

    set_marks(client, media_item_id=ids["show"], favorite=True)
    state = rows(client, ids["show"])
    assert state[(0, -1, -1)].is_favorite is True
    assert (0, 0, 0) not in state
    assert client.get(f"/Items/{show_guid}", params=auth).json()["UserData"]["IsFavorite"] is True

    # 反向：Infuse 里给整剧点心，网页读整剧目标为真、读单集目标仍为假
    client.request("DELETE", f"/UserFavoriteItems/{show_guid}", params=auth)
    assert get_marks(client, media_item_id=ids["show"])["is_favorite"] is False
    client.post(f"/UserFavoriteItems/{show_guid}", params=auth)
    assert get_marks(client, media_item_id=ids["show"])["is_favorite"] is True
    assert (
        get_marks(client, media_item_id=ids["show"], season_number=1, episode_number=1)[
            "is_favorite"
        ]
        is False
    )


def test_series_played_cascades_like_jellyfin(client, tmp_path):
    """整剧标记已看级联到全部集；取消只清本季；Jellyfin 的 UnplayedItemCount 同步。"""
    ids = seed(client, tmp_path)
    auth = jf_auth(client)
    show_guid = item_guid(ids["show"])

    assert set_marks(client, media_item_id=ids["show"], played=True) == {
        "played": True,
        "is_favorite": False,
        "unplayed_count": 0,
    }
    state = rows(client, ids["show"])
    assert all(state[(0, 1, e)].played for e in (1, 2, 3))
    assert all(state[(0, 1, e)].play_count == 1 for e in (1, 2, 3))
    assert (
        client.get(f"/Items/{show_guid}", params=auth).json()["UserData"]["UnplayedItemCount"] == 0
    )

    # 单集取消：整剧目标变成「没看完，剩 1 集」
    single = set_marks(
        client, media_item_id=ids["show"], season_number=1, episode_number=2, played=False
    )
    assert single == {"played": False, "is_favorite": False, "unplayed_count": None}
    assert get_marks(client, media_item_id=ids["show"]) == {
        "played": False,
        "is_favorite": False,
        "unplayed_count": 1,
    }
    assert get_marks(client, media_item_id=ids["show"], season_number=1)["unplayed_count"] == 1

    # Infuse 把整剧标回已看，网页读到全看完
    client.post(f"/UserPlayedItems/{show_guid}", params=auth)
    assert get_marks(client, media_item_id=ids["show"])["played"] is True


def test_played_mark_resets_resume_point(client, tmp_path):
    """标已看清零续播点（对齐 Jellyfin）：播放按钮回到「重新播放」；取消已看
    则播放次数一并清零（ResetPlayedState 语义，不是减一）。"""
    ids = seed(client, tmp_path)
    client.post(
        f"{_PB}/progress",
        json={"media_item_id": ids["movie"], "event": "start"},
    )
    client.post(
        f"{_PB}/progress",
        json={"media_item_id": ids["movie"], "event": "progress", "position_ms": 120_000},
    )
    set_marks(client, media_item_id=ids["movie"], season_number=0, episode_number=0, played=True)
    resume = client.get(f"{_PB}/resume", params={"media_item_id": ids["movie"]}).json()["data"]
    assert resume["played"] is True and resume["position_ms"] == 0 and resume["play_count"] == 1

    set_marks(client, media_item_id=ids["movie"], played=False)
    resume = client.get(f"{_PB}/resume", params={"media_item_id": ids["movie"]}).json()["data"]
    assert resume["played"] is False and resume["play_count"] == 0


# ---------------------------------------------------------------------------
# 事件与校验
# ---------------------------------------------------------------------------


def test_marks_emit_webhook_events_with_web_client(client, tmp_path, emitted):
    """网页端标记与 Jellyfin 端一样产生事件，client 标成网页播放器。"""
    ids = seed(client, tmp_path)
    set_marks(client, media_item_id=ids["show"], favorite=True, device_id="browser-a")
    set_marks(client, media_item_id=ids["show"], played=True)
    names = [e.event for e in emitted]
    assert names.count("item.favorited") == 1
    assert names.count("playback.marked_played") == 3  # 级联三集逐条发
    fav = next(e for e in emitted if e.event == "item.favorited")
    assert fav.data["media"]["type"] == "series"
    assert fav.data["client"]["name"] == "MovieClaw Web"
    assert fav.data["client"]["device_id"] == "web-0-browser-a"
    batch_ids = {e.batch_id for e in emitted if e.event == "playback.marked_played"}
    assert len(batch_ids) == 1 and None not in batch_ids


def test_marks_require_a_field_and_a_season_for_episode(client, tmp_path):
    ids = seed(client, tmp_path)
    assert client.post(f"{_PB}/marks", json={"media_item_id": ids["movie"]}).status_code == 400
    assert (
        client.post(
            f"{_PB}/marks", json={"media_item_id": ids["show"], "episode_number": 1, "played": True}
        ).status_code
        == 400
    )
    # 目标季不存在：没有可标记的单元
    assert (
        client.post(
            f"{_PB}/marks", json={"media_item_id": ids["show"], "season_number": 9, "played": True}
        ).status_code
        == 404
    )


def test_marks_are_isolated_per_member_and_hidden_library_is_404(client, tmp_path):
    """成员各点各的心；成员看不见的库里的条目，标记接口同样 404。"""
    ids = seed(client, tmp_path)
    set_marks(client, media_item_id=ids["movie"], favorite=True)

    # 建一个只看得到剧集库的成员（新建只收账号，白名单走编辑），切到其会话
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
    login = client.post(
        "/api/v1/auth/login", json={"username": "family", "password": "family-pass-1"}
    )
    assert login.status_code == 200, login.text

    assert client.get(f"{_PB}/marks", params={"media_item_id": ids["movie"]}).status_code == 404
    assert get_marks(client, media_item_id=ids["show"])["is_favorite"] is False
    set_marks(client, media_item_id=ids["show"], favorite=True)
    state = rows(client, ids["show"])
    member_rows = [k for k in state if k[0] != 0]
    assert len(member_rows) == 1 and state[member_rows[0]].is_favorite is True
    assert (0, -1, -1) not in state  # 超管名下没被顺手写上
