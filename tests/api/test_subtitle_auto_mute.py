"""「不再自动生成字幕」台账（issue #221 的第二层）。

只把失败任务从「需要处理」里藏起来是不够的：入库后自动生成的
"同一文件本进程只自动尝试一次"是**内存**集合，容器一重启就清零，
下次扫描收尾又会为同一个文件建一条新任务、再失败一次——用户会觉得
忽略根本没生效。台账把这个决定写进数据库，扫描前置查表短路。

本文件锁住三条：静音写得进、扫描确实跳过、撤销能真正解除。
"""

from __future__ import annotations

import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.subtitle_gen import auto
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    Job,
    JobStatus,
    Library,
    LibraryFile,
    MediaItem,
    SubtitleAutoMute,
)


@pytest_asyncio.fixture
async def mute_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'mute.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


async def _seed_library(mute_db) -> tuple[int, int]:
    """静音记录挂在 library_file 上，外键要求库与条目真实存在；同库复用。"""
    async with mute_db.session() as session:
        library = (
            await session.execute(select(Library).where(Library.name == "电影库"))
        ).scalar_one_or_none()
        if library is None:
            library = Library(name="电影库", kind="movie", root_paths=["/media"], sort_order=1)
            item = MediaItem(
                kind="movie", tmdb_id=300, title="测试电影", original_title="Test", year=2026
            )
            session.add_all([library, item])
            await session.commit()
            await session.refresh(library)
            await session.refresh(item)
        else:
            item = (await session.execute(select(MediaItem))).scalars().first()
        assert library.id is not None and item is not None and item.id is not None
        return library.id, item.id


async def _library_file(mute_db, path: str = "/media/Movie.mkv") -> int:
    library_id, media_item_id = await _seed_library(mute_db)
    async with mute_db.session() as session:
        row = LibraryFile(
            library_id=library_id,
            media_item_id=media_item_id,
            file_path=path,
            duration_seconds=6000,
            source="scanned",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.id is not None
        return row.id


async def _failed_subtitle_job(mute_db, file_id: int, target: str = "chs") -> Job:
    async with mute_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="subtitle.generate",
            subject="Movie.mkv",
            input_data={"file_id": file_id, "target_language": target},
        )
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.FAILED
        await session.commit()
        await session.refresh(row)
        return row


async def test_mute_from_job_records_file_and_language(mute_db) -> None:
    file_id = await _library_file(mute_db)
    job = await _failed_subtitle_job(mute_db, file_id)

    async with mute_db.session() as session:
        muted = await auto.mute_from_job(session, job, muted_by="admin:tester")

    assert muted is True
    async with mute_db.session() as session:
        rows = list((await session.execute(select(SubtitleAutoMute))).scalars())
    assert len(rows) == 1
    assert rows[0].library_file_id == file_id
    assert rows[0].target_language == "chs"
    assert rows[0].muted_by == "admin:tester"


async def test_mute_is_idempotent(mute_db) -> None:
    """同一 (文件, 语言) 重复静音不制造第二行——批量忽略会反复撞到它。"""
    file_id = await _library_file(mute_db)
    job = await _failed_subtitle_job(mute_db, file_id)

    async with mute_db.session() as session:
        await auto.mute_from_job(session, job)
    async with mute_db.session() as session:
        await auto.mute_from_job(session, job)

    async with mute_db.session() as session:
        rows = list((await session.execute(select(SubtitleAutoMute))).scalars())
    assert len(rows) == 1


async def test_muted_file_is_skipped_by_scan_even_after_restart(mute_db) -> None:
    """核心验收：静音跨进程有效。

    ``_attempted`` 是内存集合，这里显式清空来模拟一次容器重启——重启后
    扫描仍然必须跳过被静音的文件，否则忽略就是白点的。
    """
    file_id = await _library_file(mute_db)
    other_id = await _library_file(mute_db, "/media/Other.mkv")
    job = await _failed_subtitle_job(mute_db, file_id)

    async with mute_db.session() as session:
        await auto.mute_from_job(session, job)

    auto._attempted.clear()  # 模拟重启：内存护栏归零，只剩台账兜底

    muted = await auto._muted_file_ids("chs")
    assert file_id in muted
    assert other_id not in muted


async def test_mute_is_scoped_to_the_target_language(mute_db) -> None:
    """静音的是"这个文件的这种字幕"，换一种目标语言仍然照常自动生成。"""
    file_id = await _library_file(mute_db)
    job = await _failed_subtitle_job(mute_db, file_id, target="chs")

    async with mute_db.session() as session:
        await auto.mute_from_job(session, job)

    assert file_id in await auto._muted_file_ids("chs")
    assert file_id not in await auto._muted_file_ids("cht")


async def test_unmute_releases_both_ledger_and_memory_guard(mute_db) -> None:
    """撤销忽略必须真正解除静音，否则"撤销"只撤了一半。"""
    file_id = await _library_file(mute_db)
    job = await _failed_subtitle_job(mute_db, file_id)

    async with mute_db.session() as session:
        await auto.mute_from_job(session, job)
    auto._attempted.add(file_id)

    async with mute_db.session() as session:
        released = await auto.unmute_from_job(session, job)

    assert released is True
    assert await auto._muted_file_ids("chs") == set()
    # 内存护栏也要放开，否则本进程内仍然拒绝再次自动尝试
    assert file_id not in auto._attempted


async def test_non_subtitle_jobs_have_no_auto_source_to_mute(mute_db) -> None:
    """别的任务类型没有会自动重建它的来源，如实返回 False，不假装静音成功。"""
    async with mute_db.session() as session:
        created = await jobs.create_job(
            session, job_type="library.scan", input_data={"library_id": 1}
        )
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.FAILED
        await session.commit()
        await session.refresh(row)

    async with mute_db.session() as session:
        assert await auto.mute_from_job(session, row) is False

    async with mute_db.session() as session:
        rows = list((await session.execute(select(SubtitleAutoMute))).scalars())
    assert rows == []


async def test_deleting_the_file_takes_its_mute_with_it(mute_db) -> None:
    """外键级联：文件没了静音记录也该没，否则复用的行 id 会凭空继承静音。"""
    file_id = await _library_file(mute_db)
    job = await _failed_subtitle_job(mute_db, file_id)
    async with mute_db.session() as session:
        await auto.mute_from_job(session, job)

    async with mute_db.session() as session:
        await session.execute(
            LibraryFile.__table__.delete().where(LibraryFile.__table__.c.id == file_id)
        )
        await session.commit()

    async with mute_db.session() as session:
        rows = list((await session.execute(select(SubtitleAutoMute))).scalars())
    assert rows == []
