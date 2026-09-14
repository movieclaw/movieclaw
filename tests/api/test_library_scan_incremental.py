"""定期对账的目录 mtime 增量遍历（models/library_dir_snapshot.py）。

对账的成本必须是 O(变动) 而不是 O(整库)：上一轮完整遍历时记下每个目录的
mtime，这一轮只重列变过的目录，没变的叶子目录不再 readdir、底下的台账行按
"仍在原位"处理。用例用 ``os.scandir`` 的探针证明"没列"是真的没列，而不只是
summary 上的一个数字。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.scan as scan_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.scan import scan_library
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileState, Library, LibraryDirSnapshot, LibraryFile, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    # 识别链不在本测试范围：TMDB 客户端打桩为惰性哑对象；暂缓补扫不真等
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: object())
    monkeypatch.setattr(scan_mod, "_arm_rescan", lambda *a: None)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _age(path, seconds: int = 3600) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


def _bump(path) -> None:
    """把目录 mtime 拨到"现在"：文件系统时间粒度粗时，同一秒内的增删可能不改 mtime。"""
    now = time.time()
    os.utime(path, (now, now))


async def _seed(db, root) -> tuple[int, dict[str, object]]:
    """三个电影目录，各一个存量文件。"""
    dirs = {}
    for name in ("A", "B", "C"):
        d = root / name
        d.mkdir(parents=True)
        video = d / f"{name.lower()}.mkv"
        video.write_bytes(b"settled")
        _age(video)
        _age(d)
        dirs[name] = d
    _age(root)
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name="测试电影库", kind=MediaKind.MOVIE.value, root_paths=[str(root)]
        )
        return row.id, dirs


def _scandir_spy(monkeypatch, root) -> list[str]:
    """记录遍历器本轮 readdir 过的目录，其余原样放行。

    只数 ``_walk_videos`` 发出的：入账链路自己也会列目录（海报 sidecar 发现走
    artwork.dir_listing），那是"处理这个文件"的成本，不是"盘点整库"的成本。
    """
    real = os.scandir
    listed: list[str] = []

    def spy(path=".", *args, **kwargs):
        if str(path).startswith(str(root)) and sys._getframe(1).f_code.co_name == "_walk_videos":
            listed.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(scan_mod.os, "scandir", spy)
    return listed


async def _rows(db) -> dict[str, LibraryFile]:
    async with db.session() as session:
        return {
            r.file_path: r for r in (await session.execute(select(LibraryFile))).scalars().all()
        }


async def _library(db, library_id) -> Library:
    async with db.session() as session:
        return await session.get(Library, library_id)


@pytest.mark.asyncio
async def test_reconcile_lists_only_changed_dirs(db, tmp_path, monkeypatch):
    """全量之后的对账只列根与变过的目录；没变的目录下的文件既不重处理也不判丢失。"""
    root = tmp_path / "movies"
    library_id, dirs = await _seed(db, root)

    # 用户主动扫描 = 全量：三个目录都列，产出快照并盖上全量时间
    first = await scan_library(library_id)
    assert first.scanned == 3 and first.dirs_skipped == 0 and first.dirs_listed == 4
    lib = await _library(db, library_id)
    assert lib.dir_snapshot_full_at is not None
    async with db.session() as session:
        snap = (await session.execute(select(LibraryDirSnapshot))).scalars().all()
    assert {s.path for s in snap} == {str(root), *(str(d) for d in dirs.values())}
    assert all(s.leaf for s in snap if s.path != str(root))

    # 什么都没变的对账：只有根被 readdir，三个电影目录一个都没列
    listed = _scandir_spy(monkeypatch, root)
    second = await scan_library(library_id, backfill_existing_specs=False)
    assert listed == [str(root)], listed
    assert second.dirs_listed == 1 and second.dirs_skipped == 3
    assert second.skipped_known == 0  # 跳过的目录下的文件根本不进 pending
    assert second.marked_missing == 0
    assert all(r.state == FileState.IN_PLACE for r in (await _rows(db)).values())

    # B 加了一部、C 的片被删：只有 B 与 C 被重列，A 仍然跳过
    # 尺寸刻意与 c.mkv 不同：同尺寸会被改名归并当成 c.mkv 搬了家（那是另一条链路）
    new_video = dirs["B"] / "b2.mkv"
    new_video.write_bytes(b"a brand new file, not a rename")
    _age(new_video)
    _bump(dirs["B"])
    (dirs["C"] / "c.mkv").unlink()
    _bump(dirs["C"])
    listed.clear()
    third = await scan_library(library_id, backfill_existing_specs=False)
    assert sorted(listed) == sorted([str(root), str(dirs["B"]), str(dirs["C"])]), listed
    assert third.dirs_skipped == 1 and third.scanned == 1 and third.marked_missing == 1
    rows = await _rows(db)
    assert rows[str(new_video)].state == FileState.IN_PLACE
    assert rows[str(dirs["C"] / "c.mkv")].state == FileState.MISSING
    assert rows[str(dirs["A"] / "a.mkv")].state == FileState.IN_PLACE

    # 快照按差异维护：B、C 的 mtime 已更新，A 原样
    async with db.session() as session:
        snap = {
            s.path: s.mtime_ns
            for s in (await session.execute(select(LibraryDirSnapshot))).scalars().all()
        }
    assert snap[str(dirs["B"])] == os.stat(dirs["B"]).st_mtime_ns
    assert snap[str(dirs["C"])] == os.stat(dirs["C"]).st_mtime_ns


@pytest.mark.asyncio
async def test_full_walk_forced_when_snapshot_is_stale(db, tmp_path, monkeypatch):
    """全量时间超过一周就强制全量一轮（保险），并刷新全量时间；手动扫描永远全量。"""
    root = tmp_path / "movies"
    library_id, _dirs = await _seed(db, root)
    await scan_library(library_id)

    async with db.session() as session:
        lib = await session.get(Library, library_id)
        lib.dir_snapshot_full_at = utcnow() - timedelta(
            seconds=scan_mod.DIR_SNAPSHOT_FULL_INTERVAL_SECONDS + 60
        )
        await session.commit()

    listed = _scandir_spy(monkeypatch, root)
    stale = await scan_library(library_id, backfill_existing_specs=False)
    assert len(listed) == 4 and stale.dirs_skipped == 0
    lib = await _library(db, library_id)
    assert (utcnow() - lib.dir_snapshot_full_at).total_seconds() < 60

    # 手动扫描不看快照：三个目录照列
    listed.clear()
    manual = await scan_library(library_id)
    assert len(listed) == 4 and manual.dirs_skipped == 0


@pytest.mark.asyncio
async def test_reprobe_path_forces_its_dir_to_be_listed(db, tmp_path, monkeypatch):
    """点名重探的文件所在目录即使没变也要列：文件得进 pending 才会被 stat 比对。"""
    root = tmp_path / "movies"
    library_id, dirs = await _seed(db, root)
    await scan_library(library_id)

    listed = _scandir_spy(monkeypatch, root)
    summary = await scan_library(
        library_id,
        backfill_existing_specs=False,
        reprobe_paths={str(dirs["A"] / "a.mkv")},
    )
    assert sorted(listed) == sorted([str(root), str(dirs["A"])]), listed
    # A 里的文件进了 pending（本环境 TMDB 打桩，行是待识别态，走的是识别重试而非秒过）
    assert summary.dirs_skipped == 2 and summary.scanned == 0


@pytest.mark.asyncio
async def test_scoped_scan_leaves_snapshot_alone(db, tmp_path, monkeypatch):
    """范围扫描（监听事件）只看了一部分，不产出也不消费快照。"""
    root = tmp_path / "movies"
    library_id, dirs = await _seed(db, root)
    await scan_library(library_id)
    async with db.session() as session:
        before = {
            (s.path, s.mtime_ns)
            for s in (await session.execute(select(LibraryDirSnapshot))).scalars().all()
        }

    (dirs["A"] / "a.mkv").unlink()
    _bump(dirs["A"])
    scoped = await scan_library(
        library_id, backfill_existing_specs=False, scope_paths={str(dirs["A"])}
    )
    assert scoped.marked_missing == 1
    assert scoped.dirs_listed == 0 and scoped.dirs_skipped == 0
    async with db.session() as session:
        after = {
            (s.path, s.mtime_ns)
            for s in (await session.execute(select(LibraryDirSnapshot))).scalars().all()
        }
    assert after == before
