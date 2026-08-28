"""手动提交下载接口（POST /downloaders/submit）的端到端测试。

覆盖：成功提交（保存目录/标签/种子字节正确传递）、幂等重复提交、
无默认可用下载器、站点不可用、站点取种失败、下载器提交失败、参数校验。
站点访问与下载器适配器均为假实现，不发真实网络请求。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import movieclaw_api.api.routes.downloaders as downloaders_route
import movieclaw_api.services.downloader_config as downloader_config_service
import movieclaw_api.services.torrent_submit as torrent_submit_service
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.site_access import SiteUnavailableError
from movieclaw_db.engine import get_database
from movieclaw_db.models import Library, ManualDownloadIntent, MediaItem
from movieclaw_downloader import DownloaderInfo
from movieclaw_downloader.models import DownloadRequest, SubmitResult

# 每次提交收到的 DownloadRequest，供断言"种子字节/目录/标签传对了"
_captured_requests: list[DownloadRequest] = []


class _FakeSite:
    """假站点客户端：把 download_url 原样编码成"种子字节"，便于断言透传。"""

    async def download_torrent(self, url: str) -> bytes:
        if "sitefail" in url:
            raise RuntimeError("站点返回 500")
        return url.encode()


class _FakeSiteAccess:
    """假站点访问管理器：按 site_id 决定可用与否。"""

    async def get(self, site_id: str):
        if site_id == "nosite":
            raise SiteUnavailableError(f"站点未配置：{site_id}")
        return _FakeSite()


class _FakeDownloader:
    """假下载器适配器：按种子字节内容决定提交结果。"""

    def __init__(self, config) -> None:
        self.config = config

    async def test_connection(self) -> DownloaderInfo:
        return DownloaderInfo(type=self.config.type, version="v5.0.2")

    async def submit(self, request: DownloadRequest) -> SubmitResult:
        _captured_requests.append(request)
        assert request.torrent_bytes is not None
        if b"clientfail" in request.torrent_bytes:
            raise RuntimeError("下载器拒绝了请求")
        return SubmitResult(
            info_hash="a" * 40,
            name="Some.Movie.2024.2160p",
            already_exists=b"dup" in request.torrent_bytes,
        )

    async def close(self) -> None:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()

    _captured_requests.clear()
    # 配置侧（连接测试）与提交侧共用同一个假适配器
    monkeypatch.setattr(downloader_config_service, "create_downloader", _FakeDownloader)
    monkeypatch.setattr(torrent_submit_service, "create_downloader", _FakeDownloader)
    monkeypatch.setattr(torrent_submit_service, "get_site_access", lambda: _FakeSiteAccess())

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _add_default_downloader(c: TestClient, **overrides) -> int:
    """建一台下载器（第一台自动成为默认），返回 id。"""
    payload = {
        "name": overrides.pop("name", "家里的 qBittorrent"),
        "client_type": "qbittorrent",
        "url": "http://192.168.1.10:8080",
        "save_path": "/downloads/movies",
        **overrides,
    }
    r = c.post("/api/v1/downloaders", json=payload)
    assert r.status_code == 200
    return r.json()["data"]["id"]


_SUBMIT = {"site_id": "mteam", "download_url": "https://example.org/download.php?id=1"}


def test_submit_success(client) -> None:
    _add_default_downloader(client)
    r = client.post("/api/v1/downloaders/submit", json=_SUBMIT)
    assert r.status_code == 200
    body = r.json()
    assert "已提交到" in body["message"]
    data = body["data"]
    assert data["info_hash"] == "a" * 40
    assert data["already_exists"] is False
    assert data["downloader_name"] == "家里的 qBittorrent"
    assert data["save_path"] == "/downloads/movies"

    # 种子字节 = 站点按 download_url 取回的内容；目录/分类/标签按约定传递
    req = _captured_requests[-1]
    assert req.torrent_bytes == _SUBMIT["download_url"].encode()
    assert req.save_path == "/downloads/movies"
    assert req.category == "movieclaw"
    assert req.tags == ["movieclaw-manual"]


def test_submit_with_library_derives_save_path(client) -> None:
    """带入库目标：保存目录 = 库主根/标题 (年份)，覆盖下载器默认目录。"""
    _add_default_downloader(client)
    lib = client.post(
        "/api/v1/libraries",
        json={"name": "电影库2", "kind": "movie", "root_paths": ["/vol1/media/movies"]},
    ).json()["data"]
    r = client.post(
        "/api/v1/downloaders/submit",
        json={**_SUBMIT, "library_id": lib["id"], "title": "沙丘", "year": 2021},
    )
    assert r.status_code == 200
    body = r.json()
    assert "入库到「电影库2」" in body["message"]
    assert body["data"]["save_path"] == "/vol1/media/movies/沙丘 (2021)"
    assert _captured_requests[-1].save_path == "/vol1/media/movies/沙丘 (2021)"

    # 只选库不带标题：落库主根（身份未确认时不造目录名）
    r = client.post(
        "/api/v1/downloaders/submit",
        json={**_SUBMIT, "download_url": "https://example.org/dl?id=2", "library_id": lib["id"]},
    )
    assert r.json()["data"]["save_path"] == "/vol1/media/movies"


def test_translate_save_path() -> None:
    """路径映射翻译：最长前缀优先、分隔符边界安全、未命中原样放行。"""
    from movieclaw_api.services.torrent_submit import translate_save_path

    mappings = [
        {"local": "/data", "remote": "/mnt/data"},
        {"local": "/data/downloads", "remote": "/downloads"},
    ]
    # 最长前缀赢：/data/downloads 优先于 /data
    assert translate_save_path("/data/downloads/watch", mappings) == "/downloads/watch"
    # 恰好等于前缀本身
    assert translate_save_path("/data/downloads", mappings) == "/downloads"
    # 短前缀兜底
    assert translate_save_path("/data/movies", mappings) == "/mnt/data/movies"
    # 边界安全：/data/downloads2 不该被 /data/downloads 误配
    assert translate_save_path("/data/downloads2/x", mappings) == "/mnt/data/downloads2/x"
    # 未命中 / 无映射 / 无路径：原样放行
    assert translate_save_path("/other/place", mappings) == "/other/place"
    assert translate_save_path("/data/downloads", None) == "/data/downloads"
    assert translate_save_path(None, mappings) is None
    # 残缺映射行（单边为空）跳过不参与匹配
    assert translate_save_path("/data/x", [{"local": "/data", "remote": ""}]) == "/data/x"


def test_translate_to_local_and_mapping_covers() -> None:
    """反向翻译（下载器视角 → movieclaw 视角）与覆盖判定。"""
    from movieclaw_api.services.torrent_submit import mapping_covers, translate_to_local

    mappings = [{"local": "/data/downloads", "remote": "/downloads"}]
    assert translate_to_local("/downloads/watch", mappings) == "/data/downloads/watch"
    assert translate_to_local("/downloads", mappings) == "/data/downloads"
    # 未命中原样返回（视角一致部署）
    assert translate_to_local("/other", mappings) == "/other"
    assert mapping_covers("/data/downloads/movies", mappings) is True
    assert mapping_covers("/vol1/media", mappings) is False


def test_submit_with_path_mappings_translates_save_path(client) -> None:
    """配了路径映射：发给下载器的目录是下载器视角，接口回显同款。"""
    _add_default_downloader(
        client,
        save_path="/data/downloads/movies",
        path_mappings=[{"local": "/data/downloads", "remote": "/downloads"}],
    )
    r = client.post("/api/v1/downloaders/submit", json=_SUBMIT)
    assert r.status_code == 200
    assert r.json()["data"]["save_path"] == "/downloads/movies"
    assert _captured_requests[-1].save_path == "/downloads/movies"

    # 守门：库推导路径不被任何映射覆盖 → 拒绝提交（下载器大概率无法访问，
    # 投出去会落进容器黑洞），错误信息给出可操作指引
    lib = client.post(
        "/api/v1/libraries",
        json={"name": "电影库3", "kind": "movie", "root_paths": ["/vol1/media/movies"]},
    ).json()["data"]
    r = client.post(
        "/api/v1/downloaders/submit",
        json={
            **_SUBMIT,
            "download_url": "https://example.org/dl?id=3",
            "library_id": lib["id"],
            "title": "沙丘",
            "year": 2021,
        },
    )
    assert r.status_code == 400
    assert "路径映射覆盖范围" in r.json()["message"]
    # 补一条覆盖库根的等价映射（下载器可直达同名路径的声明）后放行
    downloaders = client.get("/api/v1/downloaders").json()["data"]
    client.put(
        f"/api/v1/downloaders/{downloaders[0]['id']}",
        json={
            "name": downloaders[0]["name"],
            "client_type": downloaders[0]["client_type"],
            "url": downloaders[0]["url"],
            "save_path": downloaders[0]["save_path"],
            "path_mappings": [
                {"local": "/data/downloads", "remote": "/downloads"},
                {"local": "/vol1/media", "remote": "/vol1/media"},
            ],
        },
    )
    r = client.post(
        "/api/v1/downloaders/submit",
        json={
            **_SUBMIT,
            "download_url": "https://example.org/dl?id=3",
            "library_id": lib["id"],
            "title": "沙丘",
            "year": 2021,
        },
    )
    assert r.status_code == 200
    assert _captured_requests[-1].save_path == "/vol1/media/movies/沙丘 (2021)"


def test_downloader_payload_rejects_relative_mapping(client) -> None:
    """路径映射两端必须是绝对路径，相对路径 422。"""
    r = client.post(
        "/api/v1/downloaders",
        json={
            "name": "坏映射",
            "client_type": "qbittorrent",
            "url": "http://192.168.1.10:8080",
            "path_mappings": [{"local": "data/downloads", "remote": "/downloads"}],
        },
    )
    assert r.status_code == 422


def test_submit_with_manual_save_path_override(client) -> None:
    """下载弹窗手选目录：优先于库推导，照常过映射翻译；相对路径 422。"""
    _add_default_downloader(
        client,
        path_mappings=[{"local": "/data/downloads", "remote": "/downloads"}],
    )
    r = client.post(
        "/api/v1/downloaders/submit",
        json={**_SUBMIT, "save_path": "/data/downloads/manual/"},
    )
    assert r.status_code == 200
    # 尾斜杠归一 + 映射翻译成下载器视角
    assert _captured_requests[-1].save_path == "/downloads/manual"
    assert r.json()["data"]["save_path"] == "/downloads/manual"

    r = client.post(
        "/api/v1/downloaders/submit",
        json={**_SUBMIT, "download_url": "https://example.org/dl?id=9", "save_path": "relative/x"},
    )
    assert r.status_code == 422


def test_dispatch_preview_routes_and_warnings(client) -> None:
    """投递路由预检：与真实投递同源的三级兜底 + 映射守门判定。"""
    # 没有任何下载器：不 ok，指引先配下载器
    lib = client.post(
        "/api/v1/libraries",
        json={"name": "预检电影库", "kind": "movie", "root_paths": ["/vol1/media/movies"]},
    ).json()["data"]
    r = client.get("/api/v1/subscriptions/download-routing-preview", params={"kind": "movie"})
    assert r.status_code == 200
    assert r.json()["data"]["ok"] is False
    assert "默认下载器" in r.json()["data"]["warning"]

    # 有下载器 + 库无监听规则：inplace 模式，基底 = 库主根
    _add_default_downloader(client)
    r = client.get(
        "/api/v1/subscriptions/download-routing-preview",
        params={"kind": "movie", "library_id": lib["id"]},
    )
    data = r.json()["data"]
    assert data["ok"] is True
    assert data["mode"] == "inplace"
    assert data["path"] == "/vol1/media/movies"
    assert data["library_name"] == "预检电影库"

    # 下载器配了映射但不覆盖库根：不 ok，警示映射缺口
    downloaders = client.get("/api/v1/downloaders").json()["data"]
    client.put(
        f"/api/v1/downloaders/{downloaders[0]['id']}",
        json={
            "name": downloaders[0]["name"],
            "client_type": downloaders[0]["client_type"],
            "url": downloaders[0]["url"],
            "path_mappings": [{"local": "/data/downloads", "remote": "/downloads"}],
        },
    )
    r = client.get(
        "/api/v1/subscriptions/download-routing-preview",
        params={"kind": "movie", "library_id": lib["id"]},
    )
    data = r.json()["data"]
    assert data["ok"] is False
    assert "路径映射覆盖范围" in data["warning"]

    # 配上监听导入规则：watch 模式，基底 = 规则源目录（映射覆盖到位则 ok）
    client.put(
        f"/api/v1/downloaders/{downloaders[0]['id']}",
        json={
            "name": downloaders[0]["name"],
            "client_type": downloaders[0]["client_type"],
            "url": downloaders[0]["url"],
            "path_mappings": [{"local": "/data/downloads", "remote": "/downloads"}],
        },
    )
    r = client.post(
        "/api/v1/import-watch",
        json={
            "source_path": "/data/downloads/watch",
            "strategy": "copy",
            "library_id": lib["id"],
        },
    )
    assert r.status_code == 200, r.json()
    r = client.get(
        "/api/v1/subscriptions/download-routing-preview",
        params={"kind": "movie", "library_id": lib["id"]},
    )
    data = r.json()["data"]
    assert data["ok"] is True
    assert data["mode"] == "watch"
    assert data["path"] == "/data/downloads/watch"


def test_downloader_payload_rejects_duplicate_mapping(client) -> None:
    """映射两端各自不允许重复（尾斜杠归一后比较），重复 422。"""
    base = {"name": "重复映射", "client_type": "qbittorrent", "url": "http://192.168.1.10:8080"}
    # movieclaw 路径重复（/data/ 与 /data 视为同一个）
    r = client.post(
        "/api/v1/downloaders",
        json={
            **base,
            "path_mappings": [
                {"local": "/data/", "remote": "/downloads"},
                {"local": "/data", "remote": "/mnt/other"},
            ],
        },
    )
    assert r.status_code == 422
    # 下载器路径重复
    r = client.post(
        "/api/v1/downloaders",
        json={
            **base,
            "path_mappings": [
                {"local": "/data/a", "remote": "/downloads"},
                {"local": "/data/b", "remote": "/downloads"},
            ],
        },
    )
    assert r.status_code == 422


def test_submit_duplicate_is_idempotent(client) -> None:
    _add_default_downloader(client)
    r = client.post(
        "/api/v1/downloaders/submit",
        json={**_SUBMIT, "download_url": "https://example.org/dup.torrent"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["already_exists"] is True
    assert "已在下载器中" in r.json()["message"]


def test_submit_without_any_downloader(client) -> None:
    r = client.post("/api/v1/downloaders/submit", json=_SUBMIT)
    assert r.status_code == 400
    assert "默认下载器" in r.json()["message"]


def test_resolve_target_returns_existing_route_preview(client, monkeypatch) -> None:
    """手动下载预检：唯一身份后复用订阅的收藏范围/监听目录预检结论。"""
    import movieclaw_api.services.subscription as subscription_service
    from movieclaw_api.services.torrent_submit import ManualTargetResolution

    async def resolve_target(**kwargs):
        assert kwargs["kind"] == "movie" and kwargs["title"] == "测试电影"
        return ManualTargetResolution(tmdb_id=42, candidates=[])

    async def preview(session, *, kind, library_id, tmdb_id, downloader_id=None, **identity):
        assert (kind, library_id, tmdb_id, downloader_id) == ("movie", None, 42, 8)
        # 没有候选时用种子识别出的标题/年份渲染条目目录预览
        assert identity == {"title": "测试电影", "year": 2024}
        return {
            "mode": "watch",
            "path": "/downloads/manual/movies",
            "entry_dir": "/data/movies/测试电影 (2024)",
            "staging_path": None,
            "library_id": 7,
            "library_name": "国内电影",
            "downloader_name": "家里的 qBittorrent",
            "route_matched": True,
            "route_reason": "命中「国内电影」：区域=中国",
            "ok": True,
            "warning": None,
        }

    monkeypatch.setattr(downloaders_route, "resolve_manual_target", resolve_target)
    monkeypatch.setattr(subscription_service, "preview_dispatch_route", preview)
    r = client.post(
        "/api/v1/downloaders/resolve-target",
        json={"kind": "movie", "title": "测试电影", "year": 2024, "downloader_id": 8},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "ready"
    assert data["tmdb_id"] == 42
    assert data["library_name"] == "国内电影"
    assert data["path"] == "/downloads/manual/movies"
    assert data["entry_dir"] == "/data/movies/测试电影 (2024)"


def test_resolve_target_accepts_a_confirmed_candidate(client, monkeypatch) -> None:
    """歧义候选由用户确认后，可继续复用同一套自动路由预检。"""
    import movieclaw_api.services.subscription as subscription_service
    from movieclaw_api.services.library.resolve import ResolveCandidate
    from movieclaw_api.services.torrent_submit import ManualTargetResolution

    async def resolve_target(**kwargs):
        return ManualTargetResolution(
            tmdb_id=None,
            candidates=[ResolveCandidate(tmdb_id=9527, title="确认影片", year=2024)],
        )

    async def preview(session, *, kind, library_id, tmdb_id, downloader_id=None, **identity):
        assert (kind, library_id, tmdb_id, downloader_id) == ("movie", None, 9527, None)
        # 用户确认了候选：条目目录预览用候选（TMDB）标题，而不是种子标题
        assert identity == {"title": "确认影片", "year": 2024}
        return {
            "mode": "watch",
            "path": "/downloads/manual/movies",
            "entry_dir": "/data/movies/确认影片 (2024)",
            "staging_path": None,
            "library_id": 7,
            "library_name": "国内电影",
            "downloader_name": "家里的 qBittorrent",
            "route_matched": True,
            "route_reason": "命中「国内电影」：区域=中国",
            "ok": True,
            "warning": None,
        }

    monkeypatch.setattr(downloaders_route, "resolve_manual_target", resolve_target)
    monkeypatch.setattr(subscription_service, "preview_dispatch_route", preview)
    r = client.post(
        "/api/v1/downloaders/resolve-target",
        json={
            "kind": "movie",
            "title": "确认影片",
            "year": 2024,
            "selected_tmdb_id": 9527,
        },
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["status"] == "ready"
    assert r.json()["data"]["tmdb_id"] == 9527


def test_auto_route_submits_to_watch_and_anchors_info_hash(client, monkeypatch) -> None:
    """真实提交时服务端重新路由，并用 infohash 保存监听导入可认领的身份。"""
    import movieclaw_api.services.library.routing as routing_service
    import movieclaw_api.services.media_discover as media_discover_service
    from movieclaw_api.services.library.routing import RouteDecision
    from movieclaw_api.services.media_library import MediaLibraryService

    _add_default_downloader(client)
    selected_downloader_id = _add_default_downloader(
        client,
        name="NAS 上的 qBittorrent",
        url="http://192.168.1.20:8080",
        path_mappings=[{"local": "/downloads/manual", "remote": "/mnt/manual"}],
    )
    library = client.post(
        "/api/v1/libraries",
        json={"name": "自动路由电影库", "kind": "movie", "root_paths": ["/media/movies"]},
    ).json()["data"]
    watch = client.post(
        "/api/v1/import-watch",
        json={
            "source_path": "/downloads/manual/movies",
            "strategy": "copy",
            "library_id": library["id"],
        },
    )
    assert watch.status_code == 200, watch.json()

    async def ensure(self, kind, tmdb_id, **kwargs):
        row = MediaItem(
            kind=kind.value,
            tmdb_id=tmdb_id,
            title="确认影片",
            original_title="Confirmed Movie",
            year=2024,
            aliases=[],
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def route_for_item(session, kind, item):
        target = await session.get(Library, library["id"])
        assert target is not None
        return RouteDecision(
            library=target,
            matched=True,
            reason="命中「自动路由电影库」：区域=中国",
        )

    monkeypatch.setattr(MediaLibraryService, "ensure_media_item", ensure)
    monkeypatch.setattr(routing_service, "route_for_item", route_for_item)
    monkeypatch.setattr(media_discover_service, "get_tmdb_client", object)
    r = client.post(
        "/api/v1/downloaders/submit",
        json={
            **_SUBMIT,
            "torrent_id": "8866",
            "auto_route": True,
            "media_kind": "movie",
            "tmdb_id": 9527,
            "title": "确认影片",
            "year": 2024,
            "downloader_id": selected_downloader_id,
        },
    )
    assert r.status_code == 200, r.json()
    assert _captured_requests[-1].save_path == "/mnt/manual/movies"
    assert r.json()["data"]["downloader_id"] == selected_downloader_id

    async def load_intent():
        async with get_database().session() as session:
            return (await session.execute(select(ManualDownloadIntent))).scalar_one()

    intent = asyncio.run(load_intent())
    assert intent.info_hash == "a" * 40
    assert intent.library_id == library["id"]
    assert intent.downloader_id == selected_downloader_id
    assert intent.download_name == "Some.Movie.2024.2160p"
    assert intent.save_path == "/downloads/manual/movies"
    # 站点种子 ID 随身份锚落库，任务中心据此反查详情页提供「打开种子页」
    assert intent.torrent_id == "8866"


def test_anchor_sweeps_stale_orphan_intents(client) -> None:
    """锚定新下载时顺带清理超窗孤儿锚；窗口内的在途锚不受影响。

    任务下载中途被用户从下载器删除时没有入库时刻消费锚，靠这次机会
    兜底回收，表不会无限累积。
    """
    from datetime import timedelta

    from movieclaw_api.services.torrent_submit import _INTENT_STALE_AFTER, anchor_manual_download
    from movieclaw_db.models import utcnow

    async def run() -> set[str]:
        async with get_database().session() as session:
            item = MediaItem(
                kind="movie", tmdb_id=1, title="片", original_title="Pian", year=2020, aliases=[]
            )
            lib = Library(name="库", kind="movie", root_paths=["/media/movies"])
            session.add_all([item, lib])
            await session.commit()
            await session.refresh(item)
            await session.refresh(lib)
            assert item.id is not None and lib.id is not None
            stale = ManualDownloadIntent(
                info_hash="b" * 40, media_item_id=item.id, library_id=lib.id
            )
            stale.created_at = utcnow() - _INTENT_STALE_AFTER - timedelta(days=1)
            fresh = ManualDownloadIntent(
                info_hash="c" * 40, media_item_id=item.id, library_id=lib.id
            )
            session.add_all([stale, fresh])
            await session.commit()
            await anchor_manual_download(
                session,
                info_hash="d" * 40,
                media_item_id=item.id,
                library_id=lib.id,
                site_id="mteam",
            )
            rows = (await session.execute(select(ManualDownloadIntent))).scalars().all()
            return {row.info_hash for row in rows}

    assert asyncio.run(run()) == {"c" * 40, "d" * 40}


def test_anchor_backfills_new_task_identity_columns_on_legacy_row(client) -> None:
    """重复提交旧版锚时补真实任务名和落点，但保持首次媒体/库归属。"""
    from movieclaw_api.services.torrent_submit import anchor_manual_download

    async def run() -> ManualDownloadIntent:
        async with get_database().session() as session:
            item = MediaItem(
                kind="movie",
                tmdb_id=2,
                title="旧锚影片",
                original_title="Legacy Anchor",
                year=2020,
                aliases=[],
            )
            library = Library(name="旧锚库", kind="movie", root_paths=["/media/movies"])
            session.add_all([item, library])
            await session.commit()
            await session.refresh(item)
            await session.refresh(library)
            legacy = ManualDownloadIntent(
                info_hash="e" * 40,
                media_item_id=item.id,
                library_id=library.id,
                site_id="mteam",
            )
            session.add(legacy)
            await session.commit()
            await anchor_manual_download(
                session,
                info_hash="e" * 40,
                media_item_id=item.id,
                library_id=library.id,
                site_id="mteam",
                download_name="Legacy.Anchor.2020.2160p",
                save_path="/downloads/movies",
            )
            await session.refresh(legacy)
            return legacy

    intent = asyncio.run(run())
    assert intent.download_name == "Legacy.Anchor.2020.2160p"
    assert intent.save_path == "/downloads/movies"


def test_submit_with_disabled_default_downloader(client) -> None:
    did = _add_default_downloader(client)
    client.patch(f"/api/v1/downloaders/{did}/status", json={"enabled": False})
    r = client.post("/api/v1/downloaders/submit", json=_SUBMIT)
    assert r.status_code == 400
    assert "默认下载器" in r.json()["message"]


def test_submit_with_specified_downloader(client) -> None:
    """指定 downloader_id：投给指定台而非默认台。"""
    _add_default_downloader(client)
    second = _add_default_downloader(
        client, name="NAS 上的 qBittorrent", url="http://192.168.1.20:8080", save_path="/dl2"
    )
    r = client.post("/api/v1/downloaders/submit", json={**_SUBMIT, "downloader_id": second})
    assert r.status_code == 200
    assert r.json()["data"]["downloader_name"] == "NAS 上的 qBittorrent"
    assert _captured_requests[-1].save_path == "/dl2"


def test_submit_with_specified_downloader_unavailable(client) -> None:
    """指定的下载器停用/不存在：指向明确的中文错误，不静默回落默认台。"""
    _add_default_downloader(client)
    second = _add_default_downloader(client, name="备用机", url="http://192.168.1.30:8080")
    client.patch(f"/api/v1/downloaders/{second}/status", json={"enabled": False})
    r = client.post("/api/v1/downloaders/submit", json={**_SUBMIT, "downloader_id": second})
    assert r.status_code == 400
    assert "备用机" in r.json()["message"]

    r = client.post("/api/v1/downloaders/submit", json={**_SUBMIT, "downloader_id": 9999})
    assert r.status_code == 400
    assert "不存在" in r.json()["message"]


def test_submit_site_unavailable(client) -> None:
    _add_default_downloader(client)
    r = client.post("/api/v1/downloaders/submit", json={**_SUBMIT, "site_id": "nosite"})
    assert r.status_code == 400
    assert "站点未配置" in r.json()["message"]


def test_submit_site_download_failed(client) -> None:
    _add_default_downloader(client)
    r = client.post(
        "/api/v1/downloaders/submit",
        json={**_SUBMIT, "download_url": "https://example.org/sitefail.torrent"},
    )
    assert r.status_code == 502
    assert "取回种子失败" in r.json()["message"]


def test_submit_downloader_rejected(client) -> None:
    _add_default_downloader(client)
    r = client.post(
        "/api/v1/downloaders/submit",
        json={**_SUBMIT, "download_url": "https://example.org/clientfail.torrent"},
    )
    assert r.status_code == 502
    assert "提交到下载器" in r.json()["message"]


def test_submit_validation(client) -> None:
    assert client.post("/api/v1/downloaders/submit", json={"site_id": "mteam"}).status_code == 422
    assert (
        client.post(
            "/api/v1/downloaders/submit", json={"site_id": "", "download_url": "x"}
        ).status_code
        == 422
    )
