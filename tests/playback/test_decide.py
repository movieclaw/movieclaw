"""播放决策引擎单测（docs/design/web-player.md §9.2）。

决策引擎是纯函数，因此这里**不碰数据库、不起 ffmpeg**，全部表驱动——
转码没法在 CI 跑真硬件，把决策与执行切开后，判定逻辑这一层能被几十种组合
完整覆盖，跑在 ``pytest -m "not integration"`` 里。

决策错了，用户看到的是「莫名其妙全在转码」或「莫名其妙播不了」，这类回归
靠人工完全兜不住——所以这份清单是整个播放器质量保障的地基。
"""

from __future__ import annotations

import pytest

from movieclaw_playback.capability import (
    AudioSupport,
    ClientCapability,
    VideoSupport,
    universal_capability,
)
from movieclaw_playback.decide import (
    AudioTrack,
    ConsentRequired,
    MediaProfile,
    PlaybackPlan,
    PlaybackPolicy,
    PlaybackRejected,
    PlaybackTier,
    SubtitleTrack,
    decide_playback,
)

# ---------------------------------------------------------------------------
# 浏览器能力档案：固定几份真实浏览器的探测结果，作为判定表的一列
# ---------------------------------------------------------------------------

CHROME_NO_HEVC = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("vp9"), VideoSupport("av1")),
    audio=(AudioSupport("aac"), AudioSupport("opus"), AudioSupport("flac")),
    containers=frozenset({"mp4", "hls-fmp4"}),
)
CHROME_HEVC = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("hevc"), VideoSupport("av1")),
    audio=(AudioSupport("aac"), AudioSupport("flac")),
    containers=frozenset({"mp4", "hls-fmp4"}),
)
SAFARI_MAC = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("hevc")),
    audio=(AudioSupport("aac"), AudioSupport("ac3"), AudioSupport("eac3")),
    containers=frozenset({"mp4", "hls-fmp4"}),
    hdr_passthrough=True,
)
PHONE_SOFT_HEVC = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("hevc", power_efficient=False)),
    audio=(AudioSupport("aac", max_channels=2),),
    containers=frozenset({"mp4", "hls-fmp4"}),
    mse="managed",
    is_mobile=True,
)

WITH_GPU = PlaybackPolicy(hardware_available=True)
NO_GPU = PlaybackPolicy(hardware_available=False)
NO_GPU_SOFT_ON = PlaybackPolicy(hardware_available=False, software_transcode_enabled=True)

AAC_51 = AudioTrack(ref="embedded:1", codec="aac", channels=6, is_default=True)
TRUEHD_71 = AudioTrack(ref="embedded:1", codec="truehd", channels=8, is_default=True)
DTS_51 = AudioTrack(ref="embedded:1", codec="dts", channels=6, is_default=True)
AC3_51 = AudioTrack(ref="embedded:1", codec="ac3", channels=6, is_default=True)


def media(**kwargs) -> MediaProfile:
    """构造一个默认「可直通」的片子，用关键字覆盖要测的那一项。"""
    base = {
        "file_id": 1,
        "container": "mkv",
        "video_codec": "h264",
        "resolution": "1080p",
        "audio_tracks": (AAC_51,),
        "keyframe_interval_s": 4.0,
    }
    base.update(kwargs)
    return MediaProfile(**base)


# ---------------------------------------------------------------------------
# 主判定表：容器 × 编码 × 音轨 × 硬件 → 档位
# ---------------------------------------------------------------------------

CASES = [
    # (用例名, media, capability, policy, 期望档位)
    ("MP4+H264+AAC 原文件直出", media(container="mp4"), CHROME_NO_HEVC, WITH_GPU,
     PlaybackTier.DIRECT_PLAY),
    ("MKV 容器需重封装", media(), CHROME_NO_HEVC, WITH_GPU, PlaybackTier.REMUX),
    ("MKV+HEVC Safari 直通", media(video_codec="hevc"), SAFARI_MAC, WITH_GPU,
     PlaybackTier.REMUX),
    ("MKV+HEVC Chrome 无 HEVC 需转码", media(video_codec="hevc"), CHROME_NO_HEVC,
     WITH_GPU, PlaybackTier.HARDWARE_TRANSCODE),
    ("HEVC+TrueHD 视频直通音频单转", media(video_codec="hevc", audio_tracks=(TRUEHD_71,)),
     SAFARI_MAC, WITH_GPU, PlaybackTier.AUDIO_TRANSCODE),
    ("HEVC+DTS 视频直通音频单转", media(video_codec="hevc", audio_tracks=(DTS_51,)),
     CHROME_HEVC, WITH_GPU, PlaybackTier.AUDIO_TRANSCODE),
    ("AC3 在 Safari 可直通", media(video_codec="hevc", audio_tracks=(AC3_51,)),
     SAFARI_MAC, WITH_GPU, PlaybackTier.REMUX),
    ("AC3 在 Chrome 要转", media(video_codec="hevc", audio_tracks=(AC3_51,)),
     CHROME_HEVC, WITH_GPU, PlaybackTier.AUDIO_TRANSCODE),
    ("VC-1 有显卡走硬件转码", media(video_codec="vc1"), CHROME_NO_HEVC, WITH_GPU,
     PlaybackTier.HARDWARE_TRANSCODE),
    ("VC-1 无显卡但已开软转", media(video_codec="vc1"), CHROME_NO_HEVC, NO_GPU_SOFT_ON,
     PlaybackTier.SOFTWARE_TRANSCODE),
    ("MP4 直出不受关键帧稀疏影响", media(container="mp4", keyframe_interval_s=30.0),
     CHROME_NO_HEVC, WITH_GPU, PlaybackTier.DIRECT_PLAY),
]


@pytest.mark.parametrize(
    "name,profile,capability,policy,expected",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_tier_matrix(name, profile, capability, policy, expected):
    decision = decide_playback(profile, capability, policy)
    assert isinstance(decision, PlaybackPlan), f"{name}: 期望出计划，实际 {decision}"
    assert decision.tier is expected, f"{name}: 期望档 {expected}，实际档 {decision.tier}"
    assert decision.reason, f"{name}: reason 是一等公民字段，不能为空"


# ---------------------------------------------------------------------------
# 恒等快照：Jellyfin 兼容层的行为必须保持不变
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile",
    [
        media(video_codec="hevc", audio_tracks=(TRUEHD_71,), hdr="Dolby Vision"),
        media(video_codec="vc1", container="m2ts", keyframe_interval_s=None),
        media(resolution="2160p", hdr="HDR10"),
    ],
)
def test_universal_capability_always_direct_play(profile):
    """全解码播放器（Infuse/VidHub）恒为档 0——即使是 DV + TrueHD + 长 GOP。

    这一条守住 jellyfin-compat.md §0 硬边界 2：直连侧永不转码。
    """
    decision = decide_playback(profile, universal_capability(), NO_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.DIRECT_PLAY
    assert decision.video.action == "copy"
    assert decision.audio.action == "copy"


# ---------------------------------------------------------------------------
# strm：硬边界 2，只允许直连
# ---------------------------------------------------------------------------


def test_strm_always_direct_play():
    """strm 通常没有探测数据，乐观直连；服务器零流量。"""
    decision = decide_playback(
        MediaProfile(file_id=7, is_strm=True), CHROME_NO_HEVC, WITH_GPU
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.DIRECT_PLAY


def test_strm_never_transcodes_even_after_failure():
    """strm 直连失败后是明确拒绝，绝不偷偷起转码（会打爆网盘流量）。"""
    decision = decide_playback(
        MediaProfile(file_id=7, is_strm=True, video_codec="vc1"),
        CHROME_NO_HEVC,
        WITH_GPU,
        failed_tiers=frozenset({PlaybackTier.DIRECT_PLAY}),
    )
    assert isinstance(decision, PlaybackRejected)
    assert "网盘" in decision.reason
    assert decision.suggestion


# ---------------------------------------------------------------------------
# 软件转码的同意链路（§3.6）
# ---------------------------------------------------------------------------


def test_software_transcode_requires_consent_when_disabled():
    decision = decide_playback(media(video_codec="vc1"), CHROME_NO_HEVC, NO_GPU)
    assert isinstance(decision, ConsentRequired)
    assert decision.tier is PlaybackTier.SOFTWARE_TRANSCODE
    assert decision.setting_namespace == "playback.policy"
    assert decision.setting_key == "software_transcode_enabled"
    assert decision.reason and decision.cost_hint


@pytest.mark.parametrize("can_self_enable", [True, False])
def test_consent_carries_permission_flag(can_self_enable):
    """普通成员看到的应是说明文字而非按钮——不要给一个点了会 403 的按钮。"""
    decision = decide_playback(
        media(video_codec="vc1"),
        CHROME_NO_HEVC,
        NO_GPU,
        can_self_enable=can_self_enable,
    )
    assert isinstance(decision, ConsentRequired)
    assert decision.can_self_enable is can_self_enable


def test_hardware_transcode_needs_no_consent():
    """硬件转码开销低，不打扰用户。"""
    decision = decide_playback(media(video_codec="vc1"), CHROME_NO_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE


# ---------------------------------------------------------------------------
# HDR / Dolby Vision（§7-④：判错就是绿紫画面）
# ---------------------------------------------------------------------------


def test_dolby_vision_always_transcodes_with_tone_map():
    """DV 一律转码 + tone-map：P5 用 IPTPQc2，直通会输出绿紫画面。"""
    decision = decide_playback(media(hdr="Dolby Vision"), SAFARI_MAC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert decision.video.tone_map is True


def test_dolby_vision_without_gpu_is_rejected_not_software_transcoded():
    """软件 tone-map 转 4K 是幻灯片——明确拒绝并说明，不硬撑。"""
    decision = decide_playback(media(hdr="Dolby Vision"), SAFARI_MAC, NO_GPU)
    assert isinstance(decision, PlaybackRejected)
    assert "Dolby Vision" in decision.reason
    assert decision.suggestion


def test_hdr10_passthrough_when_display_supports_it():
    decision = decide_playback(media(video_codec="hevc", hdr="HDR10"), SAFARI_MAC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX
    assert decision.video.tone_map is False


def test_hdr10_tone_maps_when_display_cannot():
    decision = decide_playback(media(video_codec="hevc", hdr="HDR10"), CHROME_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert decision.video.tone_map is True


# ---------------------------------------------------------------------------
# 关键帧密度（§7-②：remux 的分片只能切在 IDR 上）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interval,expected",
    [
        (2.0, PlaybackTier.REMUX),
        (14.9, PlaybackTier.REMUX),
        (20.0, PlaybackTier.HARDWARE_TRANSCODE),   # GOP 太长，直通反而更慢
        (None, PlaybackTier.HARDWARE_TRANSCODE),   # 索引未就绪，保守不赌
    ],
)
def test_keyframe_interval_gates_remux(interval, expected):
    decision = decide_playback(
        media(keyframe_interval_s=interval), CHROME_NO_HEVC, WITH_GPU
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is expected


# ---------------------------------------------------------------------------
# MediaCapabilities 三态：canPlayType 给不出的那两条
# ---------------------------------------------------------------------------


def test_smooth_false_forces_transcode():
    """能解码但会掉帧——canPlayType 会说 probably，实际播成幻灯片。"""
    cap = ClientCapability(
        video=(VideoSupport("hevc", smooth=False),),
        audio=(AudioSupport("aac"),),
        containers=frozenset({"hls-fmp4"}),
    )
    decision = decide_playback(media(video_codec="hevc"), cap, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE


def test_power_efficient_false_only_degrades_on_mobile():
    """软解在桌面可以忍，在手机上是发热掉电。"""
    desktop = ClientCapability(
        video=(VideoSupport("hevc", power_efficient=False),),
        audio=(AudioSupport("aac"),),
        containers=frozenset({"hls-fmp4"}),
    )
    decision = decide_playback(media(video_codec="hevc"), desktop, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX

    decision = decide_playback(media(video_codec="hevc"), PHONE_SOFT_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE


def test_resolution_above_device_ceiling_transcodes():
    cap = ClientCapability(
        video=(VideoSupport("h264", max_height=1080),),
        audio=(AudioSupport("aac"),),
        containers=frozenset({"hls-fmp4"}),
    )
    decision = decide_playback(media(resolution="2160p"), cap, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert decision.video.height == 1080


# ---------------------------------------------------------------------------
# 音轨：换轨优于转码；降混必须显式
# ---------------------------------------------------------------------------


def test_switches_track_instead_of_transcoding():
    """首选轨放不了，但另一条能直通时应换轨——换轨是免费的，转码不是。"""
    profile = media(
        video_codec="hevc",
        audio_tracks=(DTS_51, AudioTrack(ref="embedded:2", codec="aac", channels=6)),
    )
    decision = decide_playback(profile, CHROME_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX
    assert decision.audio.track_ref == "embedded:2"


def test_downmix_marked_when_channels_exceed_device():
    """5.1 → 立体声必须标 downmix：不带中置加权系数，对白会明显偏小。"""
    decision = decide_playback(media(audio_tracks=(AAC_51,)), PHONE_SOFT_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.AUDIO_TRANSCODE
    assert decision.audio.downmix is True
    assert decision.audio.channels == 2


def test_prefers_eac3_to_keep_multichannel():
    """能保多声道就别降混——Safari 支持 EAC3。"""
    decision = decide_playback(
        media(video_codec="hevc", audio_tracks=(TRUEHD_71,)), SAFARI_MAC, WITH_GPU
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.audio.codec == "eac3"
    assert decision.audio.downmix is False


def test_no_audio_track_still_plays():
    decision = decide_playback(media(audio_tracks=()), CHROME_NO_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX


# ---------------------------------------------------------------------------
# 字幕：硬边界 1「绝不烧录」
# ---------------------------------------------------------------------------


def test_subtitle_planning_never_burns_in():
    profile = media(
        subtitle_tracks=(
            SubtitleTrack(ref="embedded:3", codec="ass", language="chi", is_default=True),
            SubtitleTrack(ref="embedded:4", codec="subrip", language="eng"),
            SubtitleTrack(ref="embedded:5", codec="hdmv_pgs_subtitle", language="chi"),
            SubtitleTrack(ref="embedded:6", codec="dvd_subtitle", language="eng"),
        )
    )
    decision = decide_playback(profile, CHROME_NO_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    kinds = {s.track_ref: s.kind for s in decision.subtitles}
    assert kinds == {"embedded:3": "ass", "embedded:4": "vtt", "embedded:5": "pgs"}
    # VobSub 不支持就是不提供——宁可少一条轨，也不烧录（烧录=全转码）
    assert "embedded:6" not in kinds
    # 字幕不影响档位
    assert decision.tier is PlaybackTier.REMUX


# ---------------------------------------------------------------------------
# 降档回路（§6.3）：兜住「看起来兼容、实际 copy 出来是坏流」的源片
# ---------------------------------------------------------------------------


def test_failed_tier_degrades_to_next():
    decision = decide_playback(
        media(), CHROME_NO_HEVC, WITH_GPU, failed_tiers=frozenset({PlaybackTier.REMUX})
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier > PlaybackTier.REMUX
    assert decision.degraded_from is PlaybackTier.REMUX


def test_degrade_still_tries_audio_transcode_after_remux_fails():
    """档 1 失败后先试档 2，不直接跳到转码。

    档 2 同样 ``-c:v copy``，失败原因若在视频码流则会同样失败；但若在音轨，
    档 2 正好修好。多试一档只浪费几秒（一次性），跳过它却可能让本可直通的
    视频永久多转一路（每次播放都付）——这个不对称决定了保留 1→2。
    """
    decision = decide_playback(
        media(), CHROME_NO_HEVC, WITH_GPU, failed_tiers=frozenset({PlaybackTier.REMUX})
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.AUDIO_TRANSCODE


def test_degrade_skips_hardware_tier_without_gpu():
    """没有可用显卡时，降档要越过档 3 直接落档 4。"""
    decision = decide_playback(
        media(),
        CHROME_NO_HEVC,
        NO_GPU_SOFT_ON,
        failed_tiers=frozenset({PlaybackTier.REMUX, PlaybackTier.AUDIO_TRANSCODE}),
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.SOFTWARE_TRANSCODE


def test_all_tiers_failed_is_rejected_with_suggestion():
    decision = decide_playback(
        media(),
        CHROME_NO_HEVC,
        NO_GPU_SOFT_ON,
        failed_tiers=frozenset(
            {
                PlaybackTier.REMUX,
                PlaybackTier.AUDIO_TRANSCODE,
                PlaybackTier.SOFTWARE_TRANSCODE,
            }
        ),
    )
    assert isinstance(decision, PlaybackRejected)
    assert decision.suggestion
