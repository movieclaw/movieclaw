"""播放质量指标测试（docs/design/web-player.md §8）。

重点是**北极星指标直通率**算得对，以及样本不足时不编数字——一个假的
「直通率 100%（1 次播放）」比没有数字更误导人。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.playback import metrics
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import PlaybackMetric


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def metric(**kwargs) -> PlaybackMetric:
    base = dict(member_id=0, library_file_id=1, tier=1, watched_ms=60_000)
    base.update(kwargs)
    return PlaybackMetric(**base)


async def seed(db, rows):
    async with db.session() as session:
        for row in rows:
            session.add(row)
        await session.commit()


async def test_empty_history_reports_no_numbers(db):
    """一条记录都没有时不能给 0——那会被读成「直通率 0%」。"""
    async with db.session() as session:
        stats = await metrics.summarize(session)
    assert stats.sessions == 0
    assert stats.direct_ratio is None
    assert stats.ttff_p50_ms is None
    assert stats.rebuffer_ratio is None


async def test_direct_ratio_counts_tier_0_and_1(db):
    """档 0 原文件直出与档 1 remux 都不重编码，合起来才是「直通」。"""
    await seed(db, [
        metric(tier=0), metric(tier=1), metric(tier=1),
        metric(tier=3), metric(tier=4),
    ])
    async with db.session() as session:
        stats = await metrics.summarize(session)
    assert stats.sessions == 5
    assert stats.direct_ratio == pytest.approx(0.6)
    assert stats.tier_counts == {0: 1, 1: 2, 3: 1, 4: 1}


async def test_degraded_ratio_measures_decision_mistakes(db):
    """降档次数是「决策引擎判错了多少」的直接度量。"""
    await seed(db, [metric(), metric(degraded_from=1), metric(degraded_from=2), metric()])
    async with db.session() as session:
        stats = await metrics.summarize(session)
    assert stats.degraded_ratio == pytest.approx(0.5)


async def test_rebuffer_ratio_uses_cta_2066_denominator(db):
    """卡顿率 = 卡顿时长 /（观看时长 + 卡顿时长），与 CTA-2066 同口径。"""
    await seed(db, [metric(watched_ms=90_000, rebuffer_ms=10_000)])
    async with db.session() as session:
        stats = await metrics.summarize(session)
    assert stats.rebuffer_ratio == pytest.approx(0.1)


async def test_ttff_percentiles_ignore_sessions_that_never_showed_a_frame(db):
    await seed(db, [
        metric(ttff_ms=100), metric(ttff_ms=200), metric(ttff_ms=300),
        metric(ttff_ms=None),  # 没出画的不该把 p50 拉低
    ])
    async with db.session() as session:
        stats = await metrics.summarize(session)
    assert stats.ttff_p50_ms == 200
    assert stats.ttff_p95_ms == 300


async def test_dropped_ratio_needs_frame_counts(db):
    await seed(db, [metric(dropped_frames=5, total_frames=1000)])
    async with db.session() as session:
        stats = await metrics.summarize(session)
    assert stats.dropped_ratio == pytest.approx(0.005)


async def test_dropped_ratio_is_none_without_frame_counts(db):
    """浏览器不给 getVideoPlaybackQuality 时不能算成 0% 掉帧。"""
    await seed(db, [metric(dropped_frames=None, total_frames=None)])
    async with db.session() as session:
        stats = await metrics.summarize(session)
    assert stats.dropped_ratio is None


async def test_summary_looks_at_recent_sessions_only(db):
    await seed(db, [metric(tier=4) for _ in range(10)] + [metric(tier=0) for _ in range(3)])
    async with db.session() as session:
        stats = await metrics.summarize(session, limit=3)
    assert stats.sessions == 3
    assert stats.direct_ratio == pytest.approx(1.0)  # 最近三次都是直通


async def test_purge_keeps_the_recent_tail(db):
    """指标是趋势数据不是台账——攒到几十万行只会拖慢 data 卷上的 SQLite。"""
    await seed(db, [metric() for _ in range(50)])
    async with db.session() as session:
        removed = await metrics.purge_older_than(session, keep=10)
        assert removed == 40
        assert await metrics.count(session) == 10


async def test_purge_under_the_threshold_is_a_noop(db):
    await seed(db, [metric() for _ in range(5)])
    async with db.session() as session:
        assert await metrics.purge_older_than(session, keep=10) == 0
        assert await metrics.count(session) == 5
