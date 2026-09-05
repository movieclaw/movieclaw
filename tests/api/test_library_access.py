"""媒体库可见范围（docs/design/library-access.md）的端到端测试。

覆盖：
1. 超管把自己摘出范围：库列表仍列出（viewer_access=false）、配置可读、
   管理动作可用，但海报墙 / 封面 / 条目图片一律 404；勾回自己即恢复；
2. 「指定成员」的库对 all_libraries 成员默认不可见；库设置页勾选成员后可见，
   且与成员管理页的 library_ids 是同一份数据（互通）；
3. 最近观看 / 活动页对范围外的库：首页不出现，活动页只报个数不出片名；
4. 清除观看记录：三种范围只删自己的，其他成员不受影响，范围外的库 404；
5. 令牌主体只看 everyone 库（不继承超管授权）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import Principal, reset_auth_state
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, MediaItem, PlaybackState
from movieclaw_db.models.base import utcnow

_AUTH = "/api/v1/auth"
_MEMBERS = "/api/v1/members"
_LIBS = "/api/v1/libraries"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}
_MEMBER = {"username": "family", "password": "family-pass-1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        resp = c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        assert resp.status_code == 200, resp.text
        yield c
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _login(client: TestClient, username: str, password: str) -> str:
    client.cookies.clear()
    resp = client.post(f"{_AUTH}/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return client.cookies.get("movieclaw_session")


def _use(client: TestClient, cookie: str) -> None:
    client.cookies.clear()
    client.cookies.set("movieclaw_session", cookie)


def _admin_cookie(client: TestClient) -> str:
    """超管 Cookie：建成员前从 jar 里取，建成员后从记录里取（jar 已被切走）。"""
    return _ADMIN_COOKIE.get(id(client)) or client.cookies.get("movieclaw_session")


_ADMIN_COOKIE: dict[int, str] = {}


def _create_member(client: TestClient) -> tuple[int, str]:
    """建默认成员（all_libraries=True），返回 (成员 id, 成员 Cookie)。"""
    admin = _admin_cookie(client)
    _ADMIN_COOKIE[id(client)] = admin
    created = client.post(_MEMBERS, json=_MEMBER)
    assert created.status_code == 200, created.text
    member_id = created.json()["data"]["id"]
    cookie = _login(client, _MEMBER["username"], _MEMBER["password"])
    _use(client, admin)
    return member_id, cookie


def _create_library(client: TestClient, name: str, root: str, **extra) -> int:
    resp = client.post(_LIBS, json={"name": name, "kind": "movie", "root_paths": [root], **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


def _update_library(client: TestClient, library_id: int, **fields) -> dict:
    current = client.get(f"{_LIBS}/{library_id}").json()["data"]
    payload = {
        "name": current["name"],
        "kind": current["kind"],
        "root_paths": current["root_paths"],
        **fields,
    }
    resp = client.put(f"{_LIBS}/{library_id}", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _seed_item(library_id: int, title: str, tmdb_id: int) -> int:
    """直接播种一个有在位台账的电影条目，返回 media_item_id。"""
    async with get_database().session() as session:
        item = MediaItem(
            kind="movie", tmdb_id=tmdb_id, title=title, original_title=title, year=2020, aliases=[]
        )
        session.add(item)
        await session.commit()
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item.id,
                file_path=f"/media/{library_id}/{tmdb_id}.mkv",
                source="scanned",
                size_bytes=1_000,
            )
        )
        await session.commit()
        assert item.id is not None
        return item.id


async def _seed_state(member_id: int, media_item_id: int) -> None:
    async with get_database().session() as session:
        session.add(
            PlaybackState(
                member_id=member_id,
                media_item_id=media_item_id,
                position_ms=60_000,
                play_count=1,
                last_played_at=utcnow(),
            )
        )
        await session.commit()


def _write_poster(media_item_id: int) -> None:
    folder = Path(get_settings().metadata_dir) / "images" / str(media_item_id)
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 30), "#4a6fa5").save(folder / "poster.jpg", "JPEG")


# ---------------------------------------------------------------------------
# 1. 超管：管理权常在，浏览权要自己勾
# ---------------------------------------------------------------------------


async def test_admin_out_of_scope_manages_but_cannot_browse(client: TestClient) -> None:
    lib = _create_library(
        client, "家庭录像", "/m/home", access_mode="selected", admin_visible=False
    )
    item = await _seed_item(lib, "生日录像", 1001)
    _write_poster(item)

    # 列表仍列出，但标记为只有管理权
    rows = {r["id"]: r for r in client.get(_LIBS).json()["data"]}
    assert rows[lib]["access_mode"] == "selected"
    assert rows[lib]["admin_visible"] is False
    assert rows[lib]["viewer_access"] is False
    assert rows[lib]["member_ids"] == []

    # 配置可读、管理面可用（建库已自动排了一次扫描，这里用缺失清单代表管理面）
    assert client.get(f"{_LIBS}/{lib}").status_code == 200
    assert client.get(f"{_LIBS}/{lib}/missing").status_code == 200

    # 浏览面一律 404：海报墙、封面、条目图片
    assert client.get(f"{_LIBS}/{lib}/items").status_code == 404
    assert client.get(f"{_LIBS}/{lib}/cover").status_code == 404
    assert client.get(f"/api/v1/images/assets/{item}/poster.jpg").status_code == 404

    # 勾回自己：全部恢复
    updated = _update_library(client, lib, admin_visible=True)
    assert updated["viewer_access"] is True
    assert client.get(f"{_LIBS}/{lib}/items").status_code == 200
    assert client.get(f"/api/v1/images/assets/{item}/poster.jpg").status_code == 200


# ---------------------------------------------------------------------------
# 2. 成员：指定成员的库默认不含，显式勾选后可见，且与成员页互通
# ---------------------------------------------------------------------------


async def test_selected_library_requires_explicit_member_grant(client: TestClient) -> None:
    shared = _create_library(client, "电影", "/m/movies")
    selected = _create_library(client, "私藏", "/m/private", access_mode="selected")
    item = await _seed_item(selected, "私藏片", 2001)
    _write_poster(item)
    member_id, member_cookie = _create_member(client)

    # all_libraries 成员：只见 everyone 库
    _use(client, member_cookie)
    assert [r["id"] for r in client.get(_LIBS).json()["data"]] == [shared]
    assert client.get(f"{_LIBS}/{selected}").status_code == 404
    assert client.get(f"{_LIBS}/{selected}/items").status_code == 404
    assert client.get(f"/api/v1/images/assets/{item}/poster.jpg").status_code == 404

    # 库设置页勾选该成员
    _use(client, _admin_cookie(client))
    updated = _update_library(client, selected, member_ids=[member_id])
    assert updated["member_ids"] == [member_id]
    # 成员管理页看到同一份授权
    assert client.get(f"{_MEMBERS}/{member_id}").json()["data"]["library_ids"] == [selected]

    _use(client, member_cookie)
    assert {r["id"] for r in client.get(_LIBS).json()["data"]} == {shared, selected}
    assert client.get(f"{_LIBS}/{selected}/items").status_code == 200
    assert client.get(f"/api/v1/images/assets/{item}/poster.jpg").status_code == 200
    # 成员端不暴露名单
    assert all(r["member_ids"] == [] for r in client.get(_LIBS).json()["data"])

    # 从成员管理页取消（切白名单且不含该库）→ 库设置页同步消失
    _use(client, _admin_cookie(client))
    client.put(f"{_MEMBERS}/{member_id}", json={"all_libraries": False, "library_ids": [shared]})
    assert client.get(f"{_LIBS}/{selected}").json()["data"]["member_ids"] == []

    # 未知成员 id 拒绝
    bad = client.put(
        f"{_LIBS}/{selected}",
        json={"name": "私藏", "kind": "movie", "root_paths": ["/m/private"], "member_ids": [9999]},
    )
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# 3. 聚合面：最近观看与活动页
# ---------------------------------------------------------------------------


async def test_recent_watch_and_activity_hide_out_of_scope(client: TestClient) -> None:
    lib = _create_library(client, "私藏", "/m/private", access_mode="selected", admin_visible=False)
    item = await _seed_item(lib, "私藏片", 3001)
    await _seed_state(0, item)

    assert client.get("/api/v1/playback/recent").json()["data"]["items"] == []
    activity = client.get("/api/v1/playback/activity").json()["data"]
    assert activity["recent"] == []
    assert activity["hidden_recent_count"] == 1

    _update_library(client, lib, admin_visible=True)
    assert [
        i["media_item_id"] for i in client.get("/api/v1/playback/recent").json()["data"]["items"]
    ] == [item]
    activity = client.get("/api/v1/playback/activity").json()["data"]
    assert [r["media"]["title"] for r in activity["recent"]] == ["私藏片"]
    assert activity["hidden_recent_count"] == 0


# ---------------------------------------------------------------------------
# 4. 清除观看记录
# ---------------------------------------------------------------------------


async def test_clear_history_scopes_only_touch_own_rows(client: TestClient) -> None:
    lib_a = _create_library(client, "A", "/m/a")
    lib_b = _create_library(client, "B", "/m/b")
    a1 = await _seed_item(lib_a, "A1", 4001)
    a2 = await _seed_item(lib_a, "A2", 4002)
    b1 = await _seed_item(lib_b, "B1", 4003)
    member_id, member_cookie = _create_member(client)
    for item in (a1, a2, b1):
        await _seed_state(0, item)
        await _seed_state(member_id, item)

    def _recent_ids() -> list[int]:
        return sorted(
            i["media_item_id"]
            for i in client.get("/api/v1/playback/recent").json()["data"]["items"]
        )

    # 按条目
    resp = client.delete("/api/v1/playback/history", params={"scope": "item", "media_item_id": a1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted_states"] == 1
    assert _recent_ids() == sorted([a2, b1])

    # 按库
    resp = client.delete(
        "/api/v1/playback/history", params={"scope": "library", "library_id": lib_a}
    )
    assert resp.json()["data"]["deleted_states"] == 1
    assert _recent_ids() == [b1]

    # 全部
    resp = client.delete("/api/v1/playback/history", params={"scope": "all"})
    assert resp.json()["data"]["deleted_states"] == 1
    assert _recent_ids() == []

    # 成员的记录一条没动
    _use(client, member_cookie)
    assert _recent_ids() == sorted([a1, a2, b1])

    # 范围外的库不能按库清
    _use(client, _admin_cookie(client))
    _update_library(client, lib_b, access_mode="selected", admin_visible=False)
    resp = client.delete(
        "/api/v1/playback/history", params={"scope": "library", "library_id": lib_b}
    )
    assert resp.status_code == 404
    # 缺参数 400
    assert client.delete("/api/v1/playback/history", params={"scope": "item"}).status_code == 400


# ---------------------------------------------------------------------------
# 5. 令牌主体只看 everyone 库
# ---------------------------------------------------------------------------


async def test_token_principals_only_see_everyone_libraries(client: TestClient) -> None:
    shared = _create_library(client, "电影", "/m/movies")
    _create_library(client, "私藏", "/m/private", access_mode="selected", admin_visible=True)
    async with get_database().session() as session:
        pat = Principal(kind="pat", name="cli", is_admin=True, client_type="cli")
        assert await visible_library_ids(session, pat) == {shared}
        admin = Principal(kind="admin", name="admin", member_id=0, is_admin=True)
        assert len(await visible_library_ids(session, admin)) == 2
