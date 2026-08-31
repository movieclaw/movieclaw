"""洗版基线快照测试（quality-upgrade.md §4.1/§4.4：实测优先、出处采信名称、
存量回填纯 DB 变换）。"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.upgrade import backfill_upgrade_snapshots
from movieclaw_api.services.subscription.wanted_fulfillment import close_fulfilled_wanted
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_matcher import SNAPSHOT_VERSION


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'upg.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, *, rule_spec=None, wanted_status=WantedStatus.GRABBED, info_hash="abc123"):
    """建 库/条目/订阅/工单 的最小闭包。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        item = MediaItem(kind="tv", tmdb_id=200, title="测试剧集", original_title="Test", year=2024)
        rule_set = RuleSet(name="默认", spec=rule_spec or {})
        session.add_all([item, rule_set])
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
            grabbed_at=utcnow(),
        )
        session.add(wanted)
        await session.commit()
        await session.refresh(wanted)
        return library.id, item.id, sub.id, wanted.id


@pytest.mark.asyncio
async def test_fulfillment_snapshot_probe_overrides_name(db):
    """入库对账落快照：分辨率/HDR 以实测为准（种子标称 2160p 实测 1080p），
    片源/制作组采信投递时的种子名解析（attempt.quality）。"""
    library_id, item_id, sub_id, wanted_id = await _seed(db)
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="abc123",
                units=[[1, 1]],
                quality={"resolution": "2160p", "media_source": "WEB-DL", "release_group": "FAKE"},
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/测试剧集 (2024)/Season 01/S01E01.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
                resolution="1080p",
                hdr="Dolby Vision",
                bit_rate=8_000_000,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        # 落库一律全键（§16.2）：{} 因此在结构上专属于"无法识别"哨兵
        assert wanted.quality == {
            "v": 3,  # 结构版本，驱动新增维度后的存量重算（§16.3）
            "resolution": "1080p",  # 实测覆盖名称的 2160p 虚标
            "media_source": "WEB-DL",  # 出处采信名称
            "remux": False,
            "release_group": "FAKE",
            "hdr": ["DV"],  # probe 的 "Dolby Vision" 归一为词表值
            "video_codec": None,  # 名称与 probe 都没给
            "platforms": [],
            "bit_rate": 8_000_000,
        }


@pytest.mark.asyncio
async def test_disc_snapshot_outranks_torrent_name_source(db):
    """原盘入库：快照片源按结构判 T6，压过投递记录里名称解析出的 Blu-ray。

    少这一条，一个 Remux 候选（T5）就会被判成对原盘（曾记为 T4）的升级，
    而 Remux 正是从这张盘剥出来的——默认 upgrade_keep_old=False 还会把
    原盘送进回收站（issue #163）。
    """
    library_id, item_id, sub_id, wanted_id = await _seed(db)
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="abc123",
                units=[[1, 1]],
                quality={"resolution": "2160p", "media_source": "Blu-ray"},
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/测试剧集 (2024)/Season 01/S01E01 BDMV",
                size_bytes=1,
                source=FileSource.IMPORTED,
                container="bluray",  # BDMV 目录：整张盘，不是从盘里剥出来的文件
                resolution="2160p",
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["media_source"] == "Disc"
        assert wanted.quality["remux"] is False


@pytest.mark.asyncio
async def test_fulfillment_snapshot_without_attempt_uses_filename_enrich(db):
    """手工/扫描入库（无投递记录）：出处维度对文件名重跑 enrich；
    probe 完全失败时不冒充实测，分辨率取名称解析值。"""
    library_id, item_id, _sub_id, wanted_id = await _seed(db, info_hash=None)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/Test.2024.S01E01.1080p.WEB-DL.H264-GRP.mkv",
                size_bytes=1,
                source=FileSource.SCANNED,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["resolution"] == "1080p"
        assert wanted.quality["media_source"] == "WEB-DL"


@pytest.mark.asyncio
async def test_backfill_fills_only_upgrade_enabled_rule_sets(db):
    """存量回填只处理配置了洗版目标的规则组引用的订阅；处理后不再重复。"""
    library_id, item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/e1.mkv",
                size_bytes=1,
                source=FileSource.SCANNED,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        await session.commit()

    await backfill_upgrade_snapshots()
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality is not None
        assert wanted.quality["resolution"] == "1080p"
        assert wanted.quality["media_source"] == "WEB-DL"


@pytest.mark.asyncio
async def test_backfill_skips_rule_sets_without_upgrade(db):
    """未配置洗版目标：历史单元保持 NULL，不做无谓回填。"""
    _library_id, _item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={}, wanted_status=WantedStatus.IMPORTED
    )
    await backfill_upgrade_snapshots()
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality is None


@pytest.mark.asyncio
async def test_backfill_marks_unresolvable_unit_with_sentinel(db):
    """在位文件缺失（imported 但文件已丢）：写 {} 哨兵，避免每 tick 重试。"""
    _library_id, _item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    await backfill_upgrade_snapshots()
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == {}


@pytest.mark.asyncio
async def test_stale_snapshot_is_recomputed_to_current_version(db, monkeypatch):
    """结构版本落后的存量快照被逐批重算（§16.3）。

    不重算的后果不是"少一个维度"，而是这些单元在新维度上恒为单侧未知 ⇒
    比较被截断 ⇒ 大面积变成不可比、洗版停摆。重算是纯 DB 变换：probe 各列
    都在 library_file 里，不重新探测文件。
    """
    from movieclaw_api.services.subscription import upgrade as upgrade_mod

    # 游标是进程内全局状态，前面的用例可能已把它推高——测试里显式归零
    monkeypatch.setattr(upgrade_mod, "_stale_snapshot_cursor", 0)
    library_id, item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/测试剧集 (2024)/Season 01/S01E01.NF.WEB-DL.x265.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
                resolution="1080p",
                video_codec="hevc",
                bit_rate=8_000_000,
            )
        )
        wanted = await session.get(WantedItem, wanted_id)
        # v1 老快照：没有版本键，也没有 video_codec / platforms
        wanted.quality = {"resolution": "1080p", "media_source": "WEB-DL"}
        await session.commit()

    await backfill_upgrade_snapshots()

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
    assert wanted.quality["v"] == SNAPSHOT_VERSION
    # probe 定族、名称定写法：文件名写 x265、probe 给 hevc，同族 → 留 x265
    assert wanted.quality["video_codec"] == "x265"
    assert wanted.quality["platforms"] == ["netflix"]


@pytest.mark.asyncio
async def test_sentinel_snapshot_is_not_recomputed(db, monkeypatch):
    """{} 哨兵不参与版本重算——它表示"没有在位文件/什么都识别不出"，
    与新增维度无关，重算只会每 tick 白白重扫。"""
    from movieclaw_api.services.subscription import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "_stale_snapshot_cursor", 0)
    _library_id, _item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.quality = {}
        await session.commit()

    await backfill_upgrade_snapshots()

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
    assert wanted.quality == {}


@pytest.mark.asyncio
async def test_manual_source_annotation_survives_snapshot_rebuild(db, monkeypatch):
    """人工标注的片源不能被快照重算抹回名称值。

    ``library_file.media_source_manual`` 的语义就是"自动名称解析不得覆盖"
    （media-source-annotation.md），而重算时用的 ``attempt.quality`` 正是
    名称解析的产物。标注存在的意义是把「无法确认」的单元解卡，重算把它抹掉
    等于把单元重新卡死——而且是静默的。
    """
    from movieclaw_api.services.subscription import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "_stale_snapshot_cursor", 0)
    library_id, item_id, sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="abc123",
                units=[[1, 1]],
                # 种子名解析出的片源是 WEB-DL；用户后来人工标注为 Blu-ray
                quality={"resolution": "1080p", "media_source": "WEB-DL"},
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/测试剧集 (2024)/Season 01/S01E01.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
                resolution="1080p",
                bit_rate=8_000_000,
                media_source="Blu-ray",
                media_source_manual=True,
            )
        )
        wanted = await session.get(WantedItem, wanted_id)
        wanted.quality = {"resolution": "1080p", "media_source": "Blu-ray"}  # v1 老快照
        await session.commit()

    await backfill_upgrade_snapshots()

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
    assert wanted.quality["v"] == SNAPSHOT_VERSION  # 确实重算过
    assert wanted.quality["media_source"] == "Blu-ray"  # 人工标注仍在


@pytest.mark.asyncio
async def test_refill_never_downgrades_a_good_baseline_to_sentinel(db, monkeypatch):
    """文件此刻不在位时，重算保留旧基线而不是写 {} 哨兵。

    入库验证确认升级时会把旧文件移进回收站，重算撞进这个窗口就会看到
    "该单元没有在位文件"。写下 {} 的后果是粘的：哨兵被排除在重算之外，
    这个单元从此永久退出洗版。文件不在位是暂时状态，真相是"这次测不了"。
    """
    from movieclaw_api.services.subscription import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "_stale_snapshot_cursor", 0)
    _library_id, _item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    good = {"resolution": "1080p", "media_source": "WEB-DL"}  # v1，会被判定为陈旧
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.quality = good
        await session.commit()  # 故意不建 LibraryFile：模拟文件不在位

    await backfill_upgrade_snapshots()

    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
    assert wanted.quality == good
