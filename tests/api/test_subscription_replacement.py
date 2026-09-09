"""订阅死种换源的品质底线、退避与试用晋升测试。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.download_progress as progress_mod
import movieclaw_api.services.subscription.replacement as replacement_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription import (
    promote_trial,
    quality_not_lower,
    replacement_backoff,
    run_replacement_search,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    DownloaderClient,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_downloader import TorrentStatus
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import TorrentCandidate


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'replacement.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _candidate(**attrs) -> TorrentCandidate:
    return TorrentCandidate(
        site_id="test",
        torrent_id="2",
        title="Test.Movie.2026",
        subtitle="",
        attrs=TorrentAttrs(**attrs),
    )


def test_replacement_quality_never_silently_downgrades() -> None:
    old = {
        "resolution": "2160p",
        "media_source": "WEB-DL",
        "hdr": ["HDR10"],
    }
    assert quality_not_lower(
        _candidate(
            resolution="2160p",
            media_source="Blu-ray",
            hdr=["HDR10", "DV"],
        ),
        old,
    )
    assert not quality_not_lower(
        _candidate(resolution="1080p", media_source="Blu-ray", hdr=["HDR10"]),
        old,
    )
    assert not quality_not_lower(
        _candidate(resolution="2160p", media_source="WEB-DL", hdr=[]),
        old,
    )
    assert not quality_not_lower(
        _candidate(resolution="2160p", media_source="WEB-Rip", hdr=["HDR10"]),
        old,
    )
    assert not quality_not_lower(
        _candidate(resolution="2160p", media_source="Blu-ray"),
        {"resolution": "2160p", "media_source": "Blu-ray", "remux": True},
    )
    # 升级前的在途任务可能无法恢复原品质。无法证明底线时必须拒绝自动换源，
    # 不能把“未知”误当成“没有要求”。
    assert not quality_not_lower(_candidate(resolution="2160p", media_source="Blu-ray"), {})


def test_replacement_backoff_caps_at_daily_search() -> None:
    assert [replacement_backoff(index) for index in range(6)] == [
        timedelta(hours=1),
        timedelta(hours=3),
        timedelta(hours=12),
        timedelta(hours=24),
        timedelta(hours=24),
        timedelta(hours=24),
    ]


@pytest.mark.asyncio
async def test_real_search_distinguishes_no_candidate_from_site_failure(db, monkeypatch) -> None:
    """成功但无候选走 1h 退避；全站技术失败只短等 15 分钟且不升档。"""
    async with db.session() as session:
        media = MediaItem(
            kind="movie",
            tmdb_id=41,
            title="搜索换源",
            original_title="Replacement Search",
        )
        rule = RuleSet(name="搜索规则")
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
                info_hash="c" * 40,
            )
        )
        attempt = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash="c" * 40,
            torrent_title="Old.1080p.WEB-DL",
            units=[[0, 0]],
            quality={"resolution": "1080p", "media_source": "WEB-DL"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow() - timedelta(minutes=31),
            next_search_at=utcnow(),
        )
        session.add(attempt)
        await session.commit()
        attempt_id = attempt.id

    searched_keywords: list[str] = []

    async def empty_search(keyword, *, categories=None, exclude_protected=False):
        searched_keywords.append(keyword)
        return SimpleNamespace(
            sites=[SimpleNamespace(error=None, site_name="测试站")],
            items=[],
        )

    import movieclaw_api.services.site_search as site_search

    monkeypatch.setattr(site_search, "search_all_sites", empty_search)
    assert await run_replacement_search(attempt_id) is False
    assert searched_keywords == ["Replacement Search", "搜索换源"]

    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        assert attempt.search_attempts == 1
        delay = attempt.next_search_at - utcnow()
        assert timedelta(minutes=55) < delay <= timedelta(hours=1)
        attempt.next_search_at = utcnow()
        await session.commit()

    async def failed_search(keyword, *, categories=None, exclude_protected=False):
        return SimpleNamespace(
            sites=[SimpleNamespace(error="连接超时", site_name="故障站")],
            items=[],
        )

    monkeypatch.setattr(site_search, "search_all_sites", failed_search)
    assert await run_replacement_search(attempt_id, force=True) is False
    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        assert attempt.search_attempts == 1
        delay = attempt.next_search_at - utcnow()
        assert timedelta(minutes=14) < delay <= timedelta(minutes=15)


@pytest.mark.asyncio
async def test_trial_progress_switches_wanted_but_retains_unowned_old_task(db) -> None:
    """新源证明有进度后切换；旧任务所有权不明时绝不自动删除。"""
    async with db.session() as session:
        media = MediaItem(kind="movie", tmdb_id=42, title="测试电影", original_title="Test")
        rule = RuleSet(name="默认")
        session.add_all([media, rule])
        await session.flush()
        subscription = Subscription(
            media_item_id=media.id,
            kind="movie",
            rule_set_id=rule.id,
        )
        session.add(subscription)
        await session.flush()
        wanted = WantedItem(
            subscription_id=subscription.id,
            media_item_id=media.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash="a" * 40,
        )
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash="a" * 40,
            torrent_title="Old.2160p.WEB-DL",
            units=[[0, 0]],
            quality={"resolution": "2160p", "media_source": "WEB-DL"},
            owned_by_movieclaw=False,
            hit_and_run=None,
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow() - timedelta(minutes=31),
        )
        session.add_all([wanted, old])
        await session.flush()
        trial = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            replaces_attempt_id=old.id,
            info_hash="b" * 40,
            torrent_title="New.2160p.BluRay",
            units=[[0, 0]],
            quality={"resolution": "2160p", "media_source": "Blu-ray"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.TRIAL,
            baseline_completed_bytes=0,
            last_completed_bytes=2 * 1024 * 1024,
            last_progress_at=utcnow(),
        )
        session.add(trial)
        await session.commit()

        promoted = await promote_trial(
            session,
            trial,
            TorrentStatus(
                info_hash="b" * 40,
                name="New.2160p.BluRay",
                progress=0.01,
                completed_bytes=2 * 1024 * 1024,
                completed=False,
                save_path="/downloads",
                state="downloading",
            ),
        )
        assert promoted is True

        refreshed_wanted = (
            await session.execute(select(WantedItem).where(WantedItem.id == wanted.id))
        ).scalar_one()
        refreshed_old = await session.get(SubscriptionDownloadAttempt, old.id)
        refreshed_trial = await session.get(SubscriptionDownloadAttempt, trial.id)
        assert refreshed_wanted.info_hash == "b" * 40
        assert refreshed_trial.status == DownloadAttemptStatus.ACTIVE
        assert refreshed_old.status == DownloadAttemptStatus.RETAINED
        assert "投递前已经存在" in refreshed_old.cleanup_note


@pytest.mark.asyncio
async def test_trial_promotion_stops_when_target_left_subscription_scope(db) -> None:
    """试用源晋升前目标已出域时，新旧任务都保留，不能继续切换或清理。"""
    async with db.session() as session:
        media = MediaItem(kind="movie", tmdb_id=420, title="取消换源", original_title="Cancel")
        rule = RuleSet(name="取消换源规则")
        session.add_all([media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        wanted = WantedItem(
            subscription_id=subscription.id,
            media_item_id=media.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash="1" * 40,
            in_scope=False,
        )
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash="1" * 40,
            units=[[0, 0]],
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow(),
        )
        session.add_all([wanted, old])
        await session.flush()
        trial = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            replaces_attempt_id=old.id,
            info_hash="2" * 40,
            units=[[0, 0]],
            status=DownloadAttemptStatus.TRIAL,
            last_progress_at=utcnow(),
        )
        session.add(trial)
        await session.commit()

        promoted = await promote_trial(
            session,
            trial,
            TorrentStatus(
                info_hash=trial.info_hash,
                name="Trial",
                progress=0.1,
                completed_bytes=1024 * 1024,
                downloaded_bytes=1024 * 1024,
                completed=False,
                save_path="/downloads",
                state="downloading",
            ),
        )
        await session.refresh(old)
        await session.refresh(trial)
        await session.refresh(wanted)

        assert promoted is False
        assert old.status == DownloadAttemptStatus.CANCELLED
        assert trial.status == DownloadAttemptStatus.CANCELLED
        assert wanted.info_hash == "1" * 40


@pytest.mark.asyncio
async def test_pending_cleanup_never_deletes_after_scope_cancel(db, monkeypatch) -> None:
    """替代源已晋升但旧源尚待清理时，取消订阅必须抢占自动删除。"""
    async with db.session() as session:
        media = MediaItem(kind="movie", tmdb_id=421, title="清理竞态", original_title="Race")
        rule = RuleSet(name="清理竞态规则")
        session.add_all([media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        wanted = WantedItem(
            subscription_id=subscription.id,
            media_item_id=media.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash="4" * 40,
            in_scope=False,
        )
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash="3" * 40,
            units=[[0, 0]],
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.CLEANUP_PENDING,
            last_progress_at=utcnow(),
        )
        new = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            replaces_attempt_id=None,
            info_hash="4" * 40,
            units=[[0, 0]],
            status=DownloadAttemptStatus.ACTIVE,
            last_progress_at=utcnow(),
        )
        session.add_all([wanted, old])
        await session.flush()
        new.replaces_attempt_id = old.id
        session.add(new)
        await session.commit()

        def must_not_open_downloader(*args, **kwargs):
            raise AssertionError("取消范围后不应连接下载器执行自动删除")

        monkeypatch.setattr(replacement_mod, "create_downloader", must_not_open_downloader)
        await replacement_mod._cleanup_replaced_attempt(
            session,
            old,
            new,
            TorrentStatus(
                info_hash=new.info_hash,
                name="New",
                progress=0.2,
                completed_bytes=1024,
                completed=False,
                save_path="/downloads",
                state="downloading",
            ),
        )
        await session.refresh(old)
        await session.refresh(new)

        assert old.status == DownloadAttemptStatus.CANCELLED
        assert new.status == DownloadAttemptStatus.CANCELLED


@pytest.mark.asyncio
async def test_failed_trial_does_not_restore_backoff_after_scope_cancel(db, monkeypatch) -> None:
    """试用判失败与取消撞车时，不得重排旧源搜索或删除试用任务。"""
    async with db.session() as session:
        media = MediaItem(kind="movie", tmdb_id=422, title="失败竞态", original_title="Race")
        rule = RuleSet(name="失败竞态规则")
        session.add_all([media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        wanted = WantedItem(
            subscription_id=subscription.id,
            media_item_id=media.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash="5" * 40,
            in_scope=False,
        )
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash=wanted.info_hash,
            units=[[0, 0]],
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow(),
        )
        session.add_all([wanted, old])
        await session.flush()
        trial = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            replaces_attempt_id=old.id,
            info_hash="6" * 40,
            units=[[0, 0]],
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.TRIAL,
            last_progress_at=utcnow(),
        )
        session.add(trial)
        await session.commit()

        def must_not_open_downloader(*args, **kwargs):
            raise AssertionError("取消范围后不应连接下载器清理失败试用源")

        monkeypatch.setattr(replacement_mod, "create_downloader", must_not_open_downloader)
        await replacement_mod.fail_trial(session, trial, reason="试用无进度")
        await session.refresh(old)
        await session.refresh(trial)

        assert old.status == DownloadAttemptStatus.CANCELLED
        assert old.next_search_at is None
        assert trial.status == DownloadAttemptStatus.CANCELLED


@pytest.mark.asyncio
async def test_promoted_attempt_detects_scope_cancel_by_its_own_hash(db) -> None:
    """已晋升子尝试必须按新 hash 识别取消，不能继续沿用试用阶段的父 hash。"""
    async with db.session() as session:
        media = MediaItem(kind="movie", tmdb_id=423, title="晋升取消", original_title="Cancel")
        rule = RuleSet(name="晋升取消规则")
        session.add_all([media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        wanted = WantedItem(
            subscription_id=subscription.id,
            media_item_id=media.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash="8" * 40,
            in_scope=False,
        )
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash="7" * 40,
            units=[[0, 0]],
            status=DownloadAttemptStatus.RETAINED,
            last_progress_at=utcnow(),
        )
        session.add_all([wanted, old])
        await session.flush()
        replacement = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            replaces_attempt_id=old.id,
            info_hash=wanted.info_hash,
            units=[[0, 0]],
            status=DownloadAttemptStatus.ACTIVE,
            last_progress_at=utcnow(),
        )
        session.add(replacement)
        await session.commit()

        assert await progress_mod._cancel_attempt_if_out_of_scope(session, replacement)
        await session.refresh(replacement)
        assert replacement.status == DownloadAttemptStatus.CANCELLED


@pytest.mark.asyncio
async def test_trial_requires_network_bytes_not_rechecked_existing_blocks(db, monkeypatch) -> None:
    """复用旧文件产生的完成块不是网络进度；只有下载流量增长才能晋升试用源。"""
    async with db.session() as session:
        media = MediaItem(kind="movie", tmdb_id=43, title="校验测试", original_title="Verify")
        rule = RuleSet(name="校验规则")
        session.add_all([media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash="c" * 40,
            units=[[0, 0]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=False,
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow(),
        )
        wanted = WantedItem(
            subscription_id=subscription.id,
            media_item_id=media.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash=old.info_hash,
        )
        session.add_all([old, wanted])
        await session.flush()
        trial = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            replaces_attempt_id=old.id,
            info_hash="d" * 40,
            units=[[0, 0]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.TRIAL,
            baseline_completed_bytes=0,
            last_completed_bytes=0,
            last_progress_at=utcnow(),
        )
        session.add(trial)
        await session.commit()
        trial_id = trial.id

    fake_downloader = SimpleNamespace(id=None, name="测试下载器", path_mappings=[])
    status = SimpleNamespace(
        name="Verify.1080p",
        completed=False,
        completed_bytes=2 * 1024 * 1024,
        downloaded_bytes=0,
        progress=0.2,
        state="downloading",
        save_path="/downloads",
        files=[],
    )

    async def lookup(*args, **kwargs):
        return progress_mod._TorrentLookup(match=(fake_downloader, status), reachable_count=1)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup)
    await progress_mod._observe_attempt(trial_id, [])
    async with db.session() as session:
        trial = await session.get(SubscriptionDownloadAttempt, trial_id)
        wanted = (
            await session.execute(
                select(WantedItem).where(WantedItem.subscription_id == trial.subscription_id)
            )
        ).scalar_one()
        assert trial.status == DownloadAttemptStatus.TRIAL
        assert wanted.info_hash == "c" * 40

    status.downloaded_bytes = 2 * 1024 * 1024
    await progress_mod._observe_attempt(trial_id, [])
    async with db.session() as session:
        trial = await session.get(SubscriptionDownloadAttempt, trial_id)
        wanted = (
            await session.execute(
                select(WantedItem).where(WantedItem.subscription_id == trial.subscription_id)
            )
        ).scalar_one()
        assert trial.status == DownloadAttemptStatus.ACTIVE
        assert wanted.info_hash == "d" * 40


@pytest.mark.asyncio
async def test_trial_persists_cleanup_pending_before_touching_downloader(db, monkeypatch) -> None:
    """工单切换后即使进程退出，旧任务也必须留下可重试的清理状态。"""
    async with db.session() as session:
        media = MediaItem(kind="movie", tmdb_id=44, title="清理测试", original_title="Cleanup")
        rule = RuleSet(name="清理规则")
        session.add_all([media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash="e" * 40,
            units=[[0, 0]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow(),
        )
        wanted = WantedItem(
            subscription_id=subscription.id,
            media_item_id=media.id,
            season_number=0,
            episode_number=0,
            status=WantedStatus.GRABBED,
            info_hash=old.info_hash,
        )
        session.add_all([old, wanted])
        await session.flush()
        trial = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            replaces_attempt_id=old.id,
            info_hash="f" * 40,
            units=[[0, 0]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.TRIAL,
            last_progress_at=utcnow(),
        )
        session.add(trial)
        await session.commit()

        async def simulate_process_exit(*args, **kwargs):
            return None

        monkeypatch.setattr(replacement_mod, "_cleanup_replaced_attempt", simulate_process_exit)
        assert await promote_trial(
            session,
            trial,
            TorrentStatus(
                info_hash=trial.info_hash,
                name="Cleanup.1080p",
                progress=0.1,
                completed=False,
                save_path="/downloads",
                state="downloading",
            ),
        )
        await session.refresh(old)
        assert old.status == "cleanup_pending"


@pytest.mark.asyncio
async def test_pending_cleanup_is_idempotently_reconciled_after_restart(db, monkeypatch) -> None:
    """巡检能续做 cleanup_pending；重叠文件只删旧任务，不删数据。"""
    async with db.session() as session:
        downloader = DownloaderClient(
            name="清理下载器",
            client_type="qbittorrent",
            url="http://downloader:8080",
        )
        media = MediaItem(kind="movie", tmdb_id=45, title="恢复清理", original_title="Resume")
        rule = RuleSet(name="恢复规则")
        session.add_all([downloader, media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="movie", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            downloader_id=downloader.id,
            info_hash="1" * 40,
            units=[[0, 0]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status="cleanup_pending",
            last_progress_at=utcnow(),
        )
        session.add(old)
        await session.flush()
        new = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            downloader_id=downloader.id,
            replaces_attempt_id=old.id,
            info_hash="2" * 40,
            units=[[0, 0]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.ACTIVE,
            last_progress_at=utcnow(),
        )
        session.add(new)
        await session.commit()
        old_id = old.id

    statuses = {
        "1" * 40: TorrentStatus(
            info_hash="1" * 40,
            name="Old",
            progress=0.1,
            completed=False,
            save_path="/downloads",
            files=[],
        ),
        "2" * 40: TorrentStatus(
            info_hash="2" * 40,
            name="New",
            progress=0.1,
            completed=False,
            save_path="/downloads",
            files=[],
        ),
    }
    deleted: list[tuple[str, bool]] = []

    class FakeAdapter:
        async def get_torrent(self, info_hash, *, include_files=True):
            return statuses.get(info_hash)

        async def delete_torrent(self, info_hash, *, delete_files=False):
            deleted.append((info_hash, delete_files))
            statuses.pop(info_hash, None)

        async def close(self):
            return None

    monkeypatch.setattr(replacement_mod, "create_downloader", lambda config: FakeAdapter())
    assert await replacement_mod.reconcile_pending_cleanup(old_id)
    assert deleted == [("1" * 40, False)]
    async with db.session() as session:
        old = await session.get(SubscriptionDownloadAttempt, old_id)
        assert old.status == DownloadAttemptStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_upgraded_old_attempt_converges_when_torrent_gone(db, monkeypatch) -> None:
    """洗版留下的 cleanup_pending 能收敛：下载器里已经没有的旧任务判 SUPERSEDED。

    2026-09-07 生产事故：洗版链路是在新版本入库的同一时刻才把旧任务挂进
    CLEANUP_PENDING，此时新 attempt 已是 IMPORTED；而巡检只按 ACTIVE/COMPLETED
    找替代源台账，找不到就直接返回，旧任务原地卡死两天（NAS 上《早春晴朗》
    S01E19 的 1080p 死种，从头到尾一个字节没下）。
    """
    async with db.session() as session:
        downloader = DownloaderClient(
            name="洗版下载器", client_type="qbittorrent", url="http://downloader:8080"
        )
        media = MediaItem(kind="tv", tmdb_id=46, title="洗版收敛", original_title="Converge")
        rule = RuleSet(name="洗版规则")
        session.add_all([downloader, media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="tv", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            downloader_id=downloader.id,
            info_hash="3" * 40,
            units=[[1, 19]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            # 生产现场就是"考核状态未知"，不能靠 hit_and_run=False 才走通
            hit_and_run=None,
            status="cleanup_pending",
            last_progress_at=utcnow(),
        )
        session.add(old)
        await session.flush()
        new = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            downloader_id=downloader.id,
            replaces_attempt_id=old.id,
            info_hash="4" * 40,
            units=[[1, 19]],
            quality={"resolution": "2160p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.IMPORTED,
            last_progress_at=utcnow(),
        )
        session.add(new)
        await session.commit()
        old_id = old.id

    deleted: list[tuple[str, bool]] = []

    class FakeAdapter:
        async def get_torrent(self, info_hash, *, include_files=True):
            return None  # 新旧两个种子都已不在下载器里（生产现场如此）

        async def delete_torrent(self, info_hash, *, delete_files=False):
            deleted.append((info_hash, delete_files))

        async def close(self):
            return None

    monkeypatch.setattr(replacement_mod, "create_downloader", lambda config: FakeAdapter())
    assert await replacement_mod.reconcile_pending_cleanup(old_id)
    assert deleted == [], "状态收敛不得连带删除下载器任务"
    async with db.session() as session:
        old = await session.get(SubscriptionDownloadAttempt, old_id)
        assert old.status == DownloadAttemptStatus.SUPERSEDED
        assert old.cleanup_note == "旧任务已不在下载器中"


@pytest.mark.asyncio
async def test_upgraded_old_attempt_kept_seeding_when_still_present(db, monkeypatch) -> None:
    """旧种还在下载器里：保留做种并如实说明，不在洗版链路上新开删除行为。"""
    async with db.session() as session:
        downloader = DownloaderClient(
            name="洗版下载器2", client_type="qbittorrent", url="http://downloader:8080"
        )
        media = MediaItem(kind="tv", tmdb_id=47, title="洗版保留", original_title="Retain")
        rule = RuleSet(name="洗版规则2")
        session.add_all([downloader, media, rule])
        await session.flush()
        subscription = Subscription(media_item_id=media.id, kind="tv", rule_set_id=rule.id)
        session.add(subscription)
        await session.flush()
        old = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            downloader_id=downloader.id,
            info_hash="5" * 40,
            units=[[1, 20]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status="cleanup_pending",
            last_progress_at=utcnow(),
        )
        session.add(old)
        await session.flush()
        new = SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            downloader_id=downloader.id,
            replaces_attempt_id=old.id,
            info_hash="6" * 40,
            units=[[1, 20]],
            quality={"resolution": "2160p"},
            owned_by_movieclaw=True,
            hit_and_run=False,
            status=DownloadAttemptStatus.IMPORTED,
            last_progress_at=utcnow(),
        )
        session.add(new)
        await session.commit()
        old_id = old.id

    deleted: list[tuple[str, bool]] = []

    class FakeAdapter:
        async def get_torrent(self, info_hash, *, include_files=True):
            return TorrentStatus(
                info_hash=info_hash,
                name="Seeding",
                progress=1.0,
                completed=True,
                save_path="/downloads",
                files=[],
            )

        async def delete_torrent(self, info_hash, *, delete_files=False):
            deleted.append((info_hash, delete_files))

        async def close(self):
            return None

    monkeypatch.setattr(replacement_mod, "create_downloader", lambda config: FakeAdapter())
    assert await replacement_mod.reconcile_pending_cleanup(old_id)
    assert deleted == [], "洗版链路此前从未删过旧种，不能借这次修复顺手打开"
    async with db.session() as session:
        old = await session.get(SubscriptionDownloadAttempt, old_id)
        assert old.status == DownloadAttemptStatus.RETAINED
        assert "保留做种" in (old.cleanup_note or "")
