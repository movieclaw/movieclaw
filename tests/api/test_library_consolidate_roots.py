"""库内根路径归并：换盘、换挂载点、把多级分类目录拍平成一层。

与批量转移共用同一个搬运引擎（跨库转移的目标根是目标库主根、台账改归属；
库内归并的目标根是指定的那个根、台账只改路径），所以这里只覆盖归并独有的
三件事：选择集按「根」而不是按条目、root_paths 的先加后删、以及没搬干净时
绝不摘源根。
"""

from __future__ import annotations

import asyncio

import pytest_asyncio
from sqlmodel import select

from movieclaw_api.api.routes.libraries import (
    preview_consolidate_roots,
    start_consolidate_roots,
)
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.library import ConsolidateRootsPayload
from movieclaw_api.services import jobs
from movieclaw_api.services.library import batch_transfer as batch
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, JobStatus, Library, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'consolidate.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    await jobs.init_job_dispatcher(max_parallel=1)
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _no_downstream(monkeypatch):
    async def _noop() -> None:
        return None

    async def _no_briefs():
        return []

    monkeypatch.setattr(
        "movieclaw_api.services.media_server_notify.notify_media_server_refresh", _noop
    )
    monkeypatch.setattr("movieclaw_api.api.routes.libraries._downloader_briefs", _no_briefs)


async def _setup(db, tmp_path):
    """一个电影库，内容散在「华语」「欧美」两个分类根下——典型的历史多级目录。"""
    base = tmp_path / "media"
    cn = base / "华语"
    us = base / "欧美"
    flat = base / "电影"
    for d in (cn, us, flat):
        d.mkdir(parents=True)

    entries = {}
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(cn), str(us)]
        )
        assert library.id
        for index, (root, name) in enumerate(
            [(cn, "甲 (2020)"), (cn, "乙 (2021)"), (us, "丙 (2022)")], start=1
        ):
            entry = root / name
            entry.mkdir()
            video = entry / f"{name}.mkv"
            video.write_bytes(b"x" * 100)
            item = MediaItem(kind="movie", tmdb_id=3000 + index, title=name, original_title=name)
            session.add(item)
            await session.flush()
            assert item.id
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item.id,
                    file_path=str(video),
                    size_bytes=100,
                    source=FileSource.SCANNED,
                )
            )
            entries[name] = (item.id, entry)
        await session.commit()
        return library.id, cn, us, flat, entries


async def _drain(library_id: int) -> dict:
    for _ in range(800):
        async with get_database().session() as session:
            latest = await jobs.latest_job_for_resource(
                session, "library", library_id, job_type=batch.CONSOLIDATE_JOB_TYPE
            )
        if latest is not None and latest.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.BLOCKED,
        ):
            return {"status": latest.status, "result": latest.result or {}, "job": latest}
        await asyncio.sleep(0.01)
    raise AssertionError("归并作业没有在时限内到达终态")


async def _roots(library_id: int) -> list[str]:
    async with get_database().session() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        return [r.rstrip("/") for r in library.root_paths]


# ---------------------------------------------------------------------------


async def test_preview_defaults_to_every_other_root(db, tmp_path) -> None:
    """--from 留空 = 除目标根外的全部根，这正是「拍平多级目录」最常见的意图。"""
    library_id, cn, us, _, _ = await _setup(db, tmp_path)
    async with get_database().session() as session:
        response = await preview_consolidate_roots(
            library_id, ConsolidateRootsPayload(into=str(cn)), session=session
        )
    view = response.data
    assert view.from_roots == [str(us)]
    assert view.into_is_new_root is False
    assert view.selected == 1  # 只有「丙」在欧美根下


async def test_preview_flags_a_brand_new_target_root(db, tmp_path) -> None:
    """归并到一个还不在配置里的新路径（换盘场景）要如实标出来。"""
    library_id, _, _, flat, _ = await _setup(db, tmp_path)
    async with get_database().session() as session:
        response = await preview_consolidate_roots(
            library_id, ConsolidateRootsPayload(into=str(flat)), session=session
        )
    assert response.data.into_is_new_root is True
    assert response.data.selected == 3  # 两个旧根下的全部条目


async def test_unknown_source_root_is_rejected(db, tmp_path) -> None:
    """源根必须是这个库真有的根，否则用户多半打错了路径。"""
    library_id, cn, _, _, _ = await _setup(db, tmp_path)
    async with get_database().session() as session:
        try:
            await preview_consolidate_roots(
                library_id,
                ConsolidateRootsPayload(into=str(cn), from_roots=["/不存在的根"]),
                session=session,
            )
        except BadRequestException as exc:
            assert "不在媒体库的配置里" in str(exc)
        else:
            raise AssertionError("不存在的源根应当被拒绝")


async def test_consolidation_moves_everything_and_settles_root_paths(db, tmp_path) -> None:
    """归并到新根：文件搬过去、台账改路径、目标根先加进配置、源根最后摘掉。"""
    library_id, cn, us, flat, entries = await _setup(db, tmp_path)

    async with get_database().session() as session:
        await start_consolidate_roots(
            library_id, ConsolidateRootsPayload(into=str(flat)), session=session
        )
    outcome = await _drain(library_id)

    assert outcome["status"] is JobStatus.SUCCEEDED
    assert outcome["result"]["moved"] == 3
    for name, (item_id, entry) in entries.items():
        assert not entry.exists(), f"{name} 的旧目录应已搬走"
        assert (flat / name).is_dir()
        async with get_database().session() as session:
            row = (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == item_id)
                )
            ).scalar_one()
            # 库内归并只改路径，不改归属
            assert row.library_id == library_id
            assert row.file_path.startswith(str(flat))
    # 先加后删：最终只剩目标根
    assert await _roots(library_id) == [str(flat)]


async def test_source_roots_are_kept_when_something_was_not_moved(db, tmp_path) -> None:
    """还剩内容就绝不摘源根——摘了那些台账会瞬间指到库根之外，下次扫描全标 missing。"""
    library_id, cn, us, flat, _ = await _setup(db, tmp_path)
    (flat / "丙 (2022)").mkdir()  # 目标位置已被占用 → 这一条会被跳过

    async with get_database().session() as session:
        await start_consolidate_roots(
            library_id, ConsolidateRootsPayload(into=str(flat)), session=session
        )
    outcome = await _drain(library_id)

    assert outcome["status"] is JobStatus.SUCCEEDED
    assert outcome["result"]["skipped"] == 1
    roots = await _roots(library_id)
    # 目标根已加入（先加），但两个源根一个都没摘（因为没搬干净）
    assert str(flat) in roots
    assert str(cn) in roots and str(us) in roots
