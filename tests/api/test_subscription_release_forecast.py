"""追新发布时间预测：存量观测推导、保守降级与站点同步规划。"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription import (
    next_forecast_probe_times_by_wanted,
    refresh_release_forecasts,
)
from movieclaw_api.services.torrent_sync import _plan_sync
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    AuthType,
    ConfigStatus,
    MediaEpisode,
    MediaItem,
    RuleSet,
    SiteCredential,
    SiteSyncCursor,
    SiteTorrent,
    Subscription,
    SubscriptionStatus,
    TorrentSource,
    WantedItem,
    WantedStatus,
    utcnow,
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'forecast.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed_target(
    session, *, cadence_days: int = 7, follow_future: bool = True
) -> tuple[WantedItem, datetime]:
    """建立 E1 已发布、E2 待追的最小周播/日播样本。"""
    today = utcnow().date()
    first_air = today - timedelta(days=1)
    target_air = first_air + timedelta(days=cadence_days)
    first_publish = datetime.combine(first_air, time(15, 35))

    rule = RuleSet(name=f"预测规则-{cadence_days}", is_default=True, spec={})
    item = MediaItem(
        kind="tv",
        tmdb_id=9000 + cadence_days,
        title="测试剧集",
        original_title="Test Show",
        year=today.year,
        aliases=["测试剧集", "Test Show"],
        status="Returning Series",
    )
    session.add_all([rule, item])
    await session.commit()
    await session.refresh(rule)
    await session.refresh(item)
    assert rule.id is not None and item.id is not None

    session.add_all(
        [
            MediaEpisode(
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                name="E1",
                air_date=first_air,
            ),
            MediaEpisode(
                media_item_id=item.id,
                season_number=1,
                episode_number=2,
                name="E2",
                air_date=target_air,
            ),
        ]
    )
    subscription = Subscription(
        media_item_id=item.id,
        kind="tv",
        selected_seasons=[],
        follow_future=follow_future,
        rule_set_id=rule.id,
        status=SubscriptionStatus.ACTIVE,
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    assert subscription.id is not None

    wanted = WantedItem(
        subscription_id=subscription.id,
        media_item_id=item.id,
        season_number=1,
        episode_number=2,
        status=WantedStatus.WANTED,
        air_date=target_air,
        next_search_at=datetime.combine(target_air, time()) + timedelta(hours=48),
    )
    torrent = SiteTorrent(
        site_id="site-a",
        torrent_id=f"episode-1-{cadence_days}",
        title="Test Show S01E01 1080p WEB-DL",
        attrs={
            "media_type": "tv",
            "year": today.year,
            "seasons": [1],
            "episodes": [1],
            "resolution": "1080p",
        },
        enrich_version=1,
        source=TorrentSource.LIST,
        publish_time=first_publish,
    )
    session.add_all([wanted, torrent])
    await session.commit()
    await session.refresh(wanted)
    return wanted, first_publish + timedelta(days=cadence_days)


@pytest.mark.parametrize("cadence_days", [1, 7])
async def test_e2_bootstrap_predicts_daily_and_weekly_release(db, cadence_days: int) -> None:
    """一个同季前序样本即可从 E2 启动，日播/周播由实际档期差自然决定。"""
    async with db.session() as session:
        wanted, expected = await _seed_target(session, cadence_days=cadence_days)
        changed = await refresh_release_forecasts(
            session, media_item_ids={wanted.media_item_id}
        )

        assert changed == 1
        assert wanted.release_forecast is not None
        forecast = wanted.release_forecast
        assert forecast["confidence"] == "bootstrap"
        assert forecast["sample_count"] == 1
        assert forecast["cadence_days"] == cadence_days
        assert datetime.fromisoformat(forecast["predicted_at"]).astimezone(UTC).replace(
            tzinfo=None
        ) == expected
        assert forecast["basis_units"] == [[1, 1]]
        assert forecast["sites"][0]["site_id"] == "site-a"
        assert len(forecast["sites"][0]["probe_times"]) == 3


async def test_selected_episode_forecast_ignores_follow_future(db) -> None:
    """自动续订关闭后，已在当前范围内的待播集仍须生成预测。"""
    async with db.session() as session:
        wanted, _expected = await _seed_target(session, follow_future=False)
        changed = await refresh_release_forecasts(
            session, media_item_ids={wanted.media_item_id}
        )

        assert changed == 1
        assert wanted.release_forecast is not None


async def test_displayed_probe_uses_scheduler_courtesy_time(db) -> None:
    """首页返回的探测点必须与调度器应用 15 分钟礼貌间隔后的时间一致。"""
    async with db.session() as session:
        wanted, _expected = await _seed_target(session, follow_future=False)
        now = utcnow()
        last_sync_at = now - timedelta(minutes=5)
        raw_probe = now + timedelta(minutes=1)
        expected_probe = last_sync_at + timedelta(minutes=15)
        wanted.release_forecast = {
            "version": 1,
            "target_air_date": wanted.air_date.isoformat(),
            "confidence": "bootstrap",
            "window_end": (now + timedelta(hours=1)).replace(tzinfo=UTC).isoformat(),
            "sites": [
                {
                    "site_id": "site-a",
                    "probe_times": [raw_probe.replace(tzinfo=UTC).isoformat()],
                }
            ],
        }
        session.add_all(
            [
                wanted,
                SiteCredential(
                    site_id="site-a",
                    auth_type=AuthType.COOKIE,
                    cookie="session=test",
                    enabled=True,
                    status=ConfigStatus.ACTIVE,
                ),
                SiteSyncCursor(
                    site_id="site-a",
                    tracking_since=now - timedelta(days=1),
                    last_sync_at=last_sync_at,
                    next_sync_at=now + timedelta(hours=6),
                ),
            ]
        )
        await session.commit()

        probe_times = await next_forecast_probe_times_by_wanted(
            session, wanted_items=[wanted]
        )

        assert probe_times[wanted.id] == expected_probe


async def test_pack_is_not_used_as_single_episode_observation(db) -> None:
    """整季包发布时间无法代表某一集，必须排除，避免污染下一集预测。"""
    async with db.session() as session:
        wanted, _expected = await _seed_target(session)
        torrent = (await session.execute(select(SiteTorrent))).scalar_one()
        assert torrent is not None
        torrent.attrs = {
            "media_type": "tv",
            "year": utcnow().year,
            "seasons": [1],
            "complete": True,
            "resolution": "1080p",
        }
        session.add(torrent)
        await session.commit()

        changed = await refresh_release_forecasts(
            session, media_item_ids={wanted.media_item_id}
        )
        assert changed == 0
        assert wanted.release_forecast is None


async def test_prediction_probe_advances_existing_site_sync(db) -> None:
    """预测不受自动续订影响；普通同步消费探测点，暂停后探测立即失效。"""
    async with db.session() as session:
        wanted, _expected = await _seed_target(session, follow_future=False)
        now = utcnow()
        wanted.release_forecast = {
            "version": 1,
            "target_air_date": wanted.air_date.isoformat(),
            "confidence": "bootstrap",
            "window_end": (now + timedelta(hours=1)).replace(tzinfo=UTC).isoformat(),
            "sites": [
                {
                    "site_id": "site-disabled",
                    "probe_times": [
                        (now - timedelta(minutes=2)).replace(tzinfo=UTC).isoformat()
                    ],
                },
                {
                    "site_id": "site-a",
                    "probe_times": [
                        (now - timedelta(minutes=1)).replace(tzinfo=UTC).isoformat()
                    ],
                }
            ],
        }
        cursor = SiteSyncCursor(
            site_id="site-a",
            tracking_since=now - timedelta(days=1),
            last_sync_at=now - timedelta(hours=1),
            next_sync_at=now + timedelta(hours=6),
        )
        session.add_all([wanted, cursor])
        await session.commit()

    site = SimpleNamespace(site_id="site-a")
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == [site]

    async with db.session() as session:
        cursor = (
            await session.execute(
                select(SiteSyncCursor).where(SiteSyncCursor.site_id == "site-a")
            )
        ).scalar_one()
        cursor.last_sync_at = now - timedelta(minutes=5)
        session.add(cursor)
        await session.commit()
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == []  # 探测点虽已到，但距上次同步不足 15 分钟，先遵守礼貌间隔

    async with db.session() as session:
        cursor = (
            await session.execute(
                select(SiteSyncCursor).where(SiteSyncCursor.site_id == "site-a")
            )
        ).scalar_one()
        cursor.last_sync_at = utcnow()
        session.add(cursor)
        await session.commit()
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == []  # 普通同步晚于探测点，同样视为已经消费

    async with db.session() as session:
        cursor = (
            await session.execute(
                select(SiteSyncCursor).where(SiteSyncCursor.site_id == "site-a")
            )
        ).scalar_one()
        subscription = (await session.execute(select(Subscription))).scalar_one()
        cursor.last_sync_at = now - timedelta(hours=1)
        subscription.status = SubscriptionStatus.PAUSED
        session.add_all([cursor, subscription])
        await session.commit()
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == []


# ---------------------------------------------------------------------------
# 后台刷新（refresh_release_forecasts_soon）
# ---------------------------------------------------------------------------
# conftest 为免悬空任务把它全局打桩成空函数；这里在收集阶段就拿到真实实现，
# 专门验证它自开会话、合并排队与删除竞态下的行为。
from movieclaw_api.services.subscription import release_forecast  # noqa: E402

_real_refresh_soon = release_forecast.refresh_release_forecasts_soon


@pytest.fixture
def fresh_refresh_state(monkeypatch):
    """每个用例用独立的排队状态与锁，避免跨用例（跨事件循环）串味。"""
    import asyncio

    monkeypatch.setattr(release_forecast, "_queued_ids", set())
    monkeypatch.setattr(release_forecast, "_running_ids", set())
    monkeypatch.setattr(release_forecast, "_refresh_lock", asyncio.Lock())


async def _drain_background_refreshes() -> None:
    import asyncio

    while release_forecast._refresh_tasks:
        await asyncio.gather(*list(release_forecast._refresh_tasks))


async def test_background_refresh_coalesces_and_reports_pending(
    db, monkeypatch, fresh_refresh_state
) -> None:
    """同一条目排队中重复触发只跑一次；排队到跑完之间对外报告「刷新中」。"""
    async with db.session() as session:
        wanted, _ = await _seed_target(session, cadence_days=7)
    media_item_id = wanted.media_item_id

    calls: list[set[int]] = []
    real_refresh = release_forecast.refresh_release_forecasts

    async def spy(session, *, media_item_ids=None):
        calls.append(set(media_item_ids or ()))
        return await real_refresh(session, media_item_ids=media_item_ids)

    monkeypatch.setattr(release_forecast, "refresh_release_forecasts", spy)

    assert not release_forecast.forecast_refresh_pending(media_item_id)
    _real_refresh_soon({media_item_id})
    _real_refresh_soon({media_item_id})
    assert release_forecast.forecast_refresh_pending(media_item_id)

    await _drain_background_refreshes()

    assert calls == [{media_item_id}]
    assert not release_forecast.forecast_refresh_pending(media_item_id)
    async with db.session() as session:
        stored = await session.get(WantedItem, wanted.id)
        assert stored is not None and stored.release_forecast is not None


async def test_background_refresh_survives_subscription_deleted_mid_refresh(
    db, monkeypatch, fresh_refresh_state, caplog
) -> None:
    """刷新途中有订阅被删（订阅后立刻取消）：不报警告，同批其他条目照常出预测。"""
    import sqlite3

    async with db.session() as session:
        doomed, _ = await _seed_target(session, cadence_days=1)
        kept, _ = await _seed_target(session, cadence_days=7)
    db_path = get_settings().database_url.removeprefix("sqlite+aiosqlite:///")

    real_observations = release_forecast._observations_for_item
    deleted: list[int] = []

    def delete_then_observe(**kwargs):
        # 在工单已装载、尚未提交的窗口里用另一条连接删订阅，模拟用户秒退
        if not deleted:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM subscription WHERE id = ?", (doomed.subscription_id,))
            conn.commit()
            conn.close()
            deleted.append(doomed.subscription_id)
        return real_observations(**kwargs)

    monkeypatch.setattr(release_forecast, "_observations_for_item", delete_then_observe)

    with caplog.at_level("INFO", logger="movieclaw_api.subscription.release_forecast"):
        _real_refresh_soon({doomed.media_item_id, kept.media_item_id})
        await _drain_background_refreshes()

    assert deleted, "竞态注入没有生效"
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    async with db.session() as session:
        assert await session.get(WantedItem, doomed.id) is None
        survivor = await session.get(WantedItem, kept.id)
        assert survivor is not None and survivor.release_forecast is not None
