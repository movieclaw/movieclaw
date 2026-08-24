"""起播预热的挑选与去重逻辑（services/playback/warmup.py）。

预热本体是「调两个已有缓存的探测函数」，这里测的是**什么时候调、调哪条轨**
——挑错轨或对整季剧集开闸，预热就从省 IO 变成烧 IO。
"""

from __future__ import annotations

import asyncio

import pytest

from movieclaw_api.services.playback import warmup
from movieclaw_db.models import FileSource, FileState, LibraryFile


def make_file(tmp_path, streams, *, file_id=1) -> LibraryFile:
    path = tmp_path / f"movie{file_id}.mkv"
    path.write_bytes(b"x")
    return LibraryFile(
        id=file_id,
        library_id=1,
        media_item_id=1,
        file_path=str(path),
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        duration_seconds=600,
        subtitle_streams=streams,
    )


# ---------------------------------------------------------------------------
# 轨挑选：默认轨优先，其次第一条支持的轨；全不支持则不抽
# ---------------------------------------------------------------------------


def test_default_track_wins(tmp_path):
    file = make_file(tmp_path, [
        {"codec": "subrip"},
        {"codec": "ass", "default": True},
    ])
    assert warmup._pick_subtitle_index(file) == 1


def test_first_supported_track_when_no_default(tmp_path):
    """VobSub 网页端不支持，跳过它选后面第一条能用的。"""
    file = make_file(tmp_path, [
        {"codec": "dvd_subtitle"},
        {"codec": "hdmv_pgs_subtitle"},
        {"codec": "subrip"},
    ])
    assert warmup._pick_subtitle_index(file) == 1


def test_no_supported_tracks_means_no_extraction(tmp_path):
    file = make_file(tmp_path, [{"codec": "dvd_subtitle"}, "不是字典"])
    assert warmup._pick_subtitle_index(file) is None


def test_unprobed_file_has_nothing_to_pick(tmp_path):
    assert warmup._pick_subtitle_index(make_file(tmp_path, None)) is None


# ---------------------------------------------------------------------------
# schedule：剧集不开闸、并发去重
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_inflight():
    warmup._in_flight.clear()
    yield
    warmup._in_flight.clear()


def test_series_with_many_files_is_skipped(tmp_path, monkeypatch):
    """整季剧集详情页猜不到用户要播哪集，全预热是几 GB 的无谓 IO。"""
    called = []
    monkeypatch.setattr(warmup, "_warm_file", lambda f: called.append(f.id))
    files = [make_file(tmp_path, [], file_id=i) for i in range(1, 7)]

    async def run():
        warmup.schedule(1, files)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert called == []
    assert 1 not in warmup._in_flight


def test_movie_files_are_warmed_in_background(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(warmup, "_warm_file", lambda f: called.append(f.id))
    files = [make_file(tmp_path, [], file_id=1), make_file(tmp_path, [], file_id=2)]

    async def run():
        warmup.schedule(1, files)
        # 后台任务要真正跑完：to_thread 需要让出事件循环几拍
        for _ in range(10):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert called == [1, 2]
    assert 1 not in warmup._in_flight  # 结束后放行下一次


def test_concurrent_schedule_runs_once(tmp_path, monkeypatch):
    """详情页反复刷新不能叠加探测 IO。"""
    called = []

    def slow_warm(f):
        called.append(f.id)

    monkeypatch.setattr(warmup, "_warm_file", slow_warm)
    files = [make_file(tmp_path, [], file_id=1)]

    async def run():
        warmup.schedule(1, files)
        warmup.schedule(1, files)  # 第一次还没跑完（任务尚未调度）
        for _ in range(10):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert called == [1]


def test_warm_failure_never_leaks_inflight(tmp_path, monkeypatch):
    """预热炸了要放行下一次，否则这个条目在进程生命周期里永远不再预热。"""

    def boom(f):
        raise RuntimeError("存储抖了一下")

    monkeypatch.setattr(warmup, "_warm_file", boom)

    async def run():
        warmup.schedule(1, [make_file(tmp_path, [], file_id=1)])
        for _ in range(10):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert 1 not in warmup._in_flight
