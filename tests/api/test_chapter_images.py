"""章节场景图（docs/design/video-chapters.md）：抓图、懒触发资格、详情接口投影、
整库作业的目标筛选。抓图部分需要真实 ffmpeg（lavfi 合成带章节的测试片），
没装则跳过；其余不依赖 ffmpeg。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.api.routes.libraries import get_library_item
from movieclaw_api.core.config import get_settings
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


async def test_item_regenerate_route_schedules_force(db, tmp_path, monkeypatch):
    """条目菜单「重新生成场景图」：独立于刷新元数据，后台 force 重抓；库关了开关拒绝。"""
    from movieclaw_api.api.routes.libraries import regenerate_item_chapter_images
    from movieclaw_api.exceptions import ConflictException

    video = tmp_path / "media" / "r.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    lib_id, item_id, _file_id = await _seed(db, video, chapters=[], chapter_images=[])
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        chapters_mod,
        "schedule_item_chapter_images",
        lambda i, *, force=False: calls.append((i, force)) or True,
    )
    async with db.session() as session:
        resp = await regenerate_item_chapter_images(lib_id, item_id, session)
        assert resp.data == {"started": True} and calls == [(item_id, True)]

        lib = await session.get(Library, lib_id)
        lib.extract_chapter_images = False
        await session.commit()
        with pytest.raises(ConflictException):
            await regenerate_item_chapter_images(lib_id, item_id, session)
