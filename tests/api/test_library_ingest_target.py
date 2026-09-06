"""监听导入「自定义目录」目标的测试（docs/design/strm-workflow.md R1/R2）。

覆盖：
- 路径规则条目：识别改名后落 target_path（库同款命名规范），不写库台账、
  不生成 NFO，结论文案提示"外部流转后自动入账"；
- 订阅认领的条目：身份与落点分离——认领只取身份，文件仍落 target_path，
  工单保持在途（"库里有"才算完成，strm 回流入账时才关单）；
- 校验：library_id 与 target_path 互斥、kind 必填、每 kind 至多一条、
  与库根/监听源重叠拒绝、硬链同盘检测；
- resolve_dispatch_rule：专属规则 → auto 规则 → 路径规则的查找链。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select
from tests.api.test_library_ingest import _stub_unit

import movieclaw_api.services.library.ingest as ingest_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services.import_watch_config import (
    ImportWatchConfigService,
    resolve_dispatch_rule,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ImportWatch,
    IngestEntry,
    IngestStatus,
    LibraryFile,
    MediaItem,
    MediaMetadata,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

_FAKE_SPEC = SimpleNamespace(
    resolution="1080p",
    video_codec="hevc",
    hdr=None,
    bit_depth=10,
    duration_seconds=3600,
    bit_rate=None,
    frame_rate=23.976,
    color_space="BT.709",
    audio_streams=[],
    subtitle_streams=[],
    chapters=[],
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(db, *, name: str, root, kind="tv") -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name=name, kind=kind, root_paths=[str(root)], match_rules=[]
        )
        return row.id


async def _make_item(db, *, title: str, year: int, kind="tv") -> MediaItem:
    async with db.session() as session:
        item = MediaItem(
            kind=kind,
            tmdb_id=hash(title) % 100000,
            title=title,
            original_title=title,
            year=year,
            aliases=[],
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        session.add(MediaMetadata(media_item_id=item.id, genres=[], genre_ids=[]))
        await session.commit()
        return item


def _target_rule(watch, target, kind="tv") -> ImportWatch:
    return ImportWatch(
        source_path=str(watch),
        strategy="hardlink",
        library_id=None,
        kind=kind,
        target_path=str(target),
    )


@pytest.mark.asyncio
async def test_target_rule_transfers_without_library_ledger(db, tmp_path, monkeypatch):
    """路径规则：规范命名落 target_path，不写库台账、不生成 NFO。"""
    tv_root, watch, staging = tmp_path / "tv", tmp_path / "watch", tmp_path / "staging"
    watch.mkdir()
    staging.mkdir()
    await _make_library(db, name="剧集库", root=tv_root)
    item = await _make_item(db, title="某日剧", year=2023)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    async def identify(session, kind, watch_root, main, spec):
        return item

    monkeypatch.setattr(ingest_mod, "_identify", identify)

    entry = watch / "某日剧 (2023)"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"video")

    for _ in range(2):
        await ingest_mod._sweep_dir(_target_rule(watch, staging), None, execute_inline=True)

    # 落点：自定义目录下的库同款命名结构，库根不受任何影响
    final = staging / "某日剧 (2023)" / "Season 01" / "某日剧 (2023) - S01E01.mkv"
    assert final.exists()
    assert not (tv_root / "某日剧 (2023)").exists()
    # 过客语义：不写库台账、不生成 NFO
    assert not list((staging / "某日剧 (2023)").rglob("*.nfo"))
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert files == []
    assert record.status == IngestStatus.IMPORTED
    assert record.library_id is None
    assert "自动入账" in (record.message or "")


@pytest.mark.asyncio
async def test_target_rule_claimed_identity_keeps_wanted_open(db, tmp_path, monkeypatch):
    """认领身份与落点分离：文件落 target_path、工单保持在途不关单。"""
    from movieclaw_db.models import RuleSet, Subscription, WantedItem, WantedStatus
    from movieclaw_downloader import TorrentBrief

    tv_root, watch, staging = tmp_path / "tv", tmp_path / "watch", tmp_path / "staging"
    watch.mkdir()
    staging.mkdir()
    tv_id = await _make_library(db, name="剧集库", root=tv_root)
    item = await _make_item(db, title="某动画", year=2023)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    async with db.session() as session:
        rule_set = RuleSet(name="默认", spec={})
        session.add(rule_set)
        await session.commit()
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="tv", rule_set_id=rule_set.id, library_id=tv_id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add(
            WantedItem(
                subscription_id=sub.id,
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                status=WantedStatus.GRABBED,
                info_hash="hash1",
            )
        )
        await session.commit()

    brief = TorrentBrief(
        name="Cryptic.Anime.S01",
        content_name="Cryptic.Anime.S01",
        completed=True,
        info_hash="hash1",
    )

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    _stub_unit(monkeypatch, lambda file: (1, 1))

    entry = watch / "Cryptic.Anime.S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"video")

    await ingest_mod._sweep_dir(_target_rule(watch, staging), None, execute_inline=True)

    # 认领拿到了身份（规范命名），但落点由规则声明决定——不进订阅定格的库
    assert (staging / "某动画 (2023)" / "Season 01" / "某动画 (2023) - S01E01.mkv").exists()
    assert not (tv_root / "某动画 (2023)").exists()
    async with db.session() as session:
        wanted = (await session.execute(select(WantedItem))).scalar_one()
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    # "库里有"才算完成：文件尚在外部流转，工单保持在途
    assert wanted.status == WantedStatus.GRABBED
    assert files == []


@pytest.mark.asyncio
async def test_target_rule_validation(db, tmp_path):
    """校验：互斥、kind 必填、每 kind 一条、与库根/监听源重叠拒绝。"""
    tv_root = tmp_path / "tv"
    tv_id = await _make_library(db, name="剧集库", root=tv_root)
    watch1, watch2, staging = tmp_path / "w1", tmp_path / "w2", tmp_path / "staging"
    for d in (watch1, watch2, staging):
        d.mkdir()

    async with db.session() as session:
        service = ImportWatchConfigService(session)
        # library_id 与 target_path 互斥
        with pytest.raises(BadRequestException, match="不能同时"):
            await service.create(
                source_path=str(watch1),
                strategy="copy",
                library_id=tv_id,
                target_path=str(staging),
            )
        # kind 必填
        with pytest.raises(BadRequestException, match="媒体类型"):
            await service.create(
                source_path=str(watch1),
                strategy="copy",
                library_id=None,
                target_path=str(staging),
            )
        # 自定义目录不得与库根重叠
        with pytest.raises(BadRequestException, match="根路径重叠"):
            await service.create(
                source_path=str(watch1),
                strategy="copy",
                library_id=None,
                kind="tv",
                target_path=str(tv_root / "sub"),
            )
        # 自定义目录不得与本条规则的源目录重叠
        with pytest.raises(BadRequestException, match="监听源目录重叠"):
            await service.create(
                source_path=str(watch1),
                strategy="copy",
                library_id=None,
                kind="tv",
                target_path=str(watch1 / "out"),
            )
        # 正常创建
        row = await service.create(
            source_path=str(watch1),
            strategy="copy",
            library_id=None,
            kind="tv",
            target_path=str(staging),
        )
        assert row.target_path == str(staging) and row.kind == "tv"
        # 同 kind 第二条路径规则被拒
        with pytest.raises(BadRequestException, match="至多一条"):
            await service.create(
                source_path=str(watch2),
                strategy="copy",
                library_id=None,
                kind="tv",
                target_path=str(tmp_path / "staging2"),
            )
        # 新规则的源目录不得与既有规则的自定义目录重叠
        with pytest.raises(BadRequestException, match="自定义目录重叠"):
            await service.create(
                source_path=str(staging),
                strategy="copy",
                library_id=None,
                kind="movie",
                target_path=str(tmp_path / "staging3"),
            )


@pytest.mark.asyncio
async def test_target_rule_coexists_with_auto_and_dispatch_chain(db, tmp_path):
    """auto 与路径规则同 kind 可并存；投递查找链：专属 → auto → 路径。"""
    tv_root = tmp_path / "tv"
    tv_id = await _make_library(db, name="剧集库", root=tv_root)
    auto_watch, target_watch, staging = tmp_path / "auto", tmp_path / "tw", tmp_path / "staging"
    for d in (auto_watch, target_watch, staging):
        d.mkdir()

    async with db.session() as session:
        service = ImportWatchConfigService(session)
        path_rule = await service.create(
            source_path=str(target_watch),
            strategy="copy",
            library_id=None,
            kind="tv",
            target_path=str(staging),
        )
        # 只有路径规则时：查找链兜到它
        rule = await resolve_dispatch_rule(session, tv_id, kind="tv")
        assert rule is not None and rule.id == path_rule.id
        # auto 规则与路径规则同 kind 并存（互不挤占"每 kind 一条"名额）
        auto_rule = await service.create(
            source_path=str(auto_watch), strategy="copy", library_id=None, kind="tv"
        )
        # auto 优先于路径规则
        rule = await resolve_dispatch_rule(session, tv_id, kind="tv")
        assert rule is not None and rule.id == auto_rule.id


@pytest.mark.asyncio
async def test_target_rule_skips_units_already_in_library(db, tmp_path, monkeypatch):
    """季包补集的重处理：已回流入库的旧集不再搬进中转（防外部工具重复上传）。"""
    from movieclaw_db.models import FileSource

    tv_root, watch, staging = tmp_path / "tv", tmp_path / "watch", tmp_path / "staging"
    watch.mkdir()
    staging.mkdir()
    tv_id = await _make_library(db, name="剧集库", root=tv_root)
    item = await _make_item(db, title="某日剧", year=2023)
    # 第 1 集已作为 strm 回流入库（任一库在位即视为闭环完成）
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=tv_id,
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                file_path=str(tv_root / "某日剧 (2023)" / "某日剧 (2023) - S01E01.strm"),
                size_bytes=1,
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()

    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    async def identify(session, kind, watch_root, main, spec):
        return item

    monkeypatch.setattr(ingest_mod, "_identify", identify)

    entry = watch / "某日剧 (2023)"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"video")
    (entry / "ep2.mkv").write_bytes(b"video")

    for _ in range(2):
        await ingest_mod._sweep_dir(_target_rule(watch, staging), None, execute_inline=True)

    # 只有未回流的第 2 集进了中转；第 1 集被跳过
    season_dir = staging / "某日剧 (2023)" / "Season 01"
    assert (season_dir / "某日剧 (2023) - S01E02.mkv").exists()
    assert not (season_dir / "某日剧 (2023) - S01E01.mkv").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert "跳过" in (record.message or "")


@pytest.mark.asyncio
async def test_library_root_cannot_overlap_rule_target_path(db, tmp_path):
    """库侧反向校验：根路径不得落在监听导入自定义目录之内（或包含它）。"""
    from movieclaw_api.services.library.config import LibraryConfigService
    from movieclaw_media.models import MediaKind

    watch, staging = tmp_path / "watch", tmp_path / "staging"
    watch.mkdir()
    staging.mkdir()
    async with db.session() as session:
        await ImportWatchConfigService(session).create(
            source_path=str(watch),
            strategy="copy",
            library_id=None,
            kind="tv",
            target_path=str(staging),
        )
        with pytest.raises(BadRequestException, match="自定义目录重叠"):
            await LibraryConfigService(session).create(
                name="剧集库", kind=MediaKind.TV, root_paths=[str(staging / "sub")]
            )
