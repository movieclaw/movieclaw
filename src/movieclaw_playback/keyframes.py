"""全片关键帧索引——服务端生成 VOD 播放列表的地基（docs/design/web-player.md §12）。

为什么需要全片索引而不是采样：VOD 播放列表要在开会话时一次性写出**每个分片
的精确时长**（EXTINF）。直通档 ``-c:v copy`` 只能切在源片已有的关键帧上，
分片边界必须与 ffmpeg 实际会切的位置逐一吻合，差半个 GOP 播放器就会在
seek 时拿错分片。采样估出的平均间隔（media_probe）只够回答「稀不稀疏」，
回答不了「第 137 个分片从哪一秒开始」。

两条读取路径，按容器分：

- **Matroska（mkv）**：解析文件里的 Cues 索引元素。remux 场景的 mkv 基本
  出自 mkvmerge，它默认给视频轨每个 I 帧写一个 CuePoint；Cues 整块只有
  几十 KB 且 SeekHead 直接给出偏移，读一次是毫秒级。**不能**用 ffprobe：
  mkv 没有全局包索引，ffprobe 列包要顺序读完整个文件，30 GB 的 remux
  要读几分钟。（Jellyfin 的 MatroskaKeyframeExtractor 同款思路。）
- **其余容器（mp4/mov 等）**：ffprobe 列视频包挑关键帧。mp4 的包元数据
  全部来自 moov 索引，ffprobe 不需要碰 mdat，大文件也只是几 MB 的 I/O。
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("movieclaw_playback.keyframes")

_FFPROBE_TIMEOUT = 120.0

# --- Matroska EBML 元素 ID（保留前导标记位的原始形式） ------------------------
_EBML_HEADER = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEK_HEAD = 0x114D9B74
_SEEK = 0x4DBB
_SEEK_ID = 0x53AB
_SEEK_POSITION = 0x53AC
_INFO = 0x1549A966
_TIMESTAMP_SCALE = 0x2AD7B1
_CUES = 0x1C53BB6B
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_TRACK_TYPE_VIDEO = 1


@dataclass(frozen=True)
class KeyframeIndex:
    """一个文件的视频关键帧时间表（秒，升序）。"""

    times_s: tuple[float, ...]


#: (路径, mtime_ns, 大小) → 索引。mkv 解析毫秒级本可不缓存，但 mp4 走
#: ffprobe 可能上秒；同一文件反复开会话（切集来回、降档重开）不该重算。
#: 满了整体清空——纯加速缓存，命中率短暂下降无所谓（media_probe 同款取舍）。
_index_cache: dict[tuple[str, int, int], KeyframeIndex] = {}
_INDEX_CACHE_MAX = 256


def read_keyframe_index(path: str | Path) -> KeyframeIndex | None:
    """读取全片关键帧索引；失败返回 None（调用方退回旧的会话式播放）。

    结果按 (路径, mtime, 大小) 缓存；文件被换掉（洗版、改名归并）时三元组
    变化，缓存自然失效。只缓存成功结果——瞬时 IO 故障不该被钉死。
    """
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _index_cache.get(key)
    if cached is not None:
        return cached
    index = _read_keyframe_index(path)
    if index is not None:
        if len(_index_cache) >= _INDEX_CACHE_MAX:
            _index_cache.clear()
        _index_cache[key] = index
    return index


def _read_keyframe_index(path: Path) -> KeyframeIndex | None:
    try:
        if path.suffix.lower() in {".mkv", ".webm"}:
            times = _read_matroska_cues(path)
        else:
            times = _ffprobe_keyframes(path)
    # ValueError 是解析器自己抛的（不是 Matroska / 缺 Cues / 结构异常），
    # IndexError 是截断或损坏的文件让 _read_vint 读越了界。这里是「按合同
    # 失败返回 None」的唯一出口——扩展名叫 .mkv 不代表内容真是 Matroska，
    # 漏接任何一类都会把开会话接口打成 500，整个文件从此放不了（VOD 只是
    # 优化，读不出索引本该安静退回旧的会话式播放）。
    except (OSError, ValueError, IndexError) as exc:
        logger.warning("关键帧索引读取失败：%s（%s）", path, exc)
        return None
    if not times:
        return None
    # 索引必须从 0 附近开始：首个关键帧就是首帧（任何正常视频都如此），
    # 若 Cues 缺了片头（个别残缺文件），补一个 0 保证第一个分片存在。
    if times[0] > 0.001:
        times = [0.0, *times]
    return KeyframeIndex(times_s=tuple(times))


# --- Matroska Cues ----------------------------------------------------------


def _read_vint(data: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int]:
    """读一个 EBML 变长整数，返回 (值, 新位置)。keep_marker=True 用于元素 ID。"""
    first = data[pos]
    length = 1
    mask = 0x80
    while mask and not (first & mask):
        length += 1
        mask >>= 1
    if not mask:
        raise ValueError("非法的 EBML 变长整数")
    value = first if keep_marker else first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, pos + length


def _iter_children(data: bytes, start: int, end: int):
    """遍历 [start, end) 区间内的 EBML 子元素，产出 (id, body_start, body_end)。"""
    pos = start
    while pos < end:
        element_id, pos = _read_vint(data, pos, keep_marker=True)
        size, pos = _read_vint(data, pos, keep_marker=False)
        yield element_id, pos, pos + size
        pos += size


def _read_uint(data: bytes, start: int, end: int) -> int:
    return int.from_bytes(data[start:end], "big")


def _read_matroska_cues(path: Path) -> list[float]:
    """从 mkv 的 SeekHead 定位 Cues 与 Info，解出关键帧时间表（秒）。"""
    with path.open("rb") as f:
        # 头部读 64KB：EBML 头 + Segment 头 + SeekHead 一定在这里面
        head = f.read(65536)
        pos = 0
        element_id, pos = _read_vint(head, pos, keep_marker=True)
        if element_id != _EBML_HEADER:
            raise ValueError("不是 Matroska 文件")
        size, pos = _read_vint(head, pos, keep_marker=False)
        pos += size
        element_id, pos = _read_vint(head, pos, keep_marker=True)
        if element_id != _SEGMENT:
            raise ValueError("缺少 Segment 元素")
        _, pos = _read_vint(head, pos, keep_marker=False)
        segment_start = pos  # SeekHead 的偏移都相对这里

        # SeekHead：id → 相对偏移
        offsets: dict[int, int] = {}
        for element_id, body_start, body_end in _iter_children(head, pos, len(head)):
            if element_id != _SEEK_HEAD:
                # SeekHead 总在 Segment 最前面；遇到第一个别的元素就停
                break
            for child_id, c_start, c_end in _iter_children(head, body_start, body_end):
                if child_id != _SEEK:
                    continue
                target_id = target_pos = None
                for f_id, f_start, f_end in _iter_children(head, c_start, c_end):
                    if f_id == _SEEK_ID:
                        target_id = _read_uint(head, f_start, f_end)
                    elif f_id == _SEEK_POSITION:
                        target_pos = _read_uint(head, f_start, f_end)
                if target_id is not None and target_pos is not None:
                    offsets[target_id] = target_pos
        if _CUES not in offsets:
            raise ValueError("SeekHead 里没有 Cues（此文件缺关键帧索引）")

        # 视频轨号：Cues 里混着音频/字幕轨的 CuePoint（mkvmerge 会为多轨写
        # cue），不按轨过滤会把索引搅密好几倍，分片边界全错。
        video_tracks: set[int] = set()
        if _TRACKS in offsets:
            f.seek(segment_start + offsets[_TRACKS])
            tracks = f.read(65536)
            p = 0
            element_id, p = _read_vint(tracks, p, keep_marker=True)
            size, p = _read_vint(tracks, p, keep_marker=False)
            for child_id, c_start, c_end in _iter_children(
                tracks, p, min(p + size, len(tracks))
            ):
                if child_id != _TRACK_ENTRY:
                    continue
                number = kind = None
                for f_id, f_start, f_end in _iter_children(tracks, c_start, c_end):
                    if f_id == _TRACK_NUMBER:
                        number = _read_uint(tracks, f_start, f_end)
                    elif f_id == _TRACK_TYPE:
                        kind = _read_uint(tracks, f_start, f_end)
                if kind == _TRACK_TYPE_VIDEO and number is not None:
                    video_tracks.add(number)

        timestamp_scale = 1_000_000  # EBML 默认：每 tick 1ms
        if _INFO in offsets:
            f.seek(segment_start + offsets[_INFO])
            info = f.read(4096)
            p = 0
            element_id, p = _read_vint(info, p, keep_marker=True)
            size, p = _read_vint(info, p, keep_marker=False)
            for child_id, c_start, c_end in _iter_children(info, p, min(p + size, len(info))):
                if child_id == _TIMESTAMP_SCALE:
                    timestamp_scale = _read_uint(info, c_start, c_end)

        # Cues 整块读进来（几十 KB~几 MB）
        f.seek(segment_start + offsets[_CUES])
        header = f.read(16)
        p = 0
        element_id, p = _read_vint(header, p, keep_marker=True)
        if element_id != _CUES:
            raise ValueError("SeekHead 指向的位置不是 Cues")
        size, p = _read_vint(header, p, keep_marker=False)
        f.seek(segment_start + offsets[_CUES])
        cues = f.read(p + size)

        times: list[float] = []
        for child_id, c_start, c_end in _iter_children(cues, p, len(cues)):
            if child_id != _CUE_POINT:
                continue
            cue_time = None
            is_video = not video_tracks  # Tracks 解析失败时不过滤，聊胜于无
            for f_id, f_start, f_end in _iter_children(cues, c_start, c_end):
                if f_id == _CUE_TIME:
                    cue_time = _read_uint(cues, f_start, f_end)
                elif f_id == _CUE_TRACK_POSITIONS:
                    for g_id, g_start, g_end in _iter_children(cues, f_start, f_end):
                        if g_id == _CUE_TRACK:
                            if _read_uint(cues, g_start, g_end) in video_tracks:
                                is_video = True
                            break
            if is_video and cue_time is not None:
                times.append(cue_time * timestamp_scale / 1_000_000_000)
        # 同一时间可能有多条（多视频轨/重复 cue），去重再排序
        return sorted(set(times))


# --- 其余容器：ffprobe ------------------------------------------------------


def _ffprobe_keyframes(path: Path) -> list[float]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,flags",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FFPROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe 列关键帧失败：%s（%s）", path, exc)
        return []
    if proc.returncode != 0:
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    times: list[float] = []
    for packet in payload.get("packets") or []:
        if "K" not in (packet.get("flags") or ""):
            continue
        try:
            times.append(float(packet.get("pts_time")))
        except (TypeError, ValueError):
            continue
    times.sort()
    return times
