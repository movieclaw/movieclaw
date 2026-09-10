"""影片分享（docs/design/media-share.md）的端到端测试。

覆盖：
1. 创建 / 查询 / 取消；已有有效分享时再建原样返回（SHARE_EXISTS）；取消后
   再建得到新 slug；
2. 访客探针三态：有效 / 过期 / 取消（不存在同取消）；
3. 密码：错密码 401、连错触发 429、正确后种下 Path 收窄的解锁 Cookie；
   取消分享后 Cookie 与所有端点一并失效；
4. 作用域：分享主体的可见库集只有分享的那个库；分享 Cookie 打不开任何既有
   业务接口；播放器条目信息只认分享的条目；
5. 访客视图：没有落盘路径与库归属，站内图片地址全部改走分享通道，TMDB 绝对
   地址原样；
6. 取流 token：分享访客的 token 带分享 id、有效期不超过到期剩余；取消后同一
   token 立即失效；
7. 条目删除级联删分享；外部访问地址有 / 无两种链接；活动页哨兵名。
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import share as share_service
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.services.playback.signing import (
    STREAM_TOKEN_TTL_S,
    issue_stream_token,
    verify_stream_token,
)
from movieclaw_api.services.playback_activity import _member_names
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, MediaItem
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.media_share import MediaShare

_AUTH = "/api/v1/auth"
_LIBS = "/api/v1/libraries"
_SHARE = "/api/v1/share"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}


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


def _anonymous(client: TestClient) -> TestClient:
    """访客视角：清掉超管会话 Cookie。"""
    client.cookies.clear()
    return client


def _create_library(client: TestClient, name: str, root: str, **extra) -> int:
    resp = client.post(_LIBS, json={"name": name, "kind": "movie", "root_paths": [root], **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def _seed_item(library_id: int, title: str, tmdb_id: int, *, kind: str = "movie") -> int:
    async with get_database().session() as session:
        item = MediaItem(
            kind=kind,
            tmdb_id=tmdb_id,
            title=title,
            original_title=title,
            year=2020,
            aliases=[],
            poster_path="/tmdb-poster.jpg",
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


def _write_poster(media_item_id: int) -> None:
    folder = Path(get_settings().metadata_dir) / "images" / str(media_item_id)
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 30), "#4a6fa5").save(folder / "poster.jpg", "JPEG")


def _create_share(client: TestClient, lib: int, item: int, **payload) -> dict:
    resp = client.post(f"{_LIBS}/{lib}/items/{item}/share", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _set_expired(slug: str) -> None:
    async with get_database().session() as session:
        row = await share_service.get_by_slug(session, slug)
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        await session.commit()


# ---------------------------------------------------------------------------
# 1. 生命周期
# ---------------------------------------------------------------------------


async def test_share_lifecycle_one_active_per_item(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)

    assert client.get(f"{_LIBS}/{lib}/items/{item}/share").json()["data"] is None

    first = _create_share(client, lib, item, expires_in_days=3, password=" k7pw2m ")
    assert first["code"] == "OK"
    view = first["data"]
    assert len(view["slug"]) == 16
    assert view["url"] == f"/s/{view['slug']}"  # 未配置外部访问地址：相对路径
    assert view["password"] == "k7pw2m"
    assert view["title"] == "沙丘 2"
    assert view["kind"] == "movie"
    assert view["view_count"] == 0
    expires = time.mktime(time.strptime(view["expires_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    assert abs(expires - (time.mktime(time.gmtime()) + 3 * 86400)) < 120

    # 再建：原样返回同一条，code 提示已存在
    again = _create_share(client, lib, item, expires_in_days=30)
    assert again["code"] == "SHARE_EXISTS"
    assert again["data"]["slug"] == view["slug"]
    assert client.get(f"{_LIBS}/{lib}/items/{item}/share").json()["data"]["slug"] == view["slug"]

    # 管理列表
    listed = client.get("/api/v1/shares").json()["data"]
    assert [s["slug"] for s in listed] == [view["slug"]]

    # 取消：幂等；列表清空；再建得到新 slug
    assert client.delete(f"{_LIBS}/{lib}/items/{item}/share").json()["data"]["revoked"] is True
    assert client.delete(f"{_LIBS}/{lib}/items/{item}/share").json()["data"]["revoked"] is False
    assert client.get("/api/v1/shares").json()["data"] == []
    fresh = _create_share(client, lib, item)["data"]
    assert fresh["slug"] != view["slug"]
    assert fresh["password"] is None
    # 按 id 取消（管理页）
    assert client.delete(f"/api/v1/shares/{fresh['id']}").status_code == 200
    assert client.get(f"{_LIBS}/{lib}/items/{item}/share").json()["data"] is None


async def test_share_validation(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    bad_days = client.post(f"{_LIBS}/{lib}/items/{item}/share", json={"expires_in_days": 2})
    assert bad_days.status_code == 400
    bad_pw = client.post(f"{_LIBS}/{lib}/items/{item}/share", json={"password": "abc"})
    assert bad_pw.status_code == 400
    # 条目不在这个库里 → 404
    other = _create_library(client, "剧集", "/m/tv")
    assert client.post(f"{_LIBS}/{other}/items/{item}/share", json={}).status_code == 404


async def test_share_url_uses_external_url(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    resp = client.put("/api/v1/app/config", json={"external_url": "https://nas.example.com/"})
    assert resp.status_code == 200, resp.text
    view = _create_share(client, lib, item)["data"]
    assert view["url"] == f"https://nas.example.com/s/{view['slug']}"


# ---------------------------------------------------------------------------
# 2. 访客探针
# ---------------------------------------------------------------------------


async def test_probe_states(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    slug = _create_share(client, lib, item, expires_in_days=1)["data"]["slug"]
    _anonymous(client)

    probe = client.get(f"{_SHARE}/{slug}")
    assert probe.status_code == 200
    assert probe.json()["data"] == {
        "requires_password": False,
        "unlocked": True,
        "expires_at": probe.json()["data"]["expires_at"],
        "media_item_id": item,
        "collection_id": None,
    }
    assert probe.json()["data"]["expires_at"].endswith("+00:00")

    missing = client.get(f"{_SHARE}/no-such-share")
    assert missing.status_code == 404
    assert missing.json()["code"] == "SHARE_NOT_FOUND"

    await _set_expired(slug)
    expired = client.get(f"{_SHARE}/{slug}")
    assert expired.status_code == 404
    assert expired.json()["code"] == "SHARE_EXPIRED"
    assert client.get(f"{_SHARE}/{slug}/item").status_code == 404


# ---------------------------------------------------------------------------
# 3. 密码与解锁 Cookie
# ---------------------------------------------------------------------------


async def test_password_unlock_and_throttle(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    slug = _create_share(client, lib, item, password="k7pw2m")["data"]["slug"]
    _anonymous(client)

    probe = client.get(f"{_SHARE}/{slug}").json()["data"]
    assert probe["requires_password"] is True
    assert probe["unlocked"] is False
    assert probe["media_item_id"] is None  # 密码之前不露条目
    locked = client.get(f"{_SHARE}/{slug}/item")
    assert locked.status_code == 401
    assert locked.json()["code"] == "SHARE_LOCKED"

    for _ in range(5):
        wrong = client.post(f"{_SHARE}/{slug}/unlock", json={"password": "nope00"})
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "SHARE_PASSWORD_INVALID"
    throttled = client.post(f"{_SHARE}/{slug}/unlock", json={"password": "k7pw2m"})
    assert throttled.status_code == 429
    assert "密码错误" in throttled.json()["message"]

    reset_auth_state()  # 清限流桶，模拟等待期满
    ok = client.post(f"{_SHARE}/{slug}/unlock", json={"password": "k7pw2m"})
    assert ok.status_code == 200, ok.text
    set_cookie = ok.headers["set-cookie"]
    assert "movieclaw_share=" in set_cookie
    assert f"Path=/api/v1/share/{slug}" in set_cookie
    assert "HttpOnly" in set_cookie
    cookie = ok.cookies.get("movieclaw_share")
    assert cookie

    client.cookies.set("movieclaw_share", cookie)
    unlocked_probe = client.get(f"{_SHARE}/{slug}").json()["data"]
    assert unlocked_probe["unlocked"] is True
    assert unlocked_probe["media_item_id"] == item
    shown = client.get(f"{_SHARE}/{slug}/item")
    assert shown.status_code == 200, shown.text
    assert shown.json()["data"]["title"] == "沙丘 2"

    # 解锁 Cookie 打不开任何既有业务接口
    assert client.get(_LIBS).status_code == 401
    assert client.get(f"{_LIBS}/{lib}/items/{item}").status_code == 401
    assert client.get("/api/v1/playback/resume?media_item_id=1").status_code == 401

    # 超管取消分享 → 同一 Cookie 对所有端点失效
    client.cookies.clear()
    client.post(f"{_AUTH}/login", json=_ADMIN)
    assert client.delete(f"{_LIBS}/{lib}/items/{item}/share").status_code == 200
    _anonymous(client)
    client.cookies.set("movieclaw_share", cookie)
    assert client.get(f"{_SHARE}/{slug}").status_code == 404
    assert client.get(f"{_SHARE}/{slug}/item").status_code == 404


# ---------------------------------------------------------------------------
# 4. 作用域
# ---------------------------------------------------------------------------


async def test_share_principal_scope(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    other_lib = _create_library(client, "私藏", "/m/private", access_mode="selected")
    item = await _seed_item(lib, "沙丘 2", 693134)
    other_item = await _seed_item(other_lib, "别的片", 1)
    slug = _create_share(client, lib, item)["data"]["slug"]

    async with get_database().session() as session:
        row = await share_service.get_by_slug(session, slug)
        assert row is not None
        principal = share_service.share_principal(row)
        assert principal.kind == "share"
        assert principal.is_admin is False
        assert principal.member_id == share_service.SHARE_VISITOR_MEMBER_ID
        assert await visible_library_ids(session, principal) == {lib}
        names = await _member_names(session, {0, -1})
        assert names[-1] == "分享访客"

    _anonymous(client)
    assert client.get(f"{_SHARE}/{slug}/playback/items/{item}").status_code == 200
    assert client.get(f"{_SHARE}/{slug}/playback/items/{other_item}").status_code == 404
    assert (
        client.get(
            f"{_SHARE}/{slug}/playback/items/{other_item}/episodes?season_number=1"
        ).status_code
        == 404
    )
    # 决策请求指向别的条目 / 别的文件 → 404
    capability = {"video": [], "audio": [], "containers": []}
    wrong_item = client.post(
        f"{_SHARE}/{slug}/playback/decide",
        json={"media_item_id": other_item, "capability": capability},
    )
    assert wrong_item.status_code == 404
    async with get_database().session() as session:
        other_file = (
            await session.execute(
                LibraryFile.__table__.select().where(LibraryFile.media_item_id == other_item)
            )
        ).first()
    assert other_file is not None
    wrong_file = client.post(
        f"{_SHARE}/{slug}/playback/decide",
        json={"file_id": other_file.id, "capability": capability},
    )
    assert wrong_file.status_code == 404
    # 分享条目外的图片资产 404
    _write_poster(other_item)
    assert client.get(f"{_SHARE}/{slug}/images/assets/{other_item}/poster.jpg").status_code == 404


# ---------------------------------------------------------------------------
# 5. 访客视图投影
# ---------------------------------------------------------------------------


async def test_shared_item_view_projection(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    _write_poster(item)
    slug = _create_share(client, lib, item)["data"]["slug"]
    _anonymous(client)

    resp = client.get(f"{_SHARE}/{slug}/item")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) == {
        "media_item_id",
        "kind",
        "tmdb_id",
        "imdb_id",
        "douban_id",
        "title",
        "original_title",
        "year",
        "poster_url",
        "backdrop_url",
        "primary_aspect",
        "local_meta",
        "files",
        "seasons",
        "expires_at",
    }
    assert data["title"] == "沙丘 2"
    assert len(data["files"]) == 1
    assert "file_path" not in data["files"][0]
    assert "file_name" not in data["files"][0]
    # 本例没有本地刮削资产行：海报是 TMDB 绝对地址，改走分享通道的图片代理
    assert data["poster_url"].startswith(f"/share/{slug}/images/proxy?url=https%3A%2F%2F")
    assert data["backdrop_url"] is None
    assert "/api/v1/libraries/" not in resp.text and "/images/assets/" not in resp.text
    assert "/images/proxy" not in resp.text.replace(f"/share/{slug}/images/proxy", "")

    # 站内地址的改写规则：三类相对地址改走分享通道，认不出的置空
    from movieclaw_api.api.routes.shares import _rewrite_url

    rw = lambda u: _rewrite_url(u, slug, lib, item)  # noqa: E731
    assert rw(f"/images/assets/{item}/poster.jpg?v=1") == (
        f"/share/{slug}/images/assets/{item}/poster.jpg?v=1"
    )
    assert rw(f"/libraries/{lib}/items/{item}/artwork?kind=fanart&v=2") == (
        f"/share/{slug}/artwork?kind=fanart&v=2"
    )
    assert rw("/libraries/files/7/thumb") == f"/share/{slug}/files/7/thumb"
    assert rw("https://image.tmdb.org/t/p/w500/x.jpg") == (
        f"/share/{slug}/images/proxy?url=https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw500%2Fx.jpg"
    )
    assert rw(f"/libraries/{lib}/items/{item}/something-else") is None
    # 图片通道：分享条目自己的资产 200
    assert client.get(f"{_SHARE}/{slug}/images/assets/{item}/poster.jpg").status_code == 200

    # 打开一次影片页 → 计数 +1
    client.post(f"{_AUTH}/login", json=_ADMIN)
    view = client.get(f"{_LIBS}/{lib}/items/{item}/share").json()["data"]
    assert view["view_count"] == 1
    assert view["last_accessed_at"] is not None


# ---------------------------------------------------------------------------
# 6. 取流 token
# ---------------------------------------------------------------------------


async def test_stream_token_bound_to_share(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    slug = _create_share(client, lib, item, expires_in_days=1)["data"]["slug"]

    from movieclaw_api.api.routes.playback import _share_stream_kwargs

    async with get_database().session() as session:
        row = await share_service.get_by_slug(session, slug)
        assert row is not None
        principal = share_service.share_principal(row)
        share_id = row.id
    kwargs = _share_stream_kwargs(principal)
    assert kwargs["share_id"] == share_id
    assert kwargs["ttl_seconds"] == STREAM_TOKEN_TTL_S  # 剩余一天 > 12 小时：取 12 小时
    # 快到期的分享：token 有效期收窄到剩余时间
    soon = principal.share.__class__(
        **{**principal.share.__dict__, "expires_at": utcnow() + timedelta(seconds=90)}
    )
    soon_principal = principal.__class__(**{**principal.__dict__, "share": soon})
    assert 0 < _share_stream_kwargs(soon_principal)["ttl_seconds"] <= 90

    token = await issue_stream_token(member_id=-1, file_id=1, **kwargs)
    grant = await verify_stream_token(token, file_id=1)
    assert grant is not None and grant.share_id == share_id and grant.member_id == -1

    async with get_database().session() as session:
        row = await share_service.get_by_slug(session, slug)
        assert row is not None
        await share_service.revoke(session, row)
    assert await verify_stream_token(token, file_id=1) is None
    _anonymous(client)
    assert client.get(f"/api/v1/playback/files/1/stream?token={token}").status_code == 404


# ---------------------------------------------------------------------------
# 7. 访客心跳：只进活动页的实时会话，不落观看状态
# ---------------------------------------------------------------------------


async def test_visitor_progress_is_live_only(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    slug = _create_share(client, lib, item)["data"]["slug"]
    _anonymous(client)
    body = {
        "media_item_id": item,
        "season_number": 0,
        "episode_number": 0,
        "event": "start",
        "device_id": "browser-a",
    }
    assert client.post(f"{_SHARE}/{slug}/playback/progress", json=body).status_code == 200
    beat = client.post(
        f"{_SHARE}/{slug}/playback/progress",
        json={**body, "event": "progress", "position_ms": 30_000},
    )
    assert beat.status_code == 200
    assert beat.json()["data"]["ended_by_admin"] is False
    # 别的条目 404
    assert (
        client.post(
            f"{_SHARE}/{slug}/playback/progress", json={**body, "media_item_id": 999}
        ).status_code
        == 404
    )

    # 超管：活动页看到「分享访客」；成员表里没有访客的任何状态
    client.post(f"{_AUTH}/login", json=_ADMIN)
    overview = client.get("/api/v1/playback/activity").text
    assert "分享访客" in overview
    async with get_database().session() as session:
        from movieclaw_db.models import PlaybackState

        rows = (await session.execute(PlaybackState.__table__.select())).all()
        assert rows == []
    resume = client.get(
        f"/api/v1/playback/resume?media_item_id={item}&season_number=0&episode_number=0"
    ).json()["data"]
    assert resume["position_ms"] == 0

    # 超管结束这次播放 → 访客下一次心跳收到 ended_by_admin，会话从活动页消失
    device = "web--1-browser-a"
    ended = client.post(f"/api/v1/playback/activity/sessions/{device}/end")
    assert ended.status_code == 200, ended.text
    _anonymous(client)
    beat = client.post(
        f"{_SHARE}/{slug}/playback/progress",
        json={**body, "event": "progress", "position_ms": 40_000},
    )
    assert beat.json()["data"]["ended_by_admin"] is True
    assert (
        client.post(
            f"{_SHARE}/{slug}/playback/progress", json={**body, "event": "stop"}
        ).status_code
        == 200
    )
    client.post(f"{_AUTH}/login", json=_ADMIN)
    assert "分享访客" not in client.get("/api/v1/playback/activity").text


# ---------------------------------------------------------------------------
# 8. 级联
# ---------------------------------------------------------------------------


async def test_item_delete_cascades_share(client: TestClient) -> None:
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "沙丘 2", 693134)
    slug = _create_share(client, lib, item)["data"]["slug"]
    async with get_database().session() as session:
        row = await session.get(MediaItem, item)
        assert row is not None
        await session.delete(row)
        await session.commit()
    async with get_database().session() as session:
        assert (await share_service.get_by_slug(session, slug)) is None
        assert (await session.get(MediaShare, 1)) is None
    _anonymous(client)
    assert client.get(f"{_SHARE}/{slug}").status_code == 404


# ---------------------------------------------------------------------------
# 合集分享（F4.4）
# ---------------------------------------------------------------------------


async def test_collection_share_covers_its_members(client: TestClient) -> None:
    """一条链接 = 一个合集此刻的成员。

    成员是**每次访问现算**的：规则驱动的合集会自己长，分享出去之后新入库的
    片也会出现在对方那边；被移出去的片立刻打不开，不需要再来取消一次。
    """
    lib = _create_library(client, "电影", "/m/movies")
    a = await _seed_item(lib, "第一部", 111)
    b = await _seed_item(lib, "第二部", 222)
    outsider = await _seed_item(lib, "圈外的", 333)

    created = client.post(
        "/api/v1/collections", json={"name": "给朋友的", "library_id": lib, "item_ids": [a, b]}
    )
    assert created.status_code == 200, created.text
    collection_id = created.json()["data"]["id"]

    share = client.post(f"/api/v1/collections/{collection_id}/share", json={})
    assert share.status_code == 200, share.text
    body = share.json()["data"]
    assert body["collection_id"] == collection_id
    assert body["media_item_id"] is None
    assert body["item_count"] == 2
    slug = body["slug"]

    _anonymous(client)
    listing = client.get(f"{_SHARE}/{slug}/collection")
    assert listing.status_code == 200, listing.text
    assert [row["title"] for row in listing.json()["data"]["items"]] == ["第一部", "第二部"]

    # 名单里的片打得开
    assert client.get(f"{_SHARE}/{slug}/item?item={a}").status_code == 200
    assert client.get(f"{_SHARE}/{slug}/item?item={b}").status_code == 200
    # 圈外的打不开——GUID/id 都是能猜的，只挡列表等于没挡
    assert client.get(f"{_SHARE}/{slug}/item?item={outsider}").status_code == 404
    # 不指定看哪一部也不行（合集分享没有"默认那一部"）
    assert client.get(f"{_SHARE}/{slug}/item").status_code == 404


async def test_removing_a_member_closes_the_door(client: TestClient) -> None:
    """从合集里移出去 = 立刻打不开，不需要任何撤销动作。"""
    lib = _create_library(client, "电影", "/m/movies")
    item = await _seed_item(lib, "会被移走的", 444)
    collection_id = client.post(
        "/api/v1/collections", json={"name": "临时", "library_id": lib, "item_ids": [item]}
    ).json()["data"]["id"]
    slug = client.post(f"/api/v1/collections/{collection_id}/share", json={}).json()["data"]["slug"]

    _anonymous(client)
    assert client.get(f"{_SHARE}/{slug}/item?item={item}").status_code == 200

    client.post(f"{_AUTH}/login", json=_ADMIN)
    assert client.delete(f"/api/v1/collections/{collection_id}/items/{item}").status_code == 200

    _anonymous(client)
    assert client.get(f"{_SHARE}/{slug}/item?item={item}").status_code == 404


async def test_item_share_ignores_the_item_parameter(client: TestClient) -> None:
    """条目分享的范围就那一个：``item`` 参数不能变成一把越权的钥匙。"""
    lib = _create_library(client, "电影", "/m/movies")
    a = await _seed_item(lib, "分享的", 555)
    slug = _create_share(client, lib, a)["data"]["slug"]
    _anonymous(client)
    # 换个 id 传进来仍然只给分享的那一部（服务端根本不看这个参数）
    resp = client.get(f"{_SHARE}/{slug}/item?item=999999")
    assert resp.status_code == 200
    assert resp.json()["data"]["media_item_id"] == a
