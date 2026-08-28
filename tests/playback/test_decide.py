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
    needs_keyframe_probe,
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
# 无 MSE 的老 iOS：前端会把它交给系统原生播放器（AVPlayer），因此吃 AVPlayer
# 专属的保守限制。mse="none" 是这条分支的**必要条件**，不能省——见下面的
# IOS_MANAGED_MSE。
IOS_NATIVE_HLS = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("hevc")),
    audio=(AudioSupport("aac"), AudioSupport("ac3"), AudioSupport("eac3")),
    containers=frozenset({"hls-fmp4"}),
    mse="none",
    is_mobile=True,
    native_hls=True,
)
# iOS 17+ 的真实形态：具备原生 HLS 能力，但同时暴露 ManagedMediaSource，
# 前端 resolvePlaybackMode 会交给 hls.js。AVPlayer 不参与解码，所以**不该**
# 吃 AVPlayer 的限制。
IOS_MANAGED_MSE = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("hevc")),
    audio=(AudioSupport("aac"), AudioSupport("ac3"), AudioSupport("eac3")),
    containers=frozenset({"mp4", "hls-fmp4"}),
    mse="managed",
    is_mobile=True,
    native_hls=True,
)

WITH_GPU = PlaybackPolicy(hardware_available=True)
NO_GPU = PlaybackPolicy(hardware_available=False)
NO_GPU_SOFT_ON = PlaybackPolicy(hardware_available=False, software_transcode_enabled=True)

AAC_51 = AudioTrack(ref="embedded:1", codec="aac", channels=6, is_default=True)
AAC_STEREO = AudioTrack(ref="embedded:1", codec="aac", channels=2, is_default=True)
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


def test_keyframe_probe_is_only_needed_for_video_copy_tiers():
    """关键帧采样只为 Remux/音频单转服务，转码与原文件直出无需采样。"""
    assert needs_keyframe_probe(media(), CHROME_NO_HEVC, WITH_GPU)
    assert not needs_keyframe_probe(
        media(video_codec="hevc"), CHROME_NO_HEVC, WITH_GPU
    )
    assert not needs_keyframe_probe(
        media(video_codec="hevc", resolution="2160p"), IOS_NATIVE_HLS, WITH_GPU
    )
    assert not needs_keyframe_probe(
        media(container="mp4"), CHROME_NO_HEVC, WITH_GPU
    )
    assert not needs_keyframe_probe(
        media(),
        CHROME_NO_HEVC,
        WITH_GPU,
        failed_tiers=frozenset(
            {PlaybackTier.REMUX, PlaybackTier.AUDIO_TRANSCODE}
        ),
    )


# ---------------------------------------------------------------------------
# MediaCapabilities 三态：canPlayType 给不出的那两条
# ---------------------------------------------------------------------------


def test_smooth_false_still_direct_plays():
    """smooth=false 只是参考，不再抢先转码（§12.15 Jellyfin 对照）。

    实测 Safari 对 HEVC 整族报 smooth=false，同一台设备直通同一文件 0 掉帧。
    预测掉帧就转码等于用一定发生的转码代价对冲一个未必发生的掉帧；真掉帧
    由运行期 framedrop watchdog 带着 failed_tiers 回来降档。
    """
    cap = ClientCapability(
        video=(VideoSupport("hevc", smooth=False),),
        audio=(AudioSupport("aac"),),
        containers=frozenset({"hls-fmp4"}),
    )
    decision = decide_playback(media(video_codec="hevc"), cap, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX


def test_power_efficient_false_no_longer_forces_transcode():
    """powerEfficient=false 同为参考：手机软解费电但能放，视频直通优先。

    这台手机 AAC 只到立体声，5.1 音轨要降混（档 2），但**视频保持 copy**——
    改造前这里是整路视频转码（档 3）。
    """
    decision = decide_playback(media(video_codec="hevc"), PHONE_SOFT_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.AUDIO_TRANSCODE
    assert decision.video.action == "copy"


def test_dropped_frames_feedback_degrades_via_failed_tiers():
    """掉帧兜底的服务端半边：watchdog 逐级报废档位，最终落到真转码。

    档 0/1 失败后引擎先给档 2（视频仍 copy——浏览器归因不可靠，逐级降是
    既定取舍）；档 2 再被掉帧报废，才落到档 3 的视频转码。
    """
    cap = ClientCapability(
        video=(VideoSupport("hevc", smooth=False),),
        audio=(AudioSupport("aac"),),
        containers=frozenset({"hls-fmp4"}),
    )
    step1 = decide_playback(
        media(video_codec="hevc"),
        cap,
        WITH_GPU,
        failed_tiers=frozenset({PlaybackTier.DIRECT_PLAY, PlaybackTier.REMUX}),
    )
    assert isinstance(step1, PlaybackPlan)
    assert step1.tier is PlaybackTier.AUDIO_TRANSCODE
    assert step1.video.action == "copy"

    step2 = decide_playback(
        media(video_codec="hevc"),
        cap,
        WITH_GPU,
        failed_tiers=frozenset(
            {PlaybackTier.DIRECT_PLAY, PlaybackTier.REMUX, PlaybackTier.AUDIO_TRANSCODE}
        ),
    )
    assert isinstance(step2, PlaybackPlan)
    assert step2.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert step2.video.action != "copy"


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


def test_mobile_native_hls_4k_uses_server_transcode_height():
    """iOS 原生 HLS 不把 decodingInfo 的 4K supported 当成长期可播放保证。"""
    cap = ClientCapability(
        video=(VideoSupport("h264"), VideoSupport("hevc")),
        audio=(AudioSupport("aac"),),
        containers=frozenset({"hls-fmp4"}),
        mse="none",
        is_mobile=True,
        native_hls=True,
    )

    decision = decide_playback(
        media(video_codec="hevc", resolution="2160p", bit_depth=10), cap, WITH_GPU
    )

    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert decision.video.action == "transcode"
    assert decision.video.codec == "h264"
    assert decision.video.height == 1080
    assert decision.video.source_bit_depth == 10
    assert "原生 HLS" in decision.reason


def test_managed_mse_iphone_keeps_4k_passthrough():
    """iOS 17+ 走 hls.js，不该吃 AVPlayer 的 1080p 上限。

    前端 resolvePlaybackMode 只在 mse="none" 时才选 native-hls 引擎；决策层
    若只判 native_hls+is_mobile，现代 iPhone 的 4K HEVC 会被无谓降到 1080p，
    而它本来能直通。真播不动由 failed_tiers 回路兜底。
    """
    decision = decide_playback(
        media(video_codec="hevc", resolution="2160p"), IOS_MANAGED_MSE, WITH_GPU
    )

    assert isinstance(decision, PlaybackPlan)
    # 容器仍是 mkv 所以要重封装，但视频码流必须原样带过去、不降分辨率
    assert decision.tier is PlaybackTier.REMUX
    assert decision.video.action == "copy"


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


def test_mobile_native_hls_pins_audio_to_aac_lc_stereo():
    """Safari 原生 HLS 即使声称支持 E-AC-3，也统一输出 AAC-LC 双声道。"""
    decision = decide_playback(
        media(video_codec="h264", audio_tracks=(TRUEHD_71,)),
        IOS_NATIVE_HLS,
        WITH_GPU,
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.AUDIO_TRANSCODE
    assert decision.audio.codec == "aac"
    assert decision.audio.channels == 2
    assert decision.audio.downmix is True
    assert "AAC-LC" in decision.reason


def test_native_hls_keeps_aac_stereo_source():
    """源轨已经是 AAC 双声道时，AVPlayer 那条兜底不该再转一遍。

    它已经是目标格式，重编只是白起一路 ffmpeg，还会把档 0 抬到档 2。
    """
    decision = decide_playback(
        media(container="mp4", video_codec="h264", audio_tracks=(AAC_STEREO,)),
        IOS_NATIVE_HLS,
        WITH_GPU,
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.DIRECT_PLAY


def test_managed_mse_iphone_keeps_aac_stereo_direct_play():
    """iOS 17+ 的 AAC 立体声片子必须保持直出，不能被 AVPlayer 兜底波及。

    回归守卫：曾经只判 native_hls+is_mobile，导致 iPhone 上本可直出的片子
    每次播放都起一路 ffmpeg；关键帧索引未就绪时更会一路抬到软转同意弹窗。
    """
    profile = media(container="mp4", video_codec="h264", audio_tracks=(AAC_STEREO,))
    decision = decide_playback(profile, IOS_MANAGED_MSE, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.DIRECT_PLAY

    # 关键帧索引未就绪 + 无 GPU：档 0 不受关键帧门槛影响，仍应直出而不是
    # 弹「要不要开启软件转码」。
    no_keyframes = media(
        container="mp4",
        video_codec="h264",
        audio_tracks=(AAC_STEREO,),
        keyframe_interval_s=None,
    )
    decision = decide_playback(no_keyframes, IOS_MANAGED_MSE, NO_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.DIRECT_PLAY


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
# 字幕：硬边界 1「默认绝不烧录」
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


def test_subtitle_plan_carries_ai_flag():
    """AI 生成的标记要一路带到播放器——菜单靠它打标（§6.2）。"""
    profile = media(
        subtitle_tracks=(
            SubtitleTrack(ref="external:a.srt", codec="srt", is_external=True, is_ai=True),
            SubtitleTrack(ref="external:b.srt", codec="srt", is_external=True),
        )
    )
    decision = decide_playback(profile, CHROME_NO_HEVC, WITH_GPU)
    assert isinstance(decision, PlaybackPlan)
    assert {s.track_ref: s.is_ai for s in decision.subtitles} == {
        "external:a.srt": True,
        "external:b.srt": False,
    }


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


# ---------------------------------------------------------------------------
# 用户点选音轨（web-player.md §6.9）
# ---------------------------------------------------------------------------

JPN_AAC = AudioTrack(ref="embedded:1", codec="aac", channels=2, language="jpn", is_default=True)
CHI_AAC = AudioTrack(ref="embedded:2", codec="aac", channels=2, language="chi")
CHI_DTS = AudioTrack(ref="embedded:2", codec="dts", channels=6, language="chi")


def test_preferred_audio_is_honoured():
    """用户点了第二条轨就放第二条，不管默认轨是哪条。"""
    decision = decide_playback(
        media(container="mp4", audio_tracks=(JPN_AAC, CHI_AAC)),
        CHROME_HEVC,
        WITH_GPU,
        preferred_audio="embedded:2",
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.audio.track_ref == "embedded:2"


def test_non_default_audio_forces_remux_out_of_direct_play():
    """直出档整份文件交给 <video>，浏览器只放默认轨——选了别的轨就必须重封装。

    这一条不只是新功能的边界：不加这个判定，「自动换轨」也会悄悄落进档 0，
    结果是决策说放第二条、用户听到的还是第一条。
    """
    profile = media(container="mp4", audio_tracks=(JPN_AAC, CHI_AAC))
    # 不点选：默认轨能直通 → mp4 容器走直出
    assert decide_playback(profile, CHROME_HEVC, WITH_GPU).tier is PlaybackTier.DIRECT_PLAY
    # 点选第二条 → 必须降到 remux 才能把它 map 出来
    picked = decide_playback(profile, CHROME_HEVC, WITH_GPU, preferred_audio="embedded:2")
    assert picked.tier is PlaybackTier.REMUX
    assert picked.audio.track_ref == "embedded:2"
    assert "默认音轨" in picked.reason


def test_picking_default_audio_keeps_direct_play():
    """点选的就是默认轨时不该白白掉档。"""
    decision = decide_playback(
        media(container="mp4", audio_tracks=(JPN_AAC, CHI_AAC)),
        CHROME_HEVC,
        WITH_GPU,
        preferred_audio="embedded:1",
    )
    assert decision.tier is PlaybackTier.DIRECT_PLAY


def test_preferred_audio_transcodes_instead_of_switching_away():
    """用户点的轨放不了就转它，**绝不能**替他换回另一条能直通的。

    自动换轨是「用户没表态时帮他找一条能放的」；他表了态还替他改，等于点了没用。
    """
    decision = decide_playback(
        media(audio_tracks=(JPN_AAC, CHI_DTS)),
        CHROME_HEVC,
        WITH_GPU,
        preferred_audio="embedded:2",
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.audio.track_ref == "embedded:2"
    assert decision.audio.action == "transcode"
    assert decision.tier is PlaybackTier.AUDIO_TRANSCODE


def test_unknown_preferred_audio_falls_back_to_automatic():
    """点选的轨不在这个文件里（换了版本文件）：退回自动选轨，不能报错也不能静音。"""
    decision = decide_playback(
        media(container="mp4", audio_tracks=(JPN_AAC,)),
        CHROME_HEVC,
        WITH_GPU,
        preferred_audio="embedded:9",
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.audio.track_ref == "embedded:1"
    assert decision.tier is PlaybackTier.DIRECT_PLAY


def test_plan_carries_candidate_audio_tracks():
    """候选列表由决策层给：前端要靠它渲染音轨菜单，不该再回头查文件详情。"""
    decision = decide_playback(
        media(audio_tracks=(JPN_AAC, CHI_AAC)), CHROME_HEVC, WITH_GPU
    )
    assert isinstance(decision, PlaybackPlan)
    assert [t.ref for t in decision.audio_tracks] == ["embedded:1", "embedded:2"]
    assert [t.language for t in decision.audio_tracks] == ["jpn", "chi"]


# ---------------------------------------------------------------------------
# 用户画质上限（§10「手动选清晰度」：弱网救急）
# ---------------------------------------------------------------------------


def test_quality_cap_forces_transcode_when_source_exceeds():
    """源 2160p、用户限 720p：即使编码可直通也必须转码降下去。"""
    plan = decide_playback(
        media(resolution="2160p"), CHROME_NO_HEVC, WITH_GPU, max_height=720
    )
    assert isinstance(plan, PlaybackPlan)
    assert plan.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert plan.video.action == "transcode"
    assert plan.video.height == 720


def test_quality_cap_keeps_direct_when_source_within_limit():
    """上限是上限不是目标：1080p 源限 1080p，照常直通（无损优于转码）。"""
    plan = decide_playback(
        media(resolution="1080p"), CHROME_NO_HEVC, WITH_GPU, max_height=1080
    )
    assert isinstance(plan, PlaybackPlan)
    assert plan.tier is PlaybackTier.REMUX
    assert plan.video.action == "copy"


def test_quality_cap_combines_with_admin_limit():
    """用户上限与管理员 max_transcode_height 取更小的那个。"""
    policy = PlaybackPolicy(hardware_available=True, max_transcode_height=720)
    plan = decide_playback(
        media(resolution="2160p"), CHROME_NO_HEVC, policy, max_height=1080
    )
    assert isinstance(plan, PlaybackPlan)
    assert plan.video.height == 720


def test_quality_cap_does_not_touch_strm():
    """strm 禁止转码（硬边界 2），画质上限对它不生效。"""
    plan = decide_playback(
        media(is_strm=True, container="strm", resolution="2160p"),
        CHROME_NO_HEVC,
        WITH_GPU,
        max_height=480,
    )
    assert isinstance(plan, PlaybackPlan)
    assert plan.tier is PlaybackTier.DIRECT_PLAY


def test_copy_audio_plan_carries_codec_and_channels():
    """直通计划也要带源轨 codec/声道——诊断面板靠它回答「直通的是什么」。

    不带的表现就是面板上那格「直通 · 未知」；与视频侧当年 hvc1 打标的坑同构
    （copy 计划不带 codec，下游要用时已经没有了）。
    """
    decision = decide_playback(
        media(container="mp4", audio_tracks=(AC3_51,)), SAFARI_MAC, WITH_GPU
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.audio.action == "copy"
    assert decision.audio.codec == "ac3"
    assert decision.audio.channels == 6

    universal_decision = decide_playback(
        media(audio_tracks=(AC3_51,)), universal_capability(), WITH_GPU
    )
    assert isinstance(universal_decision, PlaybackPlan)
    assert universal_decision.audio.codec == "ac3"

    strm_decision = decide_playback(
        media(is_strm=True, audio_tracks=(AC3_51,)), CHROME_HEVC, WITH_GPU
    )
    assert isinstance(strm_decision, PlaybackPlan)
    assert strm_decision.audio.codec == "ac3"


# ---------------------------------------------------------------------------
# PGS 烧录：硬边界 1 的唯一例外，必须由用户显式选中触发（Emby 语义）
# ---------------------------------------------------------------------------

PGS_MEDIA_KW = dict(
    subtitle_tracks=(
        SubtitleTrack(ref="embedded:0", codec="subrip", language="eng"),
        SubtitleTrack(ref="embedded:1", codec="hdmv_pgs_subtitle", language="chi"),
    )
)


def test_selecting_pgs_burns_and_forces_transcode():
    """选中 PGS → 视频转码 + burn_subtitle；有显卡落档 3。"""
    decision = decide_playback(
        media(**PGS_MEDIA_KW), CHROME_NO_HEVC, WITH_GPU, preferred_subtitle="embedded:1"
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert decision.video.action == "transcode"
    assert decision.video.burn_subtitle == "embedded:1"
    assert "压制" in decision.reason
    # 被烧的轨仍在旁挂清单里：前端菜单要靠它渲染选中态
    assert any(s.track_ref == "embedded:1" for s in decision.subtitles)


def test_selecting_pgs_without_gpu_requires_consent():
    """无显卡时烧录落到软件转码——沿用既有的同意链路，不静默烧 CPU。"""
    decision = decide_playback(
        media(**PGS_MEDIA_KW), CHROME_NO_HEVC, NO_GPU, preferred_subtitle="embedded:1"
    )
    assert isinstance(decision, ConsentRequired)


@pytest.mark.parametrize("selected", [None, "off", "embedded:0"])
def test_text_or_no_subtitle_never_changes_video_policy(selected):
    """文本轨 / 关字幕 / 不选：视频策略完全不变（默认硬边界照旧）。"""
    decision = decide_playback(
        media(**PGS_MEDIA_KW), CHROME_NO_HEVC, WITH_GPU, preferred_subtitle=selected
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX
    assert decision.video.action == "copy"
    assert decision.video.burn_subtitle is None


def test_stale_pgs_ref_is_ignored():
    """引用不在本文件里（换了版本轨序变了）→ 宁可不烧也不能烧错轨。"""
    decision = decide_playback(
        media(**PGS_MEDIA_KW), CHROME_NO_HEVC, WITH_GPU, preferred_subtitle="embedded:9"
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.video.burn_subtitle is None
    assert decision.tier is PlaybackTier.REMUX


def test_strm_ignores_pgs_selection():
    """strm 禁转码（硬边界 2）优先级更高：选中 PGS 也不烧，仍旧直连。"""
    decision = decide_playback(
        media(is_strm=True, container="mp4", **PGS_MEDIA_KW),
        CHROME_NO_HEVC,
        WITH_GPU,
        preferred_subtitle="embedded:1",
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.video.action == "copy"
    assert decision.video.burn_subtitle is None


def test_unrelated_failure_history_is_not_marked_as_degraded():
    """带着「档 1 失败」的历史来开烧录会话：目标本来就是档 3，与那次失败
    无关，不能标 degraded_from——否则诊断面板给毫无关系的会话挂红字。"""
    decision = decide_playback(
        media(**PGS_MEDIA_KW),
        CHROME_NO_HEVC,
        WITH_GPU,
        preferred_subtitle="embedded:1",
        failed_tiers=frozenset({PlaybackTier.REMUX}),
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert decision.degraded_from is None
    assert "已自动降档" not in decision.reason


def test_real_degrade_still_marks_the_origin_tier():
    """真被 failed_tiers 顶下来的会话要标起点档位（§6.3 的既有语义不变）。"""
    decision = decide_playback(
        media(),  # 默认可 REMUX
        CHROME_NO_HEVC,
        WITH_GPU,
        failed_tiers=frozenset({PlaybackTier.REMUX, PlaybackTier.AUDIO_TRANSCODE}),
    )
    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.HARDWARE_TRANSCODE
    assert decision.degraded_from is PlaybackTier.REMUX
    assert "已自动降档" in decision.reason
