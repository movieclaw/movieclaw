"""入库规格核验测试（issue #242）：实测规格与种子声称不符时必须出声。

投递前只有种子名可信，入库后 ffprobe 已经拿到真实规格——"声称 1080p /
实测 540p"这个矛盾在对账那一刻是可判定的，不该静默。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.wanted_fulfillment import close_fulfilled_wanted
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    SystemNotice,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

# 种子名解析出来的声称规格（issue #242 实例：CMCTV 那个"1080p"发布）
_CLAIMED_1080P = {
    "resolution": "1080p",
    "media_source": "WEB-DL",
    "video_codec": "H.264",
    "release_group": "CMCTV",
}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'spec_audit.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(
    db,
    *,
    rule_spec: dict,
    claimed: dict | None = _CLAIMED_1080P,
    probe: dict,
    info_hash: str = "abc123",
):
    """建 库/条目/规则组/订阅/grabbed 工单 + 投递记录 + 入库文件（带实测列）。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/media/movie"]
        )
        item = MediaItem(
            kind="movie",
            tmdb_id=501,
            title="米切尔一家大战机器",
            original_title="The Mitchells vs. The Machines",
            year=2021,
        )
        rule_set = RuleSet(name="默认", spec=rule_spec)
        session.add_all([item, rule_set])
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
            info_hash=info_hash,
            grabbed_at=utcnow(),
        )
        session.add(wanted)
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub.id,
                info_hash=info_hash,
                units=[[0, 0]],
                quality=claimed or {},
                site_id="demo",
                torrent_id="t1",
                status=DownloadAttemptStatus.COMPLETED,
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                season_number=0,
                episode_number=0,
                file_path="/media/movie/米切尔一家大战机器 (2021)/米切尔一家大战机器 (2021).mkv",
                size_bytes=1_160_000_000,
                source=FileSource.IMPORTED,
                media_source="WEB-DL",
                release_group="CMCTV",
                **probe,
            )
        )
        await session.commit()
        return item.id, sub.id


async def _activities(session, subscription_id: int) -> list[SubscriptionActivity]:
    return list(
        (
            await session.execute(
                select(SubscriptionActivity).where(
                    SubscriptionActivity.subscription_id == subscription_id
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_measured_below_rule_set_raises_notice(db):
    """声称 1080p、实测 540p，且 540p 不在规则组白名单 → 活动 + 待处理告警。"""
    item_id, sub_id = await _seed(
        db,
        rule_spec={"resolutions": ["2160p", "1080p"]},
        probe={"resolution": "540p", "video_codec": "h264", "bit_rate": 1_360_000},
    )
    async with db.session() as session:
        assert await close_fulfilled_wanted(session, item_id) == 1

        mismatch = [a for a in await _activities(session, sub_id) if a.type == "spec_mismatch"]
        assert len(mismatch) == 1
        assert "540p" in mismatch[0].message and "1080p" in mismatch[0].message
        assert mismatch[0].payload["mismatches"] == [
            {"dimension": "分辨率", "claimed": "1080p", "measured": "540p"}
        ]

        notice = (
            await session.execute(
                select(SystemNotice).where(
                    SystemNotice.dedupe_key == f"subscription.spec_mismatch:{sub_id}:0:0"
                )
            )
        ).scalar_one_or_none()
        assert notice is not None
        assert "2160p/1080p" in notice.message


@pytest.mark.asyncio
async def test_mismatch_within_rule_set_only_records_activity(db):
    """实测与声称不符但仍满足规则组硬过滤 → 只记活动，不打扰用户。"""
    item_id, sub_id = await _seed(
        db,
        rule_spec={"resolutions": ["2160p", "1080p", "720p"]},
        probe={"resolution": "720p", "video_codec": "h264"},
    )
    async with db.session() as session:
        assert await close_fulfilled_wanted(session, item_id) == 1
        assert any(a.type == "spec_mismatch" for a in await _activities(session, sub_id))
        assert (await session.execute(select(SystemNotice))).scalars().all() == []


@pytest.mark.asyncio
async def test_codec_family_mismatch_is_reported(db):
    """声称 H.264、实测 hevc（不同编码族）→ 同样算货不对板。"""
    item_id, sub_id = await _seed(
        db,
        rule_spec={},
        probe={"resolution": "1080p", "video_codec": "hevc"},
    )
    async with db.session() as session:
        assert await close_fulfilled_wanted(session, item_id) == 1
        mismatch = [a for a in await _activities(session, sub_id) if a.type == "spec_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0].payload["mismatches"] == [
            {"dimension": "视频编码", "claimed": "H.264", "measured": "hevc"}
        ]


@pytest.mark.asyncio
async def test_matching_specs_stay_silent(db):
    """实测与声称一致（编码只是写法不同）→ 一个字都不该多说。"""
    item_id, sub_id = await _seed(
        db,
        rule_spec={"resolutions": ["2160p", "1080p"]},
        probe={"resolution": "1080p", "video_codec": "h264"},
    )
    async with db.session() as session:
        assert await close_fulfilled_wanted(session, item_id) == 1
        assert not any(a.type == "spec_mismatch" for a in await _activities(session, sub_id))
        assert (await session.execute(select(SystemNotice))).scalars().all() == []


@pytest.mark.asyncio
async def test_unknown_side_is_not_a_mismatch(db):
    """探测失败（无实测分辨率）或种子名没写规格 → 未知不当已知用，不报警。"""
    item_id, sub_id = await _seed(db, rule_spec={"resolutions": ["1080p"]}, probe={})
    async with db.session() as session:
        assert await close_fulfilled_wanted(session, item_id) == 1
        assert not any(a.type == "spec_mismatch" for a in await _activities(session, sub_id))
        assert (await session.execute(select(SystemNotice))).scalars().all() == []
