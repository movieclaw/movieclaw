"""「订阅止于投递」两半的测试：库存对账关闭工单 + 投递救援巡检。

订阅不再亲自跟踪完成与搬运：工单完成状态由 library_file 库存对账推导
（任何入库路径都能关闭工单）；订阅只照看投递结果的死活，并在完成字节
连续 15/30 分钟不增长时提醒/自动试用同品质替代源。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.download_progress as progress_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.subscription.wanted_fulfillment import close_fulfilled_wanted
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ActivityType,
    DownloadAttemptStatus,
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    SiteTorrent,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    TorrentSource,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"


def _fake_tmdb() -> TmdbClient:
    """本文件的用例都用已建好的条目，不再回源；给个恒 404 的传输层即可。"""
    return TmdbClient(
        _KEY,
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})),
    )


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'fulfill.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, *, wanted_status=WantedStatus.GRABBED, info_hash="abc123", grabbed_at=None):
    """建 库/条目/订阅/工单 的最小闭包，返回 (library_id, item_id, sub_id, wanted_id)。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        item = MediaItem(kind="tv", tmdb_id=200, title="测试剧集", original_title="Test", year=2024)
        rule_set = RuleSet(name="默认", spec={})
        session.add(item)
        session.add(rule_set)
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="tv", rule_set_id=rule_set.id, library_id=library.id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        wanted = WantedItem(
            subscription_id=sub.id,
            media_item_id=item.id,
            season_number=1,
            episode_number=1,
            status=wanted_status,
            info_hash=info_hash,
            grabbed_at=grabbed_at or utcnow(),
        )
        session.add(wanted)
        await session.commit()
        await session.refresh(wanted)
        return library.id, item.id, sub.id, wanted.id


@pytest.mark.asyncio
async def test_inventory_closes_wanted_and_records_activity(db):
    """库存出现在位单元 → 对应工单标记 imported + 时间线活动 + 状态重算。"""
    library_id, item_id, sub_id, wanted_id = await _seed(db)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/测试剧集 (2024)/Season 01/测试剧集 (2024) - S01E01.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()

        closed = await close_fulfilled_wanted(session, item_id)
        assert closed == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.IMPORTED
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(a.type == "imported" and "对账" in a.message for a in activities)

        # 幂等：再次对账无事发生
        assert await close_fulfilled_wanted(session, item_id) == 0


@pytest.mark.asyncio
async def test_inventory_emits_fulfilled_webhook(db, monkeypatch):
    """对账关闭工单时产生 subscription.fulfilled webhook 事件（P3 订阅域接入）。"""
    import movieclaw_api.services.webhook as webhook_mod

    emitted = []
    monkeypatch.setattr(webhook_mod, "emit_events", emitted.extend)

    library_id, item_id, _, _ = await _seed(db)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/测试剧集 (2024)/Season 01/测试剧集 (2024) - S01E01.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1

    assert [e.event for e in emitted] == ["subscription.fulfilled"]
    assert emitted[0].data["media"]["tmdb_id"] == 200
    assert emitted[0].data["units"] == [[1, 1]]


@pytest.mark.asyncio
async def test_inventory_ignores_unrelated_units(db):
    """库里只有别的集：工单保持开放。"""
    library_id, item_id, sub_id, wanted_id = await _seed(db)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=2,  # 不是工单要的 E01
                file_path="/media/tv/x/e2.mkv",
                size_bytes=1,
                source=FileSource.SCANNED,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 0
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.GRABBED


@pytest.mark.asyncio
async def test_rescue_does_not_treat_unreachable_downloader_as_missing(db, monkeypatch):
    """所有下载器不可达只表示未知，不能清空工单或累计缺失次数。"""
    _library_id, _item_id, sub_id, wanted_id = await _seed(db)

    async def lookup_unknown(*args, **kwargs):
        return progress_mod._TorrentLookup(match=None, reachable_count=0)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_unknown)
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.GRABBED
        assert wanted.info_hash == "abc123"
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert attempt.last_downloader_state == "unknown"
        assert attempt.missing_observations == 0


@pytest.mark.asyncio
async def test_reachable_missing_keeps_infohash_until_three_observations(db, monkeypatch):
    """下载器可达但查无任务要连续确认三次；证据不足前保留旧 infohash 不动。"""
    stale = utcnow() - timedelta(minutes=20)
    _library_id, _item_id, sub_id, wanted_id = await _seed(db, grabbed_at=stale)

    async def lookup_missing(*args, **kwargs):
        return progress_mod._TorrentLookup(match=None, reachable_count=1)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_missing)
    for _ in range(2):
        await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.GRABBED
        assert wanted.info_hash == "abc123"
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert attempt.missing_observations == 2
        assert attempt.status == DownloadAttemptStatus.ACTIVE


@pytest.mark.asyncio
async def test_missing_main_source_requeues_wanted_and_reopens_manual_entries(db, monkeypatch):
    """主源确证消失（三次可达查无）：工单退回 wanted，人工介入入口重新可用。

    issue #238：换源保守策略只覆盖"源还在但没进度"，源已经从下载器消失时
    工单继续挂在 grabbed，业务层判定"在途"，立即搜索/手动选种全被"没有缺口"
    拒绝，用户只能反复点「立即换种」。
    """
    stale = utcnow() - timedelta(minutes=20)
    _library_id, _item_id, sub_id, wanted_id = await _seed(db, grabbed_at=stale)

    async def lookup_missing(*args, **kwargs):
        return progress_mod._TorrentLookup(match=None, reachable_count=1)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_missing)
    for _ in range(3):
        await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.WANTED
        assert wanted.info_hash is None
        assert wanted.grabbed_at is None
        assert wanted.next_search_at is not None
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.FAILED
        assert attempt.next_search_at is None
        assert attempt.cleanup_note
        # 时间线上要看得到"发生了什么"，而不是按钮突然变了
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id,
                        SubscriptionActivity.type == ActivityType.DISPATCH_FAILED,
                    )
                )
            ).scalars()
        )
        assert len(activities) == 1
        assert (activities[0].payload or {}).get("reason") == "source_missing"
        assert "下载器" in activities[0].message

        # 报告里被拒的两个人工入口之一：现在有真实缺口，不再报"没有缺口"
        service = SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))
        assert await service.search_now(sub_id) == 1


@pytest.mark.asyncio
async def test_missing_main_source_waits_for_trial_verdict(db, monkeypatch):
    """名下还有试用源时先让试用裁决；试用失败后的下一轮才退回工单。

    否则退回会让试用源失去目标被判失败连数据一起清理——而它可能正在正常下载。
    """
    stale = utcnow() - timedelta(minutes=20)
    _library_id, _item_id, sub_id, wanted_id = await _seed(db, grabbed_at=stale)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        await progress_mod._ensure_attempts(session, {(sub_id, "abc123"): [wanted]})
        parent = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        trial = SubscriptionDownloadAttempt(
            subscription_id=sub_id,
            info_hash="def456",
            torrent_title="Trial.Source",
            units=[[1, 1]],
            owned_by_movieclaw=True,
            status=DownloadAttemptStatus.TRIAL,
            replaces_attempt_id=parent.id,
            last_progress_at=utcnow(),
        )
        session.add(trial)
        await session.commit()
        trial_id = trial.id

    async def lookup_missing(*args, **kwargs):
        return progress_mod._TorrentLookup(match=None, reachable_count=1)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_missing)
    for _ in range(3):
        await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.GRABBED
        assert wanted.info_hash == "abc123"
        # 试用源自行失败后，主源消失的证据不变，下一轮才退回
        trial = await session.get(SubscriptionDownloadAttempt, trial_id)
        trial.status = DownloadAttemptStatus.FAILED
        await session.commit()

    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.WANTED
        assert wanted.info_hash is None


@pytest.mark.asyncio
async def test_completed_task_missing_before_import_reenters_rescue(db, monkeypatch):
    """下载完成但尚未入库时任务被删，不能藏在 completed 永久失去救援。"""
    stale = utcnow() - timedelta(minutes=20)
    _library_id, _item_id, sub_id, _wanted_id = await _seed(db, grabbed_at=stale)
    async with db.session() as session:
        wanted = (
            await session.execute(
                select(WantedItem).where(WantedItem.subscription_id == sub_id)
            )
        ).scalar_one()
        await progress_mod._ensure_attempts(session, {(sub_id, "abc123"): [wanted]})
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        attempt.status = DownloadAttemptStatus.COMPLETED
        attempt.last_progress_at = stale
        await session.commit()

    async def lookup_missing(*args, **kwargs):
        return progress_mod._TorrentLookup(match=None, reachable_count=1)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_missing)
    # 多跑两轮：曾经完成过的任务文件可能已落盘等入库，任何一轮都不能退回工单
    # 重复下载，只能沿用 15/30 分钟换源窗口。
    for _ in range(5):
        await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = (
            await session.execute(
                select(WantedItem).where(WantedItem.subscription_id == sub_id)
            )
        ).scalar_one()
        assert wanted.status == WantedStatus.GRABBED
        assert wanted.info_hash == "abc123"
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.ACTIVE
        assert attempt.stalled_notified_at is not None


@pytest.mark.asyncio
async def test_rescue_warns_at_15_minutes_and_replaces_at_30(db, monkeypatch):
    """完成字节不增长：15 分钟提醒，30 分钟保留工单并进入换源。"""
    from types import SimpleNamespace

    _library_id, _item_id, sub_id, wanted_id = await _seed(db)

    status = SimpleNamespace(
        name="Slow.Torrent",
        completed=False,
        completed_bytes=500,
        progress=0.5,
        state="stalled",
    )

    fake_downloader = SimpleNamespace(id=None, name="测试下载器", path_mappings=None)

    async def lookup_status(*args, **kwargs):
        return progress_mod._TorrentLookup(
            match=(fake_downloader, status), reachable_count=1
        )

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_status)
    # 首次采样只建立完成字节基线。
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        attempt.last_progress_at = utcnow() - timedelta(minutes=16)
        await session.commit()
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])
    async with db.session() as session:
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.ACTIVE
        assert attempt.stalled_notified_at is not None
        attempt.last_progress_at = utcnow() - timedelta(minutes=31)
        await session.commit()

    searched = []

    async def fake_search(attempt_id, *, force=False):
        searched.append(attempt_id)
        return False

    import movieclaw_api.services.subscription as subscription_services

    monkeypatch.setattr(subscription_services, "run_replacement_search", fake_search)
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.GRABBED
        assert wanted.info_hash == "abc123"
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.REPLACEMENT_PENDING
        assert searched == [attempt.id]

    # 已完成待入库（宽限期内）：救援不做任何事（搬运归监听导入/扫描，
    # 工单归库存对账，落点核验也等宽限期过后才判）
    _library_id2, _item_id2, sub_id2, wanted_id2 = await _seed_second(db)
    status.completed = True
    status.completed_bytes = 1000
    status.state = "completed"
    await progress_mod._rescue_group(sub_id2, "def456", downloaders=[])
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id2)
        assert wanted.status == WantedStatus.GRABBED


@pytest.mark.asyncio
async def test_rescue_alerts_unreachable_landing(db, monkeypatch, tmp_path):
    """落点核验：完成种子的实际目录在 movieclaw 侧不可见 → 告警活动去重记一次；
    可见则安静等待入库。"""
    from types import SimpleNamespace

    stale = utcnow() - timedelta(minutes=progress_mod._LANDING_GRACE_MINUTES + 5)
    _library_id, _item_id, sub_id, wanted_id = await _seed(db, grabbed_at=stale)

    # 下载器视角 /downloads ↔ movieclaw 视角 tmp_path/downloads（内容不存在）
    fake_downloader = SimpleNamespace(
        id=None,
        name="qb",
        path_mappings=[{"local": str(tmp_path / "downloads"), "remote": "/downloads"}],
    )
    status = SimpleNamespace(
        name="Some.Show.S01",
        completed=True,
        completed_bytes=1,
        progress=1.0,
        state="completed",
        save_path="/downloads",
        files=[SimpleNamespace(path="Some.Show.S01/e1.mkv", size_bytes=1)],
    )

    async def lookup_status(*args, **kwargs):
        return progress_mod._TorrentLookup(
            match=(fake_downloader, status), reachable_count=1
        )

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_status)
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])  # 二跑验证去重

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.GRABBED  # 不退回：数据真实存在
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id,
                        SubscriptionActivity.type == "import_failed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(activities) == 1  # 去重：只告警一次
        assert "看不到它" in activities[0].message
        assert activities[0].payload["reason"] == "path_unreachable"
        assert activities[0].payload["local_dir"] == str(tmp_path / "downloads")

    # 内容出现在预期位置后：不再新增告警（新种子哈希避开去重逻辑的干扰；
    # 工单时间改旧到宽限期外，确保走到落点判定而非被宽限期短路）
    (tmp_path / "downloads" / "Some.Show.S01").mkdir(parents=True)
    _l2, _i2, sub_id2, w2 = await _seed_second(db)
    async with db.session() as session:
        second = await session.get(WantedItem, w2)
        second.grabbed_at = stale
        second.updated_at = stale
        await session.commit()
    await progress_mod._rescue_group(sub_id2, "def456", downloaders=[])
    async with db.session() as session:
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id2,
                        SubscriptionActivity.type == "import_failed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert activities == []


@pytest.mark.asyncio
async def test_completed_attempt_rechecks_landing_after_grace(db, monkeypatch, tmp_path):
    """首次完成仍在宽限期时，过期后必须继续核验，而不是永久退出观察。"""
    _library_id, _item_id, sub_id, wanted_id = await _seed(db)
    fake_downloader = type(
        "Downloader",
        (),
        {
            "id": None,
            "name": "qb",
            "path_mappings": [
                {"local": str(tmp_path / "downloads"), "remote": "/downloads"}
            ],
        },
    )()
    status = type(
        "Status",
        (),
        {
            "name": "Grace.Show.S01",
            "completed": True,
            "completed_bytes": 1,
            "downloaded_bytes": 1,
            "progress": 1.0,
            "state": "completed",
            "save_path": "/downloads",
            "files": [type("File", (), {"path": "Grace.Show.S01/e1.mkv"})()],
        },
    )()

    async def lookup_status(*args, **kwargs):
        return progress_mod._TorrentLookup(match=(fake_downloader, status), reachable_count=1)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_status)
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        stale = utcnow() - timedelta(minutes=progress_mod._LANDING_GRACE_MINUTES + 1)
        wanted.grabbed_at = stale
        wanted.updated_at = stale
        await session.commit()

    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])
    async with db.session() as session:
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id,
                        SubscriptionActivity.type == "import_failed",
                    )
                )
            ).scalars()
        )
        assert len(activities) == 1


@pytest.mark.asyncio
async def test_legacy_attempt_does_not_guess_quality_without_exact_info_hash(db) -> None:
    """旧活动没有 infohash 时，即使单元重叠也不能猜成当前种子的品质。"""
    _library_id, _item_id, sub_id, wanted_id = await _seed(db)
    async with db.session() as session:
        session.add(
            SiteTorrent(
                site_id="legacy",
                torrent_id="wrong-source",
                title="Wrong.Source.720p",
                attrs={"resolution": "720p", "media_source": "WEB-Rip"},
                enrich_version=1,
                source=TorrentSource.SEARCH,
            )
        )
        session.add(
            SubscriptionActivity(
                subscription_id=sub_id,
                wanted_item_id=wanted_id,
                type="grabbed",
                message="旧版投递活动",
                payload={
                    "site_id": "legacy",
                    "torrent_id": "wrong-source",
                    "units": [[1, 1]],
                },
            )
        )
        await session.commit()
        wanted = await session.get(WantedItem, wanted_id)
        await progress_mod._ensure_attempts(session, {(sub_id, "abc123"): [wanted]})
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert attempt.quality == {}


async def _seed_second(db):
    """第二组样本（不同名称/哈希，避开唯一约束）。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/media/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=300, title="某电影", original_title="M", year=2020)
        rule_set = RuleSet(name="规则二", spec={})
        session.add(item)
        session.add(rule_set)
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="movie", rule_set_id=rule_set.id, library_id=library.id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        wanted = WantedItem(
            subscription_id=sub.id,
            media_item_id=item.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash="def456",
            grabbed_at=utcnow(),
        )
        session.add(wanted)
        await session.commit()
        await session.refresh(wanted)
        return library.id, item.id, sub.id, wanted.id
