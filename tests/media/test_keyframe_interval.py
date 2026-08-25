"""关键帧密度估算单测（docs/design/web-player.md §3.5 / §7-②）。

这个数字是 remux 直通（档 1/2）的闸门：估小了会让长 GOP 的片子走直通、
分片切在非关键帧上花屏；估大了则把本可无损直通的片子推进转码，白白掉画质
和烧 GPU。所以按纯函数单独测，不依赖 ffprobe。
"""

from __future__ import annotations

from movieclaw_api.services.media_probe import (
    _keyframe_windows,
    keyframe_interval_from_packets,
    last_keyframe_at_or_before,
)


def kf(*times: float) -> list[dict]:
    """构造一串关键帧 packet。"""
    return [{"pts_time": str(t), "flags": "K_"} for t in times]


def test_regular_gop_gives_that_interval():
    packets = kf(*[i * 4.0 for i in range(8)])
    assert keyframe_interval_from_packets(packets, [(0, 30)]) == 4.0


def test_non_keyframe_packets_are_ignored():
    packets = kf(0.0, 4.0, 8.0) + [
        {"pts_time": "1.0", "flags": "__"},
        {"pts_time": "2.0", "flags": "__"},
    ]
    assert keyframe_interval_from_packets(packets, [(0, 30)]) == 4.0


def test_intervals_never_cross_window_boundaries():
    """两段采样之间的巨大空档不能被当成一个超长 GOP——这是采样式估算最容易
    踩的坑：不隔离窗口的话，任何片子都会被判成关键帧稀疏。"""
    packets = kf(0.0, 3.0, 6.0, 9.0) + kf(3600.0, 3603.0, 3606.0, 3609.0)
    windows = [(0, 30), (3600, 3630)]
    assert keyframe_interval_from_packets(packets, windows) == 3.0


def test_median_resists_one_sparse_window():
    """片尾静态画面那种异常稀疏的一段不该带偏整体判定。"""
    packets = kf(0.0, 2.0, 4.0, 6.0) + kf(100.0, 102.0, 104.0) + kf(200.0, 228.0)
    windows = [(0, 30), (100, 130), (200, 230)]
    interval = keyframe_interval_from_packets(packets, windows)
    assert interval == 2.0


def test_single_keyframe_window_degrades_conservatively():
    """一个窗口里只有一个关键帧 → 给出偏大的估计，让决策引擎不走 remux。"""
    interval = keyframe_interval_from_packets(kf(5.0), [(0, 30)])
    assert interval is not None and interval >= 30.0


def test_empty_window_is_conservative_too():
    interval = keyframe_interval_from_packets(kf(500.0), [(0, 30)])
    assert interval is not None and interval >= 30.0


def test_no_keyframes_at_all_is_unknown():
    """无关键帧信息 → None，决策引擎保守不走 remux（索引未知就赌不起）。"""
    assert keyframe_interval_from_packets([], [(0, 30)]) is None
    assert keyframe_interval_from_packets([{"pts_time": "1.0", "flags": "__"}], [(0, 30)]) is None


def test_malformed_pts_is_skipped_not_crashed():
    packets = kf(0.0, 4.0) + [{"pts_time": "N/A", "flags": "K_"}, {"flags": "K_"}]
    assert keyframe_interval_from_packets(packets, [(0, 30)]) == 4.0


def test_full_scan_mode_infers_window_from_data():
    """短片整片扫（windows 为空）时，窗口由数据本身推出。"""
    packets = kf(0.0, 5.0, 10.0, 15.0)
    assert keyframe_interval_from_packets(packets, []) == 5.0


class TestWindowPlanning:
    def test_short_media_scans_whole_file(self):
        assert _keyframe_windows(60) == []
        assert _keyframe_windows(None) == []
        assert _keyframe_windows(0) == []

    def test_long_media_samples_three_windows(self):
        windows = _keyframe_windows(7200)
        assert len(windows) == 3
        assert windows[0][0] == 720 and windows[1][0] == 3600 and windows[2][0] == 6480
        # 窗口不重叠，且都落在片内
        assert all(end <= 7200 + 30 for _, end in windows)
        assert windows[0][1] < windows[1][0] < windows[1][1] < windows[2][0]


def test_last_keyframe_at_or_before_picks_nearest():
    """续播校正：取 ≤ 目标点的最后一个关键帧；恰好相等也算（含浮点容差）。"""
    packets = [
        {"pts_time": "10.0", "flags": "K__"},
        {"pts_time": "12.5", "flags": "___"},
        {"pts_time": "14.0", "flags": "K__"},
        {"pts_time": "18.0", "flags": "K__"},
    ]
    assert last_keyframe_at_or_before(packets, 15.0) == 14.0
    assert last_keyframe_at_or_before(packets, 14.0) == 14.0
    assert last_keyframe_at_or_before(packets, 9.0) is None
    # 坏数据不炸：缺 pts / N/A 直接跳过
    broken = [{"flags": "K__"}, {"pts_time": "N/A", "flags": "K__"}]
    assert last_keyframe_at_or_before(broken, 5) is None
