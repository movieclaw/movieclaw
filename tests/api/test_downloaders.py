"""下载器配置管理接口的端到端测试。

覆盖：增删改查校验、保存后异步连接测试的状态流转、密码脱敏与落库加密、
启用停用、更新重测。真实的 create_downloader 被替换为假下载器，
不发真实请求，使状态流转可确定性断言。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import movieclaw_api.services.downloader_config as downloader_service
from movieclaw_api.core.config import get_settings
from movieclaw_downloader import (
    DownloaderConnectError,
    DownloaderInfo,
    TorrentBrief,
    TorrentFile,
    TorrentStatus,
)
from movieclaw_downloader.models import DownloaderConfig

# 假下载器行为开关：url 含 "unreachable" 时模拟连不上
_UNREACHABLE_MARK = "unreachable"

# 每次连接测试收到的 DownloaderConfig，供断言"传给适配器的密码已解密"
_captured_configs: list[DownloaderConfig] = []
_fake_torrents: list[TorrentBrief] = []
_fake_statuses: dict[str, TorrentStatus] = {}
_delete_calls: list[tuple[str, bool]] = []


class _FakeDownloader:
    """假适配器：跳过真实网络，按 url 决定测试结果。"""

    def __init__(self, config: DownloaderConfig) -> None:
        self.config = config

    async def test_connection(self) -> DownloaderInfo:
        _captured_configs.append(self.config)
        if _UNREACHABLE_MARK in self.config.url:
            raise DownloaderConnectError("无法连接到 qBittorrent，请检查 WebUI 地址和端口")
        return DownloaderInfo(type=self.config.type, version="v5.0.2")

    async def list_torrents(self) -> list[TorrentBrief]:
        if "task-error" in self.config.url:
            raise DownloaderConnectError("读取下载任务失败")
        return list(_fake_torrents)

    async def get_torrent(
        self, info_hash: str, *, include_files: bool = True
    ) -> TorrentStatus | None:
        status = _fake_statuses.get(info_hash)
        if status is None or include_files:
            return status
        return status.model_copy(update={"files": []})

    async def delete_torrent(self, info_hash: str, *, delete_files: bool = False) -> None:
        if "delete-error" in self.config.url:
            raise DownloaderConnectError("删除种子时无法连接到下载器")
        _delete_calls.append((info_hash, delete_files))
        _fake_torrents[:] = [
            torrent for torrent in _fake_torrents if torrent.info_hash != info_hash
        ]

    async def close(self) -> None:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 每个测试用独立临时 SQLite 库与密钥文件，保证隔离
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()

    # 用假适配器替换真实下载器客户端
    _captured_configs.clear()
    _fake_torrents.clear()
    _fake_statuses.clear()
    _delete_calls.clear()
    monkeypatch.setattr(downloader_service, "create_downloader", _FakeDownloader)
    import movieclaw_api.services.download_tasks as download_tasks_service

    monkeypatch.setattr(download_tasks_service, "create_downloader", _FakeDownloader)

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    # 本文件只测下载器配置业务，登录鉴权用依赖覆盖绕过（鉴权本身在 test_auth 覆盖）
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:  # with 块内触发 lifespan：建库、迁移、初始化加密器
        yield c, db_file
    get_settings.cache_clear()


_PAYLOAD = {
    "name": "家里的 qBittorrent",
    "client_type": "qbittorrent",
    "url": "http://192.168.1.10:8080",
    "username": "admin",
    "password": "s3cret",
    "save_path": "/downloads",
}


def test_create_then_async_verify_active_and_desensitized(client) -> None:
    c, _ = client
    r = c.post("/api/v1/downloaders", json=_PAYLOAD)
    assert r.status_code == 200
    data = r.json()["data"]
    # 接口立即返回 verifying（同步占位），绝不回传密码
    assert data["status"] == "verifying"
    assert "password" not in data

    # TestClient 的 BackgroundTasks 在响应后同步执行完毕 → 再查已是终态
    detail = c.get(f"/api/v1/downloaders/{data['id']}").json()["data"]
    assert detail["status"] == "active"
    assert detail["usable"] is True
    assert detail["version"] == "v5.0.2"
    assert detail["last_error"] is None
    assert detail["last_checked_at"] is not None

    # 传给适配器的密码是解密后的明文（证明加密→解密链路正确）
    assert _captured_configs[-1].password == "s3cret"
    assert _captured_configs[-1].username == "admin"


def test_password_encrypted_at_rest(client) -> None:
    c, db_file = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)

    # 直接读 SQLite 文件核实落库形态：密文带 enc:: 前缀，不含明文
    row = sqlite3.connect(db_file).execute("SELECT password FROM downloader_client").fetchone()
    assert row[0].startswith("enc::")
    assert "s3cret" not in row[0]


def test_unreachable_downloader_marked_failed(client) -> None:
    c, _ = client
    payload = {**_PAYLOAD, "url": "http://unreachable:8080"}
    r = c.post("/api/v1/downloaders", json=payload)
    detail = c.get(f"/api/v1/downloaders/{r.json()['data']['id']}").json()["data"]
    assert detail["status"] == "failed"
    assert detail["usable"] is False
    assert "无法连接" in detail["last_error"]


def test_create_rejects_invalid_url(client) -> None:
    c, _ = client
    r = c.post("/api/v1/downloaders", json={**_PAYLOAD, "url": "192.168.1.10:8080"})
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_create_rejects_duplicate_name(client) -> None:
    c, _ = client
    assert c.post("/api/v1/downloaders", json=_PAYLOAD).status_code == 200
    r = c.post("/api/v1/downloaders", json={**_PAYLOAD, "url": "http://other:8080"})
    assert r.status_code == 409
    assert "已被使用" in r.json()["message"]


def test_get_unknown_returns_404(client) -> None:
    c, _ = client
    assert c.get("/api/v1/downloaders/999").status_code == 404


def test_list_returns_all(client) -> None:
    c, _ = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)
    c.post(
        "/api/v1/downloaders",
        json={
            "name": "NAS 的 Transmission",
            "client_type": "transmission",
            "url": "http://192.168.1.20:9091",
        },
    )
    data = c.get("/api/v1/downloaders").json()["data"]
    assert [d["name"] for d in data] == ["家里的 qBittorrent", "NAS 的 Transmission"]
    # 未填用户名密码的下载器同样能通过测试（未开鉴权场景）
    assert data[1]["username"] is None
    assert data[1]["status"] == "active"


def test_task_center_aggregates_live_downloads_and_subscription_context(client) -> None:
    """只返回活跃/仍待入库任务，并为订阅任务补齐媒体身份、海报与季集上下文。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        SiteTorrent,
        Subscription,
        SubscriptionDownloadAttempt,
        TorrentSource,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    linked_hash = "a" * 40
    external_hash = "b" * 40
    old_seed_hash = "c" * 40
    missing_hash = "d" * 40
    media_identity: dict[str, int] = {}
    _fake_torrents.extend(
        [
            TorrentBrief(
                name="Subscription.Show.S01",
                content_name="Subscription.Show.S01",
                completed=True,
                info_hash=linked_hash,
                progress=1,
                size_bytes=1024,
                state="completed",
            ),
            TorrentBrief(
                name="External.Movie",
                content_name="External.Movie",
                completed=False,
                info_hash=external_hash,
                progress=0.42,
                dlspeed_bytes=2048,
                upspeed_bytes=512,
                state="downloading",
            ),
            TorrentBrief(
                name="Old.Seed",
                content_name="Old.Seed",
                completed=True,
                info_hash=old_seed_hash,
                progress=1,
                state="completed",
            ),
        ]
    )

    async def seed_subscription() -> None:
        db = get_database()
        async with db.session() as session:
            rule = RuleSet(name="任务中心测试规则", is_default=True)
            media = MediaItem(
                kind="tv",
                tmdb_id=98765,
                title="聚合测试剧",
                original_title="Task Center Show",
                poster_path="/task-center-show.jpg",
            )
            session.add(rule)
            session.add(media)
            await session.flush()
            assert rule.id is not None and media.id is not None
            media_identity["id"] = media.id
            subscription = Subscription(
                media_item_id=media.id,
                kind="tv",
                selected_seasons=[1],
                rule_set_id=rule.id,
            )
            session.add(subscription)
            await session.flush()
            assert subscription.id is not None
            session.add_all(
                [
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=1,
                        episode_number=1,
                        status=WantedStatus.GRABBED,
                        info_hash=linked_hash,
                    ),
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=1,
                        episode_number=2,
                        status=WantedStatus.GRABBED,
                        info_hash=missing_hash,
                    ),
                    # 季包边下边入库：同一 hash 的已入库集要以 imported 标注
                    # 补回覆盖列表，让前端标绿
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=1,
                        episode_number=3,
                        status=WantedStatus.IMPORTED,
                        info_hash=linked_hash,
                    ),
                ]
            )
            session.add(
                SubscriptionDownloadAttempt(
                    subscription_id=subscription.id,
                    downloader_id=downloader_id,
                    info_hash=missing_hash,
                    site_id="pt-demo",
                    torrent_id="4321",
                    torrent_title="Missing.Show.S01E02",
                    units=[[1, 2]],
                    quality={"resolution": "1080p", "media_source": "WEB-DL"},
                    owned_by_movieclaw=True,
                    hit_and_run=False,
                    status=DownloadAttemptStatus.ACTIVE,
                    last_progress_at=utcnow() - timedelta(minutes=16),
                )
            )
            # 站点快照索引里存有该种子的详情页，任务中心据此提供"打开种子页"
            session.add(
                SiteTorrent(
                    site_id="pt-demo",
                    torrent_id="4321",
                    title="Missing.Show.S01E02",
                    detail_url="https://pt.example.com/details.php?id=4321",
                    source=TorrentSource.LIST,
                )
            )
            await session.commit()

    asyncio.run(seed_subscription())

    response = c.get("/api/v1/downloaders/tasks")
    assert response.status_code == 200
    payload = response.json()["data"]
    by_hash = {item["info_hash"]: item for item in payload["items"]}
    assert set(by_hash) == {linked_hash, external_hash, missing_hash}
    assert by_hash[linked_hash]["source"] == "subscription"
    assert by_hash[linked_hash]["state"] == "completed"
    assert by_hash[linked_hash]["media_item_id"] == media_identity["id"]
    assert by_hash[linked_hash]["media_title"] == "聚合测试剧"
    assert by_hash[linked_hash]["poster_url"].endswith("/w342/task-center-show.jpg")
    assert by_hash[linked_hash]["subscriptions"][0]["media_item_id"] == media_identity["id"]
    assert (
        by_hash[linked_hash]["subscriptions"][0]["poster_url"] == by_hash[linked_hash]["poster_url"]
    )
    # 没有 attempt 台账声明覆盖范围时，units 只含在途工单；同 hash 的已
    # 入库集（上面 seed 的 S01E03）不会越过台账口径混进列表
    assert by_hash[linked_hash]["subscriptions"][0]["units"] == [
        {
            "season_number": 1,
            "episode_number": 1,
            "status": "grabbed",
            "replaced": False,
            "content_missing": False,
        }
    ]
    assert by_hash[external_hash]["source"] == "external"
    assert by_hash[external_hash]["progress"] == 0.42
    assert by_hash[external_hash]["upspeed_bytes"] == 512
    assert by_hash[missing_hash]["state"] == "missing"
    assert by_hash[missing_hash]["downloader_id"] == downloader_id
    # 投递台账带回站点身份、详情页与质量快照；外部任务没有这些信息
    assert by_hash[missing_hash]["site_id"] == "pt-demo"
    assert by_hash[missing_hash]["site_name"] == "pt-demo"
    assert by_hash[missing_hash]["page_url"] == "https://pt.example.com/details.php?id=4321"
    assert by_hash[missing_hash]["resolution"] == "1080p"
    assert by_hash[missing_hash]["media_source"] == "WEB-DL"
    # remux 缺席即否定（快照没写 = 不是原盘），不因缺键而丢字段
    assert by_hash[missing_hash]["remux"] is False
    assert by_hash[external_hash]["site_id"] is None
    assert by_hash[external_hash]["page_url"] is None
    assert by_hash[missing_hash]["can_replace"] is True
    assert by_hash[missing_hash]["no_progress_seconds"] >= 15 * 60
    assert "立即换种" in by_hash[missing_hash]["rescue_message"]
    # 同一剧集的多个资源带回同一个稳定媒体 ID，前端据此安全合并；不按标题猜测。
    assert by_hash[missing_hash]["media_item_id"] == by_hash[linked_hash]["media_item_id"]
    assert payload["sources"] == [
        {
            "id": downloader_id,
            "name": _PAYLOAD["name"],
            "client_type": "qbittorrent",
            "status": "active",
            "message": None,
            "task_count": 2,
        }
    ]


def test_task_view_keeps_remux_and_media_source_orthogonal() -> None:
    """Remux 与片源是两个字段：原盘直封的片源仍是 UHD Blu-ray，两者都要带回。

    （enrich 侧就这么分：REMUX 是封装方式不是片源，见 movieclaw_enrich.vocab）
    """
    from movieclaw_api.services.download_tasks import _task_dict

    view = _task_dict(
        task_id="qb:abcd",
        info_hash="abcd",
        torrent=None,
        downloader=None,
        subscriptions=[
            {
                "_quality": {
                    "resolution": "2160p",
                    "media_source": "UHD Blu-ray",
                    "remux": True,
                }
            }
        ],
        manual=None,
    )
    assert view["resolution"] == "2160p"
    assert view["media_source"] == "UHD Blu-ray"
    assert view["remux"] is True


def test_task_center_manual_task_links_site_torrent_page(client) -> None:
    """手动下载锚定了站点种子 ID 时，任务中心带回站点身份与种子详情页。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        Library,
        ManualDownloadIntent,
        MediaItem,
        SiteTorrent,
        TorrentSource,
    )

    c, _ = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)
    info_hash = "f" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Manual.Movie.2160p",
            content_name="Manual.Movie.2160p",
            completed=False,
            info_hash=info_hash,
            progress=0.2,
            state="downloading",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(
                kind="movie",
                tmdb_id=54321,
                title="手动种子页测试",
                original_title="Manual Page Test",
            )
            library = Library(name="手动种子页测试库", kind="movie", root_paths=["/media"])
            session.add_all([media, library])
            await session.flush()
            assert media.id is not None and library.id is not None
            session.add(
                ManualDownloadIntent(
                    info_hash=info_hash,
                    media_item_id=media.id,
                    library_id=library.id,
                    site_id="pt-demo",
                    torrent_id="777",
                )
            )
            session.add(
                SiteTorrent(
                    site_id="pt-demo",
                    torrent_id="777",
                    title="Manual.Movie.2160p",
                    detail_url="https://pt.example.com/details.php?id=777",
                    source=TorrentSource.LIST,
                )
            )
            await session.commit()

    asyncio.run(seed())

    items = c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["source"] == "manual"
    assert items[0]["site_id"] == "pt-demo"
    assert items[0]["site_name"] == "pt-demo"
    assert items[0]["page_url"] == "https://pt.example.com/details.php?id=777"


def test_task_center_degrades_single_downloader_failure(client) -> None:
    """一台下载器读取失败只标记来源异常，不能拖垮其余下载器任务。"""
    c, _ = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)
    failed = c.post(
        "/api/v1/downloaders",
        json={
            "name": "故障下载器",
            "client_type": "transmission",
            "url": "http://task-error:9091",
        },
    ).json()["data"]
    _fake_torrents.append(
        TorrentBrief(
            name="Still.Visible",
            content_name="Still.Visible",
            completed=False,
            info_hash="e" * 40,
            progress=0.2,
            state="downloading",
        )
    )

    response = c.get("/api/v1/downloaders/tasks")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert [item["name"] for item in payload["items"]] == ["Still.Visible"]
    sources = {source["id"]: source for source in payload["sources"]}
    assert sources[failed["id"]]["status"] == "error"
    assert "检查下载器连接" in sources[failed["id"]]["message"]


def test_task_center_keeps_full_unit_coverage_during_partial_ingest(client) -> None:
    """边下边入库时，覆盖集列表不能随入库推进缩水，且要带逐集状态。

    真实事故：整季种子只下了 19%，其中 E01 完整落盘并先行入库。旧实现按在途
    工单过滤 units，E01 一入库就从"覆盖剧集"里消失（显示成 E02–E10）；前端
    又只能靠"有没有入库 Job"猜整体状态，把 19% 的下载显示成"入库完成"。
    """
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "e" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Partial.Ingest.S01",
            content_name="Partial.Ingest.S01",
            completed=False,
            info_hash=info_hash,
            progress=0.19,
            size_bytes=68685304935,
            state="downloading",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(kind="tv", tmdb_id=4242, title="边下边入库剧", original_title="PI")
            rule = RuleSet(name="边下边入库规则")
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(
                media_item_id=media.id, kind="tv", selected_seasons=[1], rule_set_id=rule.id
            )
            session.add(subscription)
            await session.flush()
            # E01 已入库，E02 已落盘待搬，E03 仍在下载
            session.add_all(
                [
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=1,
                        episode_number=episode,
                        status=status,
                        info_hash=info_hash,
                    )
                    for episode, status in (
                        (1, WantedStatus.IMPORTED),
                        (2, WantedStatus.DOWNLOADED),
                        (3, WantedStatus.GRABBED),
                    )
                ]
            )
            session.add(
                SubscriptionDownloadAttempt(
                    subscription_id=subscription.id,
                    downloader_id=downloader_id,
                    info_hash=info_hash,
                    torrent_title="Partial.Ingest.S01",
                    units=[[1, 1], [1, 2], [1, 3]],
                    status=DownloadAttemptStatus.ACTIVE,
                    last_progress_at=utcnow(),
                )
            )
            await session.commit()

    asyncio.run(seed())
    items = c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
    task = next(item for item in items if item["info_hash"] == info_hash)
    # 已入库的 E01 仍留在覆盖列表里，且逐集状态可区分
    assert task["subscriptions"][0]["units"] == [
        {
            "season_number": 1,
            "episode_number": 1,
            "status": "imported",
            "replaced": False,
            "content_missing": False,
        },
        {
            "season_number": 1,
            "episode_number": 2,
            "status": "downloaded",
            "replaced": False,
            "content_missing": False,
        },
        {
            "season_number": 1,
            "episode_number": 3,
            "status": "grabbed",
            "replaced": False,
            "content_missing": False,
        },
    ]
    # 下载轴不被入库进度污染：种子仍在下载
    assert task["state"] == "downloading"
    assert task["progress"] == 0.19


def test_task_center_marks_content_missing_units(client) -> None:
    """真实事故：「S04 全集包」下完 7.59 GB，但包里根本没有被 TMDB 编号为
    S04E14 的特别版。工单退回重找后卡片既丢了影片身份（退化成外部任务），
    又继续挂着「已入库，其余等待下载完成」——和「下载完成」自相矛盾。

    内容核验的结论必须一路透到卡片：关联保留，那一集标成 content_missing。
    """
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "c" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Missing.Special.S04.Complete",
            content_name="Missing.Special.S04.Complete",
            completed=True,
            info_hash=info_hash,
            progress=1.0,
            size_bytes=8149700444,
            state="completed",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(kind="tv", tmdb_id=4343, title="内容不符剧", original_title="CM")
            rule = RuleSet(name="内容不符规则")
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(
                media_item_id=media.id, kind="tv", selected_seasons=[4], rule_set_id=rule.id
            )
            session.add(subscription)
            await session.flush()
            # E01 入库成功；E14 被核验判定不在包里，已退回 wanted（info_hash 清空）
            session.add_all(
                [
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=4,
                        episode_number=1,
                        status=WantedStatus.IMPORTED,
                        info_hash=info_hash,
                    ),
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=4,
                        episode_number=14,
                        status=WantedStatus.WANTED,
                    ),
                ]
            )
            session.add(
                SubscriptionDownloadAttempt(
                    subscription_id=subscription.id,
                    downloader_id=downloader_id,
                    info_hash=info_hash,
                    torrent_title="Missing.Special.S04.Complete",
                    units=[[4, 1], [4, 14]],
                    status=DownloadAttemptStatus.COMPLETED,
                    last_progress_at=utcnow(),
                    content_missing={
                        "units": [[4, 14]],
                        "sources": [["chdbits", "126352"]],
                    },
                )
            )
            await session.commit()

    asyncio.run(seed())
    items = c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
    task = next(item for item in items if item["info_hash"] == info_hash)
    # 工单已退回，关联仍在：卡片保住影片身份，才有地方讲清"为什么等不到"
    assert task["source"] == "subscription"
    assert task["media_title"] == "内容不符剧"
    assert task["subscriptions"][0]["units"] == [
        {
            "season_number": 4,
            "episode_number": 1,
            "status": "imported",
            "replaced": False,
            "content_missing": False,
        },
        {
            "season_number": 4,
            "episode_number": 14,
            "status": "wanted",
            "replaced": False,
            "content_missing": True,
        },
    ]


def test_task_center_reports_upgrade_progress_as_replacement(client) -> None:
    """洗版任务的进度必须按"已替换"讲，不能沿用"已入库"。

    真实事故：整季 2160p 洗版种刚投递、下载 0%，任务中心却显示"（有 16 集
    已入库）"——洗版单元在投递前就已经是 imported（库里有旧版本），那是洗版
    的**前提**而不是成果。判据只能是工单 info_hash 是否已被升级流程改指本
    种子（upgrade.py 确认升级时才改写），未替换的集必须全为 False。
    """
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    old_hash = "a1" * 20
    new_hash = "b2" * 20
    _fake_torrents.append(
        TorrentBrief(
            name="Upgrade.S07.2160p",
            content_name="Upgrade.S07.2160p",
            completed=False,
            info_hash=new_hash,
            progress=0.003,
            size_bytes=14548578304,
            state="downloading",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(kind="tv", tmdb_id=4343, title="洗版剧", original_title="UP")
            rule = RuleSet(name="洗版规则", spec={"upgrade_source": "web-dl"})
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(
                media_item_id=media.id, kind="tv", selected_seasons=[7], rule_set_id=rule.id
            )
            session.add(subscription)
            await session.flush()
            # 三集都早已入库（旧版本），工单 info_hash 仍指向旧种子
            session.add_all(
                [
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=7,
                        episode_number=episode,
                        status=WantedStatus.IMPORTED,
                        info_hash=old_hash,
                        imported_at=utcnow() - timedelta(days=30),
                    )
                    for episode in (1, 2, 3)
                ]
            )
            session.add(
                SubscriptionDownloadAttempt(
                    subscription_id=subscription.id,
                    downloader_id=downloader_id,
                    info_hash=new_hash,
                    torrent_title="Upgrade.S07.2160p",
                    units=[[7, 1], [7, 2], [7, 3]],
                    purpose="upgrade",
                    status=DownloadAttemptStatus.ACTIVE,
                    last_progress_at=utcnow(),
                )
            )
            await session.commit()

    asyncio.run(seed())
    subscription_view = next(
        item
        for item in c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
        if item["info_hash"] == new_hash
    )["subscriptions"][0]
    assert subscription_view["purpose"] == "upgrade"
    # 规则组没开收藏家模式：替换后旧版本会进回收站，卡片据此预告"待回收"
    assert subscription_view["upgrade_keep_old"] is False
    # 覆盖列表照常给出全部三集，但一集都还没替换
    assert subscription_view["units"] == [
        {
            "season_number": 7,
            "episode_number": 1,
            "status": "imported",
            "replaced": False,
            "content_missing": False,
        },
        {
            "season_number": 7,
            "episode_number": 2,
            "status": "imported",
            "replaced": False,
            "content_missing": False,
        },
        {
            "season_number": 7,
            "episode_number": 3,
            "status": "imported",
            "replaced": False,
            "content_missing": False,
        },
    ]

    async def confirm_first_episode() -> None:
        """模拟升级确认：E01 的工单改指新种子。"""
        async with get_database().session() as session:
            wanted = (
                await session.execute(
                    select(WantedItem).where(
                        WantedItem.season_number == 7, WantedItem.episode_number == 1
                    )
                )
            ).scalar_one()
            wanted.info_hash = new_hash
            await session.commit()

    asyncio.run(confirm_first_episode())
    replaced = [
        unit["replaced"]
        for unit in next(
            item
            for item in c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
            if item["info_hash"] == new_hash
        )["subscriptions"][0]["units"]
    ]
    assert replaced == [True, False, False]


def test_task_center_predicts_watch_rule_transfer_target(client) -> None:
    """命中监听导入规则时，卡片要能预告"下载完成后复制/硬链接到哪个库、哪个目录"。

    原来这一步只会说「等待下载完成 · 下一步」——对每条任务都一样，等于没说。
    落点口径必须与真正投递一致（同走 resolve_save_path），否则会出现"卡片说
    好、投递却挂"的漂移。
    """
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        ImportWatch,
        Library,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "e5" * 20
    _fake_torrents.append(
        TorrentBrief(
            name="Planned.S01",
            content_name="Planned.S01",
            completed=False,
            info_hash=info_hash,
            progress=0.1,
            state="downloading",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(
                kind="tv", tmdb_id=4646, title="落点剧", original_title="PL", year=2024
            )
            rule = RuleSet(name="落点规则")
            library = Library(name="综艺", kind="tv", root_paths=["/media/综艺"])
            session.add_all([media, rule, library])
            await session.flush()
            # 按 kind 的自动路由规则：源目录收下载，完成后 copy 进目标库
            session.add(ImportWatch(source_path="/download/剧集", strategy="copy", kind="tv"))
            subscription = Subscription(
                media_item_id=media.id,
                kind="tv",
                selected_seasons=[1],
                rule_set_id=rule.id,
                library_id=library.id,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                WantedItem(
                    subscription_id=subscription.id,
                    media_item_id=media.id,
                    season_number=1,
                    episode_number=1,
                    status=WantedStatus.GRABBED,
                    info_hash=info_hash,
                )
            )
            session.add(
                SubscriptionDownloadAttempt(
                    subscription_id=subscription.id,
                    downloader_id=downloader_id,
                    info_hash=info_hash,
                    torrent_title="Planned.S01",
                    units=[[1, 1]],
                    status=DownloadAttemptStatus.ACTIVE,
                    last_progress_at=utcnow(),
                )
            )
            await session.commit()

    asyncio.run(seed())
    task = next(
        item
        for item in c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
        if item["info_hash"] == info_hash
    )
    assert task["plan"] == {
        "mode": "watch",
        "strategy": "copy",
        "library_name": "综艺",
        "dest_path": "/media/综艺/落点剧 (2024)",
        "enters_library": True,
    }


def test_task_center_plan_reports_inplace_and_no_library(client) -> None:
    """没有监听规则=原地下载进库（无搬运步骤）；库无根路径=根本不会自动入库。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        Library,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    inplace_hash = "f6" * 20
    orphan_hash = "a7" * 20
    for name, h in (("Inplace.S01", inplace_hash), ("Orphan.S01", orphan_hash)):
        _fake_torrents.append(
            TorrentBrief(
                name=name,
                content_name=name,
                completed=False,
                info_hash=h,
                progress=0.1,
                state="downloading",
            )
        )

    async def seed() -> None:
        async with get_database().session() as session:
            rule = RuleSet(name="无监听规则")
            # 有根路径的库 → 原地下载；无根路径的库 → 下载器默认目录
            rooted = Library(name="纪录片", kind="tv", root_paths=["/media/纪录片"])
            rootless = Library(name="待配置", kind="tv", root_paths=[])
            session.add_all([rule, rooted, rootless])
            await session.flush()
            for idx, (title, library, info_hash) in enumerate(
                (("原地剧", rooted, inplace_hash), ("无根剧", rootless, orphan_hash))
            ):
                media = MediaItem(
                    kind="tv", tmdb_id=4700 + idx, title=title, original_title=title, year=2025
                )
                session.add(media)
                await session.flush()
                subscription = Subscription(
                    media_item_id=media.id,
                    kind="tv",
                    selected_seasons=[1],
                    rule_set_id=rule.id,
                    library_id=library.id,
                )
                session.add(subscription)
                await session.flush()
                session.add(
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=1,
                        episode_number=1,
                        status=WantedStatus.GRABBED,
                        info_hash=info_hash,
                    )
                )
                session.add(
                    SubscriptionDownloadAttempt(
                        subscription_id=subscription.id,
                        downloader_id=downloader_id,
                        info_hash=info_hash,
                        units=[[1, 1]],
                        status=DownloadAttemptStatus.ACTIVE,
                        last_progress_at=utcnow(),
                    )
                )
            await session.commit()

    asyncio.run(seed())
    by_hash = {
        item["info_hash"]: item
        for item in c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
    }
    assert by_hash[inplace_hash]["plan"] == {
        "mode": "inplace",
        "strategy": None,
        "library_name": "纪录片",
        "dest_path": "/media/纪录片/原地剧 (2025)",
        "enters_library": True,
    }
    # 库没根路径：投递落下载器默认目录，谁也搬不动它
    assert by_hash[orphan_hash]["plan"]["mode"] == "downloader_default"
    assert by_hash[orphan_hash]["plan"]["dest_path"] is None


def test_task_center_mixed_upgrade_does_not_count_gap_units_as_replaced(client) -> None:
    """混合投递（同一整季包既洗版又补缺）：缺口集不得被算成"已替换"。

    真实数据教训：S06 整季包洗 E01–E10、顺带补上从没有过的 E11。缺口工单从
    投递那一刻起 info_hash 就等于这个种子，只看 hash 会把它读成"已完成 1 集
    替换"——而它其实连下都没下完。判据必须再叠一条：该集要在 attempt 创建前
    就已入库，才可能是这次洗版的替换对象（与 upgrade.py 认定洗版目标同源）。
    """
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "d4" * 20
    _fake_torrents.append(
        TorrentBrief(
            name="Mixed.S06.2160p",
            content_name="Mixed.S06.2160p",
            completed=False,
            info_hash=info_hash,
            progress=0.4,
            state="downloading",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(kind="tv", tmdb_id=4545, title="混合投递剧", original_title="MIX")
            # 开了收藏家模式：替换后旧版本共存不删，卡片要预告"旧版本保留"
            rule = RuleSet(
                name="混合规则", spec={"upgrade_source": "web-dl", "upgrade_keep_old": True}
            )
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(
                media_item_id=media.id, kind="tv", selected_seasons=[6], rule_set_id=rule.id
            )
            session.add(subscription)
            await session.flush()
            attempt = SubscriptionDownloadAttempt(
                subscription_id=subscription.id,
                downloader_id=downloader_id,
                info_hash=info_hash,
                torrent_title="Mixed.S06.2160p",
                units=[[6, 1], [6, 2]],
                purpose="upgrade",
                status=DownloadAttemptStatus.ACTIVE,
                last_progress_at=utcnow(),
            )
            session.add(attempt)
            await session.flush()
            created = attempt.created_at
            session.add_all(
                [
                    # 洗版目标：attempt 之前就已入库，且已确认改指本种子
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=6,
                        episode_number=1,
                        status=WantedStatus.IMPORTED,
                        info_hash=info_hash,
                        imported_at=created - timedelta(days=7),
                    ),
                    # 缺口集：同一个包顺带补的，hash 从投递起就指向本种子
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=6,
                        episode_number=2,
                        status=WantedStatus.GRABBED,
                        info_hash=info_hash,
                    ),
                ]
            )
            await session.commit()

    asyncio.run(seed())
    subscription_view = next(
        item
        for item in c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
        if item["info_hash"] == info_hash
    )["subscriptions"][0]
    assert subscription_view["upgrade_keep_old"] is True
    units = subscription_view["units"]
    assert units == [
        {
            "season_number": 6,
            "episode_number": 1,
            "status": "imported",
            "replaced": True,
            "content_missing": False,
        },
        {
            "season_number": 6,
            "episode_number": 2,
            "status": "grabbed",
            "replaced": False,
            "content_missing": False,
        },
    ]


def test_task_center_marks_gap_download_units_as_not_replaced(client) -> None:
    """补缺下载的工单 hash 从投递起就等于本种子，绝不能被读成"已替换"。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "c3" * 20
    _fake_torrents.append(
        TorrentBrief(
            name="Gap.S01",
            content_name="Gap.S01",
            completed=False,
            info_hash=info_hash,
            progress=0.5,
            state="downloading",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(kind="tv", tmdb_id=4444, title="补缺剧", original_title="GAP")
            rule = RuleSet(name="补缺规则")
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(
                media_item_id=media.id, kind="tv", selected_seasons=[1], rule_set_id=rule.id
            )
            session.add(subscription)
            await session.flush()
            session.add(
                WantedItem(
                    subscription_id=subscription.id,
                    media_item_id=media.id,
                    season_number=1,
                    episode_number=1,
                    status=WantedStatus.GRABBED,
                    info_hash=info_hash,
                )
            )
            session.add(
                SubscriptionDownloadAttempt(
                    subscription_id=subscription.id,
                    downloader_id=downloader_id,
                    info_hash=info_hash,
                    torrent_title="Gap.S01",
                    units=[[1, 1]],
                    purpose="download",
                    status=DownloadAttemptStatus.ACTIVE,
                    last_progress_at=utcnow(),
                )
            )
            await session.commit()

    asyncio.run(seed())
    subscription_view = next(
        item
        for item in c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
        if item["info_hash"] == info_hash
    )["subscriptions"][0]
    assert subscription_view["purpose"] == "download"
    assert subscription_view["units"] == [
        {
            "season_number": 1,
            "episode_number": 1,
            "status": "grabbed",
            "replaced": False,
            "content_missing": False,
        }
    ]


def test_task_center_hides_completed_attempt_after_wanted_is_imported(client) -> None:
    """入库关闭工单后，继续保种的任务不能永久显示成“等待入库”。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "7" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Already.Imported",
            content_name="Already.Imported",
            completed=True,
            info_hash=info_hash,
            progress=1,
            state="completed",
        )
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(kind="movie", tmdb_id=778, title="已经入库", original_title="Done")
            rule = RuleSet(name="已入库规则")
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
            session.add(subscription)
            await session.flush()
            session.add(
                WantedItem(
                    subscription_id=subscription.id,
                    media_item_id=media.id,
                    season_number=0,
                    episode_number=0,
                    status=WantedStatus.IMPORTED,
                    info_hash=info_hash,
                )
            )
            session.add(
                SubscriptionDownloadAttempt(
                    subscription_id=subscription.id,
                    downloader_id=downloader_id,
                    info_hash=info_hash,
                    units=[[0, 0]],
                    status=DownloadAttemptStatus.COMPLETED,
                    last_progress_at=utcnow(),
                )
            )
            await session.commit()

    asyncio.run(seed())
    assert c.get("/api/v1/downloaders/tasks").json()["data"]["items"] == []


def test_immediate_replacement_rejects_ambiguous_shared_info_hash(client) -> None:
    """同一 hash 关联多个订阅时应返回可读错误，不能 scalar_one 直接 500。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "6" * 40

    async def seed() -> None:
        async with get_database().session() as session:
            for index in range(2):
                media = MediaItem(
                    kind="movie",
                    tmdb_id=880 + index,
                    title=f"共享任务 {index}",
                    original_title=f"Shared {index}",
                )
                rule = RuleSet(name=f"共享规则 {index}")
                session.add_all([media, rule])
                await session.flush()
                subscription = Subscription(
                    media_item_id=media.id, kind="movie", rule_set_id=rule.id
                )
                session.add(subscription)
                await session.flush()
                session.add(
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=0,
                        episode_number=0,
                        status=WantedStatus.GRABBED,
                        info_hash=info_hash,
                    )
                )
                session.add(
                    SubscriptionDownloadAttempt(
                        subscription_id=subscription.id,
                        downloader_id=downloader_id,
                        info_hash=info_hash,
                        units=[[0, 0]],
                        quality={"resolution": "1080p"},
                        status=DownloadAttemptStatus.ACTIVE,
                        last_progress_at=utcnow() - timedelta(minutes=16),
                    )
                )
            await session.commit()

    asyncio.run(seed())
    response = c.post(f"/api/v1/downloaders/{downloader_id}/torrents/{info_hash}/replace")
    assert response.status_code == 400
    assert "多个订阅" in response.json()["message"]


def test_task_center_can_request_immediate_replacement(client, monkeypatch) -> None:
    """15 分钟提醒后的“立即换种”只受理后台搜索，旧工单保持 grabbed。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "1" * 40
    attempt_identity: dict[str, int] = {}

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(kind="movie", tmdb_id=111, title="换源测试", original_title="Swap")
            rule = RuleSet(name="换源规则")
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(
                media_item_id=media.id,
                kind="movie",
                rule_set_id=rule.id,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                WantedItem(
                    subscription_id=subscription.id,
                    media_item_id=media.id,
                    season_number=0,
                    episode_number=0,
                    status=WantedStatus.GRABBED,
                    info_hash=info_hash,
                )
            )
            attempt = SubscriptionDownloadAttempt(
                subscription_id=subscription.id,
                downloader_id=downloader_id,
                info_hash=info_hash,
                torrent_title="Swap.2026.1080p.WEB-DL",
                units=[[0, 0]],
                quality={"resolution": "1080p", "media_source": "WEB-DL"},
                owned_by_movieclaw=True,
                hit_and_run=False,
                status=DownloadAttemptStatus.ACTIVE,
                last_progress_at=utcnow() - timedelta(minutes=16),
            )
            session.add(attempt)
            await session.commit()
            attempt_identity["id"] = attempt.id

    asyncio.run(seed())
    searched: list[tuple[int, bool]] = []

    async def fake_search(attempt_id: int, *, force: bool = False) -> bool:
        searched.append((attempt_id, force))
        return False

    import movieclaw_api.services.subscription as subscription_services

    monkeypatch.setattr(subscription_services, "run_replacement_search", fake_search)
    response = c.post(
        f"/api/v1/downloaders/{downloader_id}/torrents/{info_hash}/replace"
    )

    assert response.status_code == 200
    assert response.json()["data"]["attempt_id"] == attempt_identity["id"]
    assert searched == [(attempt_identity["id"], True)]

    async def state() -> tuple[str, str, str | None]:
        async with get_database().session() as session:
            attempt = await session.get(SubscriptionDownloadAttempt, attempt_identity["id"])
            wanted = (
                await session.execute(select(WantedItem).where(WantedItem.info_hash == info_hash))
            ).scalar_one()
            return attempt.status, wanted.status, wanted.info_hash

    assert asyncio.run(state()) == (
        DownloadAttemptStatus.REPLACEMENT_PENDING,
        WantedStatus.GRABBED,
        info_hash,
    )


def test_delete_download_task_targets_downloader_and_keeps_files_by_default(client) -> None:
    """任务中心按下载器 ID + infohash 删除；默认只删任务，不碰磁盘文件。"""
    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "f" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Delete.Me",
            content_name="Delete.Me",
            completed=False,
            info_hash=info_hash,
            progress=0.25,
            state="downloading",
        )
    )

    response = c.delete(f"/api/v1/downloaders/{downloader_id}/torrents/{info_hash}")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "downloader_id": downloader_id,
        "info_hash": info_hash,
        "delete_files": False,
    }
    assert _delete_calls == [(info_hash, False)]
    assert c.get("/api/v1/downloaders/tasks").json()["data"]["items"] == []


def test_cancelled_season_task_becomes_external_then_disappears_after_delete(client) -> None:
    """取消季后不再合成订阅任务；下载器任务可见为外部，手删后彻底消失。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
        utcnow,
    )

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "e" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Cancelled.Show.S01",
            content_name="Cancelled.Show.S01",
            completed=False,
            info_hash=info_hash,
            progress=0.3,
            state="downloading",
        )
    )
    attempt_id: dict[str, int] = {}

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(
                kind="tv",
                tmdb_id=1212,
                title="已取消第一季",
                original_title="Cancelled Season One",
            )
            rule = RuleSet(name="取消季规则")
            session.add_all([media, rule])
            await session.flush()
            subscription = Subscription(
                media_item_id=media.id,
                kind="tv",
                selected_seasons=[2],
                rule_set_id=rule.id,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                WantedItem(
                    subscription_id=subscription.id,
                    media_item_id=media.id,
                    season_number=1,
                    episode_number=1,
                    status=WantedStatus.GRABBED,
                    info_hash=info_hash,
                    in_scope=False,
                )
            )
            attempt = SubscriptionDownloadAttempt(
                subscription_id=subscription.id,
                downloader_id=downloader_id,
                info_hash=info_hash,
                units=[[1, 1]],
                status=DownloadAttemptStatus.CANCELLED,
                last_progress_at=utcnow(),
                cleanup_note="关联单元已退出当前订阅范围",
            )
            session.add(attempt)
            await session.commit()
            attempt_id["value"] = attempt.id

    asyncio.run(seed())

    before = c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
    assert len(before) == 1
    assert before[0]["info_hash"] == info_hash
    assert before[0]["source"] == "external"
    assert before[0]["subscriptions"] == []

    response = c.delete(f"/api/v1/downloaders/{downloader_id}/torrents/{info_hash}")
    assert response.status_code == 200
    assert c.get("/api/v1/downloaders/tasks").json()["data"]["items"] == []

    async def attempt_status() -> str:
        async with get_database().session() as session:
            attempt = await session.get(SubscriptionDownloadAttempt, attempt_id["value"])
            return attempt.status

    assert asyncio.run(attempt_status()) == DownloadAttemptStatus.CANCELLED


def test_delete_download_task_can_remove_data_files(client) -> None:
    """用户显式选择后，删除文件参数必须完整透传到下载器。"""
    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "a" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Delete.Files.Too",
            content_name="Delete.Files.Too",
            completed=True,
            info_hash=info_hash,
            progress=1.0,
            state="completed",
        )
    )

    response = c.delete(
        f"/api/v1/downloaders/{downloader_id}/torrents/{info_hash}",
        params={"delete_files": True},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "downloader_id": downloader_id,
        "info_hash": info_hash,
        "delete_files": True,
    }
    assert response.json()["message"] == "已删除种子任务和数据文件"
    assert _delete_calls == [(info_hash, True)]


def test_deleted_manual_download_does_not_reappear_as_missing(client) -> None:
    """删掉手动任务时同步清锚，不能幽灵般重现或留下 90 天孤儿行。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import Library, ManualDownloadIntent, MediaItem

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    info_hash = "e" * 40
    _fake_torrents.append(
        TorrentBrief(
            name="Manual.Delete.Me",
            content_name="Manual.Delete.Me",
            completed=False,
            info_hash=info_hash,
            progress=0.1,
            state="downloading",
        )
    )

    async def seed_manual_intent() -> None:
        async with get_database().session() as session:
            media = MediaItem(
                kind="movie",
                tmdb_id=12345,
                title="手动下载测试",
                original_title="Manual Download Test",
            )
            library = Library(name="手动下载测试库", kind="movie", root_paths=["/media"])
            session.add_all([media, library])
            await session.flush()
            assert media.id is not None and library.id is not None
            session.add(
                ManualDownloadIntent(
                    info_hash=info_hash,
                    media_item_id=media.id,
                    library_id=library.id,
                )
            )
            await session.commit()

    asyncio.run(seed_manual_intent())
    before = c.get("/api/v1/downloaders/tasks").json()["data"]["items"]
    assert before[0]["source"] == "manual"

    assert c.delete(f"/api/v1/downloaders/{downloader_id}/torrents/{info_hash}").status_code == 200

    assert c.get("/api/v1/downloaders/tasks").json()["data"]["items"] == []

    async def intent_exists() -> bool:
        async with get_database().session() as session:
            return (
                await session.execute(select(ManualDownloadIntent))
            ).scalar_one_or_none() is not None

    assert asyncio.run(intent_exists()) is False


def test_completed_manual_download_is_reconciled_after_inplace_library_scan(client) -> None:
    """历史原地下载已逐文件入账后，刷新任务中心会回收残留身份锚。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import Library, LibraryFile, ManualDownloadIntent, MediaItem

    c, _ = client
    c.post(
        "/api/v1/downloaders",
        json={
            **_PAYLOAD,
            "save_path": "/media",
            "path_mappings": [{"local": "/media", "remote": "/mnt/media"}],
        },
    )
    info_hash = "9" * 40
    save_path = "/media/movies/已入库影片 (2026)"
    file_path = f"{save_path}/release.mkv"
    _fake_torrents.append(
        TorrentBrief(
            name="Finished.Release",
            content_name="Finished.Release",
            completed=True,
            info_hash=info_hash,
            progress=1,
            state="completed",
        )
    )
    _fake_statuses[info_hash] = TorrentStatus(
        info_hash=info_hash,
        name="Finished.Release",
        progress=1,
        completed=True,
        # 下载器回报 remote 视角；对账必须先映射回上面的本地 save_path。
        save_path="/mnt/media/movies/已入库影片 (2026)",
        files=[TorrentFile(path="release.mkv", size_bytes=2048)],
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(
                kind="movie", tmdb_id=2026, title="已入库影片", original_title="Finished"
            )
            library = Library(name="电影库", kind="movie", root_paths=["/media/movies"])
            session.add_all([media, library])
            await session.flush()
            assert media.id is not None and library.id is not None
            session.add(
                ManualDownloadIntent(
                    info_hash=info_hash,
                    media_item_id=media.id,
                    library_id=library.id,
                )
            )
            await session.flush()
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=media.id,
                    file_path=file_path,
                    size_bytes=2048,
                    source="scanned",
                )
            )
            await session.commit()

    asyncio.run(seed())

    response = c.get("/api/v1/downloaders/tasks")

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []

    async def intent_exists() -> bool:
        async with get_database().session() as session:
            return (
                await session.execute(select(ManualDownloadIntent))
            ).scalar_one_or_none() is not None

    assert asyncio.run(intent_exists()) is False


def test_completed_tv_pack_waits_until_every_new_video_is_in_library(client) -> None:
    """旧集和部分新集不能误证整包完成；全部视频入账前继续展示。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import Library, LibraryFile, ManualDownloadIntent, MediaItem

    c, _ = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)
    info_hash = "8" * 40
    save_path = "/media/tv/谨慎对账 (2026)"
    _fake_torrents.append(
        TorrentBrief(
            name="Careful.Show.S01",
            content_name="Careful.Show.S01",
            completed=True,
            info_hash=info_hash,
            progress=1,
            state="completed",
        )
    )
    _fake_statuses[info_hash] = TorrentStatus(
        info_hash=info_hash,
        name="Careful.Show.S01",
        progress=1,
        completed=True,
        save_path=save_path,
        files=[
            TorrentFile(path="ep01.mkv", size_bytes=1001),
            TorrentFile(path="ep02.mkv", size_bytes=1002),
        ],
    )

    async def seed() -> None:
        async with get_database().session() as session:
            media = MediaItem(
                kind="tv", tmdb_id=3026, title="谨慎对账", original_title="Careful"
            )
            library = Library(name="剧集库", kind="tv", root_paths=["/media/tv"])
            session.add_all([media, library])
            await session.flush()
            assert media.id is not None and library.id is not None
            # 旧库存先于本次身份锚存在；即便尺寸与缺失的新集相同也不能拿来抵数。
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=media.id,
                    season_number=0,
                    episode_number=1,
                    file_path=f"{save_path}/old-episode.mkv",
                    size_bytes=1002,
                    source="scanned",
                )
            )
            await session.commit()
            session.add(
                ManualDownloadIntent(
                    info_hash=info_hash,
                    media_item_id=media.id,
                    library_id=library.id,
                )
            )
            await session.commit()
            # 本次季包只入账了第一集，第二集仍未被扫描确认。
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=media.id,
                    season_number=1,
                    episode_number=1,
                    file_path=f"{save_path}/ep01.mkv",
                    size_bytes=1001,
                    source="scanned",
                )
            )
            await session.commit()

    asyncio.run(seed())

    items = c.get("/api/v1/downloaders/tasks").json()["data"]["items"]

    assert len(items) == 1
    assert items[0]["info_hash"] == info_hash
    assert items[0]["state"] == "completed"


def test_delete_download_task_reports_missing_downloader_and_upstream_error(client) -> None:
    c, _ = client
    info_hash = "d" * 40
    assert c.delete(f"/api/v1/downloaders/999/torrents/{info_hash}").status_code == 404

    failed_id = c.post(
        "/api/v1/downloaders",
        json={**_PAYLOAD, "name": "删除失败", "url": "http://delete-error:8080"},
    ).json()["data"]["id"]
    response = c.delete(f"/api/v1/downloaders/{failed_id}/torrents/{info_hash}")
    assert response.status_code == 502
    assert response.json()["code"] == "UPSTREAM_ERROR"
    assert "无法连接" in response.json()["message"]


def test_enable_disable_toggles_usable(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]

    r = c.patch(f"/api/v1/downloaders/{did}/status", json={"enabled": False})
    assert r.json()["data"]["enabled"] is False
    assert r.json()["data"]["usable"] is False  # 已停用即不可用，与验证状态无关

    r = c.patch(f"/api/v1/downloaders/{did}/status", json={"enabled": True})
    assert r.json()["data"]["usable"] is True


def test_update_overwrites_and_reverifies(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]

    updated = {
        "name": "改名后的 qB",
        "client_type": "qbittorrent",
        "url": "http://10.0.0.2:8080",
        # 不再填用户名密码 → 覆盖为 None（未开鉴权语义）
    }
    r = c.put(f"/api/v1/downloaders/{did}", json=updated)
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "verifying"

    detail = c.get(f"/api/v1/downloaders/{did}").json()["data"]
    assert detail["name"] == "改名后的 qB"
    assert detail["url"] == "http://10.0.0.2:8080"
    assert detail["username"] is None
    assert detail["status"] == "active"
    # 重测时传给适配器的凭证已被覆盖清空
    assert _captured_configs[-1].password is None


def test_reverify_endpoint(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    checks_before = len(_captured_configs)

    r = c.post(f"/api/v1/downloaders/{did}/verify")
    assert r.status_code == 200
    assert len(_captured_configs) == checks_before + 1
    assert c.get(f"/api/v1/downloaders/{did}").json()["data"]["status"] == "active"


def _create_two(c) -> tuple[int, int]:
    """建两台下载器，返回 (第一台 id, 第二台 id)。"""
    first = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]
    second = c.post(
        "/api/v1/downloaders",
        json={
            "name": "NAS 的 Transmission",
            "client_type": "transmission",
            "url": "http://192.168.1.20:9091",
        },
    ).json()["data"]
    return first["id"], second["id"]


def test_first_created_becomes_default(client) -> None:
    c, _ = client
    first_id, second_id = _create_two(c)
    data = {d["id"]: d for d in c.get("/api/v1/downloaders").json()["data"]}
    assert data[first_id]["is_default"] is True
    assert data[second_id]["is_default"] is False


def test_set_default_switches_exclusively(client) -> None:
    c, _ = client
    first_id, second_id = _create_two(c)

    r = c.post(f"/api/v1/downloaders/{second_id}/default")
    assert r.status_code == 200
    assert r.json()["data"]["is_default"] is True

    # 有且只有一个默认：原默认被清掉
    data = {d["id"]: d for d in c.get("/api/v1/downloaders").json()["data"]}
    assert data[first_id]["is_default"] is False
    assert data[second_id]["is_default"] is True

    assert c.post("/api/v1/downloaders/999/default").status_code == 404


def test_delete_default_promotes_remaining(client) -> None:
    c, _ = client
    first_id, second_id = _create_two(c)

    assert c.delete(f"/api/v1/downloaders/{first_id}").status_code == 200
    remaining = c.get("/api/v1/downloaders").json()["data"]
    assert [d["id"] for d in remaining] == [second_id]
    # 默认自动让位给剩下的一台，一键下载永远有目标
    assert remaining[0]["is_default"] is True

    # 删非默认不影响默认归属
    third = c.post(
        "/api/v1/downloaders",
        json={"name": "第三台", "client_type": "qbittorrent", "url": "http://x:8080"},
    ).json()["data"]
    assert third["is_default"] is False
    c.delete(f"/api/v1/downloaders/{third['id']}")
    data = c.get("/api/v1/downloaders").json()["data"]
    assert data[0]["id"] == second_id and data[0]["is_default"] is True


def test_delete_then_404(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    assert c.delete(f"/api/v1/downloaders/{did}").status_code == 200
    assert c.get(f"/api/v1/downloaders/{did}").status_code == 404
    assert c.delete(f"/api/v1/downloaders/{did}").status_code == 404
