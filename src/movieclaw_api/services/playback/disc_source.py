"""原盘（BDMV）播放源解析——两条播放链路共用（docs/design/disc-playback.md §3.2）。

台账里原盘的 ``file_path`` 是目录，ffmpeg 与播放器都吃不了目录。本模块把它
解析成主播放列表引用的剪辑序列，向上提供三样东西：

- **单剪辑判定**：主片只有一个 m2ts 时，Jellyfin 兼容层直接按 Range 供流，
  零 ffmpeg（Infuse/VidHub 自带 TS 解复用）；
- **concat 清单**：多剪辑时给 ffmpeg concat demuxer 的输入（每段 ``file`` /
  ``inpoint`` / ``outpoint`` / ``duration``），比 Jellyfin 多写 IN/OUT——播放列表
  可能只用剪辑的一段，整文件拼接会多播花絮；
- **关键帧索引**：各剪辑 CLPI 的 EP_map 按段累加偏移合成全片关键帧表，供 VOD
  预生成分片（web-player.md §12）。不读 m2ts 本体。

时间轴口径：**播放列表时间**——0 秒是第一段的 IN_time，各段时长累加；
台账 ``duration_seconds`` 与章节标记都按这个口径，concat 拼出来的流也是。

剪辑清单优先取台账 ``disc_playlist``（探测时落库，浏览/播放零磁盘 IO）；
存量行还没补探时才回盘上读 MPLS，结果按 PLAYLIST 目录 mtime 缓存。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from movieclaw_api.services.library.bluray import (
    MPLS_CLOCK_HZ,
    MplsPlaylist,
    disc_playlist_stale,
    read_clpi_keyframes,
    read_main_playlist,
)
from movieclaw_db.models import LibraryFile
from movieclaw_playback.keyframes import KeyframeIndex

logger = logging.getLogger("movieclaw_api.disc_source")

#: 会话目录里 concat 清单的文件名（随会话目录一起清理，不落持久化中间文件）
CONCAT_LIST_NAME = "source.concat"


@dataclass(frozen=True)
class DiscClip:
    """主播放列表里的一段：哪个 m2ts、播哪一截（45 kHz 时间戳）。"""

    clip_id: str
    path: Path
    in_time: int
    out_time: int

    @property
    def duration_s(self) -> float:
        return max(0, self.out_time - self.in_time) / MPLS_CLOCK_HZ


@dataclass(frozen=True)
class DiscSource:
    """一张原盘的可播形态。"""

    disc_dir: Path
    playlist_name: str
    clips: tuple[DiscClip, ...]

    @property
    def duration_s(self) -> float:
        return sum(clip.duration_s for clip in self.clips)

    @property
    def single_clip(self) -> DiscClip | None:
        """主片只有一个 m2ts 时返回它（可直接按文件供流）；多剪辑返回 None。"""
        return self.clips[0] if len(self.clips) == 1 else None

    @property
    def display_name(self) -> str:
        """诊断/活动页用的片名：原盘目录名比 ``00001.m2ts`` 更能说明在播什么。"""
        return self.disc_dir.name

    def concat_list(self) -> str:
        """ffmpeg concat demuxer 的清单文本（``-f concat -safe 0 -i 清单``）。

        每段写 ``inpoint``/``outpoint``（剪辑内的绝对 PTS，秒）与 ``duration``：
        concat demuxer 靠 ``duration`` 在不打开后续文件的前提下算出各段在拼接
        时间轴上的起点，``-ss`` 也据此直接定位到对应剪辑。单引号按 concat 的
        规则转义（``'`` → ``'\\''``）。
        """
        lines = ["ffconcat version 1.0"]
        for clip in self.clips:
            escaped = str(clip.path).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
            lines.append(f"inpoint {clip.in_time / MPLS_CLOCK_HZ:.6f}")
            lines.append(f"outpoint {clip.out_time / MPLS_CLOCK_HZ:.6f}")
            lines.append(f"duration {clip.duration_s:.6f}")
        return "\n".join(lines) + "\n"

    def keyframe_index(self) -> KeyframeIndex | None:
        """全片关键帧表（播放列表时间轴，秒）；任一段的 CLPI 读不出即 None。

        只收落在各段 IN/OUT 之内的入口点，再加上该段在时间轴上的偏移。
        """
        cached = _keyframe_cache.get(self._cache_key)
        if cached is not None:
            return cached
        times: list[float] = []
        offset = 0.0
        for clip in self.clips:
            pts_list = read_clpi_keyframes(clip.path)
            if pts_list is None:
                logger.warning(
                    "原盘关键帧索引不可用（CLPI 缺失或无 EP_map）：%s，VOD 分片退回会话式播放",
                    clip.path,
                )
                return None
            times.extend(
                offset + (pts - clip.in_time) / MPLS_CLOCK_HZ
                for pts in pts_list
                if clip.in_time <= pts < clip.out_time
            )
            offset += clip.duration_s
        if not times:
            return None
        if times[0] > 0.001:
            times.insert(0, 0.0)
        index = KeyframeIndex(times_s=tuple(times))
        if len(_keyframe_cache) >= _CACHE_MAX:
            _keyframe_cache.clear()
        _keyframe_cache[self._cache_key] = index
        return index

    def keyframe_interval_s(self) -> float | None:
        """平均关键帧间隔（秒）——决策引擎判断能否 remux 的输入（web-player.md §3.5）。"""
        index = self.keyframe_index()
        if index is None or len(index.times_s) < 2:
            return None
        return self.duration_s / len(index.times_s)

    @property
    def _cache_key(self) -> tuple[str, str, tuple[tuple[str, int, int], ...]]:
        return (
            str(self.disc_dir),
            self.playlist_name,
            tuple((c.clip_id, c.in_time, c.out_time) for c in self.clips),
        )


#: 关键帧表缓存：一张盘的 EP_map 只有几十 KB，但在 NFS 上逐段读也是网络往返；
#: 同一部片反复开会话（seek 重启不重读，但换音轨/字幕会重开）不该重算。
_keyframe_cache: dict[tuple, KeyframeIndex] = {}
#: 回盘上读 MPLS 的结果缓存：(原盘目录, PLAYLIST 目录 mtime) → 主播放列表
_playlist_cache: dict[tuple[str, int], MplsPlaylist | None] = {}
_CACHE_MAX = 128


def disc_source_for_file(file: LibraryFile, *, read_disc: bool = True) -> DiscSource | None:
    """台账行 → 播放源；非蓝光原盘、或清单不可得时返回 None。

    ``read_disc=False`` 是浏览场景（列表/详情 DTO）的口径：只认台账里的
    ``disc_playlist``，绝不回盘上读——每个原盘条目读一次 PLAYLIST 目录，在
    网络挂载上就是一次往返，列表页有上百张盘时不可接受。播放场景传 True，
    存量未补探的行退回读盘（结果缓存）。
    """
    if (file.container or "") != "bluray":
        return None
    disc_dir = Path(file.file_path)
    record = file.disc_playlist
    if not disc_playlist_stale(record):
        assert isinstance(record, dict)
        clips = tuple(
            DiscClip(
                clip_id=str(clip["id"]),
                path=disc_dir / "BDMV" / "STREAM" / f"{clip['id']}.m2ts",
                in_time=int(clip["in"]),
                out_time=int(clip["out"]),
            )
            for clip in record.get("clips") or []
            if isinstance(clip, dict) and {"id", "in", "out"} <= clip.keys()
        )
        if clips:
            return DiscSource(
                disc_dir=disc_dir, playlist_name=str(record.get("name") or ""), clips=clips
            )
    if not read_disc:
        return None
    playlist = _read_playlist_cached(disc_dir)
    if playlist is None:
        return None
    return disc_source_from_playlist(disc_dir, playlist)


def disc_source_from_playlist(disc_dir: Path, playlist: MplsPlaylist) -> DiscSource:
    """主播放列表 → 播放源（探测链路与读盘回退共用的装配）。"""
    stream_dir = disc_dir / "BDMV" / "STREAM"
    return DiscSource(
        disc_dir=disc_dir,
        playlist_name=playlist.path.name,
        clips=tuple(
            DiscClip(
                clip_id=item.clip_id,
                path=_existing_stream(stream_dir, item.clip_id),
                in_time=item.in_time,
                out_time=item.out_time,
            )
            for item in playlist.items
        ),
    )


def _existing_stream(stream_dir: Path, clip_id: str) -> Path:
    """剪辑文件路径；扩展名大小写由制作工具决定，先按实际目录项匹配。"""
    default = stream_dir / f"{clip_id}.m2ts"
    if default.is_file():
        return default
    try:
        for path in stream_dir.iterdir():
            if path.stem == clip_id and path.suffix.lower() == ".m2ts":
                return path
    except OSError:
        pass
    return default


def _read_playlist_cached(disc_dir: Path) -> MplsPlaylist | None:
    playlist_dir = disc_dir / "BDMV" / "PLAYLIST"
    try:
        key = (str(disc_dir), playlist_dir.stat().st_mtime_ns)
    except OSError:
        return None
    if key in _playlist_cache:
        return _playlist_cache[key]
    playlist = read_main_playlist(disc_dir)
    if len(_playlist_cache) >= _CACHE_MAX:
        _playlist_cache.clear()
    _playlist_cache[key] = playlist
    return playlist
