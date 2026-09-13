"""转码产物的跨会话复用（docs/design/player-pipeline-optimization.md §B）。

用假 ffmpeg 验会话层的行为：目录按指纹命名、台账落盘、同指纹认领、起播段
命中时不起写者、播到没转过的分片再拉起写者、指纹不同不认领、并发同指纹不共写、
淘汰按保留期与配额。真 ffmpeg 的端到端在 test_playback_e2e.py。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.cache import (
    MANIFEST_NAME,
    Manifest,
    cache_components,
    cache_key,
)
from movieclaw_api.services.playback.ffmpeg_args import TranscodeCommand
from movieclaw_api.services.playback.session import TranscodeSessionManager
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan
from movieclaw_playback.hls_vod import SegmentPlan

#: 假 ffmpeg：从 start_number 起立刻写出 N 个分片并登记，然后挂着
PRODUCES_N_THEN_SLEEPS = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
start = int(sys.argv[2]); n = int(sys.argv[3])
(out.parent / "init.mp4").write_bytes(b"init")
lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
for i in range(start, start + n):
    (out.parent / ("seg%05d.m4s" % i)).write_bytes(("seg%d" % i).encode())
    lines += ["#EXTINF:4.0,", "seg%05d.m4s" % i]
    out.write_text("\\n".join(lines) + "\\n")
time.sleep(300)
"""


def _plan(track: str = "embedded:0", file_id: int = 1) -> PlaybackPlan:
    return PlaybackPlan(
        tier=PlaybackTier.REMUX,
        file_id=file_id,
        container="hls-fmp4",
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref=track),
        reason="测试",
    )


def _segments(count: int = 20) -> SegmentPlan:
    return SegmentPlan(boundaries=tuple(float(i * 4) for i in range(count)), duration_s=count * 4.0)


class Spawns:
    """记录假 ffmpeg 被拉起了几次、每次从哪一片起。"""

    def __init__(self) -> None:
        self.starts: list[int] = []


def install(monkeypatch, *, produce: int) -> Spawns:
    spawns = Spawns()

    def fake_build(plan, *, source_path, session_dir, start_ms=0, hw_backend=None,
                   start_number=None, **_):
        spawns.starts.append(start_number or 0)
        playlist = Path(session_dir) / "live.m3u8"
        return TranscodeCommand(
            argv=["python3", "-c", PRODUCES_N_THEN_SLEEPS, str(playlist),
                  str(start_number or 0), str(produce)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", fake_build)
    return spawns


@pytest.fixture
def manager(tmp_path) -> TranscodeSessionManager:
    return TranscodeSessionManager(root=tmp_path / "transcodes")


@pytest.fixture
def source(tmp_path) -> str:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"not really a movie")
    return str(path)


async def _start(manager, source, **kwargs):
    return await manager.start(
        kwargs.pop("plan", _plan()), source_path=source, member_id=0,
        segment_plan=kwargs.pop("segment_plan", _segments()), **kwargs,
    )


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------


def test_fingerprint_covers_every_output_affecting_input(source):
    base = cache_components(_plan(), source_path=source, hw_backend=None, remote=False,
                            segment_plan=_segments())
    key = cache_key(base)
    # 换音轨、换后端、换远程/本地、换分片边界、换源文件内容（size/mtime），都是新指纹
    assert cache_key(cache_components(_plan("embedded:1"), source_path=source, hw_backend=None,
                                      remote=False, segment_plan=_segments())) != key
    assert cache_key(cache_components(_plan(), source_path=source, hw_backend="vaapi",
                                      remote=False, segment_plan=_segments())) != key
    assert cache_key(cache_components(_plan(), source_path=source, hw_backend=None,
                                      remote=True, segment_plan=_segments())) != key
    assert cache_key(cache_components(_plan(), source_path=source, hw_backend=None,
                                      remote=False, segment_plan=_segments(21))) != key
    Path(source).write_bytes(b"replaced with a different file")
    assert cache_key(cache_components(_plan(), source_path=source, hw_backend=None,
                                      remote=False, segment_plan=_segments())) != key


def test_fingerprint_includes_the_bitrate_cap(source):
    """按带宽压过的码率（§C）进指纹：同一部片不同码率的产物不能互认。"""
    from dataclasses import replace

    capped = replace(_plan(), video=VideoPlan(action="transcode", codec="h264", height=720,
                                              bitrate_cap_bps=2_000_000))
    plain = replace(_plan(), video=VideoPlan(action="transcode", codec="h264", height=720))
    a = cache_key(cache_components(capped, source_path=source, hw_backend=None, remote=False,
                                   segment_plan=_segments()))
    b = cache_key(cache_components(plain, source_path=source, hw_backend=None, remote=False,
                                   segment_plan=_segments()))
    assert a != b


def test_manifest_round_trips_and_rejects_garbage(tmp_path):
    m = Manifest(key="k", components={"a": 1}, boundaries=[0.0, 4.0], duration_s=8.0,
                 completed=[1, 0], created_at=1.0, last_used_at=2.0)
    m.save(tmp_path)
    loaded = Manifest.load(tmp_path)
    assert loaded is not None and loaded.completed == [0, 1] and loaded.matches({"a": 1})
    (tmp_path / MANIFEST_NAME).write_text("{not json")
    assert Manifest.load(tmp_path) is None
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"format": 99}))
    assert Manifest.load(tmp_path) is None


# ---------------------------------------------------------------------------
# 认领与写者
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_session_adopts_the_cache_and_skips_the_writer(manager, source, monkeypatch):
    """第一路转出 5 段后结束；第二路同指纹从第 2 段起播：命中缓存、不起 ffmpeg，
    直到请求第 5 段（没转过）才拉起写者，且从第 5 段接着转。"""
    spawns = install(monkeypatch, produce=5)
    first = await _start(manager, source)
    assert first.manifest is not None and not first.cache_hit
    directory = first.directory
    assert directory.name == first.manifest.key  # 目录按指纹命名
    for i in range(5):
        assert await manager.ensure_segment(first, i) is not None
    await manager.stop(first.id)
    assert directory.exists() and (directory / MANIFEST_NAME).exists()
    assert Manifest.load(directory).completed == [0, 1, 2, 3, 4]
    assert spawns.starts == [0]

    second = await _start(manager, source, start_ms=8_000)
    try:
        assert second.directory == directory
        assert second.cache_hit and second.cached_segments == 5
        assert second.state == "ready" and second.process is None  # 没起写者
        assert spawns.starts == [0]
        assert await manager.ensure_segment(second, 2) is not None
        assert await manager.ensure_segment(second, 4) is not None
        assert spawns.starts == [0]  # 缓存里有的分片一个 ffmpeg 都不起
        # 第 5 段没转过：这时才拉写者，且从 5 起
        assert await manager.ensure_segment(second, 5) is not None
        assert spawns.starts == [0, 5]
        assert second.head_segment == 5
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_different_fingerprint_does_not_adopt(manager, source, monkeypatch):
    install(monkeypatch, produce=3)
    first = await _start(manager, source)
    assert await manager.ensure_segment(first, 0) is not None
    await manager.stop(first.id)
    second = await _start(manager, source, plan=_plan("embedded:1"))
    try:
        assert not second.cache_hit
        assert second.directory != first.directory
        assert first.directory.exists()  # 旧缓存还在，各自独立
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_concurrent_same_fingerprint_never_shares_a_writer(manager, source, monkeypatch):
    """另一位成员同时在看同一部片同一档：第二路退到独立目录，绝不两个 ffmpeg 写一处。"""
    install(monkeypatch, produce=3)
    first = await _start(manager, source)
    second = await manager.start(
        _plan(), source_path=source, member_id=1, segment_plan=_segments()
    )
    try:
        assert second.directory != first.directory
        assert second.directory.name.startswith(first.directory.name + "~")
        assert not second.cache_hit
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_directory_without_manifest_is_wiped_before_reuse(manager, source, monkeypatch):
    """有目录没台账 = 上次进程没走完 stop 的残留，整个重来，旧字节不混进新会话。"""
    install(monkeypatch, produce=2)
    components = cache_components(_plan(), source_path=source, hw_backend=None, remote=False,
                                  segment_plan=_segments())
    stale = manager._root / cache_key(components)
    stale.mkdir(parents=True)
    (stale / "seg00007.m4s").write_bytes(b"garbage")
    session = await _start(manager, source)
    try:
        assert session.directory == stale
        assert not session.cache_hit
        assert not (stale / "seg00007.m4s").exists()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cache_disabled_keeps_the_old_behaviour(manager, source, monkeypatch):
    install(monkeypatch, produce=2)
    session = await _start(manager, source, cache=False)
    directory = session.directory
    assert session.manifest is None and directory.name == session.id
    assert await manager.ensure_segment(session, 0) is not None
    await manager.stop(session.id)
    assert not directory.exists()


@pytest.mark.asyncio
async def test_stopping_dir_is_not_adopted_mid_teardown(manager, source, monkeypatch):
    """stop 途中（会话已摘、进程还在收尾）的目录当作在用：新会话退到独立目录。"""
    install(monkeypatch, produce=2)
    first = await _start(manager, source)
    manager._stopping_dirs.add(first.directory.name)
    manager._sessions.pop(first.id)
    try:
        second = await _start(manager, source)
        assert second.directory != first.directory
        assert second.directory.name.startswith(first.directory.name + "~")
    finally:
        manager._stopping_dirs.discard(first.directory.name)
        await manager._terminate(first)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_session_leaves_no_cache(manager, source, monkeypatch):
    """起不来的会话（命令有错）不留目录：半成品不能被下一次认领。"""
    monkeypatch.setattr(session_mod, "VOD_FAST_FAIL_WINDOW_S", 2.0)

    def dead_build(plan, *, source_path, session_dir, start_ms=0, hw_backend=None,
                   start_number=None, **_):
        playlist = Path(session_dir) / "live.m3u8"
        return TranscodeCommand(
            argv=["python3", "-c", "import sys; sys.exit(1)"],
            playlist_path=playlist, init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", dead_build)
    with pytest.raises(session_mod.SessionStartError):
        await _start(manager, source)
    assert not any(manager._root.iterdir()) if manager._root.exists() else True


# ---------------------------------------------------------------------------
# 回收
# ---------------------------------------------------------------------------


def _cold_dir(root: Path, name: str, *, size: int, age_s: float) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "seg00000.m4s").write_bytes(b"x" * size)
    Manifest(key=name, components={}, boundaries=[0.0], duration_s=4.0, completed=[0],
             created_at=time.time() - age_s, last_used_at=time.time() - age_s).save(directory)
    return directory


def test_startup_cleanup_keeps_manifested_dirs_and_drops_the_rest(manager, tmp_path):
    manager._root.mkdir(parents=True)
    kept = _cold_dir(manager._root, "kept", size=10, age_s=60)
    orphan = manager._root / "orphan"
    orphan.mkdir()
    (orphan / "seg00000.m4s").write_bytes(b"x")
    assert manager.cleanup_orphans() == 1
    assert kept.exists() and not orphan.exists()


def test_eviction_drops_expired_then_oldest_until_under_quota(manager):
    manager._root.mkdir(parents=True)
    expired = _cold_dir(manager._root, "expired", size=10, age_s=session_mod.CACHE_RETENTION_S + 5)
    oldest = _cold_dir(manager._root, "oldest", size=100, age_s=3_000)
    newer = _cold_dir(manager._root, "newer", size=100, age_s=1_000)
    newest = _cold_dir(manager._root, "newest", size=100, age_s=10)
    # 配额正好装下两个最新的（连台账文件一起算）：过期的先走，然后从最旧的删
    keep = session_mod._dir_size(newer) + session_mod._dir_size(newest)
    removed = manager.evict_cold(quota_bytes=keep)
    assert removed == 2
    assert not expired.exists() and not oldest.exists()
    assert newer.exists() and newest.exists()


@pytest.mark.asyncio
async def test_eviction_never_touches_active_sessions(manager, source, monkeypatch):
    install(monkeypatch, produce=2)
    session = await _start(manager, source)
    try:
        (session.directory / "seg00000.m4s").write_bytes(b"x" * 10_000)
        assert manager.evict_cold(quota_bytes=0) == 0
        assert session.directory.exists()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_manifest_is_refreshed_by_the_reaper(manager, source, monkeypatch):
    """进程崩了也只丢最近一次巡检之后的登记：reap 顺手把台账刷一遍。"""
    install(monkeypatch, produce=3)
    session = await _start(manager, source)
    try:
        for i in range(3):
            assert await manager.ensure_segment(session, i) is not None
        await manager.reap()
        assert Manifest.load(session.directory).completed == [0, 1, 2]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_seek_restart_saves_manifest_before_respawning(manager, source, monkeypatch):
    install(monkeypatch, produce=2)
    session = await _start(manager, source)
    try:
        assert await manager.ensure_segment(session, 0) is not None
        assert await manager.ensure_segment(session, 10) is not None  # 远跳 → 重启
        loaded = Manifest.load(session.directory)
        assert loaded is not None and 0 in loaded.completed
    finally:
        await manager.shutdown()


def test_usage_and_cold_bytes_are_separate(manager):
    manager._root.mkdir(parents=True)
    _cold_dir(manager._root, "a", size=30, age_s=1)
    assert manager.cold_cache_bytes() >= 30
    assert manager.usage_bytes() >= 30


async def _drain() -> None:
    await asyncio.sleep(0)
