"""闭环供片节流（docs/design/player-pipeline-optimization.md §A）。

用假 ffmpeg：每隔一小段时间落一个分片并追加进 live.m3u8，模拟「转码头一路
往前跑」。要验的是会话层按领先量 SIGSTOP / SIGCONT 的闭环，与转码内容无关。
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
from pathlib import Path

import pytest

from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.ffmpeg_args import TranscodeCommand
from movieclaw_api.services.playback.session import (
    PAUSE_DISK,
    PAUSE_LEAD,
    TranscodeSessionManager,
)
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan
from movieclaw_playback.hls_vod import SegmentPlan

#: 假 ffmpeg：从 start_number 起每 STEP 秒产出一个 4 秒分片，写完就登记进 live.m3u8
PRODUCES_SEGMENTS = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
start = int(sys.argv[2])
step = float(sys.argv[3])
count = int(sys.argv[4])
lines = ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.mp4"']
(out.parent / "init.mp4").write_bytes(b"init")
out.write_text("\\n".join(lines) + "\\n")
for i in range(start, count):
    (out.parent / ("seg%05d.m4s" % i)).write_bytes(b"x" * 1024)
    lines.append("#EXTINF:4.0,")
    lines.append("seg%05d.m4s" % i)
    out.write_text("\\n".join(lines) + "\\n")
    time.sleep(step)
time.sleep(300)
"""


def _plan() -> PlaybackPlan:
    return PlaybackPlan(
        tier=PlaybackTier.REMUX,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
        reason="测试",
    )


def _segments(count: int) -> SegmentPlan:
    return SegmentPlan(boundaries=tuple(float(i * 4) for i in range(count)), duration_s=count * 4.0)


def install_producer(monkeypatch, *, step: float, count: int) -> None:
    def fake_build(plan, *, source_path, session_dir, start_ms=0, hw_backend=None,
                   start_number=None, **_):
        playlist = Path(session_dir) / "live.m3u8"
        return TranscodeCommand(
            argv=["python3", "-c", PRODUCES_SEGMENTS, str(playlist),
                  str(start_number or 0), str(step), str(count)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", fake_build)


def _stopped(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip().startswith("T")


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
def manager(tmp_path) -> TranscodeSessionManager:
    return TranscodeSessionManager(root=tmp_path / "transcodes")


# ---------------------------------------------------------------------------
# 领先量的算法（纯逻辑）
# ---------------------------------------------------------------------------


def _vod_session(tmp_path, *, head: int, completed: set[int], requested: int | None):
    session = session_mod.TranscodeSession(
        id="s", file_id=1, member_id=0, tier=PlaybackTier.REMUX,
        directory=tmp_path, start_ms=0, plan=_plan(),
        segment_plan=_segments(100), head_segment=head,
        completed_segments=set(completed), last_requested_segment=requested,
    )
    (tmp_path / "live.m3u8").write_text("#EXTM3U\n")
    return session


def test_lead_is_measured_from_the_requested_segment(manager, tmp_path):
    """领先量 = 已连续产出末端 − 播放头（最近一次请求的分片）起点。"""
    session = _vod_session(tmp_path, head=0, completed=set(range(30)), requested=10)
    # 产出到第 29 片（末端 120 秒），播放头在第 10 片（40 秒）→ 领先 80 秒
    assert manager.lead_seconds(session) == 80.0


def test_lead_falls_back_to_round_start_when_playhead_is_behind(manager, tmp_path):
    """播放头落在本轮起点之前（刚 seek 重启、旧轮次分片仍可服务）：从本轮起点量。"""
    session = _vod_session(tmp_path, head=50, completed=set(range(50, 55)), requested=3)
    assert manager.lead_seconds(session) == 20.0


def test_lead_is_zero_before_anything_is_produced(manager, tmp_path):
    session = _vod_session(tmp_path, head=50, completed=set(), requested=50)
    assert manager.lead_seconds(session) == 0.0


def test_lead_is_none_without_a_segment_plan(manager, tmp_path):
    session = _vod_session(tmp_path, head=0, completed=set(), requested=None)
    session.segment_plan = None
    assert manager.lead_seconds(session) is None


# ---------------------------------------------------------------------------
# 真进程：领先过多 → 挂起；播放头追上 → 恢复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_far_ahead_writer_is_paused_and_resumes_when_playhead_catches_up(
    manager, monkeypatch
):
    """转码头领先播放头超过上限就 SIGSTOP，产出停住；播放头追到下限以内再 SIGCONT。"""
    install_producer(monkeypatch, step=0.05, count=100)
    monkeypatch.setattr(session_mod, "LEAD_HIGH_S", 24.0)  # 6 片
    monkeypatch.setattr(session_mod, "LEAD_LOW_S", 8.0)    # 2 片
    session = await manager.start(
        _plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_segments(100)
    )
    pid = session.process.pid
    try:
        assert await manager.ensure_segment(session, 0) is not None
        # 播放头停在第 0 片，转码头一路往前：巡检应在领先 ≥ 24 秒时挂起它
        for _ in range(60):
            await manager.throttle()
            if session.lead_paused:
                break
            await asyncio.sleep(0.05)
        assert session.lead_paused, "领先量超上限却没有挂起"
        assert await wait_until(lambda: _stopped(pid))
        produced_at_pause = manager._highest_produced(session)
        assert produced_at_pause >= 5
        # 挂起后产出真的停了：等一会儿再数，不许再涨
        await asyncio.sleep(0.4)
        assert manager._highest_produced(session) == produced_at_pause
        # 盘上占用有硬上限：领先 24 秒 = 6 片，加上巡检间隔的余量不会离谱
        assert produced_at_pause <= 6 + 3

        # 播放头追上来（请求靠近转码头的分片）：领先回落到下限以内 → 恢复
        catch_up = produced_at_pause - 1
        assert await manager.ensure_segment(session, catch_up) is not None
        assert not session.lead_paused
        assert await wait_until(lambda: not _stopped(pid))
        assert await wait_until(lambda: manager._highest_produced(session) > produced_at_pause)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_pause_reasons_do_not_clobber_each_other(manager, monkeypatch):
    """磁盘低水位与领先量各自独立：磁盘回升不能把领先量的挂起一并解开。"""
    install_producer(monkeypatch, step=0.05, count=100)
    monkeypatch.setattr(session_mod, "LEAD_HIGH_S", 8.0)
    monkeypatch.setattr(session_mod, "LEAD_LOW_S", 4.0)
    session = await manager.start(
        _plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_segments(100)
    )
    pid = session.process.pid
    try:
        assert await manager.ensure_segment(session, 0) is not None
        for _ in range(60):
            await manager.throttle()
            if session.lead_paused:
                break
            await asyncio.sleep(0.05)
        assert session.lead_paused
        # 磁盘也告急：两个原因叠加
        assert await manager._pause(session, PAUSE_DISK)
        assert session.pause_reasons == {PAUSE_LEAD, PAUSE_DISK}
        assert _stopped(pid)
        # 磁盘回升：只撤磁盘原因，进程仍然挂着
        assert await manager._resume(session, PAUSE_DISK)
        assert session.pause_reasons == {PAUSE_LEAD}
        await asyncio.sleep(0.1)
        assert _stopped(pid)
        # 领先量回落才真正放行
        assert await manager._resume(session, PAUSE_LEAD)
        assert session.pause_reasons == set()
        assert await wait_until(lambda: not _stopped(pid))
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_seek_restart_releases_a_lead_paused_writer(manager, monkeypatch):
    """挂起中的进程不响应 SIGTERM/SIGKILL 的收尾等待；seek 重启前必须先解冻，
    否则要干等超时。重启后新进程从不挂起态起步。"""
    install_producer(monkeypatch, step=0.05, count=100)
    monkeypatch.setattr(session_mod, "LEAD_HIGH_S", 8.0)
    monkeypatch.setattr(session_mod, "LEAD_LOW_S", 4.0)
    session = await manager.start(
        _plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_segments(100)
    )
    try:
        assert await manager.ensure_segment(session, 0) is not None
        for _ in range(60):
            await manager.throttle()
            if session.lead_paused:
                break
            await asyncio.sleep(0.05)
        assert session.lead_paused
        old_pid = session.process.pid
        # 远跳：杀掉重启直奔目标
        target = await manager.ensure_segment(session, 80)
        assert target is not None and target.exists()
        assert session.head_segment == 80
        assert session.process.pid != old_pid
        assert session.pause_reasons == set()
        assert not _stopped(session.process.pid)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stop_releases_before_killing(manager, monkeypatch):
    install_producer(monkeypatch, step=0.05, count=100)
    session = await manager.start(
        _plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_segments(100)
    )
    pid = session.process.pid
    assert await manager._pause(session, PAUSE_LEAD)
    assert await wait_until(lambda: _stopped(pid))
    import time

    started = time.monotonic()
    assert await manager.stop(session.id)
    assert time.monotonic() - started < 2.0  # 走 SIGCONT + SIGTERM 快路径
    assert await wait_until(lambda: not _pid_alive(pid))


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.mark.asyncio
async def test_throttle_loop_runs_with_the_reaper(manager, monkeypatch):
    """巡检任务随 reaper 一起起停：播放头不动时只有它能发现该暂停了。"""
    install_producer(monkeypatch, step=0.02, count=100)
    monkeypatch.setattr(session_mod, "LEAD_HIGH_S", 8.0)
    monkeypatch.setattr(session_mod, "LEAD_LOW_S", 4.0)
    monkeypatch.setattr(session_mod, "THROTTLE_INTERVAL_S", 0.05)
    manager.start_reaper()
    session = await manager.start(
        _plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_segments(100)
    )
    try:
        assert await manager.ensure_segment(session, 0) is not None
        assert await wait_until(lambda: session.lead_paused, timeout=3.0)
    finally:
        await manager.shutdown()
    assert manager._throttler is None


def test_signal_constants_are_the_expected_ones():
    """SIGSTOP 不可被捕获，挂起是确定的；SIGCONT 恢复。"""
    assert signal.SIGSTOP.value == 19
    assert signal.SIGCONT.value == 18
