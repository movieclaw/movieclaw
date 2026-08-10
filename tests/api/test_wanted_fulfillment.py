"""「订阅止于投递」两半的测试：库存对账关闭工单 + 投递救援巡检。

订阅不再亲自跟踪完成与搬运：工单完成状态由 library_file 库存对账推导
（任何入库路径都能关闭工单）；订阅只照看投递结果的死活（种子被删/卡死
→ 退回重新找资源）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.download_progress as progress_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.wanted_fulfillment import close_fulfilled_wanted
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionActivity,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'fulfill.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(
    db,
    *,
    wanted_status=WantedStatus.GRABBED,
    info_hash="abc123",
    grabbed_at=None,
    quality_policy=None,
    kind="tv",
):
    """建 库/条目/订阅/工单 的最小闭包，返回 (library_id, item_id, sub_id, wanted_id)。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库" if kind == "tv" else "电影库",
            kind=kind,
            root_paths=["/media/tv" if kind == "tv" else "/media/movies"],
        )
        item = MediaItem(
            kind=kind,
            tmdb_id=200 if kind == "tv" else 100,
            title="测试剧集" if kind == "tv" else "测试电影",
            original_title="Test",
            year=2024,
        )
        rule_set = RuleSet(name="默认", spec={})
        session.add(item)
        session.add(rule_set)
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id,
            kind=kind,
            rule_set_id=rule_set.id,
            library_id=library.id,
            quality_policy=quality_policy,
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        wanted = WantedItem(
            subscription_id=sub.id,
            media_item_id=item.id,
            season_number=1 if kind == "tv" else 0,
            episode_number=1 if kind == "tv" else 0,
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
async def test_first_import_locks_title_profile_for_future_episodes(db):
    """首次成功入库后保存平台、制作组、分辨率及 HDR 档案。"""
    profile = {
        "resolutions": ["2160p"],
        "platforms_allow": ["iqiyi"],
        "release_groups_allow": ["Pure@HDSWEB"],
        "source_match_mode": "all",
        "hdr": "require",
        "dv": "forbid",
    }
    policy = {
        "mode": "lock_first",
        "pending": {
            "1:1": {
                "profile": profile,
                "meets_target": False,
                "was_upgrading": False,
            }
        },
    }
    library_id, item_id, sub_id, wanted_id = await _seed(db, quality_policy=policy)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/Test/Season 01/Test - S01E01.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()

        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        sub = await session.get(Subscription, sub_id)

    assert wanted.status == WantedStatus.IMPORTED
    assert sub.quality_policy["locked"] == profile
    assert "pending" not in sub.quality_policy


@pytest.mark.asyncio
async def test_tv_upgrade_keeps_one_anchor_open_until_target_imports(db):
    """低版本先入库后只洗一个锚点；目标标题版本入库后固定后续。"""
    target = {
        "resolutions": ["2160p"],
        "platforms_allow": ["iqiyi"],
        "hdr": "require",
        "dv": "forbid",
    }
    policy = {
        "mode": "upgrade",
        "target": target,
        "pending": {
            "1:1": {
                "profile": {"resolutions": ["1080p"], "hdr": "forbid"},
                "meets_target": False,
                "was_upgrading": False,
            }
        },
    }
    library_id, item_id, sub_id, wanted_id = await _seed(db, quality_policy=policy)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/Test/Season 01/Test - S01E01.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()

        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        sub = await session.get(Subscription, sub_id)
        assert wanted.status == WantedStatus.UPGRADING
        assert sub.quality_policy["anchor_unit"] == "1:1"

        wanted.status = WantedStatus.GRABBED
        wanted.info_hash = "target123"
        sub.quality_policy = {
            **sub.quality_policy,
            "pending": {
                "1:1": {
                    "profile": target,
                    "meets_target": True,
                    "was_upgrading": True,
                }
            },
        }
        session.add(wanted)
        session.add(sub)
        await session.commit()

        assert await close_fulfilled_wanted(session, item_id) == 1
        await session.refresh(wanted)
        await session.refresh(sub)

    assert wanted.status == WantedStatus.IMPORTED
    assert sub.quality_policy["locked"] == target
    assert "anchor_unit" not in sub.quality_policy
    assert "pending" not in sub.quality_policy


@pytest.mark.asyncio
async def test_movie_keeps_upgrading_until_target_title_version_imports(db):
    """电影基础版先可用，目标标题版真正入库后才结束自动洗版。"""
    target = {"resolutions": ["2160p"], "hdr": "require", "dv": "forbid"}
    policy = {
        "mode": "upgrade",
        "target": target,
        "pending": {
            "0:0": {
                "profile": {"resolutions": ["1080p"], "hdr": "forbid"},
                "meets_target": False,
                "was_upgrading": False,
            }
        },
    }
    library_id, item_id, sub_id, wanted_id = await _seed(
        db, quality_policy=policy, kind="movie"
    )
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=0,
                episode_number=0,
                file_path="/media/movies/Test (2024)/Test (2024).mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()

        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        sub = await session.get(Subscription, sub_id)
        assert wanted.status == WantedStatus.UPGRADING

        wanted.status = WantedStatus.GRABBED
        sub.quality_policy = {
            **sub.quality_policy,
            "pending": {
                "0:0": {
                    "profile": target,
                    "meets_target": True,
                    "was_upgrading": True,
                }
            },
        }
        session.add(wanted)
        session.add(sub)
        await session.commit()

        assert await close_fulfilled_wanted(session, item_id) == 1
        await session.refresh(wanted)
        await session.refresh(sub)

    assert wanted.status == WantedStatus.IMPORTED
    assert "pending" not in sub.quality_policy


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
async def test_rescue_requeues_missing_torrent(db, monkeypatch):
    """救援巡检：种子在下载器中消失 → 工单退回 wanted 并记活动。"""
    _library_id, _item_id, sub_id, wanted_id = await _seed(db)

    async def query_none(info_hash, downloaders):
        return None

    monkeypatch.setattr(progress_mod, "_query_torrent", query_none)
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.WANTED
        assert wanted.info_hash is None
        assert wanted.next_search_at is not None
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
        assert any("不在下载器" in a.message for a in activities)


@pytest.mark.asyncio
async def test_rescue_restores_upgrade_state_and_clears_pending(db, monkeypatch):
    """洗版下载消失后退回 upgrading，不能退化成普通缺集。"""
    policy = {
        "mode": "upgrade",
        "target": {"resolutions": ["2160p"]},
        "anchor_unit": "1:1",
        "pending": {
            "1:1": {
                "profile": {"resolutions": ["2160p"]},
                "meets_target": True,
                "was_upgrading": True,
            }
        },
    }
    _library_id, _item_id, sub_id, wanted_id = await _seed(db, quality_policy=policy)

    async def query_none(info_hash, downloaders):
        return None

    monkeypatch.setattr(progress_mod, "_query_torrent", query_none)
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        sub = await session.get(Subscription, sub_id)
    assert wanted.status == WantedStatus.UPGRADING
    assert "pending" not in sub.quality_policy


@pytest.mark.asyncio
async def test_rescue_requeues_stalled_torrent(db, monkeypatch):
    """救援巡检：投递超时仍未完成 → 视为卡死退回；未超时/已完成不动。"""
    from types import SimpleNamespace

    stale = utcnow() - timedelta(days=progress_mod.STALLED_REQUEUE_DAYS + 1)
    _library_id, _item_id, sub_id, wanted_id = await _seed(db, grabbed_at=stale)

    status = SimpleNamespace(name="Slow.Torrent", completed=False, progress=0.5)

    fake_downloader = SimpleNamespace(name="测试下载器", path_mappings=None)

    async def query_status(info_hash, downloaders):
        return fake_downloader, status

    monkeypatch.setattr(progress_mod, "_query_torrent", query_status)
    await progress_mod._rescue_group(sub_id, "abc123", downloaders=[])

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.WANTED  # 卡死退回

    # 已完成待入库（宽限期内）：救援不做任何事（搬运归监听导入/扫描，
    # 工单归库存对账，落点核验也等宽限期过后才判）
    _library_id2, _item_id2, sub_id2, wanted_id2 = await _seed_second(db)
    status.completed = True
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
        name="qb",
        path_mappings=[{"local": str(tmp_path / "downloads"), "remote": "/downloads"}],
    )
    status = SimpleNamespace(
        name="Some.Show.S01",
        completed=True,
        progress=1.0,
        save_path="/downloads",
        files=[SimpleNamespace(path="Some.Show.S01/e1.mkv", size_bytes=1)],
    )

    async def query_status(info_hash, downloaders):
        return fake_downloader, status

    monkeypatch.setattr(progress_mod, "_query_torrent", query_status)
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
