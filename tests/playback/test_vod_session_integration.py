"""VOD 会话的端到端验证（docs/design/web-player.md §12）——真起 ffmpeg。

覆盖三件事：

1. 关键帧索引 → 分片规划 → 会话起播，客户端能按预生成列表拿到分片；
2. seek 到远处的分片会触发「杀掉重启直奔目标」，编号接上、文件产出；
3. 重启后分片内部时间戳仍是**文件绝对时间**（copyts 三件套的效果）——
   这是预生成列表与实际分片能对上的根本。

标 ``integration``：需要系统 ffmpeg/ffprobe。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from movieclaw_api.services.playback import ffmpeg_args
from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.ffmpeg_args import SEGMENT_PATTERN, SEGMENT_SECONDS
from movieclaw_api.services.playback.session import TranscodeSessionManager
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan
from movieclaw_playback.hls_vod import compute_segment_plan
from movieclaw_playback.keyframes import read_keyframe_index

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="需要系统 ffmpeg/ffprobe",
    ),
]

#: 样本时长：够切出 15 个 4 秒分片，覆盖「seek 到远处触发重启」的场景
DURATION = 60


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    """2 秒一个关键帧的 H.264 MKV——模拟 remux 直通片源。"""
    root = tmp_path_factory.mktemp("vod-corpus")
    path = root / "h264-2s-gop.mkv"
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
            "-c:a", "aac", "-shortest", "-y", str(path),
        ],
        capture_output=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]
    return path


def _plan() -> PlaybackPlan:
    return PlaybackPlan(
        tier=PlaybackTier.REMUX,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
        reason="VOD 集成测试",
    )


def _segment_start_time(directory: Path, index: int) -> float:
    """拼上 init 段读分片的真实起始时间（文件绝对时间）。"""
    joined = directory / f"joined-{index}.mp4"
    joined.write_bytes(
        (directory / "init.mp4").read_bytes()
        + (directory / (SEGMENT_PATTERN % index)).read_bytes()
    )
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", str(joined),
        ],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-1000:]
    return float(json.loads(proc.stdout)["format"]["start_time"])


def _slow_down(monkeypatch, *, readrate: float, burst_s: int) -> None:
    """给真命令塞一个 readrate：VOD 模式本身已不带限速（供片由会话层按领先量
    闭环节流，§A），但 60 秒的样本会瞬间转完，「seek 远处必须重启直奔」的场景
    根本构造不出来。这里只是让 ffmpeg 慢下来，与被测逻辑无关。"""
    real_build = session_mod.build_hls_command

    def slow_build(*args, **kwargs):
        command = real_build(*args, **kwargs)
        argv = list(command.argv)
        at = argv.index("-i")
        argv[at:at] = [
            "-readrate", str(readrate), "-readrate_initial_burst", str(burst_s),
        ]
        return ffmpeg_args.TranscodeCommand(
            argv=argv, playlist_path=command.playlist_path, init_path=command.init_path
        )

    monkeypatch.setattr(session_mod, "build_hls_command", slow_build)


def test_vod_session_serves_and_seeks(sample, tmp_path, monkeypatch):
    _slow_down(monkeypatch, readrate=0.5, burst_s=4)

    async def scenario() -> None:
        index = read_keyframe_index(sample)
        assert index is not None and len(index.times_s) >= DURATION // 2 - 1
        plan = compute_segment_plan(index.times_s, float(DURATION), target_s=SEGMENT_SECONDS)
        assert plan.count >= 12

        manager = TranscodeSessionManager(root=tmp_path / "transcodes")
        session = await manager.start(
            _plan(),
            source_path=str(sample),
            member_id=0,
            start_ms=0,
            segment_plan=plan,
        )
        try:
            # 1. 顺播：第 0、1 个分片按需就绪
            first = await manager.ensure_segment(session, 0)
            assert first is not None and first.exists()
            second = await manager.ensure_segment(session, 1)
            assert second is not None
            # 分片时间戳 = 文件绝对时间（起点即分片边界）
            assert abs(_segment_start_time(session.directory, 1) - plan.boundaries[1]) < 0.6

            # 2. seek 到远处 → 触发重启直奔目标，编号接上
            far = plan.count - 2
            target = await manager.ensure_segment(session, far)
            assert target is not None and target.exists()
            assert session.head_segment == far  # 确实是重启而不是顺序转过去
            # 重启后的分片时间戳仍是文件绝对时间——copyts 三件套的意义
            assert abs(_segment_start_time(session.directory, far) - plan.boundaries[far]) < 0.6

            # 3. 回看第一轮已转出的分片：直接命中跨轮次台账，**不重启**——
            #    没有台账时这里会白杀一次 ffmpeg 再转一遍（修过的坑）
            head_before = session.head_segment
            back = await manager.ensure_segment(session, 1)
            assert back is not None and back.exists()
            assert session.head_segment == head_before

            # 4. 往回 seek 到从未转出的分片 → 这才需要重启
            never = await manager.ensure_segment(session, far - 3)
            assert never is not None and never.exists()
            assert session.head_segment == far - 3
        finally:
            await manager.stop(session.id)

    asyncio.run(scenario())


def test_lead_throttle_pauses_real_ffmpeg_and_keeps_disk_bounded(sample, tmp_path, monkeypatch):
    """真 ffmpeg 上的闭环节流（§A）：不限速的 remux 会在几百毫秒内把 60 秒样本
    全转完；把领先上限压到 12 秒后，巡检应在播放头不动时把 ffmpeg 挂起，盘上
    分片数停在上限附近；播放头追上后恢复并转完。"""
    monkeypatch.setattr(session_mod, "LEAD_HIGH_S", 12.0)
    monkeypatch.setattr(session_mod, "LEAD_LOW_S", 4.0)
    monkeypatch.setattr(session_mod, "THROTTLE_INTERVAL_S", 0.02)
    # 让 ffmpeg 至少比巡检慢一点（2 倍速）：全速 remux 在一次巡检之内就能
    # 写完整部片，那样测的就不是节流而是巡检的运气
    _slow_down(monkeypatch, readrate=2.0, burst_s=0)

    async def scenario() -> None:
        index = read_keyframe_index(sample)
        plan = compute_segment_plan(index.times_s, float(DURATION), target_s=SEGMENT_SECONDS)
        manager = TranscodeSessionManager(root=tmp_path / "transcodes")
        manager.start_reaper()
        session = await manager.start(
            _plan(), source_path=str(sample), member_id=0, start_ms=0, segment_plan=plan
        )
        try:
            assert await manager.ensure_segment(session, 0) is not None
            # 播放头停在第 0 片：等巡检把它挂起
            deadline = asyncio.get_event_loop().time() + 8.0
            while asyncio.get_event_loop().time() < deadline and not session.lead_paused:
                await asyncio.sleep(0.05)
            assert session.lead_paused, "真 ffmpeg 领先超上限却没被挂起"
            produced = manager._highest_produced(session)
            # 12 秒上限 ≈ 3 片，再加巡检间隔内的余量
            assert 2 <= produced <= 6, produced
            await asyncio.sleep(0.5)
            assert manager._highest_produced(session) == produced  # 挂起后不再涨
            # 播放头追上 → 恢复 → 最终全片转完
            assert await manager.ensure_segment(session, produced) is not None
            assert not session.lead_paused
            last = await manager.ensure_segment(session, plan.count - 1)
            assert last is not None and last.exists()
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
