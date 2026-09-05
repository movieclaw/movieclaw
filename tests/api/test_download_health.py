"""下载健康三件事：落点故障按根因分组、换源刹车、停滞事件折叠。

来自同一次真实事故（挂载失效 45 小时、16 个种子无法入库、白烧 90GB）：
- 一个目录级根因只能表现为一条红灯，单种子告警被收编而不是各自刷屏；
- 红灯亮着时自动换源必须刹车，手动换种不受限；
- 同订阅同单元的停滞事件折叠计数，不把真信号淹没。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.download_progress as progress_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.download_health import (
    landing_brake,
    landing_group_key,
    record_stalled,
)
from movieclaw_api.services.subscription.replacement import run_replacement_search
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.models.downloader_client import ClientType, DownloaderClient
from movieclaw_db.models.system_notice import NoticeStatus, SystemNotice
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'health.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, *, tmdb_id: int, title: str, info_hash: str, grabbed_at):
    """库/条目/订阅/在途工单 的最小闭包，返回 (sub_id, wanted_id)。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name=f"库{tmdb_id}", kind="tv", root_paths=[f"/media/{tmdb_id}"]
        )
        item = MediaItem(kind="tv", tmdb_id=tmdb_id, title=title, original_title=title)
        rule_set = RuleSet(name=f"规则{tmdb_id}", spec={})
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
            status=WantedStatus.GRABBED,
            info_hash=info_hash,
            grabbed_at=grabbed_at,
            updated_at=grabbed_at,
        )
        session.add(wanted)
        await session.commit()
        await session.refresh(wanted)
        return sub.id, wanted.id


def _completed(name: str, size: int) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        completed=True,
        completed_bytes=size,
        size_bytes=size,
        progress=1.0,
        state="completed",
        save_path="/downloads/剧集",
        files=[SimpleNamespace(path=f"{name}/e1.mkv", size_bytes=size)],
    )


async def _active_notices(db) -> list[SystemNotice]:
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(SystemNotice).where(SystemNotice.status == NoticeStatus.ACTIVE.value)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


@pytest.mark.asyncio
async def test_empty_dir_groups_landing_failures_into_one_root_cause(db, monkeypatch, tmp_path):
    """目录存在但为空 → 目录级红灯一条；两部剧的单种子告警被它收编并累计体积；
    目录恢复可见后目录级红灯熄灭。"""
    stale = utcnow() - timedelta(minutes=progress_mod._LANDING_GRACE_MINUTES + 5)
    local_root = tmp_path / "download"
    (local_root / "剧集").mkdir(parents=True)  # 剧集目录在、但空：挂载失效形态
    sub_a, _ = await _seed(db, tmdb_id=1, title="剧A", info_hash="a" * 40, grabbed_at=stale)
    sub_b, _ = await _seed(db, tmdb_id=2, title="剧B", info_hash="b" * 40, grabbed_at=stale)

    downloader = SimpleNamespace(
        id=7,
        name="qb",
        path_mappings=[{"local": str(local_root), "remote": "/downloads"}],
    )
    statuses = {
        "a" * 40: _completed("Show.A.S01", 3 * 2**30),
        "b" * 40: _completed("Show.B.S01", 5 * 2**30),
    }

    async def lookup(info_hash, *args, **kwargs):
        return progress_mod._TorrentLookup(
            match=(downloader, statuses[info_hash]), reachable_count=1
        )

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup)
    await progress_mod._rescue_group(sub_a, "a" * 40, downloaders=[])
    await progress_mod._rescue_group(sub_b, "b" * 40, downloaders=[])

    notices = await _active_notices(db)
    group_key = landing_group_key(7, str(local_root / "剧集"))
    group = next(n for n in notices if n.dedupe_key == group_key)
    children = [n for n in notices if n.dedupe_key.startswith("subscription.landing:")]
    assert len(children) == 2, "单种子告警必须保持活跃：任务卡片靠它们挂红标"
    assert all(c.payload["grouped_under"] == group_key for c in children)
    assert group.payload["count"] == 2
    assert group.payload["state"] == "empty"
    assert group.payload["wasted_bytes"] == 8 * 2**30
    assert sorted(group.payload["subscription_ids"]) == sorted([sub_a, sub_b])
    assert "8.0 GB" in group.message
    assert "重启" in group.message  # 根因的确切结论，不是三选一
    assert "自动换源已暂停" in group.message
    # 单种子文案不再给"可能是 A 或 B 或 C"
    assert "可能无法访问该路径" not in children[0].message
    assert "重启" in children[0].message

    # 目录恢复（内容可见）→ 目录级红灯熄灭
    (local_root / "剧集" / "Show.A.S01").mkdir()
    await progress_mod._rescue_group(sub_a, "a" * 40, downloaders=[])
    notices = await _active_notices(db)
    assert not [n for n in notices if n.dedupe_key == group_key]


@pytest.mark.asyncio
async def test_visible_dir_missing_content_is_torrent_level_not_grouped(db, monkeypatch, tmp_path):
    """目录可见、只是这个种子的内容不在：种子级问题，文案指向"被移动"，不升格目录级。"""
    stale = utcnow() - timedelta(minutes=progress_mod._LANDING_GRACE_MINUTES + 5)
    local_root = tmp_path / "download"
    (local_root / "剧集" / "别的内容").mkdir(parents=True)
    sub_a, _ = await _seed(db, tmdb_id=1, title="剧A", info_hash="a" * 40, grabbed_at=stale)
    downloader = SimpleNamespace(
        id=7, name="qb", path_mappings=[{"local": str(local_root), "remote": "/downloads"}]
    )

    async def lookup(*args, **kwargs):
        return progress_mod._TorrentLookup(
            match=(downloader, _completed("Show.A.S01", 1)), reachable_count=1
        )

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup)
    await progress_mod._rescue_group(sub_a, "a" * 40, downloaders=[])
    notices = await _active_notices(db)
    assert not [n for n in notices if n.dedupe_key.startswith("downloader.landing:")]
    child = next(n for n in notices if n.dedupe_key.startswith("subscription.landing:"))
    assert "被移动或改名" in child.message
    assert "grouped_under" not in child.payload


@pytest.mark.asyncio
async def test_auto_replacement_brakes_while_landing_group_active(db, monkeypatch, tmp_path):
    """目录级红灯活跃时自动换源刹车（不搜、退避 1 小时、时间线说明原因）；
    手动 force 不受限；红灯被用户忽略后刹车松开。"""
    async with db.session() as session:
        # downloader_id 有外键，投递台账与红灯都要挂在真实的下载器记录上
        dl = DownloaderClient(name="qb", client_type=ClientType.QBITTORRENT, url="http://qb:8080")
        session.add(dl)
        await session.flush()
        dl_id = dl.id
        media = MediaItem(kind="tv", tmdb_id=9, title="刹车剧", original_title="Brake")
        rule = RuleSet(name="刹车规则", spec={})
        session.add_all([media, rule])
        await session.flush()
        sub = Subscription(media_item_id=media.id, kind="tv", rule_set_id=rule.id)
        session.add(sub)
        await session.flush()
        session.add(
            WantedItem(
                subscription_id=sub.id,
                media_item_id=media.id,
                season_number=1,
                episode_number=1,
                status=WantedStatus.GRABBED,
                info_hash="c" * 40,
            )
        )
        attempt = SubscriptionDownloadAttempt(
            subscription_id=sub.id,
            downloader_id=dl_id,
            info_hash="c" * 40,
            torrent_title="Old.1080p",
            save_path="/download/剧集",
            units=[[1, 1]],
            quality={"resolution": "1080p"},
            owned_by_movieclaw=True,
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow() - timedelta(minutes=31),
            next_search_at=utcnow(),
        )
        session.add(attempt)
        # 目录级红灯：与投递目录同一片区域
        session.add(
            SystemNotice(
                dedupe_key=landing_group_key(dl_id, "/download/剧集"),
                severity="error",
                source="downloader",
                title="下载器「qb」的目录 /download/剧集 movieclaw 看不到",
                message="x",
                payload={
                    "group_key": landing_group_key(dl_id, "/download/剧集"),
                    "downloader_id": dl_id,
                    "local_dir": "/download/剧集",
                },
            )
        )
        await session.commit()
        attempt_id, sub_id = attempt.id, sub.id

    searched: list[str] = []

    async def fake_search(keyword, *, categories=None, exclude_protected=False):
        searched.append(keyword)
        return SimpleNamespace(sites=[SimpleNamespace(error=None, site_name="站")], items=[])

    import movieclaw_api.services.site_search as site_search

    monkeypatch.setattr(site_search, "search_all_sites", fake_search)

    assert await run_replacement_search(attempt_id) is False
    assert searched == [], "刹车期间不该发起任何搜索"
    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        delay = attempt.next_search_at - utcnow()
        assert timedelta(minutes=58) < delay <= timedelta(hours=1)
        acts = (
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
        assert len(acts) == 1 and acts[0].payload["reason"] == "landing_brake"
        assert "自动换源已暂停" in acts[0].message
        attempt.next_search_at = utcnow()
        await session.commit()

    # 手动立即换种：用户的明确决定，刹车不拦
    assert await run_replacement_search(attempt_id, force=True) is False
    assert searched, "force 必须越过刹车真正去搜"

    # 用户忽略红灯 = "我知道，别管"：刹车随之松开
    async with db.session() as session:
        notice = (await session.execute(select(SystemNotice))).scalar_one()
        notice.status = NoticeStatus.DISMISSED.value
        await session.commit()
        assert await landing_brake(session, dl_id, "/download/剧集") is None


@pytest.mark.asyncio
async def test_stalled_events_fold_by_subscription_and_units(db):
    """同订阅、同单元、同原因 24 小时内折叠成一条并计数；不同单元各自一条。"""
    async with db.session() as session:
        media = MediaItem(kind="tv", tmdb_id=5, title="折叠剧", original_title="Fold")
        rule = RuleSet(name="折叠规则", spec={})
        session.add_all([media, rule])
        await session.flush()
        sub = Subscription(media_item_id=media.id, kind="tv", rule_set_id=rule.id)
        session.add(sub)
        await session.commit()
        sub_id = sub.id

        for hash_ in ("1" * 40, "2" * 40, "3" * 40):  # 换了三次源，各自停滞
            await record_stalled(
                session,
                subscription_id=sub_id,
                info_hash=hash_,
                units=[[1, 16]],
                reason="no_byte_progress",
                message="下载已连续 15 分钟没有进度",
            )
        await record_stalled(  # 另一集：独立一条
            session,
            subscription_id=sub_id,
            info_hash="4" * 40,
            units=[[1, 17]],
            reason="no_byte_progress",
            message="下载已连续 15 分钟没有进度",
        )
        rows = (
            (
                await session.execute(
                    select(SubscriptionActivity)
                    .where(SubscriptionActivity.subscription_id == sub_id)
                    .order_by(SubscriptionActivity.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        folded, other = rows
        assert folded.payload["occurrences"] == 3
        assert folded.payload["info_hash"] == "3" * 40  # 指向最近一次
        assert "3 次" in folded.message
        assert other.payload["occurrences"] == 1
        assert "次" not in other.message


@pytest.mark.asyncio
async def test_landing_group_folds_under_path_probe_notice_both_orders(db):
    """路径体检红灯与目录级落点红灯是同一挂载故障的两个入口：无论谁先亮，
    后者都收编到前者下面，告警中心只显示一条。"""
    from movieclaw_api.services.download_health import (
        adopt_landing_groups,
        paths_key,
        upsert_landing_group,
    )
    from movieclaw_api.services.downloader_paths import probe_local_dir
    from movieclaw_api.services.system_notice import upsert_notice
    from movieclaw_db.models.system_notice import NoticeSeverity

    async with db.session() as session:
        dl = DownloaderClient(name="qb", client_type=ClientType.QBITTORRENT, url="http://qb:8080")
        session.add(dl)
        await session.commit()
        await session.refresh(dl)
        empty_probe = probe_local_dir("/definitely/not/there")

        # 顺序一：目录级红灯先亮，再点亮路径体检红灯 → adopt 反向收编
        await upsert_landing_group(session, dl, "/download/a", empty_probe)
        await upsert_notice(
            session,
            dedupe_key=paths_key(dl.id),
            severity=NoticeSeverity.ERROR,
            source="downloader",
            title="路径",
            message="x",
            payload={"group_key": paths_key(dl.id), "downloader_id": dl.id},
        )
        assert await adopt_landing_groups(session, dl.id) == 1

        # 顺序二：路径体检红灯已活跃，新亮的目录级红灯自己挂上去
        await upsert_landing_group(session, dl, "/download/b", empty_probe)

        rows = (
            (
                await session.execute(
                    select(SystemNotice).where(
                        SystemNotice.dedupe_key.startswith("downloader.landing:")
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(r.payload["grouped_under"] == paths_key(dl.id) for r in rows)
