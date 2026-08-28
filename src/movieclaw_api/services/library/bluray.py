"""蓝光原盘 MPLS 主播放列表、CLPI 语言元数据解析与 ffprobe 结果合并。

BDMV 的 m2ts 是 MPEG-TS 流文件，ffprobe 能稳定拿到编码、声道与流 PID，
但不少原盘没有把语言描述符写进 TS；真实语言位于同编号的
``BDMV/CLIPINF/<clip_id>.clpi``。本模块只读 CLPI 的 ProgramInfo 小节，并用
PID 关联两边的轨道，绝不按数组下标猜测——播放列表裁剪、交互图形轨或工具
识别差异都可能让两边数量不同，错一位就会把整组语言写错。

解析失败属于可选元数据缺失：调用方保留原 ffprobe 结果，扫描照常完成。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from movieclaw_api.services.media_probe import MediaSpec

logger = logging.getLogger("movieclaw_api.bluray")

_MPLS_CLOCK_HZ = 45_000


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
    """可用于选主片的 MPLS 播放列表。"""

    path: Path
    items: tuple[MplsPlayItem, ...]

    @property
    def duration_seconds(self) -> int:
        return round(sum(item.duration_seconds for item in self.items))

    @property
    def clip_ids(self) -> tuple[str, ...]:
        return tuple(item.clip_id for item in self.items)


def parse_mpls_playlist(data: bytes, *, path: Path = Path("unknown.mpls")) -> MplsPlaylist:
    """解析 MPLS 的 PlayList 小节。

    这里只读取选择主片所需的 clip id 与 IN/OUT 时间；STN、角度和 SubPath
    都保留在完整原盘目录中，不需要为了入库而解释或改写。
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
    _require(pos, 6, section_end, "PlayList header")
    pos += 2  # reserved
    play_item_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 4  # number_of_play_items + number_of_sub_paths
    items: list[MplsPlayItem] = []
    for _ in range(play_item_count):
        _require(pos, 2, section_end, "PlayItem length")
        item_length = int.from_bytes(data[pos : pos + 2], "big")
        item_start = pos + 2
        item_end = item_start + item_length
        _require(item_start, item_length, section_end, "PlayItem")
        # clip id(5) + codec id(4) + flags(2) + stc id(1) + in/out(8)
        _require(item_start, 20, item_end, "PlayItem core")
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
    return MplsPlaylist(path=path, items=tuple(items))


def read_main_playlist(disc_dir: Path) -> MplsPlaylist | None:
    """从完整 BDMV 中选择主播放列表；损坏/伪列表只降级，不阻断扫描。

    候选必须引用实际存在的 STREAM 文件。按有效播放时长优先，同一剪辑序列
    只保留一个，避免不同语言/菜单入口的等价 MPLS 放大候选数量。
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


def _require(pos: int, size: int, end: int, field: str) -> None:
    if size < 0 or pos < 0 or pos + size > end:
        raise ClpiParseError(f"{field} 数据越界")


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
