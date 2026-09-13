"""按实测带宽收紧转码码率（docs/design/player-pipeline-optimization.md §C）。

纯函数，表驱动：线路多宽 → 高度落到哪一档、码率上限是多少。
"""

from __future__ import annotations

import pytest

from movieclaw_api.services.playback.adaptive import (
    MIN_CAP_BPS,
    adapt_to_downlink,
    quantize_cap,
)
from movieclaw_api.services.playback.ffmpeg_args import build_hls_command, maxrate_for_video
from movieclaw_playback.decide import (
    AudioPlan,
    ConsentRequired,
    PlaybackPlan,
    PlaybackTier,
    VideoPlan,
)


def _plan(action: str = "transcode", height: int | None = 1080) -> PlaybackPlan:
    return PlaybackPlan(
        tier=PlaybackTier.SOFTWARE_TRANSCODE if action == "transcode" else PlaybackTier.REMUX,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(action=action, codec="h264", height=height),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
        reason="测试",
    )


def test_no_reading_leaves_plan_untouched():
    plan = _plan()
    assert adapt_to_downlink(plan, None) is plan
    assert adapt_to_downlink(plan, 0) is plan


def test_copy_video_is_never_adapted():
    """直通视频的码率改不了，那条路由前端提示用户手动换画质。"""
    plan = _plan(action="copy", height=None)
    assert adapt_to_downlink(plan, 1_000_000) is plan


def test_non_plan_decisions_pass_through():
    consent = ConsentRequired(
        tier=PlaybackTier.SOFTWARE_TRANSCODE, reason="r", cost_hint="c",
        can_self_enable=True,
    )
    assert adapt_to_downlink(consent, 1_000_000) is consent


def test_wide_pipe_keeps_the_ladder_value():
    """线路吃得下阶梯值就不设上限：与没有读数时行为完全一致。"""
    plan = _plan(height=1080)
    assert adapt_to_downlink(plan, 50_000_000) is plan


@pytest.mark.parametrize(
    ("downlink_bps", "height", "cap"),
    [
        (7_000_000, 1080, 5_500_000),   # 5.6M ≥ 6M × 0.75：保住 1080p，只压码率
        (5_000_000, 720, 3_000_000),    # 4M < 4.5M：降到 720p，钉在阶梯 3M
        (4_000_000, 720, 3_000_000),    # 3.2M 装得下 720p（3M）→ 720p，钉在 3M
        (2_000_000, 480, 1_500_000),    # 1.6M 只装得下 480p
        (1_000_000, 480, 750_000),      # 0.8M 一档都装不下 → 最低档 + 上限 0.75M
        (200_000, 480, MIN_CAP_BPS),    # 再差也不低于下限
    ],
)
def test_ladder_and_cap_follow_the_pipe(downlink_bps, height, cap):
    adapted = adapt_to_downlink(_plan(height=1080), downlink_bps)
    assert isinstance(adapted, PlaybackPlan)
    assert adapted.video.height == height
    assert adapted.video.bitrate_cap_bps == cap
    assert "实测线路" in adapted.reason


def test_five_mbps_falls_back_to_720p():
    """5 Mbps 的线路：0.8 × 5 = 4M 装不下 1080p（6M），装得下 720p（3M）。
    压 1080p 到 4M 是糊的，降到 720p 反而清楚。"""
    adapted = adapt_to_downlink(_plan(height=1080), 5_000_000)
    assert adapted.video.height == 720
    assert adapted.video.bitrate_cap_bps == 3_000_000


def test_target_height_is_an_upper_bound():
    """源只有 720p 时目标高度是 720，不会被「升」到 1080。"""
    adapted = adapt_to_downlink(_plan(height=720), 3_000_000)
    assert adapted.video.height == 720
    assert adapted.video.bitrate_cap_bps == 2_250_000


def test_off_ladder_height_rounds_up_to_the_next_step():
    """900p 这种不在阶梯上的高度按 1080p 的阶梯值判，够就原样保留高度。"""
    adapted = adapt_to_downlink(_plan(height=900), 7_000_000)
    assert adapted.video.height == 900
    assert adapted.video.bitrate_cap_bps == 5_500_000


def test_unknown_height_only_caps_bitrate():
    adapted = adapt_to_downlink(_plan(height=None), 2_000_000)
    assert adapted.video.height is None
    assert adapted.video.bitrate_cap_bps == 1_500_000


def test_cap_is_quantized_for_cache_reuse():
    """上限量化到 250 kbps：不量化的话每次实测差几 kbps 就是一份新缓存。"""
    assert quantize_cap(1_234_567) == 1_000_000
    assert quantize_cap(1_999_999) == 1_750_000
    assert quantize_cap(10) == MIN_CAP_BPS


def test_ffmpeg_maxrate_honours_the_cap():
    video = VideoPlan(action="transcode", codec="h264", height=720, bitrate_cap_bps=2_250_000)
    assert maxrate_for_video(video) == "2.25M"
    # 上限高于阶梯值时阶梯值说了算
    assert maxrate_for_video(VideoPlan(action="transcode", codec="h264", height=720,
                                       bitrate_cap_bps=9_000_000)) == "3M"


def test_ffmpeg_command_uses_capped_maxrate_and_bufsize(tmp_path):
    adapted = adapt_to_downlink(_plan(height=1080), 2_000_000)
    argv = build_hls_command(adapted, source_path="/m/a.mkv", session_dir=tmp_path).argv
    assert argv[argv.index("-maxrate") + 1] == "1.5M"
    assert argv[argv.index("-bufsize") + 1] == "3M"
    # 高度也一起降了：scale 滤镜里应当是 480
    vf = argv[argv.index("-vf") + 1]
    assert "480" in vf
