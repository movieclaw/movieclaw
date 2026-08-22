"""洗版入库验证测试（quality-upgrade.md §6.3/§7）：实测确认、证伪排除、
熔断、旧版本回收站、旧任务清理通道、手工升级路径。"""

from __future__ import annotations

import os

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
    FileState,
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


def _write_seeded(path, data: bytes) -> None:
    """写测试文件并挂一个硬链接副本，模拟推荐的硬链入库形态（st_nlink ≥ 2）。

    做种保护上线后（§7.1：唯一硬链接绝不改名），回收站相关断言都以
    硬链形态为前提；单链接（原地下载/复制导入）的行为由专门用例覆盖。
    """
    path.write_bytes(data)
    seed_dir = path.parent / ".seed-copies"
    seed_dir.mkdir(exist_ok=True)
    os.link(path, seed_dir / f"{len(list(seed_dir.iterdir()))}-{path.name}")


_WEBDL = {"resolution": "1080p", "media_source": "WEB-DL"}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'verify.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, tmp_path, *, quality=_WEBDL, old_hash="old1", rule_spec=None):
    """库(真实 tmp 根)/条目/订阅/imported 工单 + 旧版本物理文件。"""
    root = tmp_path / "tv"
    root.mkdir(exist_ok=True)
    old_file = root / "Testshow.S01E01.1080p.WEB-DL.mkv"
    _write_seeded(old_file, b"old")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
        item = MediaItem(
            kind="tv", tmdb_id=200, title="测试剧集", original_title="Testshow", year=2024,
            aliases=["Testshow"],
        )
        rule_set = RuleSet(name="默认", spec=rule_spec or {"upgrade_source": "remux"})
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
            status=WantedStatus.IMPORTED,
            quality=quality,
            info_hash=old_hash,
            imported_at=utcnow(),
        )
        session.add(wanted)
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                file_path=str(old_file),
                size_bytes=3,
                source=FileSource.IMPORTED,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        # 旧版本对应的下载 attempt（洗版确认后应进清理通道）
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub.id,
                info_hash=old_hash,
                units=[[1, 1]],
                quality=_WEBDL,
                status=DownloadAttemptStatus.COMPLETED,
                last_progress_at=utcnow(),
            )
        )
        await session.commit()
        await session.refresh(wanted)
        return library, item.id, sub.id, wanted.id, root, old_file


async def _add_upgrade_delivery(
    db, sub_id, item_id, library_id, root, *, claimed_quality, probed, new_hash="new1"
):
    """模拟洗版 attempt + 其下载文件入库：新 library_file 行 + attempt。"""
    new_file = root / "Testshow.S01E01.1080p.REMUX.mkv"
    _write_seeded(new_file, b"new-version")
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash=new_hash,
                site_id="site-a",
                torrent_id="up1",
                units=[[1, 1]],
                quality=claimed_quality,
                purpose="upgrade",
                status=DownloadAttemptStatus.COMPLETED,
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(new_file),
                size_bytes=11,
                source=FileSource.IMPORTED,
                site_id="site-a",
                torrent_id="up1",
                **probed,
            )
        )
        await session.commit()
    return new_file


@pytest.mark.asyncio
async def test_confirm_upgrade_replaces_old_and_chains_cleanup(db, tmp_path):
    """实测确认：快照刷新、info_hash 切换、旧文件进回收站、旧 attempt 进
    清理通道、UPGRADED 活动。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        probed={"resolution": "1080p", "bit_rate": 30_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["remux"] is True
        assert wanted.quality["media_source"] == "Blu-ray"
        assert wanted.info_hash == "new1"
        assert wanted.upgrade_verify_failures == 0
        # 旧文件物理移入回收站
        assert not old_file.exists()
        assert (root / ".movieclaw-trash").is_dir()
        assert len(list((root / ".movieclaw-trash").iterdir())) == 1
        # 旧 attempt 进入清理通道，新 attempt 完结并记录替换关系
        old_attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old1"
                )
            )
        ).scalar_one()
        new_attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "new1"
                )
            )
        ).scalar_one()
        assert old_attempt.status == DownloadAttemptStatus.CLEANUP_PENDING
        assert new_attempt.status == DownloadAttemptStatus.IMPORTED
        assert new_attempt.replaces_attempt_id == old_attempt.id
        # 活动
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type == "upgraded"
                    )
                )
            ).scalars()
        )
        assert len(acts) == 1
        assert "1080p WEB-DL → 1080p Remux" in acts[0].message


@pytest.mark.asyncio
async def test_refuted_upgrade_trashes_new_file_and_excludes(db, tmp_path):
    """证伪：标称 Remux 实测仍是 WEB-DL 档 → 新文件进回收站、旧文件不动、
    attempt=FAILED、熔断计数 +1、UPGRADE_VERIFY_FAILED 活动。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    new_file = await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        # 纯分辨率虚标：标称 2160p WEB-DL，实测 1080p → 与基线同档，证伪
        # （片源声明如实采信是 §4.2 明确接受的残余风险，不在证伪范围）
        claimed_quality={"resolution": "2160p", "media_source": "WEB-DL"},
        probed={"resolution": "1080p", "bit_rate": 6_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == _WEBDL  # 基线不动
        assert wanted.info_hash == "old1"
        assert wanted.upgrade_verify_failures == 1
        assert old_file.exists()  # 旧版本原样在位
        assert not new_file.exists()  # 证伪文件移入回收站
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "new1"
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.FAILED
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type == "upgrade_verify_failed"
                    )
                )
            ).scalars()
        )
        assert len(acts) == 1
        assert "实测" in acts[0].message


@pytest.mark.asyncio
async def test_third_refutation_fuses_and_notices(db, tmp_path):
    """连续第 3 次证伪：转入 30 天冷却 + system_notice。"""
    library, item_id, sub_id, wanted_id, root, _old = await _seed(db, tmp_path)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.upgrade_verify_failures = 2
        await session.commit()
    await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "2160p", "media_source": "WEB-DL"},
        probed={"resolution": "1080p", "bit_rate": 6_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.upgrade_verify_failures == 3
        assert wanted.next_search_at is not None
        assert (wanted.next_search_at - utcnow()).days >= 29
        notice = (
            await session.execute(
                select(SystemNotice).where(
                    SystemNotice.dedupe_key == f"subscription.upgrade:{sub_id}:1:1"
                )
            )
        ).scalar_one()
        assert "连续 3 次" in notice.message


@pytest.mark.asyncio
async def test_manual_better_file_confirms_without_attempt(db, tmp_path):
    """手工塞入更优文件（无 attempt）：实测为证同样确认，快照刷新、旧文件
    进回收站、info_hash 保持（没有新种子可关联）。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    manual = root / "Testshow.S01E01.2160p.WEB-DL.mkv"
    _write_seeded(manual, b"manual-4k")
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(manual),
                size_bytes=9,
                source=FileSource.SCANNED,
                resolution="2160p",
                media_source="WEB-DL",
                bit_rate=20_000_000,
            )
        )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["resolution"] == "2160p"
        assert wanted.info_hash == "old1"  # 无新 attempt 可关联，保持
        assert not old_file.exists() and manual.exists()


@pytest.mark.asyncio
async def test_equal_manual_file_only_collected(db, tmp_path):
    """手工塞入同档文件：仅收编为多版本，不动快照、不触发任何洗版活动。"""
    library, item_id, _sub, wanted_id, root, old_file = await _seed(db, tmp_path)
    dup = root / "Testshow.S01E01.1080p.WEB-DL.DUP.mkv"
    _write_seeded(dup, b"dup")
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(dup),
                size_bytes=3,
                source=FileSource.SCANNED,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == _WEBDL
        assert old_file.exists() and dup.exists()  # 两个版本都保留
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type.in_(("upgraded", "upgrade_verify_failed"))  # type: ignore[attr-defined]
                    )
                )
            ).scalars()
        )
        assert acts == []


@pytest.mark.asyncio
async def test_no_upgrade_rule_never_touches_files(db, tmp_path):
    """未开洗版的规则组：手工塞入更优文件只静默刷新基线，绝不移动/删除
    任何文件（删除性动作必须有洗版目标这个显式 opt-in）。"""
    library, item_id, _sub, wanted_id, root, old_file = await _seed(db, tmp_path)
    async with db.session() as session:
        rule_set = (await session.execute(select(RuleSet))).scalar_one()
        rule_set.spec = {}  # 关掉洗版
        await session.commit()
    better = root / "Testshow.S01E01.2160p.WEB-DL.mkv"
    _write_seeded(better, b"manual-4k")
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(better),
                size_bytes=9,
                source=FileSource.SCANNED,
                resolution="2160p",
                media_source="WEB-DL",
                bit_rate=20_000_000,
            )
        )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["resolution"] == "2160p"  # 基线静默刷新
        assert old_file.exists() and better.exists()  # 两份文件都原地不动
        assert not (root / ".movieclaw-trash").exists()
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type.in_(("upgraded", "upgrade_verify_failed"))  # type: ignore[attr-defined]
                    )
                )
            ).scalars()
        )
        assert acts == []


@pytest.mark.asyncio
async def test_mixed_attempt_gap_unit_not_refuted(db, tmp_path):
    """混合投递（purpose=upgrade 的 attempt 同时覆盖缺口单元）：靠这次投递
    才入库的缺口单元不能走证伪分支——判据是单元入库时间晚于 attempt 创建。"""
    from datetime import timedelta as _td

    library, item_id, sub_id, wanted_id, root, _old = await _seed(db, tmp_path)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old1"
                )
            )
        ).scalar_one()
        # 模拟：attempt 是混合洗版投递，单元靠它入库（imported_at 晚于 attempt）
        attempt.purpose = "upgrade"
        attempt.created_at = utcnow() - _td(minutes=10)
        wanted.imported_at = utcnow()
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old1"
                )
            )
        ).scalar_one()
        assert wanted.upgrade_verify_failures == 0  # 没有被误证伪
        assert attempt.status == DownloadAttemptStatus.COMPLETED  # 未被打成 FAILED


@pytest.mark.asyncio
async def test_honest_resource_superseded_is_cancelled_not_refuted(db, tmp_path):
    """诚实资源被抢先 ≠ 证伪：下载期间基线被手工入库的更优版本刷高，
    洗版结果落地时已不再需要——attempt 收口为 CANCELLED，不计熔断、
    不进排除清单、不写证伪活动。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    async with db.session() as session:
        # 基线已被手工 Remux 抢先刷高
        wanted = await session.get(WantedItem, wanted_id)
        wanted.quality = {"resolution": "1080p", "media_source": "Blu-ray", "remux": True}
        await session.commit()
    # 洗版 attempt 诚实交付了它声称的 Blu-ray 重编码（低于新基线但符合声称）
    new_file = await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray"},
        probed={"resolution": "1080p", "bit_rate": 15_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.upgrade_verify_failures == 0  # 不计熔断
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "new1"
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.CANCELLED  # 不是 FAILED
        assert "抢先" in (attempt.cleanup_note or "")
        assert not new_file.exists()  # 不需要的结果仍进回收站
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type == "upgrade_verify_failed"
                    )
                )
            ).scalars()
        )
        assert acts == []  # 没有错误的证伪指控


@pytest.mark.asyncio
async def test_pack_confirm_retains_extra_old_attempts(db, tmp_path):
    """整季包一次替换多个来源不同的旧单集：replaces 指针只能指一个，
    其余旧 attempt 保守 RETAINED（绝不悬空 CLEANUP_PENDING 等不到清理）。"""
    library, item_id, sub_id, _w1, root, _old1 = await _seed(db, tmp_path)
    # 第二个单元 E02：独立旧文件 + 独立旧 attempt（old2）
    old_file2 = root / "Testshow.S01E02.1080p.WEB-DL.mkv"
    _write_seeded(old_file2, b"old2")
    async with db.session() as session:
        session.add(
            WantedItem(
                subscription_id=sub_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=2,
                status=WantedStatus.IMPORTED,
                quality=_WEBDL,
                info_hash="old2",
                imported_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=2,
                file_path=str(old_file2),
                size_bytes=4,
                source=FileSource.IMPORTED,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="old2",
                units=[[1, 2]],
                quality=_WEBDL,
                status=DownloadAttemptStatus.COMPLETED,
                last_progress_at=utcnow(),
            )
        )
        # 整季包洗版 attempt 覆盖 E01+E02，两个新文件已入库
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="packhash",
                site_id="site-a",
                torrent_id="pack1",
                units=[[1, 1], [1, 2]],
                quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
                purpose="upgrade",
                status=DownloadAttemptStatus.COMPLETED,
                last_progress_at=utcnow(),
            )
        )
        for episode in (1, 2):
            new_file = root / f"Testshow.S01E0{episode}.REMUX.mkv"
            _write_seeded(new_file, b"remux")
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item_id,
                    season_number=1,
                    episode_number=episode,
                    file_path=str(new_file),
                    size_bytes=5,
                    source=FileSource.IMPORTED,
                    site_id="site-a",
                    torrent_id="pack1",
                    resolution="1080p",
                    bit_rate=30_000_000,
                )
            )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        old1 = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old1"
                )
            )
        ).scalar_one()
        old2 = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old2"
                )
            )
        ).scalar_one()
        statuses = {old1.status, old2.status}
        # 一个走清理通道（被 replaces 指针指到），另一个保守保留
        assert DownloadAttemptStatus.CLEANUP_PENDING in statuses
        assert DownloadAttemptStatus.RETAINED in statuses


@pytest.mark.asyncio
async def test_trash_failure_keeps_ledger_row(db, tmp_path, monkeypatch):
    """回收站移动失败：台账行必须保留（行删了文件还在，扫描会把它当新
    文件重新收编，台账必须与磁盘一致）。"""
    import shutil as _shutil

    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        probed={"resolution": "1080p", "bit_rate": 30_000_000},
    )

    def broken_move(*args, **kwargs):
        raise OSError("模拟移动失败")

    monkeypatch.setattr(_shutil, "move", broken_move)
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["remux"] is True  # 升级本身确认
        assert old_file.exists()  # 移动失败，文件在原位
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == item_id)
                )
            ).scalars()
        )
        # 旧文件的台账行保留（与磁盘一致），新文件行在位
        assert {r.file_path for r in rows} >= {str(old_file)}


@pytest.mark.asyncio
async def test_refuted_residue_file_quarantined_not_reborn(db, tmp_path):
    """证伪残留文件（FAILED attempt 的来源）重新出现在台账时：隔离清理，
    绝不参与最优选择——否则会借文件名解析"重生"为手工升级。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    residue = root / "Testshow.S01E01.FAKE.REMUX.mkv"
    _write_seeded(residue, b"fake")
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="fakehash",
                site_id="site-a",
                torrent_id="fake1",
                units=[[1, 1]],
                quality={"resolution": "2160p", "media_source": "WEB-DL"},
                purpose="upgrade",
                status=DownloadAttemptStatus.FAILED,
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(residue),
                size_bytes=4,
                source=FileSource.IMPORTED,
                site_id="site-a",
                torrent_id="fake1",
                resolution="1080p",
                media_source="Blu-ray",  # 文件名/来源声称高档——不能被采信
                bit_rate=6_000_000,
            )
        )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == _WEBDL  # 基线纹丝不动
        assert not residue.exists()  # 残留文件被隔离进回收站
        assert old_file.exists()


@pytest.mark.asyncio
async def test_old_pack_attempt_kept_while_serving_other_units(db, tmp_path):
    """旧整季包 attempt 仍服务其他单元（E02 还指着它）时，只洗 E01 绝不把
    整包送进清理通道——否则会杀掉其余集的做种。"""
    library, item_id, sub_id, _w1, root, _old1 = await _seed(db, tmp_path, old_hash="packold")
    # E02 也来自同一个旧整季包（info_hash 同为 packold），不参与本次洗版
    old_file2 = root / "Testshow.S01E02.1080p.WEB-DL.mkv"
    _write_seeded(old_file2, b"old2")
    async with db.session() as session:
        session.add(
            WantedItem(
                subscription_id=sub_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=2,
                status=WantedStatus.IMPORTED,
                quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
                info_hash="packold",  # 已到顶，但仍靠旧包做种
                imported_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=2,
                file_path=str(old_file2),
                size_bytes=4,
                source=FileSource.IMPORTED,
                resolution="1080p",
                bit_rate=30_000_000,
            )
        )
        old_pack = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "packold"
                )
            )
        ).scalar_one()
        old_pack.units = [[1, 1], [1, 2]]
        await session.commit()
    await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        probed={"resolution": "1080p", "bit_rate": 30_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.season_number == 1, WantedItem.episode_number == 1
                )
            )
        ).scalar_one()
        assert wanted.info_hash == "new1"  # E01 升级确认
        old_pack = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "packold"
                )
            )
        ).scalar_one()
        # E02 仍指着旧包 → 旧包保持原状态，不进清理通道
        assert old_pack.status == DownloadAttemptStatus.COMPLETED


@pytest.mark.asyncio
async def test_confirm_recycles_single_link_old_file_too(db, tmp_path):
    """唯一硬链接的旧文件（复制导入形态）同样移入回收站并倒计时
    （2026-08-17 收敛）：copy 入库下库文件 nlink 恒为 1，旧的做种保护判据
    必然全命中、旧版本永久滞留；做种原始文件在下载目录，不受库内移动影响。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    # 拆掉硬链接副本，模拟复制导入：old_file 变成唯一副本（st_nlink == 1）
    for entry in (root / ".seed-copies").iterdir():
        if entry.name.endswith(old_file.name):
            entry.unlink()
    assert old_file.stat().st_nlink == 1
    await _add_upgrade_delivery(
        db,
        sub_id,
        item_id,
        library.id,
        root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        probed={"resolution": "1080p", "bit_rate": 30_000_000},
    )
    async with db.session() as session:
        from movieclaw_api.services.subscription.upgrade import verify_upgrades

        await verify_upgrades(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.info_hash == "new1"  # 升级本身照常确认
        assert wanted.quality["remux"] is True
        files = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == item_id)
                )
            ).scalars()
        )
        assert len(files) == 2  # 旧行保留为待回收行（可恢复）
        trashed = [f for f in files if f.state == FileState.TRASHED]
        assert len(trashed) == 1
        assert trashed[0].purge_after is not None  # 一律按保留期倒计时
        assert ".movieclaw-trash" in trashed[0].file_path
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id
                    )
                )
            ).scalars()
        )
        assert any(a.payload.get("trash_paths") for a in activities)
        assert all(not a.payload.get("kept_in_place") for a in activities)
    assert not old_file.exists()  # 旧文件已进回收站


@pytest.mark.asyncio
async def test_confirm_keep_old_coexists(db, tmp_path):
    """「保留共存」（upgrade_keep_old，收藏家模式）：升级照常确认，
    旧版本既不进回收站也不转待回收——两个在位版本并存。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(
        db, tmp_path, rule_spec={"upgrade_source": "remux", "upgrade_keep_old": True}
    )
    await _add_upgrade_delivery(
        db,
        sub_id,
        item_id,
        library.id,
        root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        probed={"resolution": "1080p", "bit_rate": 30_000_000},
    )
    async with db.session() as session:
        from movieclaw_api.services.subscription.upgrade import verify_upgrades

        await verify_upgrades(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.info_hash == "new1"  # 升级本身照常确认
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == item_id)
                )
            ).scalars()
        )
        assert len(rows) == 2
        assert all(r.state == FileState.IN_PLACE for r in rows)  # 都在位，无待回收
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id
                    )
                )
            ).scalars()
        )
        assert any(a.payload.get("kept_coexisting") for a in activities)
    assert old_file.exists()


_LADDER_SPEC = {
    "upgrade_source": "web-dl",
    "resolutions": ["1080p"],
    "video_codecs": ["x265", "x264"],  # 顺序即偏好：x265 更优
    "upgrade_ladder": ["resolution", "source", "video_codec"],
}
_WEBDL_X264 = {
    "v": 2,
    "resolution": "1080p",
    "media_source": "WEB-DL",
    "video_codec": "x264",
}


@pytest.mark.asyncio
async def test_codec_only_upgrade_is_confirmed_not_refuted(db, tmp_path):
    """只在低位维度（编码）上更优的洗版，实测验证必须确认而不是判否。

    验证走的是**同一条档位阶梯**——它此前只比分辨率与片源，对 §14 新加的
    维度视而不见。后果不是"少确认一次"：判否会把刚下完的文件扔进回收站，
    而"诚实资源"判定又不会把它拉黑，于是下一轮搜索再抓同一个候选，
    下载 → 回收 → 再下载 无限循环，每轮都在烧上传量。
    """
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(
        db, tmp_path, quality=_WEBDL_X264, rule_spec=_LADDER_SPEC
    )
    new_file = await _add_upgrade_delivery(
        db,
        sub_id,
        item_id,
        library.id,
        root,
        claimed_quality={
            "resolution": "1080p",
            "media_source": "WEB-DL",
            "video_codec": "x265",
        },
        # probe 给 ffprobe 命名空间的 hevc，与标题的 x265 同族 → 快照留 x265
        probed={"resolution": "1080p", "bit_rate": 9_000_000, "video_codec": "hevc"},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["video_codec"] == "x265"  # 基线前进
        assert wanted.info_hash == "new1"
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "new1"
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.IMPORTED
    assert new_file.exists()  # 新文件没有被当成证伪扔进回收站
