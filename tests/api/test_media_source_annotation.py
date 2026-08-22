"""片源人工标注测试（docs/design/media-source-annotation.md）。

覆盖：整季落库与人工保护位、快照只刷新出处维度、名称解析已知值不被批量
覆盖、NULL/`{}` 哨兵快照不动、重扫 upsert 不覆盖人工值、幂等与改标。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.source_annotation import (
    annotate_media_source,
    list_annotation_candidates,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'ann.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _file(library_id, item_id, episode, *, media_source=None, manual=False, res="2160p"):
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=7,
        episode_number=episode,
        file_path=f"/media/tv/十三邀 (2016)/Season 7/十三邀 S07E{episode:02d}.mp4",
        size_bytes=800_000_000,
        source=FileSource.SCANNED,
        resolution=res,
        bit_rate=1_900_000,
        media_source=media_source,
        media_source_manual=manual,
    )


async def _seed(db):
    """库/条目/订阅 + S7 两集（E01 快照有值、E02 快照 NULL）。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="综艺库", kind="tv", root_paths=["/media/tv"]
        )
        item = MediaItem(kind="tv", tmdb_id=100, title="十三邀", original_title="13", year=2016)
        rule_set = RuleSet(
            name="电视剧", spec={"resolutions": ["2160p", "1080p"], "upgrade_source": "web-dl"}
        )
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
        w1 = WantedItem(
            subscription_id=sub.id,
            media_item_id=item.id,
            season_number=7,
            episode_number=1,
            status=WantedStatus.IMPORTED,
            quality={"resolution": "2160p", "bit_rate": 1_900_000},
        )
        w2 = WantedItem(
            subscription_id=sub.id,
            media_item_id=item.id,
            season_number=7,
            episode_number=2,
            status=WantedStatus.IMPORTED,
            quality=None,
        )
        session.add_all([w1, w2, _file(library.id, item.id, 1), _file(library.id, item.id, 2)])
        await session.commit()
        return library.id, item.id, sub.id, w1.id, w2.id


@pytest.mark.asyncio
async def test_annotate_marks_files_and_refreshes_snapshot(db):
    """整季标注：文件落值 + 保护位；有快照的单元只刷新出处维度
    （probe 字段原样），NULL 快照不动（交给既有回填）。"""
    library_id, item_id, sub_id, w1_id, w2_id = await _seed(db)
    async with db.session() as session:
        result = await annotate_media_source(
            session, media_item_id=item_id, season_number=7, media_source="WEB-DL"
        )
    assert result == {"files": 2, "snapshots": 1}
    async with db.session() as session:
        files = (await session.execute(
            LibraryFile.__table__.select().where(LibraryFile.media_item_id == item_id)
        )).all()
        assert all(f.media_source == "WEB-DL" and f.media_source_manual for f in files)
        w1 = await session.get(WantedItem, w1_id)
        # 标注是**定点修补**而非重建：只动出处维度，其余键原样，结构版本
        # 也不动（本例种的是 v1 老快照，补齐新维度是回填任务的事，§16.3）
        assert w1.quality == {
            "resolution": "2160p",
            "bit_rate": 1_900_000,
            "media_source": "WEB-DL",
            "remux": False,  # 人工标注片源即否定 Remux（显式 False，不删键）
        }
        w2 = await session.get(WantedItem, w2_id)
        assert w2.quality is None


@pytest.mark.asyncio
async def test_annotate_user_lowest_and_relabel_is_idempotent(db):
    """标「按最低档」后可改标；人工行始终在标注范围内（幂等改标）。"""
    _, item_id, _, w1_id, _ = await _seed(db)
    async with db.session() as session:
        await annotate_media_source(
            session, media_item_id=item_id, season_number=7, media_source="user-lowest"
        )
    async with db.session() as session:
        w1 = await session.get(WantedItem, w1_id)
        assert w1.quality["media_source"] == "user-lowest"
        result = await annotate_media_source(
            session, media_item_id=item_id, season_number=7, media_source="WEBRip"
        )
    assert result["files"] == 2
    async with db.session() as session:
        w1 = await session.get(WantedItem, w1_id)
        assert w1.quality["media_source"] == "WEBRip"


@pytest.mark.asyncio
async def test_parsed_source_not_overwritten_and_snapshot_untouched(db):
    """名称解析出的已知片源不在标注范围；该单元最优文件非人工时快照不被污染。"""
    library_id, item_id, _, w1_id, _ = await _seed(db)
    async with db.session() as session:
        # E01 另有一个名称解析出 WEB-DL 的更优文件（多版本并存）
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=7,
                episode_number=1,
                file_path="/media/tv/十三邀 (2016)/Season 7/十三邀 S07E01 - WEB-DL.mp4",
                size_bytes=2_000_000_000,
                source=FileSource.SCANNED,
                resolution="2160p",
                bit_rate=8_000_000,
                media_source="WEB-DL",
            )
        )
        # 该单元快照本来就是对的（来自 WEB-DL 文件）
        w1 = await session.get(WantedItem, w1_id)
        w1.quality = {"resolution": "2160p", "media_source": "WEB-DL", "bit_rate": 8_000_000}
        await session.commit()

        candidates = await list_annotation_candidates(
            session, media_item_id=item_id, season_number=7
        )
        assert {c.file_path for c in candidates} == {
            "/media/tv/十三邀 (2016)/Season 7/十三邀 S07E01.mp4",
            "/media/tv/十三邀 (2016)/Season 7/十三邀 S07E02.mp4",
        }
        result = await annotate_media_source(
            session, media_item_id=item_id, season_number=7, media_source="user-lowest"
        )
    assert result["files"] == 2
    async with db.session() as session:
        rows = (await session.execute(
            LibraryFile.__table__.select().where(
                LibraryFile.media_item_id == item_id, LibraryFile.episode_number == 1
            )
        )).all()
        by_path = {r.file_path: r for r in rows}
        parsed = by_path["/media/tv/十三邀 (2016)/Season 7/十三邀 S07E01 - WEB-DL.mp4"]
        assert parsed.media_source == "WEB-DL"
        assert not parsed.media_source_manual
        # E01 最优文件是名称解析的 WEB-DL（非人工）→ 快照保持原基线
        w1 = await session.get(WantedItem, w1_id)
        assert w1.quality["media_source"] == "WEB-DL"
        assert w1.quality["bit_rate"] == 8_000_000


@pytest.mark.asyncio
async def test_empty_sentinel_snapshot_untouched(db):
    """`{}` 哨兵快照（当时无在位文件）不在标注修补范围。"""
    _, item_id, _, w1_id, _ = await _seed(db)
    async with db.session() as session:
        w1 = await session.get(WantedItem, w1_id)
        w1.quality = {}
        await session.commit()
        await annotate_media_source(
            session, media_item_id=item_id, season_number=7, media_source="WEB-DL"
        )
    async with db.session() as session:
        w1 = await session.get(WantedItem, w1_id)
        assert w1.quality == {}


@pytest.mark.asyncio
async def test_rescan_upsert_preserves_manual_source(db):
    """重扫走 upsert 整体覆盖路径时，人工标注的片源不被文件名解析冲掉。"""
    library_id, item_id, _, _, _ = await _seed(db)
    async with db.session() as session:
        await annotate_media_source(
            session, media_item_id=item_id, season_number=7, media_source="WEBRip"
        )
    async with db.session() as session:
        repo = LibraryFileRepository(session)
        # 模拟重扫对同路径行的重新入库（enrich 解析不出片源 → row 上是 None）
        await repo.upsert_by_path(_file(library_id, item_id, 1, media_source=None))
    async with db.session() as session:
        row = (await session.execute(
            LibraryFile.__table__.select().where(
                LibraryFile.media_item_id == item_id, LibraryFile.episode_number == 1
            )
        )).one()
        assert row.media_source == "WEBRip"
        assert row.media_source_manual
