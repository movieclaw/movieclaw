"""远程 Worker 面板的播放位置快照（session.playback_snapshot）。

观众位置取播放器自己的进度上报，不取「最近请求的分片」（那是下载位置，会比画面
快几十秒）；准备到哪儿、总长来自 VOD 分片计划。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from movieclaw_api.services.playback.session import (
    PLAYBACK_EXTRAPOLATE_MAX_S,
    TranscodeSession,
    TranscodeSessionManager,
)
from movieclaw_playback import activity
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan
from movieclaw_playback.events import ClientInfo
from movieclaw_playback.hls_vod import SegmentPlan


@pytest.fixture(autouse=True)
def _clean_activity():
    activity.reset()
    yield
    activity.reset()


def _session(tmp_path, *, segments: SegmentPlan | None) -> TranscodeSession:
    plan = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=7,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=1080),
        audio=AudioPlan(action="copy", track_ref=None),
        reason="测试",
    )
    return TranscodeSession(
        id="s1",
        file_id=7,
        member_id=1,
        tier=plan.tier,
        directory=tmp_path,
        start_ms=0,
        plan=plan,
        segment_plan=segments,
        device_id="iphone-1",
        remote=True,
        remote_job_id="job-1",
        head_segment=10,
    )


def _report(position_ms: int, *, paused: bool, seconds_ago: float = 0) -> None:
    activity.report_progress(
        "iphone-1",
        member_id=1,
        client=ClientInfo(name="Infuse", device_id="iphone-1"),
        unit=(1, 0, 0),
        position_ms=position_ms,
        paused=paused,
    )
    viewer = activity.current("iphone-1")
    assert viewer is not None
    viewer.last_report_at -= timedelta(seconds=seconds_ago)


def test_snapshot_uses_reported_position_and_segment_plan(tmp_path, monkeypatch):
    manager = TranscodeSessionManager(root=tmp_path)
    session = _session(
        tmp_path,
        segments=SegmentPlan(
            boundaries=tuple(float(i * 6) for i in range(100)),
            duration_s=600.0,
        ),
    )
    # 本轮从第 10 片起，已连续产出到第 29 片 ⇒ 准备到第 30 片的起点 180 秒
    monkeypatch.setattr(manager, "_highest_produced", lambda s: 29)
    _report(90_000, paused=True, seconds_ago=8)

    assert manager.playback_snapshot(session) == {
        "position_ms": 90_000,  # 暂停中不外推
        "viewer_paused": True,
        "duration_ms": 600_000,
        "prepared_ms": 180_000,
    }


def test_playing_position_is_extrapolated_but_capped(tmp_path, monkeypatch):
    manager = TranscodeSessionManager(root=tmp_path)
    session = _session(tmp_path, segments=None)
    _report(90_000, paused=False, seconds_ago=5)
    snapshot = manager.playback_snapshot(session)
    # 距上次上报 5 秒、没暂停 ⇒ 往前推约 5 秒
    assert 94_900 <= snapshot["position_ms"] <= 95_500
    # 非 VOD 会话没有分片计划：不给总长与准备位置
    assert "duration_ms" not in snapshot and "prepared_ms" not in snapshot

    _report(90_000, paused=False, seconds_ago=600)
    stale = manager.playback_snapshot(session)
    assert stale["position_ms"] == 90_000 + int(PLAYBACK_EXTRAPOLATE_MAX_S * 1000)


def test_snapshot_is_empty_without_viewer_or_plan(tmp_path):
    manager = TranscodeSessionManager(root=tmp_path)
    assert manager.playback_snapshot(_session(tmp_path, segments=None)) == {}
