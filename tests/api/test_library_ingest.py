"""下载监听导入的测试。

覆盖：完成检测（下载器权威信号优先、进行中标记阻断并重置计时、指纹
静默窗口、逐文件探测门禁）、硬链接/复制两种搬运策略、电影/剧集的规范
落位、台账幂等（同指纹不重复处理、指纹变化自动重试、季包增量补集）、
识别失败的失败记录、配置校验（监听目录与根路径重叠拒绝）。识别与季集
解析依赖 NER 模型与 TMDB，此处打桩——识别链本体由扫描器测试覆盖。
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.ingest as ingest_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services import jobs
from movieclaw_api.services.import_watch_config import ImportWatchConfigService
from movieclaw_api.services.library.layout import explicit_unit
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloaderClient,
    FileSource,
    ImportWatch,
    IngestEntry,
    IngestStatus,
    Job,
    JobResource,
    JobStatus,
    Library,
    LibraryFile,
    ManualDownloadIntent,
    MediaItem,
)
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.downloader_client import ClientType
from movieclaw_db.models.manual_download_intent import MANUAL_DOWNLOAD_INTENT_TTL
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

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
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'ingest.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    # 每个测试独立的静默观察表/挂起表 + 立即落定的静默窗口（两次巡检即可
    # 导入：第一轮记录指纹，第二轮确认稳定）；下载器概览缓存清空（默认无
    # 下载器 → 权威信号缺席 → 走启发式路径）
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "_deferred", {})
    monkeypatch.setattr(ingest_mod, "_failed_retry", {})
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(db, *, kind: MediaKind, root) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name=f"测试{kind.value}库", kind=kind.value, root_paths=[str(root)]
        )
        return row.id


async def _make_rule(db, *, library_id: int, source, strategy="hardlink") -> None:
    async with db.session() as session:
        session.add(ImportWatch(source_path=str(source), strategy=strategy, library_id=library_id))
        await session.commit()


async def _make_item(db, *, kind: MediaKind, title: str, year: int) -> MediaItem:
    async with db.session() as session:
        item = MediaItem(kind=kind.value, tmdb_id=300, title=title, original_title=title, year=year)
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def _get_library(db, library_id: int) -> Library:
    async with db.session() as session:
        row = await session.get(Library, library_id)
        assert row is not None
        return row


async def _add_ingest_job(session, *, job_id: str, entry_id: int, status: JobStatus) -> Job:
    row = Job(id=job_id, job_type="library.ingest", status=status, input_data={})
    session.add(row)
    session.add(
        JobResource(
            job_id=job_id,
            resource_type="ingest_entry",
            resource_id=str(entry_id),
        )
    )
    return row


@pytest.mark.asyncio
async def test_ignore_entry_cancels_every_active_job_without_rewriting_history(db, tmp_path):
    """多批次/重试可留下多条活跃作业；忽略必须全部收口且不串资源。"""
    async with db.session() as session:
        ignored = IngestEntry(
            entry_path=str(tmp_path / "watch" / "待忽略"),
            fingerprint="fp-ignored",
            status=IngestStatus.PENDING,
        )
        unrelated = IngestEntry(
            entry_path=str(tmp_path / "watch" / "其他条目"),
            fingerprint="fp-unrelated",
            status=IngestStatus.PENDING,
        )
        session.add(ignored)
        session.add(unrelated)
        await session.commit()
        await session.refresh(ignored)
        await session.refresh(unrelated)
        assert ignored.id is not None and unrelated.id is not None

        for suffix, status in (
            ("queued", JobStatus.QUEUED),
            ("waiting", JobStatus.WAITING),
            ("retry", JobStatus.RETRY_WAIT),
            ("blocked", JobStatus.BLOCKED),
            ("running", JobStatus.RUNNING),
        ):
            await _add_ingest_job(
                session,
                job_id=f"job_ignored_{suffix}",
                entry_id=ignored.id,
                status=status,
            )
        cancelling = await _add_ingest_job(
            session,
            job_id="job_ignored_cancelling",
            entry_id=ignored.id,
            status=JobStatus.CANCELLING,
        )
        cancelling.cancel_requested_by = "prior-request"
        await _add_ingest_job(
            session,
            job_id="job_ignored_succeeded",
            entry_id=ignored.id,
            status=JobStatus.SUCCEEDED,
        )
        await _add_ingest_job(
            session,
            job_id="job_ignored_failed",
            entry_id=ignored.id,
            status=JobStatus.FAILED,
        )
        await _add_ingest_job(
            session,
            job_id="job_unrelated_blocked",
            entry_id=unrelated.id,
            status=JobStatus.BLOCKED,
        )
        await session.commit()

        await ingest_mod.ignore_entry(session, ignored.id)

    async with db.session() as session:
        record = await session.get(IngestEntry, ignored.id)
        assert record is not None and record.status == IngestStatus.IGNORED
        for suffix in ("queued", "waiting", "retry", "blocked"):
            row = await session.get(Job, f"job_ignored_{suffix}")
            assert row is not None and row.status == JobStatus.CANCELLED
            assert row.cancel_requested_by == "监听导入清单"
        running = await session.get(Job, "job_ignored_running")
        cancelling = await session.get(Job, "job_ignored_cancelling")
        assert running is not None and running.status == JobStatus.CANCELLING
        assert running.cancel_requested_by == "监听导入清单"
        assert cancelling is not None and cancelling.status == JobStatus.CANCELLING
        assert cancelling.cancel_requested_by == "prior-request"
        succeeded = await session.get(Job, "job_ignored_succeeded")
        failed = await session.get(Job, "job_ignored_failed")
        unrelated = await session.get(Job, "job_unrelated_blocked")
        assert succeeded is not None and succeeded.status == JobStatus.SUCCEEDED
        assert failed is not None and failed.status == JobStatus.FAILED
        assert unrelated is not None and unrelated.status == JobStatus.BLOCKED


@pytest.mark.asyncio
async def test_ignored_entry_job_reconcile_repairs_old_rows_once(db, tmp_path):
    """升级启动对账会修旧数据；再次运行无写入，未忽略条目和终态不受影响。"""
    async with db.session() as session:
        ignored = IngestEntry(
            entry_path=str(tmp_path / "watch" / "旧版已忽略"),
            fingerprint="fp-old-ignored",
            status=IngestStatus.IGNORED,
        )
        pending = IngestEntry(
            entry_path=str(tmp_path / "watch" / "仍待处理"),
            fingerprint="fp-pending",
            status=IngestStatus.PENDING,
        )
        session.add(ignored)
        session.add(pending)
        await session.commit()
        await session.refresh(ignored)
        await session.refresh(pending)
        assert ignored.id is not None and pending.id is not None
        await _add_ingest_job(
            session,
            job_id="job_old_queued",
            entry_id=ignored.id,
            status=JobStatus.QUEUED,
        )
        await _add_ingest_job(
            session,
            job_id="job_old_blocked",
            entry_id=ignored.id,
            status=JobStatus.BLOCKED,
        )
        await _add_ingest_job(
            session,
            job_id="job_old_cancelled",
            entry_id=ignored.id,
            status=JobStatus.CANCELLED,
        )
        await _add_ingest_job(
            session,
            job_id="job_pending_blocked",
            entry_id=pending.id,
            status=JobStatus.BLOCKED,
        )
        await session.commit()

    assert await ingest_mod.reconcile_ignored_entry_jobs() == 2
    assert await ingest_mod.reconcile_ignored_entry_jobs() == 0

    async with db.session() as session:
        queued = await session.get(Job, "job_old_queued")
        blocked = await session.get(Job, "job_old_blocked")
        terminal = await session.get(Job, "job_old_cancelled")
        unrelated = await session.get(Job, "job_pending_blocked")
        assert queued is not None and queued.status == JobStatus.CANCELLED
        assert blocked is not None and blocked.status == JobStatus.CANCELLED
        assert queued.cancel_requested_by == "system:ignored-ingest-reconcile"
        assert blocked.cancel_requested_by == "system:ignored-ingest-reconcile"
        assert terminal is not None and terminal.status == JobStatus.CANCELLED
        assert unrelated is not None and unrelated.status == JobStatus.BLOCKED


@pytest.mark.asyncio
async def test_ignored_entry_job_reconcile_failure_does_not_block_startup(db, monkeypatch):
    """旧数据清理是自愈项：短暂数据库异常只记日志，不能拖垮应用启动。"""

    async def fail(*_args, **_kwargs):
        raise RuntimeError("temporary database failure")

    monkeypatch.setattr(ingest_mod, "_cancel_active_entry_jobs", fail)
    assert await ingest_mod.reconcile_ignored_entry_jobs() == 0


@pytest.mark.asyncio
async def test_disc_batch_job_reconcile_cancels_only_active_disc_batches(db):
    """升级收口原盘分段 Job 与 pending 台账；普通批次和无关终态不被改写。"""
    async with db.session() as session:
        direct_entry = IngestEntry(
            entry_path="/watch/disc-direct",
            fingerprint="ready:direct",
            status=IngestStatus.PENDING,
            message="已取最大文件为正片，忽略其余 37 个视频",
        )
        nested_entry = IngestEntry(
            entry_path="/watch/disc-nested",
            fingerprint="ready:nested",
            status=IngestStatus.PENDING,
        )
        episode_entry = IngestEntry(
            entry_path="/watch/episode",
            fingerprint="ready:episode",
            status=IngestStatus.PENDING,
        )
        succeeded_entry = IngestEntry(
            entry_path="/watch/disc-succeeded",
            fingerprint="ready:succeeded",
            status=IngestStatus.PENDING,
        )
        previously_reconciled_entry = IngestEntry(
            entry_path="/watch/disc-already-cancelled",
            fingerprint="ready:already-cancelled",
            status=IngestStatus.PENDING,
        )
        session.add_all(
            [
                direct_entry,
                nested_entry,
                episode_entry,
                succeeded_entry,
                previously_reconciled_entry,
            ]
        )
        await session.flush()
        assert direct_entry.id is not None
        assert nested_entry.id is not None
        assert episode_entry.id is not None
        assert succeeded_entry.id is not None
        assert previously_reconciled_entry.id is not None
        session.add_all(
            [
                Job(
                    id="job_disc_direct",
                    job_type="library.ingest",
                    status=JobStatus.BLOCKED,
                    input_data={"ready_files": [{"path": "BDMV/STREAM/00001.m2ts"}]},
                ),
                Job(
                    id="job_disc_nested",
                    job_type="library.ingest",
                    status=JobStatus.QUEUED,
                    input_data={
                        "ready_files": [
                            {"path": "电影一/VIDEO_TS/VTS_01_1.VOB"},
                        ]
                    },
                ),
                Job(
                    id="job_episode_blocked",
                    job_type="library.ingest",
                    status=JobStatus.BLOCKED,
                    input_data={"ready_files": [{"path": "Season 01/S01E01.mkv"}]},
                ),
                Job(
                    id="job_disc_succeeded",
                    job_type="library.ingest",
                    status=JobStatus.SUCCEEDED,
                    input_data={"ready_files": [{"path": "BDMV/STREAM/00002.m2ts"}]},
                ),
                Job(
                    id="job_disc_already_cancelled",
                    job_type="library.ingest",
                    status=JobStatus.CANCELLED,
                    cancel_requested_by="system:disc-batch-reconcile",
                    input_data={"ready_files": [{"path": "BDMV/STREAM/00003.m2ts"}]},
                ),
                JobResource(
                    job_id="job_disc_direct",
                    resource_type="ingest_entry",
                    resource_id=str(direct_entry.id),
                ),
                JobResource(
                    job_id="job_disc_nested",
                    resource_type="ingest_entry",
                    resource_id=str(nested_entry.id),
                ),
                JobResource(
                    job_id="job_episode_blocked",
                    resource_type="ingest_entry",
                    resource_id=str(episode_entry.id),
                ),
                JobResource(
                    job_id="job_disc_succeeded",
                    resource_type="ingest_entry",
                    resource_id=str(succeeded_entry.id),
                ),
                JobResource(
                    job_id="job_disc_already_cancelled",
                    resource_type="ingest_entry",
                    resource_id=str(previously_reconciled_entry.id),
                ),
            ]
        )
        await session.commit()

    assert await ingest_mod.reconcile_disc_batch_jobs() == 2
    assert await ingest_mod.reconcile_disc_batch_jobs() == 0

    async with db.session() as session:
        direct = await session.get(Job, "job_disc_direct")
        nested = await session.get(Job, "job_disc_nested")
        episode = await session.get(Job, "job_episode_blocked")
        succeeded = await session.get(Job, "job_disc_succeeded")
        direct_record = await session.get(IngestEntry, direct_entry.id)
        nested_record = await session.get(IngestEntry, nested_entry.id)
        episode_record = await session.get(IngestEntry, episode_entry.id)
        succeeded_record = await session.get(IngestEntry, succeeded_entry.id)
        previously_reconciled_record = await session.get(
            IngestEntry, previously_reconciled_entry.id
        )
    assert direct is not None and direct.status == JobStatus.CANCELLED
    assert nested is not None and nested.status == JobStatus.CANCELLED
    assert direct.cancel_requested_by == "system:disc-batch-reconcile"
    assert nested.cancel_requested_by == "system:disc-batch-reconcile"
    assert episode is not None and episode.status == JobStatus.BLOCKED
    assert succeeded is not None and succeeded.status == JobStatus.SUCCEEDED
    assert direct_record is not None and direct_record.status == IngestStatus.SKIPPED
    assert nested_record is not None and nested_record.status == IngestStatus.SKIPPED
    assert "旧版原盘流分段" in (direct_record.message or "")
    assert episode_record is not None and episode_record.status == IngestStatus.PENDING
    assert succeeded_record is not None and succeeded_record.status == IngestStatus.PENDING
    assert (
        previously_reconciled_record is not None
        and previously_reconciled_record.status == IngestStatus.SKIPPED
    )


@pytest.mark.asyncio
async def test_reconciled_legacy_disc_job_is_replaced_by_complete_disc_ingest(
    db, tmp_path, monkeypatch
):
    """升级取消旧分段 Job 后自动建立整盘 Job，不能被“已取消”通用门禁拦住。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="升级原盘", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    entry = watch / "Legacy.Disc.2026"
    stream = entry / "BDMV" / "STREAM" / "00001.m2ts"
    stream.parent.mkdir(parents=True)
    stream.write_bytes(b"complete-main-feature")
    fingerprint = ingest_mod._snapshot(entry).fingerprint
    async with db.session() as session:
        record = IngestEntry(
            entry_path=str(entry),
            fingerprint=fingerprint,
            status=IngestStatus.PENDING,
            message="已取最大文件为正片，忽略其余 37 个视频",
        )
        session.add(record)
        await session.flush()
        assert record.id is not None
        old_job = Job(
            id="job_legacy_disc_upgrade",
            job_type="library.ingest",
            status=JobStatus.BLOCKED,
            input_data={"ready_files": [{"path": "BDMV/STREAM/00001.m2ts"}]},
        )
        session.add(old_job)
        session.add(
            JobResource(
                job_id=old_job.id,
                resource_type="ingest_entry",
                resource_id=str(record.id),
            )
        )
        await session.commit()

    assert await ingest_mod.reconcile_disc_batch_jobs() == 1
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        rows = list((await session.execute(select(Job))).scalars())
    assert len(rows) == 2
    replacement = next(row for row in rows if row.id != "job_legacy_disc_upgrade")
    assert "ready_files" not in replacement.input_data

    await jobs.init_job_dispatcher(max_parallel=1)
    await _wait_job_status(replacement.id, JobStatus.SUCCEEDED)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        files = list((await session.execute(select(LibraryFile))).scalars())
    assert record.status == IngestStatus.IMPORTED
    assert len(files) == 1 and files[0].container == "bluray"
    assert Path(files[0].file_path).is_dir()


@pytest.mark.asyncio
async def test_ingest_watcher_reconciles_ignored_jobs_before_starting(monkeypatch):
    """应用启动监听时先修旧台账，再接收新文件事件。"""
    calls: list[str] = []

    async def reconcile() -> int:
        calls.append("reconcile-ignored")
        return 1

    async def reconcile_disc() -> int:
        calls.append("reconcile-disc")
        return 1

    class Watcher:
        async def start(self) -> None:
            calls.append("start")

        async def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr(ingest_mod, "reconcile_ignored_entry_jobs", reconcile)
    monkeypatch.setattr(ingest_mod, "reconcile_disc_batch_jobs", reconcile_disc)
    monkeypatch.setattr(ingest_mod, "IngestWatcher", Watcher)
    await ingest_mod.init_ingest_watcher()
    assert calls == ["reconcile-ignored", "reconcile-disc", "start"]
    await ingest_mod.close_ingest_watcher()
    assert calls == ["reconcile-ignored", "reconcile-disc", "start", "stop"]


async def _wait_job_status(job_id: str, status: JobStatus, timeout: float = 3.0) -> Job:
    """等待统一调度器收口状态，避免测试依赖固定 sleep。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with get_database().session() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            if row.status is status:
                return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"作业未进入 {status.value}，当前为 {row.status.value}")
        await asyncio.sleep(0.01)


def _stub_identify(monkeypatch, item):
    async def identify(session, kind, watch_root, main, spec):
        return item

    monkeypatch.setattr(ingest_mod, "_identify", identify)


def _stub_unit(monkeypatch, parse):
    """把「单文件 → (季号, 集号)」的桩接到批次解析器 resolve_units 上。

    本文件的用例关心的是入库收口行为（幂等、部分入库、挂起补偿……），不是季集
    解析本身——后者由 test_library_units.py 单独覆盖。所以这里继续按文件打桩，
    由本 helper 适配成批次签名。``explicit_pilot`` 按显式 SxxEyy 标记还原真实
    语义：集号 0 只有在文件名确实写了 E00 时才算先导占位，否则是解析缺口。
    """

    def resolve(files, **_kwargs):
        units = {}
        for file in files:
            season, episode = parse(file)
            units[file] = ingest_mod.FileUnit(
                season=season,
                episode=episode,
                explicit_pilot=episode == 0 and explicit_unit(file.stem) is not None,
            )
        return units

    monkeypatch.setattr(ingest_mod, "resolve_units", resolve)


def _fixed_rule(watch, strategy="hardlink", library_id=None) -> ImportWatch:
    """指定库规则的瞬时对象（_sweep_dir 只读 source_path/strategy/kind）。"""
    return ImportWatch(source_path=str(watch), strategy=strategy, library_id=library_id)


async def _sweep_twice(db, library_id, watch, strategy="hardlink"):
    """两轮巡检：第一轮记录指纹，第二轮确认静默后处理。"""
    for _ in range(2):
        library = await _get_library(db, library_id)
        await ingest_mod._sweep_dir(
            _fixed_rule(watch, strategy, library_id), library, execute_inline=True
        )


@pytest.mark.asyncio
async def test_marker_blocks_and_resets_quiet_window(db, tmp_path, monkeypatch):
    """有下载中标记：任凭巡检多少轮都不入库；标记消失后重新静默再导入。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    marker = entry / "movie.mkv.aria2"
    marker.write_bytes(b"ctl")

    await _sweep_twice(db, library_id, watch)
    await _sweep_twice(db, library_id, watch)
    assert not (root / "某电影 (2020)").exists()
    # 标记在场必须留痕：CIFS/NFS 等无事件挂载上标记消失不会有事件，
    # 兜底巡检全靠 _has_pending 看见它才不跳过该目录（回归）
    assert ingest_mod._has_pending(str(watch))

    marker.unlink()
    await _sweep_twice(db, library_id, watch)
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"


@pytest.mark.asyncio
async def test_unstable_fingerprint_defers_import(db, tmp_path, monkeypatch):
    """指纹还在变化（写入中）：不导入；稳定后下一轮才导入。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    video = entry / "movie.mkv"
    video.write_bytes(b"part1")

    library = await _get_library(db, library_id)
    rule = _fixed_rule(watch, library_id=library_id)
    await ingest_mod._sweep_dir(rule, library, execute_inline=True)  # 记录指纹 A
    video.write_bytes(b"part1-part2")  # 下载继续，指纹变为 B
    await ingest_mod._sweep_dir(rule, library, execute_inline=True)  # B 首见，重新起算
    assert not (root / "某电影 (2020)").exists()
    await ingest_mod._sweep_dir(rule, library, execute_inline=True)  # B 稳定 → 导入
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"part1-part2"


@pytest.mark.asyncio
async def test_movie_hardlink_import_and_ledger(db, tmp_path, monkeypatch):
    """电影硬链接入库：同 inode 零占用、台账落账、处理台账 imported、源文件不动。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    entry = watch / "Some.Movie.2020.1080p"
    entry.mkdir()
    src = entry / "some.movie.mkv"
    src.write_bytes(b"video")

    await _sweep_twice(db, library_id, watch)

    target = root / "某电影 (2020)" / "某电影 (2020).mkv"
    assert target.stat().st_ino == src.stat().st_ino  # 硬链接：同一 inode
    assert src.read_bytes() == b"video"  # 源文件原地保留（保种）
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        records = list((await session.execute(select(IngestEntry))).scalars().all())
    assert [f.file_path for f in files] == [str(target)]
    assert files[0].resolution == "1080p"
    assert files[0].added_batch_id is not None
    assert [r.status for r in records] == [IngestStatus.IMPORTED]
    assert records[0].imported_count == 1

    # 幂等：指纹未变，再巡检不重复处理
    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(files) == 1


@pytest.mark.asyncio
async def test_movie_import_avoids_disc_entry_dir(db, tmp_path, monkeypatch):
    """入库落点避让（docs/design/disc-version-layout.md §3）：条目目录本身
    是原盘时，新文件改落同级版本目录 ``标题 (年份) - 标签``，绝不钻进
    原盘内部（下游播放器丢版本、扫描误标缺失、旧盘清理殃及新文件）。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    disc_entry = root / "某电影 (2020)"  # 扁平摆放：条目目录即原盘（生态规范形态）
    (disc_entry / "BDMV" / "STREAM").mkdir(parents=True)
    (disc_entry / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"disc")

    entry = watch / "Some.Movie.2020.1080p"
    entry.mkdir()
    (entry / "some.movie.mkv").write_bytes(b"video")

    await _sweep_twice(db, library_id, watch)

    target = root / "某电影 (2020) - 1080p" / "某电影 (2020).mkv"
    assert target.exists()
    assert not (disc_entry / "某电影 (2020).mkv").exists()  # 原盘内部零新增
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert [f.file_path for f in files] == [str(target)]
    assert files[0].media_item_id == item.id  # 与原盘版本同条目归并


@pytest.mark.asyncio
async def test_movie_import_reuses_existing_version_dir(db, tmp_path, monkeypatch):
    """版本目录已存在（同标签再次入库）→ 复用目录，文件级冲突退让兜底。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    (root / "某电影 (2020)" / "BDMV").mkdir(parents=True)
    version_dir = root / "某电影 (2020) - 1080p"
    version_dir.mkdir()
    (version_dir / "某电影 (2020).mkv").write_bytes(b"earlier version")

    entry = watch / "Some.Movie.2020.Better"
    entry.mkdir()
    (entry / "better.mkv").write_bytes(b"new video")

    await _sweep_twice(db, library_id, watch)

    fallback = version_dir / "某电影 (2020) - 1080p.mkv"  # 冲突退让命名
    assert fallback.exists()
    assert (version_dir / "某电影 (2020).mkv").read_bytes() == b"earlier version"


def test_avoid_disc_entry_dir_pure(tmp_path):
    """落点避让纯函数：非原盘原样返回；原盘退同级版本目录；同名版本目录
    也是原盘时追加序号，绝不落进任何原盘内部。"""
    base = tmp_path / "某电影 (2020)"
    (base / "BDMV").mkdir(parents=True)
    assert ingest_mod._avoid_disc_entry_dir(str(base), "2160p") == str(
        tmp_path / "某电影 (2020) - 2160p"
    )
    (tmp_path / "某电影 (2020) - 2160p" / "VIDEO_TS").mkdir(parents=True)
    assert ingest_mod._avoid_disc_entry_dir(str(base), "2160p") == str(
        tmp_path / "某电影 (2020) - 2160p (2)"
    )
    plain = tmp_path / "别的电影 (2021)"
    assert ingest_mod._avoid_disc_entry_dir(str(plain), "2160p") == str(plain)


@pytest.mark.asyncio
async def test_ingest_job_reports_only_files_added_by_current_run(db, tmp_path, monkeypatch):
    """任务中心只展示本 Job 新增的文件，不混入批次里此前已入库的文件。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title="某剧", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / "Some.Show.S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")
    (entry / "ep2.mkv").write_bytes(b"episode-2")
    season = root / "某剧 (2020)" / "Season 01"
    season.mkdir(parents=True)
    (season / "某剧 (2020) - S01E01.mkv").write_bytes(b"episode-1")
    snap = ingest_mod._snapshot(entry)

    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        created = await ingest_mod.enqueue_ingest_job(
            session,
            rule,
            entry,
            snap,
            matched_hashes=[],
        )
        job_id = created.job.id

    await jobs.init_job_dispatcher(max_parallel=1)
    completed = await _wait_job_status(job_id, JobStatus.SUCCEEDED)
    assert completed.result["imported_count"] == 1
    assert completed.progress["details"]["imported_files"] == ["ep2.mkv"]
    assert "already_present_files" not in completed.progress["details"]
    assert (season / "某剧 (2020) - S01E02.mkv").read_bytes() == b"episode-2"


@pytest.mark.asyncio
async def test_copy_strategy(db, tmp_path, monkeypatch):
    """复制策略：目标是独立文件（不同 inode），内容一致。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    src = watch / "某电影 (2020).mkv"  # 裸文件条目
    src.write_bytes(b"video")

    await _sweep_twice(db, library_id, watch, strategy="copy")

    target = root / "某电影 (2020)" / "某电影 (2020).mkv"
    assert target.read_bytes() == b"video"
    assert target.stat().st_ino != src.stat().st_ino
    assert not target.with_name(target.name + ".part").exists()  # 临时文件已清


@pytest.mark.asyncio
async def test_probe_gate_blocks_partial_file(db, tmp_path, monkeypatch):
    """探测门禁：ffprobe 可用但主视频探测失败 → 记 failed，不搬运。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: None)
    monkeypatch.setattr(ingest_mod, "ffprobe_available", lambda: True)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"partial")

    await _sweep_twice(db, library_id, watch)

    assert not (root / "某电影 (2020)").exists()
    async with db.session() as session:
        records = list((await session.execute(select(IngestEntry))).scalars().all())
    assert [r.status for r in records] == [IngestStatus.FAILED]
    assert "探测失败" in (records[0].message or "")


@pytest.mark.asyncio
async def test_identify_failure_goes_pending_without_retry(db, tmp_path, monkeypatch):
    """识别不出记 pending（信息不足，等人拍板）：指纹不变**永不**定时重试
    （不打 TMDB），指纹变化（用户改名/补文件）才重新处理。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    calls = {"n": 0}

    async def identify_none(session, kind, watch_root, main, spec):
        calls["n"] += 1
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    entry = watch / "unknown-release"
    entry.mkdir()
    video = entry / "video.mkv"
    video.write_bytes(b"x")

    await _sweep_twice(db, library_id, watch)
    assert calls["n"] == 1
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.PENDING
    assert "认领" in (record.message or "")

    # 指纹不变：待处理不定时重试（等的是人，不是时间）
    await _sweep_twice(db, library_id, watch)
    assert calls["n"] == 1

    # 指纹变化（如用户改名/补文件）：重新处理
    video.write_bytes(b"xy")
    await _sweep_twice(db, library_id, watch)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_tmdb_outage_goes_failed_not_pending(db, tmp_path, monkeypatch):
    """TMDB 不可达是环境故障：记 failed（退避重试），不能钉进待处理清单。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    async def identify_outage(session, kind, watch_root, main, spec):
        raise ingest_mod.IdentifyUnavailable("TMDB 暂时不可达（模拟）")

    monkeypatch.setattr(ingest_mod, "_identify", identify_outage)

    entry = watch / "some-release"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"x")

    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.FAILED
    assert "自动重试" in (record.message or "")


@pytest.mark.asyncio
async def test_tv_import_and_incremental_episodes(db, tmp_path, monkeypatch):
    """剧集：按季集落 Season 目录；季包补集后指纹变化，增量导入新集。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    # 季集解析依赖 NER 模型（测试环境缺失），按文件名打桩：epN.mkv → S01EN
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"e1")
    (entry / "ep2.mkv").write_bytes(b"e2")

    await _sweep_twice(db, library_id, watch)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    assert (season_dir / "测试剧集 (2024) - S01E01.mkv").read_bytes() == b"e1"
    assert (season_dir / "测试剧集 (2024) - S01E02.mkv").read_bytes() == b"e2"

    # 补集：指纹变化 → 重新处理，已在库的 E01/E02 幂等跳过，只新增 E03
    (entry / "ep3.mkv").write_bytes(b"e3")
    await _sweep_twice(db, library_id, watch)
    assert (season_dir / "测试剧集 (2024) - S01E03.mkv").read_bytes() == b"e3"
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert len(files) == 3
    assert record.imported_count == 3


@pytest.mark.asyncio
async def test_downloader_signal_is_authoritative(db, tmp_path, monkeypatch):
    """名称匹配到下载器种子：未完成时任凭静默也不导入；完成则单轮立即导入。"""
    from movieclaw_downloader import TorrentBrief

    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    brief = TorrentBrief(name="Some.Movie.2020", content_name="Some.Movie.2020", completed=False)

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)

    entry = watch / "Some.Movie.2020"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")

    # 下载器说没完成：静默窗口为 0 也不能导入（暂停种子的根治场景）；
    # 条目须进入挂起表——完成瞬间 API 慢半拍时全靠它被轮询/兜底接住（回归）
    await _sweep_twice(db, library_id, watch)
    await _sweep_twice(db, library_id, watch)
    assert not (root / "某电影 (2020)").exists()
    assert str(entry) in ingest_mod._deferred

    # 下载器确认完成：无需静默等待，单轮巡检立即导入，挂起记录清除
    brief.completed = True
    library = await _get_library(db, library_id)
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"


@pytest.mark.asyncio
async def test_completed_files_are_ingested_while_same_torrent_keeps_downloading(
    db, tmp_path, monkeypatch
):
    """季包尚未整体完成时，下载器确认完成的单集先入库，剩余文件继续挂起。"""
    from movieclaw_downloader import TorrentBrief, TorrentFile, TorrentStatus

    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="分批剧集", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    entry = watch / "Partial.Show.S01"
    entry.mkdir()
    ep1 = entry / "ep1.mkv"
    ep2 = entry / "ep2.mkv"
    ep1.write_bytes(b"episode-1")
    ep2.write_bytes(b"episode-2-partial")
    brief = TorrentBrief(
        name=entry.name,
        content_name=entry.name,
        completed=False,
        info_hash="partial-hash",
    )
    status = TorrentStatus(
        info_hash=brief.info_hash,
        name=entry.name,
        progress=0.75,
        completed=False,
        save_path=str(watch),
        files=[
            TorrentFile(
                path=f"{entry.name}/{ep1.name}",
                size_bytes=ep1.stat().st_size,
                completed_bytes=ep1.stat().st_size,
            ),
            TorrentFile(
                path=f"{entry.name}/{ep2.name}",
                size_bytes=ep2.stat().st_size,
                completed_bytes=3,
            ),
        ],
    )

    async def briefs():
        return [brief]

    async def statuses(_matches):
        return [(SimpleNamespace(path_mappings=None), status)]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "_matched_torrent_statuses", statuses)
    library = await _get_library(db, library_id)
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )

    season = root / "分批剧集 (2026)" / "Season 01"
    assert (season / "分批剧集 (2026) - S01E01.mkv").read_bytes() == b"episode-1"
    assert not (season / "分批剧集 (2026) - S01E02.mkv").exists()
    assert str(entry) in ingest_mod._deferred
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.imported_count == 1

    # 同一个种子仍未整体完成，但下一集的完成字节翻转后，自检应识别出新批次。
    status.files[1].completed_bytes = status.files[1].size_bytes
    assert await ingest_mod._deferred_flipped(str(watch) + "/") is True
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )
    assert (season / "分批剧集 (2026) - S01E02.mkv").read_bytes() == (b"episode-2-partial")
    assert str(entry) in ingest_mod._deferred
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.imported_count == 2


@pytest.mark.asyncio
async def test_completed_file_waits_when_another_torrent_still_writes_same_path(
    db, tmp_path, monkeypatch
):
    """同名种子的文件路径重叠时，任一写入者未完成都不能提前入库。"""
    from movieclaw_downloader import TorrentBrief, TorrentFile, TorrentStatus

    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="重叠剧集", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, 1))

    entry = watch / "Overlap.Show.S01"
    entry.mkdir()
    source = entry / "ep1.mkv"
    source.write_bytes(b"episode-1")
    completed = TorrentBrief(
        name=entry.name,
        content_name=entry.name,
        completed=True,
        info_hash="completed-hash",
    )
    downloading = TorrentBrief(
        name=entry.name,
        content_name=entry.name,
        completed=False,
        info_hash="downloading-hash",
    )
    complete_status = TorrentStatus(
        info_hash=completed.info_hash,
        name=entry.name,
        progress=1.0,
        completed=True,
        save_path=str(watch),
        files=[
            TorrentFile(
                path=f"{entry.name}/{source.name}",
                size_bytes=source.stat().st_size,
                completed_bytes=source.stat().st_size,
            )
        ],
    )
    writing_status = TorrentStatus(
        info_hash=downloading.info_hash,
        name=entry.name,
        progress=0.5,
        completed=False,
        save_path=str(watch),
        files=[
            TorrentFile(
                path=f"{entry.name}/{source.name}",
                size_bytes=source.stat().st_size,
                completed_bytes=3,
            )
        ],
    )

    async def briefs():
        return [completed, downloading]

    async def statuses(_matches):
        return [
            (SimpleNamespace(path_mappings=None), complete_status),
            (SimpleNamespace(path_mappings=None), writing_status),
        ]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "_matched_torrent_statuses", statuses)
    library = await _get_library(db, library_id)
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )

    assert not (root / "重叠剧集 (2026)").exists()
    assert str(entry) in ingest_mod._deferred


@pytest.mark.asyncio
async def test_completed_torrent_creates_file_scoped_job_while_sibling_downloads(
    db, tmp_path, monkeypatch
):
    """《重器》同类场景：已完成种子的独立文件先进入持久化入库作业。"""
    from movieclaw_downloader import TorrentBrief, TorrentFile, TorrentStatus

    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title="同目录剧集", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)
    entry = watch / "Shared.Show.S01"
    entry.mkdir()
    ep1 = entry / "ep1.mkv"
    ep2 = entry / "ep2.mkv"
    ep1.write_bytes(b"episode-1")
    ep2.write_bytes(b"episode-2-partial")
    completed = TorrentBrief(
        name=entry.name,
        content_name=entry.name,
        completed=True,
        info_hash="completed-hash",
    )
    downloading = TorrentBrief(
        name=entry.name,
        content_name=entry.name,
        completed=False,
        info_hash="downloading-hash",
    )
    complete_status = TorrentStatus(
        info_hash=completed.info_hash,
        name=entry.name,
        progress=1.0,
        completed=True,
        save_path=str(watch),
        files=[
            TorrentFile(
                path=f"{entry.name}/{ep1.name}",
                size_bytes=ep1.stat().st_size,
                completed_bytes=ep1.stat().st_size,
            )
        ],
    )
    writing_status = TorrentStatus(
        info_hash=downloading.info_hash,
        name=entry.name,
        progress=0.5,
        completed=False,
        save_path=str(watch),
        files=[
            TorrentFile(
                path=f"{entry.name}/{ep2.name}",
                size_bytes=ep2.stat().st_size,
                completed_bytes=3,
            )
        ],
    )

    async def briefs():
        return [completed, downloading]

    async def statuses(_matches):
        return [
            (SimpleNamespace(path_mappings=None), complete_status),
            (SimpleNamespace(path_mappings=None), writing_status),
        ]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "_matched_torrent_statuses", statuses)
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)

    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
        resources = await jobs.resources_for_jobs(session, [job.id])
    assert job.input_data["ready_files"] == [
        {
            "path": "ep1.mkv",
            "size_bytes": ep1.stat().st_size,
            "info_hashes": ["completed-hash"],
        }
    ]
    assert job.input_data["consume_info_hashes"] == ["completed-hash"]
    assert job.input_data["keep_deferred"] is True
    assert {row.resource_id for row in resources[job.id] if row.resource_type == "download"} == {
        "completed-hash"
    }

    await jobs.init_job_dispatcher(max_parallel=1)
    await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    season = root / "同目录剧集 (2026)" / "Season 01"
    assert (season / "同目录剧集 (2026) - S01E01.mkv").read_bytes() == b"episode-1"
    assert not (season / "同目录剧集 (2026) - S01E02.mkv").exists()
    assert str(entry) in ingest_mod._deferred


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative_path",
    [
        "BDMV/STREAM/00001.m2ts",
        "电影一 (2001)/BDMV/STREAM/00001.m2ts",
    ],
    ids=["single-disc", "collection-with-nested-disc"],
)
async def test_downloading_disc_never_imports_completed_stream_segments(
    db, tmp_path, monkeypatch, relative_path
):
    """原盘下载中已完成的 m2ts 不是正片；单盘和合集嵌套盘都必须整种等待。"""
    from movieclaw_downloader import TorrentBrief, TorrentFile, TorrentStatus

    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="原盘电影", year=2001)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    entry = watch / "Movie.Collection.2001-2023.UHD.BluRay"
    stream = entry.joinpath(*relative_path.split("/"))
    stream.parent.mkdir(parents=True)
    stream.write_bytes(b"ten-second-bluray-segment")
    download_complete = {"value": False}

    async def briefs():
        return [
            TorrentBrief(
                name=entry.name,
                content_name=entry.name,
                completed=download_complete["value"],
                info_hash="disc-hash",
            )
        ]

    async def statuses(_matches):
        return [
            (
                SimpleNamespace(path_mappings=None),
                TorrentStatus(
                    info_hash="disc-hash",
                    name=entry.name,
                    progress=0.5,
                    completed=False,
                    save_path=str(watch),
                    files=[
                        TorrentFile(
                            path=f"{entry.name}/{relative_path}",
                            size_bytes=stream.stat().st_size,
                            completed_bytes=stream.stat().st_size,
                        )
                    ],
                ),
            )
        ]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "_matched_torrent_statuses", statuses)
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None

    # 下载中：即便流分段已完成，也不能产生作业、台账或假入库文件。
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        assert list((await session.execute(select(Job))).scalars()) == []
        assert list((await session.execute(select(IngestEntry))).scalars()) == []
        assert list((await session.execute(select(LibraryFile))).scalars()) == []
    assert str(entry) in ingest_mod._deferred

    # 整种完成：完整原盘树一次性硬链接入库，仍不会产生任何分片条目。
    download_complete["value"] = True
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        files = list((await session.execute(select(LibraryFile))).scalars())
    assert record.status == IngestStatus.IMPORTED
    assert "完整原盘" in (record.message or "")
    assert len(files) == 1
    final = Path(files[0].file_path)
    assert files[0].container == "bluray"
    assert (final / "BDMV" / "STREAM" / "00001.m2ts").stat().st_ino == stream.stat().st_ino


@pytest.mark.asyncio
async def test_downloader_outage_fails_closed_without_snapshot_ingest(db, tmp_path, monkeypatch):
    """配置下载器全部不可达时不再退回静默时间猜测，恢复前零入库。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="未完成电影", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    entry = watch / "Incomplete.Movie.2026"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"preallocated-partial")

    async def unavailable():
        return None

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", unavailable)
    await _sweep_twice(db, library_id, watch)
    assert not (root / "未完成电影 (2026)").exists()
    assert str(entry) in ingest_mod._deferred
    async with db.session() as session:
        assert list((await session.execute(select(IngestEntry))).scalars()) == []


@pytest.mark.asyncio
async def test_queued_ingest_job_rechecks_downloader_outage_before_snapshot(
    db, tmp_path, monkeypatch
):
    """巡检后、Job 执行前下载器掉线时也必须 fail closed，不能从队列绕过门禁。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    async with db.session() as session:
        rule_id = (await session.execute(select(ImportWatch.id))).scalar_one()
    entry = watch / "Queued.Incomplete.Movie.2026"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"preallocated-partial")

    async def unavailable():
        return None

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", unavailable)

    class Context:
        async def current_progress(self):
            return {}

        async def update_progress(self, **_kwargs):
            return None

    with pytest.raises(jobs.JobRetry, match="下载器当前不可达"):
        await ingest_mod._execute_ingest_job(
            Context(),
            {
                "rule_id": rule_id,
                "entry_path": str(entry),
                "detected_fingerprint": "queued-before-outage",
            },
        )
    assert not (root / "Queued Incomplete Movie (2026)").exists()
    async with db.session() as session:
        assert list((await session.execute(select(IngestEntry))).scalars()) == []


@pytest.mark.asyncio
async def test_legacy_queued_torrent_job_waits_when_reachable_api_returns_empty(
    db, tmp_path, monkeypatch
):
    """升级前 Job 只有 infohash 也足以证明受管，空列表不能把它当普通文件放行。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    async with db.session() as session:
        rule_id = (await session.execute(select(ImportWatch.id))).scalar_one()
    entry = watch / "Legacy.Managed.Movie.2026"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"preallocated-partial")

    async def empty():
        return []

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", empty)

    class Context:
        async def current_progress(self):
            return {}

        async def update_progress(self, **_kwargs):
            return None

    with pytest.raises(jobs.JobRetry, match="暂未返回该受管任务"):
        await ingest_mod._execute_ingest_job(
            Context(),
            {
                "rule_id": rule_id,
                "entry_path": str(entry),
                "info_hashes": ["a" * 40],
                "detected_fingerprint": "queued-before-upgrade",
            },
        )
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_enabled_unverified_downloader_is_unavailable_not_unconfigured(db):
    """启用中的 pending/failed 配置仍是权威状态源，验证窗口不能启发式放行。"""
    async with db.session() as session:
        row = DownloaderClient(
            name="等待验证的下载器",
            client_type=ClientType.QBITTORRENT,
            url="http://127.0.0.1:65535",
            enabled=True,
            status=ConfigStatus.PENDING,
        )
        session.add(row)
        await session.commit()

    ingest_mod._briefs_cache = (float("-inf"), None)
    assert await ingest_mod._downloader_briefs() is None

    async with db.session() as session:
        row = (await session.execute(select(DownloaderClient))).scalar_one()
        row.enabled = False
        await session.commit()
    ingest_mod._briefs_cache = (float("-inf"), None)
    assert await ingest_mod._downloader_briefs() == []


@pytest.mark.asyncio
async def test_one_unreachable_downloader_makes_combined_brief_incomplete(
    db, monkeypatch
):
    """多下载器只成功一台时不能把部分列表当完整事实，未命中条目继续等待。"""
    async with db.session() as session:
        session.add_all(
            [
                DownloaderClient(
                    name="可达下载器",
                    client_type=ClientType.QBITTORRENT,
                    url="http://reachable.invalid",
                    enabled=True,
                    status=ConfigStatus.ACTIVE,
                ),
                DownloaderClient(
                    name="失联下载器",
                    client_type=ClientType.QBITTORRENT,
                    url="http://offline.invalid",
                    enabled=True,
                    status=ConfigStatus.ACTIVE,
                ),
            ]
        )
        await session.commit()

    class Adapter:
        def __init__(self, url: str):
            self.url = url

        async def list_torrents(self):
            if "offline" in self.url:
                raise OSError("offline")
            return []

        async def close(self):
            return None

    monkeypatch.setattr(
        "movieclaw_downloader.create_downloader",
        lambda config: Adapter(config.url),
    )
    ingest_mod._briefs_cache = (float("-inf"), None)
    assert await ingest_mod._downloader_briefs() is None


@pytest.mark.asyncio
async def test_persisted_download_name_blocks_transient_empty_torrent_list(
    db, tmp_path, monkeypatch
):
    """API 可达却暂时返回空列表时，真实投递名锚仍阻止受管任务被误放行。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="受管电影", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    entry = watch / "Managed.Movie.2026"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"preallocated-partial")
    async with db.session() as session:
        session.add(
            ManualDownloadIntent(
                info_hash="a" * 40,
                media_item_id=item.id,
                library_id=library_id,
                download_name=entry.name,
                save_path=str(watch),
            )
        )
        await session.commit()

    async def empty():
        return []

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", empty)
    await _sweep_twice(db, library_id, watch)
    assert not (root / "受管电影 (2026)").exists()
    assert str(entry) in ingest_mod._deferred


@pytest.mark.asyncio
async def test_stale_manual_download_name_no_longer_blocks_ingest(db, tmp_path):
    """任务被删除且 90 天未入库的孤儿锚不能让同名目录永久等待。"""
    root = tmp_path / "movies"
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="旧任务", year=2026)
    entry = tmp_path / "watch" / "Stale.Managed.Movie.2026"
    async with db.session() as session:
        intent = ManualDownloadIntent(
            info_hash="f" * 40,
            media_item_id=item.id,
            library_id=library_id,
            download_name=entry.name,
            save_path=str(entry.parent),
        )
        intent.created_at = utcnow() - MANUAL_DOWNLOAD_INTENT_TTL - timedelta(days=1)
        session.add(intent)
        await session.commit()
        assert await ingest_mod._has_managed_download_claim(session, entry) is False


def test_legacy_site_title_matches_real_downloader_name_without_crossing_movie_year():
    """旧台账没有真实下载名时，站点标题允许发布属性差异，但不同年份不串。"""
    actual = "Mission.Impossible.1996.2160p.BluRay.DoVi.x265.10bit.TrueHD5.1-WiKi"
    site = "Mission: Impossible 1996 2160p BluRay DoVi x265 10bit 3Audios TrueHD 5.1-WiKi"
    assert ingest_mod._download_name_matches(actual, site)
    assert not ingest_mod._download_name_matches(actual, site.replace("1996", "2018"))


def test_disc_tree_copy_is_atomic_and_idempotent(tmp_path):
    """跨盘策略保留完整目录树；再次执行识别为同内容，不复制第二份。"""
    source = tmp_path / "source" / "Movie"
    stream = source / "BDMV" / "STREAM" / "00001.m2ts"
    playlist = source / "BDMV" / "PLAYLIST" / "00001.mpls"
    stream.parent.mkdir(parents=True)
    playlist.parent.mkdir(parents=True)
    (source / "BDMV" / "AUXDATA").mkdir()
    (source / "BDMV" / "AUXDATA" / "empty.bin").touch()
    stream.write_bytes(b"main-feature")
    playlist.write_bytes(b"playlist")
    base = tmp_path / "library" / "电影 (2026)"

    final, created = ingest_mod._transfer_disc_tree(source, base, "copy", "2160p")
    assert created is True and final == base
    assert (final / "BDMV" / "STREAM" / "00001.m2ts").read_bytes() == b"main-feature"
    assert (final / "BDMV" / "PLAYLIST" / "00001.mpls").read_bytes() == b"playlist"
    assert (final / "BDMV" / "STREAM" / "00001.m2ts").stat().st_ino != stream.stat().st_ino
    again, created_again = ingest_mod._transfer_disc_tree(source, base, "copy", "2160p")
    assert again == final and created_again is False


def test_disc_tree_same_sizes_with_different_mtime_is_not_deduplicated(tmp_path):
    """路径和尺寸碰巧相同的另一张盘不能被静默当成已入库内容。"""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_stream = first / "BDMV" / "STREAM" / "00001.m2ts"
    second_stream = second / "BDMV" / "STREAM" / "00001.m2ts"
    first_stream.parent.mkdir(parents=True)
    second_stream.parent.mkdir(parents=True)
    first_stream.write_bytes(b"first-disc")
    second_stream.write_bytes(b"other-disc")
    os.utime(first_stream, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second_stream, ns=(2_000_000_000, 2_000_000_000))
    base = tmp_path / "library" / "电影 (2026)"

    final, created = ingest_mod._transfer_disc_tree(first, base, "copy", "2160p")
    assert created is True and final == base
    alternate, already_present = ingest_mod._disc_destination(second, base, "2160p")
    assert already_present is False
    assert alternate != base


@pytest.mark.asyncio
async def test_disc_tree_job_copy_resumes_and_publishes_atomically(tmp_path, monkeypatch):
    """原盘复制被服务更新打断后保留确认字节，重跑续传并只发布完整目录。"""
    source = tmp_path / "source" / "Movie"
    stream = source / "BDMV" / "STREAM" / "00001.m2ts"
    playlist = source / "BDMV" / "PLAYLIST" / "00001.mpls"
    stream.parent.mkdir(parents=True)
    playlist.parent.mkdir(parents=True)
    (source / "BDMV" / "AUXDATA").mkdir()
    (source / "BDMV" / "AUXDATA" / "empty.bin").touch()
    stream.write_bytes(bytes(range(32)))
    playlist.write_bytes(b"playlist")
    base = tmp_path / "library" / "电影 (2026)"
    monkeypatch.setattr(ingest_mod, "_INGEST_COPY_CHUNK_BYTES", 8)

    class Context:
        async def raise_if_cancelled(self):
            return None

    interrupted = False

    async def interrupt_after_first_chunk(copied: int, total: int):
        nonlocal interrupted
        if copied and copied < total and not interrupted:
            interrupted = True
            raise jobs.JobCancelled

    with pytest.raises(jobs.JobCancelled):
        await ingest_mod._transfer_disc_tree_for_job(
            source,
            base,
            "copy",
            "2160p",
            Context(),
            interrupt_after_first_chunk,
        )
    stage, state_path = ingest_mod._disc_ingest_paths(base)
    assert stage.is_dir() and state_path.is_file()
    assert not base.exists()
    assert any(path.stat().st_size == 8 for path in stage.rglob("*.part"))

    offsets: list[int] = []

    async def record_progress(copied: int, _total: int):
        offsets.append(copied)

    final, created = await ingest_mod._transfer_disc_tree_for_job(
        source,
        base,
        "copy",
        "2160p",
        Context(),
        record_progress,
    )
    assert created is True and final == base
    assert offsets[:2] == [0, 8], "重跑从零复制，没有复用上次确认的字节"
    assert stream.read_bytes() == (final / "BDMV" / "STREAM" / "00001.m2ts").read_bytes()
    assert playlist.read_bytes() == (final / "BDMV" / "PLAYLIST" / "00001.mpls").read_bytes()
    assert (final / "BDMV" / "AUXDATA").is_dir()
    assert (final / "BDMV" / "AUXDATA" / "empty.bin").stat().st_size == 0
    assert not stage.exists() and not state_path.exists()


@pytest.mark.asyncio
async def test_multi_disc_collection_stays_pending_without_partial_import(
    db, tmp_path, monkeypatch
):
    """合集含多个 BDMV 时不能猜电影边界，完整保留源并只生成一条待处理结论。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    entry = watch / "Movie.Collection"
    for number in (1, 2):
        stream = entry / f"Movie {number}" / "BDMV" / "STREAM" / "00001.m2ts"
        stream.parent.mkdir(parents=True)
        stream.write_bytes(f"disc-{number}".encode())

    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        files = list((await session.execute(select(LibraryFile))).scalars())
    assert record.status == IngestStatus.PENDING
    assert "包含 2 个原盘" in (record.message or "")
    assert files == []
    assert all(path.exists() for path in entry.rglob("*.m2ts"))


@pytest.mark.asyncio
async def test_direct_bdmv_entry_never_treats_whole_watch_root_as_disc(db, tmp_path):
    """BDMV 直接位于监听根时只能待人工，绝不能把兄弟下载一起搬进媒体库。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    stream = watch / "BDMV" / "STREAM" / "00001.m2ts"
    stream.parent.mkdir(parents=True)
    stream.write_bytes(b"disc")
    sibling = watch / "Other.Download" / "keep.txt"
    sibling.parent.mkdir()
    sibling.write_text("must-stay", encoding="utf-8")

    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        records = list((await session.execute(select(IngestEntry))).scalars())
        files = list((await session.execute(select(LibraryFile))).scalars())
    bdmv = next(record for record in records if Path(record.entry_path).name == "BDMV")
    assert bdmv.status == IngestStatus.PENDING
    assert "不能直接作为监听目录的顶层条目" in (bdmv.message or "")
    assert sibling.read_text(encoding="utf-8") == "must-stay"
    assert files == []


@pytest.mark.asyncio
async def test_file_scoped_blocked_job_not_woken_by_tree_fingerprint(db, tmp_path, monkeypatch):
    """文件级批次 blocked 后不被全树指纹差异反复唤醒；升级补偿仍只唤醒一次。

    批次记录的 ready: 指纹与全树指纹永不相等，内容变化唤醒若不排除文件级
    作业，会让 blocked 批次每轮巡检被拉起重跑再挂起（无限唤醒，回归）。
    """
    from movieclaw_downloader import TorrentBrief, TorrentFile, TorrentStatus

    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title="批次挂起剧集", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    # 已完成的 ep1 解析不出集号 → 批次作业以解析缺口挂起
    _stub_unit(monkeypatch, lambda file: (1, 0))

    entry = watch / "Blocked.Batch.S01"
    entry.mkdir()
    ep1 = entry / "ep1.mkv"
    ep2 = entry / "ep2.mkv"
    ep1.write_bytes(b"episode-1")
    ep2.write_bytes(b"episode-2-partial")
    completed = TorrentBrief(
        name=entry.name, content_name=entry.name, completed=True, info_hash="completed-hash"
    )
    downloading = TorrentBrief(
        name=entry.name, content_name=entry.name, completed=False, info_hash="downloading-hash"
    )
    complete_status = TorrentStatus(
        info_hash=completed.info_hash,
        name=entry.name,
        progress=1.0,
        completed=True,
        save_path=str(watch),
        files=[
            TorrentFile(
                path=f"{entry.name}/{ep1.name}",
                size_bytes=ep1.stat().st_size,
                completed_bytes=ep1.stat().st_size,
            )
        ],
    )
    writing_status = TorrentStatus(
        info_hash=downloading.info_hash,
        name=entry.name,
        progress=0.5,
        completed=False,
        save_path=str(watch),
        files=[
            TorrentFile(
                path=f"{entry.name}/{ep2.name}",
                size_bytes=ep2.stat().st_size,
                completed_bytes=3,
            )
        ],
    )

    async def briefs():
        return [completed, downloading]

    async def statuses(_matches):
        return [
            (SimpleNamespace(path_mappings=None), complete_status),
            (SimpleNamespace(path_mappings=None), writing_status),
        ]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "_matched_torrent_statuses", statuses)
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    assert job.input_data.get("ready_files")
    await jobs.init_job_dispatcher(max_parallel=1)
    blocked = await _wait_job_status(job.id, JobStatus.BLOCKED)
    assert blocked.error["code"] == "INGEST_EPISODE_PARSE_REQUIRED"

    # 同修订号再巡检：不因 ready: 指纹与全树指纹不等而唤醒
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        unchanged = await session.get(Job, job.id)
        assert unchanged is not None
        assert unchanged.status == JobStatus.BLOCKED

    # 升级补偿对文件级作业照常生效：唤醒一次、按原白名单重跑后再次挂起
    async with db.session() as session:
        stored = await session.get(Job, job.id)
        assert stored is not None
        stored.handler_revision = "library.ingest.v1"
        await session.commit()
    await ingest_mod._sweep_dir(rule, library)
    reblocked = await _wait_job_status(job.id, JobStatus.BLOCKED)
    assert reblocked.handler_revision == ingest_mod._ingest_handler_revision()
    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
    assert len(all_jobs) == 1


@pytest.mark.asyncio
async def test_blocked_batch_does_not_stall_remaining_episodes(db, tmp_path, monkeypatch):
    """某一批挂起后，同条目剩下的集必须能继续入库，不被连坐。

    blocked 属于活跃状态，巡检见到就跳过整个条目；各批又曾共用一个 dedupe_key，
    于是 E01 识别失败挂起会把 E02–E10 全部永久堵死，而任务中心只显示那一个
    受阻任务，用户看不出后面还有几集被连累（回归）。
    """
    from movieclaw_downloader import TorrentBrief, TorrentFile, TorrentStatus

    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title="连坐测试剧", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)
    # ep1 解析不出集号 → 第一批挂起；ep2 正常
    _stub_unit(
        monkeypatch,
        lambda file: (1, 0) if file.stem == "ep1" else (1, int(file.stem.removeprefix("ep"))),
    )

    entry = watch / "Collateral.Show.S01"
    entry.mkdir()
    ep1 = entry / "ep1.mkv"
    ep2 = entry / "ep2.mkv"
    ep3 = entry / "ep3.mkv"
    ep1.write_bytes(b"episode-1")
    ep2.write_bytes(b"episode-2-partial")
    ep3.write_bytes(b"episode-3-partial")
    completed = TorrentBrief(
        name=entry.name, content_name=entry.name, completed=True, info_hash="done-hash"
    )
    downloading = TorrentBrief(
        name=entry.name, content_name=entry.name, completed=False, info_hash="writing-hash"
    )

    def _status(info_hash: str, name: Path, done: bool) -> TorrentStatus:
        size = name.stat().st_size
        return TorrentStatus(
            info_hash=info_hash,
            name=entry.name,
            progress=1.0 if done else 0.5,
            completed=done,
            save_path=str(watch),
            files=[
                TorrentFile(
                    path=f"{entry.name}/{name.name}",
                    size_bytes=size,
                    completed_bytes=size if done else 3,
                )
            ],
        )

    # 可变开关：后续轮次依次把 ep2、ep3 切成已完成
    completed_later = {ep2.name: False, ep3.name: False}

    def _writing_status() -> TorrentStatus:
        files = []
        for file in (ep2, ep3):
            size = file.stat().st_size
            done = completed_later[file.name]
            files.append(
                TorrentFile(
                    path=f"{entry.name}/{file.name}",
                    size_bytes=size,
                    completed_bytes=size if done else 3,
                )
            )
        return TorrentStatus(
            info_hash=downloading.info_hash,
            name=entry.name,
            progress=0.75,
            completed=False,
            save_path=str(watch),
            files=files,
        )

    async def briefs():
        return [completed, downloading]

    async def statuses(_matches):
        return [
            (SimpleNamespace(path_mappings=None), _status(completed.info_hash, ep1, True)),
            (SimpleNamespace(path_mappings=None), _writing_status()),
        ]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "_matched_torrent_statuses", statuses)
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None

    # 第一轮：只有 ep1 完成，批次作业因解析缺口挂起
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        first = (await session.execute(select(Job))).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    blocked = await _wait_job_status(first.id, JobStatus.BLOCKED)
    assert blocked.error["code"] == "INGEST_EPISODE_PARSE_REQUIRED"

    # 第二轮：ep2 落盘，必须另立作业，而不是并回挂起的那一批
    completed_later[ep2.name] = True
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
    assert len(all_jobs) == 2, "新一批被挂起作业连坐，没能另立作业"
    follow_up = next(job for job in all_jobs if job.id != first.id)
    await _wait_job_status(follow_up.id, JobStatus.SUCCEEDED)

    # ep2 已进库；挂起的 ep1 留在原地等人工，没有被静默带走
    season = root / "连坐测试剧 (2026)" / "Season 01"
    assert (season / "连坐测试剧 (2026) - S01E02.mkv").read_bytes() == b"episode-2-partial"
    async with db.session() as session:
        still_blocked = await session.get(Job, first.id)
        assert still_blocked is not None
        assert still_blocked.status == JobStatus.BLOCKED

    # 第三轮：ep2 的成功作业已成为“最新作业”，仍不能因此忘掉更早 blocked
    # 认领的 ep1。新作业白名单只能包含真正新增的 ep3，否则会重现 NAS 上
    # 1300 个旧 m2ts 被反复带入下一批、任务中心持续膨胀的坏例。
    completed_later[ep3.name] = True
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
    assert len(all_jobs) == 3
    third = max(all_jobs, key=lambda job: job.created_at)
    assert [ready["path"] for ready in third.input_data["ready_files"]] == [ep3.name]
    await _wait_job_status(third.id, JobStatus.SUCCEEDED)
    assert (season / "连坐测试剧 (2026) - S01E03.mkv").read_bytes() == b"episode-3-partial"


@pytest.mark.asyncio
async def test_file_scoped_job_never_falls_back_to_whole_directory():
    """白名单损坏时停止作业，不能静默扩大为整目录入库。"""
    with pytest.raises(jobs.JobFailed, match="文件级入库白名单无效"):
        await ingest_mod._execute_ingest_job(
            SimpleNamespace(),
            {
                "rule_id": 1,
                "entry_path": "/downloads/Partial.Show",
                "info_hashes": ["partial-hash"],
                "ready_files": [],
            },
        )


@pytest.mark.asyncio
async def test_manual_download_identity_claim_via_info_hash(db, tmp_path, monkeypatch):
    """手动下载投到共享监听目录后，按提交时锚定的身份和库入库。"""
    from movieclaw_downloader import TorrentBrief

    default_root, target_root, watch = tmp_path / "default", tmp_path / "target", tmp_path / "watch"
    watch.mkdir()
    default_library_id = await _make_library(db, kind=MediaKind.MOVIE, root=default_root)
    target_root.mkdir()
    async with db.session() as session:
        target = await LibraryRepository(session).create(
            name="手动下载目标库",
            kind=MediaKind.MOVIE.value,
            root_paths=[str(target_root)],
        )
        assert target.id is not None
        target_library_id = target.id
    item = await _make_item(db, kind=MediaKind.MOVIE, title="手动确认影片", year=2024)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    # 名称识别链必然失败，只有手动提交时保存的 hash 身份锚能让导入成功。
    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)
    async with db.session() as session:
        assert item.id is not None
        session.add(
            ManualDownloadIntent(
                info_hash="manualhash",
                media_item_id=item.id,
                library_id=target_library_id,
                site_id="mteam",
            )
        )
        await session.commit()

    brief = TorrentBrief(
        name="Cryptic.Manual.Release",
        content_name="Cryptic.Manual.Release",
        completed=True,
        info_hash="manualhash",
    )

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    entry = watch / "Cryptic.Manual.Release"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"video")

    # auto 监听规则没有指定库；若没有手动身份锚，会走到 identify_none → pending。
    rule = ImportWatch(source_path=str(watch), strategy="hardlink", library_id=None, kind="movie")
    await ingest_mod._sweep_dir(rule, None, execute_inline=True)

    assert (target_root / "手动确认影片 (2024)" / "手动确认影片 (2024).mkv").exists()
    assert not (default_root / "手动确认影片 (2024)").exists()
    assert default_library_id != target_library_id
    assert str(entry) not in ingest_mod._deferred
    async with db.session() as session:
        assert (await session.execute(select(ManualDownloadIntent))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_probe_gate_applies_per_file(db, tmp_path, monkeypatch):
    """季包部分探测失败：完整集照常入库，但条目保留 failed 供自动重试。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "ffprobe_available", lambda: True)
    # ep2 残缺：探测失败；其余正常
    monkeypatch.setattr(
        ingest_mod, "probe_media", lambda p: None if "ep2" in str(p) else _FAKE_SPEC
    )
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"full-episode")  # 最大文件 = 主文件，探测通过
    (entry / "ep2.mkv").write_bytes(b"partial")

    await _sweep_twice(db, library_id, watch)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    assert (season_dir / "测试剧集 (2024) - S01E01.mkv").exists()
    assert not (season_dir / "测试剧集 (2024) - S01E02.mkv").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.FAILED
    assert record.imported_count == 1
    assert "探测失败" in (record.message or "")


@pytest.mark.asyncio
async def test_partial_episode_parse_stays_pending(db, tmp_path, monkeypatch):
    """季包部分解析失败不能冒充完整入库；已成功的文件仍保留且累计。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, 1) if file.stem == "ep1" else (1, 0))

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")
    (entry / "ep2.mkv").write_bytes(b"episode-2")

    await _sweep_twice(db, library_id, watch)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    assert (season_dir / "测试剧集 (2024) - S01E01.mkv").exists()
    assert not (season_dir / "测试剧集 (2024) - S01E02.mkv").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.PENDING
    assert record.imported_count == 1
    assert "硬链接 1 个文件" in (record.message or "")
    assert "ep2.mkv」解析不出集号，未入库" in (record.message or "")


@pytest.mark.asyncio
async def test_name_conflict_concludes_pending_not_failed(db, tmp_path, monkeypatch):
    """目标同名冲突（基础名与多版本退让名都被占）是确定性冲突：结论必须是
    pending 等人处理，不能归为环境故障进无限退避重试（NAS 实测洗版季包里
    一集与库存冲突，任务中心永远「等待重试」刷屏）。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, 1))

    # 基础名与 1080p 退让名都已被不同内容占用（尺寸互不相同 → 非同一载荷）
    season_dir = root / "测试剧集 (2024)" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "测试剧集 (2024) - S01E01.mkv").write_bytes(b"old")
    (season_dir / "测试剧集 (2024) - S01E01 - 1080p.mkv").write_bytes(b"other-1080p")

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"new-episode-1-longer")

    await _sweep_twice(db, library_id, watch)

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.PENDING
    assert "目标已存在同名文件，跳过以免覆盖：测试剧集 (2024) - S01E01 - 1080p.mkv" in (
        record.message or ""
    )


@pytest.mark.asyncio
async def test_name_conflict_with_same_tier_version_skips_as_imported(db, tmp_path, monkeypatch):
    """同名冲突但库里已有同档版本：重复内容跳过即是完成，任务正常闭环
    （NAS 实测：换源后两个 1080p 季包先后投递，第二包附带的 E05 与第一包
    收编的版本同档不同尺寸，不能变成待处理/重试）。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, 1))

    # 两个名字都被不同内容占用，且 1080p 版本已入台账（同档在库）
    season_dir = root / "测试剧集 (2024)" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "测试剧集 (2024) - S01E01.mkv").write_bytes(b"old")
    versioned = season_dir / "测试剧集 (2024) - S01E01 - 1080p.mkv"
    versioned.write_bytes(b"other-1080p")
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                file_path=str(versioned),
                size_bytes=versioned.stat().st_size,
                resolution="1080p",
                media_source="WEB-DL",
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"new-episode-1-longer")

    await _sweep_twice(db, library_id, watch)

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert "已有同档或更高版本" in (record.message or "")
    # 来件没有落任何新文件
    assert not (season_dir / "测试剧集 (2024) - S01E01 - 1080p (2).mkv").exists()


@pytest.mark.asyncio
async def test_subscription_extra_same_tier_file_not_imported_as_new_version(
    db, tmp_path, monkeypatch
):
    """订阅投递的季包附带集与库存同档但扩展名不同：不得绕过同名幂等落成
    多余版本（NAS 实测：洗 E04/E06 的 CHDWEB 包把 E01 的 .mp4 又入了一份，
    与已有 .mkv 并存成垃圾副本）。目标集照常入库，且文件行带来源戳——
    洗版验证靠它精确匹配「文件 ↔ 投递」，不再退化到时间兜底误配。"""
    from movieclaw_db.models import (
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        WantedItem,
        WantedStatus,
    )
    from movieclaw_downloader import TorrentBrief

    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    # 库里已有 E01 的同档版本（.mkv）；E02 是本次投递的目标缺口
    season_dir = root / "测试剧集 (2024)" / "Season 01"
    season_dir.mkdir(parents=True)
    existing = season_dir / "测试剧集 (2024) - S01E01.mkv"
    existing.write_bytes(b"existing-e01")
    async with db.session() as session:
        rule_set = RuleSet(name="默认", spec={})
        session.add(rule_set)
        await session.commit()
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="tv", rule_set_id=rule_set.id, library_id=library_id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add_all(
            [
                WantedItem(
                    subscription_id=sub.id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=2,
                    status=WantedStatus.GRABBED,
                    info_hash="hash-s1",
                ),
                SubscriptionDownloadAttempt(
                    subscription_id=sub.id,
                    info_hash="hash-s1",
                    site_id="mteam",
                    torrent_id="12345",
                    units=[[1, 2]],
                    last_progress_at=utcnow(),
                ),
                LibraryFile(
                    library_id=library_id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=1,
                    file_path=str(existing),
                    size_bytes=existing.stat().st_size,
                    resolution="1080p",
                    media_source="WEB-DL",
                    source=FileSource.IMPORTED,
                ),
            ]
        )
        await session.commit()

    brief = TorrentBrief(
        name="测试剧集.S01.Pack",
        content_name="测试剧集.S01.Pack",
        completed=True,
        info_hash="hash-s1",
    )

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)

    entry = watch / "测试剧集.S01.Pack"
    entry.mkdir()
    (entry / "ep1.mp4").write_bytes(b"same-tier-duplicate")
    (entry / "ep2.mp4").write_bytes(b"target-episode-2")

    library = await _get_library(db, library_id)
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )

    # 目标集入库；附带的同档 E01 没有落成 .mp4 第二版本
    assert (season_dir / "测试剧集 (2024) - S01E02.mp4").exists()
    assert not (season_dir / "测试剧集 (2024) - S01E01.mp4").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        imported_row = (
            await session.execute(select(LibraryFile).where(LibraryFile.episode_number == 2))
        ).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert "1 个文件在库中已有同档或更高版本，跳过" in (record.message or "")
    # 来源戳：投递入库的文件行必须能精确回溯到 (站点, 种子)
    assert (imported_row.site_id, imported_row.torrent_id) == ("mteam", "12345")


@pytest.mark.asyncio
async def test_explicit_e00_pilot_skipped_without_blocking(db, tmp_path, monkeypatch):
    """显式 E00（先导/特辑占位）不入库但也不阻塞：正片照常入库、结论 imported、
    文案如实说明——不再误报「解析不出集号」把含 E00 的季包钉在待处理。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    entry = watch / "测试剧集.S01.2160p.Pack"
    entry.mkdir()
    # 真实走 _unit 的显式 SxxEyy 解析，不打桩
    (entry / "Show.S01E00.2160p.mp4").write_bytes(b"pilot-placeholder")
    (entry / "Show.S01E01.2160p.mp4").write_bytes(b"episode-1-content")

    await _sweep_twice(db, library_id, watch)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    assert (season_dir / "测试剧集 (2024) - S01E01.mp4").exists()
    assert not (season_dir / "测试剧集 (2024) - S01E00.mp4").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert "第 0 集（先导/特辑），暂不自动入库" in (record.message or "")
    assert "解析不出" not in (record.message or "")


@pytest.mark.asyncio
async def test_all_dup_skipped_still_closes_fulfilled_wanted(db, tmp_path, monkeypatch):
    """整包都被同档跳过时也要做库存对账：工单集早已由别的源入库的投递，
    结论「无需整理」的同时必须关闭已满足的工单——否则下载任务永远挂在
    「等待入库」（NAS 实测 S06E11）。"""
    from movieclaw_db.models import RuleSet, Subscription, WantedItem, WantedStatus
    from movieclaw_downloader import TorrentBrief

    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, 1))

    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    # E01 已由别的源入库（同档 .mkv），本次投递的 .mp4 将整包被同档跳过
    season_dir = root / "测试剧集 (2024)" / "Season 01"
    season_dir.mkdir(parents=True)
    existing = season_dir / "测试剧集 (2024) - S01E01.mkv"
    existing.write_bytes(b"existing-e01")
    async with db.session() as session:
        rule_set = RuleSet(name="默认", spec={})
        session.add(rule_set)
        await session.commit()
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="tv", rule_set_id=rule_set.id, library_id=library_id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add_all(
            [
                WantedItem(
                    subscription_id=sub.id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=1,
                    status=WantedStatus.GRABBED,
                    info_hash="hash-dup",
                ),
                LibraryFile(
                    library_id=library_id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=1,
                    file_path=str(existing),
                    size_bytes=existing.stat().st_size,
                    resolution="1080p",
                    media_source="WEB-DL",
                    source=FileSource.IMPORTED,
                ),
            ]
        )
        await session.commit()

    brief = TorrentBrief(
        name="测试剧集.S01E01.AltGroup",
        content_name="测试剧集.S01E01.AltGroup",
        completed=True,
        info_hash="hash-dup",
    )

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)

    entry = watch / "测试剧集.S01E01.AltGroup"
    entry.mkdir()
    (entry / "ep1.mp4").write_bytes(b"same-tier-from-other-source")

    library = await _get_library(db, library_id)
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        wanted = (await session.execute(select(WantedItem))).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert "已有同档或更高版本" in (record.message or "")
    assert wanted.status == WantedStatus.IMPORTED


@pytest.mark.asyncio
async def test_full_tree_import_cancels_stale_blocked_batches(db, tmp_path, monkeypatch):
    """整树入库成功后，同条目滞留的分批 blocked 作业必须收口为 cancelled：
    升级补偿只唤醒每个条目最新的作业，被新一轮取代的历史批次没人收口，
    会以「需要处理」永远挂在任务中心（NAS 实测滞留 4 条）。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")

    # 昨天的分批作业因解析失败挂起（白名单钉死 ep1），之后没人收口
    async with db.session() as session:
        session.add(
            Job(
                id="job_stale_batch",
                job_type="library.ingest",
                status=JobStatus.BLOCKED,
                dedupe_key=ingest_mod._ingest_dedupe_key(str(entry), "ready-old"),
                input_data={"entry_path": str(entry), "ready_files": [{"path": "ep1.mkv"}]},
            )
        )
        await session.commit()

    await _sweep_twice(db, library_id, watch)

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        stale = await session.get(Job, "job_stale_batch")
    assert record.status == IngestStatus.IMPORTED
    assert stale.status == JobStatus.CANCELLED
    assert stale.cancel_requested_by == "system:ingest-superseded"


@pytest.mark.asyncio
async def test_upgrade_retries_legacy_partial_import_through_job(db, tmp_path, monkeypatch):
    """旧版误标 imported 的部分入库记录在升级补扫后新建 Job，并补齐漏集。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    parser_ready = False

    def parse_unit(file):
        if file.stem == "ep1" or parser_ready:
            return 1, int(file.stem.removeprefix("ep"))
        return 1, 0

    async def no_assets(_media_item_id: int) -> None:
        return None

    _stub_unit(monkeypatch, parse_unit)
    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")
    (entry / "ep2.mkv").write_bytes(b"episode-2")

    # 先用当前语义制造“成功一集、漏一集”的真实台账，再改成旧版错误状态。
    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        assert record.status == IngestStatus.PENDING
        record.status = IngestStatus.IMPORTED
        await session.commit()

    parser_ready = True
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)

    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    assert job.handler_revision == ingest_mod._ingest_handler_revision()
    await jobs.init_job_dispatcher(max_parallel=1)
    await _wait_job_status(job.id, JobStatus.SUCCEEDED)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    assert (season_dir / "测试剧集 (2024) - S01E01.mkv").exists()
    assert (season_dir / "测试剧集 (2024) - S01E02.mkv").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert record.imported_count == 2
    assert "解析不出" not in (record.message or "")


@pytest.mark.asyncio
async def test_wanted_identity_claim_via_info_hash(db, tmp_path, monkeypatch):
    """订阅身份优先认领；同 hash 的手动锚仍随成功入库一起消费。"""
    from movieclaw_db.models import RuleSet, Subscription, WantedItem, WantedStatus
    from movieclaw_downloader import TorrentBrief

    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    # 名称识别链打桩为必然失败：只有工单认领能给出身份
    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    async with db.session() as session:
        rule_set = RuleSet(name="默认", spec={})
        session.add(rule_set)
        await session.commit()
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="movie", rule_set_id=rule_set.id, library_id=library_id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add_all(
            [
                WantedItem(
                    subscription_id=sub.id,
                    media_item_id=item.id,
                    season_number=0,
                    episode_number=0,
                    status=WantedStatus.GRABBED,
                    info_hash="abc123",
                ),
                # 重复手动点过同一个种子时可能同时留下身份锚。识别优先级会
                # 选择订阅工单，但成功入库仍必须消费这颗锚，不能让任务中心残留。
                ManualDownloadIntent(
                    info_hash="abc123",
                    media_item_id=item.id,
                    library_id=library_id,
                    site_id="mteam",
                ),
            ]
        )
        await session.commit()

    brief = TorrentBrief(
        name="Cryptic.Release.Name",
        content_name="Cryptic.Release.Name",
        completed=True,
        info_hash="abc123",
    )

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)

    entry = watch / "Cryptic.Release.Name"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"video")

    library = await _get_library(db, library_id)
    # 下载器确认完成,单轮即处理
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"

    # 库存对账闭环：入库单元关闭了对应工单（订阅止于投递的另一半）
    async with db.session() as session:
        wanted = (await session.execute(select(WantedItem))).scalars().one()
        manual = (await session.execute(select(ManualDownloadIntent))).scalar_one_or_none()
    assert wanted.status == WantedStatus.IMPORTED
    assert manual is None


@pytest.mark.asyncio
async def test_fallback_only_sweeps_unwatched_dirs(db, tmp_path, monkeypatch):
    """兜底巡检只扫监听覆盖不到的目录：被实时监听的目录绝不重复主动扫。"""
    root = tmp_path / "movies"
    watch1, watch2 = tmp_path / "watch1", tmp_path / "watch2"
    watch1.mkdir()
    watch2.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch1)
    await _make_rule(db, library_id=library_id, source=watch2)

    swept: list[str] = []

    async def record_sweep(rule, library):
        swept.append(rule.source_path)

    monkeypatch.setattr(ingest_mod, "_sweep_dir", record_sweep)

    class _StubWatcher:
        """只监听 watch1 的假观察者。"""

        def watched_keys(self):
            return frozenset({str(watch1)})

        async def refresh_watches(self):
            pass

    monkeypatch.setattr(ingest_mod, "_watcher", _StubWatcher())

    await ingest_mod.ingest_tick()
    assert swept == [str(watch2)]


@pytest.mark.asyncio
async def test_fallback_sweeps_watched_dir_with_pending_entries(db, tmp_path, monkeypatch):
    """回归：被监听目录里还有等待中的条目时，兜底巡检不再跳过它。

    线上实证 bug：下载完成瞬间下载器 API 仍报「未完成」→ 条目被挂起后
    无人唤醒（无新事件、静默自检挂不上、兜底又一刀切跳过被监听目录），
    入库延迟 1 小时+ 且全程无告警。兜底巡检是自检链死亡后的最后保险。
    """
    root = tmp_path / "movies"
    watch1, watch2 = tmp_path / "watch1", tmp_path / "watch2"
    watch1.mkdir()
    watch2.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch1)
    await _make_rule(db, library_id=library_id, source=watch2)

    swept: list[str] = []

    async def record_sweep(rule, library):
        swept.append(rule.source_path)

    monkeypatch.setattr(ingest_mod, "_sweep_dir", record_sweep)

    class _StubWatcher:
        """两个目录都在监听的假观察者。"""

        def watched_keys(self):
            return frozenset({str(watch1), str(watch2)})

        async def refresh_watches(self):
            pass

    monkeypatch.setattr(ingest_mod, "_watcher", _StubWatcher())
    # watch1 里有一个挂起条目（下载器报未完成）；watch2 没有任何等待
    ingest_mod._deferred[str(watch1 / "Some.Movie.2020")] = 0.0

    await ingest_mod.ingest_tick()
    assert swept == [str(watch1)]


@pytest.mark.asyncio
async def test_failed_entry_retried_by_fallback_without_new_events(db, tmp_path, monkeypatch):
    """回归：失败条目必须有唤醒源。此前失败结论落账时静默表/挂起表都被
    弹出、下载结束后也不会再有 fs 事件——三条触发路径同时失灵，台账里
    承诺的「自动退避重试」永远不会发生。修复后失败条目记入进程内失败
    重试表：_has_pending 看得见它，被实时监听的目录在没有任何新事件时
    也会被兜底巡检接住并重试。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    # 环境故障：ffprobe 在但探测失败 → failed
    monkeypatch.setattr(ingest_mod, "ffprobe_available", lambda: True)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: None)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.FAILED
    # 失败落账后条目留在失败重试表：兜底巡检据此不再跳过该目录（修复核心）
    assert ingest_mod._has_pending(str(watch))

    # 环境修复 + 退避到点：目录被实时监听、没有任何新 fs 事件——兜底巡检
    # 仍要看见失败条目并重试成功
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    monkeypatch.setattr(ingest_mod, "FAILED_RETRY_SECONDS", 0)

    class _StubWatcher:
        """目录在实时监听中、但不投递任何事件的假观察者。"""

        def watched_keys(self):
            return frozenset({str(watch)})

        async def refresh_watches(self):
            pass

        def _arm_recheck(self, source_path):
            pass

    monkeypatch.setattr(ingest_mod, "_watcher", _StubWatcher())
    await ingest_mod.ingest_tick()  # 第一轮：重新记录静默指纹
    await ingest_mod.ingest_tick()  # 第二轮：静默确认 → 创建可恢复入库 Job
    async with db.session() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "library.ingest"))
        ).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    deadline = asyncio.get_running_loop().time() + 3
    while True:
        async with db.session() as session:
            job = await session.get(Job, job.id)
            assert job is not None
        if job.status == JobStatus.SUCCEEDED:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"监听入库 Job 未完成，当前状态：{job.status}")
        await asyncio.sleep(0.01)
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.IMPORTED


@pytest.mark.asyncio
async def test_persistent_ingest_copy_resumes_after_dispatcher_restart(db, tmp_path, monkeypatch):
    """复制到一半更新服务：作业退回队列，隐藏副本保留并从已有字节续跑。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch, strategy="copy")
    item = await _make_item(db, kind=MediaKind.MOVIE, title="续传电影", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    monkeypatch.setattr(ingest_mod, "_INGEST_COPY_CHUNK_BYTES", 8)

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / "续传电影 (2026)"
    entry.mkdir()
    source = entry / "movie.mkv"
    source.write_bytes(bytes(range(128)))

    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)

    async with db.session() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "library.ingest"))
        ).scalar_one()
        resources = await jobs.resources_for_jobs(session, [job.id])
    assert {row.resource_type for row in resources[job.id]} >= {
        "import_watch",
        "ingest_path",
        "library",
    }
    final = root / "续传电影 (2026)" / "续传电影 (2026).mkv"
    partial, state_path = ingest_mod._ingest_copy_paths(final)
    assert not final.exists()  # 巡检只入队，不在监听协程里直接搬运

    loop = asyncio.get_running_loop()
    first_chunk = asyncio.Event()
    release_thread = threading.Event()
    thread_finished = threading.Event()
    offsets: list[int] = []
    real_copy_chunk = ingest_mod._copy_ingest_chunk

    def controlled_copy_chunk(source_path, partial_path, **kwargs):
        offsets.append(partial_path.stat().st_size if partial_path.exists() else 0)
        copied = real_copy_chunk(source_path, partial_path, **kwargs)
        if len(offsets) == 1:
            loop.call_soon_threadsafe(first_chunk.set)
            release_thread.wait(timeout=2)
            thread_finished.set()
        return copied

    monkeypatch.setattr(ingest_mod, "_copy_ingest_chunk", controlled_copy_chunk)
    await jobs.init_job_dispatcher(max_parallel=1)
    await asyncio.wait_for(first_chunk.wait(), timeout=2)
    assert partial.stat().st_size == 8
    await jobs.close_job_dispatcher()
    paused = await _wait_job_status(job.id, JobStatus.QUEUED)
    assert paused.attempt == 0
    assert partial.stat().st_size == 8

    release_thread.set()
    assert await asyncio.to_thread(thread_finished.wait, 2)
    await jobs.init_job_dispatcher(max_parallel=1)
    completed = await _wait_job_status(job.id, JobStatus.SUCCEEDED)

    assert final.read_bytes() == source.read_bytes()
    assert offsets[0] == 0 and offsets[1] == 8
    assert not partial.exists() and not state_path.exists()
    assert completed.progress["percent"] == 100.0


@pytest.mark.asyncio
async def test_pending_ingest_claim_unblocks_same_job(db, tmp_path, monkeypatch):
    """识别不出时 Job 待处理；人工认领后复用同一稳定 job id 完成入库。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="认领后入库", year=2026)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    async def identify_none(*_args):
        return None

    async def ensure_item(_service, kind, tmdb_id):
        assert kind is MediaKind.MOVIE and tmdb_id == item.tmdb_id
        return item

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)
    monkeypatch.setattr(ingest_mod.MediaLibraryService, "ensure_media_item", ensure_item)
    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / "unknown-release"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)

    async with db.session() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "library.ingest"))
        ).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    blocked = await _wait_job_status(job.id, JobStatus.BLOCKED)
    assert blocked.error["code"] == "INGEST_IDENTITY_REQUIRED"

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        assert record.status == IngestStatus.PENDING
        await ingest_mod.claim_entry(session, record.id, item.tmdb_id)

    completed = await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    assert completed.id == job.id
    assert (root / "认领后入库 (2026)" / "认领后入库 (2026).mkv").exists()


@pytest.mark.asyncio
async def test_upgrade_unblocks_old_revision_parser_gap_job(db, tmp_path, monkeypatch):
    """旧处理器因季集解析挂起的 Job，升级后只唤醒原 Job，不制造重复任务。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title="补偿剧集", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    parser_ready = False

    def parse_unit(file):
        if file.stem == "ep1" or parser_ready:
            return 1, int(file.stem.removeprefix("ep"))
        return 1, 0

    async def no_assets(_media_item_id: int) -> None:
        return None

    _stub_unit(monkeypatch, parse_unit)
    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / "补偿剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")
    (entry / "ep2.mkv").write_bytes(b"episode-2")
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)

    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    blocked = await _wait_job_status(job.id, JobStatus.BLOCKED)
    assert blocked.error["code"] == "INGEST_EPISODE_PARSE_REQUIRED"

    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        unchanged = await session.get(Job, job.id)
        assert unchanged is not None
        assert unchanged.status == JobStatus.BLOCKED
        assert unchanged.handler_revision == ingest_mod._ingest_handler_revision()

    # 模拟解析能力升级：旧 revision 的 blocked Job 应原地恢复；同一 revision
    # 后续再扫只会短路，避免每次启动都重新跑一遍。
    async with db.session() as session:
        stored = await session.get(Job, job.id)
        assert stored is not None
        stored.handler_revision = "library.ingest.v1"
        await session.commit()
    parser_ready = True
    await ingest_mod._sweep_dir(rule, library)
    completed = await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    assert completed.id == job.id

    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert len(all_jobs) == 1
    assert record.status == IngestStatus.IMPORTED
    assert record.imported_count == 2
    assert (root / "补偿剧集 (2026)" / "Season 01" / "补偿剧集 (2026) - S01E02.mkv").exists()


async def _make_blocked_parser_gap_job(db, tmp_path, monkeypatch, *, title: str):
    """公共铺垫：ep1 入库、ep2 解析不出集号 → 记录 pending + Job blocked。

    返回 (root, watch, entry, rule, library, job)。parse_unit 对 ep2 永远
    解析失败，其余 epN 解析为 (1, N)——改名/补集/能力升级场景在各测试里
    自行推进。
    """
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title=title, year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    def parse_unit(file):
        if file.stem == "ep2":
            return 1, 0
        return 1, int(file.stem.removeprefix("ep"))

    async def no_assets(_media_item_id: int) -> None:
        return None

    _stub_unit(monkeypatch, parse_unit)
    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / f"{title} S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")
    (entry / "ep2.mkv").write_bytes(b"episode-2")
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    blocked = await _wait_job_status(job.id, JobStatus.BLOCKED)
    assert blocked.error["code"] == "INGEST_EPISODE_PARSE_REQUIRED"
    return root, watch, entry, rule, library, job


@pytest.mark.asyncio
async def test_blocked_parser_gap_job_wakes_on_content_change(db, tmp_path, monkeypatch):
    """blocked 不能堵死"内容变化自动重试"：条目内容变化时原地唤醒同一 Job。"""
    root, _watch, entry, rule, library, job = await _make_blocked_parser_gap_job(
        db, tmp_path, monkeypatch, title="补集剧集"
    )

    # 用户删掉解析不出的文件、换上修好的一集（指纹变化，条目路径不变）——
    # 任务化之前这正是靠指纹对比自动重入库的场景
    (entry / "ep2.mkv").unlink()
    (entry / "ep3.mkv").write_bytes(b"episode-3!")
    await ingest_mod._sweep_dir(rule, library)
    completed = await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    assert completed.id == job.id

    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert len(all_jobs) == 1
    assert record.status == IngestStatus.IMPORTED
    assert record.imported_count == 2
    assert (root / "补集剧集 (2026)" / "Season 01" / "补集剧集 (2026) - S01E03.mkv").exists()


@pytest.mark.asyncio
async def test_model_upgrade_wakes_blocked_parser_gap_job(db, tmp_path, monkeypatch):
    """纯 NER 模型升级（代码修订不变）也要触发一次补偿：修订号含模型 tag。"""
    root, _watch, _entry, rule, library, job = await _make_blocked_parser_gap_job(
        db, tmp_path, monkeypatch, title="模型剧集"
    )

    # 模拟应用内一键更新模型后的重启：MOVIECLAW_NER_DIR 指向带新 tag 的目录，
    # 同时新模型解析能力覆盖 ep2
    model_dir = tmp_path / "models" / "torrent-ner-v9"
    model_dir.mkdir(parents=True)
    (model_dir / ".release-tag").write_text("torrent-ner-v9\n", encoding="utf-8")
    monkeypatch.setenv("MOVIECLAW_NER_DIR", str(model_dir))
    _stub_unit(monkeypatch, lambda file: (1, int(file.stem.removeprefix("ep"))))

    await ingest_mod._sweep_dir(rule, library)
    completed = await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    assert completed.handler_revision == f"{ingest_mod._INGEST_HANDLER_REVISION}+torrent-ner-v9"

    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert len(all_jobs) == 1
    assert record.status == IngestStatus.IMPORTED
    assert record.imported_count == 2
    assert (root / "模型剧集 (2026)" / "Season 01" / "模型剧集 (2026) - S01E02.mkv").exists()


@pytest.mark.asyncio
async def test_compensation_zero_new_import_keeps_imported_summary(db, tmp_path, monkeypatch):
    """补偿重跑零新增时，message 不能丢掉"此前已入库 N 个文件"的事实。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.TV, title="摘要剧集", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, 1) if file.stem == "ep1" else (1, 0))

    entry = watch / "摘要剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")
    (entry / "ep2.mkv").write_bytes(b"episode-2")

    # 制造旧版误标 imported 的部分入库台账，再触发升级补偿（解析能力未变）
    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        record.status = IngestStatus.IMPORTED
        await session.commit()

    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    await _wait_job_status(job.id, JobStatus.BLOCKED)

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.PENDING
    assert record.imported_count == 1
    assert "此前已入库 1 个文件" in (record.message or "")
    assert "ep2.mkv」解析不出集号，未入库" in (record.message or "")


@pytest.mark.asyncio
async def test_staging_partial_import_not_auto_compensated(db, tmp_path, monkeypatch):
    """有过成功搬运的中转记录不自动补偿：已消费的文件重跑会被重复上传。"""
    root, watch, staging = tmp_path / "tv", tmp_path / "watch", tmp_path / "staging"
    watch.mkdir()
    staging.mkdir()
    await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="中转剧集", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    _stub_unit(monkeypatch, lambda file: (1, 1) if file.stem == "ep1" else (1, 0))
    async with db.session() as session:
        session.add(
            ImportWatch(
                source_path=str(watch), strategy="hardlink", kind="tv", target_path=str(staging)
            )
        )
        await session.commit()

    entry = watch / "中转剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"episode-1")
    (entry / "ep2.mkv").write_bytes(b"episode-2")
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
    for _ in range(2):
        await ingest_mod._sweep_dir(rule, None, execute_inline=True)
    transferred = staging / "中转剧集 (2026)" / "Season 01" / "中转剧集 (2026) - S01E01.mkv"
    assert transferred.exists()

    # 外部工具消费（上传后删除），且尚未回流入库；台账为旧版误标的 imported
    transferred.unlink()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        assert record.imported_count == 1
        record.status = IngestStatus.IMPORTED
        await session.commit()

    await ingest_mod._sweep_dir(rule, None)
    await ingest_mod._sweep_dir(rule, None)
    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
        record = (await session.execute(select(IngestEntry))).scalar_one()
    # 不建补偿任务、不重新搬运（否则 ep1 会被重复上传）；记录保持升级前状态
    assert all_jobs == []
    assert record.status == IngestStatus.IMPORTED
    assert not transferred.exists()


@pytest.mark.asyncio
async def test_cancelled_ingest_is_not_resurrected_by_startup_sweep(db, tmp_path, monkeypatch):
    """用户取消的同一磁盘版本不能在下次启动补扫时被静默重新创建。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="取消入库", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    entry = watch / "取消入库 (2026)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")

    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
        await jobs.request_cancel(session, job.id, requested_by="test")

    monkeypatch.setattr(ingest_mod, "_stability", {})  # 模拟新进程启动
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
    assert len(all_jobs) == 1 and all_jobs[0].status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_auto_routed_ingest_adds_target_library_resource(db, tmp_path, monkeypatch):
    """自动路由在识别后补充真实库资源，让所有库级作业共享同一互斥锁。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="自动路由电影", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)
    entry = watch / "自动路由电影 (2026)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    async with db.session() as session:
        rule = ImportWatch(
            source_path=str(watch),
            strategy="hardlink",
            library_id=None,
            kind="movie",
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
    await ingest_mod._sweep_dir(rule, None)
    await ingest_mod._sweep_dir(rule, None)

    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    async with db.session() as session:
        resources = await jobs.resources_for_jobs(session, [job.id])
    assert any(
        row.resource_type == "library"
        and row.resource_id == str(library_id)
        and row.relation == "target"
        for row in resources[job.id]
    )
    assert not ingest_mod._has_pending(str(watch))  # 重试成功后重试表清空


@pytest.mark.asyncio
async def test_deferred_recheck_polls_api_and_wakes_on_flip(db, tmp_path, monkeypatch):
    """挂起条目的状态轮询自检：种子未完成时只查 API 重挂下一轮（不触发
    巡检）；翻转成完成后立即唤醒对应目录的巡检。"""
    from movieclaw_downloader import TorrentBrief

    watch = tmp_path / "watch"
    watch.mkdir()
    brief = TorrentBrief(name="Some.Movie.2020", content_name="Some.Movie.2020", completed=False)

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "DEFERRED_POLL_SECONDS", 0.01)
    ingest_mod._deferred[str(watch / "Some.Movie.2020")] = 0.0

    watcher = ingest_mod.IngestWatcher()
    try:
        watcher._arm_recheck(str(watch))
        await asyncio.sleep(0.1)
        assert watcher._queue.empty()  # 未翻转：不巡检，持续重挂
        brief.completed = True
        await asyncio.sleep(0.1)
        assert watcher._queue.get_nowait() == str(watch)  # 翻转：唤醒巡检
    finally:
        for task in watcher._rechecks.values():
            task.cancel()


def test_ingest_event_filter_ignores_read_only_events():
    """只读事件（做种上传/ffprobe 读文件）不触发巡检；写类事件放行。"""
    assert not ingest_mod._is_ingest_relevant(SimpleNamespace(event_type="opened"))
    assert not ingest_mod._is_ingest_relevant(SimpleNamespace(event_type="closed_no_write"))
    for kind in ("created", "modified", "moved", "deleted", "closed"):
        assert ingest_mod._is_ingest_relevant(SimpleNamespace(event_type=kind))


@pytest.mark.asyncio
async def test_rule_validation(db, tmp_path):
    """规则校验：与任何库根重叠拒绝、源目录去重、坏策略拒绝、同盘检测。"""
    root = tmp_path / "movies"
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    watch = tmp_path / "watch"
    watch.mkdir()
    async with db.session() as session:
        service = ImportWatchConfigService(session)
        with pytest.raises(BadRequestException):
            await service.create(
                source_path=str(root / "inbox"), strategy="hardlink", library_id=library_id
            )
        with pytest.raises(BadRequestException):
            await service.create(source_path=str(watch), strategy="move", library_id=library_id)
        # 合法创建；同源目录再建拒绝
        await service.create(source_path=str(watch), strategy="copy", library_id=library_id)
        with pytest.raises(BadRequestException):
            await service.create(source_path=str(watch), strategy="hardlink", library_id=library_id)


@pytest.mark.asyncio
async def test_entry_stats_counts_entries_and_imported_files(db, tmp_path):
    """摘要行两口径：状态 → 条目数，外加已入库文件总数（剧集季包一条目多集）。

    只认源目录顶层直系条目：嵌套子路径与其他源目录的行不串数；
    文件数只累计已入库条目（pending 行的 imported_count 不算）。
    """
    watch, other = tmp_path / "watch", tmp_path / "other"
    async with db.session() as session:
        rule = ImportWatch(source_path=str(watch), strategy="hardlink", kind="tv")
        other_rule = ImportWatch(source_path=str(other), strategy="hardlink", kind="movie")
        session.add(rule)
        session.add(other_rule)
        for path, status, files in (
            (watch / "剧A.S01", "imported", 8),
            (watch / "剧A.S02", "imported", 2),
            (watch / "认不出的目录", "pending", 0),
            (watch / "剧A.S01" / "nested", "imported", 99),  # 嵌套路径不属于本规则
            (other / "电影B", "imported", 1),
        ):
            session.add(
                IngestEntry(
                    entry_path=str(path), fingerprint="fp", status=status, imported_count=files
                )
            )
        await session.commit()
        await session.refresh(rule)
        await session.refresh(other_rule)

        stats = await ingest_mod.entry_stats(session, [rule, other_rule])

    ledger = stats[rule.id]
    assert ledger.counts["imported"] == 2
    assert ledger.counts["pending"] == 1
    assert ledger.imported_files == 10  # 8 + 2：嵌套行与 pending 行都不计
    assert stats[other_rule.id].counts["imported"] == 1
    assert stats[other_rule.id].imported_files == 1


@pytest.mark.asyncio
async def test_entry_stats_dedupes_works_across_seasons_and_versions(db, tmp_path):
    """「已入库」报作品数：同一部剧的多季与多版本合并计一部。

    真实场景（用户只入了 4 部剧却显示「已入库 11」）：一部剧的 S01/S02 各是
    一个条目，同一季的不同发布组/DV/HDR 版本又各是一个条目——条目数远大于
    作品数。按 media_item_id 去重后才是"入库了几部"。
    没有 media_item_id 的老条目（迁移前入库、回填未命中）按条目各计一部，
    宁可少合并也不凭标题猜。
    """
    watch = tmp_path / "watch"
    async with db.session() as session:
        rule = ImportWatch(source_path=str(watch), strategy="hardlink", kind="tv")
        session.add(rule)
        for name, item_id, files in (
            ("剧A.S01.CHDWEB", 1, 8),  # 同一部剧：两季 + S02 三个版本
            ("剧A.S02.MWeb.DV", 1, 6),
            ("剧A.S02.MWeb.HDR", 1, 6),
            ("剧A.S02.CMCTV.DV", 1, 6),
            ("剧B.S01", 2, 10),
            ("剧C.S01", None, 3),  # 老条目：未回填，单独计一部
            ("认不出的", None, 0),  # pending 不计入作品数
        ):
            session.add(
                IngestEntry(
                    entry_path=str(watch / name),
                    fingerprint=f"fp-{name}",
                    status="imported" if files else "pending",
                    imported_count=files,
                    media_item_id=item_id,
                )
            )
        await session.commit()
        await session.refresh(rule)

        stats = await ingest_mod.entry_stats(session, [rule])

    ledger = stats[rule.id]
    assert ledger.counts["imported"] == 6  # 条目数照旧
    assert ledger.imported_works == 3  # 剧A + 剧B + 未回填的剧C
    assert ledger.imported_files == 39
