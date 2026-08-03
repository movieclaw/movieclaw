"""媒体库 L4 与识别增强的测试。

覆盖：NFO 写出（不覆盖既有）、媒体服务器通知（成功/未配置/失败不抛）、
原盘目录识别（BDMV 整体一个条目）、电影时长消歧（歧义候选 ±2 分钟
唯一命中）、watchdog 实时监控（文件事件 → 去抖 → 增量扫描）。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.library.watch as watch_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.nfo import write_entry_nfo, write_full_nfo
from movieclaw_api.services.library.scan import scan_library
from movieclaw_api.services.media_probe import MediaSpec
from movieclaw_api.services.media_server_notify import notify_media_server_refresh
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_MOVIE_DETAIL = {
    "external_ids": {},
    "alternative_titles": {"titles": []},
    "translations": {"translations": []},
    "status": "Released",
}

_ROUTES = {
    "/3/movie/400": {
        "id": 400,
        "title": "阿凡达",
        "original_title": "Avatar",
        "release_date": "2009-12-18",
        **_MOVIE_DETAIL,
    },
    "/3/movie/401": {
        "id": 401,
        "title": "两生花",
        "original_title": "Two Lives",
        "release_date": "1991-05-15",
        "runtime": 120,
        **_MOVIE_DETAIL,
    },
    "/3/movie/402": {
        "id": 402,
        "title": "两生花",
        "original_title": "Double Life",
        "release_date": "2011-03-02",
        "runtime": 90,
        **_MOVIE_DETAIL,
    },
}


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/search/movie":
            query = request.url.params.get("query", "")
            if "阿凡达" in query:
                results = [
                    {
                        "id": 400,
                        "title": "阿凡达",
                        "original_title": "Avatar",
                        "release_date": "2009-12-18",
                    }
                ]
            elif query.startswith("两生"):
                # 同名双候选：无年份时靠时长消歧
                results = [
                    {"id": 401, "title": "两生花", "release_date": "1991-05-15"},
                    {"id": 402, "title": "两生花", "release_date": "2011-03-02"},
                ]
            else:
                results = []
            return httpx.Response(200, json={"results": results})
        if path == "/3/search/tv":
            return httpx.Response(200, json={"results": []})
        payload = _ROUTES.get(path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'l4.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    client = _fake_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    # 测试文件都是刚创建的，关掉"疑似写入中"静默窗口（该行为有专门测试覆盖）
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# NFO 写出
# ---------------------------------------------------------------------------


def test_write_entry_nfo_and_respect_existing(tmp_path) -> None:
    item = MediaItem(
        kind="movie",
        tmdb_id=42,
        imdb_id="tt0042",
        title="某电影",
        original_title="Some Movie",
        year=2020,
        aliases=[],
    )
    entry = tmp_path / "某电影 (2020)"
    entry.mkdir()
    write_entry_nfo(entry, item)
    nfo = entry / "movie.nfo"
    text = nfo.read_text(encoding="utf-8")
    assert "<tmdbid>42</tmdbid>" in text and 'type="imdb"' in text

    # 既有 NFO 绝不覆盖（尊重 TMM/Emby 的刮削成果）
    nfo.write_text("precious", encoding="utf-8")
    write_entry_nfo(entry, item)
    assert nfo.read_text(encoding="utf-8") == "precious"


def test_full_nfo_refresh_alignment(tmp_path) -> None:
    """完整 NFO 的刷新对齐语义（2026-08-04 翻转）：有档案即重写，无档案不降级。"""
    from datetime import UTC, datetime

    from movieclaw_db.models import MediaMetadata

    item = MediaItem(
        kind="movie",
        tmdb_id=42,
        imdb_id=None,
        title="某电影",
        original_title="Some Movie",
        year=2020,
        aliases=[],
    )
    meta = MediaMetadata(
        media_item_id=1,
        overview="新简介",
        genres=["剧情"],
        scraped_at=datetime.now(UTC),
    )
    entry = tmp_path / "某电影 (2020)"
    entry.mkdir()
    nfo = entry / "movie.nfo"

    # 既有富 NFO（同 tmdbid）+ 新档案 → 对齐重写
    nfo.write_text(
        "<movie><tmdbid>42</tmdbid><plot>旧简介</plot></movie>", encoding="utf-8"
    )
    write_full_nfo(entry, item, meta)
    text = nfo.read_text(encoding="utf-8")
    assert "新简介" in text and "旧简介" not in text

    # 内容无变化 → 不落盘（mtime 不动，watchdog/播放器不受扰）
    before = nfo.stat().st_mtime_ns
    write_full_nfo(entry, item, meta)
    assert nfo.stat().st_mtime_ns == before

    # 无档案 → 绝不覆盖既有内容（不能拿身份档降级刮削成果）
    nfo.write_text("<movie><tmdbid>42</tmdbid><plot>precious</plot></movie>", encoding="utf-8")
    write_full_nfo(entry, item, None)
    assert "precious" in nfo.read_text(encoding="utf-8")

    # 声明了不同 tmdbid → 不写（留给认领纠错链路）
    nfo.write_text("<movie><tmdbid>99</tmdbid><plot>other</plot></movie>", encoding="utf-8")
    write_full_nfo(entry, item, meta)
    assert "other" in nfo.read_text(encoding="utf-8")


def test_episode_nfo_refresh_alignment(tmp_path) -> None:
    """分集 NFO 对齐重写；骨架档案行（无简介无日期）不覆盖第三方成果。"""
    from datetime import date

    from movieclaw_api.services.library.nfo import write_episode_nfo
    from movieclaw_db.models import MediaEpisode

    video = tmp_path / "S01E01.mkv"
    video.write_bytes(b"x")
    nfo = tmp_path / "S01E01.nfo"
    rich = MediaEpisode(
        media_item_id=1,
        season_number=1,
        episode_number=1,
        name="Pilot",
        overview="新的分集简介",
        air_date=date(2020, 1, 1),
    )
    # 既有文件 + 有实质档案 → 对齐重写
    nfo.write_text("<episodedetails><plot>旧</plot></episodedetails>", encoding="utf-8")
    write_episode_nfo(video, rich)
    assert "新的分集简介" in nfo.read_text(encoding="utf-8")

    # 骨架行（无简介无日期）→ 不碰既有文件
    nfo.write_text("<episodedetails><plot>rich</plot></episodedetails>", encoding="utf-8")
    skeleton = MediaEpisode(media_item_id=1, season_number=1, episode_number=2, name="E2")
    write_episode_nfo(video, skeleton)
    assert "rich" in nfo.read_text(encoding="utf-8")


def test_entry_nfo_refuses_kind_conflicting_dir(tmp_path) -> None:
    """目录里已有 tvshow.nfo 时，绝不再写 movie.nfo（反之亦然）。

    这是身份判错的强信号（TMDB 的 movie/tv 是两套独立 id 空间，库类型选错
    时同一个数字在另一侧照样能取到条目），而写错的 NFO 会被下次识别当权威
    读回、自我固化——宁可不写。
    """
    item = MediaItem(
        kind="movie",
        tmdb_id=61746,
        imdb_id=None,
        title="毫不相干的电影",
        original_title="Unrelated Movie",
        year=2006,
        aliases=[],
    )
    entry = tmp_path / "某剧 (2014)"
    entry.mkdir()
    (entry / "tvshow.nfo").write_text("<tvshow><tmdbid>61746</tmdbid></tvshow>", encoding="utf-8")

    write_entry_nfo(entry, item)
    write_full_nfo(entry, item, None)
    assert not (entry / "movie.nfo").exists()


# ---------------------------------------------------------------------------
# 媒体服务器通知
# ---------------------------------------------------------------------------


async def test_media_server_notify(monkeypatch) -> None:
    # 未配置：no-op
    monkeypatch.setenv("MEDIA_SERVER_URL", "")
    get_settings.cache_clear()
    assert await notify_media_server_refresh() is False

    # 已配置：POST /Library/Refresh 带 token；失败（服务器 500）不抛只返回 False
    calls: list[tuple[str, str]] = []

    class _FakeClient:
        def __init__(self, ok: bool):
            self._ok = ok

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            calls.append((url, (headers or {}).get("X-Emby-Token", "")))
            return httpx.Response(200 if self._ok else 500, request=httpx.Request("POST", url))

    monkeypatch.setenv("MEDIA_SERVER_URL", "http://emby:8096/")
    monkeypatch.setenv("MEDIA_SERVER_TOKEN", "tok123")
    get_settings.cache_clear()
    # 实现走统一出口层（transport=egress_transport(...)），假客户端收下即弃
    monkeypatch.setattr(
        "movieclaw_api.services.media_server_notify.httpx.AsyncClient",
        lambda timeout, transport=None: _FakeClient(ok=True),
    )
    assert await notify_media_server_refresh() is True
    assert calls[-1] == ("http://emby:8096/Library/Refresh", "tok123")

    monkeypatch.setattr(
        "movieclaw_api.services.media_server_notify.httpx.AsyncClient",
        lambda timeout, transport=None: _FakeClient(ok=False),
    )
    assert await notify_media_server_refresh() is False  # 失败不抛
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 原盘识别 + 时长消歧
# ---------------------------------------------------------------------------


async def test_scan_recognizes_bluray_disc(db, tmp_path) -> None:
    root = tmp_path / "movies"
    stream = root / "阿凡达 (2009)" / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00001.m2ts").write_bytes(b"x" * 100)
    (stream / "00002.m2ts").write_bytes(b"y" * 10)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.scanned == 1 and summary.identified == 1  # 整盘一个条目，不下钻

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.container == "bluray"
        assert row.file_path.endswith("阿凡达 (2009)")
        assert row.size_bytes == 110  # 盘内文件总大小
        assert row.media_item_id is not None


async def test_movie_runtime_disambiguation(db, tmp_path, monkeypatch) -> None:
    """同名双候选、文件名无年份：实测 120 分钟 → 唯一命中 runtime=120 的候选。"""
    root = tmp_path / "movies"
    folder = root / "两生花"
    folder.mkdir(parents=True)
    (folder / "两生花.1080p.mkv").write_bytes(b"movie")
    monkeypatch.setattr(
        scan_mod,
        "probe_media",
        lambda _path: MediaSpec(
            resolution="1080p",
            video_codec="hevc",
            hdr=None,
            bit_depth=10,
            duration_seconds=120 * 60 + 30,
            bit_rate=None,
        ),
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1 and summary.unidentified == 0

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item.tmdb_id == 401  # runtime 120 的那部，而非 90 的同名片


# ---------------------------------------------------------------------------
# watchdog 实时监控
# ---------------------------------------------------------------------------


async def test_watcher_triggers_incremental_scan(db, tmp_path, monkeypatch) -> None:
    pytest.importorskip("watchdog")
    # 缩短去抖窗口，测试秒级完成
    monkeypatch.setattr(watch_mod, "_QUIET_SECONDS", 0.2)
    monkeypatch.setattr(watch_mod, "_MAX_WAIT_SECONDS", 2.0)

    root = tmp_path / "movies"
    root.mkdir()
    async with db.session() as session:
        await LibraryRepository(session).create(name="电影库", kind="movie", root_paths=[str(root)])

    watcher = watch_mod.LibraryWatcher()
    await watcher.start()
    try:
        folder = root / "阿凡达 (2009)"
        folder.mkdir()
        (folder / "Avatar.2009.1080p.mkv").write_bytes(b"movie")
        # 等事件 → 去抖 → 扫描落账（轮询最多 10 秒）
        for _ in range(100):
            await asyncio.sleep(0.1)
            async with db.session() as session:
                rows = list((await session.execute(select(LibraryFile))).scalars().all())
            if rows:
                break
        assert rows, "监控未在 10 秒内触发增量扫描"
        assert rows[0].file_path.endswith("Avatar.2009.1080p.mkv")
    finally:
        await watcher.stop()


def test_watcher_ignores_read_only_events() -> None:
    """只读事件绝不能触发扫描——否则扫描补探（ffprobe 读文件）产生的
    opened/closed_no_write 事件会喂回监控，形成「扫描 → 再扫描」的自激循环。"""
    events = pytest.importorskip("watchdog.events")

    path = "/lib/movie.mkv"
    # 读文件产生的事件：忽略
    assert not watch_mod._is_relevant_event(events.FileOpenedEvent(path))
    assert not watch_mod._is_relevant_event(events.FileClosedNoWriteEvent(path))
    # 视频文件内容真变化的事件：照常触发
    assert watch_mod._is_relevant_event(events.FileCreatedEvent(path))
    assert watch_mod._is_relevant_event(events.FileModifiedEvent(path))
    assert watch_mod._is_relevant_event(events.FileDeletedEvent(path))
    assert watch_mod._is_relevant_event(events.FileMovedEvent(path, "/lib/renamed.mkv"))
    # 目录：仅移动/删除有意义（新建/修改由目录内的文件事件另行上报）
    assert not watch_mod._is_relevant_event(events.DirModifiedEvent("/lib/dir"))
    assert watch_mod._is_relevant_event(events.DirDeletedEvent("/lib/dir"))
    assert watch_mod._is_relevant_event(events.DirMovedEvent("/lib/dir", "/lib/dir2"))


def test_watcher_ignores_non_video_file_events() -> None:
    """非视频文件的写事件不触发扫描——刮削/整库刷新写 NFO 和图片、下载器
    写 .!qB 半成品都发生在库目录里，不滤掉的话「正在扫描」会在刷新与
    下载期间反复闪现（也是一种系统自触发）。"""
    events = pytest.importorskip("watchdog.events")

    # 刮削产物与下载器半成品：忽略
    assert not watch_mod._is_relevant_event(events.FileCreatedEvent("/lib/movie.nfo"))
    assert not watch_mod._is_relevant_event(events.FileModifiedEvent("/lib/poster.jpg"))
    assert not watch_mod._is_relevant_event(events.FileModifiedEvent("/lib/movie.mkv.!qB"))
    assert not watch_mod._is_relevant_event(events.FileCreatedEvent("/lib/sub.chs.srt"))
    # 下载完成改名成视频（终点是视频扩展名）：触发
    assert watch_mod._is_relevant_event(
        events.FileMovedEvent("/lib/movie.mkv.!qB", "/lib/movie.mkv")
    )
    # 视频被改走（起点是视频扩展名）：同样触发
    assert watch_mod._is_relevant_event(events.FileMovedEvent("/lib/movie.mkv", "/lib/movie.bak"))
