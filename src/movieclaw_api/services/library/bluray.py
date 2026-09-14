"""蓝光原盘 MPLS 主播放列表、CLPI 元数据解析与 ffprobe 结果合并。

BDMV 的 m2ts 是 MPEG-TS 流文件，ffprobe 能稳定拿到编码、声道与流 PID，
但不少原盘没有把语言描述符写进 TS；真实语言位于同编号的
``BDMV/CLIPINF/<clip_id>.clpi``。本模块只读 CLPI 的 ProgramInfo 小节，并用
PID 关联两边的轨道，绝不按数组下标猜测——播放列表裁剪、交互图形轨或工具
识别差异都可能让两边数量不同，错一位就会把整组语言写错。

本模块同时承担原盘**播放**所需的两样结构化数据（docs/design/disc-playback.md）：

- MPLS 的 PlayListMark（章节入口标记）——原盘章节的唯一来源，m2ts 本身没有
  章节；
- CLPI 的 EP_map（蓝光自带的 I 帧入口表：PTS 与源包号）——原盘的天然关键帧
  索引。77 GB 的 m2ts 用 ffprobe 列包要顺序读完整个文件，EP_map 只有几十 KB。

解析失败属于可选元数据缺失：调用方保留原 ffprobe 结果，扫描照常完成。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from movieclaw_api.services.media_probe import MediaSpec

logger = logging.getLogger("movieclaw_api.bluray")

#: MPLS / CLPI 的时间戳单位（33 位 90 kHz PTS 的高 32 位，即 45 kHz）
MPLS_CLOCK_HZ = 45_000
_MPLS_CLOCK_HZ = MPLS_CLOCK_HZ

#: 蓝光主视频流的固定 PID。EP_map 可能给多路流各建一张表（Dolby Vision 双层
#: 盘的增强层也有），关键帧索引只认主视频这一张。
_PRIMARY_VIDEO_PID = 0x1011

#: PlayListMark 的 mark_type：1 = 章节入口（entry mark），2 = 链接点（link
#: point，给 BD-J/菜单跳转用，不是给人看的章节）
_MARK_TYPE_ENTRY = 1


class MplsParseError(ValueError):
    """MPLS 结构不完整或字段越界。"""


@dataclass(frozen=True)
class MplsPlayItem:
    """播放列表里的一个剪辑及有效播放区间。"""

    clip_id: str
    in_time: int
    out_time: int

    @property
    def duration_seconds(self) -> float:
        return max(0, self.out_time - self.in_time) / _MPLS_CLOCK_HZ


@dataclass(frozen=True)
class MplsPlaylist:
    """可用于选主片的 MPLS 播放列表。

    ``marks`` 是章节入口标记，元素为**播放列表时间轴**上的毫秒起点（各
    PlayItem 的时长累加，与播放时 concat 拼接出来的时间轴同一口径）。
    """

    path: Path
    items: tuple[MplsPlayItem, ...]
    marks: tuple[int, ...] = ()

    @property
    def duration_seconds(self) -> int:
        return round(sum(item.duration_seconds for item in self.items))

    @property
    def clip_ids(self) -> tuple[str, ...]:
        return tuple(item.clip_id for item in self.items)

    @property
    def has_loops(self) -> bool:
        """同一剪辑以同一 IN_time 出现两次即为循环列表。

        这是 BDInfo（Jellyfin 选主片所用的库）``TSPlaylistFile.HasLoops`` 的
        同款判据。防拷贝诱饵列表把同一剪辑循环引用几百次、时长虚高数倍，只按
        时长取最长必然中招——实测 141 张盘约 50 张选错，台账时长错一倍以上。
        """
        seen: set[tuple[str, int]] = set()
        for item in self.items:
            key = (item.clip_id, item.in_time)
            if key in seen:
                return True
            seen.add(key)
        return False

    def chapters(self) -> list[dict]:
        """章节入口标记 → 台账 ``chapters`` 列的元素形态（与 ffprobe 章节同构）。

        原盘的 m2ts 没有内嵌章节，MPLS 的 PlayListMark 是唯一来源。起点取播放
        列表时间轴；标题恒 None（蓝光不给章节命名，展示层按序号补「第 N 章」）。
        """
        return [{"start_ms": start_ms, "end_ms": None, "title": None} for start_ms in self.marks]


def parse_mpls_playlist(data: bytes, *, path: Path = Path("unknown.mpls")) -> MplsPlaylist:
    """解析 MPLS 的 PlayList 与 PlayListMark 小节。

    PlayList 只读取选择主片所需的 clip id 与 IN/OUT 时间；STN、角度和 SubPath
    都保留在完整原盘目录中，不需要为了入库而解释或改写。PlayListMark 解析
    失败只丢章节不丢列表——章节是可选元数据。
    """
    if len(data) < 20 or data[:4] != b"MPLS":
        raise MplsParseError("文件签名不是 MPLS 或文件头过短")
    playlist_offset = int.from_bytes(data[8:12], "big")
    if playlist_offset + 10 > len(data):
        raise MplsParseError("PlayList 偏移越界")
    section_length = int.from_bytes(data[playlist_offset : playlist_offset + 4], "big")
    section_end = playlist_offset + 4 + section_length
    if section_end > len(data):
        raise MplsParseError("PlayList 长度越界")
    pos = playlist_offset + 4
    _require(pos, 6, section_end, "PlayList header", error=MplsParseError)
    pos += 2  # reserved
    play_item_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 4  # number_of_play_items + number_of_sub_paths
    items: list[MplsPlayItem] = []
    for _ in range(play_item_count):
        _require(pos, 2, section_end, "PlayItem length", error=MplsParseError)
        item_length = int.from_bytes(data[pos : pos + 2], "big")
        item_start = pos + 2
        item_end = item_start + item_length
        _require(item_start, item_length, section_end, "PlayItem", error=MplsParseError)
        # clip id(5) + codec id(4) + flags(2) + stc id(1) + in/out(8)
        _require(item_start, 20, item_end, "PlayItem core", error=MplsParseError)
        clip_raw = data[item_start : item_start + 5]
        codec_raw = data[item_start + 5 : item_start + 9]
        try:
            clip_id = clip_raw.decode("ascii")
            codec = codec_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise MplsParseError("PlayItem 标识不是 ASCII") from exc
        if not clip_id.isdigit() or codec != "M2TS":
            raise MplsParseError("PlayItem 的 clip/codec 标识无效")
        in_time = int.from_bytes(data[item_start + 12 : item_start + 16], "big")
        out_time = int.from_bytes(data[item_start + 16 : item_start + 20], "big")
        if out_time < in_time:
            raise MplsParseError("PlayItem 的 OUT_time 早于 IN_time")
        items.append(MplsPlayItem(clip_id=clip_id, in_time=in_time, out_time=out_time))
        pos = item_end
    if not items:
        raise MplsParseError("播放列表没有 PlayItem")
    try:
        marks = _parse_playlist_marks(data, tuple(items))
    except MplsParseError as exc:
        logger.warning("蓝光 MPLS 章节标记解析失败，按无章节处理：%s（%s）", path, exc)
        marks = ()
    return MplsPlaylist(path=path, items=tuple(items), marks=marks)


def _parse_playlist_marks(data: bytes, items: tuple[MplsPlayItem, ...]) -> tuple[int, ...]:
    """PlayListMark 小节 → 播放列表时间轴上的章节起点（毫秒，升序去重）。

    文件头 ``0x0c`` 是 PlayListMark 的绝对偏移。每个标记 14 字节：保留(1)、
    mark_type(1)、ref_to_PlayItem_id(2)、mark_time_stamp(4)、entry_ES_PID(2)、
    duration(4)。只收 entry mark；时间戳是所属 PlayItem 的剪辑时间，减去该
    PlayItem 的 IN_time 再加上前面各段的累计时长，才是播放列表时间轴。
    """
    mark_offset = int.from_bytes(data[12:16], "big")
    if mark_offset == 0:
        return ()
    if mark_offset + 6 > len(data):
        raise MplsParseError("PlayListMark 偏移越界")
    section_length = int.from_bytes(data[mark_offset : mark_offset + 4], "big")
    section_end = mark_offset + 4 + section_length
    if section_end > len(data):
        raise MplsParseError("PlayListMark 长度越界")
    pos = mark_offset + 4
    _require(pos, 2, section_end, "PlayListMark header", error=MplsParseError)
    mark_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    # 各 PlayItem 在播放列表时间轴上的起点（45 kHz）
    offsets: list[int] = []
    cursor = 0
    for item in items:
        offsets.append(cursor)
        cursor += max(0, item.out_time - item.in_time)
    marks: set[int] = set()
    for _ in range(mark_count):
        _require(pos, 14, section_end, "PlayListMark entry", error=MplsParseError)
        mark_type = data[pos + 1]
        item_index = int.from_bytes(data[pos + 2 : pos + 4], "big")
        timestamp = int.from_bytes(data[pos + 4 : pos + 8], "big")
        pos += 14
        if mark_type != _MARK_TYPE_ENTRY or item_index >= len(items):
            continue
        item = items[item_index]
        relative = min(max(timestamp, item.in_time), item.out_time) - item.in_time
        marks.add(round((offsets[item_index] + relative) * 1000 / _MPLS_CLOCK_HZ))
    return tuple(sorted(marks))


#: 台账 ``library_file.disc_playlist`` 的结构版本。字段集变了就 +1，补探会把
#: 旧版本的行重算一遍（与 CLPI 语言版本戳同一思路）。
DISC_PLAYLIST_VERSION = 1


def disc_playlist_record(playlist: MplsPlaylist | None) -> dict:
    """主播放列表 → 台账 ``disc_playlist`` 列（docs/design/disc-playback.md §3.2）。

    只落播放所需的最小事实：列表名、各剪辑 id 与 IN/OUT（45 kHz 整数）。播放
    时据此构造 concat 清单与关键帧索引，不必再读盘上成百上千个 MPLS——诱饵盘
    动辄上千个列表，NFS 上每次全读要一到三秒。

    ``playlist=None`` 写「读过但没有主播放列表」的哨兵（剪辑清单为空）：残缺盘
    不该每轮补探都重读一遍，播放时播放源解析器见到空清单会再试一次读盘。
    """
    if playlist is None:
        return {"version": DISC_PLAYLIST_VERSION, "name": None, "clips": []}
    return {
        "version": DISC_PLAYLIST_VERSION,
        "name": playlist.path.name,
        "clips": [
            {"id": item.clip_id, "in": item.in_time, "out": item.out_time}
            for item in playlist.items
        ],
    }


def disc_playlist_stale(record: object) -> bool:
    """台账里的 ``disc_playlist`` 是否缺失或版本落后，需要补探重算。"""
    return not isinstance(record, dict) or record.get("version") != DISC_PLAYLIST_VERSION


def read_main_playlist(disc_dir: Path) -> MplsPlaylist | None:
    """从完整 BDMV 中选择主播放列表；损坏/伪列表只降级，不阻断扫描。

    候选必须引用实际存在的 STREAM 文件，且**不含循环剪辑**（``has_loops``，
    防拷贝诱饵列表的判据，与 Jellyfin/BDInfo 同口径）。按有效播放时长优先，
    同一剪辑序列只保留一个，避免不同语言/菜单入口的等价 MPLS 放大候选数量。
    """
    playlist_dir = disc_dir / "BDMV" / "PLAYLIST"
    stream_dir = disc_dir / "BDMV" / "STREAM"
    if not playlist_dir.is_dir() or not stream_dir.is_dir():
        return None
    by_sequence: dict[tuple[str, ...], MplsPlaylist] = {}
    try:
        paths = sorted(
            path
            for path in playlist_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".mpls"
        )
        streams = {
            path.stem: path
            for path in stream_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".m2ts"
        }
    except OSError:
        return None
    for path in paths:
        try:
            playlist = parse_mpls_playlist(path.read_bytes(), path=path)
        except (OSError, MplsParseError) as exc:
            logger.warning("蓝光 MPLS 解析失败，跳过候选：%s（%s）", path, exc)
            continue
        if not all(clip_id in streams for clip_id in playlist.clip_ids):
            continue
        if playlist.has_loops:
            continue
        current = by_sequence.get(playlist.clip_ids)
        if current is None or playlist.duration_seconds > current.duration_seconds:
            by_sequence[playlist.clip_ids] = playlist
    return max(
        by_sequence.values(),
        key=lambda row: (row.duration_seconds, len(row.items), row.path.name),
        default=None,
    )


# 写进音轨/字幕 JSON 的内部版本戳。API 会显式投影公开字段，因此不会把它暴露
# 给前端；存量补探靠它区分“旧数据尚未读 CLPI”和“读过但语言确实未知”。
CLPI_LANGUAGE_VERSION = 1
_VERSION_KEY = "clpi_language_version"

_AUDIO_TYPES = {
    0x03,  # MPEG-1 Audio
    0x04,  # MPEG-2 Audio
    0x80,  # LPCM
    0x81,  # AC-3
    0x82,  # DTS
    0x83,  # Dolby TrueHD
    0x84,  # E-AC-3
    0x85,  # DTS-HD
    0x86,  # DTS-HD MA
    0xA1,  # secondary E-AC-3
    0xA2,  # secondary DTS-HD
}
# 0x91 是菜单交互图形，不是用户可选择的影片字幕；0xA0 虽也带
# language 字段，但不是标准 PGS/TextST 字幕类型。两者都不回填到
# subtitle_streams，避免与 ffprobe 的影片字幕误关联。
_SUBTITLE_TYPES = {0x90, 0x92}  # PGS、TextST


class ClpiParseError(ValueError):
    """CLPI 结构不完整或字段越界；错误文本可直接写入中文日志。"""


@dataclass(frozen=True)
class ClpiLanguages:
    """按 MPEG-TS PID 分组的语言表；只包含可用于音轨/字幕的流。"""

    audio: dict[int, str]
    subtitles: dict[int, str]


def clpi_path_for_stream(stream_path: Path) -> Path | None:
    """主 m2ts → 同编号 CLPI；路径不是标准 BDMV/STREAM 布局时返回 None。"""
    if stream_path.suffix.lower() != ".m2ts":
        return None
    stream_dir = stream_path.parent
    bdmv_dir = stream_dir.parent
    if stream_dir.name.upper() != "STREAM" or bdmv_dir.name.upper() != "BDMV":
        return None
    return bdmv_dir / "CLIPINF" / f"{stream_path.stem}.clpi"


def read_clpi_languages(stream_path: Path) -> ClpiLanguages | None:
    """读取主流对应的 CLPI；主副本都不可用时才降级为 None。

    标准蓝光会在 ``BDMV/BACKUP/CLIPINF`` 保留一份镜像。部分盘面或
    网盘拷贝的主 CLPI 缺失/损坏，此时仍应尝试备份，不阻断扫描。
    """
    primary_path = clpi_path_for_stream(stream_path)
    if primary_path is None:
        return None
    backup_path = primary_path.parent.parent / "BACKUP" / "CLIPINF" / primary_path.name
    for clpi_path in (primary_path, backup_path):
        if not clpi_path.is_file():
            continue
        try:
            return parse_clpi_languages(clpi_path.read_bytes())
        except (OSError, ClpiParseError) as exc:
            logger.warning(
                "蓝光 CLPI 语言元数据解析失败，继续尝试降级路径：%s（%s）", clpi_path, exc
            )
    return None


def parse_clpi_languages(data: bytes) -> ClpiLanguages:
    """解析 CLPI ProgramInfo，返回 PID → ISO 639-2 三字码。

    文件头 ``0x0c`` 保存 ProgramInfo 的绝对偏移。每条流先给 PID，再给带长度
    的 StreamCodingInfo；不同编码的语言偏移不同，因此始终尊重条目长度并做
    边界校验。一个 PID 在多个 program sequence 中声明冲突语言时将其丢弃，
    宁可保持未知也不写入不确定结果。
    """
    if len(data) < 16:
        raise ClpiParseError("文件头不足 16 字节")
    if data[:4] != b"HDMV":
        raise ClpiParseError("文件签名不是 HDMV")

    program_offset = int.from_bytes(data[12:16], "big")
    if program_offset + 6 > len(data):
        raise ClpiParseError("ProgramInfo 偏移越界")

    section_length = int.from_bytes(data[program_offset : program_offset + 4], "big")
    section_end = program_offset + 4 + section_length
    if section_end > len(data):
        raise ClpiParseError("ProgramInfo 长度越界")

    pos = program_offset + 4
    _require(pos, 2, section_end, "ProgramInfo header")
    pos += 1  # reserved
    program_count = data[pos]
    pos += 1

    audio: dict[int, str] = {}
    subtitles: dict[int, str] = {}
    audio_conflicts: set[int] = set()
    subtitle_conflicts: set[int] = set()

    for _ in range(program_count):
        _require(pos, 8, section_end, "program sequence")
        pos += 4  # spn_program_sequence_start
        pos += 2  # program_map_pid
        stream_count = data[pos]
        pos += 1
        pos += 1  # number_of_groups

        for _ in range(stream_count):
            _require(pos, 3, section_end, "stream entry")
            pid = int.from_bytes(data[pos : pos + 2], "big")
            coding_length = data[pos + 2]
            pos += 3
            _require(pos, coding_length, section_end, "StreamCodingInfo")
            if coding_length < 1:
                raise ClpiParseError("StreamCodingInfo 长度为 0")

            coding_type = data[pos]
            language: str | None = None
            target: dict[int, str] | None = None
            conflicts: set[int] | None = None
            if coding_type in _AUDIO_TYPES:
                language = _language_at(data, pos + 2, pos + coding_length)
                target, conflicts = audio, audio_conflicts
            elif coding_type in _SUBTITLE_TYPES:
                language_offset = 2 if coding_type == 0x92 else 1
                language = _language_at(data, pos + language_offset, pos + coding_length)
                target, conflicts = subtitles, subtitle_conflicts

            if language is not None and target is not None and conflicts is not None:
                _record_language(target, conflicts, pid, language)
            pos += coding_length

    return ClpiLanguages(audio=audio, subtitles=subtitles)


def read_clpi_keyframes(stream_path: Path) -> list[int] | None:
    """读取剪辑的关键帧 PTS 表（45 kHz，升序）；CLPI 缺失/损坏/无 EP_map 时 None。

    与 ``read_clpi_languages`` 同样先主 CLPI 后 ``BDMV/BACKUP`` 镜像。返回 None
    时调用方退回「关键帧未知」——网页播放器保守不走 remux，Jellyfin 层的多剪辑
    HLS 退回会话式播放，都不阻断。
    """
    primary_path = clpi_path_for_stream(stream_path)
    if primary_path is None:
        return None
    backup_path = primary_path.parent.parent / "BACKUP" / "CLIPINF" / primary_path.name
    for clpi_path in (primary_path, backup_path):
        if not clpi_path.is_file():
            continue
        try:
            keyframes = parse_clpi_keyframes(clpi_path.read_bytes())
        except (OSError, ClpiParseError) as exc:
            logger.warning("蓝光 CLPI 关键帧表解析失败，继续尝试降级路径：%s（%s）", clpi_path, exc)
            continue
        return keyframes or None
    return None


def parse_clpi_keyframes(data: bytes) -> list[int]:
    """解析 CLPI 的 CPI/EP_map，返回主视频流每个入口点的 PTS（45 kHz，升序去重）。

    文件头 ``0x10`` 是 CPI 的绝对偏移。CPI 先给长度（0 = 没有 EP_map，返回空表）
    与类型，随后是 EP_map：每路流一条 12 字节索引（PID、类型、粗/细表条目数、
    该流表的相对起址），流表以粗表（8 字节：ref_to_EP_fine_id 18 位、
    PTS_EP_coarse 14 位、SPN 32 位）和细表（4 字节：角度标志 1 位、I 帧尾偏移
    3 位、PTS_EP_fine 11 位、SPN 17 位）两级组织。完整 PTS 的拼法照抄 libbluray
    ``clpi_access_point``：``((coarse & ~1) << 18) + (fine << 8)``——粗表最低位与
    细表最高位是同一位，低 8 位丢失（约 5.7 毫秒，切片边界判定可以接受）。

    有多路视频（Dolby Vision 双层）时只取主视频 PID 0x1011 的表；没有它就取
    PID 最小的一路。
    """
    if len(data) < 20:
        raise ClpiParseError("文件头不足 20 字节")
    if data[:4] != b"HDMV":
        raise ClpiParseError("文件签名不是 HDMV")
    cpi_offset = int.from_bytes(data[16:20], "big")
    if cpi_offset == 0:
        return []
    if cpi_offset + 4 > len(data):
        raise ClpiParseError("CPI 偏移越界")
    cpi_length = int.from_bytes(data[cpi_offset : cpi_offset + 4], "big")
    if cpi_length == 0:
        return []
    cpi_end = cpi_offset + 4 + cpi_length
    if cpi_end > len(data):
        raise ClpiParseError("CPI 长度越界")
    # 2 字节：12 位保留 + 4 位 cpi_type；EP_map 内的相对地址以其后为基准
    ep_map_pos = cpi_offset + 4 + 2
    _require(ep_map_pos, 2, cpi_end, "EP_map header")
    stream_count = data[ep_map_pos + 1]
    pos = ep_map_pos + 2
    entries: list[tuple[int, int, int, int]] = []  # (pid, coarse 数, fine 数, 起址)
    for _ in range(stream_count):
        _require(pos, 12, cpi_end, "EP_map stream entry")
        raw = int.from_bytes(data[pos : pos + 12], "big")
        pid = raw >> 80
        coarse_count = (raw >> 50) & 0xFFFF
        fine_count = (raw >> 32) & 0x3FFFF
        start = raw & 0xFFFFFFFF
        entries.append((pid, coarse_count, fine_count, start))
        pos += 12
    if not entries:
        return []
    chosen = next((e for e in entries if e[0] == _PRIMARY_VIDEO_PID), None) or min(
        entries, key=lambda e: e[0]
    )
    _, coarse_count, fine_count, start = chosen
    stream_pos = ep_map_pos + start
    _require(stream_pos, 4, cpi_end, "EP_map stream table")
    fine_start = int.from_bytes(data[stream_pos : stream_pos + 4], "big")
    coarse_pos = stream_pos + 4
    _require(coarse_pos, coarse_count * 8, cpi_end, "EP_map coarse table")
    coarse: list[tuple[int, int]] = []  # (ref_to_EP_fine_id, PTS_EP_coarse)
    for i in range(coarse_count):
        raw = int.from_bytes(data[coarse_pos + i * 8 : coarse_pos + i * 8 + 8], "big")
        coarse.append((raw >> 46, (raw >> 32) & 0x3FFF))
    fine_pos = stream_pos + fine_start
    _require(fine_pos, fine_count * 4, cpi_end, "EP_map fine table")
    fine_pts: list[int] = []
    for i in range(fine_count):
        raw = int.from_bytes(data[fine_pos + i * 4 : fine_pos + i * 4 + 4], "big")
        fine_pts.append((raw >> 17) & 0x7FF)
    keyframes: set[int] = set()
    for i, (fine_from, pts_coarse) in enumerate(coarse):
        fine_to = coarse[i + 1][0] if i + 1 < len(coarse) else fine_count
        for j in range(fine_from, min(fine_to, fine_count)):
            keyframes.add(((pts_coarse & ~1) << 18) + (fine_pts[j] << 8))
    return sorted(keyframes)


def enrich_spec_with_clpi(spec: MediaSpec, languages: ClpiLanguages) -> MediaSpec:
    """按 PID 给缺语言的 ffprobe 流回填 CLPI，并给所有流写完成版本戳。

    列表顺序保持原样：字幕抽取和播放器使用数组下标生成 ``0:s:<k>`` 引用，
    为了“对齐”而重排会让已保存的轨引用失效。已有语言只标记来源为 ffprobe，
    绝不被 CLPI 覆盖。
    """

    def enrich(raw: dict, fallback: dict[int, str]) -> dict:
        stream = dict(raw)
        language = stream.get("language")
        # ffprobe 常用 und 或空字符串表示“未知”，语义上与 None 相同，
        # 不应阻止 CLPI 回填。只保护确实有效的 ffprobe 语言。
        has_language = isinstance(language, str) and language.strip().lower() not in {"", "und"}
        pid = stream.get("pid")
        if not has_language and isinstance(pid, int) and pid in fallback:
            stream["language"] = fallback[pid]
            stream["language_source"] = "clpi"
        elif has_language:
            stream.setdefault("language_source", "ffprobe")
        stream[_VERSION_KEY] = CLPI_LANGUAGE_VERSION
        return stream

    return replace(
        spec,
        audio_streams=[enrich(stream, languages.audio) for stream in spec.audio_streams],
        subtitle_streams=[enrich(stream, languages.subtitles) for stream in spec.subtitle_streams],
    )


def streams_have_clpi_metadata(audio_streams: list | None, subtitle_streams: list | None) -> bool:
    """这组持久化流是否已由当前版本检查过 CLPI。"""
    if audio_streams is None or subtitle_streams is None:
        return False
    streams = [*audio_streams, *subtitle_streams]
    return all(stream.get(_VERSION_KEY) == CLPI_LANGUAGE_VERSION for stream in streams)


def _require(
    pos: int, size: int, end: int, field: str, *, error: type[ValueError] = ClpiParseError
) -> None:
    if size < 0 or pos < 0 or pos + size > end:
        raise error(f"{field} 数据越界")


def _language_at(data: bytes, pos: int, end: int) -> str | None:
    if pos + 3 > end:
        raise ClpiParseError("语言码数据越界")
    raw = data[pos : pos + 3]
    if not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in raw):
        return None
    language = raw.decode("ascii").lower()
    return None if language == "und" else language


def _record_language(target: dict[int, str], conflicts: set[int], pid: int, language: str) -> None:
    if pid in conflicts:
        return
    current = target.get(pid)
    if current is None:
        target[pid] = language
    elif current != language:
        target.pop(pid, None)
        conflicts.add(pid)
