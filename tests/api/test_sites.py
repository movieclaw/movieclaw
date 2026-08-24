"""站点配置管理接口的端到端测试。

覆盖：目录（可选项）、配置校验、保存后异步验证的状态流转、脱敏、启用停用、
更新重验、删除连带清理。验证流程被替换为"假验证"，不发真实网络请求，
使状态流转可确定性断言。
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import movieclaw_api.api.routes.sites as sites_routes
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import ConflictException
from movieclaw_api.services import SiteConfigService
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models.site_credential import AuthType, ConfigStatus
from movieclaw_db.repositories.credential_repo import CredentialRepository
from movieclaw_db.repositories.profile_repo import ProfileRepository
from movieclaw_tracker import load_all_sites

# 假验证成功时落库的资料快照（模拟真实 verify_site 拉到的 UserProfile）
_FAKE_PROFILE = {
    "user_id": "10086",
    "username": "tester",
    "user_class": "Power User",
    "uploaded_bytes": 1_649_267_441_664,  # 1.5 TB
    "downloaded_bytes": 858_993_459_200,  # 800 GB
    "ratio": 1.92,
    "bonus": 12345.6,
    "seeding_count": 35,
    "leeching_count": 2,
}


async def _fake_verify_site(site_id: str) -> None:
    """假验证：mteam 判为成功，其余判为失败，避免真实网络依赖。

    成功路径与真实 verify_site 保持一致：更新状态的同时落库资料快照。
    """
    async with get_database().session() as session:
        repo = CredentialRepository(session)
        if site_id == "mteam":
            await repo.update_status(site_id, ConfigStatus.ACTIVE)
            await ProfileRepository(session).upsert(site_id=site_id, **_FAKE_PROFILE)
        else:
            await repo.update_status(site_id, ConfigStatus.FAILED, last_error="模拟：凭据无效")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 每个测试用独立临时 SQLite 库，保证隔离
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    # Settings 带 lru_cache，改环境变量后需清缓存，确保读到临时库路径
    get_settings.cache_clear()

    # 用假验证替换真实网络验证
    monkeypatch.setattr(sites_routes, "verify_site", _fake_verify_site)

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    # 本文件只测站点配置业务，登录鉴权用依赖覆盖绕过（鉴权本身在 test_auth 覆盖）
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:  # with 块内触发 lifespan：建库、迁移、加载目录
        yield c
    get_settings.cache_clear()


def test_catalog_lists_supported_sites_with_requirements(client: TestClient) -> None:
    data = client.get("/api/v1/sites/catalog").json()["data"]
    by_id = {x["site_id"]: x for x in data}
    assert {"mteam", "ttg"} <= set(by_id)

    mteam_types = [a["auth_type"] for a in by_id["mteam"]["supported_auth_types"]]
    assert mteam_types == ["apikey"]

    ttg_reqs = {a["auth_type"]: a["required_fields"] for a in by_id["ttg"]["supported_auth_types"]}
    assert ttg_reqs == {"cookie": ["cookie"], "credential": ["username", "password"]}


def test_configure_rejects_unsupported_auth_type(client: TestClient) -> None:
    r = client.post(
        "/api/v1/sites", json={"site_id": "mteam", "auth_type": "cookie", "cookie": "x"}
    )
    assert r.status_code == 400
    assert r.json()["code"] == "BAD_REQUEST"


def test_configure_rejects_missing_required_fields(client: TestClient) -> None:
    r = client.post(
        "/api/v1/sites",
        json={"site_id": "ttg", "auth_type": "credential", "username": "u"},
    )
    assert r.status_code == 400


def test_configure_rejects_unknown_site(client: TestClient) -> None:
    r = client.post("/api/v1/sites", json={"site_id": "nope", "auth_type": "cookie", "cookie": "x"})
    assert r.status_code == 404


def test_configure_then_async_verify_active_and_desensitized(client: TestClient) -> None:
    r = client.post(
        "/api/v1/sites", json={"site_id": "mteam", "auth_type": "apikey", "api_key": "k"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    # 保存即同步占位为 verifying（关闭并发窗口）
    assert data["status"] == "verifying"
    # 脱敏：响应绝不含密钥
    assert not ({"api_key", "cookie", "password", "username"} & set(data))

    # 后台验证（在 with TestClient 内已执行完）后转 active、可用
    d = client.get("/api/v1/sites/mteam").json()["data"]
    assert d["status"] == "active"
    assert d["usable"] is True


def test_failed_verification_records_error(client: TestClient) -> None:
    client.post(
        "/api/v1/sites",
        json={"site_id": "ttg", "auth_type": "credential", "username": "u", "password": "p"},
    )
    d = client.get("/api/v1/sites/ttg").json()["data"]
    assert d["status"] == "failed"
    assert d["usable"] is False
    # 失败原因与本次检查时间都记录下来，供页面展示
    assert d["last_error"]
    assert d["last_checked_at"] is not None


def test_disable_makes_site_unusable_without_changing_status(client: TestClient) -> None:
    client.post("/api/v1/sites", json={"site_id": "mteam", "auth_type": "apikey", "api_key": "k"})
    d = client.patch("/api/v1/sites/mteam/status", json={"enabled": False}).json()["data"]
    assert d["enabled"] is False
    assert d["status"] == "active"  # 验证结论不变
    assert d["usable"] is False  # 但因停用而不可用


def test_boost_pause_resume_roundtrip(client: TestClient) -> None:
    """刷流暂停/恢复闸：未开刷流时 400；开启后可暂停、可恢复；
    暂停中关闭刷流会连带清掉暂停态（暂停依附于刷流本身）。"""
    client.post("/api/v1/sites", json={"site_id": "mteam", "auth_type": "apikey", "api_key": "k"})
    r = client.patch("/api/v1/sites/mteam/ratio-boost/pause", json={"paused": True})
    assert r.status_code == 400  # 未开启刷流无从暂停

    d = client.patch("/api/v1/sites/mteam/ratio-boost", json={"enabled": True}).json()["data"]
    assert d["boost_paused"] is False
    d = client.patch("/api/v1/sites/mteam/ratio-boost/pause", json={"paused": True}).json()["data"]
    assert d["boost_paused"] is True
    d = client.patch("/api/v1/sites/mteam/ratio-boost/pause", json={"paused": False}).json()["data"]
    assert d["boost_paused"] is False

    client.patch("/api/v1/sites/mteam/ratio-boost/pause", json={"paused": True})
    d = client.patch("/api/v1/sites/mteam/ratio-boost", json={"enabled": False}).json()["data"]
    assert d["boost_enabled"] is False
    assert d["boost_paused"] is False


def test_update_credentials_resets_and_reverifies(client: TestClient) -> None:
    client.post("/api/v1/sites", json={"site_id": "mteam", "auth_type": "apikey", "api_key": "k1"})
    assert client.get("/api/v1/sites/mteam").json()["data"]["status"] == "active"

    r = client.put(
        "/api/v1/sites/mteam",
        json={"auth_type": "apikey", "api_key": "k2", "enabled": True},
    )
    assert r.status_code == 200
    # 更新后重新验证，最终仍 active
    assert client.get("/api/v1/sites/mteam").json()["data"]["status"] == "active"


def test_delete_removes_site(client: TestClient) -> None:
    client.post(
        "/api/v1/sites",
        json={"site_id": "ttg", "auth_type": "credential", "username": "u", "password": "p"},
    )
    assert client.delete("/api/v1/sites/ttg").status_code == 200
    assert client.get("/api/v1/sites/ttg").status_code == 404


# ---------------------------------------------------------------------------
# 站点用户资料快照：验证成功后落库，嵌入 ConfiguredSite 视图展示
# ---------------------------------------------------------------------------


def test_profile_embedded_after_successful_verification(client: TestClient) -> None:
    """验证成功后，列表与详情接口都应带上资料快照。"""
    client.post("/api/v1/sites", json={"site_id": "mteam", "auth_type": "apikey", "api_key": "k"})

    d = client.get("/api/v1/sites/mteam").json()["data"]
    assert d["status"] == "active"
    p = d["profile"]
    assert p["username"] == "tester"
    assert p["user_class"] == "Power User"
    assert p["uploaded_bytes"] == _FAKE_PROFILE["uploaded_bytes"]
    assert p["downloaded_bytes"] == _FAKE_PROFILE["downloaded_bytes"]
    assert p["ratio"] == pytest.approx(1.92)
    assert p["seeding_count"] == 35
    # 快照时间带 UTC 时区标记（前端按 UTC 解析，避免时区错位）
    assert p["fetched_at"].endswith("+00:00")

    # 列表接口同样嵌入
    rows = client.get("/api/v1/sites").json()["data"]
    assert rows[0]["profile"]["username"] == "tester"


def test_profile_absent_when_never_verified(client: TestClient) -> None:
    """从未验证成功过的站点，profile 应为 null（而非报错或空对象）。"""
    client.post(
        "/api/v1/sites",
        json={"site_id": "ttg", "auth_type": "credential", "username": "u", "password": "p"},
    )
    d = client.get("/api/v1/sites/ttg").json()["data"]
    assert d["status"] == "failed"
    assert d["profile"] is None


def test_profile_survives_enable_toggle(client: TestClient) -> None:
    """启停等操作的响应也必须带 profile —— 前端用响应整体替换本地状态。"""
    client.post("/api/v1/sites", json={"site_id": "mteam", "auth_type": "apikey", "api_key": "k"})
    d = client.patch("/api/v1/sites/mteam/status", json={"enabled": False}).json()["data"]
    assert d["profile"]["username"] == "tester"


# ---------------------------------------------------------------------------
# 并发状态守卫：VERIFYING 期间的操作应被拒绝（Service 层直测，状态可控）
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """初始化独立临时库并加载目录，直接返回 Database，供 Service 层直测。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'svc.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.database_url, echo=False)
    await run_migrations()
    load_all_sites()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _make_verifying(db, site_id: str) -> None:
    """配置一个站点并把它占位为 VERIFYING，模拟"验证进行中"。"""
    async with db.session() as session:
        service = SiteConfigService(session)
        await service.configure(site_id=site_id, auth_type=AuthType.APIKEY, api_key="k")
        await service.start_verification(site_id)  # 同步占位 VERIFYING


async def test_delete_blocked_while_verifying(db) -> None:
    await _make_verifying(db, "mteam")
    async with db.session() as session:
        service = SiteConfigService(session)
        with pytest.raises(ConflictException):
            await service.delete("mteam")


async def test_reverify_blocked_while_verifying(db) -> None:
    await _make_verifying(db, "mteam")
    async with db.session() as session:
        service = SiteConfigService(session)
        with pytest.raises(ConflictException):
            await service.start_verification("mteam")  # 已在验证中


async def test_update_blocked_while_verifying(db) -> None:
    await _make_verifying(db, "mteam")
    async with db.session() as session:
        service = SiteConfigService(session)
        with pytest.raises(ConflictException):
            await service.configure(site_id="mteam", auth_type=AuthType.APIKEY, api_key="k2")


async def test_enable_disable_allowed_while_verifying(db) -> None:
    """启用/停用与验证正交，VERIFYING 期间仍允许。"""
    await _make_verifying(db, "mteam")
    async with db.session() as session:
        service = SiteConfigService(session)
        row = await service.set_enabled("mteam", False)
        assert row.enabled is False
        assert row.status == ConfigStatus.VERIFYING  # 验证状态不受影响


# ---------------------------------------------------------------------------
# 资料快照的存取语义（Repository / Service 层直测）
# ---------------------------------------------------------------------------


async def test_profile_upsert_overwrites_single_row(db) -> None:
    """快照是覆盖式更新：同一站点始终只有一行，重复写入取最新值。"""
    async with db.session() as session:
        repo = ProfileRepository(session)
        first = await repo.upsert(site_id="mteam", **_FAKE_PROFILE)
        second = await repo.upsert(
            site_id="mteam", **{**_FAKE_PROFILE, "uploaded_bytes": 999, "ratio": None}
        )
        assert second.id == first.id  # 同一行，而非新增
        assert second.uploaded_bytes == 999
        assert second.ratio is None  # None（未知）也如实覆盖，不残留旧值


async def test_delete_site_removes_profile_snapshot(db) -> None:
    """删除站点配置时，资料快照作为派生缓存一并清理。"""
    async with db.session() as session:
        service = SiteConfigService(session)
        await service.configure(site_id="mteam", auth_type=AuthType.APIKEY, api_key="k")
        await ProfileRepository(session).upsert(site_id="mteam", **_FAKE_PROFILE)
        await service.delete("mteam")
        assert await ProfileRepository(session).get_by_site("mteam") is None


# ---------------------------------------------------------------------------
# 种子缓存统计：GET /sites/sync-stats（站点配置页的"缓存感知"数据源）
# ---------------------------------------------------------------------------


def test_sync_stats_empty_before_any_sync(client: TestClient) -> None:
    """尚无任何缓存/游标时返回空字典；也验证路由不被 /{site_id} 吞掉。"""
    r = client.get("/api/v1/sites/sync-stats")
    assert r.status_code == 200
    assert r.json()["data"] == {}


def test_sync_stats_reports_counts_and_cursor(client: TestClient) -> None:
    """种子入库、游标回写后，端点应按站点返回计数与同步节奏。"""
    from movieclaw_db.models.site_torrent import TorrentSource
    from movieclaw_db.repositories.torrent_repo import (
        TorrentObservation,
        TorrentRepository,
    )

    async def seed() -> None:
        async with get_database().session() as session:
            repo = TorrentRepository(session)
            await repo.bulk_upsert(
                [
                    TorrentObservation(
                        site_id="mteam",
                        torrent_id=str(i),
                        title=f"种子{i}",
                        source=TorrentSource.LIST,
                    )
                    for i in range(3)
                ]
            )
            await repo.update_cursor_after_sync(
                "mteam", new_count=3, full_page=False, next_interval_seconds=600
            )

    # TestClient 的 portal 在 app 的事件循环里执行播种，与请求共用同一个库连接池
    client.portal.call(seed)

    data = client.get("/api/v1/sites/sync-stats").json()["data"]
    s = data["mteam"]
    assert s["torrent_count"] == 3
    assert s["sync_interval_seconds"] == 600
    assert s["last_new_count"] == 3
    assert s["last_error"] is None
    assert s["consecutive_failures"] == 0
    # 时间字段带 UTC 时区标记（前端按 UTC 解析，避免时区错位）
    assert s["last_sync_at"].endswith("+00:00")
    assert s["last_success_at"].endswith("+00:00")
    assert s["next_sync_at"].endswith("+00:00")


async def test_sync_stats_view_handles_count_without_cursor(db) -> None:
    """只有快照残留、游标缺失时（from_parts 的另一分支），视图应给出可用默认值。"""
    from movieclaw_api.schemas.site import SiteSyncStatsView

    view = SiteSyncStatsView.from_parts(5, None)
    assert view.torrent_count == 5
    assert view.last_sync_at is None
    assert view.sync_interval_seconds is None


async def test_bulk_upsert_batches_existing_row_lookup(db) -> None:
    """批量同步只做批次级查询，不能随种子数逐条 SELECT 而拖死事件循环。"""
    from sqlalchemy import event

    from movieclaw_db.models.site_torrent import TorrentSource
    from movieclaw_db.repositories.torrent_repo import (
        TorrentObservation,
        TorrentRepository,
    )

    def observation(torrent_id: str, *, seeders: int) -> TorrentObservation:
        return TorrentObservation(
            site_id="mteam",
            torrent_id=torrent_id,
            title=f"种子{torrent_id}",
            source=TorrentSource.LIST,
            seeders=seeders,
        )

    async with db.session() as session:
        await TorrentRepository(session).upsert(observation("known", seeders=1))

    selects: list[str] = []

    def record_select(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "site_torrent" in statement:
            selects.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record_select)
    try:
        async with db.session() as session:
            stats = await TorrentRepository(session).bulk_upsert(
                [
                    observation("known", seeders=2),
                    observation("new-1", seeders=3),
                    observation("new-2", seeders=4),
                    observation("new-2", seeders=999),  # 同批重复仍保留首条
                ]
            )
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record_select)

    assert stats.inserted == 2
    assert stats.updated == 1
    assert len(selects) == 1

    async with db.session() as session:
        repo = TorrentRepository(session)
        assert (await repo.get("mteam", "known")).seeders == 2
        assert (await repo.get("mteam", "new-2")).seeders == 4


async def test_torrent_sync_offloads_page_enrichment(monkeypatch) -> None:
    """整页 NER 扩充必须在线程池执行，不能阻塞 FastAPI 的健康检查。"""
    import threading

    import movieclaw_api.services.torrent_sync as torrent_sync

    caller_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fake_to_observation(site_id, item, *, trust_volatile):
        assert site_id == "mteam"
        assert trust_volatile is True
        worker_threads.append(threading.get_ident())
        return item

    monkeypatch.setattr(torrent_sync, "_to_observation", fake_to_observation)
    items = [object(), object()]

    assert await torrent_sync._to_observations("mteam", items, trust_volatile=True) == items
    assert worker_threads
    assert all(thread_id != caller_thread for thread_id in worker_threads)


# ---------------------------------------------------------------------------
# 验证失败原因的中文归类
# ---------------------------------------------------------------------------


def test_friendly_error_categorizes_common_failures() -> None:
    from movieclaw_api.services.verification import friendly_error
    from movieclaw_tracker.exceptions import (
        TrackerAuthError,
        TrackerNetworkError,
        TrackerParseError,
    )

    assert "认证失败" in friendly_error(TrackerAuthError("用户名或密码错误"))
    assert "无法连接" in friendly_error(TrackerNetworkError())
    assert "无法连接" in friendly_error(httpx.ConnectError("boom"))
    assert "格式异常" in friendly_error(TrackerParseError())
    assert "未知错误" in friendly_error(ValueError("x"))


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    """构造带指定状态码的 httpx.HTTPStatusError 测试样本。"""
    request = httpx.Request("GET", "https://example.com")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


def test_friendly_error_distinguishes_status_codes() -> None:
    """5xx（含 Cloudflare 521）是站点故障，绝不能提示用户去检查凭据。"""
    from movieclaw_api.services.verification import friendly_error

    msg_521 = friendly_error(_http_status_error(521))
    assert "站点服务器暂时不可用" in msg_521
    assert "凭据" not in msg_521
    assert "认证" not in msg_521

    assert "凭据" in friendly_error(_http_status_error(401))
    assert "凭据" in friendly_error(_http_status_error(403))
    assert "限流" in friendly_error(_http_status_error(429))
    # 其他 4xx 保持通用提示
    assert "异常状态码" in friendly_error(_http_status_error(418))


def test_transient_error_classification() -> None:
    """瞬时/非瞬时分流：决定同步失败时「退避重试」还是「作废会话重认证」。"""
    from movieclaw_api.services.verification import is_transient_error
    from movieclaw_tracker.exceptions import (
        TrackerAuthError,
        TrackerNetworkError,
        TrackerParseError,
    )

    # 瞬时：网络不可达、超时、5xx、429——等待即可自愈，不应作废认证会话
    assert is_transient_error(TrackerNetworkError())
    assert is_transient_error(httpx.ConnectError("boom"))
    assert is_transient_error(httpx.ReadTimeout("slow"))
    assert is_transient_error(_http_status_error(521))
    assert is_transient_error(_http_status_error(503))
    assert is_transient_error(_http_status_error(429))

    # 非瞬时：认证/解析/其余 4xx——可能凭据失效，需作废会话重建
    assert not is_transient_error(TrackerAuthError())
    assert not is_transient_error(TrackerParseError())
    assert not is_transient_error(_http_status_error(403))
    assert not is_transient_error(ValueError("x"))


# ---------------------------------------------------------------------------
# 同步失败降级：指数退避与游标失败台账
# ---------------------------------------------------------------------------


def test_adapt_interval_backs_off_on_consecutive_failures() -> None:
    """失败退避：首次失败原速重试（可能只是抖动），连续失败指数放疏、封顶 MAX。"""
    from movieclaw_api.services.torrent_sync import (
        _MAX_INTERVAL,
        _adapt_interval,
    )

    kw = {"new_count": 0, "full_page": False}
    # 首次失败：维持当前间隔
    assert _adapt_interval(300, consecutive_failures=1, **kw) == 300
    # 连续失败：每轮 ×2（current 已含上一轮翻倍，效果即指数退避）
    assert _adapt_interval(300, consecutive_failures=2, **kw) == 600
    assert _adapt_interval(600, consecutive_failures=3, **kw) == 1200
    # 封顶：宕机再久也不会超过 MAX
    assert _adapt_interval(_MAX_INTERVAL, consecutive_failures=9, **kw) == _MAX_INTERVAL
    # 成功（失败数为 0）：恢复正常自适应——冷站放疏
    assert _adapt_interval(600, consecutive_failures=0, **kw) == 900


def test_adapt_interval_pinned_for_boost_sites() -> None:
    """刷流站点钉在最快节奏：成功时无论冷热都回 MIN（抢免费新种拼发现速度）；
    失败退避不受钉住影响（对挂掉的站高频重试没有意义）。"""
    from movieclaw_api.services.torrent_sync import _MIN_INTERVAL, _adapt_interval

    kw = {"new_count": 0, "full_page": False, "pinned": True}
    # 冷站本会放疏到小时级，钉住后仍回 MIN
    assert _adapt_interval(21600, consecutive_failures=0, **kw) == _MIN_INTERVAL
    assert _adapt_interval(600, consecutive_failures=0, **kw) == _MIN_INTERVAL
    # 失败退避照常：钉住不等于对宕机站点高频硬打
    assert _adapt_interval(600, consecutive_failures=2, **kw) == 1200


async def test_expire_cursor_marks_immediate_due(db) -> None:
    """开启刷流时游标标记立即到期：next_sync_at 置 NULL，下一个 tick 即同步；
    游标不存在时返回 False，不隐式建游标（t0 语义归 ensure_cursor）。"""
    from movieclaw_db.repositories.torrent_repo import TorrentRepository

    async with get_database().session() as session:
        repo = TorrentRepository(session)
        await repo.ensure_cursor("mteam")
        # 先跑出一个"排期在几小时后"的游标
        await repo.update_cursor_after_sync(
            "mteam", new_count=0, full_page=False, next_interval_seconds=21600
        )
        cursor = await repo.get_cursor("mteam")
        assert cursor is not None and cursor.next_sync_at is not None

        assert await repo.expire_cursor("mteam")
        cursor = await repo.get_cursor("mteam")
        assert cursor is not None and cursor.next_sync_at is None

        assert not await repo.expire_cursor("no-such-site")


async def test_cursor_tracks_failure_streak_and_last_success(db) -> None:
    """游标失败台账：失败不动 last_success_at、累计 consecutive_failures；成功双双复位。"""
    from movieclaw_db.repositories.torrent_repo import TorrentRepository

    async with get_database().session() as session:
        repo = TorrentRepository(session)
        # 一次成功：写 last_success_at、失败数为 0
        await repo.update_cursor_after_sync(
            "mteam", new_count=1, full_page=False, next_interval_seconds=600
        )
        cursor = await repo.get_cursor("mteam")
        assert cursor.last_success_at is not None
        assert cursor.consecutive_failures == 0
        success_at = cursor.last_success_at

        # 两次失败：last_success_at 停在原处，失败数按调用方传入累计
        for n in (1, 2):
            await repo.update_cursor_after_sync(
                "mteam",
                error="站点服务器暂时不可用（状态码 521）",
                consecutive_failures=n,
                next_interval_seconds=1200,
            )
        cursor = await repo.get_cursor("mteam")
        assert cursor.last_success_at == success_at
        assert cursor.consecutive_failures == 2
        assert "521" in cursor.last_error

        # 恢复成功：失败数清零、last_error 清空、last_success_at 前移
        await repo.update_cursor_after_sync(
            "mteam", new_count=0, full_page=False, next_interval_seconds=900
        )
        cursor = await repo.get_cursor("mteam")
        assert cursor.consecutive_failures == 0
        assert cursor.last_error is None
        assert cursor.last_success_at >= success_at


async def test_reverify_success_resets_sync_cursor(db, monkeypatch) -> None:
    """重新验证成功后复位同步游标：清旧错误、清失败计数、标记立即到期。

    覆盖真实场景：熔断后用户更新凭据并重验成功，页面不应继续挂着
    「同步已暂停」的旧文案，也不应等熔断时排下的数小时退避走完才恢复同步。
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import movieclaw_api.services.verification as verification
    from movieclaw_db.repositories.torrent_repo import TorrentRepository

    await _make_active_with_failures(db, "mteam", failures=4)

    profile = SimpleNamespace(
        user_id="1",
        username="tester",
        user_class=None,
        uploaded_bytes=0,
        downloaded_bytes=0,
        ratio=None,
        bonus=None,
        seeding_count=None,
        leeching_count=None,
        avatar_url=None,
        join_date=None,
    )
    fake_site = SimpleNamespace(
        authenticate=AsyncMock(return_value=SimpleNamespace(success=True, message=None)),
        get_user_profile=AsyncMock(return_value=profile),
        client=SimpleNamespace(close=AsyncMock()),
    )

    async def _fake_create_site(site_id, **kwargs):
        return fake_site

    async def _noop_invalidate(site_id):
        pass

    monkeypatch.setattr(verification, "build_auth_provider", lambda cred: None)
    monkeypatch.setattr(verification, "create_site", _fake_create_site)
    monkeypatch.setattr(verification, "invalidate_site_access", _noop_invalidate)

    await verification.verify_site("mteam")

    async with db.session() as session:
        row = await CredentialRepository(session).get_by_site("mteam")
        cursor = await TorrentRepository(session).get_cursor("mteam")
    assert row.status == ConfigStatus.ACTIVE
    assert cursor.last_error is None
    assert cursor.consecutive_failures == 0
    assert cursor.next_sync_at is None  # NULL = 立即到期，下一个 tick 即恢复同步


# ---------------------------------------------------------------------------
# 熔断：连续非瞬时失败达阈值 → 凭据置 FAILED、同步暂停；瞬时失败永不熔断
# ---------------------------------------------------------------------------


class _FailingAccess:
    """替身站点访问器：get() 总是抛出指定异常，模拟同步必失败的站点。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def get(self, site_id: str):
        raise self._exc


async def _noop_invalidate(site_id: str) -> None:
    pass


async def _make_active_with_failures(db, site_id: str, failures: int):
    """造一个 ACTIVE 站点并把游标预置为已连续失败 N 次，返回凭据快照。"""
    from movieclaw_db.repositories.torrent_repo import TorrentRepository

    async with db.session() as session:
        service = SiteConfigService(session)
        await service.configure(site_id=site_id, auth_type=AuthType.APIKEY, api_key="k")
    async with db.session() as session:
        repo = CredentialRepository(session)
        await repo.update_status(site_id, ConfigStatus.ACTIVE)
        await TorrentRepository(session).update_cursor_after_sync(
            site_id,
            error="模拟：历史失败",
            consecutive_failures=failures,
            next_interval_seconds=600,
        )
        return await repo.get_by_site(site_id)


async def test_breaker_trips_on_repeated_nontransient_failures(db, monkeypatch) -> None:
    """第 10 次非瞬时失败（解析类）触发熔断：凭据转 FAILED，原因说明如何恢复。"""
    import movieclaw_api.services.torrent_sync as torrent_sync
    from movieclaw_db.repositories.torrent_repo import TorrentRepository
    from movieclaw_tracker.exceptions import TrackerParseError

    cred = await _make_active_with_failures(db, "mteam", failures=9)
    monkeypatch.setattr(
        torrent_sync, "get_site_access", lambda: _FailingAccess(TrackerParseError())
    )
    monkeypatch.setattr(torrent_sync, "invalidate_site_access", _noop_invalidate)

    await torrent_sync._sync_one_site(cred)

    async with db.session() as session:
        row = await CredentialRepository(session).get_by_site("mteam")
        cursor = await TorrentRepository(session).get_cursor("mteam")
    assert row.status == ConfigStatus.FAILED
    assert "同步已暂停" in row.last_error
    assert "重新验证" in row.last_error
    assert cursor.consecutive_failures == 10
    assert "同步已暂停" in cursor.last_error


async def test_breaker_ignores_transient_failures(db, monkeypatch) -> None:
    """瞬时失败（网络类）即使连续 10+ 次也不熔断：站点自愈后应自动恢复同步。"""
    import movieclaw_api.services.torrent_sync as torrent_sync
    from movieclaw_tracker.exceptions import TrackerNetworkError

    cred = await _make_active_with_failures(db, "mteam", failures=20)
    monkeypatch.setattr(
        torrent_sync, "get_site_access", lambda: _FailingAccess(TrackerNetworkError())
    )
    monkeypatch.setattr(torrent_sync, "invalidate_site_access", _noop_invalidate)

    await torrent_sync._sync_one_site(cred)

    async with db.session() as session:
        row = await CredentialRepository(session).get_by_site("mteam")
    assert row.status == ConfigStatus.ACTIVE
    assert "自动重试" in (await _cursor_error(db, "mteam"))


async def test_auth_failure_trips_breaker_fast(db, monkeypatch) -> None:
    """认证类失败（如账号已停用）第 2 次即熔断，不必等满 10 次。

    同时熔断要点亮全局系统通知——站点静默退出搜索/投递必须被用户感知。
    """
    from sqlmodel import select

    import movieclaw_api.services.torrent_sync as torrent_sync
    from movieclaw_db.models import NoticeStatus, SystemNotice
    from movieclaw_tracker.exceptions import TrackerAuthError

    cred = await _make_active_with_failures(db, "mteam", failures=1)
    monkeypatch.setattr(
        torrent_sync,
        "get_site_access",
        lambda: _FailingAccess(TrackerAuthError("M-Team 接口返回错误：帳號已停用")),
    )
    monkeypatch.setattr(torrent_sync, "invalidate_site_access", _noop_invalidate)

    await torrent_sync._sync_one_site(cred)

    async with db.session() as session:
        row = await CredentialRepository(session).get_by_site("mteam")
        notice = (
            await session.execute(
                select(SystemNotice).where(SystemNotice.dedupe_key == "site:mteam")
            )
        ).scalar_one()
    assert row.status == ConfigStatus.FAILED
    assert "帳號已停用" in row.last_error
    assert "同步已暂停" in row.last_error
    assert "重新验证" in row.last_error
    assert notice.status == NoticeStatus.ACTIVE


async def test_first_auth_failure_leaves_selfheal_chance(db, monkeypatch) -> None:
    """首次认证失败不熔断：可能只是共享会话过期，下一轮重建会话即可自愈。"""
    import movieclaw_api.services.torrent_sync as torrent_sync
    from movieclaw_db.repositories.torrent_repo import TorrentRepository
    from movieclaw_tracker.exceptions import TrackerAuthError

    cred = await _make_active_with_failures(db, "mteam", failures=0)
    monkeypatch.setattr(
        torrent_sync, "get_site_access", lambda: _FailingAccess(TrackerAuthError())
    )
    monkeypatch.setattr(torrent_sync, "invalidate_site_access", _noop_invalidate)

    await torrent_sync._sync_one_site(cred)

    async with db.session() as session:
        row = await CredentialRepository(session).get_by_site("mteam")
        cursor = await TorrentRepository(session).get_cursor("mteam")
    assert row.status == ConfigStatus.ACTIVE
    assert cursor.consecutive_failures == 1


async def test_breaker_not_tripped_below_threshold(db, monkeypatch) -> None:
    """非瞬时失败但未达阈值：只累计失败数与退避，不动凭据状态。"""
    import movieclaw_api.services.torrent_sync as torrent_sync
    from movieclaw_db.repositories.torrent_repo import TorrentRepository
    from movieclaw_tracker.exceptions import TrackerParseError

    cred = await _make_active_with_failures(db, "mteam", failures=3)
    monkeypatch.setattr(
        torrent_sync, "get_site_access", lambda: _FailingAccess(TrackerParseError())
    )
    monkeypatch.setattr(torrent_sync, "invalidate_site_access", _noop_invalidate)

    await torrent_sync._sync_one_site(cred)

    async with db.session() as session:
        row = await CredentialRepository(session).get_by_site("mteam")
        cursor = await TorrentRepository(session).get_cursor("mteam")
    assert row.status == ConfigStatus.ACTIVE
    assert cursor.consecutive_failures == 4


async def _cursor_error(db, site_id: str) -> str:
    from movieclaw_db.repositories.torrent_repo import TorrentRepository

    async with db.session() as session:
        cursor = await TorrentRepository(session).get_cursor(site_id)
    return cursor.last_error or ""
