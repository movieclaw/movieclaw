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
    MAX_GOP_FRAMES,
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
# §7-①  fMP4 编码标签：Safari 原生 HLS 依赖稳定的 sample entry
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
    """直通档 -ss 带 +0.5s 关键帧吸附补偿（start_ms 已被上游校正到关键帧，
    恰等时 ffmpeg 会回退到前一个关键帧）；转码档 accurate_seek 精确，不加。"""
    argv = argv_of(plan(PlaybackTier.REMUX), start_ms=90_000)
    assert argv.index("-ss") < argv.index("-i")
    assert pair(argv, "-ss") == "90.500"
    transcode_argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        ),
        start_ms=90_000,
    )
    assert pair(transcode_argv, "-ss") == "90.000"


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
    # 不降混就不该有 pan 系数；但时间戳对齐的 aresample 任何音频转码都要带
    af = pair(argv, "-af")
    assert af is not None and "pan=" not in af and "aresample=async=1" in af
    assert pair(argv, "-ac") == "6"
    assert pair(argv, "-c:a") == "eac3"


def test_audio_transcode_always_aligns_timestamps():
    """aresample=async=1 是唇音同步的保险：视频 copy + 音频转码时，音频起点
    偏移不吸收掉就是起播瞬间的音画不同步。降混时两个滤镜串同一条链。"""
    downmixed = argv_of(
        plan(
            PlaybackTier.AUDIO_TRANSCODE,
            audio=AudioPlan(
                action="transcode", track_ref="embedded:0", codec="aac",
                channels=2, downmix=True,
            ),
        )
    )
    af = pair(downmixed, "-af")
    assert af is not None and "pan=" in af and af.index("pan=") < af.index("aresample=")


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
    """转码档自己控制 GOP，可以精确对齐 4 秒；直通档做不到（§7-②）。"""
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        )
    )
    assert pair(argv, "-force_key_frames") == f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"


@pytest.mark.parametrize("backend", [None, "videotoolbox"])
def test_transcode_bounds_gop_for_stable_fmp4_segments(backend):
    """高位点 copyts seek 时也不能让编码器自行切出极短的分片。"""
    tier = PlaybackTier.SOFTWARE_TRANSCODE if backend is None else PlaybackTier.HARDWARE_TRANSCODE
    argv = argv_of(
        plan(tier, video=VideoPlan(action="transcode", codec="h264", height=1080)),
        hw_backend=backend,
        start_number=10,
    )
    assert pair(argv, "-g") == str(MAX_GOP_FRAMES)


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
            video=VideoPlan(
                action="transcode", codec="h264", height=1080, source_bit_depth=8
            ),
        ),
        hw_backend=backend,
    )
    assert pair(argv, "-c:v") == encoder
    assert pair(argv, "-hwaccel") == hwaccel
    assert "-crf" not in argv  # 硬件编码器不认 CRF


def test_videotoolbox_keeps_hardware_decode_with_explicit_software_scaling_bridge():
    """软件 scale 前显式下载 VideoToolbox 帧，避免隐式格式桥接失败。"""
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(
                action="transcode", codec="h264", height=1080, source_bit_depth=8
            ),
        ),
        hw_backend="videotoolbox",
    )

    assert pair(argv, "-c:v") == "h264_videotoolbox"
    assert pair(argv, "-hwaccel") == "videotoolbox"
    assert pair(argv, "-hwaccel_output_format") == "videotoolbox_vld"
    assert pair(argv, "-vf") == "hwdownload,format=nv12,scale=-2:1080,format=yuv420p"


def test_videotoolbox_uses_p010le_bridge_for_10bit_source():
    """10-bit HEVC 的 VideoToolbox 硬件帧不能以 NV12 下载。"""
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(
                action="transcode", codec="h264", height=1080, source_bit_depth=10
            ),
        ),
        hw_backend="videotoolbox",
    )

    assert pair(argv, "-hwaccel") == "videotoolbox"
    assert pair(argv, "-hwaccel_output_format") == "videotoolbox_vld"
    assert pair(argv, "-vf") == "hwdownload,format=p010le,scale=-2:1080,format=yuv420p"


def test_videotoolbox_unknown_bit_depth_uses_software_decode():
    """未知位深不能猜下载格式，宁可软件解码也不能让 Worker 起空转进程。"""
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        ),
        hw_backend="videotoolbox",
    )

    assert "-hwaccel" not in argv
    assert pair(argv, "-vf") == "scale=-2:1080"
    assert pair(argv, "-c:v") == "h264_videotoolbox"


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
    assert vf and "tonemapx=tonemap=bt2390" in vf  # BT.2390 EETF，不是简单 clip
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


# ---------------------------------------------------------------------------
# 读入限速：占盘增速的闸门（一晚 200 GB 的教训）
# ---------------------------------------------------------------------------


def test_readrate_throttle_precedes_input():
    """限速是输入选项，必须出现在 -i 之前，否则 ffmpeg 会当作输出选项报错。"""
    argv = argv_of(plan(PlaybackTier.REMUX))
    assert argv.index("-readrate") < argv.index("-i")
    assert argv.index("-readrate_initial_burst") < argv.index("-i")


def test_readrate_applies_to_every_tier():
    """remux 与转码档都要限，但闸门不同：直通不吃 CPU 放到 4 倍让缓冲快攒，
    真转码维持 1.5 倍护 CPU。全不限的教训是一晚 200 GB。"""
    copy_argv = argv_of(plan(PlaybackTier.REMUX))
    assert pair(copy_argv, "-readrate") == "4"
    assert pair(copy_argv, "-readrate_initial_burst") == "60"
    transcode_argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        )
    )
    assert pair(transcode_argv, "-readrate") == "1.5"
    assert pair(transcode_argv, "-readrate_initial_burst") == "60"


# ---------------------------------------------------------------------------
# 分片衔接的时间基修正：fMP4 拼接闪屏的三个闸门（参照 Jellyfin）
# ---------------------------------------------------------------------------


def test_fmp4_segment_options_fix_boundary_pts():
    """+frag_discont 让 TFDT 带真实 DTS，+skip_sidx 防 open-GOP 边界 PTS 被改写。"""
    for tier in (PlaybackTier.REMUX, PlaybackTier.SOFTWARE_TRANSCODE):
        argv = argv_of(plan(tier))
        assert pair(argv, "-hls_segment_options") == "movflags=+frag_discont+skip_sidx"


def test_copy_video_gets_genpts_input_flag():
    """直通档输入侧补 PTS；转码档解码器自己重建时间戳，不需要。"""
    copy_argv = argv_of(plan(PlaybackTier.REMUX))
    assert pair(copy_argv, "-fflags") == "+genpts"
    assert copy_argv.index("-fflags") < copy_argv.index("-i")
    transcode_argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        )
    )
    assert "-fflags" not in transcode_argv


# ---------------------------------------------------------------------------
# 码率阶梯：maxrate 必须随目标高度走
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("height", "maxrate", "bufsize"),
    [(2160, "16M", "32M"), (1080, "6M", "12M"), (720, "3M", "6M"), (480, "1.5M", "3M")],
)
def test_bitrate_ladder_follows_target_height(height, maxrate, bufsize):
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=height),
        )
    )
    assert pair(argv, "-maxrate") == maxrate
    assert pair(argv, "-bufsize") == bufsize


def test_software_transcode_keeps_crf_with_maxrate_cap():
    """CRF 恒定质量优先，阶梯只是上限兜底——两者必须同时在场。"""
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        )
    )
    assert pair(argv, "-crf") == "21"
    assert pair(argv, "-maxrate") == "6M"


def test_software_transcode_pins_8bit_output():
    """软编 H.264 必须钉死 yuv420p（2026-08-25 真机事故）：10-bit 源不钉的话
    libx264 顺着输入位深编出 High 10 profile——iPhone/多数硬解都不认，表现
    为真实解码错误，且降到哪一档软转都一样炸。Jellyfin 同款做法。"""
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=None),
        )
    )
    assert pair(argv, "-pix_fmt") == "yuv420p"


def test_videotoolbox_transcode_lets_encoder_choose_level_for_4k():
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=2160),
        ),
        hw_backend="videotoolbox",
    )

    assert pair(argv, "-profile:v") == "high"
    assert "-level:v" not in argv
    assert pair(argv, "-pix_fmt") == "yuv420p"


def test_software_h264_transcode_keeps_level_4_1():
    argv = argv_of(
        plan(
            PlaybackTier.SOFTWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=1080),
        )
    )

    assert pair(argv, "-level:v") == "4.1"


@pytest.mark.parametrize("backend", [None, "videotoolbox"])
def test_h264_transcode_uses_avc1_sample_entry(backend):
    tier = PlaybackTier.SOFTWARE_TRANSCODE if backend is None else PlaybackTier.HARDWARE_TRANSCODE
    argv = argv_of(
        plan(tier, video=VideoPlan(action="transcode", codec="h264", height=1080)),
        hw_backend=backend,
    )
    assert pair(argv, "-tag:v") == "avc1"


def test_aac_transcode_pins_aac_lc_profile():
    argv = argv_of(
        plan(
            PlaybackTier.AUDIO_TRANSCODE,
            audio=AudioPlan(action="transcode", track_ref="embedded:0", codec="aac", channels=2),
        )
    )
    assert pair(argv, "-profile:a") == "aac_low"


# ---------------------------------------------------------------------------
# VOD 模式（服务端预生成播放列表，§12）
# ---------------------------------------------------------------------------


def test_vod_mode_adds_copyts_and_start_number():
    """VOD 分片时间戳必须是文件绝对时间（copyts 三件套），编号接上全片规划。"""
    argv = argv_of(plan(PlaybackTier.REMUX), start_number=37)
    assert pair(argv, "-start_number") == "37"
    assert "-copyts" in argv
    assert pair(argv, "-avoid_negative_ts") == "disabled"
    assert "-start_at_zero" in argv
    # 内部进度列表用 live.m3u8，客户端的 index.m3u8 由服务端生成
    assert argv[-1].endswith("live.m3u8")


def test_event_mode_keeps_relative_timeline():
    """旧会话模式不带 copyts：前端按会话相对时间轴换算。"""
    argv = argv_of(plan(PlaybackTier.REMUX))
    assert "-copyts" not in argv
    assert "-start_number" not in argv
    assert argv[-1].endswith("index.m3u8")


# ---------------------------------------------------------------------------
# PGS 烧录：filter_complex 图与硬件编码取舍
# ---------------------------------------------------------------------------


def _burn_plan(**video_kw) -> PlaybackPlan:
    defaults = dict(action="transcode", codec="h264", height=1080, burn_subtitle="embedded:2")
    defaults.update(video_kw)
    return plan(PlaybackTier.SOFTWARE_TRANSCODE, video=VideoPlan(**defaults))


def test_burn_uses_filter_complex_with_overlay_before_scale():
    """overlay 必须在 scale 之前：PGS 位图坐标按源分辨率定位，先缩放再叠，
    字幕的位置和大小全错。"""
    argv = argv_of(_burn_plan())
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph == (
        "[0:v:0][0:s:2]overlay[burned];[burned]scale=-2:1080[pre];[pre]format=yuv420p[vout]"
    )
    assert argv[argv.index("-map") + 1] == "[vout]"
    assert "0:v:0" not in [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
    assert "-vf" not in argv  # 与 filter_complex 互斥


def test_burn_with_tonemap_runs_tonemap_before_overlay():
    """先把 HDR 画面拉回 SDR 再叠字幕：PGS 是 SDR 图形，叠上 HDR 帧再整体
    映射会把字幕颜色一起压暗。"""
    argv = argv_of(_burn_plan(tone_map=True, height=None))
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.startswith("[0:v:0]tonemapx")
    assert graph.index("tonemapx") < graph.index("overlay")
    # 末端钉 8-bit：10-bit 源经 overlay 的位深靠协商、随 ffmpeg 版本漂，
    # 协商出 10-bit 就是 iPhone 不认的 High 10（2026-08-25 真机事故）
    assert graph.endswith("overlay[pre];[pre]format=yuv420p[vout]")


def test_burn_forces_software_pipeline_for_vaapi():
    """overlay 是软件滤镜，VAAPI 编码器吃不了软件帧 → 整条退软件编码。"""
    argv = argv_of(_burn_plan(), hw_backend="vaapi")
    assert "-hwaccel" not in argv
    assert "libx264" in argv
    assert "h264_vaapi" not in argv


def test_burn_keeps_nvenc_encoder_on_software_frames():
    """NVENC 编码器接系统内存帧没问题：软件解码+overlay，硬件编码。"""
    argv = argv_of(_burn_plan(), hw_backend="nvenc")
    assert "-hwaccel" not in argv  # 解码侧仍是软件（滤镜链在软件侧）
    assert "h264_nvenc" in argv


def test_no_burn_keeps_plain_vf_path():
    """无烧录的普通转码不受影响：-map 0:v:0 + -vf，一切照旧。"""
    argv = argv_of(
        plan(
            PlaybackTier.HARDWARE_TRANSCODE,
            video=VideoPlan(action="transcode", codec="h264", height=720),
        ),
        hw_backend="vaapi",
    )
    assert "-filter_complex" not in argv
    assert "0:v:0" in [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
    assert "scale_vaapi=w=-2:h=720" in " ".join(argv)


# ---------------------------------------------------------------------------
# 真 ffmpeg 烧录端到端（integration）
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_burn_session_produces_burned_segments(tmp_path):
    """整条真链路：合成 PGS → mux 进 MKV → 烧录转码 → 分片里像素级验证
    字幕在显示窗口内是白色、窗口外与结束后是画面原色。"""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("需要系统 ffmpeg")
    from pgs_sup import make_sup

    sup = tmp_path / "s.sup"
    sup.write_bytes(make_sup())
    movie = tmp_path / "m.mkv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=6",
            "-f", "sup", "-i", str(sup),
            "-map", "0:v", "-map", "1:s",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:s", "copy",
            "-y", str(movie),
        ],
        check=True, capture_output=True, timeout=120,
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    burn = plan(
        PlaybackTier.SOFTWARE_TRANSCODE,
        video=VideoPlan(action="transcode", codec="h264", height=360, burn_subtitle="embedded:0"),
        audio=AudioPlan(action="copy", track_ref=None),
    )
    cmd = build_hls_command(burn, source_path=str(movie), session_dir=session_dir)
    proc = subprocess.run(cmd.argv, capture_output=True, timeout=180)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-800:]

    def pixel(ss: float, x: int, y: int) -> tuple[int, int, int]:
        out = tmp_path / f"f{ss}.png"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(session_dir / "index.m3u8"),
             "-ss", str(ss), "-frames:v", "1", "-y", str(out)],
            check=True, capture_output=True, timeout=60,
        )
        from PIL import Image

        return Image.open(out).convert("RGB").getpixel((x, y))

    inside = pixel(2, 320, 305)   # 字幕矩形中心（1s–4s 显示窗口内）
    outside = pixel(2, 320, 100)  # 同帧、矩形外
    after = pixel(5, 320, 305)    # 字幕结束后同一点
    assert all(c > 220 for c in inside), f"字幕没烧进画面：{inside}"
    assert not all(c > 220 for c in outside), f"矩形外不该是白色：{outside}"
    assert not all(c > 220 for c in after), f"字幕结束后应消失：{after}"
