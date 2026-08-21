"""ffmpeg 命令装配（docs/design/web-player.md §2.1）——**纯函数，可表驱动单测**。

与决策引擎同一分层原则：**装配是纯函数，执行才需要真 ffmpeg**。§7 列的那些
陷阱（hvc1 标签、时间轴、降混系数、分片对齐）全都体现在参数上，因此单测能
钉死它们，不必每次都起进程。

本文件里的每一条标志都经过真实 ffmpeg 实测（见 tests/playback/
test_ffmpeg_args.py 与标 integration 的端到端用例），不是照文档抄的。

时间轴的取舍（实测结论）
------------------------
``-ss`` 放在 ``-i`` 之前是 input seek，copy 模式下只能落到目标时间**之前**
最近的关键帧上。三种处理实测结果（源片 30 秒，请求 ``-ss 10``）：

===========================  ================  ==========================
组合                          输出 start_time   含义
===========================  ================  ==========================
无 ``-copyts``                0                 时间轴从 0 起，真实起点不可知
``-copyts``                   8.0               保留绝对时间轴
``-copyts -start_at_zero``    8.022             被源片起始偏移带歪
===========================  ================  ==========================

**选择不用 copyts**：会话时间轴恒从 0 起，``文件时间 = start_ms + 播放器
currentTime``，确定、可断言、前端换算只有一处。

代价是**关键帧回退**。实测（源片 6 秒，关键帧在 0/2/4 秒）：

======  ========  ==========================================
-ss 值  实际起点  说明
======  ========  ==========================================
1.0     0.0       回退到前一个关键帧
2.0     0.0       **请求正好等于关键帧时间，反而退到更前一个**
2.5     2.0       回退到前一个关键帧
3.0     2.0       同上
4.0     2.0       同样是「正好在关键帧上」的情况
======  ========  ==========================================

也就是说：input seek 落在**严格早于**请求时间的那个关键帧上，最坏回退两个
GOP。方向恒为「稍早」而非「跳过内容」——对用户是安全的方向（宁可重看几秒，
不能漏看）。要精确到帧只能付出解码代价，直通档做不到，也不值得。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from movieclaw_playback.decide import PlaybackPlan, PlaybackTier
from movieclaw_playback.subtitles import parse_embedded_track

PLAYLIST_NAME = "index.m3u8"
INIT_NAME = "init.mp4"
SEGMENT_PATTERN = "seg%05d.m4s"

#: 分片时长（秒）。转码档自己控制 GOP，可以精确 2 秒；直通档 copy 模式下
#: ffmpeg 只能切在源片已有的关键帧上，这个值是「至少多久」，实际分片会更长。
SEGMENT_SECONDS = 2

#: 软件 HDR→SDR 色调映射链。必须用 BT.2390 EETF——简单 clip 会把高光全压成
#: 死白（雪景、天空、爆炸场面直接糊掉）。
_SOFTWARE_TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,"
    "tonemap=tonemap=bt2390:desat=0,"
    "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p"
)

#: 多声道降混系数：提升中置声道权重。不带它，对白会明显偏小——
#: 「音效很响但听不清台词」是用户投诉第一名（§7-⑤）。
#: 不用 ``-af volume=2`` 那种 hack，那会削顶失真。
_DOWNMIX_PAN = (
    "pan=stereo|FL=0.5*FC+0.707*FL+0.707*BL|FR=0.5*FC+0.707*FR+0.707*BR"
)


@dataclass(frozen=True)
class HwBackend:
    """一个硬件加速后端的参数三件套。

    ``tonemap_filter`` 为 None 表示该后端在**上游 ffmpeg** 里没有原生色调映射
    滤镜（实测：``tonemap_cuda`` 不在上游，是 jellyfin-ffmpeg 的补丁）。这种
    情况下退回「软件解码 + 软件 tone-map + 硬件编码」，而不是硬凑一条
    hwdownload/hwupload 的链子——后者在不同驱动上碎得厉害。
    """

    name: str
    encoder: str
    hwaccel: str | None = None
    hwaccel_output_format: str | None = None
    scale_filter: str | None = None  # 形如 "scale_vaapi"；None=用软件 scale
    tonemap_filter: str | None = None
    device: str | None = None


HW_BACKENDS: dict[str, HwBackend] = {
    "vaapi": HwBackend(
        name="vaapi",
        encoder="h264_vaapi",
        hwaccel="vaapi",
        hwaccel_output_format="vaapi",
        scale_filter="scale_vaapi",
        tonemap_filter="tonemap_vaapi=format=nv12:t=bt709",
        device="/dev/dri/renderD128",
    ),
    "qsv": HwBackend(
        name="qsv",
        encoder="h264_qsv",
        hwaccel="qsv",
        hwaccel_output_format="qsv",
        scale_filter="scale_qsv",
        tonemap_filter="vpp_qsv=tonemap=1:format=nv12",
    ),
    "nvenc": HwBackend(
        name="nvenc",
        encoder="h264_nvenc",
        hwaccel="cuda",
        hwaccel_output_format="cuda",
        scale_filter="scale_cuda",
        tonemap_filter=None,  # 上游没有 tonemap_cuda，见类文档
    ),
    "videotoolbox": HwBackend(
        name="videotoolbox",
        encoder="h264_videotoolbox",
        hwaccel="videotoolbox",
        # VideoToolbox 解码输出就在系统内存，滤镜链直接用软件 scale
        tonemap_filter=None,
    ),
}


@dataclass(frozen=True)
class TranscodeCommand:
    """一条待执行的 ffmpeg 命令及其产物位置。"""

    argv: list[str]
    playlist_path: Path
    init_path: Path


def build_hls_command(
    plan: PlaybackPlan,
    *,
    source_path: str,
    session_dir: Path,
    start_ms: int = 0,
    hw_backend: str | None = None,
) -> TranscodeCommand:
    """把播放计划翻成 ffmpeg 命令。档 0（Direct Play）不该走到这里。"""
    if plan.tier is PlaybackTier.DIRECT_PLAY:
        raise ValueError("档 0 是原文件直出，不需要 ffmpeg")

    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]

    transcoding_video = plan.video.action == "transcode"
    backend = HW_BACKENDS.get(hw_backend or "") if transcoding_video else None
    # 需要 tone-map 但该后端没有原生滤镜 → 走软件解码，让软件滤镜链能接上
    software_filters = bool(plan.video.tone_map) and (
        backend is None or backend.tonemap_filter is None
    )

    # -ss 必须在 -i 之前：input seek 快得多（不用解码到该点）
    if start_ms > 0:
        argv += ["-ss", f"{start_ms / 1000:.3f}"]
    if backend and backend.hwaccel and not software_filters:
        argv += ["-hwaccel", backend.hwaccel]
        if backend.hwaccel_output_format:
            argv += ["-hwaccel_output_format", backend.hwaccel_output_format]
        if backend.device:
            argv += ["-hwaccel_device", backend.device]
    argv += ["-i", source_path]

    # 只取一路视频一路音频；字幕与数据流一律不进容器——**绝不烧录**（硬边界 1），
    # 字幕全部旁挂由前端渲染。-map_metadata -1 去掉源片元数据（含可能的大块附件）。
    argv += ["-map", "0:v:0"]
    audio_index = _audio_index(plan)
    if audio_index is not None:
        argv += ["-map", f"0:a:{audio_index}"]
    argv += ["-sn", "-dn", "-map_metadata", "-1"]

    argv += _video_args(plan, backend, software_filters)
    argv += _audio_args(plan, has_audio=audio_index is not None)
    argv += _hls_args(session_dir)

    playlist = session_dir / PLAYLIST_NAME
    argv.append(str(playlist))
    return TranscodeCommand(
        argv=argv, playlist_path=playlist, init_path=session_dir / INIT_NAME
    )


def _audio_index(plan: PlaybackPlan) -> int | None:
    """中性轨引用 ``embedded:<k>`` → ffmpeg 的 ``0:a:<k>``。

    k 是 ``audio_streams`` 数组下标，与 ffmpeg 的「第 k 路音频流」同义，
    因此可以直接用——不能用绝对流序号，那个会被字幕/附件流搅乱。
    """
    if plan.audio.track_ref is None:
        return None
    index = parse_embedded_track(plan.audio.track_ref)
    return index if index is not None else 0


def _video_args(
    plan: PlaybackPlan, backend: HwBackend | None, software_filters: bool
) -> list[str]:
    if plan.video.action == "copy":
        args = ["-c:v", "copy"]
        # HEVC 装进 fMP4 必须打 hvc1 标签：Safari 只认 hvc1，喂 hev1 是**静默
        # 黑屏**——没有 error 事件、没有日志，只有一个不动的黑框（§7-①）。
        # 实测：不加这行 ffmpeg 输出的 codec tag 就是 hev1。
        if (plan.video.codec or "").lower() in {"hevc", "h265"}:
            args += ["-tag:v", "hvc1"]
        return args

    filters = _filter_chain(plan, backend, software_filters)
    args = []
    if filters:
        args += ["-vf", filters]
    if backend is not None:
        args += ["-c:v", backend.encoder]
        # 硬件编码器不认 CRF，用码率上限约束
        args += ["-b:v", "0", "-maxrate", "8M", "-bufsize", "16M"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    # 强制 2 秒关键帧，让分片精确对齐（转码档自己控制 GOP，直通档做不到）
    args += ["-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"]
    return args


def _filter_chain(
    plan: PlaybackPlan, backend: HwBackend | None, software_filters: bool
) -> str:
    parts: list[str] = []
    height = plan.video.height
    if plan.video.tone_map:
        if backend is not None and backend.tonemap_filter and not software_filters:
            parts.append(backend.tonemap_filter)
        else:
            parts.append(_SOFTWARE_TONEMAP)
    if height:
        if backend is not None and backend.scale_filter and not software_filters:
            parts.append(f"{backend.scale_filter}=w=-2:h={height}")
        else:
            # -2 保持宽高比并对齐到偶数（编码器要求）
            parts.append(f"scale=-2:{height}")
    return ",".join(parts)


def _audio_args(plan: PlaybackPlan, *, has_audio: bool) -> list[str]:
    if not has_audio:
        return []
    if plan.audio.action == "copy":
        return ["-c:a", "copy"]
    args = ["-c:a", plan.audio.codec or "aac"]
    if plan.audio.channels:
        args += ["-ac", str(plan.audio.channels)]
    args += ["-b:a", "256k" if (plan.audio.channels or 2) <= 2 else "640k"]
    if plan.audio.downmix:
        args += ["-af", _DOWNMIX_PAN]
    return args


def _hls_args(session_dir: Path) -> list[str]:
    """fMP4/CMAF 分片。不用 MPEG-TS：同一份分片将来可同时喂 HLS 和 DASH，
    加 DASH/离线只是多一份 manifest。

    ``-hls_playlist_type event``：playlist 只增不改，边转边给——会话起来后
    立刻返回 m3u8，不等分片（首帧延迟的关键）。
    """
    return [
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", INIT_NAME,
        "-hls_segment_filename", str(session_dir / SEGMENT_PATTERN),
        "-hls_playlist_type", "event",
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments",
        "-y",
    ]
