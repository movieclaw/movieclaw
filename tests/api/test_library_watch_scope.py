"""监听链路性能改造的行为守卫（只该改快慢、不该改结果）。

覆盖五项改造：
1. ``_walk_videos`` 换 os.scandir：产出与旧语义一致（原盘/隐藏/忽略目录、
   sample/花絮过滤、iso、根层散文件、dir_files 清单），每个目录只列一次，
   不再走 ``Path.iterdir``；
2. 范围扫描（scope_paths）：范围内入账/丢失标记照旧，范围外台账行绝不动；
   范围解析存疑（条目消失、根级文件、根不匹配）退回整库；自动清理在
   范围轮整轮跳过；
3. watch 事件 → 范围传递：事件路径折算成根下第一级条目交给扫描；扫描
   进行中收到的事件不丢弃，扫完补触发；
4. 差量重建：未变根不重建（watch 对象同一性保持）、增删只动差集、单根
   建 watch 失败不拖累其余、共享根的两个库互不拆台；
5. 观察者线程侧事件合并 + 监听初始化不阻塞启动。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time as time_mod

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.ingest as ingest_mod
import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.library.watch as watch_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.scan import scan_library
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import Library, LibraryFile
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"


def _body(tmdb_id: int) -> dict:
    return {
        "id": tmdb_id,
        "title": f"影片{tmdb_id}",
        "original_title": f"M{tmdb_id}",
        "release_date": "2020-01-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "images": {"posters": [], "backdrops": []},
        "release_dates": {"results": []},
        "credits": {"cast": [], "crew": []},
    }


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'watch-scope.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()

    def handler(request: httpx.Request) -> httpx.Response:
        seg = request.url.path.rstrip("/").split("/")[-1]
        if seg.isdigit():
            return httpx.Response(200, json=_body(int(seg)))
        return httpx.Response(200, json={"results": []})

    client = TmdbClient(api_key=_KEY, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _movie_entry(root, tmdb_id: int):
    """一个「NFO 写死 tmdbid」的电影条目目录（识别零歧义、不依赖 NER）。"""
    entry = root / f"影片{tmdb_id} (2020)"
    entry.mkdir(parents=True)
    (entry / f"影片{tmdb_id}.1080p.mkv").write_bytes(b"x" * 64)
    (entry / "movie.nfo").write_text(
        f"<movie><title>影片{tmdb_id}</title><tmdbid>{tmdb_id}</tmdbid></movie>",
        encoding="utf-8",
    )
    return entry


async def _rows(db, library_id: int) -> dict[str, LibraryFile]:
    async with db.session() as session:
        rows = (
            (await session.execute(select(LibraryFile).where(LibraryFile.library_id == library_id)))
            .scalars()
            .all()
        )
    return {r.file_path: r for r in rows}


# ---------------------------------------------------------------------------
# 1. _walk_videos：os.scandir 改写的语义等价 + 成本守卫
# ---------------------------------------------------------------------------


def _build_walk_tree(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "loose.mkv").write_bytes(b"x")
    (root / "loose.sample.mkv").write_bytes(b"x")  # sample 标记不入
    (root / "readme.txt").write_bytes(b"x")
    (root / "bare.iso").write_bytes(b"x")
    movie = root / "影片A (2020)"
    movie.mkdir()
    (movie / "A.2020.mkv").write_bytes(b"x")
    (movie / "A.2020.chs.srt").write_bytes(b"x")
    (movie / "A-trailer.mkv").write_bytes(b"x")  # extras 后缀不入
    (movie / "movie.nfo").write_bytes(b"x")
    season = root / "剧集 (2024)" / "Season 01"
    season.mkdir(parents=True)
    (season / "E01.mkv").write_bytes(b"x")
    (season / "E02.mkv").write_bytes(b"x")
    bd = root / "原盘 (2021)"
    (bd / "BDMV" / "STREAM").mkdir(parents=True)
    (bd / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"x")
    dvd = root / "DVD (1999)"
    (dvd / "VIDEO_TS").mkdir(parents=True)
    (dvd / "VIDEO_TS" / "VTS_01_1.VOB").write_bytes(b"x")
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "h.mkv").write_bytes(b"x")
    ignored = root / "extras"
    ignored.mkdir()
    (ignored / "x.mkv").write_bytes(b"x")
    return root


def test_walk_videos_matches_legacy_semantics(tmp_path, monkeypatch) -> None:
    """scandir 改写只是省系统调用，产出集合必须与旧遍历完全一致。

    顺带锁死「不再用 Path.iterdir」：一旦有人改回去，这里立刻变红。
    """
    from pathlib import Path

    root = _build_walk_tree(tmp_path)

    def _boom(self):
        raise AssertionError("遍历不应再调用 Path.iterdir（每个条目会多付 stat）")

    monkeypatch.setattr(Path, "iterdir", _boom)
    unreadable: list[str] = []
    dir_files: dict[str, list[str]] = {}
    got = {(str(p), disc) for p, disc in scan_mod._walk_videos(root, unreadable, dir_files)}

    assert got == {
        (str(root / "loose.mkv"), False),
        (str(root / "bare.iso"), False),
        (str(root / "影片A (2020)" / "A.2020.mkv"), False),
        (str(root / "剧集 (2024)" / "Season 01" / "E01.mkv"), False),
        (str(root / "剧集 (2024)" / "Season 01" / "E02.mkv"), False),
        (str(root / "原盘 (2021)"), True),
        (str(root / "DVD (1999)"), True),
    }
    assert unreadable == []
    # dir_files：走过的目录都有完整文件清单（含非视频，字幕匹配靠它）；
    # 原盘目录整体是一个条目，不记清单、更不下钻
    assert sorted(dir_files[str(root)]) == [
        "bare.iso",
        "loose.mkv",
        "loose.sample.mkv",
        "readme.txt",
    ]
    assert "A.2020.chs.srt" in dir_files[str(root / "影片A (2020)")]
    assert str(root / "原盘 (2021)") not in dir_files
    assert str(root / "原盘 (2021)" / "BDMV") not in dir_files


def test_walk_videos_lists_each_dir_exactly_once(tmp_path, monkeypatch) -> None:
    """成本守卫：每个走到的目录恰好一次 scandir，原盘/忽略/隐藏目录不重复列。

    树里可下钻目录 = 根、影片A、剧集、Season 01；原盘与 DVD 各列一次用于
    整体判定；.hidden 与 extras 在下钻前剪掉。共 6 次。
    """
    root = _build_walk_tree(tmp_path)
    real_scandir = os.scandir
    listed: list[str] = []

    def counting_scandir(path, *args, **kwargs):
        listed.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(scan_mod.os, "scandir", counting_scandir)
    list(scan_mod._walk_videos(root))
    assert len(listed) == 6, f"目录被重复列了：{sorted(listed)}"
    assert len(set(listed)) == 6


def test_walk_videos_only_top_restricts_first_level_only(tmp_path) -> None:
    """only_top 只限制根下第一级；深层不限制；根层清单仍完整（字幕匹配用）。"""
    root = _build_walk_tree(tmp_path)
    dir_files: dict[str, list[str]] = {}
    got = {
        (str(p), disc)
        for p, disc in scan_mod._walk_videos(
            root, None, dir_files, only_top={"剧集 (2024)", "loose.mkv", "原盘 (2021)"}
        )
    }
    assert got == {
        (str(root / "loose.mkv"), False),
        (str(root / "剧集 (2024)" / "Season 01" / "E01.mkv"), False),
        (str(root / "剧集 (2024)" / "Season 01" / "E02.mkv"), False),
        (str(root / "原盘 (2021)"), True),  # 范围条目是原盘时仍整体一个条目
    }
    # 根目录本就要列一次，范围不缩减它的清单
    assert sorted(dir_files[str(root)]) == [
        "bare.iso",
        "loose.mkv",
        "loose.sample.mkv",
        "readme.txt",
    ]


def test_walk_videos_collects_unreadable_dirs(tmp_path, monkeypatch) -> None:
    """列不动的目录记进 unreadable（自动清理据此让路），其余照常产出。"""
    root = _build_walk_tree(tmp_path)
    bad = str(root / "剧集 (2024)" / "Season 01")
    real_scandir = os.scandir

    def flaky_scandir(path, *args, **kwargs):
        if os.fspath(path) == bad:
            raise PermissionError(13, "mock: 网络挂载抖动")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(scan_mod.os, "scandir", flaky_scandir)
    unreadable: list[str] = []
    got = {str(p) for p, _disc in scan_mod._walk_videos(root, unreadable)}
    assert unreadable == [bad]
    assert str(root / "影片A (2020)" / "A.2020.mkv") in got
    assert not any(p.startswith(bad) for p in got)


# ---------------------------------------------------------------------------
# 2. 范围扫描：范围内照旧、范围外不动、存疑退回整库
# ---------------------------------------------------------------------------


async def test_resolve_scope_mapping_and_fallbacks(tmp_path) -> None:
    root = tmp_path / "movies"
    entry = root / "影片X (2020)"
    entry.mkdir(parents=True)
    library = Library(name="库", kind="movie", root_paths=[str(root)])

    # 正常：现存目录且在根第一级 → 按根归组
    assert await scan_mod._resolve_scope(library, {str(entry)}) == {str(root): {"影片X (2020)"}}
    # 空范围 = 整库
    assert await scan_mod._resolve_scope(library, None) is None
    assert await scan_mod._resolve_scope(library, set()) is None
    # 条目已消失（删除/改名竞态）→ 整库
    assert await scan_mod._resolve_scope(library, {str(root / "不存在")}) is None
    # 根级散文件事件 → 整库（范围单位是目录）
    loose = root / "loose.mkv"
    loose.write_bytes(b"x")
    assert await scan_mod._resolve_scope(library, {str(loose)}) is None
    # 不在任何根的第一级（深层路径 / 根已换）→ 整库
    deep = entry / "sub"
    deep.mkdir()
    assert await scan_mod._resolve_scope(library, {str(deep)}) is None
    assert await scan_mod._resolve_scope(library, {str(tmp_path / "other")}) is None
    # 混合：只要有一条存疑，整批退回整库
    assert await scan_mod._resolve_scope(library, {str(entry), str(root / "不存在")}) is None


async def test_scoped_scan_touches_only_scope(db, tmp_path, monkeypatch) -> None:
    """范围内：新文件入账、消失文件标 missing；范围外：行原封不动。

    这是范围扫描的正确性红线——范围外的行「本轮没遍历到」是设计使然，
    绝不能像整库扫描那样被判丢失。
    """
    root = tmp_path / "movies"
    e1000 = _movie_entry(root, 1000)
    e1001 = _movie_entry(root, 1001)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    assert len(await _rows(db, library.id)) == 2

    auto_clear_calls = 0
    real_auto_clear = scan_mod._auto_clear_missing

    async def counting_auto_clear(*args, **kwargs):
        nonlocal auto_clear_calls
        auto_clear_calls += 1
        return await real_auto_clear(*args, **kwargs)

    monkeypatch.setattr(scan_mod, "_auto_clear_missing", counting_auto_clear)

    # 范围内变化：1000 的视频消失、新条目 1002 出现；范围外变化：1001 的
    # 视频也消失了，但事件没提到它
    (e1000 / "影片1000.1080p.mkv").unlink()
    (e1001 / "影片1001.1080p.mkv").unlink()
    e1002 = _movie_entry(root, 1002)
    summary = await scan_library(
        library.id,
        backfill_existing_specs=False,
        scope_paths={str(e1000), str(e1002)},
    )
    assert not summary.errors
    assert auto_clear_calls == 0, "范围扫描不得执行自动清理"

    rows = await _rows(db, library.id)
    assert len(rows) == 3
    by_entry = {p: r for p, r in rows.items()}
    missing = {p for p, r in by_entry.items() if r.missing_since is not None}
    assert missing == {str(e1000 / "影片1000.1080p.mkv")}, "只有范围内消失的行标 missing"
    assert str(e1002 / "影片1002.1080p.mkv") in rows, "范围内新文件要入账"

    # 整库扫描收尾：范围外的 1001 此时才被发现丢失；自动清理恢复执行
    await scan_library(library.id, backfill_existing_specs=False)
    assert auto_clear_calls == 1
    rows = await _rows(db, library.id)
    assert {p for p, r in rows.items() if r.missing_since is not None} == {
        str(e1000 / "影片1000.1080p.mkv"),
        str(e1001 / "影片1001.1080p.mkv"),
    }


async def test_scoped_scan_walks_only_dirty_subtree(db, tmp_path, monkeypatch) -> None:
    """效果守卫：范围扫描的目录遍历量是 O(变动条目) 而不是 O(整库)。

    12 个条目的库里只有 1 个条目有事件，本轮 scandir 只允许触达根目录与
    该条目——其余 11 个条目目录一次都不能列。一旦有人把范围扫描改回
    整库遍历，这里立刻变红。
    """
    root = tmp_path / "movies"
    for i in range(12):
        _movie_entry(root, 1000 + i)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    fresh = _movie_entry(root, 1100)
    real_scandir = os.scandir
    listed: list[str] = []

    def counting_scandir(path, *args, **kwargs):
        listed.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(scan_mod.os, "scandir", counting_scandir)
    await scan_library(library.id, backfill_existing_specs=False, scope_paths={str(fresh)})
    monkeypatch.undo()

    walked = set(listed)
    assert str(root) in walked and str(fresh) in walked
    other_entries = {p for p in walked if p not in (str(root), str(fresh))}
    assert not other_entries, f"范围外的目录被遍历了：{sorted(other_entries)}"
    assert str(fresh / "影片1100.1080p.mkv") in await _rows(db, library.id)


async def test_scoped_scan_falls_back_when_entry_vanished(db, tmp_path) -> None:
    """范围条目整个被删（目录都不在了）→ 自动退回整库，丢失照常全量对齐。"""
    root = tmp_path / "movies"
    e1000 = _movie_entry(root, 1000)
    e1001 = _movie_entry(root, 1001)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    shutil.rmtree(e1000)
    (e1001 / "影片1001.1080p.mkv").unlink()
    # 事件只点名了 1000，但它已整目录消失 → 整库扫描 → 1001 也被对齐
    await scan_library(library.id, backfill_existing_specs=False, scope_paths={str(e1000)})
    rows = await _rows(db, library.id)
    assert {p for p, r in rows.items() if r.missing_since is not None} == {
        str(e1000 / "影片1000.1080p.mkv"),
        str(e1001 / "影片1001.1080p.mkv"),
    }


# ---------------------------------------------------------------------------
# 3. watch 事件 → 范围传递
# ---------------------------------------------------------------------------


def test_scope_for_first_level_entry() -> None:
    assert watch_mod._scope_for("/lib", "/lib/Movie A/BDMV/x.m2ts") == "/lib/Movie A"
    assert watch_mod._scope_for("/lib", "/lib/loose.mkv") == "/lib/loose.mkv"
    assert watch_mod._scope_for("/lib", "/lib") is None  # 根自身 → 整库
    assert watch_mod._scope_for("/lib", "/elsewhere/x.mkv") is None
    # 根带尾斜杠也归一
    assert watch_mod._scope_for("/lib/", "/lib/Movie A/x.mkv") == "/lib/Movie A"


def test_event_scopes_covers_moved_endpoints() -> None:
    from watchdog.events import FileMovedEvent

    event = FileMovedEvent("/lib/A/x.mkv", "/lib/B/x.mkv")
    assert watch_mod._event_scopes("/lib", event) == {"/lib/A", "/lib/B"}


async def test_watcher_passes_scope_to_scan(db, tmp_path, monkeypatch) -> None:
    """事件驱动的扫描必须带上条目范围——这是「一个文件事件不再遍历整库」
    的入口保证；根级散文件事件则按文件路径传递、由扫描侧退回整库。"""
    pytest.importorskip("watchdog")
    monkeypatch.setattr(watch_mod, "_QUIET_SECONDS", 0.1)
    monkeypatch.setattr(watch_mod, "_MAX_WAIT_SECONDS", 1.0)

    root = tmp_path / "movies"
    root.mkdir()
    async with db.session() as session:
        await LibraryRepository(session).create(name="电影库", kind="movie", root_paths=[str(root)])

    calls: list[dict] = []

    async def fake_scan(library_id, **kwargs):
        calls.append({"library_id": library_id, **kwargs})

    monkeypatch.setattr(scan_mod, "scan_library", fake_scan)

    watcher = watch_mod.LibraryWatcher()
    await watcher.start()
    try:
        assert watcher._startup is not None
        await watcher._startup
        entry = root / "影片X (2020)"
        # 合成事件直投观察者分发队列：仍走 dispatch 线程与 handler 注册表
        # （挂接被误拆时收不到），但不依赖真实文件系统送信时延——满载并行
        # 跑测试时 FSEvents 送达可超限时，是偶发超时的根源
        from watchdog.events import FileCreatedEvent

        watch = next(iter(watcher._entries.values()))[1]
        watcher._observer.event_queue.put((FileCreatedEvent(str(entry / "X.2020.mkv")), watch))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if calls:
                break
        assert calls, "监控未触发扫描"
        assert calls[0]["scope_paths"] == {str(entry)}
        assert calls[0]["backfill_existing_specs"] is False
    finally:
        await watcher.stop()


async def test_watcher_defers_events_during_scan(db, tmp_path, monkeypatch) -> None:
    """扫描进行中收到的事件不丢弃：等扫描结束补触发（旧实现直接丢，
    新文件要等 6 小时对账才可见）。"""
    monkeypatch.setattr(watch_mod, "_QUIET_SECONDS", 0.05)
    monkeypatch.setattr(watch_mod, "_MAX_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(watch_mod, "_RESCAN_POLL_SECONDS", 0.05)

    scanning = {"on": True}
    monkeypatch.setattr(scan_mod, "is_scanning", lambda _lib: scanning["on"])
    calls: list[dict] = []

    async def fake_scan(library_id, **kwargs):
        calls.append({"library_id": library_id, **kwargs})

    monkeypatch.setattr(scan_mod, "scan_library", fake_scan)

    watcher = watch_mod.LibraryWatcher()
    await watcher.start()
    try:
        watcher._queue.put_nowait((7, "/lib/entry", None))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if 7 in watcher._rescan_pending:
                break
        assert 7 in watcher._rescan_pending, "扫描中收到的事件应挂起等待"
        assert not calls
        scanning["on"] = False
        for _ in range(100):
            await asyncio.sleep(0.05)
            if calls:
                break
        assert calls and calls[0]["library_id"] == 7
        assert calls[0]["scope_paths"] == {"/lib/entry"}
    finally:
        await watcher.stop()


# ---------------------------------------------------------------------------
# 4. 差量重建
# ---------------------------------------------------------------------------


async def test_incremental_refresh_keeps_unchanged_watches(db, tmp_path) -> None:
    """配置没变的根不付任何重建成本：watch 对象原样保留（同一性）。"""
    pytest.importorskip("watchdog")
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    async with db.session() as session:
        repo = LibraryRepository(session)
        lib_a = await repo.create(name="A", kind="movie", root_paths=[str(root_a)])
        lib_b = await repo.create(name="B", kind="movie", root_paths=[str(root_b)])

    watcher = watch_mod.LibraryWatcher()
    watcher._loop = asyncio.get_running_loop()
    try:
        await watcher.refresh_watches()
        first = dict(watcher._entries)
        assert set(first) == {(lib_a.id, str(root_a)), (lib_b.id, str(root_b))}

        # 无变化的 refresh：对象同一性保持（没拆没建）
        await watcher.refresh_watches()
        assert {k: id(v[1]) for k, v in watcher._entries.items()} == {
            k: id(v[1]) for k, v in first.items()
        }

        # 新增一个库：只新增，未变的两个根原样
        root_c = tmp_path / "c"
        root_c.mkdir()
        async with db.session() as session:
            lib_c = await LibraryRepository(session).create(
                name="C", kind="movie", root_paths=[str(root_c)]
            )
        await watcher.refresh_watches()
        assert watcher._entries[(lib_a.id, str(root_a))][1] is first[(lib_a.id, str(root_a))][1]
        assert (lib_c.id, str(root_c)) in watcher._entries

        # 删除一个库：只拆它
        async with db.session() as session:
            victim = await session.get(Library, lib_c.id)
            await session.delete(victim)
            await session.commit()
        await watcher.refresh_watches()
        assert (lib_c.id, str(root_c)) not in watcher._entries
        assert watcher._entries[(lib_b.id, str(root_b))][1] is first[(lib_b.id, str(root_b))][1]
    finally:
        await asyncio.to_thread(watcher._stop_observer)


async def test_shared_root_survives_sibling_removal(db, tmp_path, monkeypatch) -> None:
    """两个库共用同一根路径时，删掉其中一个不能拆掉另一个的监听。"""
    pytest.importorskip("watchdog")
    monkeypatch.setattr(watch_mod, "_COALESCE_SECONDS", 0.0)
    root = tmp_path / "shared"
    root.mkdir()
    async with db.session() as session:
        repo = LibraryRepository(session)
        lib_a = await repo.create(name="A", kind="movie", root_paths=[str(root)])
        lib_b = await repo.create(name="B", kind="tv", root_paths=[str(root)])

    watcher = watch_mod.LibraryWatcher()
    watcher._loop = asyncio.get_running_loop()
    try:
        await watcher.refresh_watches()
        assert len(watcher._entries) == 2
        async with db.session() as session:
            victim = await session.get(Library, lib_b.id)
            await session.delete(victim)
            await session.commit()
        await watcher.refresh_watches()
        assert set(watcher._entries) == {(lib_a.id, str(root))}

        entry = root / "影片Y (2020)"
        # 合成事件直投观察者分发队列（不依赖真实文件系统送信时延）：事件仍
        # 按 handler 注册表分发，lib_a 的挂接若被误拆则收不到
        from watchdog.events import FileCreatedEvent

        watch = watcher._entries[(lib_a.id, str(root))][1]
        watcher._observer.event_queue.put((FileCreatedEvent(str(entry / "Y.mkv")), watch))
        deadline = time_mod.monotonic() + 10
        got = None
        while time_mod.monotonic() < deadline:
            try:
                got = watcher._queue.get_nowait()
                break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
        assert got is not None, "共享根被误拆：剩下的库收不到事件了"
        assert got[0] == lib_a.id and got[1] == str(entry)
    finally:
        await asyncio.to_thread(watcher._stop_observer)


async def test_schedule_failure_is_isolated_per_root(db, tmp_path, monkeypatch) -> None:
    """单根建 watch 失败（如 inotify watch 上限）不拖累其余根、不抛出。"""
    watchdog_observers = pytest.importorskip("watchdog.observers")
    root_ok = tmp_path / "ok"
    root_bad = tmp_path / "bad"
    root_ok.mkdir()
    root_bad.mkdir()
    async with db.session() as session:
        repo = LibraryRepository(session)
        lib_ok = await repo.create(name="OK", kind="movie", root_paths=[str(root_ok)])
        await repo.create(name="BAD", kind="movie", root_paths=[str(root_bad)])

    real_schedule = watchdog_observers.Observer.schedule

    def flaky_schedule(self, handler, path, recursive=False, **kwargs):
        if os.fspath(path) == str(root_bad):
            raise OSError(28, "mock: inotify watch limit reached")
        return real_schedule(self, handler, path, recursive=recursive, **kwargs)

    monkeypatch.setattr(watchdog_observers.Observer, "schedule", flaky_schedule)
    watcher = watch_mod.LibraryWatcher()
    watcher._loop = asyncio.get_running_loop()
    try:
        await watcher.refresh_watches()  # 不得抛出
        assert set(watcher._entries) == {(lib_ok.id, str(root_ok))}
    finally:
        await asyncio.to_thread(watcher._stop_observer)


async def test_ingest_apply_watches_incremental_and_first_time_catchup(tmp_path) -> None:
    """监听导入差量重建：只有**初次**纳入的目录进补扫名单；未变目录零成本；
    真实事件仍能送达（handler 挂接正确）。"""
    pytest.importorskip("watchdog")
    d1 = tmp_path / "watch1"
    d2 = tmp_path / "watch2"
    d1.mkdir()
    d2.mkdir()

    watcher = ingest_mod.IngestWatcher()
    watcher._loop = asyncio.get_running_loop()
    try:
        assert await asyncio.to_thread(watcher._apply_watches, [str(d1)]) == {str(d1)}
        first = dict(watcher._entries)
        # 重复 refresh：无新目录、watch 对象同一性保持
        assert await asyncio.to_thread(watcher._apply_watches, [str(d1)]) == set()
        assert watcher._entries[str(d1)][1] is first[str(d1)][1]
        # 新增目录：只有它算"初次"
        assert await asyncio.to_thread(watcher._apply_watches, [str(d1), str(d2)]) == {str(d2)}
        assert watcher.watched_keys() == {str(d1), str(d2)}
        # 摘掉 d1：差量拆除
        assert await asyncio.to_thread(watcher._apply_watches, [str(d2)]) == set()
        assert watcher.watched_keys() == {str(d2)}

        # 真实事件送达：d2 里落文件 → 队列收到该源目录标识
        (d2 / "Some.Movie.2020.mkv").write_bytes(b"x")
        deadline = time_mod.monotonic() + 10
        got = None
        while time_mod.monotonic() < deadline:
            try:
                got = watcher._queue.get_nowait()
                break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
        assert got == str(d2)
    finally:
        await asyncio.to_thread(watcher._stop_observer)


async def test_realtime_watch_toggle_controls_watching(db, tmp_path) -> None:
    """按库关闭实时监控（issue #162）：关掉的库不建监听；开关切换走差量
    重建——关闭即拆监听、重新打开即恢复，其余库的 watch 原样不动。"""
    pytest.importorskip("watchdog")
    root_on = tmp_path / "local"
    root_off = tmp_path / "smb"
    root_on.mkdir()
    root_off.mkdir()
    async with db.session() as session:
        repo = LibraryRepository(session)
        lib_on = await repo.create(name="本地库", kind="movie", root_paths=[str(root_on)])
        lib_off = await repo.create(
            name="网络库", kind="movie", root_paths=[str(root_off)], realtime_watch=False
        )
    assert lib_on.realtime_watch is True  # 默认开

    watcher = watch_mod.LibraryWatcher()
    watcher._loop = asyncio.get_running_loop()
    try:
        await watcher.refresh_watches()
        assert set(watcher._entries) == {(lib_on.id, str(root_on))}, "关闭监控的库不得建监听"
        kept = watcher._entries[(lib_on.id, str(root_on))][1]

        # 重新打开 → 差量补建；本地库的 watch 对象原样保留
        async with db.session() as session:
            row = await session.get(Library, lib_off.id)
            row.realtime_watch = True
            await session.commit()
        await watcher.refresh_watches()
        assert set(watcher._entries) == {
            (lib_on.id, str(root_on)),
            (lib_off.id, str(root_off)),
        }
        assert watcher._entries[(lib_on.id, str(root_on))][1] is kept

        # 再关掉 → 差量拆除
        async with db.session() as session:
            row = await session.get(Library, lib_off.id)
            row.realtime_watch = False
            await session.commit()
        await watcher.refresh_watches()
        assert set(watcher._entries) == {(lib_on.id, str(root_on))}
        assert watcher._entries[(lib_on.id, str(root_on))][1] is kept
    finally:
        await asyncio.to_thread(watcher._stop_observer)


# ---------------------------------------------------------------------------
# 5. 观察者线程侧合并 + 启动不阻塞
# ---------------------------------------------------------------------------


def test_thread_side_coalescing_window(monkeypatch) -> None:
    """同键短窗口合并；不同键不受影响；过窗后恢复投递。"""
    monkeypatch.setattr(watch_mod, "_COALESCE_SECONDS", 0.05)
    watcher = watch_mod.LibraryWatcher()
    key = (1, "/lib/A", None)
    assert watcher._should_enqueue(key)
    assert not watcher._should_enqueue(key)  # 风暴中的重复事件被吃掉
    assert watcher._should_enqueue((1, "/lib/B", None))  # 不同条目不受影响
    time_mod.sleep(0.06)
    assert watcher._should_enqueue(key)  # 过窗恢复

    monkeypatch.setattr(ingest_mod, "_EVENT_COALESCE_SECONDS", 0.05)
    ingest_watcher = ingest_mod.IngestWatcher()
    assert ingest_watcher._should_enqueue("/watch")
    assert not ingest_watcher._should_enqueue("/watch")
    time_mod.sleep(0.06)
    assert ingest_watcher._should_enqueue("/watch")


async def test_start_returns_before_slow_watch_setup(monkeypatch) -> None:
    """建 watch 再慢也不能拖住启动（issue #162 的 300 秒超时就是这么来的）。"""
    for watcher in (watch_mod.LibraryWatcher(), ingest_mod.IngestWatcher()):
        started = asyncio.Event()

        async def slow_refresh(_started=started):
            _started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(watcher, "refresh_watches", slow_refresh)
        t0 = time_mod.monotonic()
        await watcher.start()
        assert time_mod.monotonic() - t0 < 1.0, "start 不得等待建 watch 完成"
        await asyncio.wait_for(started.wait(), timeout=5)  # 后台任务确实在建
        await watcher.stop()  # 且能在建到一半时干净关停
