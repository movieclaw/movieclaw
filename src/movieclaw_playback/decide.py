"""播放决策引擎（docs/design/web-player.md §3）——**纯函数，零 IO**。

一句话原则：**尽可能不转码，而不是转码转得多快。**

浏览器的解码能力是残废的（MKV 容器全不支持、AC3/DTS 基本不支持、HEVC 看
硬件脸色），而 PT 片源恰恰是「MKV + HEVC + DTS-HD + ASS」重灾区。本模块的
全部复杂度都指向同一件事：把尽可能多的播放，落到不重编码的档位上。

五档降级阶梯（``PlaybackTier``）::

    档 0 Direct Play   原文件直出 + Range          零开销，无损
    档 1 Remux 直通    容器重封装，码流逐字节不变   仅 IO，无损   ← PT 主力
    档 2 音频单转      -c:v copy + 转音频           单核 5%      ← PT 次主力
    档 3 硬件转码      GPU 解+编，含 HDR tone-map   低
    档 4 软件转码      libx264                      高，默认关闭

**为什么是纯函数**：转码没法在 CI 里跑真硬件，把决策与执行切开是质量保障的
唯一支点——本模块可以被几十种组合表驱动单测覆盖，跑在
``pytest -m "not integration"`` 里；起 ffmpeg 的执行器则标 ``integration``。
因此这里**不许**出现文件读写、数据库访问或 ffprobe 调用；所有输入都由调用方
装配成 ``MediaProfile`` / ``ClientCapability`` / ``PlaybackPolicy`` 传进来。

三态返回（``PlaybackDecision``）：软件转码需要用户显式同意（§3.6），拒绝也要
带可读理由（硬边界 5「失败必须可读」），所以返回的不是单一的 plan。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from movieclaw_playback.capability import ClientCapability

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 档 1/2 的分片只能切在源片已有的 IDR 上（web-player.md §7-②）。关键帧间隔
#: 超过这个秒数时，remux 的首帧优势消失（要等一个超长 GOP），不如直接转码。
MAX_KEYFRAME_INTERVAL_S = 15.0

#: 能被 ``<video src>`` 直接消费的容器。其余（mkv/ts/m2ts…）都要 remux。
DIRECT_PLAY_CONTAINERS = frozenset({"mp4", "m4v", "mov"})

#: 归一化分辨率标签 → 高度。media_probe 只落标签不落 width/height。
_RESOLUTION_HEIGHT = {
    "4320p": 4320, "2160p": 2160, "1440p": 1440,
    "1080p": 1080, "720p": 720, "576p": 576, "480p": 480,
}

#: 文本字幕：可转 WebVTT 交给 <track> 原生渲染。
_TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "mov_text", "webvtt", "text"})
#: ASS/SSA：原样下发交给 JASSUB（libass WASM）渲染，保留特效。
_ASS_SUBTITLE_CODECS = frozenset({"ass", "ssa"})
#: PGS 位图字幕：默认前端 libbitsub 解码；用户显式选中时烧录（硬边界 1 的
#: 唯一例外，Emby 语义，见 decide_playback 的 preferred_subtitle）。
_PGS_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "pgs"})

_POLICY_NAMESPACE = "playback.policy"
_SOFTWARE_TRANSCODE_KEY = "software_transcode_enabled"


class PlaybackTier(IntEnum):
    """五档降级阶梯。数值即优先级——越小越好，决策永远取能成立的最小值。"""

    DIRECT_PLAY = 0
    REMUX = 1
    AUDIO_TRANSCODE = 2
    HARDWARE_TRANSCODE = 3
    SOFTWARE_TRANSCODE = 4


# ---------------------------------------------------------------------------
# 输入侧值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioTrack:
    """一条音轨。``ref`` 是协议无关的中性引用（``embedded:<k>``），与
    ``playback_state.audio_track`` 同源——网页播放器直接复用既有的轨选择记忆，
    不新增字段、不需要迁移（jellyfin-subtitle.md §3.3）。"""

    ref: str
    codec: str | None = None
    channels: int | None = None
    language: str | None = None
    is_default: bool = False


@dataclass(frozen=True)
class SubtitleTrack:
    """一条字幕轨（内封或外挂）。"""

    ref: str
    codec: str | None = None
    language: str | None = None
    is_default: bool = False
    is_external: bool = False
    #: 本机 AI 生成的字幕（翻译/双语）。播放器要据此打标——AI 字幕的译文与
    #: 时间轴都可能有偏差，用户有权在选之前就知道这条是机器产的。
    is_ai: bool = False


@dataclass(frozen=True)
class MediaProfile:
    """待播文件的客观规格——全部来自 ffprobe 落库的真值，不从文件名猜。

    字段与 ``LibraryFile`` 一一对应；``keyframe_interval_s`` 是新增探测
    （web-player.md §3.5），未知时为 None，此时保守不走 remux。
    """

    file_id: int
    container: str | None = None
    video_codec: str | None = None
    resolution: str | None = None
    hdr: str | None = None  # None=SDR / "HDR10" / "HLG" / "HDR10+" / "Dolby Vision"
    bit_depth: int | None = None
    duration_ms: int | None = None
    audio_tracks: tuple[AudioTrack, ...] = ()
    subtitle_tracks: tuple[SubtitleTrack, ...] = ()
    keyframe_interval_s: float | None = None
    is_strm: bool = False

    @property
    def height(self) -> int | None:
        """由归一化分辨率标签反推高度；标签缺失或不认识返回 None。"""
        return _RESOLUTION_HEIGHT.get(self.resolution or "")


@dataclass(frozen=True)
class PlaybackPolicy:
    """服务端策略（``app_setting`` 的 ``playback.policy`` 配置域，零迁移）。"""

    #: 软件转码默认关闭：低配 NAS 上一路 1080p 软转就能吃满 CPU，连带拖慢
    #: 搜索、扫描、订阅——用户感知到的是「整个应用变卡」，却不会联想到是自己
    #: 点了播放。需要它时按 §3.6 弹窗询问并永久保存，不做「仅本次允许」。
    software_transcode_enabled: bool = False
    #: 硬件自检（§5.2）的结论。为 False 时档 3 不可用，只能落档 4。
    hardware_available: bool = False
    #: 转码输出的高度上限，超出则降分辨率。
    max_transcode_height: int = 1080


# ---------------------------------------------------------------------------
# 输出侧：三态决策
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoPlan:
    action: str  # "copy" | "transcode"
    codec: str | None = None  # transcode 时的目标编码
    height: int | None = None  # transcode 时的目标高度
    #: 源视频位深（来自 ffprobe）。VideoToolbox 从硬件帧下载前要据此选择
    #: 8-bit 的 NV12 或 10-bit 的 P010；未知时由命令装配层安全回退软件解码。
    source_bit_depth: int | None = None
    tone_map: bool = False  # HDR → SDR
    #: 要烧录进画面的字幕轨（中性引用，只会是内封 PGS）。非空即 Emby 语义的
    #: 「字幕压制」：用户显式选中位图字幕，视频整路转码、字幕合成进帧——
    #: 这是**硬边界 1 唯一的例外**，且必须由用户的选择触发，决策器绝不主动。
    #: 只在 action="transcode" 时出现（烧录本身就要求转码）。
    burn_subtitle: str | None = None


@dataclass(frozen=True)
class AudioPlan:
    action: str  # "copy" | "transcode"
    track_ref: str | None = None
    codec: str | None = None
    channels: int | None = None
    #: 多声道降混到立体声。必须带中置声道加权系数，否则对白明显偏小——
    #: 「音效很响但听不清台词」是用户投诉第一名（web-player.md §7-⑤）。
    downmix: bool = False


@dataclass(frozen=True)
class SubtitlePlan:
    track_ref: str
    kind: str  # "vtt" | "ass" | "pgs"
    language: str | None = None
    is_default: bool = False
    #: 本机 AI 生成（见 SubtitleTrack.is_ai），原样透传给播放器打标
    is_ai: bool = False


@dataclass(frozen=True)
class PlaybackPlan:
    """一次播放的完整计划。"""

    tier: PlaybackTier
    file_id: int
    container: str  # "mp4" | "hls-fmp4"
    video: VideoPlan
    audio: AudioPlan
    subtitles: tuple[SubtitlePlan, ...] = ()
    #: 这个文件里全部可选音轨。与 ``subtitles`` 同理：**候选列表由决策层给**，
    #: 前端不再回头去查文件详情——播放器只认「一次决策 + 一次会话」这一组
    #: 数据，多一次往返就多一次能不一致的机会。
    audio_tracks: tuple[AudioTrack, ...] = ()
    #: ★ 中文，为什么是这个档。**一等公民字段**，不是调试信息：同时供给
    #: 诊断面板与失败提示（硬边界 5）。
    reason: str = ""
    #: 若为自动降档的结果，记录原档位（§6.3）。
    degraded_from: PlaybackTier | None = None


@dataclass(frozen=True)
class ConsentRequired:
    """决策落到需要用户显式同意的档位（当前只有档 4 软件转码）。"""

    tier: PlaybackTier
    reason: str
    cost_hint: str
    can_self_enable: bool
    setting_namespace: str = _POLICY_NAMESPACE
    setting_key: str = _SOFTWARE_TRANSCODE_KEY


@dataclass(frozen=True)
class PlaybackRejected:
    """放不了。``suggestion`` 必须给出可操作的下一步。"""

    reason: str
    suggestion: str


PlaybackDecision = PlaybackPlan | ConsentRequired | PlaybackRejected


# ---------------------------------------------------------------------------
# 决策主流程
# ---------------------------------------------------------------------------


def decide_playback(
    media: MediaProfile,
    capability: ClientCapability,
    policy: PlaybackPolicy,
    *,
    can_self_enable: bool = False,
    failed_tiers: frozenset[PlaybackTier] = frozenset(),
    preferred_audio: str | None = None,
    preferred_subtitle: str | None = None,
    max_height: int | None = None,
) -> PlaybackDecision:
    """按 web-player.md §3.3 的判定顺序算出最优播放计划。

    ``failed_tiers`` 承载运行期降档回路（§6.3）：前端播放失败后带着已失败的
    档位重新决策，本函数据此跳过它们。这条回路比穷举「看起来兼容、实际
    copy 出来是坏流」的边界情况（MKV header compression、参数集只在
    CodecPrivate、开放 GOP……）现实得多。

    ``can_self_enable`` 是当前成员能否修改全局设置（只有超管可以）。为 False
    时 ``ConsentRequired`` 由前端渲染成说明文字而非按钮——不要给一个点了会
    403 的按钮。

    ``max_height`` 是用户在播放器里选的画质上限（§10 预案的「手动选清晰度」，
    弱网救急用）。语义是**上限而不是目标**：源分辨率不超它就照常直通——
    直通是无损的，比转出来的同分辨率画质更好也更省资源；超了才强制转码降
    到该高度。None = 自动（不限制）。strm 与 universal 分支不受它影响：
    前者禁止转码（硬边界 2），后者是全解码客户端自己拉流。

    ``preferred_subtitle`` 是**用户选中的字幕轨**（None/"off" 与文本轨都是
    no-op）。只有当它指向本文件的**内封 PGS 轨**时才改变决策：视频强制转码、
    字幕烧录进画面（Emby 语义的「字幕压制」）。这是硬边界 1 唯一的例外，
    由用户的显式选择触发；文本字幕永远旁挂，视频策略不因它变。strm 与
    universal 分支同样不受影响——前者禁转码，位图字幕退回前端 canvas 渲染。
    """
    # 1. strm 网盘条目：硬规则分支，只允许直连（硬边界 2）。
    #    一旦允许转码，服务端就得先把云端内容拉下来再转，直接推翻
    #    strm-workflow.md 的「零网盘流量」，且用户点一部网盘 4K 就能同时打爆
    #    NAS 上行与网盘流量额度，还完全无感。
    if media.is_strm:
        return _decide_strm(media, failed_tiers)

    # 2. 恒等快照（全解码播放器）：永远直连，与 jellyfin-compat.md 行为一致。
    if capability.universal:
        return PlaybackPlan(
            tier=PlaybackTier.DIRECT_PLAY,
            file_id=media.file_id,
            container="mp4",
            video=VideoPlan(action="copy"),
            audio=_copy_audio_plan(
                _preferred_audio(media.audio_tracks) if media.audio_tracks else None
            ),
            subtitles=(),
            audio_tracks=media.audio_tracks,
            reason="播放器自述具备完整解码能力，原文件直连播放",
        )

    # 3–5. 视频、HDR、音频三项判定，各自算出「能不能 copy」。
    video_verdict = _judge_video(media, capability, policy, max_height)
    audio_verdict = _judge_audio(media, capability, preferred_audio)

    # 用户显式选中内封 PGS → 烧录：视频不再允许 copy（硬边界 1 唯一例外）。
    # blocked（如 HDR 无 GPU）原样保留——烧录也救不了放不出来的画面。
    burn_track = _burn_target(media, preferred_subtitle)
    if burn_track is not None and video_verdict.blocked is None:
        video_verdict = _VideoVerdict(
            can_copy=False,
            reason="已选中 PGS 图形字幕，按你的选择转码并把字幕压制进画面",
            tone_map=video_verdict.tone_map,
        )

    # 6–7. 综合定档。
    tier, container, reason, degraded_from = _resolve_tier(
        media, video_verdict, audio_verdict, policy, failed_tiers
    )
    if isinstance(tier, PlaybackRejected):
        return tier

    # 档 4 且开关未开 → 要用户同意，不直接出计划。
    if tier is PlaybackTier.SOFTWARE_TRANSCODE and not policy.software_transcode_enabled:
        return ConsentRequired(
            tier=tier,
            reason=reason,
            cost_hint=(
                "软件转码会占用大量 CPU，可能让搜索、扫描、订阅等后台任务明显"
                "变慢，首帧也需要更长时间。"
            ),
            can_self_enable=can_self_enable,
        )

    # 8. 字幕规划。文本轨不影响档位；PGS 只在用户显式选中时通过烧录改档
    #    （已并入上面的 video_verdict）。
    subtitles = plan_subtitles(media)

    return PlaybackPlan(
        tier=tier,
        file_id=media.file_id,
        container=container,
        video=_build_video_plan(
            media,
            video_verdict,
            tier,
            policy,
            max_height,
            burn_subtitle=burn_track.ref if burn_track is not None else None,
        ),
        audio=_build_audio_plan(media, audio_verdict, tier),
        subtitles=subtitles,
        audio_tracks=media.audio_tracks,
        reason=reason,
        degraded_from=degraded_from,
    )


# ---------------------------------------------------------------------------
# 分项判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _VideoVerdict:
    can_copy: bool
    reason: str
    tone_map: bool = False
    #: 无法直通且连转码也不该做（如 HDR 需 tone-map 但没有 GPU）
    blocked: str | None = None


@dataclass(frozen=True)
class _AudioVerdict:
    can_copy: bool
    track: AudioTrack | None
    reason: str
    target_codec: str | None = None
    target_channels: int | None = None
    downmix: bool = False
    #: 选中的轨不是容器里默认播的那条。**直出档下浏览器只会放默认轨**，
    #: 我们选的那条根本到不了用户耳朵里，所以这种情况必须至少走 remux，
    #: 让 ffmpeg 把选中的轨 `-map` 出来。
    needs_remap: bool = False


def _judge_video(
    media: MediaProfile,
    capability: ClientCapability,
    policy: PlaybackPolicy,
    max_height: int | None = None,
) -> _VideoVerdict:
    """视频编码 + HDR + 用户画质上限，三项判定（§3.3 步骤 3–4）。"""
    support = capability.video_support(media.video_codec)
    codec_label = (media.video_codec or "未知").upper()

    if support is None:
        return _VideoVerdict(
            can_copy=False, reason=f"视频编码 {codec_label} 浏览器不支持"
        )
    # smooth / power_efficient **只作参考，不再作硬闸**（§12.15 Jellyfin 对照）。
    # 实测 Safari 对 HEVC 会整族报 smooth=false，而同一台设备直通同一文件
    # 0 掉帧——decodingInfo 的预测是对"假想负载"（我们探测用 25 Mbps）的回答，
    # 与真实文件（可能只有 3 Mbps）对不上，两个方向都不可信。Jellyfin 压根
    # 不做流畅度预测（canPlayType 二值声明），靠出错回退兜底；我们保留更强的
    # 兜底：真实掉帧率超阈值时运行期降档（lib/player/framedrop.ts）。
    # 预测掉帧就抢先转码，等于用一定发生的转码代价去对冲一个未必发生的掉帧。
    height = media.height
    if height is not None and height > support.max_height:
        return _VideoVerdict(
            can_copy=False,
            reason=f"{media.resolution} 超出设备可解码的分辨率，已降分辨率",
        )
    # 原生 HLS 会把 fMP4 交给 AVPlayer。Safari 的能力探测只代表「存在
    # 某种可能的解码路径」，不能证明 4K 长时间播放不会触发硬件解码失败；
    # 因此沿用服务端现有 1080p 转码上限，先把视频码流变成稳定的 H.264
    # 8-bit 输出，再交给 AVPlayer。走 hls.js 的设备（含 ManagedMediaSource
    # 的现代 iPhone）不进这条分支——见 uses_native_hls_player——仍由真实
    # 播放失败后的 failed_tiers 回路处理。
    if (
        capability.uses_native_hls_player
        and height is not None
        and height > policy.max_transcode_height
    ):
        return _VideoVerdict(
            can_copy=False,
            reason=(
                f"移动端原生 HLS 播放限制为 {policy.max_transcode_height}p，"
                "已转码降分辨率"
            ),
        )
    # 用户选了画质上限且源超出：强制转码降下去。放在设备判定之后——
    # 两条都命中时归因给设备（那是硬性的，用户上限只是偏好）
    if max_height is not None and height is not None and height > max_height:
        return _VideoVerdict(
            can_copy=False,
            reason=f"按所选画质上限 {max_height}p 转码（源为 {media.resolution}）",
        )

    # HDR 判定。Dolby Vision 一律转码 + tone-map：DV Profile 5 用 IPTPQc2 色彩
    # 空间，当成普通 HDR10 直通会输出**绿紫画面**（§7-④）；而 media_probe 目前
    # 只落 "Dolby Vision" 不落 profile，分不出 P5 与自带 HDR10 基础层的 P8，
    # 因此保守全转。待探测层补齐 dv_profile 后可放开 P8 直通。
    if media.hdr == "Dolby Vision":
        if not policy.hardware_available:
            return _VideoVerdict(
                can_copy=False,
                reason="Dolby Vision 需要转换色彩空间才能正确显示",
                blocked=(
                    "这部片是 Dolby Vision，需要显卡做色调映射才能正常播放，"
                    "但未检测到可用的硬件加速设备。软件色调映射转 4K 太慢，"
                    "不予启用。"
                ),
            )
        return _VideoVerdict(
            can_copy=False, reason="Dolby Vision 已转换为 SDR 显示", tone_map=True
        )

    if media.hdr and not capability.hdr_passthrough:
        if not policy.hardware_available:
            return _VideoVerdict(
                can_copy=False,
                reason=f"{media.hdr} 需要转换为 SDR",
                blocked=(
                    f"这部片是 {media.hdr}，当前设备不支持 HDR 显示，需要显卡做"
                    "色调映射；但未检测到可用的硬件加速设备。"
                ),
            )
        return _VideoVerdict(
            can_copy=False, reason=f"{media.hdr} 已转换为 SDR 显示", tone_map=True
        )

    hdr_note = f"（{media.hdr} 直通）" if media.hdr else ""
    return _VideoVerdict(can_copy=True, reason=f"视频编码 {codec_label} 可直通{hdr_note}")


def _judge_audio(
    media: MediaProfile,
    capability: ClientCapability,
    preferred_audio: str | None = None,
) -> _AudioVerdict:
    """音轨判定（§3.3 步骤 5）：先找能直通的轨，找不到才转。

    ``preferred_audio`` 是**用户在播放器里点选的轨**。给了就认它，不再自动
    换轨——自动换轨是「用户没表态时帮他找一条能放的」，用户表了态还替他改
    等于点了没用。它放不了就转它，也不去动别的轨。
    """
    tracks = media.audio_tracks
    if not tracks:
        return _AudioVerdict(can_copy=True, track=None, reason="无音轨")

    default = _preferred_audio(tracks)
    chosen = next((t for t in tracks if t.ref == preferred_audio), None)

    # 原生 HLS（AVPlayer）的解码链比 MSE/hls.js 更挑剔：即使能力探测报告
    # E-AC-3 或多声道 AAC 可用，放进 fMP4 后仍可能在 init/首片阶段直接报
    # MEDIA_ERR_DECODE。保留用户选中的轨（或默认轨），但统一重编码成
    # AAC-LC 双声道，给 AVPlayer 一个确定的、跨 iOS 版本稳定的音频格式。
    #
    # 例外：源轨本身就是 AAC 双声道时，它已经是这条分支想要的目标格式，
    # 再编一遍纯属白烧一路 ffmpeg——而且会把本可直出（档 0）的片子抬进音频
    # 转码档，关键帧索引未就绪时甚至一路抬到软转同意弹窗。
    if capability.uses_native_hls_player:
        track = chosen or default
        channels = track.channels or 2
        if (track.codec or "").lower() == "aac" and channels <= 2:
            return _AudioVerdict(
                can_copy=True,
                track=track,
                reason=f"音轨 {_track_label(track)} 已是 AAC-LC 双声道，可直通",
                needs_remap=track.ref != default.ref,
            )
        return _transcode_audio(
            track,
            capability,
            prefix=f"移动端原生 HLS 音轨 {_track_label(track)}",
            force_aac=True,
        )

    if chosen is not None:
        support = capability.audio_support(chosen.codec)
        channels = chosen.channels or 2
        label = _track_label(chosen)
        if support is not None and channels <= support.max_channels:
            return _AudioVerdict(
                can_copy=True,
                track=chosen,
                reason=f"音轨 {label} 可直通",
                needs_remap=chosen.ref != default.ref,
            )
        return _transcode_audio(chosen, capability, prefix=f"选中的音轨 {label}")

    # 用户没表态：首选轨能直通最好；否则在其它轨里找一条能直通的（换轨优于转码）。
    for track, is_preferred in ((default, True), *((t, False) for t in tracks)):
        support = capability.audio_support(track.codec)
        if support is None:
            continue
        channels = track.channels or 2
        if channels > support.max_channels:
            continue  # 声道数超限要降混，那是转码，留给下面
        note = "" if is_preferred else "，已切换到可直通的音轨"
        return _AudioVerdict(
            can_copy=True,
            track=track,
            reason=f"音轨 {(track.codec or '未知').upper()} 可直通{note}",
            needs_remap=track.ref != default.ref,
        )

    # 都不能直通 → 转码首选轨。
    return _transcode_audio(default, capability, prefix=f"音轨 {(default.codec or '未知').upper()}")


def _transcode_audio(
    track: AudioTrack,
    capability: ClientCapability,
    *,
    prefix: str,
    force_aac: bool = False,
) -> _AudioVerdict:
    source_channels = track.channels or 2
    if force_aac:
        # 原生 HLS 的兼容性优先于保留多声道；显式输出双声道，即使源轨是单声道
        # 也让后续的 HLS CODECS/音频参数保持固定，避免设备分支再次漂移。
        target_codec = "aac"
        target_channels = 2
        reason = f"{prefix}已固定为 AAC-LC 双声道"
    else:
        eac3 = capability.audio_support("eac3")
        if eac3 is not None and source_channels > 2:
            target_codec, max_channels = "eac3", eac3.max_channels
        else:
            aac = capability.audio_support("aac")
            target_codec = "aac"
            max_channels = aac.max_channels if aac else 2
        target_channels = min(source_channels, max_channels)
        reason = f"{prefix} 浏览器不支持，已转为 {target_codec.upper()}"
    return _AudioVerdict(
        can_copy=False,
        track=track,
        reason=reason,
        target_codec=target_codec,
        target_channels=target_channels,
        downmix=target_channels < source_channels,
    )


def _track_label(track: AudioTrack) -> str:
    """音轨的中文短标签：语言 + 编码 + 声道，缺项自动省略。"""
    bits = [track.language] if track.language else []
    bits.append((track.codec or "未知").upper())
    if track.channels:
        bits.append(f"{track.channels} 声道")
    return " ".join(bits)


def _resolve_tier(
    media: MediaProfile,
    video: _VideoVerdict,
    audio: _AudioVerdict,
    policy: PlaybackPolicy,
    failed_tiers: frozenset[PlaybackTier],
    *,
    enforce_keyframe_gate: bool = True,
) -> tuple[PlaybackTier | PlaybackRejected, str, str, PlaybackTier | None]:
    """综合定档（§3.3 步骤 6–7）。返回 (档位或拒绝, 容器, 中文理由,
    降档起点)。

    降档起点只在**本次真的被 failed_tiers 顶下来**时非空——曾经失败过但
    本次目标档与失败无关（比如带着「档 1 失败」的历史来开烧录会话，目标
    本来就是档 3）不算降档。以前无条件标 min(failed_tiers)，诊断面板会给
    毫无关系的会话挂上「上一档播放失败，自动降档而来」的红字（真机踩中）。
    """
    if video.blocked:
        return (
            PlaybackRejected(
                reason=video.blocked,
                suggestion="可以用 Infuse、VidHub 等第三方播放器直连播放这部片。",
            ),
            "",
            "",
            None,
        )

    reason = f"{video.reason}；{audio.reason}"

    if video.can_copy and audio.can_copy:
        container = (media.container or "").lower()
        if container in DIRECT_PLAY_CONTAINERS and not audio.needs_remap:
            tier = PlaybackTier.DIRECT_PLAY
        elif audio.needs_remap:
            # 直出档整份文件交给 <video>，浏览器只会放容器里的默认轨——我们
            # 选的那条到不了用户耳朵里。必须重封装，让 ffmpeg 把它 map 出来。
            tier = PlaybackTier.REMUX
            reason += "；选中的不是默认音轨，已重封装以带上它"
        else:
            tier = PlaybackTier.REMUX
            reason += f"；{container.upper() or '未知'} 容器已重封装为 fMP4"
    elif video.can_copy:
        tier = PlaybackTier.AUDIO_TRANSCODE
    else:
        tier = (
            PlaybackTier.HARDWARE_TRANSCODE
            if policy.hardware_available
            else PlaybackTier.SOFTWARE_TRANSCODE
        )

    # 步骤 7：档 1/2 的分片只能切在 IDR 上。关键帧太稀疏或索引未知时，
    # remux 的首帧优势消失，不如直接转码（§7-②）。
    if enforce_keyframe_gate and tier in (
        PlaybackTier.REMUX,
        PlaybackTier.AUDIO_TRANSCODE,
    ):
        interval = media.keyframe_interval_s
        if interval is None or interval > MAX_KEYFRAME_INTERVAL_S:
            tier = (
                PlaybackTier.HARDWARE_TRANSCODE
                if policy.hardware_available
                else PlaybackTier.SOFTWARE_TRANSCODE
            )
            reason += (
                "；源片关键帧过于稀疏，直通反而更慢，已改为转码"
                if interval is not None
                else "；源片关键帧索引尚未就绪，已改为转码"
            )

    # 降档回路：已失败的档位直接跳过，逐级下降。
    #
    # 为什么档 1 失败后仍然要试档 2（而不是直接跳到转码）：档 2 同样
    # ``-c:v copy``，若失败原因在视频码流，它会同样失败——但若原因在音轨，
    # 它正好修好。多试一档只浪费几秒（一次性），跳过档 2 却可能让本可直通的
    # 视频永久多转一路（每次播放都付）。这个不对称决定了保留 1→2。
    degraded_from: PlaybackTier | None = None
    while tier in failed_tiers:
        if degraded_from is None:
            degraded_from = tier
        if tier >= PlaybackTier.SOFTWARE_TRANSCODE:
            return (
                PlaybackRejected(
                    reason="这部片在当前浏览器上尝试了所有播放方式都失败了。",
                    suggestion=(
                        "可以在「播放诊断」里查看详细信息，或用 Infuse、VidHub "
                        "等第三方播放器直连播放。"
                    ),
                ),
                "",
                "",
                None,
            )
        tier = PlaybackTier(tier + 1)
        if tier is PlaybackTier.HARDWARE_TRANSCODE and not policy.hardware_available:
            tier = PlaybackTier.SOFTWARE_TRANSCODE
    if degraded_from is not None:
        reason += "；上一档播放失败，已自动降档"

    container = "mp4" if tier is PlaybackTier.DIRECT_PLAY else "hls-fmp4"
    return tier, container, reason, degraded_from


def needs_keyframe_probe(
    media: MediaProfile,
    capability: ClientCapability,
    policy: PlaybackPolicy,
    *,
    failed_tiers: frozenset[PlaybackTier] = frozenset(),
    preferred_audio: str | None = None,
    preferred_subtitle: str | None = None,
    max_height: int | None = None,
) -> bool:
    """判断当前候选是否需要读取源片关键帧密度。

    关键帧密度只影响档 1/2（视频仍然 ``copy``）的安全性。这里先暂时不执行
    关键帧门槛，算出候选在其它条件下的基准档位；只有基准档位仍需要复制视频
    时，调用方才启动有存储开销的 ffprobe。这个预判不会把未经探测的结果交给
    用户，最终的 ``decide_playback`` 仍会执行原有的保守门槛。
    """
    if media.is_strm or capability.universal:
        return False

    video_verdict = _judge_video(media, capability, policy, max_height)
    audio_verdict = _judge_audio(media, capability, preferred_audio)
    burn_track = _burn_target(media, preferred_subtitle)
    if burn_track is not None and video_verdict.blocked is None:
        video_verdict = _VideoVerdict(
            can_copy=False,
            reason="已选中 PGS 图形字幕，按你的选择转码并把字幕压制进画面",
            tone_map=video_verdict.tone_map,
        )

    tier, _, _, _ = _resolve_tier(
        media,
        video_verdict,
        audio_verdict,
        policy,
        failed_tiers,
        enforce_keyframe_gate=False,
    )
    return tier in (PlaybackTier.REMUX, PlaybackTier.AUDIO_TRANSCODE)


# ---------------------------------------------------------------------------
# 计划装配
# ---------------------------------------------------------------------------


def _build_video_plan(
    media: MediaProfile,
    verdict: _VideoVerdict,
    tier: PlaybackTier,
    policy: PlaybackPolicy,
    max_height: int | None = None,
    burn_subtitle: str | None = None,
) -> VideoPlan:
    if tier <= PlaybackTier.AUDIO_TRANSCODE:
        # copy 也要记录流经的编码：下游装 ffmpeg 命令时据此决定要不要打
        # ``-tag:v hvc1``——HEVC 进 fMP4 不打这个标签，Safari 是静默黑屏。
        return VideoPlan(action="copy", codec=(media.video_codec or None))
    # 三个上限取最小：源高度、管理员配置、用户画质选择
    candidates = [media.height or policy.max_transcode_height, policy.max_transcode_height]
    if max_height is not None:
        candidates.append(max_height)
    return VideoPlan(
        action="transcode",
        codec="h264",
        height=min(candidates),
        source_bit_depth=media.bit_depth,
        tone_map=verdict.tone_map,
        burn_subtitle=burn_subtitle,
    )


def _burn_target(media: MediaProfile, preferred_subtitle: str | None) -> SubtitleTrack | None:
    """选中的字幕是否触发烧录：**只认本文件里的内封 PGS 轨**。

    None / "off" / 文本轨 / 外挂轨 / 引用不在本文件里（换了版本文件轨序变了）
    一律返回 None——烧录必须是用户对「这条位图轨」的明确选择，宁可不烧也
    不能烧错轨。外挂 .sup 不烧：ffmpeg 的 overlay 需要字幕在同一输入里，
    外挂位图字幕极罕见，前端 canvas 渲染已覆盖。
    """
    if not preferred_subtitle or preferred_subtitle == "off":
        return None
    track = next(
        (t for t in media.subtitle_tracks if t.ref == preferred_subtitle), None
    )
    if track is None or track.is_external:
        return None
    if (track.codec or "").lower() not in _PGS_SUBTITLE_CODECS:
        return None
    return track


def _copy_audio_plan(track: AudioTrack | None) -> AudioPlan:
    """直通计划也要带上源轨的 codec/声道。

    这不是装饰：诊断面板靠 `audio.codec` 回答「直通的到底是什么」，不带就
    显示「直通 · 未知」——用户排障时最需要的恰恰是这一格。视频侧早年为
    hvc1 打标修过同构的坑（copy 计划不带 codec 导致 `-tag:v` 永远打不上）。
    """
    if track is None:
        return AudioPlan(action="copy", track_ref=None)
    return AudioPlan(
        action="copy", track_ref=track.ref, codec=track.codec, channels=track.channels
    )


def _build_audio_plan(
    media: MediaProfile, verdict: _AudioVerdict, tier: PlaybackTier
) -> AudioPlan:
    track_ref = verdict.track.ref if verdict.track else None
    if verdict.can_copy:
        return _copy_audio_plan(verdict.track)
    return AudioPlan(
        action="transcode",
        track_ref=track_ref,
        codec=verdict.target_codec,
        channels=verdict.target_channels,
        downmix=verdict.downmix,
    )


def plan_subtitles(media: MediaProfile) -> tuple[SubtitlePlan, ...]:
    """字幕规划（§3.3 步骤 8）——**默认永不烧录**（硬边界 1）。

    字幕一律旁挂：文本轨转 VTT 交 ``<track>``，ASS/SSA 原样交 JASSUB 保留
    特效，PGS 交前端 libbitsub 解码。VobSub 暂不支持。唯一例外是用户显式
    选中内封 PGS 触发的烧录（见 ``_burn_target``），那不改变本清单——被烧
    的轨仍在清单里，前端据 ``video.burn_subtitle`` 决定不再旁挂渲染它。
    """
    plans: list[SubtitlePlan] = []
    for track in media.subtitle_tracks:
        codec = (track.codec or "").lower()
        if codec in _ASS_SUBTITLE_CODECS:
            kind = "ass"
        elif codec in _TEXT_SUBTITLE_CODECS:
            kind = "vtt"
        elif codec in _PGS_SUBTITLE_CODECS:
            kind = "pgs"
        else:
            continue  # VobSub 等：不烧录，直接不提供
        plans.append(
            SubtitlePlan(
                track_ref=track.ref,
                kind=kind,
                language=track.language,
                is_default=track.is_default,
                is_ai=track.is_ai,
            )
        )
    return tuple(plans)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _decide_strm(
    media: MediaProfile, failed_tiers: frozenset[PlaybackTier]
) -> PlaybackDecision:
    """strm 网盘条目：只允许档 0（硬边界 2）。

    strm 通常没有探测数据（media_probe 不探云端内容，保持零网盘流量），
    所以这里不做能力比对——乐观把直链交给浏览器，播不了再由降档回路
    带着失败标记回来，届时明确告知而不是偷偷起转码。
    """
    if PlaybackTier.DIRECT_PLAY in failed_tiers:
        return PlaybackRejected(
            reason="这是网盘（strm）条目，浏览器无法直接播放它的编码格式。",
            suggestion=(
                "网盘条目不支持转码播放（转码需要先把云端内容下载到服务器，"
                "会消耗大量网盘流量）。请用 Infuse、VidHub 等第三方播放器打开。"
            ),
        )
    return PlaybackPlan(
        tier=PlaybackTier.DIRECT_PLAY,
        file_id=media.file_id,
        container="mp4",
        video=VideoPlan(action="copy"),
        audio=_copy_audio_plan(
            _preferred_audio(media.audio_tracks) if media.audio_tracks else None
        ),
        subtitles=plan_subtitles(media),
        reason="网盘条目直连云端播放，服务器零流量",
    )


def _preferred_audio(tracks: tuple[AudioTrack, ...]) -> AudioTrack:
    """首选音轨：标了 default 的优先，否则取第一条。"""
    return next((t for t in tracks if t.is_default), tracks[0])
