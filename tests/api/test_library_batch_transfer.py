"""批量条目转移：一次提交搬一批，且单条出问题不能带走整批。

覆盖用户场景「合并两个媒体库」的完整路径，以及三条批量独有的语义：
冲突逐条跳过而不阻断整批、连续失败熔断到 blocked（可从断点恢复）、
已完成成员靠检查点不重搬。
"""

from __future__ import annotations

import asyncio

import pytest_asyncio
from sqlmodel import select

from movieclaw_api.api.routes.libraries import (
    preview_batch_transfer,
    start_batch_transfer,
)
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.library import BatchTransferPayload
from movieclaw_api.services import jobs
from movieclaw_api.services.library import batch_transfer as batch
from movieclaw_api.services.library import transfer as transfer_svc
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, JobStatus, Library, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'batch.db'}")
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
    """下游媒体服务器与下载器在测试里都没配置，直接短路。"""

    async def _noop() -> None:
        return None

    async def _no_briefs():
        return []

    monkeypatch.setattr(
        "movieclaw_api.services.media_server_notify.notify_media_server_refresh", _noop
    )
    monkeypatch.setattr("movieclaw_api.api.routes.libraries._downloader_briefs", _no_briefs)


def _make_movie(root, name: str):
    entry = root / name
    entry.mkdir(parents=True)
    video = entry / f"{name}.mkv"
    video.write_bytes(b"x" * 100)
    return entry, video


async def _setup(db, tmp_path, *, names: list[str]):
    """一个源电影库（若干部）+ 一个空的目标电影库。"""
    source_root = tmp_path / "media" / "混放"
    target_root = tmp_path / "media" / "电影"
    source_root.mkdir(parents=True)
    target_root.mkdir(parents=True)

    entries = {}
    async with db.session() as session:
        repo = LibraryRepository(session)
        source = await repo.create(name="混放库", kind="movie", root_paths=[str(source_root)])
        target = await repo.create(name="电影库", kind="movie", root_paths=[str(target_root)])
        assert source.id and target.id
        for index, name in enumerate(names, start=1):
            entry, video = _make_movie(source_root, name)
            item = MediaItem(kind="movie", tmdb_id=2000 + index, title=name, original_title=name)
            session.add(item)
            await session.flush()
            assert item.id
            session.add(
                LibraryFile(
                    library_id=source.id,
                    media_item_id=item.id,
                    file_path=str(video),
                    size_bytes=100,
                    source=FileSource.SCANNED,
                )
            )
            entries[name] = (item.id, entry)
        await session.commit()
        return source.id, target.id, source_root, target_root, entries


async def _drain(source_id: int) -> dict:
    """等批量作业到终态并取回结论。"""
    for _ in range(800):
        async with get_database().session() as session:
            latest = await jobs.latest_job_for_resource(
                session, "library", source_id, job_type=batch.JOB_TYPE
            )
        if latest is not None and latest.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.BLOCKED,
        ):
            return {"status": latest.status, "result": latest.result or {}, "job": latest}
        await asyncio.sleep(0.01)
    raise AssertionError("批量转移作业没有在时限内到达终态")


async def _start(source_id: int, payload: BatchTransferPayload):
    async with get_database().session() as session:
        return await start_batch_transfer(source_id, payload, session=session)


# ---------------------------------------------------------------------------
# 选择集
# ---------------------------------------------------------------------------


async def test_resolve_members_keeps_caller_order_and_drops_strangers(db, tmp_path) -> None:
    """成员顺序按用户给的来（墙上的选择顺序就是他心里的顺序），不在库里的丢掉。"""
    source_id, _, _, _, entries = await _setup(db, tmp_path, names=["甲", "乙", "丙"])
    a, b, c = (entries[n][0] for n in ("甲", "乙", "丙"))

    async with get_database().session() as session:
        members = await batch.resolve_members(
            session, source_id, media_item_ids=[c, a, 999999], all_items=False
        )
    assert [m.media_item_id for m in members] == [c, a]
    assert [m.title for m in members] == ["丙", "甲"]

    async with get_database().session() as session:
        everything = await batch.resolve_members(
            session, source_id, media_item_ids=None, all_items=True
        )
    assert {m.media_item_id for m in everything} == {a, b, c}


async def test_empty_selection_is_rejected_with_actionable_message(db, tmp_path) -> None:
    """空选择集不能静默跑一个什么都不做的作业。"""
    source_id, target_id, _, _, _ = await _setup(db, tmp_path, names=["甲"])
    async with get_database().session() as session:
        try:
            await preview_batch_transfer(
                source_id,
                BatchTransferPayload(target_library_id=target_id),
                session=session,
            )
        except BadRequestException as exc:
            assert "all_items" in str(exc)
        else:
            raise AssertionError("空选择集应当被拒绝")


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------


async def test_preview_reports_same_disk_costs_nothing(db, tmp_path) -> None:
    """同盘合库：不需要额外空间，也不该报硬链接——这是最好的情况，别吓用户。"""
    source_id, target_id, _, _, entries = await _setup(db, tmp_path, names=["甲", "乙"])
    async with get_database().session() as session:
        response = await preview_batch_transfer(
            source_id,
            BatchTransferPayload(
                target_library_id=target_id,
                media_item_ids=[entries["甲"][0], entries["乙"][0]],
            ),
            session=session,
        )
    view = response.data
    assert view.selected == 2
    assert view.movable == 2
    assert view.cross_device_items == 0
    assert view.target_required_bytes == 0
    assert view.hardlinked_items == 0
    assert view.blocked == []


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


async def test_batch_moves_every_member_and_reassigns_ledger(db, tmp_path) -> None:
    """一次提交把整批搬到目标库：磁盘目录搬走、台账改挂新库。"""
    source_id, target_id, _, target_root, entries = await _setup(
        db, tmp_path, names=["甲", "乙", "丙"]
    )
    await _start(source_id, BatchTransferPayload(target_library_id=target_id, all_items=True))
    outcome = await _drain(source_id)

    assert outcome["status"] is JobStatus.SUCCEEDED
    assert outcome["result"]["moved"] == 3
    assert outcome["result"]["failed"] == 0
    assert outcome["result"]["skipped"] == 0
    for name, (item_id, entry) in entries.items():
        assert not entry.exists(), f"{name} 的源目录应已搬走"
        assert (target_root / name).is_dir()
        async with get_database().session() as session:
            row = (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == item_id)
                )
            ).scalar_one()
            assert row.library_id == target_id
            assert row.file_path.startswith(str(target_root))


async def test_conflict_skips_one_member_and_keeps_going(db, tmp_path) -> None:
    """目标已有同名目录只是这一条跳过，其余照搬——这是批量与单条目最大的语义差别。

    单条目的「目标已有同名目录」等于整个操作失败；批量里 593 部中的 7 部冲突
    不能把另外 586 部一起废掉。
    """
    source_id, target_id, _, target_root, entries = await _setup(
        db, tmp_path, names=["甲", "乙", "丙"]
    )
    (target_root / "乙").mkdir()  # 目标位置已被占用

    await _start(source_id, BatchTransferPayload(target_library_id=target_id, all_items=True))
    outcome = await _drain(source_id)

    assert outcome["status"] is JobStatus.SUCCEEDED
    assert outcome["result"]["moved"] == 2
    assert outcome["result"]["skipped"] == 1
    assert outcome["result"]["failed"] == 0
    skips = outcome["result"]["skips"]
    assert len(skips) == 1 and skips[0]["title"] == "乙"
    # 跳过的那个原地不动，其余两个搬走了
    assert entries["乙"][1].exists()
    assert not entries["甲"][1].exists()
    assert not entries["丙"][1].exists()


async def test_on_conflict_fail_stops_the_whole_batch(db, tmp_path) -> None:
    """脚本场景要的是「要么全成、要么不动」：fail 策略遇冲突整批中止。"""
    source_id, target_id, _, target_root, _ = await _setup(db, tmp_path, names=["甲", "乙"])
    (target_root / "甲").mkdir()

    await _start(
        source_id,
        BatchTransferPayload(target_library_id=target_id, all_items=True, on_conflict="fail"),
    )
    outcome = await _drain(source_id)
    assert outcome["status"] is JobStatus.FAILED
    assert "已按 fail 策略停止整批" in (outcome["job"].error or {}).get("message", "")


async def test_consecutive_failures_trip_the_breaker_into_blocked(
    db, tmp_path, monkeypatch
) -> None:
    """连着 5 个失败一定是系统性问题：停到 blocked 等用户处理，不是继续糟蹋剩下的。

    停到 blocked（而不是 failed）是关键——blocked 的恢复是原地入队、保留检查点，
    用户腾出空间点一下继续就从断点接着搬。
    """
    names = [f"片{i}" for i in range(8)]
    source_id, target_id, _, _, _ = await _setup(db, tmp_path, names=names)

    async def _always_broken(*args, **kwargs):
        raise RuntimeError("模拟的搬运故障")

    monkeypatch.setattr(transfer_svc, "_move_resumable", _always_broken)

    await _start(source_id, BatchTransferPayload(target_library_id=target_id, all_items=True))
    outcome = await _drain(source_id)

    assert outcome["status"] is JobStatus.BLOCKED
    message = (outcome["job"].error or {}).get("message", "")
    assert "连续" in message and "暂停" in message
    # 熔断时机对：不是跑完 8 个才报，而是第 5 个就停
    progress = outcome["job"].progress or {}
    assert (progress.get("details") or {}).get("failed") == batch.MAX_CONSECUTIVE_FAILURES


async def test_checkpoint_skips_members_already_done(db, tmp_path) -> None:
    """检查点里的成员不再重搬——重启/重试不会把已完成的又搬一遍或重复计数。"""
    source_id, target_id, _, target_root, entries = await _setup(db, tmp_path, names=["甲", "乙"])
    done_id = entries["甲"][0]

    # 手工把「甲」写进检查点，模拟上一轮已经搬完它之后崩溃
    async with get_database().session() as session:
        source = await session.get(Library, source_id)
        target = await session.get(Library, target_id)
        assert source and target
        members = await batch.resolve_members(
            session, source_id, media_item_ids=None, all_items=True
        )
        created = await batch.enqueue_batch_transfer_job(
            session,
            source=source,
            target=target,
            members=members,
            on_conflict="skip",
        )
        job = created.job
        job.progress = {
            **(job.progress or {}),
            "details": {**((job.progress or {}).get("details") or {}), "done": [done_id]},
        }
        session.add(job)
        await session.commit()

    outcome = await _drain(source_id)
    assert outcome["status"] is JobStatus.SUCCEEDED
    # 只搬了「乙」；「甲」被检查点跳过，源目录仍在原位
    assert outcome["result"]["moved"] == 1
    assert entries["甲"][1].exists()
    assert (target_root / "乙").is_dir()


# ---------------------------------------------------------------------------
# 同名合并（--on-conflict merge）
# ---------------------------------------------------------------------------


async def _add_target_version(target_id: int, target_root, name: str, item_id: int) -> None:
    """在目标库里先放一个同一部作品的其他版本（同锚）。"""
    entry = target_root / name
    entry.mkdir(parents=True, exist_ok=True)
    video = entry / f"{name}.mkv"
    video.write_bytes(b"y" * 200)
    async with get_database().session() as session:
        session.add(
            LibraryFile(
                library_id=target_id,
                media_item_id=item_id,
                file_path=str(video),
                size_bytes=200,
                source=FileSource.SCANNED,
                resolution="2160p",
            )
        )
        await session.commit()


async def test_merge_folds_same_anchor_versions_into_one_directory(db, tmp_path) -> None:
    """同一部作品的其他版本：并进同一个条目目录，撞名的退让成多版本命名。

    产出形态与入库、洗版、整理完全一致（`标题 - 标签.ext`），Jellyfin/Emby
    认得，整理重跑也不会把它改回去。
    """
    source_id, target_id, _, target_root, entries = await _setup(db, tmp_path, names=["甲"])
    item_id = entries["甲"][0]
    await _add_target_version(target_id, target_root, "甲", item_id)

    # 给源文件一个可用的版本标签，撞名时才有东西可退让
    async with get_database().session() as session:
        row = (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.library_id == source_id,
                    LibraryFile.media_item_id == item_id,
                )
            )
        ).scalar_one()
        row.resolution = "1080p"
        session.add(row)
        await session.commit()

    await _start(
        source_id,
        BatchTransferPayload(target_library_id=target_id, all_items=True, on_conflict="merge"),
    )
    outcome = await _drain(source_id)

    assert outcome["status"] is JobStatus.SUCCEEDED
    assert outcome["result"]["moved"] == 1
    assert outcome["result"]["skipped"] == 0
    # 目标目录里两个版本共存，原有那份一字未动（合并只增不减）
    assert (target_root / "甲" / "甲.mkv").read_bytes() == b"y" * 200
    assert (target_root / "甲" / "甲 - 1080p.mkv").is_file()
    assert not entries["甲"][1].exists()


async def test_merge_still_skips_a_different_work_with_the_same_name(db, tmp_path) -> None:
    """只是目录重名的另一部片：任何策略下都跳过——同名是坏判据，同锚才是好判据。"""
    source_id, target_id, _, target_root, entries = await _setup(db, tmp_path, names=["甲", "乙"])
    stranger_root = target_root / "甲"
    stranger_root.mkdir()
    (stranger_root / "甲.mkv").write_bytes(b"z" * 50)
    async with get_database().session() as session:
        stranger = MediaItem(kind="movie", tmdb_id=99001, title="另一部同名片", original_title="X")
        session.add(stranger)
        await session.flush()
        assert stranger.id
        session.add(
            LibraryFile(
                library_id=target_id,
                media_item_id=stranger.id,
                file_path=str(stranger_root / "甲.mkv"),
                size_bytes=50,
                source=FileSource.SCANNED,
            )
        )
        await session.commit()

    await _start(
        source_id,
        BatchTransferPayload(target_library_id=target_id, all_items=True, on_conflict="merge"),
    )
    outcome = await _drain(source_id)

    # 「甲」跳过、「乙」照搬——冲突不阻断整批
    assert outcome["result"]["moved"] == 1
    assert outcome["result"]["skipped"] == 1
    # 别人的文件一字未动，「甲」的源目录也还在原位
    assert (stranger_root / "甲.mkv").read_bytes() == b"z" * 50
    assert entries["甲"][1].exists()
    assert not entries["乙"][1].exists()


async def test_merge_without_version_label_skips_instead_of_overwriting(db, tmp_path) -> None:
    """退让不出名字（没有分辨率/片源/发布组）时跳过——绝不覆盖是不能让的底线。"""
    source_id, target_id, _, target_root, entries = await _setup(db, tmp_path, names=["甲"])
    await _add_target_version(target_id, target_root, "甲", entries["甲"][0])

    await _start(
        source_id,
        BatchTransferPayload(target_library_id=target_id, all_items=True, on_conflict="merge"),
    )
    outcome = await _drain(source_id)

    # 目录并了，但那个撞名的文件没搬（记在 skips 里），目标原文件一字未动
    assert (target_root / "甲" / "甲.mkv").read_bytes() == b"y" * 200
    assert not (target_root / "甲" / "甲 - .mkv").exists()
    assert outcome["result"]["failed"] == 0


async def test_status_endpoint_reports_batch_outcome(db, tmp_path) -> None:
    """批量与归并的结论要能从既有的转移状态接口读到：三种搬运共用一个口径。

    跳过与失败分成两栏——跳过是用户在预检里点头同意过的，失败才是意外，
    合成一个「问题数」前端就没法只给失败那部分提供重试入口。
    """
    from movieclaw_api.api.routes.libraries import get_transfer_status

    source_id, target_id, _, target_root, entries = await _setup(
        db, tmp_path, names=["甲", "乙"]
    )
    (target_root / "乙").mkdir()  # 这一条会被跳过

    await _start(source_id, BatchTransferPayload(target_library_id=target_id, all_items=True))
    await _drain(source_id)

    async with get_database().session() as session:
        view = (await get_transfer_status(source_id, session=session)).data
    assert view.running is False
    assert view.moved_items == 1
    assert view.skipped_items == 1
    assert view.failed_items == 0
    assert len(view.skips) == 1 and "同名" in view.skips[0]
    assert view.errors == []
    # 目标库那一侧查到的是同一份结论
    async with get_database().session() as session:
        mirrored = (await get_transfer_status(target_id, session=session)).data
    assert mirrored.moved_items == 1
