"""章节场景图（docs/design/video-chapters.md）：抓图、懒触发资格、详情接口投影、
整库作业的目标筛选。抓图部分需要真实 ffmpeg（lavfi 合成带章节的测试片），
没装则跳过；其余不依赖 ffmpeg。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.api.routes.libraries import get_library_item
from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library import chapters as chapters_mod
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    Library,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    MediaSource,
)

_ADMIN = Principal(kind="admin", name="tester")
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

_CHAPTER_META = """;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=20000
title=Opening
[CHAPTER]
TIMEBASE=1/1000
START=20000
END=45000
title=00:00:20.000
[CHAPTER]
TIMEBASE=1/1000
START=45000
END=60000
"""


def _make_video(dest: Path, *, chapters: bool = True) -> None:
    """60 秒测试片：关键帧每 2 秒一个（-g 50 @ 25fps），可选三段内嵌章节。"""
    meta = dest.parent / "chapters.txt"
    meta.write_text(_CHAPTER_META)
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x180:rate=25:duration=60",
    ]
    if chapters:
        cmd += ["-i", str(meta), "-map", "0", "-map_metadata", "1"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-an", str(dest)]
    subprocess.run(cmd, check=True, timeout=120)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'chapters.db'}")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(
    db, video: Path, *, chapters=None, chapter_images=None, enabled=True
) -> tuple[int, int, int]:
    async with db.session() as session:
        lib = Library(
            name="家庭录像",
            kind="video",
            source="local",
            root_paths=[str(video.parent)],
            extract_chapter_images=enabled,
        )
        session.add(lib)
        await session.flush()
        item = MediaItem(
            kind="video",
            source=MediaSource.LOCAL,
            external_id=f"{lib.id}:path:abcd",
            title="测试片",
            original_title="",
            aliases=[],
        )
        session.add(item)
        await session.flush()
        session.add(MediaMetadata(media_item_id=item.id))
        row = LibraryFile(
            library_id=lib.id,
            media_item_id=item.id,
            file_path=str(video),
            size_bytes=video.stat().st_size if video.exists() else 0,
            container="mkv",
            duration_seconds=60,
            chapters=chapters,
            chapter_images=chapter_images,
            source=FileSource.SCANNED,
        )
        session.add(row)
        await session.commit()
        return lib.id, item.id, row.id


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")
async def test_refresh_file_extracts_embedded_chapter_stills(db, tmp_path):
    video = tmp_path / "media" / "test.mkv"
    video.parent.mkdir()
    _make_video(video)
    _lib_id, item_id, file_id = await _seed(db, video)  # chapters=None：走补探

    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert await chapters_mod.refresh_file_chapter_images(session, row)
        assert [c["start_ms"] for c in row.chapters] == [0, 20000, 45000]
        assert row.chapters[0]["title"] == "Opening" and row.chapters[1]["title"] is None
        images = row.chapter_images
        assert [e["start_ms"] for e in images] == [0, 20000, 45000]
        # 第 0 章从 15s 处抓，frame_ms 是真实帧时间（关键帧对齐后 ≥15s）
        assert images[0]["frame_ms"] >= 15000
        assert images[1]["frame_ms"] >= 20000
        assets = Path(get_settings().metadata_dir) / "images"
        for entry in images:
            assert entry["image"] == chapters_mod.image_rel_path(
                item_id, file_id, entry["start_ms"]
            )
            assert (assets / entry["image"]).stat().st_size > 0
        # 已抓过：不 force 就是 no-op
        assert not await chapters_mod.refresh_file_chapter_images(session, row)

        # 章节变了（内嵌被去掉 → 60s 不足 90s 合成 0 张）：force 重做，死图清掉
        row.chapters = []
        assert await chapters_mod.refresh_file_chapter_images(session, row, force=True)
        assert row.chapter_images == []
        assert not (assets / str(item_id) / "chapters" / str(file_id)).exists()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")
async def test_synthetic_chapters_get_stills_and_item_refresh_cleans_orphans(db, tmp_path):
    video = tmp_path / "media" / "plain.mkv"
    video.parent.mkdir()
    _make_video(video, chapters=False)
    _lib_id, item_id, file_id = await _seed(db, video, chapters=[])
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        row.duration_seconds = 100  # 90s～10min 档：合成 3 张（测试片实际 60s，越界的停止）
        await session.commit()
    assets = Path(get_settings().metadata_dir) / "images"
    orphan = assets / str(item_id) / "chapters" / "999"
    orphan.mkdir(parents=True)
    (orphan / "0000000000.jpg").write_bytes(b"x")

    assert await chapters_mod.refresh_chapter_images(item_id) == 1
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        # 合成起点 6s / 50s / 94s：94s 超出实际片长，ffmpeg 抓不到帧则没图，前两张必有
        starts = [e["start_ms"] for e in row.chapter_images]
        assert starts[:2] == [6000, 50000]
        assert all(e["frame_ms"] >= e["start_ms"] for e in row.chapter_images)
    assert not orphan.exists()  # 孤儿目录（没有对应台账行）被清掉


async def test_detail_projects_chapters_without_touching_ffmpeg(db, tmp_path, monkeypatch):
    video = tmp_path / "media" / "seeded.mkv"
    video.parent.mkdir()
    video.write_bytes(b"not a real video")
    embedded = [
        {"start_ms": 0, "end_ms": 20000, "title": "Opening"},
        {"start_ms": 20000, "end_ms": None, "title": None},
    ]
    images = [{"start_ms": 20000, "frame_ms": 22000, "image": "1/chapters/1/0000020000.jpg"}]
    lib_id, item_id, file_id = await _seed(db, video, chapters=embedded, chapter_images=images)
    assets = Path(get_settings().metadata_dir) / "images" / "1" / "chapters" / "1"
    assets.mkdir(parents=True)
    (assets / "0000020000.jpg").write_bytes(b"jpg")
    scheduled: list[int] = []
    monkeypatch.setattr(
        chapters_mod, "schedule_item_chapter_images", lambda i: scheduled.append(i) or True
    )

    async with db.session() as session:
        view = (await get_library_item(lib_id, item_id, _ADMIN, session)).data
    chapters = view.files[0].chapters
    assert chapters is not None and [c.start_ms for c in chapters] == [0, 20000]
    assert chapters[0].title == "Opening" and chapters[0].image_url is None
    assert chapters[0].end_ms == 20000 and chapters[1].end_ms == 60000
    assert chapters[1].frame_ms == 22000 and not chapters[1].synthetic
    assert chapters[1].image_url.startswith("/images/assets/1/chapters/1/0000020000.jpg?v=")
    # 已抓过图：不懒触发
    assert view.chapters_pending is False and scheduled == []

    # 没抓过图（chapter_images=NULL）：详情页懒触发一次并告知前端轮询
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        row.chapter_images = None
        await session.commit()
        view = (await get_library_item(lib_id, item_id, _ADMIN, session)).data
    assert view.chapters_pending is True and scheduled == [item_id]


async def test_detail_skips_lazy_trigger_when_library_switch_off(db, tmp_path, monkeypatch):
    video = tmp_path / "media" / "off.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    lib_id, item_id, _file_id = await _seed(db, video, chapters=None, enabled=False)
    monkeypatch.setattr(
        chapters_mod, "schedule_item_chapter_images", lambda i: pytest.fail("不该触发")
    )
    async with db.session() as session:
        view = (await get_library_item(lib_id, item_id, _ADMIN, session)).data
    assert view.chapters_pending is False
    assert view.files[0].chapters is None  # 章节未探测 → null


async def test_job_targets_skip_done_disc_and_strm(db, tmp_path):
    video = tmp_path / "media" / "a.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    lib_id, item_id, done_id = await _seed(db, video, chapters=[], chapter_images=[])
    async with db.session() as session:
        extra = [
            LibraryFile(
                library_id=lib_id,
                media_item_id=item_id,
                file_path=str(video.parent / "b.mkv"),
                container="mkv",
                source=FileSource.SCANNED,
            ),
            LibraryFile(
                library_id=lib_id,
                media_item_id=item_id,
                file_path=str(video.parent / "BD"),
                container="bluray",
                source=FileSource.SCANNED,
            ),
            LibraryFile(
                library_id=lib_id,
                media_item_id=item_id,
                file_path=str(video.parent / "c.strm"),
                container="strm",
                source=FileSource.SCANNED,
            ),
            LibraryFile(
                library_id=lib_id,
                media_item_id=None,
                file_path=str(video.parent / "d.mkv"),
                container="mkv",
                source=FileSource.SCANNED,
            ),
        ]
        session.add_all(extra)
        await session.commit()
        pending = await chapters_mod._job_targets(session, lib_id, force=False)
        everything = await chapters_mod._job_targets(session, lib_id, force=True)
        b_id = (
            await session.execute(
                select(LibraryFile.id).where(LibraryFile.file_path.endswith("b.mkv"))
            )
        ).scalar_one()
    assert pending == [b_id]  # 已抓过的、原盘、strm、未识别的都不在
    assert set(everything) == {done_id, b_id}


async def test_probe_failure_and_missing_ffmpeg_leave_row_untouched(db, tmp_path, monkeypatch):
    """章节补探失败或系统没有 ffmpeg：什么都不写（保持 NULL），下次入口自动再来。"""
    video = tmp_path / "media" / "x.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    _lib_id, _item_id, file_id = await _seed(db, video, chapters=None)
    monkeypatch.setattr(chapters_mod, "probe_chapters", lambda _p: None)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert not await chapters_mod.refresh_file_chapter_images(session, row)
        assert row.chapters is None and row.chapter_images is None

    # 章节已知但 ffmpeg 不在 PATH：抓图返回 None，chapter_images 仍为 NULL
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        row.chapters = []
        row.duration_seconds = 600
        await session.commit()

    def _no_ffmpeg(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(chapters_mod.subprocess, "run", _no_ffmpeg)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert not await chapters_mod.refresh_file_chapter_images(session, row)
        assert row.chapter_images is None


async def test_item_regenerate_route_enqueues_resumable_job(db, tmp_path):
    """条目菜单「重新生成章节」：独立于刷新元数据，落成可恢复 Job（重启不丢）；
    同条目重复点复用同一个作业；库关了开关拒绝。

    曾经的问题：它是个裸 asyncio 任务，重启直接消失，任务中心也看不到。
    """
    from movieclaw_api.api.routes.libraries import get_library_item, regenerate_item_chapter_images
    from movieclaw_api.exceptions import ConflictException

    video = tmp_path / "media" / "r.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    lib_id, item_id, _file_id = await _seed(db, video, chapters=[], chapter_images=[])
    async with db.session() as session:
        resp = await regenerate_item_chapter_images(lib_id, item_id, None, session)
        job_id = resp.data["job_id"]
        assert resp.data["started"] is True and resp.data["created"] is True
        # 重复点：返回同一个作业，不排第二份
        again = await regenerate_item_chapter_images(lib_id, item_id, None, session)
        assert again.data["job_id"] == job_id and again.data["created"] is False

    # 作业还排在队列里（进程内没有任何协程在跑）时，详情页仍要轮询
    async with db.session() as session:
        view = (await get_library_item(lib_id, item_id, _ADMIN, session)).data
        assert view.chapters_pending is True

    async with db.session() as session:
        lib = await session.get(Library, lib_id)
        lib.extract_chapter_images = False
        await session.commit()
        with pytest.raises(ConflictException):
            await regenerate_item_chapter_images(lib_id, item_id, None, session)


async def test_library_views_carry_chapter_job_progress(db, tmp_path):
    """管理页要看到章节作业的排队/进度：列表与单库接口随库带出 chapter_job，
    跑完后不再带。曾经的问题：点了「生成章节」后管理页毫无反应，只有活动页看得到。"""
    from movieclaw_api.api.routes.libraries import get_library, list_libraries
    from movieclaw_db.models.job import Job, JobStatus

    video = tmp_path / "media" / "p.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    lib_id, _item_id, _file_id = await _seed(db, video, chapters=[], chapter_images=None)
    async with db.session() as session:
        created = await chapters_mod.enqueue_library_chapter_images_job(
            session, lib_id, "家庭录像", force=False
        )
        await session.commit()
    job_id = created.job.id

    async with db.session() as session:
        views = (await list_libraries(kind=None, principal=_ADMIN, session=session)).data
        view = next(v for v in views if v.id == lib_id)
        assert view.chapter_job is not None
        assert (view.chapter_job.job_id, view.chapter_job.status) == (job_id, "queued")
        assert view.chapter_job.percent is None and view.chapter_job.stopping is False

    # 模拟跑到一半：进度字段沿用 JobContext.update_progress 的口径
    async with db.session() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.RUNNING
        job.progress = {
            **job.progress,
            "current": 3,
            "total": 8,
            "percent": 37.5,
            "details": {"failed": 1},
        }
        await session.commit()
    async with db.session() as session:
        detail = (await get_library(lib_id, principal=_ADMIN, session=session)).data
        cj = detail.chapter_job
        assert cj is not None and cj.status == "running"
        assert (cj.processed, cj.total, cj.failed, cj.percent) == (3, 8, 1, 37.5)

    # 跑完：不再随库带出
    async with db.session() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.SUCCEEDED
        await session.commit()
    async with db.session() as session:
        detail = (await get_library(lib_id, principal=_ADMIN, session=session)).data
        assert detail.chapter_job is None


async def _seed_files(db, lib_id: int, item_id: int, folder: Path, count: int) -> list[int]:
    """给同一个库再挂 count 个在位文件，返回 id。"""
    ids: list[int] = []
    async with db.session() as session:
        for i in range(count):
            row = LibraryFile(
                library_id=lib_id,
                media_item_id=item_id,
                file_path=str(folder / f"ep{i}.mkv"),
                container="mkv",
                duration_seconds=600,
                chapters=[],
                source=FileSource.SCANNED,
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()
    return ids


async def _claim(db, created) -> tuple[str, jobs.JobContext]:
    """把刚入队的作业改成"已被领取"（running + 租约），返回 job_id 与其 JobContext。"""
    from datetime import timedelta

    from movieclaw_db.models import utcnow
    from movieclaw_db.models.job import Job, JobStatus

    async with db.session() as session:
        job = await session.get(Job, created.job.id)
        job.status = JobStatus.RUNNING
        job.lease_owner = "lease-test"
        job.lease_expires_at = utcnow() + timedelta(minutes=5)
        await session.commit()
    return created.job.id, jobs.JobContext(
        created.job.id, lease_token="lease-test", lease_lost=asyncio.Event()
    )


@pytest.mark.parametrize("force", [False, True])
async def test_library_job_resumes_after_restart(db, tmp_path, monkeypatch, force):
    """重启（应用内更新/重启把 Job 退回队列重跑处理器）后不重抓已完成的文件。

    补缺靠台账、force 重抓靠进度里的游标；两种模式的进度都从断点接着数，
    而不是回到 0 再数一遍"剩下的文件"——曾经的问题：force 重抓在重启后从头
    再跑一遍整库，补缺虽然跳过了已完成的行，但进度归零看着像重跑。
    """
    from movieclaw_db.models.job import Job

    monkeypatch.setattr(jobs.JobContext, "progress_due", lambda self, **_: True)
    folder = tmp_path / "media"
    folder.mkdir()
    video = folder / "a.mkv"
    video.write_bytes(b"x")
    # 已有图的第一行：force 时它也要重抓，补缺时它不该出现在目标里
    lib_id, item_id, first_id = await _seed(db, video, chapters=[], chapter_images=[])
    rest = await _seed_files(db, lib_id, item_id, folder, 4)
    everything = {first_id, *rest}

    calls: list[int] = []
    stop_after = 2  # 抓完第二个文件就"停机"

    async def fake_refresh(session, row, *, force=False):
        if not force and row.chapter_images is not None:
            return False
        calls.append(row.id)
        if len(calls) > stop_after:
            raise asyncio.CancelledError("模拟重启")  # 停机会直接取消执行协程
        row.chapter_images = [{"start_ms": 0, "frame_ms": 0, "image": f"x/{row.id}.jpg"}]
        session.add(row)
        await session.commit()
        return True

    monkeypatch.setattr(chapters_mod, "refresh_file_chapter_images", fake_refresh)
    async with db.session() as session:
        created = await chapters_mod.enqueue_library_chapter_images_job(
            session, lib_id, "家庭录像", force=force
        )
    job_id, context = await _claim(db, created)

    with pytest.raises(asyncio.CancelledError):
        await chapters_mod._run_chapter_images_job(context, {"library_id": lib_id, "force": force})
    first_round = list(calls)
    assert len(first_round) == stop_after + 1  # 最后一个是被打断的那个

    # 重启：同一个 Job 被重新领取，处理器整体再跑一遍
    interrupted = first_round.pop()  # 被打断的文件没写回台账，重启后要重来
    calls.clear()
    stop_after = 99
    result = await chapters_mod._run_chapter_images_job(
        context, {"library_id": lib_id, "force": force}
    )

    assert not set(first_round) & set(calls), "重启后不该重抓上一轮已完成的文件"
    assert interrupted in calls  # 只有被打断的那个重来
    assert set(first_round) | set(calls) == (everything if force else everything - {first_id})
    async with db.session() as session:
        job = await session.get(Job, job_id)
    total = len(everything) if force else len(everything) - 1
    assert (job.progress["current"], job.progress["total"]) == (total, total)
    assert result["processed"] == total  # 累计口径：不是"重启后这一轮跑了几个"


async def test_item_job_targets_only_that_item_and_cleans_orphans(db, tmp_path, monkeypatch):
    """条目作业：只抓该条目的合格文件（原盘/strm 不抓），顺带清掉孤儿目录；
    条目已不存在时明确失败，而不是静默跑完。"""
    from movieclaw_db.models.job import Job, JobStatus

    folder = tmp_path / "media"
    folder.mkdir()
    video = folder / "a.mkv"
    video.write_bytes(b"x")
    lib_id, item_id, first_id = await _seed(db, video, chapters=[])
    (mine,) = await _seed_files(db, lib_id, item_id, folder, 1)
    async with db.session() as session:
        # 同库另一个条目的文件，以及本条目里不该抓的 strm
        other = MediaItem(
            kind="video",
            source=MediaSource.LOCAL,
            external_id=f"{lib_id}:path:other",
            title="别的片",
            original_title="",
            aliases=[],
        )
        session.add(other)
        await session.flush()
        session.add(MediaMetadata(media_item_id=other.id))
        session.add_all(
            [
                LibraryFile(
                    library_id=lib_id,
                    media_item_id=other.id,
                    file_path=str(folder / "other.mkv"),
                    container="mkv",
                    source=FileSource.SCANNED,
                ),
                LibraryFile(
                    library_id=lib_id,
                    media_item_id=item_id,
                    file_path=str(folder / "link.strm"),
                    container="strm",
                    source=FileSource.SCANNED,
                ),
            ]
        )
        await session.commit()
    assets = Path(get_settings().metadata_dir) / "images"
    orphan = assets / str(item_id) / "chapters" / "999"
    orphan.mkdir(parents=True)
    (orphan / "0000000000.jpg").write_bytes(b"x")

    calls: list[int] = []

    async def fake_refresh(session, row, *, force=False):
        calls.append(row.id)
        return True

    monkeypatch.setattr(chapters_mod, "refresh_file_chapter_images", fake_refresh)
    async with db.session() as session:
        created = await chapters_mod.enqueue_item_chapter_images_job(session, item_id, "测试片")
    _job_id, context = await _claim(db, created)
    result = await chapters_mod._run_item_chapter_images_job(
        context, {"media_item_id": item_id, "force": True}
    )

    assert sorted(calls) == sorted([first_id, mine])  # 别的条目与 strm 都不在内
    assert result["processed"] == 2
    assert not orphan.exists()

    # 条目连台账行都没有了：明确失败，用户能在任务中心看到原因
    async with db.session() as session:
        created2 = await chapters_mod.enqueue_item_chapter_images_job(session, 4242, "已删条目")
        job = await session.get(Job, created2.job.id)
        job.status = JobStatus.RUNNING
        job.lease_owner = "lease-gone"
        await session.commit()
    gone = jobs.JobContext(created2.job.id, lease_token="lease-gone", lease_lost=asyncio.Event())
    with pytest.raises(jobs.JobFailed):
        await chapters_mod._run_item_chapter_images_job(gone, {"media_item_id": 4242})
