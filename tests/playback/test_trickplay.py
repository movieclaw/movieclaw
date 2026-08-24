"""进度条缩略图测试（docs/design/web-player.md §6.5）。

索引计算是纯函数，重点测它——算错一格，预览图就会错位一条边或指到不存在的
雪碧图上。真生成标 ``integration``。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from movieclaw_api.services.playback import trickplay
from movieclaw_db.models import FileSource, FileState, LibraryFile


def make_file(path: Path, *, duration: int | None = 120, file_id: int = 1) -> LibraryFile:
    return LibraryFile(
        id=file_id,
        library_id=1,
        media_item_id=1,
        file_path=str(path),
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        container="mp4",
        duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# 索引计算
# ---------------------------------------------------------------------------


def test_tile_size_is_derived_from_the_actual_sheet():
    """格子尺寸从雪碧图整体尺寸反推，不能照抄常量——`scale=320:-2` 的高度
    取决于源片宽高比且向偶数对齐，差一两个像素预览图就错位一条边。"""
    index = trickplay.build_index(
        duration_seconds=120, sheet_width=3200, sheet_height=1800, sheets=["sprite_001.jpg"]
    )
    assert index.tile_width == 320
    assert index.tile_height == 180


def test_tile_size_follows_a_different_aspect_ratio():
    index = trickplay.build_index(
        duration_seconds=120, sheet_width=3200, sheet_height=2400, sheets=["sprite_001.jpg"]
    )
    assert (index.tile_width, index.tile_height) == (320, 240)


@pytest.mark.parametrize(
    "duration,expected",
    [
        (120, 12),   # 恰好 12 个 10 秒
        (125, 13),   # 不足一格也要算一格，否则最后几秒没预览
        (10, 1),
        (1, 1),      # 极短片至少一格
    ],
)
def test_thumbnail_count_covers_the_whole_file(duration, expected):
    index = trickplay.build_index(
        duration_seconds=duration, sheet_width=3200, sheet_height=1800,
        sheets=["sprite_001.jpg"],
    )
    assert index.count == expected


def test_count_never_exceeds_the_sheets_capacity():
    """索引说有 500 格而只生成了一张图，会让后半部片的预览指向不存在的雪碧图。"""
    index = trickplay.build_index(
        duration_seconds=5000, sheet_width=3200, sheet_height=1800,
        sheets=["sprite_001.jpg"],
    )
    assert index.count == trickplay.TILES_PER_SHEET


def test_multiple_sheets_raise_the_ceiling():
    # 1500 秒 = 150 格，两张图（200 格容量）放得下，不该被夹
    index = trickplay.build_index(
        duration_seconds=1500, sheet_width=3200, sheet_height=1800,
        sheets=["sprite_001.jpg", "sprite_002.jpg"],
    )
    assert index.count == 150


def test_interval_is_reported_in_milliseconds():
    index = trickplay.build_index(
        duration_seconds=120, sheet_width=3200, sheet_height=1800, sheets=["s.jpg"]
    )
    assert index.interval_ms == trickplay.INTERVAL_S * 1000


# ---------------------------------------------------------------------------
# 生成的前置条件
# ---------------------------------------------------------------------------


def test_missing_index_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert trickplay.load_index(999) is None


def test_corrupt_index_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = trickplay.trickplay_dir(1)
    directory.mkdir(parents=True)
    (directory / "index.json").write_text("{ 坏掉的 json", encoding="utf-8")
    assert trickplay.load_index(1) is None


def test_generation_needs_a_duration(tmp_path, monkeypatch):
    """没有片长就算不出格子数——宁可没有预览，也不要一份错的索引。"""
    monkeypatch.chdir(tmp_path)
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    assert trickplay.generate(make_file(video, duration=None)) is None


def test_generation_without_ffmpeg_is_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trickplay.shutil, "which", lambda _n: None)
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    assert trickplay.generate(make_file(video)) is None


def test_missing_video_is_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert trickplay.generate(make_file(tmp_path / "gone.mp4")) is None


def test_schedule_is_deduplicated(tmp_path, monkeypatch):
    """同一部片被并发触发只该生成一次——否则几路 ffmpeg 同时读同一个大文件。"""
    monkeypatch.chdir(tmp_path)
    trickplay._in_flight.clear()
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    file = make_file(video)
    # 没有事件循环时 schedule 静默跳过，这里直接验去重集合的行为
    trickplay._in_flight.add(file.id)
    calls = []
    monkeypatch.setattr(trickplay, "generate", lambda f: calls.append(f))
    trickplay.schedule(file)
    assert calls == []
    trickplay._in_flight.clear()


# ---------------------------------------------------------------------------
# 真生成
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg")
def test_generates_sheets_and_index(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=60",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-y", str(video),
        ],
        capture_output=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-800:]

    monkeypatch.chdir(tmp_path)
    file = make_file(video, duration=60)
    index = trickplay.generate(file)
    assert index is not None
    assert index.sheets and index.count == 6
    assert index.tile_width == 320
    sheet = trickplay.trickplay_dir(file.id) / index.sheets[0]
    assert sheet.is_file() and sheet.stat().st_size > 0
    assert sheet.read_bytes()[:2] == b"\xff\xd8"  # JPEG 魔数

    # 二次调用直接读索引，不重跑
    calls = []
    monkeypatch.setattr(trickplay.subprocess, "run", lambda *a, **k: calls.append(a))
    again = trickplay.generate(file)
    assert again is not None and calls == []


# ---------------------------------------------------------------------------
# 生成命令的 IO 约束（§6.10「首次起播卡缓冲」的头号元凶）
# ---------------------------------------------------------------------------


def test_generate_command_is_readrate_limited(tmp_path, monkeypatch):
    """skip_frame 只省解码不省 IO——不限速的话 demuxer 会以极限速度通读整个
    容器，跟首片转码抢盘。这里锁死 -readrate 必须在命令里且在 -i 之前。"""
    import subprocess as sp

    from movieclaw_db.models import FileSource, FileState, LibraryFile

    video = tmp_path / "m.mkv"
    video.write_bytes(b"x")
    file = LibraryFile(
        id=7, library_id=1, media_item_id=1, file_path=str(video), size_bytes=1,
        source=FileSource.SCANNED, state=FileState.IN_PLACE, duration_seconds=600,
    )
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(argv)
        raise sp.TimeoutExpired(argv, 1)  # 立刻退出，不真跑 ffmpeg

    monkeypatch.setattr(trickplay.subprocess, "run", fake_run)
    monkeypatch.setattr(trickplay.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(trickplay, "trickplay_dir", lambda fid: tmp_path / "tp")
    monkeypatch.setattr(trickplay, "load_index", lambda fid: None)
    assert trickplay.generate(file) is None
    argv = captured[0]
    assert "-readrate" in argv
    assert argv.index("-readrate") < argv.index("-i")
