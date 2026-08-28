"""服务端生成的 VOD 播放列表（docs/design/web-player.md §12）。

与旧方案（ffmpeg 边转边写 EVENT 播放列表）的根本区别：播放列表在开会话时
**一次性完整生成**——每个分片的边界与时长来自全片关键帧索引（keyframes.py），
带 ENDLIST，类型 VOD。收益：

- 播放器（hls.js / Safari 原生 HLS / AVPlayer）拿到的是「时长已知的点播」，
  不再被当成直播贴边播（那是 iPhone 周期闪黑屏与刷新进度漂移的根源）；
- seek 任意位置都在列表内，跳转由播放器直接请求对应分片，服务端按需转码，
  前端不再需要「seek 出已转区间就重开会话」的整套逻辑；
- 时间轴变成**文件绝对时间**：分片 N 的内容就是文件的第 boundaries[N] 秒起，
  start_ms 换算、关键帧校正从此消失。

分片边界规则必须与 ffmpeg hls muxer 的实际切分**逐一吻合**：ffmpeg 以
「k × hls_time 的绝对栅格」为切分目标，在每个 ≥ 栅格点的第一个关键帧切段
（详见 compute_segment_plan 的注释与实测记录）。这里用同一条规则从关键帧表
预计算，两边天然一致；ffmpeg 侧用 ``-start_number N`` 保证 seek 重启后
文件名编号接上。（Jellyfin DynamicHlsPlaylistGenerator 同款设计。）
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SegmentPlan:
    """一个文件的完整分片规划。boundaries 是每个分片的起点（秒），首元素恒 0；
    分片 i 的区间是 [boundaries[i], boundaries[i+1])，末段止于 duration_s。"""

    boundaries: tuple[float, ...]
    duration_s: float

    @property
    def count(self) -> int:
        return len(self.boundaries)

    def duration_of(self, index: int) -> float:
        end = self.boundaries[index + 1] if index + 1 < self.count else self.duration_s
        return max(0.0, end - self.boundaries[index])

    def segment_for(self, position_s: float) -> int:
        """position_s 落在哪个分片里。越界钳到首/末段。"""
        if position_s <= 0:
            return 0
        return min(self.count - 1, bisect.bisect_right(self.boundaries, position_s) - 1)


def compute_segment_plan(
    keyframes_s: tuple[float, ...] | list[float],
    duration_s: float,
    *,
    target_s: float,
) -> SegmentPlan:
    """按 ffmpeg hls muxer 的切分规则把关键帧表聚合成分片边界。

    规则是**绝对栅格**而不是「上一切点 + target」：ffmpeg（hlsenc）的切分
    目标是 ``k × hls_time``（从首包算起），在每个 ≥ 栅格点的第一个关键帧
    切段，切完后栅格推进到下一个未跨过的整数倍。两种规则大部分时候结果
    相同，但上一段超长（关键帧稀）时会分叉——实测触不可及 remux 第 12 段
    就对不上。这里必须逐包吻合，规则以 ffmpeg 为准。
    """
    boundaries: list[float] = [0.0]
    next_grid = target_s
    for time_s in keyframes_s:
        if time_s >= duration_s:
            break
        if time_s >= next_grid:
            boundaries.append(time_s)
            next_grid = (math.floor(time_s / target_s) + 1) * target_s
    # 末段短于半个目标时并入前一段：0.2 秒的尾巴单独成段只会让播放器多一次
    # 请求，还容易与 ffmpeg 的收尾行为对不齐
    if len(boundaries) > 1 and duration_s - boundaries[-1] < target_s / 2:
        boundaries.pop()
    return SegmentPlan(boundaries=tuple(boundaries), duration_s=duration_s)


def compute_uniform_plan(duration_s: float, *, target_s: float) -> SegmentPlan:
    """等长分片规划——给转码档用：视频经过重编码，``-force_key_frames
    expr:gte(t,n_forced*target)`` 在绝对栅格上强插关键帧，边界就是等差数列，
    不需要读源片的关键帧索引。"""
    count = max(1, math.ceil(duration_s / target_s))
    boundaries = tuple(i * target_s for i in range(count))
    return SegmentPlan(boundaries=boundaries, duration_s=duration_s)


def build_media_playlist(
    plan: SegmentPlan,
    *,
    init_name: str,
    segment_name: str,
    query: str = "",
) -> str:
    """媒体播放列表（VOD）。``segment_name`` 是含 %05d 的文件名模板；
    ``query`` 形如 ``?token=xxx``，逐条附在 URI 上（HLS 客户端不继承查询串）。
    """
    max_duration = max((plan.duration_of(i) for i in range(plan.count)), default=1.0)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{math.ceil(max_duration)}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f'#EXT-X-MAP:URI="{init_name}{query}"',
    ]
    for i in range(plan.count):
        lines.append(f"#EXTINF:{plan.duration_of(i):.6f},")
        lines.append(f"{segment_name % i}{query}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def build_master_playlist(
    *,
    media_uri: str,
    subtitles: list[tuple[str, str]] | None = None,
    codecs: str | None = None,
    query: str = "",
) -> str:
    """master 播放列表：一路视频 + 可选的 WEBVTT 字幕组与编码声明。

    字幕做成 HLS 字幕组的意义：Safari 原生 HLS / AVPlayer 把它当**系统级
    字幕轨**渲染——画中画小窗、原生全屏里都有字幕，这是网页 DOM 字幕层
    做不到的（PiP 图层只含视频帧）。``subtitles`` 为 (名字, 字幕列表 URI)。
    ``codecs`` 只有调用方能准确知道输出 sample entry 时才传入；旧路径不应
    为未知的源流伪造 CODECS，避免 Safari 依据错误声明选择错误的解码器。
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    subtitle_attr = ""
    if subtitles:
        for index, (name, uri) in enumerate(subtitles):
            default = "YES" if index == 0 else "NO"
            lines.append(
                '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",'
                f'NAME="{name}",DEFAULT={default},AUTOSELECT=YES,'
                f'URI="{uri}{query}"'
            )
        subtitle_attr = ',SUBTITLES="subs"'
    # BANDWIDTH 是必填属性；直通档给不出真值，报一个宽松上限即可——
    # 单变体列表没有档位切换，这个数字不参与任何决策。CODECS 对 Safari
    # 原生 HLS 尤其重要：它会在初始化 fMP4 前先按该声明筛选解码路径。
    codecs_attr = f',CODECS="{codecs}"' if codecs else ""
    lines.append(
        f"#EXT-X-STREAM-INF:BANDWIDTH=80000000{codecs_attr}{subtitle_attr}"
    )
    lines.append(f"{media_uri}{query}")
    return "\n".join(lines) + "\n"


def build_subtitle_playlist(
    *,
    vtt_uri: str,
    duration_s: float,
    query: str = "",
) -> str:
    """字幕媒体列表：整片一个 VTT 分片。

    HLS 允许字幕分片任意长；文本字幕整片不过几百 KB，切片只会多几次请求。
    """
    target = max(1, math.ceil(duration_s))
    return "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{target}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXTINF:{duration_s:.6f},",
            f"{vtt_uri}{query}",
            "#EXT-X-ENDLIST",
        ]
    ) + "\n"
