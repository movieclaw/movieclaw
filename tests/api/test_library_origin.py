"""文件来源快照（docs/design/library-duplicate-files.md §2）测试。

覆盖：四种来源的构造文案；旧行（origin 为空）的读时推导——带来源戳的入库行
查到投递记录拼订阅名、查不到时退回种子标题、扫描行按 source 给粗文案；
``upsert_by_path`` 不覆盖已有快照、``kept_at`` 写路径不碰；条目详情接口把
``origin`` / ``kept_at`` 暴露出去。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.origin import (
    derive_origins,
    manual_download_origin,
    origin_of,
    scan_origin,
    subscription_origin,
    watch_import_origin,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, LibraryFile, MediaItem, RuleSet, Subscription, utcnow
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.import_watch import ImportWatch
from movieclaw_db.models.manual_download_intent import ManualDownloadIntent
from movieclaw_db.models.site_torrent import SiteTorrent
from movieclaw_db.models.subscription import SubscriptionDownloadAttempt
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'origin.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):
    from movieclaw_api.api.deps import require_admin, require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    admin = Principal(kind="admin", name="tester")
    app.dependency_overrides[require_login] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# 构造文案
# ---------------------------------------------------------------------------


def _attempt(**kw) -> SubscriptionDownloadAttempt:
    base = dict(
        subscription_id=1,
        info_hash="a" * 40,
        site_id="unknown-site-for-test",
        torrent_id="42",
        torrent_title="Nine.Gates.2025.S01.2160p.WEB-DL-CHDWEB",
        units=[[1, 1]],
        last_progress_at=utcnow(),
    )
    base.update(kw)
    return SubscriptionDownloadAttempt(**base)


def test_subscription_origin_label_and_detail():
    auto = subscription_origin(
        _attempt(), item_title="九门", downloader_name="qBittorrent", strategy="hardlink"
    )
    assert auto["kind"] == "subscription"
    assert auto["label"] == "订阅《九门》自动投递"
    # 站点配置不存在时回落 site_id；段之间用「 · 」拼
    assert auto["detail"] == (
        "unknown-site-for-test · Nine.Gates.2025.S01.2160p.WEB-DL-CHDWEB · qBittorrent · 硬链接入库"
    )

    upgrade = subscription_origin(
        _attempt(purpose="upgrade", manual=True),
        item_title="九门",
        downloader_name=None,
        strategy="copy",
    )
    assert upgrade["label"] == "订阅《九门》洗版投递（人工选种）"
    assert upgrade["detail"].endswith("复制入库")
    assert "qBittorrent" not in upgrade["detail"]


def test_manual_watch_and_scan_origins():
    intent = ManualDownloadIntent(
        info_hash="b" * 40,
        media_item_id=1,
        library_id=1,
        site_id="site-x",
        torrent_id="7",
        download_name="Dune.Part.Two.2024",
    )
    manual = manual_download_origin(
        intent, torrent_title=None, downloader_name="qb", strategy="hardlink"
    )
    assert manual["kind"] == "manual_download"
    assert manual["label"] == "手动下载"
    # 没有种子标题时退回下载名
    assert manual["detail"] == "site-x · Dune.Part.Two.2024 · qb · 硬链接入库"

    rule = ImportWatch(source_path="/downloads/complete", strategy="copy")
    watch = watch_import_origin(rule)
    assert watch["kind"] == "watch_import"
    assert watch["detail"] == "/downloads/complete · 复制入库 · 未匹配到任何下载任务"

    assert scan_origin("manual") == {
        "kind": "scan",
        "label": "存量扫描发现（非本系统入库）",
        "detail": "手动扫描发现",
    }
    assert scan_origin("watch")["detail"] == "目录监听发现"
    assert scan_origin("scheduled")["detail"] == "定时对账发现"


# ---------------------------------------------------------------------------
# 读时推导 + 写路径保护
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derive_origins_for_legacy_rows(db, tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    async with db.session() as session:
        lib = await LibraryRepository(session).create(
            name="电影", kind="movie", root_paths=[str(root)]
        )
        item = MediaItem(kind="movie", tmdb_id=1, title="九门", original_title="NG", year=2025)
        rule = RuleSet(name="r", spec={})
        dl = DownloaderClient(name="qb@NAS", client_type="qbittorrent", url="http://x")
        session.add_all([item, rule, dl])
        await session.flush()
        sub = Subscription(media_item_id=item.id, kind="movie", rule_set_id=rule.id)
        session.add(sub)
        await session.flush()
        session.add(
            _attempt(
                subscription_id=sub.id,
                downloader_id=dl.id,
                site_id="hdsky-test",
                torrent_id="100",
                purpose="upgrade",
            )
        )
        session.add(
            SiteTorrent(
                site_id="hdsky-test",
                torrent_id="200",
                title="Nine.Gates.2025.1080p.WEB-DL-XXX",
                source="list",
            )
        )
        rows = [
            # 旧行：带来源戳，且能查到投递记录 → 订阅《九门》洗版投递
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "a.mkv"),
                source=FileSource.IMPORTED,
                site_id="hdsky-test",
                torrent_id="100",
            ),
            # 旧行：带来源戳，只有种子标题 → 手动或监听导入 · 站点 · 标题
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "b.mkv"),
                source=FileSource.IMPORTED,
                site_id="hdsky-test",
                torrent_id="200",
            ),
            # 旧行：扫描
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "c.mkv"),
                source=FileSource.SCANNED,
            ),
            # 旧行：入库但没戳
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "d.mkv"),
                source=FileSource.IMPORTED,
            ),
            # 新行：有落库快照，推导不碰它
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "e.mkv"),
                source=FileSource.SCANNED,
                origin={
                    "kind": "scan",
                    "label": "存量扫描发现（非本系统入库）",
                    "detail": "手动扫描发现",
                },
            ),
        ]
        session.add_all(rows)
        await session.commit()
        for r in rows:
            await session.refresh(r)

        derived = await derive_origins(session, rows)
        a, b, c, d, e = rows
        assert derived[a.id]["kind"] == "subscription"
        assert derived[a.id]["label"] == "订阅《九门》洗版投递"
        assert derived[a.id]["detail"].startswith(
            "hdsky-test · Nine.Gates.2025.S01.2160p.WEB-DL-CHDWEB"
        )
        assert "qb@NAS" in derived[a.id]["detail"]
        assert derived[b.id] == {
            "kind": "watch_import",
            "label": "手动或监听导入",
            "detail": "hdsky-test · Nine.Gates.2025.1080p.WEB-DL-XXX",
        }
        assert derived[c.id]["kind"] == "scan" and derived[c.id]["detail"] is None
        assert derived[d.id] == {
            "kind": "watch_import",
            "label": "监听目录导入",
            "detail": "来源种子未记录",
        }
        assert e.id not in derived
        assert origin_of(e, derived) == e.origin
        assert origin_of(a, derived) == derived[a.id]

        # 写路径保护：同路径再写入（扫描回归 / 识别重试）不覆盖已有快照，
        # kept_at 完全不碰
        e.kept_at = utcnow()
        await session.commit()
        repo = LibraryFileRepository(session)
        rewritten = await repo.upsert_by_path(
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "e.mkv"),
                source=FileSource.SCANNED,
                origin=scan_origin("scheduled"),
            )
        )
        assert rewritten.origin["detail"] == "手动扫描发现"
        assert rewritten.kept_at is not None
        # 没有快照的旧行被重新写入时补上本次的快照
        rewritten_c = await repo.upsert_by_path(
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "c.mkv"),
                source=FileSource.SCANNED,
                origin=scan_origin("scheduled"),
            )
        )
        assert rewritten_c.origin["detail"] == "定时对账发现"


@pytest.mark.asyncio
async def test_item_detail_exposes_origin(client, db, tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    async with db.session() as session:
        lib = await LibraryRepository(session).create(
            name="电影", kind="movie", root_paths=[str(root)]
        )
        item = MediaItem(kind="movie", tmdb_id=1, title="九门", original_title="NG", year=2025)
        session.add(item)
        await session.flush()
        session.add(
            LibraryFile(
                library_id=lib.id,
                media_item_id=item.id,
                file_path=str(root / "a.mkv"),
                size_bytes=1,
                source=FileSource.IMPORTED,
                origin={"kind": "manual_download", "label": "手动下载", "detail": "HDSky · qb"},
            )
        )
        await session.commit()
        lib_id, item_id = lib.id, item.id

    res = await client.get(f"/api/v1/libraries/{lib_id}/items/{item_id}")
    assert res.status_code == 200, res.text
    files = res.json()["data"]["files"]
    assert files[0]["origin"] == {
        "kind": "manual_download",
        "label": "手动下载",
        "detail": "HDSky · qb",
    }
    assert files[0]["kept_at"] is None
