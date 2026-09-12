"""媒体库文件回收机制测试（docs/design/library-file-recycle.md）：
第三态状态机、一律倒计时（2026-08-17 收敛）、恢复/立即清理、
到期清理任务、复活防线。"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.recycle import (
    purge_due_files,
    purge_file,
    recycle_file,
    restore_file,
    sweep_orphan_trash,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository

_TRIGGER = {"kind": "subscription", "id": 1, "label": "《测试》订阅洗版"}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'recycle.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed_file(db, tmp_path, *, hardlink=True, name="Movie.2020.1080p.mkv"):
    """真实 tmp 库根 + 一条在位台账行；hardlink=True 模拟硬链入库形态。"""
    root = tmp_path / "movies"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(b"data")
    if hardlink:
        (tmp_path / "downloads").mkdir(exist_ok=True)
        os.link(path, tmp_path / "downloads" / name)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name=f"电影库-{name}", kind="movie", root_paths=[str(root)]
        )
        row = LibraryFile(
            library_id=library.id,
            file_path=str(path),
            size_bytes=4,
            source=FileSource.IMPORTED,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id, path, root


@pytest.mark.asyncio
async def test_recycle_moves_hardlinked_file_to_trash(db, tmp_path):
    """硬链文件：移入库根回收站，file_path 更新为当前位置（全站不变量），
    原路径存 trash_original_path，默认按保留期倒计时，审计快照落行。"""
    file_id, path, root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        outcome = await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        assert outcome == "moved_to_trash"
        assert row.state == FileState.TRASHED
        assert not path.exists()
        assert row.file_path.startswith(str(root / ".movieclaw-trash"))
        assert row.trash_original_path == str(path)
        assert row.purge_after is not None
        assert row.trash_context["trigger"]["label"] == "《测试》订阅洗版"


@pytest.mark.asyncio
async def test_recycle_moves_unique_copy_to_trash_too(db, tmp_path):
    """唯一硬链接的文件同样移入回收站并倒计时（2026-08-17 收敛）：copy
    入库下库文件 nlink 恒为 1，旧做种保护判据必然全命中、垃圾永久滞留；
    做种原始文件在下载目录，不受库内移动影响。想保留由用户「恢复」。"""
    file_id, path, root = await _seed_file(db, tmp_path, hardlink=False)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        outcome = await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        assert outcome == "moved_to_trash"
        assert row.state == FileState.TRASHED
        assert not path.exists()
        assert row.file_path.startswith(str(root / ".movieclaw-trash"))
        assert row.trash_original_path == str(path)
        assert row.purge_after is not None  # 一律按保留期倒计时


@pytest.mark.asyncio
async def test_restore_moves_file_back(db, tmp_path):
    """恢复：文件移回原路径，状态与附属字段全部清零。"""
    file_id, path, _root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        assert await restore_file(session, row) is True
        await session.commit()
        assert row.state == FileState.IN_PLACE
        assert row.file_path == str(path)
        assert path.exists()
        assert row.trashed_at is None and row.purge_after is None
        assert row.trash_context is None


@pytest.mark.asyncio
async def test_purge_deletes_file_and_row(db, tmp_path):
    """立即清理：删物理文件 + 删行；在位行拒绝清理。"""
    file_id, path, _root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert await purge_file(session, row) is False  # 在位行不许直接清
        await recycle_file(
            session, row, reason="manual", trigger={"kind": "member", "id": 1}, note="手动删除"
        )
        trash_path = row.file_path
        assert await purge_file(session, row) is True
        await session.commit()
        assert not (tmp_path / trash_path).exists()
        assert await session.get(LibraryFile, file_id) is None


@pytest.mark.asyncio
async def test_purge_due_files_expires_and_converges(db, tmp_path):
    """到期清理任务：过期的删文件+删行；未到期保留；文件消失的行收敛。"""
    expired_id, _p1, _ = await _seed_file(db, tmp_path, name="expired.mkv")
    fresh_id, _p2, _ = await _seed_file(db, tmp_path, name="fresh.mkv")
    gone_id, gone_path, _ = await _seed_file(db, tmp_path, hardlink=False, name="gone.mkv")
    async with db.session() as session:
        for fid, purge_delta in ((expired_id, -1), (fresh_id, 1)):
            row = await session.get(LibraryFile, fid)
            await recycle_file(
                session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
            )
            row.purge_after = utcnow() + timedelta(hours=purge_delta)
        gone = await session.get(LibraryFile, gone_id)
        await recycle_file(
            session, gone, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        gone_trash = gone.file_path  # 一律移入回收站后的当前位置
        await session.commit()
    os.unlink(gone_trash)  # 待回收期间文件被外部删除

    await purge_due_files()
    async with db.session() as session:
        assert await session.get(LibraryFile, expired_id) is None  # 到期清理
        assert await session.get(LibraryFile, gone_id) is None  # 消失收敛
        fresh = await session.get(LibraryFile, fresh_id)
        assert fresh is not None and fresh.state == FileState.TRASHED  # 未到期保留


@pytest.mark.asyncio
async def test_state_guards_block_resurrection(db, tmp_path):
    """复活防线：缺失检测不覆盖待回收状态，revive 也不复活待回收行。"""
    file_id, _path, _root = await _seed_file(db, tmp_path, hardlink=False)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_refuted", trigger=_TRIGGER, note="证伪隔离"
        )
        row.mark_missing()  # 对账误触：不得覆盖待回收
        assert row.state == FileState.TRASHED and row.missing_since is None
        row.revive()  # 扫描按路径命中：不得复活
        assert row.state == FileState.TRASHED
        await session.commit()


@pytest.mark.asyncio
async def test_in_place_scope_excludes_trashed(db, tmp_path):
    """共享口径：待回收行不进默认库存口径。"""
    file_id, _path, _root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        in_place = list(
            (await session.execute(select(LibraryFile).where(LibraryFile.in_place()))).scalars()
        )
        assert in_place == []


async def _seed_item_with_two_versions(db, tmp_path):
    """条目目录 + 在位新版本 + 已移入回收站的旧版本（删除流程交叉用例）。"""
    from movieclaw_db.models import MediaItem

    root = tmp_path / "tv"
    entry = root / "某剧 (2020)"
    entry.mkdir(parents=True, exist_ok=True)
    new_file = entry / "某剧.S01E01.Remux.mkv"
    new_file.write_bytes(b"new")
    old_file = entry / "某剧.S01E01.WEB-DL.mkv"
    old_file.write_bytes(b"old")
    (tmp_path / "dl").mkdir(exist_ok=True)
    os.link(old_file, tmp_path / "dl" / "old.mkv")  # 硬链形态 → recycle 会移入回收站
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库-删除交叉", kind="tv", root_paths=[str(root)]
        )
        item = MediaItem(kind="tv", tmdb_id=911, title="某剧", original_title="X", year=2020)
        session.add(item)
        await session.flush()
        rows = []
        for path in (new_file, old_file):
            row = LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                file_path=str(path),
                size_bytes=3,
                source=FileSource.IMPORTED,
            )
            session.add(row)
            rows.append(row)
        await session.commit()
        for row in rows:
            await session.refresh(row)
        old_row = rows[1]
        await recycle_file(
            session, old_row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        assert old_row.state == FileState.TRASHED and ".movieclaw-trash" in old_row.file_path
        return library.id, item.id, rows[0].id, old_row.id, root, entry


@pytest.mark.asyncio
async def test_delete_single_file_purges_trashed_row(db, tmp_path):
    """单文件删除落在待回收行上：物理文件一并删除、行删除——绝不清账留文件
    （行没了而文件还在，扫描会把它当新文件重新收编）。"""
    from movieclaw_api.services.library.items import delete_single_file
    from movieclaw_db.models.library import Library

    library_id, _item, _new_id, old_id, _root, _entry = await _seed_item_with_two_versions(
        db, tmp_path
    )
    async with db.session() as session:
        library = await session.get(Library, library_id)
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.library_id == library_id)
                )
            ).scalars()
        )
        old_row = next(r for r in rows if r.id == old_id)
        trash_path = old_row.file_path
        result = await delete_single_file(session, library, old_row, rows)
        assert result.errors == []
        assert result.rows_deleted == 1
    from pathlib import Path as _P

    assert not _P(trash_path).exists()  # 物理文件一并删除
    async with db.session() as session:
        assert await session.get(LibraryFile, old_id) is None


@pytest.mark.asyncio
async def test_item_delete_removes_trash_file_but_keeps_trash_dir(db, tmp_path):
    """整条目删除：待回收文件按单文件清理——.movieclaw-trash 目录绝不被当作
    条目目录整删（会卷走其他条目的回收文件）。"""
    from movieclaw_api.services.library.items import delete_item_files
    from movieclaw_db.models.library import Library

    library_id, item_id, _new_id, old_id, root, entry = await _seed_item_with_two_versions(
        db, tmp_path
    )
    trash_dir = root / ".movieclaw-trash"
    (trash_dir / "别的条目的回收文件.mkv").write_bytes(b"other")  # 无台账的孤儿
    async with db.session() as session:
        library = await session.get(Library, library_id)
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.library_id == library_id)
                )
            ).scalars()
        )
        result = await delete_item_files(session, library, item_id, rows)
        assert result.errors == []
    assert not entry.exists()  # 条目目录整删
    assert trash_dir.is_dir()  # 回收站目录健在
    assert (trash_dir / "别的条目的回收文件.mkv").exists()  # 别人的文件没被卷走
    async with db.session() as session:
        assert await session.get(LibraryFile, old_id) is None


@pytest.mark.asyncio
async def test_recycle_keeps_disc_dir_in_place_and_purge_removes_it(db, tmp_path):
    """原盘目录：不改名（内部可能住着其他在案文件行，移动会让路径悬空），
    原地待回收但照常按保留期倒计时；立即清理能删整个目录。"""
    root = tmp_path / "movies"
    disc = root / "某电影 (2020)" / "BDMV"
    (disc / "STREAM").mkdir(parents=True)
    (disc / "STREAM" / "00000.m2ts").write_bytes(b"disc")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库-原盘", kind="movie", root_paths=[str(root)]
        )
        row = LibraryFile(
            library_id=library.id,
            file_path=str(disc),
            size_bytes=4,
            source=FileSource.SCANNED,
            container="bluray",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        outcome = await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        assert outcome == "kept_in_place"  # 目录绝不改名
        assert disc.is_dir()
        assert row.purge_after is not None  # 原地形态也照常倒计时

        assert await purge_file(session, row) is True  # 立即清理支持目录
        await session.commit()
    assert not disc.exists()


@pytest.mark.asyncio
async def test_recycle_moves_strm_placeholder(db, tmp_path):
    """strm 占位文件天然只有一个硬链接，但它是文本指针不是做种载荷——
    必须豁免做种保护正常移入回收站，否则永远原地留置无法自动清理。"""
    file_id, path, root = await _seed_file(db, tmp_path, hardlink=False, name="Movie.2020.strm")
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        outcome = await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        assert outcome == "moved_to_trash"
        assert not path.exists()
        assert row.file_path.startswith(str(root / ".movieclaw-trash"))
        assert row.purge_after is not None  # 正常按保留期倒计时


@pytest.mark.asyncio
async def test_sidecars_follow_recycle_restore_and_purge(db, tmp_path):
    """附属文件（字幕/NFO）随主文件进回收站、随恢复搬回、随清理删除；
    同名不同容器的视频是独立版本，绝不连带。"""
    file_id, path, root = await _seed_file(db, tmp_path)
    srt = path.with_name(path.stem + ".zh.srt")
    srt.write_text("字幕")
    nfo = path.with_name(path.stem + ".nfo")
    nfo.write_text("<movie/>")
    sibling = path.with_name(path.stem + ".mp4")  # 独立版本，不是附属
    sibling.write_bytes(b"other version")
    trash_dir = root / ".movieclaw-trash"

    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert (
            await recycle_file(
                session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
            )
            == "moved_to_trash"
        )
        await session.commit()
        trash_main = row.file_path
    assert not srt.exists() and not nfo.exists()  # 附属跟随进回收站
    trash_stem = os.path.splitext(os.path.basename(trash_main))[0]
    assert (trash_dir / f"{trash_stem}.zh.srt").exists()
    assert (trash_dir / f"{trash_stem}.nfo").exists()
    assert sibling.exists()  # 独立版本原地不动

    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert await restore_file(session, row) is True
        await session.commit()
    assert srt.exists() and nfo.exists()  # 附属随恢复搬回
    assert not (trash_dir / f"{trash_stem}.zh.srt").exists()

    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        assert await purge_file(session, row) is True
        await session.commit()
    assert not (trash_dir / f"{trash_stem}.zh.srt").exists()  # 附属随清理删除
    assert not (trash_dir / f"{trash_stem}.nfo").exists()
    assert sibling.exists()


@pytest.mark.asyncio
async def test_purge_refuses_disc_dir_containing_other_files(db, tmp_path):
    """爆炸半径保护：原盘目录里还有其他在案文件（如监听导入落在目录内的
    新版本）时拒绝整树删除；目录清空账面后才允许清理。"""
    root = tmp_path / "movies"
    disc = root / "某电影 (2020)" / "某电影.BluRay.原盘"
    (disc / "BDMV" / "STREAM").mkdir(parents=True)
    (disc / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"disc")
    inner_new = disc / "某电影.2160p.Remux.mkv"  # 站点目录结构原样落盘的新版本
    inner_new.write_bytes(b"new")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库-原盘嵌套", kind="movie", root_paths=[str(root)]
        )
        disc_row = LibraryFile(
            library_id=library.id, file_path=str(disc), size_bytes=4, source=FileSource.SCANNED
        )
        inner_row = LibraryFile(
            library_id=library.id,
            file_path=str(inner_new),
            size_bytes=3,
            source=FileSource.IMPORTED,
        )
        session.add(disc_row)
        session.add(inner_row)
        await session.commit()
        await session.refresh(disc_row)

        await recycle_file(
            session, disc_row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        assert await purge_file(session, disc_row) is False  # 目录内有别的在案文件
        assert disc.is_dir() and inner_new.exists()

        await session.delete(inner_row)
        await session.flush()
        assert await purge_file(session, disc_row) is True  # 账面清空后允许
        await session.commit()
    assert not disc.exists()


@pytest.mark.asyncio
async def test_delete_single_file_refuses_disc_dir_containing_other_version(db, tmp_path):
    """单文件删除的同款爆炸半径保护：旧原盘目录里住着新版本文件时，
    整目录删除必须拒绝并报可读错误。"""
    from movieclaw_api.services.library.items import delete_single_file
    from movieclaw_db.models import MediaItem
    from movieclaw_db.models.library import Library

    root = tmp_path / "movies"
    disc = root / "某电影 (2020)" / "某电影.BluRay.原盘"
    (disc / "BDMV").mkdir(parents=True)
    (disc / "BDMV" / "index.bdmv").write_bytes(b"disc")
    inner_new = disc / "某电影.2160p.Remux.mkv"
    inner_new.write_bytes(b"new")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库-原盘删除", kind="movie", root_paths=[str(root)]
        )
        item = MediaItem(kind="movie", tmdb_id=912, title="某电影", original_title="M", year=2020)
        session.add(item)
        await session.flush()
        rows = []
        for file_path in (disc, inner_new):
            row = LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                file_path=str(file_path),
                size_bytes=3,
                source=FileSource.SCANNED,
            )
            session.add(row)
            rows.append(row)
        await session.commit()
        for row in rows:
            await session.refresh(row)

        library_obj = await session.get(Library, library.id)
        result = await delete_single_file(session, library_obj, rows[0], rows)
        assert result.rows_deleted == 0
        assert any("目录内还有其他在案文件" in err for err in result.errors)
    assert disc.is_dir() and inner_new.exists()


@pytest.mark.asyncio
async def test_item_delete_covers_sibling_version_dirs(db, tmp_path):
    """同级版本目录规范（docs/design/disc-version-layout.md §2）：整条目
    删除按行收集条目目录，条目目录与直接躺根下的原盘版本目录都被整删，
    库根本身不动。"""
    from movieclaw_api.services.library.items import delete_item_files
    from movieclaw_db.models import MediaItem
    from movieclaw_db.models.library import Library

    root = tmp_path / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影 (2020).mkv").write_bytes(b"file")
    disc_version = root / "某电影 (2020) - 4K原盘"
    (disc_version / "BDMV").mkdir(parents=True)
    (disc_version / "BDMV" / "index.bdmv").write_bytes(b"disc")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库-版本目录", kind="movie", root_paths=[str(root)]
        )
        item = MediaItem(kind="movie", tmdb_id=913, title="某电影", original_title="M", year=2020)
        session.add(item)
        await session.flush()
        rows = []
        for path, container in ((entry / "某电影 (2020).mkv", "mkv"), (disc_version, "bluray")):
            row = LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                file_path=str(path),
                size_bytes=4,
                source=FileSource.SCANNED,
                container=container,
            )
            session.add(row)
            rows.append(row)
        await session.commit()
        for row in rows:
            await session.refresh(row)
        library_obj = await session.get(Library, library.id)
        result = await delete_item_files(session, library_obj, item.id, rows)
        assert result.errors == []
        assert result.rows_deleted == 2
    assert not entry.exists()
    assert not disc_version.exists()
    assert root.is_dir()  # 库根本身绝不动


@pytest.mark.asyncio
async def test_orphan_sweep_spares_tracked_sidecars(db, tmp_path):
    """孤儿清扫豁免在案主文件的附属文件：字幕随主文件进回收站时 mtime
    保留原值可能早已超期，不能被当孤儿提前扫掉——它归 purge 通道管。"""
    import time

    file_id, path, root = await _seed_file(db, tmp_path)
    srt = path.with_name(path.stem + ".zh.srt")
    srt.write_text("字幕")
    old = time.time() - 30 * 86400
    os.utime(srt, (old, old))  # 字幕 mtime 远超保留期
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        trash_stem = os.path.splitext(os.path.basename(row.file_path))[0]
    trash_dir = root / ".movieclaw-trash"
    trash_srt = trash_dir / f"{trash_stem}.zh.srt"
    assert trash_srt.exists()
    orphan = trash_dir / "上古遗留.mkv"
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (old, old))

    await sweep_orphan_trash()
    assert not orphan.exists()  # 真孤儿被清
    assert trash_srt.exists()  # 在案主文件的附属被豁免
