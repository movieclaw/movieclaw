"""ffmpeg 命令装配单测（docs/design/web-player.md §2.1 / §7）。

装配是纯函数，所以 §7 那些陷阱可以在这里逐条钉死，不必每次起进程：
hvc1 标签、绝不烧录、降混系数、分片对齐、hwaccel 后端矩阵、tone-map 回退。

真实 ffmpeg 的行为验证在 ``tests/playback/test_transcode_integration.py``
（标 integration），两者分工：这里管「参数对不对」，那里管「跑出来对不对」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movieclaw_api.services.playback.ffmpeg_args import (
    SEGMENT_SECONDS,
    build_hls_command,
)
from movieclaw_playback.decide import (
    AudioPlan,
    PlaybackPlan,
    PlaybackTier,
    SubtitlePlan,
    VideoPlan,
)

SESSION_DIR = Path("/data/transcodes/abc")


def plan(
    tier: PlaybackTier,
    *,
    video: VideoPlan | None = None,
    audio: AudioPlan | None = None,
    subtitles: tuple[SubtitlePlan, ...] = (),
) -> PlaybackPlan:
    return PlaybackPlan(
        tier=tier,
        file_id=1,
        container="hls-fmp4",
        video=video or VideoPlan(action="copy", codec="h264"),
        audio=audio or AudioPlan(action="copy", track_ref="embedded:0"),
        subtitles=subtitles,
        reason="测试",
    )


def build(p: PlaybackPlan, **kwargs):
    return build_hls_command(p, source_path="/m/a.mkv", session_dir=SESSION_DIR, **kwargs)


def argv_of(p: PlaybackPlan, **kwargs) -> list[str]:
    return build(p, **kwargs).argv


def pair(argv: list[str], flag: str) -> str | None:
    """取某个标志的值；标志不存在返回 None。"""
    return argv[argv.index(flag) + 1] if flag in argv else None


# ---------------------------------------------------------------------------
# §7-①  hvc1 标签：不打就是 Safari 静默黑屏
# ---------------------------------------------------------------------------


def test_hevc_copy_gets_hvc1_tag():
    argv = argv_of(plan(PlaybackTier.REMUX, video=VideoPlan(action="copy", codec="hevc")))
    assert pair(argv, "-tag:v") == "hvc1"


def test_h264_copy_gets_no_video_tag():
    argv = argv_of(plan(PlaybackTier.REMUX, video=VideoPlan(action="copy", codec="h264")))
    assert "-tag:v" not in argv


def test_audio_transcode_tier_keeps_hvc1_on_video_copy():
    """档 2 的视频仍是 copy，标签同样不能丢。"""
    argv = argv_of(
        plan(
            PlaybackTier.AUDIO_TRANSCODE,
            video=VideoPlan(action="copy", codec="hevc"),
            audio=AudioPlan(action="transcode", track_ref="embedded:0", codec="aac", channels=2),
        )
    )
    assert pair(argv, "-tag:v") == "hvc1"
    assert pair(argv, "-c:v") == "copy"


# ---------------------------------------------------------------------------
# 硬边界 1：绝不烧录字幕
# ---------------------------------------------------------------------------


def test_subtitles_are_never_muxed_or_burned():
    """字幕一律旁挂。烧录会把任何档位瞬间拖进全转码。"""
    argv = argv_of(
        plan(
            PlaybackTier.REMUX,
            subtitles=(SubtitlePlan(track_ref="embedded:0", kind="ass"),),
        )
    )
    assert "-sn" in argv
    assert not any(a.startswith("subtitles") or "burn" in a for a in argv)
    assert not any(a == "-c:s" for a in argv)
    # 也不能把字幕轨 map 进来
    assert not any(a.startswith("0:s") for a in argv)


def test_source_metadata_is_dropped():
    """-map_metadata -1：源片元数据里可能挂着大块附件，没必要进分片。"""
    assert pair(argv_of(plan(PlaybackTier.REMUX)), "-map_metadata") == "-1"


# ---------------------------------------------------------------------------
# 时间轴：-ss 必须在 -i 之前，且不用 copyts（会话时间轴恒从 0 起）
# ---------------------------------------------------------------------------


def test_seek_flag_precedes_input():
    argv = argv_of(plan(PlaybackTier.REMUX), start_ms=90_000)
    assert argv.index("-ss") < argv.index("-i")
    assert pair(argv, "-ss") == "90.000"


def test_no_seek_flag_when_starting_from_zero():
    assert "-ss" not in argv_of(plan(PlaybackTier.REMUX))


def test_copyts_is_not_used():
    """实测取舍：不用 copyts，会话时间轴恒从 0 起，前端只有一处换算
    （文件时间 = start_ms + currentTime）。"""
    argv = argv_of(plan(PlaybackTier.REMUX), start_ms=10_000)
    assert "-copyts" not in argv and "-start_at_zero" not in argv


# ---------------------------------------------------------------------------
# 音轨选择与降混
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref,expected", [("embedded:0", "0:a:0"), ("embedded:2", "0:a:2")])
def test_audio_track_maps_by_array_index(ref, expected):
    """中性引用的下标就是「第 k 路音频流」，可以直接给 ffmpeg——
    绝不能用绝对流序号，那个会被字幕/附件流搅乱。"""
    argv = argv_of(plan(PlaybackTier.REMUX, audio=AudioPlan(action="copy", track_ref=ref)))
    assert expected in argv


def test_downmix_carries_center_channel_weights():
    """不带中置加权，对白会明显偏小——用户投诉第一名。"""
    argv = argv_of(
        plan(
            PlaybackTier.AUDIO_TRANSCODE,
            audio=AudioPlan(
                action="transcode", track_ref="embedded:0", codec="aac",
                channels=2, downmix=True,
            ),
        )
    )
    af = pair(argv, "-af")
    assert af and af.startswith("pan=stereo|")
    assert "FC" in af  # 中置声道必须参与
    assert "volume=" not in af  # 不用会削顶失真的 volume hack


def test_multichannel_transcode_without_downmix_has_no_pan():
    argv = argv_of(
        plan(
            PlaybackTier.AUDIO_TRANSCODE,
            audio=AudioPlan(
                action="transcode", track_ref="embedded:0", codec="eac3",
                channels=6, downmix=False,
            ),
        )
    )
    assert "-af" not in argv
    assert pair(argv, "-ac") == "6"
    assert pair(argv, "-c:a") == "eac3"


def test_plan_without_audio_track_maps_no_audio():
    argv = argv_of(plan(PlaybackTier.REMUX, audio=AudioPlan(action="copy", track_ref=None)))
    assert not any(a.startswith("0:a") for a in argv)
    assert "-c:a" not in argv


# ---------------------------------------------------------------------------
# 分片：fMP4/CMAF，playlist 先行
# ---------------------------------------------------------------------------


def test_segments_are_fmp4_not_mpegts():
    """输出统一 CMAF：同一份分片将来可同时喂 HLS 和 DASH。"""
    argv = argv_of(plan(PlaybackTier.REMUX))
    assert pair(argv, "-hls_segment_type") == "fmp4"
    assert pair(argv, "-hls_time") == str(SEGMENT_SECONDS)


def test_playlist_is_event_type_so_it_can_be_served_before_segments_exist():
    argv = argv_of(plan(PlaybackTier.REMUX))
    assert pair(argv, "-hls_playlist_type") == "event"
    assert pair(argv, "-hls_list_size") == "0"


def test_outputs_land_in_session_dir():
    cmd = build(plan(PlaybackTier.REMUX))
    assert cmd.playlist_path == SESSION_DIR / "index.m3u8"
    assert cmd.init_path == SESSION_DIR / "init.mp4"
    assert cmd.argv[-1] == str(cmd.playlist_path)


def test_transcode_forces_keyframes_for_exact_segments():
    """转码档自己控制 GOP，可以精确对齐 2 秒；直通档做不到（§7-②）。"""
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        )
    )
    assert pair(argv, "-force_key_frames") == f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"


def test_copy_tier_does_not_force_keyframes():
    assert "-force_key_frames" not in argv_of(plan(PlaybackTier.REMUX))


# ---------------------------------------------------------------------------
# 硬件加速后端矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend,encoder,hwaccel",
    [
        ("vaapi", "h264_vaapi", "vaapi"),
        ("qsv", "h264_qsv", "qsv"),
        ("nvenc", "h264_nvenc", "cuda"),
        ("videotoolbox", "h264_videotoolbox", "videotoolbox"),
    ],
)
def test_hardware_backends_use_their_own_encoder(backend, encoder, hwaccel):
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        ),
        hw_backend=backend,
    )
    assert pair(argv, "-c:v") == encoder
    assert pair(argv, "-hwaccel") == hwaccel
    assert "-crf" not in argv  # 硬件编码器不认 CRF


def test_software_fallback_uses_libx264_with_crf():
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=720),
        )
    )
    assert pair(argv, "-c:v") == "libx264"
    assert pair(argv, "-crf") == "21"
    assert "-hwaccel" not in argv
    assert pair(argv, "-vf") == "scale=-2:720"


def test_hwaccel_only_applies_when_transcoding_video():
    """直通档不该出现任何 hwaccel 参数——没有解码，谈不上加速。"""
    argv = argv_of(plan(PlaybackTier.REMUX), hw_backend="vaapi")
    assert "-hwaccel" not in argv


# ---------------------------------------------------------------------------
# tone-map：有原生滤镜就用，没有就退回软件链（§7-④b）
# ---------------------------------------------------------------------------


def test_vaapi_uses_native_tonemap_filter():
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080, tone_map=True),
        ),
        hw_backend="vaapi",
    )
    vf = pair(argv, "-vf")
    assert vf and vf.startswith("tonemap_vaapi")
    assert "scale_vaapi" in vf


def test_nvenc_without_native_tonemap_falls_back_to_software_chain():
    """实测：上游 ffmpeg 没有 tonemap_cuda（那是 jellyfin-ffmpeg 的补丁）。
    此时走软件解码 + 软件 tone-map + 硬件编码，而不是硬凑 hwdownload 链。"""
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080, tone_map=True),
        ),
        hw_backend="nvenc",
    )
    vf = pair(argv, "-vf")
    assert vf and "tonemap=tonemap=bt2390" in vf  # BT.2390 EETF，不是简单 clip
    assert "-hwaccel" not in argv  # 解码退回软件，滤镜链才接得上
    assert pair(argv, "-c:v") == "h264_nvenc"  # 编码仍然用硬件


def test_tone_map_uses_bt2390_not_clip():
    """简单 clip 会把高光全压成死白（雪景、天空、爆炸场面直接糊掉）。"""
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080, tone_map=True),
        )
    )
    assert "bt2390" in pair(argv, "-vf")


def test_direct_play_tier_is_rejected():
    """档 0 是原文件直出，走到命令装配就是调用方的 bug。"""
    with pytest.raises(ValueError):
        build(plan(PlaybackTier.DIRECT_PLAY))
