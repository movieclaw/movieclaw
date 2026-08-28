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
#: VOD 模式下 ffmpeg 写的内部播放列表：只做「转到哪了」的进度追踪与启动
#: 就绪信号，客户端拿到的 index.m3u8 由服务端按关键帧表预生成（hls_vod.py）
LIVE_PLAYLIST_NAME = "live.m3u8"
INIT_NAME = "init.mp4"
SEGMENT_PATTERN = "seg%05d.m4s"

#: 分片时长（秒）。转码档自己控制 GOP，可以精确对齐；直通档 copy 模式下
#: ffmpeg 只能切在源片已有的关键帧上，这个值是「至少多久」，实际分片会更长。
#: 取 4：VOD 预生成列表的分片栅格与这里必须同值（hls_vod.compute_segment_plan），
#: 2 秒的列表对两小时的片有三千多行，4 秒折半，起播首段转出也只多等两秒。
SEGMENT_SECONDS = 4
# VideoToolbox 在带 ``-copyts`` 的高位点 seek 下可能自行选择过短的 GOP。
# 以常见最高 60fps 计算上限，配合下面的强制关键帧把转码档稳定在 4 秒分片；
# 这不是目标 GOP，而是防止编码器提前插入关键帧把 fMP4 切碎。
MAX_GOP_FRAMES = SEGMENT_SECONDS * 60

#: 码率阶梯：转码目标高度 → maxrate。此前硬件档写死 8M 不随分辨率变——
#: 480p 给 8M 等于没降（弱网选低画质白选），4K 给 8M 又明显不够。数值参考
#: Jellyfin 默认阶梯取整，H.264 下各档「够清晰又不虚胖」的经验值；bufsize
#: 统一给 2 倍 maxrate。软件档走 CRF 恒定质量，阶梯只作为 maxrate 上限
#: 兜底（防止高动态场景码率爆冲打满弱网带宽）。
BITRATE_LADDER: dict[int, str] = {
    2160: "16M",
    1440: "10M",
    1080: "6M",
    720: "3M",
    480: "1.5M",
}


def maxrate_for_height(height: int | None) -> str:
    """取不小于目标高度的最近阶梯档；无高度信息按 1080p 算。"""
    if height is None:
        return BITRATE_LADDER[1080]
    for step in sorted(BITRATE_LADDER):
        if height <= step:
            return BITRATE_LADDER[step]
    return BITRATE_LADDER[2160]


#: 读入限速（相对实时的倍数）与起播突发窗口（秒）。
#:
#: 不限速的教训（2026-08-23，一晚上写满 200 GB）：remux 档 `-c copy` 以磁盘
#: IO 的速度跑，点开一部 30 GB 的片看一分钟，盘上就是完整的 30 GB 分片；
#: 转码档也会一路转到片尾。分片在会话存续期间只增不减，而配额只在**开会话
#: 时**检查——活跃会话可以写穿配额直到磁盘归零，转码缓存又与 SQLite 同卷。
#:
#: `-readrate` 让 ffmpeg 限速读输入：转码进度始终领先播放位置、但占盘增速
#: 被钉住；`-readrate_initial_burst` 先全速转出开头一段，保证起播与开场
#: seek 不受限速拖累。Jellyfin 10.9+ 同款方案。
#:
#: 直通档与转码档分开限（2026-08-23 复盘 QoE 数据的结论）：1.5 倍配合前端
#: 60 秒的缓冲目标，起播后要播满两分钟缓冲才攒得够，这期间任何抖动都直接
#: stall——实测每次会话卡 2~3 次。直通档 `-c:v copy` 不吃 CPU，限它只是在
#: 省盘，而盘已有配额与低水位哨兵兜着，放到 4 倍让缓冲 20 秒内攒满；
#: 真转码档维持 1.5 倍护 CPU（软转 4 倍速本来也跑不动）。
READRATE = 1.5
READRATE_COPY = 4
READRATE_BURST_SECONDS = 60
# 远程源读取与 HLS PUT 的单次网络读写超时。没有这一项时，NAS/网络异常可能让
# ffmpeg 永久阻塞，既不再产出分片，也不退出释放 Worker 槽位；播放器的 30 秒
# 分片等待窗口也能在它超时后走失败回路。
REMOTE_IO_TIMEOUT_US = 30_000_000

#: 软件 HDR→SDR 色调映射。必须用 BT.2390 EETF——简单 clip 会把高光全压成
#: 死白（雪景、天空、爆炸场面直接糊掉）。
#:
#: 用 jellyfin-ffmpeg 专属的 ``tonemapx``（SIMD 优化的软件 tone-map 补丁，
#: 默认算法即 bt2390），不用上游的 ``zscale+tonemap+zscale`` 三级链：上游
#: ``tonemap`` 滤镜根本没有 bt2390 算法（只到 mobius，ffmpeg 8 也一样），
#: 那条链在 2026-08-23 的真机容器里实测直接报「Undefined constant」。镜像
#: 恒定内置 jellyfin-ffmpeg（§5.3），按 §12.14 不做 ffmpeg 能力探测降级。
_SOFTWARE_TONEMAP = (
    "tonemapx=tonemap=bt2390:desat=0:p=bt709:t=bt709:m=bt709:format=yuv420p"
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
    #: 编码器能否直接吃系统内存帧。烧录（overlay 是软件滤镜）会把整条滤镜链
    #: 拉回软件侧：NVENC / VideoToolbox 的编码器接软件帧没问题；VAAPI / QSV
    #: 需要 hwupload + init_hw_device 那套显存搬运，碎且驱动相关——烧录时
    #: 这两家直接退软件编码（用户选烧录已经接受了转码代价）。
    sw_frames_ok: bool = False


HW_BACKENDS: dict[str, HwBackend] = {
    "vaapi": HwBackend(
        name="vaapi",
        encoder="h264_vaapi",
        hwaccel="vaapi",
        hwaccel_output_format="vaapi",
        scale_filter="scale_vaapi",
        tonemap_filter="tonemap_vaapi=format=nv12:t=bt709",
        device="/dev/dri/renderD128",
        sw_frames_ok=False,
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
        sw_frames_ok=True,
    ),
    "videotoolbox": HwBackend(
        name="videotoolbox",
        encoder="h264_videotoolbox",
        hwaccel="videotoolbox",
        hwaccel_output_format="videotoolbox_vld",
        # VideoToolbox 硬解输出是 videotoolbox_vld；软件 scale 前必须显式
        # hwdownload + format=nv12，见 _filter_chain。没有其它软件滤镜时保留硬解。
        tonemap_filter=None,
        sw_frames_ok=True,
    ),
}


def effective_hw_backend(plan: PlaybackPlan, hw_backend: str | None) -> str | None:
    """这次会话**实际**用哪个硬件后端。

    烧录（``video.burn_subtitle``）把滤镜链拉回软件侧，编码器吃不了软件帧的
    后端（VAAPI/QSV）退回软件编码。诊断面板的 ``hw_backend`` 必须走这里——
    报一个实际没用上的后端名，用户查「为什么转码这么卡」时会被带偏。
    """
    if plan.video.action != "transcode" or hw_backend is None:
        return None if plan.video.action != "transcode" else hw_backend
    backend = HW_BACKENDS.get(hw_backend)
    if backend is None:
        return None
    if plan.video.burn_subtitle is not None and not backend.sw_frames_ok:
        return None
    return hw_backend


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
    start_number: int | None = None,
    output_base_url: str | None = None,
    output_url_suffix: str = "",
) -> TranscodeCommand:
    """把播放计划翻成 ffmpeg 命令。档 0（Direct Play）不该走到这里。

    ``start_number`` 非 None 即 VOD 模式（服务端预生成播放列表，§12）：
    分片编号从它开始接上全片规划，并加 ``-copyts`` 三件套让分片内部时间戳
    保持**文件绝对时间**——这是预生成列表与实际分片能对上的根本（EXTINF
    只是索引近似，播放器按分片真实时间戳自我校正，Jellyfin 同款取舍）。
    """
    if plan.tier is PlaybackTier.DIRECT_PLAY:
        raise ValueError("档 0 是原文件直出，不需要 ffmpeg")

    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]

    transcoding_video = plan.video.action == "transcode"
    backend = (
        HW_BACKENDS.get(effective_hw_backend(plan, hw_backend) or "") if transcoding_video else None
    )
    burn_index = _burn_subtitle_index(plan) if transcoding_video else None
    videotoolbox_unknown_bit_depth = (
        backend is not None
        and backend.name == "videotoolbox"
        and bool(plan.video.height)
        and plan.video.source_bit_depth not in (8, 10)
    )
    # 需要 tone-map 但该后端没有原生滤镜，或要烧录字幕（overlay 是软件滤镜）
    # → 走软件解码，让软件滤镜链能接上。VideoToolbox 只有软件 scale 时不在
    # 这里回退：_filter_chain 会按源位深显式下载 videotoolbox_vld 帧后再缩放，
    # 保留硬解；位深未知或不是 8/10 时无法安全选择下载格式，回退软件解码。
    software_filters = burn_index is not None or (
        bool(plan.video.tone_map)
        and (backend is None or backend.tonemap_filter is None)
    ) or videotoolbox_unknown_bit_depth

    # -ss 必须在 -i 之前：input seek 快得多（不用解码到该点）
    if start_ms > 0:
        seek_s = start_ms / 1000
        # 直通档的 start_ms 已被上游校正到关键帧（routes 里查 keyframe），但
        # ffmpeg 在 seek 目标**恰好等于**关键帧时间时会回退到前一个关键帧
        # （Jellyfin EncodingHelper 同款 workaround）——加 0.5 秒让它精确
        # 落在目标关键帧上。转码档不加：accurate_seek 解码丢帧，本来就精确。
        if not transcoding_video:
            seek_s += 0.5
        argv += ["-ss", f"{seek_s:.3f}"]
    # 视频直通时给缺 PTS 的包现算 PTS（输入选项，放输出侧无效）。转码档
    # 解码器自己会重建时间戳，不需要；copy 档少了它，只有 DTS 的源（TS 转
    # 封装的 mkv 常见）写进 fMP4 的 TFDT 就是垃圾值——Jellyfin copy 路径
    # 无条件加这一条。
    if not transcoding_video:
        argv += ["-fflags", "+genpts"]
    # 读入限速也是输入选项，必须在 -i 之前，理由见常量注释
    argv += [
        "-readrate", str(READRATE_COPY if not transcoding_video else READRATE),
        "-readrate_initial_burst", str(READRATE_BURST_SECONDS),
    ]
    if backend and backend.hwaccel and not software_filters:
        argv += ["-hwaccel", backend.hwaccel]
        if backend.hwaccel_output_format:
            argv += ["-hwaccel_output_format", backend.hwaccel_output_format]
        if backend.device:
            argv += ["-hwaccel_device", backend.device]
    if output_base_url:
        # 输入与输出分别设置一次：前者约束 HTTPS Range 读取，后者由 HLS muxer
        # 传给每个 init/segment/playlist 的 HTTP PUT。
        argv += ["-rw_timeout", str(REMOTE_IO_TIMEOUT_US)]
    argv += ["-i", source_path]

    # 只取一路视频一路音频；字幕流不进输出容器（-sn）——默认旁挂由前端渲染
    # （硬边界 1）。唯一例外：用户显式选中 PGS 触发的烧录，字幕经 filter_complex
    # 合成进视频帧，输出里依然没有独立字幕流。-map_metadata -1 去掉源片元数据。
    if burn_index is not None:
        argv += [
            "-filter_complex",
            _burn_filter_graph(plan, burn_index),
            "-map", "[vout]",
        ]
    else:
        argv += ["-map", "0:v:0"]
    audio_index = _audio_index(plan)
    if audio_index is not None:
        argv += ["-map", f"0:a:{audio_index}"]
    argv += ["-sn", "-dn", "-map_metadata", "-1"]

    argv += _video_args(plan, backend, software_filters, skip_filters=burn_index is not None)
    argv += _audio_args(
        plan, has_audio=audio_index is not None, absolute_ts=start_number is not None
    )
    if start_number is not None:
        # copyts + avoid_negative_ts disabled：保留输入的绝对时间戳，muxer
        # 不做归零平移——seek 重启后分片时间戳依旧是文件时间。start_at_zero
        # 处理 start_time != 0 的源（TS 转封装常见），照抄 Jellyfin。
        argv += ["-copyts", "-avoid_negative_ts", "disabled", "-start_at_zero"]
    argv += _hls_args(
        session_dir,
        start_number=start_number,
        output_base_url=output_base_url,
        output_url_suffix=output_url_suffix,
    )
    if output_base_url:
        # 远程 Worker 将进度写到 stdout 管道并通过控制面低频上报；不把进度
        # 写入 stderr，避免和含有源地址的 ffmpeg 警告混在一起。stdout 不会
        # 进入 NAS 媒体目录，也不会改变 HLS 输出路径。
        argv += ["-progress", "pipe:1"]

    playlist = session_dir / (LIVE_PLAYLIST_NAME if start_number is not None else PLAYLIST_NAME)
    if output_base_url:
        argv.append(
            f"{output_base_url.rstrip('/')}/{playlist.name}{output_url_suffix}"
        )
    else:
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


def _burn_subtitle_index(plan: PlaybackPlan) -> int | None:
    """烧录轨中性引用 ``embedded:<k>`` → ffmpeg 的 ``0:s:<k>``；非烧录为 None。"""
    if plan.video.burn_subtitle is None:
        return None
    return parse_embedded_track(plan.video.burn_subtitle)


def _burn_filter_graph(plan: PlaybackPlan, subtitle_index: int) -> str:
    """烧录的 filter_complex 图：tone-map →（源分辨率）overlay 烧字幕 → scale。

    顺序有讲究：
    - overlay 必须在 **scale 之前**——PGS 位图的坐标按源分辨率定位，先缩放
      画面再叠原始坐标的字幕，位置和大小全错；
    - tone-map 在 overlay 之前——PGS 是 SDR 图形，叠上 HDR 帧再整体映射会把
      字幕颜色一起压暗；先把画面拉回 SDR 再叠，字幕保持设计时的观感。
    烧录一律软件滤镜链（overlay 没有通用的硬件版本），编码器侧的取舍见
    ``effective_hw_backend``。
    """
    steps: list[tuple[str, str]] = []  # (滤镜串, 输出标签)
    if plan.video.tone_map:
        steps.append((_SOFTWARE_TONEMAP, "tm"))
    base = f"[0:v:0]{steps[0][0]}[tm];[tm]" if steps else "[0:v:0]"
    graph = f"{base}[0:s:{subtitle_index}]overlay"
    if plan.video.height:
        graph += f"[burned];[burned]scale=-2:{plan.video.height}"
    # 末端钉死 8-bit：10-bit 源（无 tonemap 的 SDR 10-bit 最常见）经 overlay
    # 后的位深由滤镜格式协商决定、随 ffmpeg 版本漂——协商出 10-bit 就会让
    # x264 编出 iPhone 不认的 High 10，或让 NVENC/VideoToolbox 直接拒帧。
    graph += "[pre];[pre]format=yuv420p"
    graph += "[vout]"
    return graph


def _video_args(
    plan: PlaybackPlan,
    backend: HwBackend | None,
    software_filters: bool,
    *,
    skip_filters: bool = False,
) -> list[str]:
    if plan.video.action == "copy":
        args = ["-c:v", "copy"]
        # HEVC 装进 fMP4 必须打 hvc1 标签：Safari 只认 hvc1，喂 hev1 是**静默
        # 黑屏**——没有 error 事件、没有日志，只有一个不动的黑框（§7-①）。
        # 实测：不加这行 ffmpeg 输出的 codec tag 就是 hev1。
        if (plan.video.codec or "").lower() in {"hevc", "h265"}:
            args += ["-tag:v", "hvc1"]
        return args

    # 烧录时滤镜已在 filter_complex 图里（-vf 与 filter_complex 互斥）
    filters = "" if skip_filters else _filter_chain(plan, backend, software_filters)
    args = []
    if filters:
        args += ["-vf", filters]
    maxrate = maxrate_for_height(plan.video.height)
    bufsize = f"{int(float(maxrate[:-1]) * 2)}M"
    if backend is not None:
        args += ["-c:v", backend.encoder]
        # iOS 原生 HLS 对 10-bit/High 10 的硬件编码结果兼容性很差，统一锁
        # 到 High profile + 8-bit yuv420p。VideoToolbox 不能在 4K 输出时强制
        # 使用 High@4.1（会以 kVTParameterErr=-12902 拒绝创建编码器），因此
        # 交给它按实际分辨率选择合法 level（2160p 通常为 5.1）。
        args += ["-profile:v", "high"]
        if backend.name != "videotoolbox":
            args += ["-level:v", "4.1"]
        if backend.name == "videotoolbox":
            args += ["-pix_fmt", "yuv420p"]
        # 明确 H.264 的 ISO BMFF sample entry。默认通常也是 avc1，但不同编码器
        # 或封装器版本可能落成 avc3；Safari 原生 HLS 需要稳定、可预告的标签。
        args += ["-tag:v", "avc1"]
        # 硬件编码器不认 CRF，用码率阶梯约束
        args += ["-b:v", "0", "-maxrate", maxrate, "-bufsize", bufsize]
    else:
        # CRF 恒定质量优先（§11-1），阶梯只作为上限兜底防码率爆冲。
        #
        # -pix_fmt yuv420p 必须显式钉死（2026-08-25 真机事故，Jellyfin 的
        # EncodingHelper 同款做法）：10-bit 源（动漫的 HEVC 10-bit SDR 极常见）
        # 不钉的话 libx264 顺着输入位深编出 **High 10 profile**——iPhone/大多数
        # 硬解都不认这个 profile，表现为真实的解码错误，且降档到哪一档软转
        # 都一样炸。烧录链的位深同理不能赌 overlay 的格式协商（不同 ffmpeg
        # 版本协商结果不同），见 _burn_filter_graph 末端的 format。
        # -preset superfast 而不是 veryfast（2026-08-25 真机事故）：无硬编的
        # 弱 CPU（NAS 常见）上 veryfast 编 1080p+overlay 实测只有 1.11× 实时，
        # 起播阶段攒不出缓冲——iPhone 的 AVPlayer 等分片超时直接放弃，抛的
        # 还是笼统的「不支持此格式」，极难排查。superfast 实测 1.67×，越过
        # readrate 1.5 的读入限速线，编码器不再是瓶颈；同机 ultrafast 2.35×
        # 但画质损失明显，1.67× 已够供片就不再降。
        args += [
            "-c:v", "libx264", "-preset", "superfast", "-crf", "21",
            "-profile:v", "high", "-level:v", "4.1",
            "-pix_fmt", "yuv420p",
            "-tag:v", "avc1",
            "-maxrate", maxrate, "-bufsize", bufsize,
        ]
    # 固定 GOP 上限，防止 VideoToolbox 在高位点 copyts seek 时生成过密 IDR，
    # 把 HLS 切成 0.4 秒一段；force_key_frames 再把关键帧对齐到分片栅格。
    args += ["-g", str(MAX_GOP_FRAMES)]
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
            if backend is not None and backend.name == "videotoolbox" and not software_filters:
                # videotoolbox_vld 是硬件帧，不能让软件 scale 隐式协商格式；
                # 8-bit 下载为 NV12，10-bit 下载为 P010；最后统一成 8-bit
                # yuv420p 给 H.264 编码器，避免 macOS ffmpeg 报格式无效。
                download_format = "p010le" if plan.video.source_bit_depth == 10 else "nv12"
                parts.append(f"hwdownload,format={download_format}")
            parts.append(f"scale=-2:{height}")
            if backend is not None and backend.name == "videotoolbox" and not software_filters:
                parts.append("format=yuv420p")
    return ",".join(parts)


def _audio_args(plan: PlaybackPlan, *, has_audio: bool, absolute_ts: bool) -> list[str]:
    if not has_audio:
        return []
    if plan.audio.action == "copy":
        return ["-c:a", "copy"]
    codec = plan.audio.codec or "aac"
    args = ["-c:a", codec]
    if codec.lower() == "aac":
        # iOS 原生 HLS 走 AVPlayer，显式锁 AAC-LC，避免编码器默认 profile
        # 或源参数让输出变成 HE-AAC/其它 AAC profile，导致 init 阶段拒绝。
        args += ["-profile:a", "aac_low"]
    if plan.audio.channels:
        args += ["-ac", str(plan.audio.channels)]
    args += ["-b:a", "256k" if (plan.audio.channels or 2) <= 2 else "640k"]
    # aresample=async=1：重采样器按时间戳对齐输出，填平/吸收源音轨的起点
    # 偏移与细小漂移。视频 copy + 音频转码是最常见组合（EAC3/DTS 浏览器不认），
    # 不对齐的表现是起播/暂停恢复的瞬间唇音差几十毫秒、几秒后才追上——
    # 对比原生播放器"不丝滑"的主要来源。Jellyfin 的音频链同款。
    #
    # first_pts=0（把音频时间轴拉回 0）**只属于会话相对模式**。VOD 模式带
    # -copyts，音频 pts 是文件绝对时间——再要求归零，重采样器会试图插入
    # 几千秒的静音来「补偿」（实测：从 3393 秒处续播，ffmpeg 埋头填了 17 秒
    # 静音才吐出第一个分片，前端就卡在「正在判断播放方式」）。
    resample = "aresample=async=1" if absolute_ts else "aresample=async=1:first_pts=0"
    if plan.audio.downmix:
        args += ["-af", f"{_DOWNMIX_PAN},{resample}"]
    else:
        args += ["-af", resample]
    return args


def _hls_args(
    session_dir: Path,
    *,
    start_number: int | None = None,
    output_base_url: str | None = None,
    output_url_suffix: str = "",
) -> list[str]:
    """fMP4/CMAF 分片。不用 MPEG-TS：同一份分片将来可同时喂 HLS 和 DASH，
    加 DASH/离线只是多一份 manifest。

    ``-hls_playlist_type event``：playlist 只增不改，边转边给——会话起来后
    立刻返回 m3u8，不等分片（首帧延迟的关键）。VOD 模式下这份列表只是内部
    进度追踪（客户端的列表由服务端预生成），``-start_number`` 让 seek 重启
    后的分片文件名接上全片编号。
    """
    if output_base_url:
        # HLS muxer 会把 fMP4 init、每个分片和 playlist 分别作为 HTTP 资源
        # 打开。init 文件名是个例外：muxer 会把它按播放列表 URL 的目录解析，
        # 这里必须传相对文件名；传完整 URL 会拼成
        # ``/artifacts/http://host/.../artifacts/init.mp4``。分片模板则由
        # muxer 直接作为 URL 使用。Worker 侧因此只在内存中暂存当前产物，NAS
        # 端点负责把请求体写入临时文件后原子替换，避免浏览器读到半个 moof。
        output_base = output_base_url.rstrip("/")
        init_filename = f"{INIT_NAME}{output_url_suffix}"
        segment_filename = f"{output_base}/{SEGMENT_PATTERN}{output_url_suffix}"
    else:
        init_filename = INIT_NAME
        segment_filename = str(session_dir / SEGMENT_PATTERN)

    args = [
        *(["-rw_timeout", str(REMOTE_IO_TIMEOUT_US)] if output_base_url else []),
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", init_filename,
        "-hls_segment_filename", segment_filename,
    ]
    if output_base_url:
        # HTTP 输出必须显式使用 PUT：POST 会被 Starlette 当成普通接口请求，
        # 也无法用同一个 URL 做幂等重传。Jellyfin-ffmpeg 的 HLS muxer 会为
        # init/分片/playlist 分别创建 HTTP 子请求，这些请求使用 chunked PUT
        # 是其正常行为，NAS 端点必须完整读取请求体后再原子替换。
        args += ["-method", "PUT"]
    if start_number is not None:
        args += ["-start_number", str(start_number)]
    args += [
        "-hls_playlist_type", "event",
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments",
        # fMP4 分片的时间基修正（Jellyfin 同款，它注释写明了这两个 movflag
        # 就是治分片衔接处画面闪）：
        # +frag_discont —— 每个 moof 的 TFDT 写含初始 delay 的真实 DTS，
        #   不写就是「假定紧接上一段」，音频有编码器 delay 时拼接点错位；
        # +skip_sidx —— HLS 用不到 sidx，而 ffmpeg 写 sidx 时会回头改写
        #   open-GOP 边界包的 PTS，正是切片处闪一帧的经典成因。
        "-hls_segment_options", "movflags=+frag_discont+skip_sidx",
        "-y",
    ]
    return args
