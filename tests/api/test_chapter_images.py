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
        # 合成起点 6s / 50s / 94s：94s 超出实际片长，抓不到帧就记一条墓碑
        # （不是干脆没记录，否则补缺每一轮都会对着它重跑），前两张必有图
        images = [e for e in row.chapter_images if "image" in e]
        assert [e["start_ms"] for e in images][:2] == [6000, 50000]
        assert all(e["frame_ms"] >= e["start_ms"] for e in images)
        assert [e for e in row.chapter_images if e.get("failed")] in (
            [],
            [{"start_ms": 94000, "failed": True}],
        )
    assert not orphan.exists()  # 孤儿目录（没有对应台账行）被清掉


async def test_detail_projects_chapters_without_touching_ffmpeg(db, tmp_path, monkeypatch):
    video = tmp_path / "media" / "seeded.mkv"
    video.parent.mkdir()
    video.write_bytes(b"not a real video")
    embedded = [
        {"start_ms": 0, "end_ms": 20000, "title": "Opening"},
        {"start_ms": 20000, "end_ms": None, "title": None},
    ]
    # 第 0 章抓不出来（墓碑）：详情页照旧当"这章没图"，补缺也认它已有结论
    images = [
        {"start_ms": 0, "failed": True},
        {"start_ms": 20000, "frame_ms": 22000, "image": "1/chapters/1/0000020000.jpg"},
    ]
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
    # 已抓齐（有图的 + 墓碑）：不懒触发
    assert view.chapters_pending is False and scheduled == []

    # 没抓齐（这里是 chapter_images=NULL）：详情页懒触发一次并告知前端轮询
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        row.chapter_images = None
        await session.commit()
        view = (await get_library_item(lib_id, item_id, _ADMIN, session)).data
    assert view.chapters_pending is True and scheduled == [item_id]

    # 半成品（第 0 章既没图也没墓碑）同样懒触发——只看"抓过没有"会永远漏掉它
    scheduled.clear()
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        row.chapter_images = images[1:]
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
        # 半成品：600s 该合成 6 张，台账里只有一张且图还不在磁盘上
        partial = LibraryFile(
            library_id=lib_id,
            media_item_id=item_id,
            file_path=str(video.parent / "e.mkv"),
            container="mkv",
            duration_seconds=600,
            chapters=[],
            chapter_images=[{"start_ms": 36000, "frame_ms": 36000, "image": "x.jpg"}],
            source=FileSource.SCANNED,
        )
        extra.append(partial)
        session.add_all(extra)
        await session.commit()
        pending = await chapters_mod._job_targets(session, lib_id, force=False)
        everything = await chapters_mod._job_targets(session, lib_id, force=True)
        b_id = (
            await session.execute(
                select(LibraryFile.id).where(LibraryFile.file_path.endswith("b.mkv"))
            )
        ).scalar_one()
    # 抓齐的、原盘、strm、未识别的都不在；没抓过的与半成品都要处理
    assert set(pending) == {b_id, partial.id}
    assert set(everything) == {done_id, b_id, partial.id}


def _bare_row(tmp_path, *, chapters, chapter_images, duration=600) -> LibraryFile:
    """只给 stills_complete 读属性用的台账行，不进数据库。"""
    return LibraryFile(
        id=1,
        library_id=1,
        media_item_id=1,
        file_path=str(tmp_path / "a.mkv"),
        container="mkv",
        duration_seconds=duration,
        chapters=chapters,
        chapter_images=chapter_images,
        source=FileSource.SCANNED,
    )


def test_stills_complete_only_counts_finished_files(tmp_path):
    """补缺的判据是"抓齐没有"而不是"抓过没有"（设计文档 §4.5）：半成品、图丢了、
    该有图却写成 [] 的行都要接着抓；墓碑与整体不抓图的才算齐。"""
    assets = tmp_path / "assets"
    embedded = [
        {"start_ms": 0, "end_ms": 20000, "title": "Opening"},
        {"start_ms": 20000, "end_ms": None, "title": None},
    ]

    def _image(start: int) -> dict:
        rel = chapters_mod.image_rel_path(1, 1, start)
        path = assets / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        return {"start_ms": start, "frame_ms": start + 2000, "image": rel}

    first, second = _image(0), _image(20000)
    complete = _bare_row(tmp_path, chapters=embedded, chapter_images=[first, second])
    assert chapters_mod.stills_complete(complete, assets)
    # 上一轮预算耗尽写回的半成品：缺的那一章要接着抓
    partial = _bare_row(tmp_path, chapters=embedded, chapter_images=[second])
    assert not chapters_mod.stills_complete(partial, assets)
    # 抓不出来的章节留了墓碑：算齐，别每轮对着它重跑
    tombstone = _bare_row(
        tmp_path, chapters=embedded, chapter_images=[{"start_ms": 0, "failed": True}, second]
    )
    assert chapters_mod.stills_complete(tombstone, assets)
    # 章节或图没探过
    assert not chapters_mod.stills_complete(
        _bare_row(tmp_path, chapters=None, chapter_images=[first, second]), assets
    )
    assert not chapters_mod.stills_complete(
        _bare_row(tmp_path, chapters=embedded, chapter_images=None), assets
    )
    # [] 的两面：该有图（600s 合成 6 张）却是空的要重来；60s 短片合成 0 张算齐
    assert not chapters_mod.stills_complete(
        _bare_row(tmp_path, chapters=[], chapter_images=[], duration=600), assets
    )
    assert chapters_mod.stills_complete(
        _bare_row(tmp_path, chapters=[], chapter_images=[], duration=60), assets
    )
    # 台账有记录、图文件没了（清过资产目录、只恢复了数据库备份）：重抓
    (assets / str(first["image"])).unlink()
    assert not chapters_mod.stills_complete(complete, assets)


async def test_partial_and_failed_stills_resume_next_round(db, tmp_path, monkeypatch):
    """半成品下一轮接着抓（已有的图原样复用），图丢了会重抓，抓不出来的记墓碑
    不再重试，force 时墓碑也重试。"""
    video = tmp_path / "media" / "p.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    embedded = [
        {"start_ms": 0, "end_ms": 20000, "title": "Opening"},
        {"start_ms": 20000, "end_ms": None, "title": None},
    ]
    _lib_id, item_id, file_id = await _seed(db, video, chapters=embedded)
    assets = Path(get_settings().metadata_dir) / "images"
    rel_first = chapters_mod.image_rel_path(item_id, file_id, 0)
    seeded = {
        "start_ms": 20000,
        "frame_ms": 22000,
        "image": chapters_mod.image_rel_path(item_id, file_id, 20000),
    }
    (assets / seeded["image"]).parent.mkdir(parents=True, exist_ok=True)
    (assets / seeded["image"]).write_bytes(b"jpg")
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        row.chapter_images = [seeded]  # 上一轮只抓到第 2 章
        await session.commit()

    seeks: list[float] = []
    state = {"fail": False}

    def _grab(_video, dest, *, seek_seconds, color):
        seeks.append(seek_seconds)
        if state["fail"]:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpg")
        return int(seek_seconds * 1000) + 500

    monkeypatch.setattr(chapters_mod, "video_color_for", lambda *_a, **_k: None)
    monkeypatch.setattr(chapters_mod, "grab_chapter_still", _grab)

    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert await chapters_mod.refresh_file_chapter_images(session, row)
        assert seeks == [15.0]  # 只补缺的那一章（第 0 章从 15s 抓）
        assert [e["start_ms"] for e in row.chapter_images] == [0, 20000]
        assert row.chapter_images[1] == seeded  # 已有的图原样复用

        # 抓齐了：再来一次是 no-op
        seeks.clear()
        assert not await chapters_mod.refresh_file_chapter_images(session, row)
        assert seeks == []

        # 图文件没了：补缺重抓那一张
        (assets / rel_first).unlink()
        assert await chapters_mod.refresh_file_chapter_images(session, row)
        assert seeks == [15.0]

        # 这一章 ffmpeg 抓不出来：记墓碑，下一轮不再对着它重跑
        (assets / rel_first).unlink()
        state["fail"] = True
        seeks.clear()
        assert await chapters_mod.refresh_file_chapter_images(session, row)
        assert row.chapter_images[0] == {"start_ms": 0, "failed": True}
        seeks.clear()
        assert not await chapters_mod.refresh_file_chapter_images(session, row)
        assert seeks == []

        # 同一个文件因为别的章节缺图再进来时，墓碑那一章也不重试
        state["fail"] = False
        (assets / seeded["image"]).unlink()
        seeks.clear()
        assert await chapters_mod.refresh_file_chapter_images(session, row)
        assert seeks == [20.0]
        assert row.chapter_images[0] == {"start_ms": 0, "failed": True}

        # force 不认墓碑，也不认已有的图
        seeks.clear()
        assert await chapters_mod.refresh_file_chapter_images(session, row, force=True)
        assert seeks == [15.0, 20.0]
        assert all("image" in e for e in row.chapter_images)


async def test_file_vanishing_midway_leaves_no_tombstone(db, tmp_path, monkeypatch):
    """文件在抓图过程中被搬走/删掉（整理改名、条目转移、洗版）：不记墓碑。

    墓碑在补缺模式下不再重试，为一次路径变更误记一条，等于这一行的图永远
    缺着。正确的收场是把已抓到的落库、这一行仍然算"没抓齐"，下一轮再来。
    """
    video = tmp_path / "media" / "v.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    embedded = [
        {"start_ms": 0, "end_ms": 20000, "title": None},
        {"start_ms": 20000, "end_ms": None, "title": None},
    ]
    _lib_id, _item_id, file_id = await _seed(db, video, chapters=embedded)
    assets = Path(get_settings().metadata_dir) / "images"

    seeks: list[float] = []

    def _grab(_video, dest, *, seek_seconds, color):
        seeks.append(seek_seconds)
        video.unlink(missing_ok=True)  # 抓第一张时文件被整理搬走
        return None

    monkeypatch.setattr(chapters_mod, "video_color_for", lambda *_a, **_k: None)
    monkeypatch.setattr(chapters_mod, "grab_chapter_still", _grab)

    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert await chapters_mod.refresh_file_chapter_images(session, row)
        assert seeks == [15.0]  # 第一章失败即收尾，不再对着已经不在的文件跑第二章
        assert row.chapter_images == []
        assert not chapters_mod.stills_complete(row, assets)  # 下一轮还会重来

        # 文件回到原位（整理结束）：照常抓，墓碑没留下过
        video.write_bytes(b"x")
        monkeypatch.setattr(
            chapters_mod,
            "grab_chapter_still",
            lambda _v, dest, *, seek_seconds, color: (
                dest.parent.mkdir(parents=True, exist_ok=True),
                dest.write_bytes(b"jpg"),
                int(seek_seconds * 1000),
            )[2],
        )
        assert await chapters_mod.refresh_file_chapter_images(session, row)
        assert [e["start_ms"] for e in row.chapter_images] == [0, 20000]
        assert all("image" in e for e in row.chapter_images)


async def test_library_orphan_chapter_dirs_are_swept(db, tmp_path):
    """整库作业收尾清孤儿目录：台账行被清出（自动清理丢失记录、手动清理、
    洗版换行）后留下的 ``{item}/chapters/{file_id}/`` 没有任何单条目入口会
    去清它——条目已经抓齐时它连作业目标都不是。"""
    video = tmp_path / "media" / "o.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    library_id, item_id, file_id = await _seed(db, video, chapters=[], chapter_images=[])
    assets = Path(get_settings().metadata_dir) / "images"
    live_dir = assets / str(item_id) / "chapters" / str(file_id)
    stale_dir = assets / str(item_id) / "chapters" / str(file_id + 9)
    for directory in (live_dir, stale_dir):
        directory.mkdir(parents=True)
        (directory / "0000000000.jpg").write_bytes(b"jpg")

    async with db.session() as session:
        assert await chapters_mod.cleanup_library_orphan_dirs(session, library_id) == 1
    assert live_dir.is_dir()
    assert not stale_dir.exists()


async def test_chapter_job_does_not_hold_the_library_lease(db, tmp_path):
    """章节作业挂 context 关系、不占库租约：它排队/运行期间，监听与定时对账
    触发的扫描照常跑。

    首轮回填可能跑几个小时，让路就等于这几个小时里新落盘的文件都不入账。
    对照组：真正会动文件与台账的作业（target 关系）仍然让扫描顺延。
    """
    from movieclaw_api.services.library.scan import scan_library

    root = tmp_path / "empty-root"
    root.mkdir()
    async with db.session() as session:
        lib = Library(name="空库", kind="video", source="local", root_paths=[str(root)])
        session.add(lib)
        await session.commit()
        library_id = lib.id
        await chapters_mod.enqueue_library_chapter_images_job(session, library_id, lib.name)

    summary = await scan_library(library_id, backfill_existing_specs=False)
    assert summary.errors == []

    async with db.session() as session:
        await jobs.create_job(
            session,
            job_type="library.organize",
            input_data={"library_id": library_id},
            resources=[jobs.ResourceRef("library", library_id)],
        )
    summary = await scan_library(library_id, backfill_existing_specs=False)
    assert any("后台作业" in message for message in summary.errors)


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

    assets = Path(get_settings().metadata_dir) / "images"

    async def fake_refresh(session, row, *, force=False):
        """抓图的替身：跳过条件与落库形态都照真的来——补缺的断点就是"抓齐没有"，
        只写半套图（或者图不落盘）会被判成半成品、下一轮又重来。"""
        if not force and chapters_mod.stills_complete(row, assets):
            return False
        calls.append(row.id)
        if len(calls) > stop_after:
            raise asyncio.CancelledError("模拟重启")  # 停机会直接取消执行协程
        planned, _ = chapters_mod.still_plan(
            chapters_mod.effective_chapters(row.chapters, row.duration_seconds),
            row.duration_seconds,
        )
        images = []
        for chapter in planned:
            rel = chapters_mod.image_rel_path(row.media_item_id, row.id, chapter.start_ms)
            (assets / rel).parent.mkdir(parents=True, exist_ok=True)
            (assets / rel).write_bytes(b"jpg")
            images.append(
                {"start_ms": chapter.start_ms, "frame_ms": chapter.start_ms, "image": rel}
            )
        row.chapter_images = images
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
