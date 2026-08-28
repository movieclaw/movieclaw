"""客户端解码能力快照（docs/design/web-player.md §3.1）。

**为什么不用 ``canPlayType()``**：它只返回 ``""`` / ``"maybe"`` / ``"probably"``，
分不清「能解码」和「能流畅解码」——4K HEVC 在无硬解的老笔记本上它会说
``probably``，实际掉帧到 5fps。前端一律用
``navigator.mediaCapabilities.decodingInfo()``，把 ``supported`` /
``smooth`` / ``powerEfficient`` 三态原样带上来。

**恒等快照**（``universal_capability()``）描述 Infuse / VidHub 这类全解码
播放器：自称「我全都能解」，因此决策引擎对它们必须恒输出档 0。

它是这条**不变量的可执行表述**，由 ``tests/playback/test_decide.py`` 的守护
用例持有——不是 Jellyfin 兼容层的实际入参。兼容层的判定是个常量，直接写在
它自己的 MediaSource DTO 里（``movieclaw_jellyfin/catalog.py``），没有绕经
本引擎；理由见 web-player.md §12.9。

⚠️ **已知偏差**：浏览器在没有本机统计数据前，会把所有 ``supported`` 的配置
乐观报成 ``smooth=true`` / ``powerEfficient=true``。因此首次探测不可全信，
必须有运行期降档回路（web-player.md §6.3）兜底——本模块只忠实承载探测结果，
不做任何"猜它其实不行"的修正。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 归一化编码家族名：与 media_probe 落库的 ``video_codec`` / 音轨 ``codec``
# 取值域对齐（ffprobe 的 codec_name），前端探测结果也须归一到同一套名字。
VIDEO_FAMILIES = frozenset({"h264", "hevc", "av1", "vp9", "vc1", "mpeg2video"})
AUDIO_FAMILIES = frozenset(
    {"aac", "ac3", "eac3", "dts", "truehd", "flac", "opus", "mp3", "vorbis"}
)


@dataclass(frozen=True)
class VideoSupport:
    """客户端对某个视频编码家族的解码能力。"""

    codec: str
    # 探测确认**能解码**的最大高度（supported 上限，不含流畅度预测）。
    # 超过这个高度的源转码降分辨率。
    max_height: int = 2160
    # decodingInfo 的三态之二。supported 不单列——不支持就不该出现在列表里。
    # ⚠️ 这两项**只作参考**（诊断与指标），决策不拿它们做硬闸：实测 Safari 对
    # HEVC 整族报 smooth=false 而实际直通 0 掉帧（decide.py §12.15 有对照）。
    smooth: bool = True
    power_efficient: bool = True


@dataclass(frozen=True)
class AudioSupport:
    """客户端对某个音频编码家族的解码能力。

    ``max_channels`` 是硬约束：Chrome 能解 AAC 但输出设备只有立体声时，
    5.1 轨仍需降混——降混是**音频转码**（档 2），不是直通。
    """

    codec: str
    max_channels: int = 8


@dataclass(frozen=True)
class ClientCapability:
    """一次播放决策的客户端侧输入。

    ``universal=True`` 表示恒等快照（全解码播放器），决策引擎见到它直接给
    档 0，不再逐项比对——见模块文档。
    """

    video: tuple[VideoSupport, ...] = ()
    audio: tuple[AudioSupport, ...] = ()
    # 客户端能直接消费的容器。"mp4"=可 <video src> 直出；"hls-fmp4"=有 MSE 或
    # 原生 HLS，能吃 fMP4 分片。两者都没有的客户端本质上放不了任何东西。
    containers: frozenset[str] = field(default_factory=frozenset)
    # 屏幕与浏览器是否支持 HDR 直出（matchMedia('(dynamic-range: high)')）。
    # 为 False 时 HDR 源必须 tone-map 到 SDR，否则画面灰暗发白。
    hdr_passthrough: bool = False
    # "full"=完整 MSE；"managed"=iOS 17+ 的 ManagedMediaSource；"none"=只有原生播放
    mse: str = "full"
    # 移动端：软解虽然 supported 也会烧电池发热，powerEfficient=False 时应主动降档
    is_mobile: bool = False
    # 移动端原生 HLS（当前主要是 iOS Safari/AVPlayer）。原生播放器对 4K
    # 码流的实际解码余量比 decodingInfo 的 supported 更保守，服务端会把它
    # 限制到既有的转码高度上限，避免把仍可能失败的 4K fMP4 交给 AVPlayer。
    native_hls: bool = False
    universal: bool = False

    def video_support(self, codec: str | None) -> VideoSupport | None:
        """取指定视频编码的支持情况；不支持返回 None。"""
        if not codec:
            return None
        codec = codec.lower()
        return next((v for v in self.video if v.codec == codec), None)

    def audio_support(self, codec: str | None) -> AudioSupport | None:
        """取指定音频编码的支持情况；不支持返回 None。"""
        if not codec:
            return None
        codec = codec.lower()
        return next((a for a in self.audio if a.codec == codec), None)


def universal_capability() -> ClientCapability:
    """恒等快照：全解码播放器（Infuse / VidHub / 本地播放器）专用。

    Jellyfin 兼容层用它调决策引擎，结果恒为档 0——与 jellyfin-compat.md
    §0 硬边界 2「不转码」等价，但复用同一套代码路径。
    """
    return ClientCapability(
        containers=frozenset({"mp4", "hls-fmp4"}),
        hdr_passthrough=True,
        universal=True,
    )
