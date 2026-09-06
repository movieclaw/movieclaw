"""监听导入「自动路由」模式的测试（docs/design/library-routing.md R3）。

覆盖：
- 同一个 auto 监听目录里的动画剧/普通剧按收藏范围分流到两个库，
  结论带路由理由；
- 订阅按 info_hash 认领的条目沿用订阅**定格**的库（不重新路由）；
- 识别失败的 auto 条目落账无归属库（幂等退避不受影响）；
- auto 规则校验：kind 必填、每 kind 至多一条、无可路由库拒绝创建；
- resolve_dispatch_rule：库专属规则优先，同 kind auto 规则兜底。
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

_ANIME_RULES = [{"field": "genres", "op": "any_of", "values": [16]}]


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'auto.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(db, *, name: str, root, rules: list | None = None, kind="tv") -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name=name, kind=kind, root_paths=[str(root)], match_rules=rules or []
        )
        return row.id


async def _make_item(db, *, title: str, year: int, genre_ids: list[int], kind="tv") -> MediaItem:
    """建条目 + 刮削档案（genre_ids 是路由事实的第一级来源）。"""
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
        session.add(
            MediaMetadata(
                media_item_id=item.id,
                genres=[],
                genre_ids=genre_ids,
                origin_countries=["JP"],
            )
        )
        await session.commit()
        return item


def _auto_rule(watch, kind="tv") -> ImportWatch:
    return ImportWatch(source_path=str(watch), strategy="hardlink", library_id=None, kind=kind)


async def _sweep_auto_twice(db, watch, kind="tv") -> None:
    for _ in range(2):
        await ingest_mod._sweep_dir(_auto_rule(watch, kind), None, execute_inline=True)


@pytest.mark.asyncio
async def test_auto_dir_routes_by_match_rules(db, tmp_path, monkeypatch):
    """同一 auto 目录：动画剧进动漫库、普通剧进默认剧集库，结论带理由。"""
    tv_root, anime_root, watch = tmp_path / "tv", tmp_path / "anime", tmp_path / "watch"
    watch.mkdir()
    await _make_library(db, name="剧集库", root=tv_root)  # 首库自动默认
    await _make_library(db, name="动漫库", root=anime_root, rules=_ANIME_RULES)
    anime = await _make_item(db, title="某动画", year=2023, genre_ids=[16])
    drama = await _make_item(db, title="某日剧", year=2023, genre_ids=[18])
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    async def identify(session, kind, watch_root, main, spec):
        return anime if "某动画" in str(main) else drama

    monkeypatch.setattr(ingest_mod, "_identify", identify)

    for title in ("某动画", "某日剧"):
        entry = watch / f"{title} (2023)"
        entry.mkdir()
        (entry / "ep1.mkv").write_bytes(b"video")

    await _sweep_auto_twice(db, watch)

    assert (anime_root / "某动画 (2023)" / "Season 01" / "某动画 (2023) - S01E01.mkv").exists()
    assert (tv_root / "某日剧 (2023)" / "Season 01" / "某日剧 (2023) - S01E01.mkv").exists()
    async with db.session() as session:
        records = {
            r.entry_path: r for r in (await session.execute(select(IngestEntry))).scalars().all()
        }
    anime_record = records[str(watch / "某动画 (2023)")]
    assert anime_record.status == IngestStatus.IMPORTED
    assert "命中「动漫库」" in (anime_record.message or "")
    drama_record = records[str(watch / "某日剧 (2023)")]
    assert "默认库「剧集库」" in (drama_record.message or "")


@pytest.mark.asyncio
async def test_auto_claimed_entry_uses_pinned_library(db, tmp_path, monkeypatch):
    """订阅认领的条目沿用定格库：即使收藏范围会把它路由去别处。"""
    from movieclaw_db.models import RuleSet, Subscription, WantedItem, WantedStatus
    from movieclaw_downloader import TorrentBrief

    tv_root, anime_root, watch = tmp_path / "tv", tmp_path / "anime", tmp_path / "watch"
    watch.mkdir()
    tv_id = await _make_library(db, name="剧集库", root=tv_root)
    await _make_library(db, name="动漫库", root=anime_root, rules=_ANIME_RULES)
    # 动画剧，但用户在订阅上手选了「剧集库」并已定格
    item = await _make_item(db, title="某动画", year=2023, genre_ids=[16])
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

    await ingest_mod._sweep_dir(
        _auto_rule(watch), None, execute_inline=True
    )  # 权威完成信号，单轮处理

    # 落在订阅定格的剧集库，而不是收藏范围指向的动漫库
    assert (tv_root / "某动画 (2023)" / "Season 01" / "某动画 (2023) - S01E01.mkv").exists()
    assert not (anime_root / "某动画 (2023)").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert "订阅指定的「剧集库」" in (record.message or "")
    assert files and all(f.library_id == tv_id for f in files)


@pytest.mark.asyncio
async def test_auto_unidentified_entry_recorded_without_library(db, tmp_path, monkeypatch):
    """识别不出的 auto 条目：台账落账为待处理（幂等依赖它），无归属库。"""
    tv_root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    await _make_library(db, name="剧集库", root=tv_root)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    entry = watch / "无法识别的东西"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"video")

    await _sweep_auto_twice(db, watch)

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.PENDING
    assert record.library_id is None
    assert "无法自动识别" in (record.message or "")


@pytest.mark.asyncio
async def test_auto_rule_validation(db, tmp_path):
    """auto 规则校验：kind 必填、每 kind 至多一条、无可路由库拒绝。"""
    tv_root = tmp_path / "tv"
    await _make_library(db, name="剧集库", root=tv_root)
    watch1, watch2 = tmp_path / "w1", tmp_path / "w2"
    watch1.mkdir()
    watch2.mkdir()

    async with db.session() as session:
        service = ImportWatchConfigService(session)
        # kind 缺失
        with pytest.raises(BadRequestException, match="媒体类型"):
            await service.create(source_path=str(watch1), strategy="copy", library_id=None)
        # 无可路由的电影库（只有剧集库）
        with pytest.raises(BadRequestException, match="电影"):
            await service.create(
                source_path=str(watch1), strategy="copy", library_id=None, kind="movie"
            )
        # 正常创建 tv 的 auto 规则
        row = await service.create(
            source_path=str(watch1), strategy="copy", library_id=None, kind="tv"
        )
        assert row.library_id is None and row.kind == "tv"
        # 同 kind 第二条 auto 规则被拒
        with pytest.raises(BadRequestException, match="至多一条"):
            await service.create(
                source_path=str(watch2), strategy="copy", library_id=None, kind="tv"
            )


@pytest.mark.asyncio
async def test_resolve_dispatch_rule_prefers_library_rule_then_auto(db, tmp_path):
    """投递目录：库专属规则 → 同 kind auto 规则 → None。"""
    tv_root, anime_root = tmp_path / "tv", tmp_path / "anime"
    tv_id = await _make_library(db, name="剧集库", root=tv_root)
    anime_id = await _make_library(db, name="动漫库", root=anime_root, rules=_ANIME_RULES)
    own_watch, auto_watch = tmp_path / "own", tmp_path / "auto"
    own_watch.mkdir()
    auto_watch.mkdir()

    async with db.session() as session:
        service = ImportWatchConfigService(session)
        await service.create(source_path=str(own_watch), strategy="copy", library_id=tv_id)
        await service.create(
            source_path=str(auto_watch), strategy="copy", library_id=None, kind="tv"
        )
        # 剧集库有专属规则：优先
        rule = await resolve_dispatch_rule(session, tv_id, kind="tv")
        assert rule is not None and rule.source_path == str(own_watch)
        # 动漫库没有专属规则：落同 kind 的 auto 目录（混合下载目录闭环）
        rule = await resolve_dispatch_rule(session, anime_id, kind="tv")
        assert rule is not None and rule.source_path == str(auto_watch)
        # 没有任何规则可用
        assert await resolve_dispatch_rule(session, None, kind="movie") is None
