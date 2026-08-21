"""播放活动注册表（movieclaw_playback.activity）的单元测试。

覆盖：上报驱动的会话生命周期（开始/进度/停止/超时）、字节流计量与速率、
上报与取流两条通路的关联（local_streamed 补判）。
"""

from __future__ import annotations

import time

import pytest

from movieclaw_playback import activity
from movieclaw_playback.events import ClientInfo

CLIENT = ClientInfo(name="Infuse", device_name="客厅 Apple TV", device_id="dev-1", version="8.0")
UNIT = (1, 0, 0)


@pytest.fixture(autouse=True)
def _clean_registry():
    activity.reset()
    yield
    activity.reset()


def test_report_lifecycle_start_progress_stop() -> None:
    """开始建会话 → 进度刷新位置/暂停态 → 停止后从快照消失。"""
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    sessions, _ = activity.snapshot()
    assert len(sessions) == 1
    assert sessions[0].unit == UNIT
    assert sessions[0].position_ms is None

    activity.report_progress(
        "dev-1", member_id=0, client=CLIENT, unit=UNIT, position_ms=90_000, paused=True
    )
    sessions, _ = activity.snapshot()
    assert sessions[0].position_ms == 90_000
    assert sessions[0].paused is True

    # 不带位置/暂停字段的心跳保持原值
    activity.report_progress(
        "dev-1", member_id=0, client=CLIENT, unit=UNIT, position_ms=None, paused=None
    )
    sessions, _ = activity.snapshot()
    assert sessions[0].position_ms == 90_000
    assert sessions[0].paused is True

    activity.report_stop("dev-1")
    sessions, _ = activity.snapshot()
    assert sessions == []


def test_progress_without_start_rebuilds_session() -> None:
    """丢开始包/进程重启后，进度上报能就地重建会话；换片重置会话。"""
    activity.report_progress(
        "dev-1", member_id=3, client=CLIENT, unit=UNIT, position_ms=5_000, paused=False
    )
    sessions, _ = activity.snapshot()
    assert len(sessions) == 1
    assert sessions[0].member_id == 3

    other_unit = (2, 1, 4)
    activity.report_progress(
        "dev-1", member_id=3, client=CLIENT, unit=other_unit, position_ms=0, paused=None
    )
    sessions, _ = activity.snapshot()
    assert len(sessions) == 1
    assert sessions[0].unit == other_unit
    assert sessions[0].position_ms == 0


def test_session_expires_without_reports_unless_streaming() -> None:
    """超时无上报的会话被清除；设备仍在读盘发包则保留。"""
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    expired = time.monotonic() + activity.SESSION_TTL_SECONDS + 1

    # 无活跃流：过期清除
    sessions, _ = activity.snapshot(now_mono=expired)
    assert sessions == []

    # 有活跃播放流：同样超时但保留
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    meter = activity.register_stream(
        device_id="dev-1",
        kind=activity.STREAM_KIND_PLAY,
        member_id=0,
        unit=UNIT,
        file_id=11,
        file_name="movie.mkv",
        size_bytes=1024,
        client=CLIENT,
    )
    sessions, meters = activity.snapshot(now_mono=expired)
    assert len(sessions) == 1
    assert meters == [meter]


def test_meter_counts_bytes_and_rate() -> None:
    """计量器累计字节并给出窗口内的平均速率；回收后从快照消失。"""
    meter = activity.register_stream(
        device_id="dev-1",
        kind=activity.STREAM_KIND_DOWNLOAD,
        member_id=0,
        unit=UNIT,
        file_id=None,
        file_name="movie.mkv",
        size_bytes=10_000,
        client=CLIENT,
    )
    meter.add(4_000)
    meter.add(4_000)
    assert meter.bytes_sent == 8_000
    assert meter.rate_bytes_per_second() > 0

    activity.unregister_stream(meter)
    _, meters = activity.snapshot()
    assert meters == []


def test_snapshot_backfills_local_streamed() -> None:
    """取流先于上报到达时，快照口径补判会话为本地直连。"""
    activity.register_stream(
        device_id="dev-1",
        kind=activity.STREAM_KIND_PLAY,
        member_id=0,
        unit=UNIT,
        file_id=11,
        file_name="movie.mkv",
        size_bytes=1024,
        client=CLIENT,
    )
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    sessions, _ = activity.snapshot()
    assert sessions[0].local_streamed is True


def test_pure_report_session_stays_remote() -> None:
    """strm 网盘直链只有上报没有取流：会话保持 remote 口径。"""
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    sessions, _ = activity.snapshot()
    assert sessions[0].local_streamed is False


def test_repeat_start_keeps_session_identity() -> None:
    """同单元重复上报开始（seek / 恢复播放）保留原会话身份与起始时间。"""
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    sessions, _ = activity.snapshot()
    started_at = sessions[0].started_at
    activity.report_progress(
        "dev-1", member_id=0, client=CLIENT, unit=UNIT, position_ms=60_000, paused=False
    )

    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    sessions, _ = activity.snapshot()
    assert len(sessions) == 1
    assert sessions[0].started_at == started_at
    # 已看位置不因重复开始而闪回
    assert sessions[0].position_ms == 60_000


def test_session_accumulates_bytes_across_connections() -> None:
    """换连接不清零：一场播放里已结束连接的字节结转进会话累计。"""
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)

    def _open() -> activity.StreamMeter:
        return activity.register_stream(
            device_id="dev-1",
            kind=activity.STREAM_KIND_PLAY,
            member_id=0,
            unit=UNIT,
            file_id=11,
            file_name="movie.mkv",
            size_bytes=100_000,
            client=CLIENT,
        )

    first = _open()
    first.add(30_000)
    activity.unregister_stream(first)
    sessions, meters = activity.snapshot()
    assert meters == []
    assert sessions[0].bytes_transferred == 30_000

    # 重复回收（on_close 被调两次）不能把同一批字节记两遍
    activity.unregister_stream(first)
    sessions, _ = activity.snapshot()
    assert sessions[0].bytes_transferred == 30_000

    second = _open()
    second.add(20_000)
    activity.unregister_stream(second)
    sessions, _ = activity.snapshot()
    assert sessions[0].bytes_transferred == 50_000


def test_finished_connection_bytes_do_not_leak_to_next_unit() -> None:
    """换集时上一集的收尾连接不把字节记到新一集头上。"""
    activity.report_start("dev-1", member_id=0, client=CLIENT, unit=UNIT)
    meter = activity.register_stream(
        device_id="dev-1",
        kind=activity.STREAM_KIND_PLAY,
        member_id=0,
        unit=UNIT,
        file_id=11,
        file_name="s01e01.mkv",
        size_bytes=100_000,
        client=CLIENT,
    )
    meter.add(30_000)

    next_unit = (1, 1, 2)
    activity.report_progress(
        "dev-1", member_id=0, client=CLIENT, unit=next_unit, position_ms=0, paused=False
    )
    activity.unregister_stream(meter)

    sessions, _ = activity.snapshot()
    assert sessions[0].unit == next_unit
    assert sessions[0].bytes_transferred == 0
