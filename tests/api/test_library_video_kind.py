"""「其他」类型媒体库（docs/design/library-other-kind.md）的回归测试。

覆盖的硬约束：
- 本地内容库扫描**零 TMDB 请求**（source 守卫）；文件逐个成为本地条目，
  sidecar NFO 有则读、无则按文件名；PLAIN 忽略规则只挡系统目录与 sample；
- 影视库里识别失败的文件拿临时本地身份：海报墙可见、待识别清单仍列出；
- 建库接口按 (kind, source) 定位能力档案，非法组合拒绝；
- 命名/订阅/转移等作用于「作品身份」的能力对本地内容库关闭；
- 监听导入落其他库走「原样落库」：不识别不改名，视频逐文件建条目；
- 下载目标为其他库时保存路径就是主根。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.ingest as ingest_mod
import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
import movieclaw_api.services.media_scrape as scrape_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.library import LibraryView
from movieclaw_api.services import jobs
from movieclaw_api.services.library import items as items_mod
from movieclaw_api.services.library.config import LibraryConfigService, derive_save_path
from movieclaw_api.services.library.organize import build_organize_plan
from movieclaw_api.services.library.profile import profile_for, profile_of
from movieclaw_api.services.library.scan import scan_library
from movieclaw_api.services.library.thumbs import build_thumbnail, primary_aspect
from movieclaw_api.services.library.transfer import assert_transferable
from movieclaw_api.services.media_probe import MediaSpec
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ImportWatch,
    IngestEntry,
    IngestStatus,
    Library,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    MediaSource,
)
from movieclaw_db.models.library_file import IdentitySource
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_FAKE_SPEC = MediaSpec(
    resolution="1080p",
    video_codec="h264",
    hdr=None,
    bit_depth=8,
    duration_seconds=95,
    bit_rate=None,
    frame_rate=30.0,
    color_space="BT.709",
    audio_streams=[],
    subtitle_streams=[],
    tag_date="2019-02-05",
)

_SIDECAR_NFO = """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>春节团圆饭</title>
  <plot>2019 年除夕全家合影与年夜饭记录。</plot>
  <premiered>2019-02-04</premiered>
  <runtime>12</runtime>
  <genre>家庭</genre>
  <tag>春节</tag>
</movie>
"""


class _NoTmdb:
    """任何 TMDB 请求都算失败：本地内容库的链路必须一次都不打。"""

    calls: list[str] = []


def _strict_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        _NoTmdb.calls.append(str(request.url))
        raise AssertionError(f"本地内容库不该请求 TMDB：{request.url}")

    return TmdbClient("test-key", transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'video.db'}")
    monkeypatch.setenv("MOVIECLAW_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    _NoTmdb.calls.clear()
    client = _strict_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    # 缩略图要真 ffmpeg；这里只验证链路，抓帧单独测
    monkeypatch.setattr(scrape_mod, "ensure_assets", _noop_assets)
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "_deferred", {})
    monkeypatch.setattr(ingest_mod, "_failed_retry", {})
    monkeypatch.setattr(ingest_mod, "_last_swept", {})
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


async def _noop_assets(_media_item_id: int, **_kwargs) -> None:
    return None


def _make_video_root(tmp_path: Path) -> Path:
    """其他库样本：带 NFO 的录像、裸文件名的录像、sample 与系统目录干扰。"""
    root = tmp_path / "media" / "home"
    (root / "2019").mkdir(parents=True)
    (root / "2019" / "春节团圆饭.mp4").write_bytes(b"v1")
    (root / "2019" / "春节团圆饭.nfo").write_text(_SIDECAR_NFO, encoding="utf-8")
    (root / "旅行 Vlog 第三期.mkv").write_bytes(b"v2")
    (root / "sample.mkv").write_bytes(b"s")  # PLAIN 规则唯一忽略的文件名
    (root / "@eaDir" / "thumb.mkv").parent.mkdir(parents=True)
    (root / "@eaDir" / "thumb.mkv").write_bytes(b"x")
    return root


async def _make_video_library(db, root: Path, **kwargs) -> Library:
    async with db.session() as session:
        return await LibraryConfigService(session).create(
            name=kwargs.pop("name", "家庭录像"),
            kind=MediaKind.VIDEO,
            root_paths=[str(root)],
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 能力档案与建库
# ---------------------------------------------------------------------------


def test_profile_table_is_capability_driven() -> None:
    video = profile_for(MediaKind.VIDEO)
    assert (video.source, video.scraped, video.naming, video.subscribable) == (
        MediaSource.LOCAL,
        False,
        False,
        False,
    )
    assert video.jellyfin_collection == "homevideos" and video.jellyfin_type == "Video"
    assert profile_for(MediaKind.MOVIE).source == MediaSource.TMDB
    # 没有识别策略的组合直接拒绝，而不是建出一个半残的库
    with pytest.raises(BadRequestException, match="不支持"):
        profile_for(MediaKind.MOVIE, "local")


async def test_create_video_library_defaults_to_local_source(db, tmp_path) -> None:
    root = _make_video_root(tmp_path)
    library = await _make_video_library(db, root)
    assert library.source == "local" and library.kind == "video"
    assert library.generate_thumbnails is True and library.exclude_from_home is False
    view = LibraryView.from_model(library)
    assert view.capabilities.scraped is False
    assert view.capabilities.default_aspect == pytest.approx(16 / 9, abs=1e-3)
    assert view.capabilities.jellyfin_collection == "homevideos"

    # 两个新开关按「不传=不改动」更新
    async with db.session() as session:
        updated = await LibraryConfigService(session).update(
            library.id,
            name=library.name,
            root_paths=list(library.root_paths),
            generate_thumbnails=False,
            exclude_from_home=True,
        )
        assert (updated.generate_thumbnails, updated.exclude_from_home) == (False, True)
        again = await LibraryConfigService(session).update(
            library.id, name=library.name, root_paths=list(library.root_paths)
        )
        assert (again.generate_thumbnails, again.exclude_from_home) == (False, True)


# ---------------------------------------------------------------------------
# 扫描：零 TMDB、逐文件条目、NFO/文件名标题、PLAIN 忽略
# ---------------------------------------------------------------------------


async def test_scan_video_library_makes_local_items_without_tmdb(db, tmp_path) -> None:
    root = _make_video_root(tmp_path)
    library = await _make_video_library(db, root)

    summary = await scan_library(library.id)
    assert _NoTmdb.calls == []  # 零 TMDB 请求
    assert summary.identified == 2 and summary.unidentified == 0

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert sorted(Path(r.file_path).name for r in rows) == [
            "旅行 Vlog 第三期.mkv",
            "春节团圆饭.mp4",
        ]
        # 一文件一条目，全部本地来源、单播放单元 (0,0)
        assert all(r.media_item_id is not None and r.unidentified_code is None for r in rows)
        assert {(r.season_number, r.episode_number) for r in rows} == {(0, 0)}
        items = {i.id: i for i in (await session.execute(select(MediaItem))).scalars().all()}
        assert len(items) == 2
        assert all(i.source == "local" and i.tmdb_id is None for i in items.values())
        by_name = {Path(r.file_path).name: r for r in rows}
        nfo_item = items[by_name["春节团圆饭.mp4"].media_item_id]
        raw_item = items[by_name["旅行 Vlog 第三期.mkv"].media_item_id]
        assert nfo_item.title == "春节团圆饭" and nfo_item.year == 2019
        assert by_name["春节团圆饭.mp4"].identity_source == IdentitySource.NFO.value
        # 没有 NFO：标题就是文件名本体，不做影视化解析
        assert raw_item.title == "旅行 Vlog 第三期"
        assert by_name["旅行 Vlog 第三期.mkv"].identity_source == IdentitySource.LOCAL.value
        meta = (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id == nfo_item.id)
            )
        ).scalar_one()
        assert meta.overview.startswith("2019 年除夕") and meta.runtime_minutes == 12
        assert str(meta.release_date) == "2019-02-04"
        assert "家庭" in meta.genres and "春节" in meta.genres

        # 待识别清单为空（本地库没有"认不出"这回事），统计以条目数为准
        assert await LibraryFileRepository(session).list_unidentified(library_id=library.id) == []
        fresh = await session.get(Library, library.id)
        assert fresh.stats_item_count == 2 and fresh.stats_unidentified_count == 0

        # 海报墙：来源与比例字段、按内容时间倒序——没有 NFO/容器日期的文件
        # 回落到 mtime（刚创建，最新），NFO 声明的 2019 年录像排后面
        wall = await items_mod.build_library_wall(session, library.id, sort="release_date")
        assert [v.title for v in wall] == ["旅行 Vlog 第三期", "春节团圆饭"]
        assert all(v.source == "local" and v.tmdb_id is None for v in wall)
        assert all(v.primary_aspect == pytest.approx(16 / 9, abs=1e-3) for v in wall)

    # 增量：再扫一次不重复建条目
    again = await scan_library(library.id)
    assert again.identified == 0 and _NoTmdb.calls == []
    async with db.session() as session:
        assert len((await session.execute(select(MediaItem))).scalars().all()) == 2


async def test_scan_video_library_absorbs_late_sidecar_nfo(db, tmp_path) -> None:
    """扫描后才补进来的 NFO 也要被吸收（秒过行重读 sidecar）。"""
    root = tmp_path / "media" / "home"
    root.mkdir(parents=True)
    video = root / "IMG_0001.mov"
    video.write_bytes(b"v")
    library = await _make_video_library(db, root)
    await scan_library(library.id)
    async with db.session() as session:
        item = (await session.execute(select(MediaItem))).scalar_one()
        assert item.title == "IMG_0001"

    video.with_suffix(".nfo").write_text(
        "<movie><title>宝宝第一次走路</title><year>2021</year></movie>", encoding="utf-8"
    )
    await scan_library(library.id)
    async with db.session() as session:
        item = (await session.execute(select(MediaItem))).scalar_one()
        assert item.title == "宝宝第一次走路" and item.year == 2021
        row = (await session.execute(select(LibraryFile))).scalar_one()
        assert row.identity_source == IdentitySource.NFO.value


# ---------------------------------------------------------------------------
# 影视库：识别失败的文件拿临时本地身份
# ---------------------------------------------------------------------------


async def test_movie_library_unidentified_file_gets_provisional_item(
    db, tmp_path, monkeypatch
) -> None:
    root = tmp_path / "media" / "movies"
    entry = root / "zzqx"
    entry.mkdir(parents=True)
    (entry / "zzqx.mkv").write_bytes(b"?")
    # 影视库识别链会打 TMDB：这里给一个"搜不到"的假客户端
    empty = TmdbClient(
        "k",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"results": []})),
    )
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: empty)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: empty)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    summary = await scan_library(library.id)
    assert summary.unidentified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalar_one()
        # 文件仍在待识别清单（人工认领通道不变）……
        assert row.unidentified_code is not None
        pending = await LibraryFileRepository(session).list_unidentified(library_id=library.id)
        assert [p.id for p in pending] == [row.id]
        # ……但已经挂着一个可见可播的临时本地条目
        assert row.media_item_id is not None
        item = await session.get(MediaItem, row.media_item_id)
        # 标题用条目目录名（用户在文件管理器里看到的），不写推断年份
        assert item.source == "local" and item.kind == "movie" and item.title == "zzqx"
        assert item.year is None
        # 主墙（正式条目口径）看不到它，独立的临时口径才有：两者不混排
        assert await items_mod.build_library_wall(session, library.id) == []
        wall = await items_mod.build_library_wall(session, library.id, identity="provisional")
        assert [(v.title, v.source, v.tmdb_id) for v in wall] == [("zzqx", "local", None)]
        assert await items_mod.build_library_index(session, library.id) == []
        # 搜索也只搜正式条目
        assert await items_mod.search_library_items(session, "zzqx") == {}
        # 临时条目不算「已识别」，统计口径与待识别数一致
        fresh = await session.get(Library, library.id)
        assert fresh.stats_item_count == 0 and fresh.stats_unidentified_count == 1
        # 影视库里的临时条目按 TMDB 海报惯例出 2:3 占位（没有抓帧尺寸时）
        assert primary_aspect(item, None, None) == pytest.approx(16 / 9, abs=1e-3)


# ---------------------------------------------------------------------------
# 作品身份能力对本地内容库关闭
# ---------------------------------------------------------------------------


async def test_identity_features_rejected_for_video_library(db, tmp_path) -> None:
    root = _make_video_root(tmp_path)
    library = await _make_video_library(db, root)
    async with db.session() as session:
        movie_lib = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(tmp_path / "movies")]
        )
        other_video = await LibraryRepository(session).create(
            name="录屏", kind="video", source="local", root_paths=[str(tmp_path / "rec")]
        )
        with pytest.raises(BadRequestException, match="本地内容库"):
            await build_organize_plan(session, library)
        with pytest.raises(BadRequestException, match="同类型"):
            assert_transferable(library, movie_lib)
        # 同形态同来源可以互转
        assert_transferable(library, other_video)

        from movieclaw_api.services.media_library import MediaLibraryService
        from movieclaw_api.services.subscription.core import SubscriptionService

        service = SubscriptionService(
            session, MediaLibraryService(session, discover_mod.get_tmdb_client())
        )
        with pytest.raises(BadRequestException, match="不能作为订阅"):
            await service._validate_library("video", library.id)
    assert profile_of(library).naming is False
    # 下载目标：没有条目目录约定，直接落主根
    assert derive_save_path(library, title="随便", year=2020) == str(root)


# ---------------------------------------------------------------------------
# 监听导入：原样落库
# ---------------------------------------------------------------------------


async def _sweep_twice(db, rule: ImportWatch, library: Library | None) -> None:
    for _ in range(2):
        await ingest_mod._sweep_dir(rule, library, execute_inline=True)


async def test_import_watch_raw_drops_into_video_library(db, tmp_path, monkeypatch) -> None:
    root = tmp_path / "home"
    root.mkdir()
    library = await _make_video_library(db, root)
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _p: _FAKE_SPEC)
    async with db.session() as session:
        session.add(ImportWatch(source_path=str(watch), strategy="copy", library_id=library.id))
        await session.commit()
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library.id)

    entry = watch / "2024 生日派对"
    entry.mkdir()
    (entry / "派对全程.mp4").write_bytes(b"a" * 10)
    (entry / "派对全程.nfo").write_text("<movie><title>生日派对</title></movie>", encoding="utf-8")
    (entry / "切蛋糕.mp4").write_bytes(b"b" * 10)
    (entry / ".DS_Store").write_bytes(b"junk")

    await _sweep_twice(db, rule, library)

    assert _NoTmdb.calls == []
    # 原样落库：目录名与文件名都不变，NFO 跟着走，隐藏文件不带
    dest = root / "2024 生日派对"
    assert sorted(p.name for p in dest.iterdir()) == ["切蛋糕.mp4", "派对全程.mp4", "派对全程.nfo"]
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        assert record.status == IngestStatus.IMPORTED and record.library_id == library.id
        assert "原样" in (record.message or "")
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(rows) == 2 and all(r.library_id == library.id for r in rows)
        assert all(r.source == "imported" and r.media_item_id is not None for r in rows)
        items = {i.id: i for i in (await session.execute(select(MediaItem))).scalars().all()}
        titles = sorted(items[r.media_item_id].title for r in rows)
        assert titles == ["切蛋糕", "生日派对"]  # NFO 的标题优先，其余用文件名
        assert all(i.source == "local" for i in items.values())
        fresh = await session.get(Library, library.id)
        assert fresh.stats_item_count == 2

    # 幂等：再扫一遍监听目录不会重复入库
    await _sweep_twice(db, rule, library)
    async with db.session() as session:
        assert len((await session.execute(select(LibraryFile))).scalars().all()) == 2


async def test_import_watch_auto_video_falls_to_default_video_library(
    db, tmp_path, monkeypatch
) -> None:
    """auto 规则声明为其他：没有收藏范围可路由，直接落该形态的默认库。"""
    root = tmp_path / "home"
    root.mkdir()
    library = await _make_video_library(db, root)
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _p: _FAKE_SPEC)
    (watch / "单文件录像.mp4").write_bytes(b"c" * 10)

    rule = ImportWatch(source_path=str(watch), strategy="copy", library_id=None, kind="video")
    await _sweep_twice(db, rule, None)

    assert (root / "单文件录像.mp4").is_file()  # 单文件条目直接落主根
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        assert record.status == IngestStatus.IMPORTED and record.library_id == library.id
        row = (await session.execute(select(LibraryFile))).scalar_one()
        assert row.file_path == str(root / "单文件录像.mp4")


async def test_import_watch_config_accepts_video_auto_rule(db, tmp_path) -> None:
    from movieclaw_api.services.import_watch_config import ImportWatchConfigService

    root = tmp_path / "home"
    library = await _make_video_library(db, root)
    assert library.is_default
    watch = tmp_path / "watch"
    watch.mkdir()
    async with db.session() as session:
        service = ImportWatchConfigService(session)
        row = await service.create(
            source_path=str(watch), strategy="copy", library_id=None, kind="video"
        )
        assert row.kind == "video"
        # 自定义目录同样可声明其他形态：原样搬进该目录、不涉及任何库
        staged = await service.create(
            source_path=str(tmp_path / "w2"),
            strategy="copy",
            library_id=None,
            kind="video",
            target_path=str(tmp_path / "out"),
        )
        assert staged.kind == "video" and staged.target_path == str(tmp_path / "out")
        with pytest.raises(BadRequestException, match="movie / tv / video"):
            await service.create(
                source_path=str(tmp_path / "w3"),
                strategy="copy",
                library_id=None,
                kind=None,
                target_path=str(tmp_path / "out2"),
            )


async def test_import_watch_custom_dir_video_raw_drops_without_library(
    db, tmp_path, monkeypatch
) -> None:
    """自定义目录 + 其他形态：不识别不改名原样搬进该目录，不建条目不写库台账。"""
    out = tmp_path / "out"
    out.mkdir()
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _p: _FAKE_SPEC)
    entry = watch / "随手拍"
    entry.mkdir()
    (entry / "片段一.mp4").write_bytes(b"a" * 10)
    (entry / "片段一.srt").write_text("1", encoding="utf-8")
    (watch / "单文件.mp4").write_bytes(b"b" * 10)

    rule = ImportWatch(
        source_path=str(watch), strategy="copy", library_id=None, kind="video", target_path=str(out)
    )
    await _sweep_twice(db, rule, None)

    assert _NoTmdb.calls == []
    assert sorted(p.name for p in (out / "随手拍").iterdir()) == ["片段一.mp4", "片段一.srt"]
    assert (out / "单文件.mp4").is_file()
    async with db.session() as session:
        records = list((await session.execute(select(IngestEntry))).scalars().all())
        assert len(records) == 2
        assert all(r.status == IngestStatus.IMPORTED and r.library_id is None for r in records)
        assert all("自定义目录" in (r.message or "") for r in records)
        # 过客文件：不建本地条目、不写库文件台账
        assert (await session.execute(select(LibraryFile))).scalars().all() == []
        assert (await session.execute(select(MediaItem))).scalars().all() == []


# ---------------------------------------------------------------------------
# 缩略图：真 ffmpeg 抓帧（环境没有 ffmpeg 时跳过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg")
def test_build_thumbnail_grabs_frame_and_reports_size(tmp_path) -> None:
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=320x180:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video),
        ],
        check=True,
        timeout=60,
    )
    dest = tmp_path / "assets" / "1" / "poster.jpg"
    size = build_thumbnail(video, dest, duration_seconds=3)
    assert size == (320, 180) and dest.is_file() and dest.stat().st_size > 0

    # sidecar 图优先于抓帧
    side = tmp_path / "clip-thumb.jpg"
    shutil.copyfile(dest, side)
    dest2 = tmp_path / "assets" / "2" / "poster.jpg"
    assert build_thumbnail(video, dest2, duration_seconds=3) == (320, 180)
    # 原盘目录没有单一文件可抓
    assert build_thumbnail(video, dest2, duration_seconds=3, is_disc=True) is None
