"""识别存疑的观察台账：把几条 shadow 判定的真实触发率读出来。

``identity-confidence.md`` §10.2 要求那几个拍脑袋的阈值先走 shadow，跑满观察期
后按触发率与误报率决定是否点灯。判定一直都在落库，但此前**全项目零读取点**，
灰度因此永远收敛不了——那条 113 MB 的假「正片」是用户自己发现的，而系统连着
两层都"看见"了。这些用例守的就是"出口真的通"。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.identity_audit import identity_audit
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ActivityType,
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db) -> tuple[int, int, int]:
    """建 库/条目/订阅 的最小闭包，返回 (library_id, item_id, sub_id)。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/media/movie"]
        )
        item = MediaItem(kind="movie", tmdb_id=42, title="恶人传", original_title="악인전")
        rule_set = RuleSet(name="默认", spec={})
        session.add_all([item, rule_set])
        await session.flush()
        sub = Subscription(media_item_id=item.id, kind="movie", rule_set_id=rule_set.id)
        session.add(sub)
        await session.commit()
        return library.id, item.id, sub.id


def _attempt(sub_id: int, info_hash: str, confidence: str | None, **kw):
    return SubscriptionDownloadAttempt(
        subscription_id=sub_id,
        info_hash=info_hash,
        identity_confidence=confidence,
        last_progress_at=utcnow(),
        **kw,
    )


async def test_empty_install_reports_zeroes_not_errors(db) -> None:
    """没有任何观察时给一份干净的零报告——灰度刚上线时这是常态。"""
    await _seed(db)
    async with db.session() as session:
        report = await identity_audit(session)
    assert report["window_days"] == 30
    assert report["confidence"] == {"dispatched": 0, "guess": 0, "ratio": 0.0}
    for key in ("bitrate_shadow", "absurd_rejected", "runtime_doubt"):
        assert report[key] == {"hits": 0, "samples": []}


async def test_confidence_ratio_is_the_headline_metric(db) -> None:
    """§10.4 点名的核心指标：只靠片名+年份认的投递占比。

    ``identity_confidence`` 为 NULL 的旧数据不进分母——把上线前的历史当成猜测
    会让这个指标从第一天起就被噪音污染。
    """
    _, _, sub_id = await _seed(db)
    async with db.session() as session:
        session.add_all(
            [
                _attempt(sub_id, "a" * 40, "exact_id"),
                _attempt(sub_id, "b" * 40, "title_year"),
                _attempt(sub_id, "c" * 40, "title_only"),
                _attempt(sub_id, "d" * 40, None),  # 特性上线前的旧数据
            ]
        )
        await session.commit()
        report = await identity_audit(session)
    assert report["confidence"] == {"dispatched": 3, "guess": 2, "ratio": round(2 / 3, 4)}


async def test_shadow_and_hard_reject_are_both_surfaced(db) -> None:
    """两档分开统计：可疑档只记录（要不要点灯看它），极端档已生效（看有没有过度开火）。"""
    _, _, sub_id = await _seed(db)
    async with db.session() as session:
        session.add_all(
            [
                SubscriptionActivity(
                    subscription_id=sub_id,
                    type=ActivityType.GRABBED,
                    message="已投递《恶人传》",
                    payload={
                        "site_id": "mt",
                        "torrent_id": "111",
                        "shadow": {"bitrate_reject": "体积与片长对不上：1.0 Mbps"},
                    },
                ),
                SubscriptionActivity(
                    subscription_id=sub_id,
                    type=ActivityType.GRABBED,
                    message="已投递《恶人传》",
                    payload={"site_id": "mt", "torrent_id": "222"},  # 正常体积不留记录
                ),
                SubscriptionActivity(
                    subscription_id=sub_id,
                    type=ActivityType.MATCH_REJECTED,
                    message="正片有候选被拒：体积离谱到不可能是正片……",
                    payload={
                        "site_id": "ssd",
                        "torrent_id": "333",
                        "reason_code": "size_absurd_for_runtime",
                    },
                ),
                SubscriptionActivity(
                    subscription_id=sub_id,
                    type=ActivityType.MATCH_REJECTED,
                    message="正片有候选被拒：分辨率不在允许范围",
                    payload={"reason_code": "resolution_not_allowed"},  # 与本台账无关
                ),
            ]
        )
        await session.commit()
        report = await identity_audit(session)

    assert report["bitrate_shadow"]["hits"] == 1
    shadow = report["bitrate_shadow"]["samples"][0]
    assert (shadow["site_id"], shadow["torrent_id"]) == ("mt", "111")
    assert "Mbps" in shadow["note"] and shadow["title"] == "恶人传"

    assert report["absurd_rejected"]["hits"] == 1
    assert report["absurd_rejected"]["samples"][0]["torrent_id"] == "333"


async def test_runtime_doubt_files_are_surfaced(db) -> None:
    """入库时长体检的台账此前零读取点——它正是这次事故第二层"看见了没说"。"""
    library_id, item_id, _ = await _seed(db)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=0,
                episode_number=0,
                file_path="/media/movie/恶人传 (2019)/恶人传.mkv",
                size_bytes=113 * 1024**2,
                source=FileSource.IMPORTED,
                site_id="ssd",
                torrent_id="333",
                identity_doubt={
                    "reason": "runtime_mismatch",
                    "expected_minutes": 110,
                    "actual_minutes": 5,
                },
            )
        )
        await session.commit()
        report = await identity_audit(session)
    assert report["runtime_doubt"]["hits"] == 1
    sample = report["runtime_doubt"]["samples"][0]
    assert "实测 5 分钟" in sample["note"] and "标注 110 分钟" in sample["note"]
    assert sample["title"] == "恶人传"


async def test_window_excludes_older_observations(db) -> None:
    """统计窗口是有意义的：点灯与否看的是"最近"的触发率，不是开服以来的总和。"""
    _, _, sub_id = await _seed(db)
    async with db.session() as session:
        old = _attempt(sub_id, "e" * 40, "title_year")
        old.created_at = utcnow() - timedelta(days=90)
        session.add(old)
        session.add(_attempt(sub_id, "f" * 40, "exact_id"))
        await session.commit()

        assert (await identity_audit(session, window_days=30))["confidence"]["dispatched"] == 1
        assert (await identity_audit(session, window_days=180))["confidence"]["dispatched"] == 2
