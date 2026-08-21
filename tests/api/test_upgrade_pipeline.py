"""洗版决策接线测试（quality-upgrade.md §5/§6）：上下文加载、判定链、
整季包铁律、在途去重、证伪排除、调度排期。全部走 dry-run 投递。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.matching import (
    UPGRADE_PRIORITY,
    evaluate_and_dispatch,
    load_match_context,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
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

_SPEC_UPGRADE = {"upgrade_source": "remux"}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pipe.db'}")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "true")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(
    db,
    *,
    spec=None,
    quality=None,
    wanted_status=WantedStatus.IMPORTED,
    units=((1, 1),),
    tmdb_id=200,
):
    """条目/订阅/工单最小闭包。quality 应用到全部单元。"""
    async with db.session() as session:
        item = MediaItem(
            kind="tv",
            tmdb_id=tmdb_id,
            title="测试剧集",
            original_title="Testshow",
            year=2024,
            aliases=["Testshow", "测试剧集"],
        )
        rule_set = RuleSet(name=f"默认-{tmdb_id}", spec=spec or _SPEC_UPGRADE)
        session.add_all([item, rule_set])
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(media_item_id=item.id, kind="tv", rule_set_id=rule_set.id)
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        ids = []
        for season, episode in units:
            wanted = WantedItem(
                subscription_id=sub.id,
                media_item_id=item.id,
                season_number=season,
                episode_number=episode,
                status=wanted_status,
                quality=quality,
                imported_at=utcnow() if wanted_status == WantedStatus.IMPORTED else None,
            )
            session.add(wanted)
            await session.commit()
            await session.refresh(wanted)
            ids.append(wanted.id)
        return item.id, sub.id, ids


def _torrent(title, *, attrs, torrent_id="t1", seeders=10, is_free=None):
    return SiteTorrent(
        site_id="site-a",
        torrent_id=torrent_id,
        title=title,
        attrs=attrs,
        enrich_version=1,
        source=TorrentSource.LIST,
        seeders=seeders,
        is_free=is_free,
        publish_time=utcnow(),
    )


_WEBDL = {"resolution": "1080p", "media_source": "WEB-DL"}
_REMUX_E1 = {
    "resolution": "1080p",
    "media_source": "Blu-ray",
    "remux": True,
    "seasons": [1],
    "episodes": [1],
}


@pytest.mark.asyncio
async def test_context_loads_upgrade_units(db):
    """已入库、快照低于目标的单元进入洗版上下文；无缺口也能形成上下文。"""
    item_id, _sub_id, _ids = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        contexts = await load_match_context(session)
        assert item_id in contexts
        ctx = contexts[item_id]
        assert not ctx.open_wanted
        assert (1, 1) in ctx.upgrade_wanted
        assert ctx.upgrade_snapshots[(1, 1)].media_source == "WEB-DL"


@pytest.mark.asyncio
async def test_context_skips_at_cutoff_and_null_snapshot(db):
    """到顶的单元与无快照（{} 哨兵）的单元不进洗版上下文。"""
    item_id, _sub, _ = await _seed(
        db,
        quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        units=((1, 1),),
    )
    async with db.session() as session:
        assert item_id not in await load_match_context(session)

    item2 = await _seed(db, quality={}, units=((1, 2),), tmdb_id=201)
    async with db.session() as session:
        assert item2[0] not in await load_match_context(session)


@pytest.mark.asyncio
async def test_upgrade_dispatches_better_candidate(db):
    """WEB-DL 已入库 + Remux 新种子 → 洗版投递：UPGRADE_GRABBED 活动、
    工单保持 imported、info_hash 不动。"""
    item_id, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01E01 1080p Blu-ray REMUX", attrs=_REMUX_E1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.IMPORTED  # 不重开工单
        assert wanted.info_hash is None  # 指向旧版本的关联不动
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id,
                        SubscriptionActivity.type == "upgrade_grabbed",
                    )
                )
            ).scalars()
        )
        assert len(activities) == 1
        assert "1080p Remux" in activities[0].message
        assert "当前 1080p WEB-DL" in activities[0].message


@pytest.mark.asyncio
async def test_upgrade_rejects_not_better_and_at_cutoff_silently(db):
    """同档/降档候选不投递也不刷活动（噪音控制）。"""
    item_id, sub_id, _ = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        row = _torrent(
            "Testshow 2024 S01E01 1080p WEB-DL",
            attrs={**_WEBDL, "seasons": [1], "episodes": [1]},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id
                    )
                )
            ).scalars()
        )
        assert activities == []


@pytest.mark.asyncio
async def test_upgrade_picks_higher_tier_over_cheaper_candidate(db):
    """洗版选优按档位而非评分：免费高做种的 WEB-DL 不该压过非免费的 Remux。

    评分里免费 +100、做种封顶 +50，都远大于档位差，按评分选会先抓 WEB-DL、
    下一轮再洗 Remux —— 同一个单元付两次下载（quality-upgrade.md §15.4）。
    """
    _item_id, _sub_id, _ = await _seed(
        db, quality={"resolution": "1080p", "media_source": "WEBRip"}
    )
    async with db.session() as session:
        cheap = _torrent(
            "Testshow 2024 S01E01 1080p WEB-DL",
            attrs={
                "resolution": "1080p",
                "media_source": "WEB-DL",
                "seasons": [1],
                "episodes": [1],
            },
            torrent_id="cheap",
            seeders=99,
            is_free=True,
        )
        better = _torrent(
            "Testshow 2024 S01E01 1080p Blu-ray REMUX",
            attrs=_REMUX_E1,
            torrent_id="better",
            seeders=1,
        )
        session.add_all([cheap, better])
        await session.commit()
        await session.refresh(cheap)
        await session.refresh(better)
        summary = await evaluate_and_dispatch(session, [cheap, better], source="被动匹配")
        assert summary.dispatched_units == 1
        assert summary.dispatched_torrents == ["site-a/better"]


@pytest.mark.asyncio
async def test_both_sides_unknown_source_rejects_silently(db):
    """双方片源都未标注 = 末位平局，不构成升级且不写活动（§14.4）。

    只有分辨率可证明低于目标的单元才进洗版上下文，所以这条在生产里可达。
    修复前它按 upgrade_not_comparable 记 MATCH_REJECTED——而"两边都没标片源"
    是命名习惯的常态，不是用户能改善的数据质量问题，记活动只会刷屏。
    """
    _item_id, sub_id, _ = await _seed(db, quality={"resolution": "720p"})
    async with db.session() as session:
        row = _torrent(
            "Testshow 2024 S01E01 720p",
            attrs={"resolution": "720p", "seasons": [1], "episodes": [1]},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id
                    )
                )
            ).scalars()
        )
        assert activities == []


_PACK_REMUX = {
    "resolution": "1080p",
    "media_source": "Blu-ray",
    "remux": True,
    "seasons": [1],
    "complete": True,
}


async def _seed_pack_units(
    db, sub_id, item_id, episodes_quality: dict[int, dict], *, status=WantedStatus.IMPORTED
):
    """给整季包测试造带播出日期的单元（air_date 已过，包才可覆盖）。

    ``status`` 默认 imported（洗版单元）；传 WANTED 可造同季的缺口单元。
    """
    from datetime import date, timedelta

    aired = date.today() - timedelta(days=30)
    imported = status == WantedStatus.IMPORTED
    async with db.session() as session:
        for episode, quality in episodes_quality.items():
            session.add(
                WantedItem(
                    subscription_id=sub_id,
                    media_item_id=item_id,
                    season_number=1,
                    episode_number=episode,
                    status=status,
                    quality=quality,
                    air_date=aired,
                    imported_at=utcnow() if imported else None,
                )
            )
        await session.commit()


_PACK_WEBDL = {
    "resolution": "1080p",
    "media_source": "WEB-DL",
    "seasons": [1],
    "complete": True,
}


@pytest.mark.asyncio
async def test_gap_selection_still_prefers_free_candidate(db):
    """缺口选优不受洗版档位排序影响：凡覆盖缺口的候选一律按评分排。

    构造：S01E01 缺口 + S01E02 可洗（基线 WEB-DL）。两个整季包竞争——
    Remux 包非免费（既能填缺口又能洗 E02），WEB-DL 免费包对 E02 不构成升级
    （铁律否决洗版侧），只能填缺口。缺口必须仍然走免费包：免费优先是 PT
    场景的既有决策（`rules._FREE_SCORE`），洗版侧的排序调整不得顺带改掉它。
    """
    item_id, sub_id, _ = await _seed(db, wanted_status=WantedStatus.WANTED, units=())
    await _seed_pack_units(db, sub_id, item_id, {2: _WEBDL})
    await _seed_pack_units(db, sub_id, item_id, {1: None}, status=WantedStatus.WANTED)
    async with db.session() as session:
        free_pack = _torrent(
            "Testshow 2024 S01 1080p WEB-DL Complete",
            attrs=_PACK_WEBDL,
            torrent_id="free-pack",
            seeders=99,
            is_free=True,
        )
        remux_pack = _torrent(
            "Testshow 2024 S01 1080p Blu-ray REMUX Complete",
            attrs=_PACK_REMUX,
            torrent_id="remux-pack",
            seeders=1,
        )
        session.add_all([free_pack, remux_pack])
        await session.commit()
        await session.refresh(free_pack)
        await session.refresh(remux_pack)
        summary = await evaluate_and_dispatch(
            session, [free_pack, remux_pack], source="被动匹配"
        )
        # 免费包先投并拿走缺口，Remux 包随后只承担 E02 的洗版
        assert summary.dispatched_torrents == ["site-a/free-pack", "site-a/remux-pack"]
        assert summary.dispatched_units == 2


@pytest.mark.asyncio
async def test_pack_dispatches_when_all_units_washable(db):
    """正向对照：包覆盖的单元全部可洗 → 整季包投递（防止铁律误伤）。"""
    item_id, sub_id, _ = await _seed(db, quality=None, units=())
    await _seed_pack_units(db, sub_id, item_id, {1: _WEBDL, 2: _WEBDL})
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01 1080p Blu-ray REMUX Complete", attrs=_PACK_REMUX)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 2


@pytest.mark.asyncio
async def test_pack_vetoed_by_at_cutoff_sibling(db):
    """整季包铁律：E01 可洗但 E02 已到顶（Remux）→ 抓包等于把 E02 重下一遍，
    整体放弃洗版维度。"""
    item_id, sub_id, _ = await _seed(db, quality=None, units=())
    await _seed_pack_units(
        db, sub_id, item_id,
        {1: _WEBDL, 2: {"resolution": "1080p", "media_source": "Blu-ray", "remux": True}},
    )
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01 1080p Blu-ray REMUX Complete", attrs=_PACK_REMUX)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0


@pytest.mark.asyncio
async def test_pack_vetoed_by_incomparable_sibling(db):
    """整季包铁律：E03 快照片源未知（不可比，不在可洗集合）同样阻挡整包。"""
    item_id, sub_id, _ = await _seed(db, quality=None, units=())
    await _seed_pack_units(db, sub_id, item_id, {1: _WEBDL, 3: {"resolution": "1080p"}})
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01 1080p Blu-ray REMUX Complete", attrs=_PACK_REMUX)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0


@pytest.mark.asyncio
async def test_in_flight_upgrade_attempt_dedupes(db):
    """已有在途洗版 attempt 的单元本轮不再比对。"""
    item_id, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="ffff",
                units=[[1, 1]],
                quality=_REMUX_E1,
                purpose="upgrade",
                status=DownloadAttemptStatus.ACTIVE,
                last_progress_at=utcnow(),
            )
        )
        await session.commit()
        contexts = await load_match_context(session)
        assert item_id not in contexts  # 唯一单元在途，上下文剔空


@pytest.mark.asyncio
async def test_failed_upgrade_attempt_excludes_candidate(db):
    """证伪排除：FAILED 洗版 attempt 的 (site, torrent) 不会被再次投递。"""
    item_id, sub_id, _ = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="eeee",
                site_id="site-a",
                torrent_id="fake1",
                units=[[1, 1]],
                quality=_REMUX_E1,
                purpose="upgrade",
                status=DownloadAttemptStatus.FAILED,
                last_progress_at=utcnow(),
            )
        )
        row = _torrent(
            "Testshow 2024 S01E01 1080p Blu-ray REMUX", attrs=_REMUX_E1, torrent_id="fake1"
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0


@pytest.mark.asyncio
async def test_fused_unit_rests_until_cooldown(db):
    """连续证伪熔断：failures 达阈值且冷却未到 → 不参与；冷却已到 → 恢复。"""
    from datetime import timedelta

    item_id, _sub, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.upgrade_verify_failures = 3
        wanted.next_search_at = utcnow() + timedelta(days=20)  # 冷却中
        await session.commit()
        assert item_id not in await load_match_context(session)

        wanted = await session.get(WantedItem, wanted_id)
        wanted.next_search_at = utcnow() - timedelta(minutes=1)  # 冷却到期
        await session.commit()
        contexts = await load_match_context(session)
        assert (1, 1) in contexts[item_id].upgrade_wanted


@pytest.mark.asyncio
async def test_arming_and_search_now(db):
    """排期：arm 后 priority=-10、next_search_at 在 24h 错峰窗内；
    立即搜索把可洗单元重置到现在。"""
    from movieclaw_api.services.subscription.upgrade import (
        arm_upgrade_candidates,
        reset_upgrade_search_now,
    )

    _item, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        armed = await arm_upgrade_candidates(session, [wanted])
        await session.commit()
        assert armed == 1
        assert wanted.priority == UPGRADE_PRIORITY
        assert wanted.next_search_at is not None

        reset = await reset_upgrade_search_now(session, sub_id)
        await session.commit()
        assert reset == 1
        assert (utcnow() - wanted.next_search_at).total_seconds() < 5


@pytest.mark.asyncio
async def test_postpone_resets_expired_fuse_counter(db):
    """熔断冷却到期后第一次搜索记账：计数清零重新观察——否则常规退避会被
    误判成仍在冷却，把被动匹配无限期关掉。"""
    from datetime import timedelta

    from movieclaw_api.services.subscription.upgrade import postpone_upgrade_wanted

    item_id, _sub, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.upgrade_verify_failures = 3
        wanted.next_search_at = utcnow() - timedelta(minutes=1)  # 冷却已到期
        await session.commit()
        await postpone_upgrade_wanted(session, item_id, delay=None, count_attempt=True)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.upgrade_verify_failures == 0
        assert wanted.next_search_at is not None  # 已按洗版退避重新排期


@pytest.mark.asyncio
async def test_import_clears_stale_gap_schedule(db):
    """入库对账把缺口时代的 next_search_at 清空——否则 imported 单元会带着
    旧排期进入洗版搜索队列，触发无谓的站点搜索。"""
    from datetime import timedelta

    from movieclaw_api.services.subscription.wanted_fulfillment import (
        close_fulfilled_wanted,
    )
    from movieclaw_db.models import FileSource, LibraryFile
    from movieclaw_db.repositories.library_repo import LibraryRepository

    item_id, sub_id, (wanted_id,) = await _seed(
        db, quality=None, wanted_status=WantedStatus.GRABBED
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        wanted = await session.get(WantedItem, wanted_id)
        wanted.next_search_at = utcnow() + timedelta(hours=4)  # 缺口时代的退避
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/e1.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
                resolution="1080p",
                media_source="Blu-ray",
                bit_rate=9_000_000,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.IMPORTED
        # 旧排期已清；随后 arm 只对可洗单元重挂（本例 Blu-ray 未到 Remux，
        # 规则组开了洗版 → 被重新排期为洗版搜索，语义正确）
        assert wanted.quality["media_source"] == "Blu-ray"


@pytest.mark.asyncio
async def test_completed_subscription_still_upgrades(db):
    """洗版的主场景是"已收齐"（completed）订阅：上下文加载与搜索队列都
    不得把它排除（paused 才停，quality-upgrade.md §6.3）。"""
    from movieclaw_api.services.subscription.wanted_search import _due_media_groups

    item_id, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        sub = await session.get(Subscription, sub_id)
        sub.status = "completed"
        wanted = await session.get(WantedItem, wanted_id)
        wanted.next_search_at = utcnow()  # 已排期到期
        await session.commit()
        contexts = await load_match_context(session)
        assert item_id in contexts and (1, 1) in contexts[item_id].upgrade_wanted
        assert item_id in await _due_media_groups(session)

        # 暂停则真正停
        sub = await session.get(Subscription, sub_id)
        sub.status = "paused"
        await session.commit()
        assert item_id not in await load_match_context(session)
        assert item_id not in await _due_media_groups(session)


@pytest.mark.asyncio
async def test_search_now_defuses_fused_unit(db):
    """「立即搜索」是熔断提示要求的人工介入：解除熔断、清零计数、立即排期。"""
    from movieclaw_api.services.subscription.upgrade import reset_upgrade_search_now

    _item, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        from datetime import timedelta

        wanted = await session.get(WantedItem, wanted_id)
        wanted.upgrade_verify_failures = 3
        wanted.next_search_at = utcnow() + timedelta(days=20)  # 冷却中
        await session.commit()
        reset = await reset_upgrade_search_now(session, sub_id)
        await session.commit()
        assert reset == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.upgrade_verify_failures == 0
        assert (utcnow() - wanted.next_search_at).total_seconds() < 5
