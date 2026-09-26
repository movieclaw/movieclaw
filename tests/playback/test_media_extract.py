"""内封轨抽取的共享底座（issue #432）。

抽取要通读整个容器，16 GB 的 MKV 是 80 秒级——这条路径上「多起一个 ffmpeg」
不是浪费一点 CPU，而是把最贵的一步整个做第二遍。本文件守的就是这件事：
同一条轨并发只跑一个进程，预检不在请求里等，产物播放器与生成端共用。

真抽取的正确性（ASS 保样式、文本转 SRT、PGS 转 sup）在
``tests/playback/test_embedded_subs.py`` 里，标 integration。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import movieclaw_api.services.media_extract as media_extract
from movieclaw_db.models import FileSource, FileState, LibraryFile


def make_file(video: Path, codec: str = "subrip") -> LibraryFile:
    return LibraryFile(
        id=7,
        library_id=1,
        media_item_id=1,
        file_path=str(video),
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        container="mkv",
        subtitle_streams=[{"codec": codec}],
    )


@pytest.fixture
def video(tmp_path: Path, monkeypatch) -> Path:
    """一个假视频 + 独立的抽取缓存目录。"""
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"source")
    monkeypatch.setattr(media_extract, "cache_dir", lambda: tmp_path / "cache")
    return path


def _fresh_product(video: Path, cache: Path, name: str, text: str = "x") -> Path:
    import os

    cache.mkdir(parents=True, exist_ok=True)
    product = cache / name
    product.write_text(text, encoding="utf-8")
    os.utime(product, ns=(video.stat().st_mtime_ns + 10**9,) * 2)
    return product


# ---------------------------------------------------------------------------
# 缓存查询：预检据此判断「这次能不能立刻给出结论」
# ---------------------------------------------------------------------------


def test_cached_track_never_starts_ffmpeg(tmp_path: Path, video: Path, monkeypatch) -> None:
    def never(*_a, **_k):  # pragma: no cover
        raise AssertionError("只查缓存的调用起了 ffmpeg")

    monkeypatch.setattr(media_extract.subprocess, "Popen", never)
    assert media_extract.cached_track(make_file(video), 0) is None

    _fresh_product(video, tmp_path / "cache", "7.s0.srt")
    track = media_extract.cached_track(make_file(video), 0)
    assert track is not None and track.format == "srt"


def test_cache_goes_stale_when_the_video_is_replaced(
    tmp_path: Path, video: Path
) -> None:
    """洗版换片后必须重抽——否则新片配旧字幕，时间轴整体对不上。"""
    import os

    product = _fresh_product(video, tmp_path / "cache", "7.s0.srt")
    assert media_extract.cached_track(make_file(video), 0) is not None

    os.utime(video, ns=(product.stat().st_mtime_ns + 10**9,) * 2)
    assert media_extract.cached_track(make_file(video), 0) is None


def test_unsupported_codec_has_no_cache_identity(video: Path) -> None:
    assert media_extract.cached_track(make_file(video, "dvd_subtitle"), 0) is None


# ---------------------------------------------------------------------------
# 单飞：issue #432 的第二条
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_ffmpeg(video: Path, monkeypatch) -> None:
    """用户等待时拨一下「双语」开关就再发一次预检，不能因此起第二个 ffmpeg。

    日志里那两条 80 秒的请求就是这么来的：前端丢掉了旧响应，后端的活一点
    没省，同一个 16 GB 的文件被同时通读了两遍。
    """
    starts = 0
    release = asyncio.Event()

    class SlowProcess:
        pid = 4321
        returncode: int | None = None

        async def communicate(self):
            await release.wait()
            self.returncode = 0
            return b"", b""

    async def fake_exec(*argv, **_kwargs):
        nonlocal starts
        starts += 1
        # 产物由「ffmpeg」写出：最后一个参数是临时文件路径
        Path(argv[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n\n", encoding="utf-8")
        return SlowProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    file = make_file(video)
    waiters = [
        asyncio.create_task(media_extract.extract_track_async(file, 0)) for _ in range(4)
    ]
    await asyncio.sleep(0)  # 让所有等待者都登记到同一个共享任务上
    release.set()
    results = await asyncio.gather(*waiters)

    assert starts == 1, f"同一条轨起了 {starts} 个 ffmpeg"
    assert all(r is not None and r.format == "srt" for r in results)


@pytest.mark.asyncio
async def test_one_caller_leaving_does_not_kill_the_others(video: Path, monkeypatch) -> None:
    """详情页预热与播放器请求会撞在同一条轨上，谁先走都不能把对方的活取消。"""
    release = asyncio.Event()

    class SlowProcess:
        pid = 4321
        returncode: int | None = None

        async def communicate(self):
            await release.wait()
            self.returncode = 0
            return b"", b""

    async def fake_exec(*argv, **_kwargs):
        Path(argv[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n\n", encoding="utf-8")
        return SlowProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    file = make_file(video)
    leaving = asyncio.create_task(media_extract.extract_track_async(file, 0))
    staying = asyncio.create_task(media_extract.extract_track_async(file, 0))
    await asyncio.sleep(0)

    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    release.set()
    assert (await staying) is not None, "一个调用方离开就把还在等的那个也弄没了"


# ---------------------------------------------------------------------------
# 后台调度：预检不在 HTTP 请求里等 ffmpeg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduling_is_idempotent_and_finishes_after_the_caller_leaves(
    video: Path, monkeypatch
) -> None:
    """轮询期间反复调度只跑一个进程；用户关掉对话框，抽取仍要做完落缓存。

    那趟昂贵的通读只值得做一次——半路丢掉等于下次从零再来。
    """
    starts = 0
    release = asyncio.Event()

    class SlowProcess:
        pid = 99
        returncode: int | None = None

        async def communicate(self):
            await release.wait()
            self.returncode = 0
            return b"", b""

    async def fake_exec(*argv, **_kwargs):
        nonlocal starts
        starts += 1
        Path(argv[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n\n", encoding="utf-8")
        return SlowProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    file = make_file(video)
    # 前端轮询三次 = 三次调度，只应该有一个 ffmpeg
    assert media_extract.schedule_extraction(file, 0) is True
    await asyncio.sleep(0)
    assert media_extract.schedule_extraction(file, 0) is True
    assert media_extract.schedule_extraction(file, 0) is True

    # 调用方全部离开（用户关掉对话框），后台任务继续把产物抽完
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if media_extract.cached_track(file, 0) is not None:
            break

    assert starts == 1, f"轮询把同一条轨抽了 {starts} 遍"
    assert media_extract.cached_track(file, 0) is not None, "后台抽取没有落成缓存"
    # 已有缓存后不再调度：轮询拿到的就是最终结论
    assert media_extract.schedule_extraction(file, 0) is False


@pytest.mark.asyncio
async def test_a_failed_track_is_not_retried_on_every_poll(video: Path, monkeypatch) -> None:
    """一条读不出来的轨，不能让前端轮询每隔两三秒催起一个新的 ffmpeg。

    没有这道闸门，打开对话框放着不管就是一个持续吃满 CPU 的循环。
    """
    starts = 0

    class FailingProcess:
        pid = 1
        returncode: int | None = None

        async def communicate(self):
            self.returncode = 1
            return b"", b"stream not found"

    async def fake_exec(*_argv, **_kwargs):
        nonlocal starts
        starts += 1
        return FailingProcess()  # 不写产物 = 抽取失败

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    file = make_file(video)
    assert media_extract.schedule_extraction(file, 0) is True
    for _ in range(10):
        await asyncio.sleep(0)
        if media_extract.extraction_failed(file, 0):
            break

    assert media_extract.extraction_failed(file, 0) is True
    # 后续轮询一律被拒，ffmpeg 不再起第二次
    assert media_extract.schedule_extraction(file, 0) is False
    assert media_extract.schedule_extraction(file, 0) is False
    for _ in range(5):
        await asyncio.sleep(0)
    assert starts == 1, f"失败的轨被重抽了 {starts} 次"


@pytest.mark.asyncio
async def test_replacing_the_video_clears_the_failure_verdict(
    video: Path, monkeypatch
) -> None:
    """洗版换片之后值得再试一次——旧片抽不出来不代表新片也不行。"""
    import os

    file = make_file(video)
    spec = media_extract._extraction_spec(file, 0)
    assert spec is not None
    media_extract._FAILED_EXTRACTIONS[media_extract._job_key(spec, 0)] = (
        video.stat().st_mtime_ns
    )
    assert media_extract.extraction_failed(file, 0) is True

    os.utime(video, ns=(video.stat().st_mtime_ns + 10**9,) * 2)
    assert media_extract.extraction_failed(file, 0) is False


@pytest.mark.asyncio
async def test_scheduling_an_unsupported_track_is_refused(video: Path) -> None:
    assert media_extract.schedule_extraction(make_file(video, "dvd_subtitle"), 0) is False


def test_scheduling_without_an_event_loop_is_refused(video: Path) -> None:
    """同步上下文（扫描脚本、CLI）没有事件循环可挂后台任务，如实返回 False。"""
    assert media_extract.schedule_extraction(make_file(video), 0) is False


# ---------------------------------------------------------------------------
# 一个文件只通读一遍、全局串行、超时按体积、失败记住（2026-09 NAS 白读几百 GB）
# ---------------------------------------------------------------------------

_SRT = "1\n00:00:01,000 --> 00:00:02,000\nhi\n\n"


def make_multi_file(video: Path, codecs: list[str], file_id: int = 7) -> LibraryFile:
    file = make_file(video)
    file.id = file_id
    file.subtitle_streams = [{"codec": c} for c in codecs]
    return file


def _outputs(argv) -> list[str]:
    """假 ffmpeg 要写出的产物：多路输出时每条轨一个 .part 临时文件。"""
    return [a for a in argv if ".part." in str(a)]


class _DoneProcess:
    pid = 1

    def __init__(self, returncode: int = 0) -> None:
        self.returncode: int | None = None
        self._rc = returncode

    async def communicate(self):
        self.returncode = self._rc
        return b"", b"" if self._rc == 0 else b"boom"


@pytest.mark.asyncio
async def test_one_ffmpeg_extracts_every_track_of_the_file(video: Path, monkeypatch) -> None:
    """一部带三条字幕的 Remux，逐轨抽等于从头读三遍；一趟多路输出全部落缓存。"""
    calls = []

    async def fake_exec(*argv, **_kwargs):
        calls.append(argv)
        for out in _outputs(argv):
            Path(out).write_text(_SRT, encoding="utf-8")
        return _DoneProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    file = make_multi_file(video, ["subrip", "ass", "hdmv_pgs_subtitle", "dvd_subtitle"])
    track = await media_extract.extract_track_async(file, 1)

    assert track is not None and track.format == "ass"
    assert len(calls) == 1, f"一个文件起了 {len(calls)} 个 ffmpeg"
    assert calls[0].count("-i") == 1 and calls[0].count("-map") == 3  # VobSub 不支持，不抽
    for index, fmt in ((0, "srt"), (2, "sup")):
        cached = media_extract.cached_track(file, index)
        assert cached is not None and cached.format == fmt


@pytest.mark.asyncio
async def test_different_tracks_of_one_file_share_one_ffmpeg(video: Path, monkeypatch) -> None:
    """播放器要轨 0、AI 生成要轨 1，同时到达也只通读一遍。"""
    starts = 0
    release = asyncio.Event()

    class SlowProcess(_DoneProcess):
        async def communicate(self):
            await release.wait()
            return await super().communicate()

    async def fake_exec(*argv, **_kwargs):
        nonlocal starts
        starts += 1
        for out in _outputs(argv):
            Path(out).write_text(_SRT, encoding="utf-8")
        return SlowProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    file = make_multi_file(video, ["subrip", "subrip"])
    first = asyncio.create_task(media_extract.extract_track_async(file, 0))
    second = asyncio.create_task(media_extract.extract_track_async(file, 1))
    await asyncio.sleep(0)
    release.set()

    assert all(r is not None for r in await asyncio.gather(first, second))
    assert starts == 1, f"同一个文件被通读了 {starts} 遍"


@pytest.mark.asyncio
async def test_a_timed_out_file_is_not_read_again(video: Path, monkeypatch) -> None:
    """超时白读一遍已经够亏；不记住的话每次打开播放器都再白读一遍。"""
    starts = 0

    class HangingProcess(_DoneProcess):
        async def communicate(self):
            await asyncio.Event().wait()

    async def fake_exec(*_argv, **_kwargs):
        nonlocal starts
        starts += 1
        return HangingProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(media_extract, "_extract_timeout", lambda _v: 0.01)
    monkeypatch.setattr(media_extract, "_PROCESS_TERM_TIMEOUT", 0.01)
    monkeypatch.setattr(media_extract, "_PROCESS_KILL_TIMEOUT", 0.01)

    file = make_multi_file(video, ["subrip", "ass"])
    assert await media_extract.extract_track_async(file, 0) is None
    assert media_extract.extraction_failed(file, 0) and media_extract.extraction_failed(file, 1)

    assert await media_extract.extract_track_async(file, 1) is None
    assert starts == 1, f"超时过的文件又被读了 {starts - 1} 遍"


@pytest.mark.asyncio
async def test_whole_file_reads_are_serialized_across_files(tmp_path: Path, monkeypatch) -> None:
    """几个 ffmpeg 并发读 NFS 只会平分带宽、一起撞超时；同一时刻只放行一个。"""
    monkeypatch.setattr(media_extract, "cache_dir", lambda: tmp_path / "cache")
    running = 0
    peak = 0

    class CountingProcess(_DoneProcess):
        async def communicate(self):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return await super().communicate()

    async def fake_exec(*argv, **_kwargs):
        for out in _outputs(argv):
            Path(out).write_text(_SRT, encoding="utf-8")
        return CountingProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    files = []
    for i in range(4):
        path = tmp_path / f"movie{i}.mkv"
        path.write_bytes(b"source")
        files.append(make_multi_file(path, ["subrip"], file_id=100 + i))

    results = await asyncio.gather(
        *(media_extract.extract_track_async(f, 0) for f in files)
    )
    assert all(r is not None for r in results)
    assert peak == 1, f"同时有 {peak} 个整文件通读在跑"


def test_timeout_scales_with_file_size(tmp_path: Path) -> None:
    """固定 120 秒读不完 60 GB 的 Remux；按保守吞吐放宽，封顶一小时。"""
    small = tmp_path / "small.mkv"
    small.write_bytes(b"x")
    assert media_extract._extract_timeout(small) == media_extract.EXTRACT_TIMEOUT

    remux = tmp_path / "remux.mkv"
    with remux.open("wb") as fh:
        fh.truncate(20 * 1024**3)  # 稀疏文件，不占真实磁盘
    assert media_extract._extract_timeout(remux) == pytest.approx(1024.0)

    huge = tmp_path / "huge.mkv"
    with huge.open("wb") as fh:
        fh.truncate(200 * 1024**3)
    assert media_extract._extract_timeout(huge) == media_extract._MAX_EXTRACT_TIMEOUT


@pytest.mark.asyncio
async def test_a_bad_track_does_not_sink_the_good_ones(video: Path, monkeypatch) -> None:
    """多轨一趟 ffmpeg 报错时退回逐轨：好轨照常出产物，只有坏轨记失败。"""
    calls = []

    async def fake_exec(*argv, **_kwargs):
        calls.append(argv)
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        if len(maps) > 1 or maps == ["0:s:1"]:
            return _DoneProcess(returncode=1)  # 坏轨 1 连累整趟
        for out in _outputs(argv):
            Path(out).write_text(_SRT, encoding="utf-8")
        return _DoneProcess()

    monkeypatch.setattr(media_extract.shutil, "which", lambda _n: "/fake/ffmpeg")
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", fake_exec)

    file = make_multi_file(video, ["subrip", "subrip"])
    assert await media_extract.extract_track_async(file, 0) is not None
    assert len(calls) == 3  # 一趟多路失败 + 逐轨两趟
    assert media_extract.extraction_failed(file, 1) is True
    assert media_extract.extraction_failed(file, 0) is False
    assert not list((video.parent / "cache").glob("*.part*")), "失败留下了半成品"
