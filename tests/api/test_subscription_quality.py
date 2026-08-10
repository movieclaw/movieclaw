"""订阅连续性/洗版策略：仅依据种子标题属性，不探测实际文件。"""

from __future__ import annotations

from typing import Any, cast

import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.subscription.quality import (
    candidate_profile,
    profile_verdict,
    public_policy,
    target_profile_from_rule,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    MediaItem,
    RuleSet,
    Subscription,
    WantedItem,
    WantedStatus,
)
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import RuleSetSpec, TorrentCandidate


def _candidate(**attrs: Any) -> TorrentCandidate:
    return TorrentCandidate(
        site_id="test",
        torrent_id="1",
        title="Test.Show.S01E01.2160p.IQ.WEB-DL.H265.HDR-Pure@HDSWEB",
        subtitle="",
        attrs=TorrentAttrs.model_validate(attrs),
    )


def test_target_snapshot_keeps_only_stable_title_fields() -> None:
    profile = target_profile_from_rule(
        RuleSetSpec(
            resolutions=["2160p", "1080p"],
            video_codecs=["H265"],
            platforms_allow=["iqiyi"],
            release_groups_allow=["Pure@HDSWEB"],
            source_match_mode="all",
            hdr="require",
            dv="forbid",
            free_only=True,
            min_seeders=5,
            size_max_mb=4096,
        )
    )

    assert profile == {
        "resolutions": ["2160p"],
        "video_codecs": ["H265"],
        "platforms_allow": ["iqiyi"],
        "release_groups_allow": ["Pure@HDSWEB"],
        "source_match_mode": "all",
        "hdr": "require",
        "dv": "forbid",
    }


def test_candidate_profile_distinguishes_hdr_dv_and_sdr() -> None:
    common = {
        "resolution": "2160p",
        "video_codec": "H265",
        "platforms": ["iqiyi"],
        "release_group": "Pure@HDSWEB",
    }
    hdr = candidate_profile(_candidate(**common, hdr=["HDR10"]))
    dv = candidate_profile(_candidate(**common, hdr=["DV", "HDR10"]))
    sdr = candidate_profile(_candidate(**common, hdr=[]))

    assert hdr["hdr"] == "require" and hdr["dv"] == "forbid"
    assert dv["hdr"] == "require" and dv["dv"] == "require"
    assert sdr["hdr"] == "forbid" and "dv" not in sdr
    assert hdr["platforms_allow"] == ["iqiyi"]
    assert hdr["release_groups_allow"] == ["Pure@HDSWEB"]
    assert hdr["source_match_mode"] == "all"


def test_locked_source_requires_platform_and_release_group() -> None:
    profile = {
        "platforms_allow": ["iqiyi"],
        "release_groups_allow": ["Pure@HDSWEB"],
        "source_match_mode": "all",
        "hdr": "require",
        "dv": "forbid",
    }
    accepted = _candidate(
        platforms=["iqiyi"], release_group="Pure@HDSWEB", hdr=["HDR10"]
    )
    wrong_group = _candidate(
        platforms=["iqiyi"], release_group="HHWEB", hdr=["HDR10"]
    )
    contains_dv = _candidate(
        platforms=["iqiyi"], release_group="Pure@HDSWEB", hdr=["HDR10", "DV"]
    )

    assert profile_verdict(accepted, profile).accepted is True
    assert profile_verdict(wrong_group, profile).accepted is False
    assert profile_verdict(contains_dv, profile).reason_code == "dv_forbidden"
    assert profile_verdict(accepted, None).reason_code == "quality_profile_invalid"


def test_public_policy_hides_pending_dispatch_profiles() -> None:
    result = public_policy(
        {
            "mode": "upgrade",
            "target": {"resolutions": ["2160p"]},
            "pending": {"1:1": {"profile": {"resolutions": ["1080p"]}}},
        }
    )
    assert result == {"mode": "upgrade", "target": {"resolutions": ["2160p"]}}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'quality.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def test_enabling_tv_upgrade_requeues_only_one_imported_anchor(db) -> None:
    async with db.session() as session:
        item = MediaItem(kind="tv", tmdb_id=200, title="测试剧集", original_title="Test")
        base = RuleSet(name="基础", spec={})
        target = RuleSet(name="目标", spec={"resolutions": ["2160p"], "dv": "require"})
        session.add(item)
        session.add(base)
        session.add(target)
        await session.commit()
        await session.refresh(item)
        await session.refresh(base)
        await session.refresh(target)
        sub = Subscription(
            media_item_id=item.id,
            kind="tv",
            selected_seasons=[1],
            rule_set_id=base.id,
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add_all(
            [
                WantedItem(
                    subscription_id=sub.id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=episode,
                    status=WantedStatus.IMPORTED,
                )
                for episode in (1, 2)
            ]
        )
        await session.commit()

        service = SubscriptionService(session, cast(Any, None))
        await service.update(
            sub.id,
            quality_policy={"mode": "upgrade", "target_rule_set_id": target.id},
        )
        rows = list(
            (
                await session.execute(
                    select(WantedItem)
                    .where(WantedItem.subscription_id == sub.id)
                    .order_by(WantedItem.episode_number)
                )
            )
            .scalars()
            .all()
        )
        await session.refresh(sub)

    assert [row.status for row in rows] == [WantedStatus.UPGRADING, WantedStatus.IMPORTED]
    assert sub.quality_policy["anchor_unit"] == "1:1"
    assert sub.quality_policy["target"] == {"resolutions": ["2160p"], "dv": "require"}

    async with db.session() as session:
        service = SubscriptionService(session, cast(Any, None))
        # 无关调整不能在首个锚点仍开放时再创建第二个洗版工单。
        await service.update(sub.id, follow_future=False)
        rows = list(
            (
                await session.execute(
                    select(WantedItem).where(WantedItem.subscription_id == sub.id)
                )
            )
            .scalars()
            .all()
        )
        assert sum(row.status == WantedStatus.UPGRADING for row in rows) == 1

        # 关闭正在投递的洗版策略：已有版本仍算 imported，不会因下载失败变成缺集。
        anchor = next(row for row in rows if row.status == WantedStatus.UPGRADING)
        managed_sub = await session.get(Subscription, sub.id)
        anchor.status = WantedStatus.GRABBED
        anchor.info_hash = "upgrade123"
        managed_sub.quality_policy = {
            **managed_sub.quality_policy,
            "pending": {
                "1:1": {
                    "profile": {"resolutions": ["2160p"], "dv": "require"},
                    "meets_target": True,
                    "was_upgrading": True,
                }
            },
        }
        session.add(anchor)
        session.add(managed_sub)
        await session.commit()
        await service.update(sub.id, quality_policy=None)
        await session.refresh(anchor)
        await session.refresh(managed_sub)
        assert anchor.status == WantedStatus.IMPORTED
        assert managed_sub.quality_policy is None

        # 再开启后仍只选一个锚点。
        await service.update(
            sub.id,
            quality_policy={"mode": "upgrade", "target_rule_set_id": target.id},
        )

        # 取消该季只移除未投递洗版工单，已有 imported 内容保留。
        await service.update(sub.id, selected_seasons=[])
        rows = list(
            (
                await session.execute(
                    select(WantedItem).where(WantedItem.subscription_id == sub.id)
                )
            )
            .scalars()
            .all()
        )
        refreshed = await session.get(Subscription, sub.id)

    assert [row.status for row in rows] == [WantedStatus.IMPORTED]
    assert "anchor_unit" not in refreshed.quality_policy
