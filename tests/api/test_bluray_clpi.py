"""BDMV CLPI 语言解析与 PID 回填回归。"""

from __future__ import annotations

import pytest

from movieclaw_api.services.library.bluray import (
    CLPI_LANGUAGE_VERSION,
    DISC_PLAYLIST_VERSION,
    ClpiLanguages,
    ClpiParseError,
    MplsParseError,
    disc_playlist_record,
    disc_playlist_stale,
    enrich_spec_with_clpi,
    parse_clpi_languages,
    parse_mpls_playlist,
    read_clpi_languages,
    read_main_playlist,
    streams_have_clpi_metadata,
)
from movieclaw_api.services.library.scan import disc_main_stream
from movieclaw_api.services.media_probe import MediaSpec, _parse_probe
from movieclaw_api.services.subtitle_gen.auto import _has_target_subtitle
from movieclaw_db.models import LibraryFile


def _mpls(*items: tuple[str, int, int]) -> bytes:
    """构造只含主链 PlayItem 的最小合法 MPLS。时间单位为 45 kHz。"""
    body = bytearray(b"\0\0" + len(items).to_bytes(2, "big") + b"\0\0")
    for clip_id, in_time, out_time in items:
        core = (
            clip_id.encode("ascii")
            + b"M2TS"
            + b"\0\0"
            + b"\0"
            + in_time.to_bytes(4, "big")
            + out_time.to_bytes(4, "big")
        )
        body += len(core).to_bytes(2, "big") + core
    header = bytearray(b"MPLS0100" + (20).to_bytes(4, "big") + b"\0" * 8)
    return bytes(header + len(body).to_bytes(4, "big") + body)


def test_mpls_parser_and_main_playlist_selection(tmp_path):
    disc = tmp_path / "Movie"
    playlist_dir = disc / "BDMV" / "PLAYLIST"
    stream_dir = disc / "BDMV" / "STREAM"
    playlist_dir.mkdir(parents=True)
    stream_dir.mkdir()
    (stream_dir / "00001.m2ts").write_bytes(b"a")
    (stream_dir / "00002.m2ts").write_bytes(b"bb")
    (stream_dir / "99999.m2ts").write_bytes(b"menu-or-bonus" * 10)
    short = _mpls(("00001", 0, 45_000 * 60))
    long = _mpls(("00001", 0, 45_000 * 60), ("00002", 0, 45_000 * 120))
    (playlist_dir / "00001.mpls").write_bytes(short)
    (playlist_dir / "00800.mpls").write_bytes(long)
    (playlist_dir / "broken.mpls").write_bytes(b"broken")

    parsed = parse_mpls_playlist(long)
    assert parsed.clip_ids == ("00001", "00002")
    assert parsed.duration_seconds == 180
    selected = read_main_playlist(disc)
    assert selected is not None
    assert selected.path.name == "00800.mpls"
    assert selected.duration_seconds == 180
    assert disc_main_stream(disc) == stream_dir / "00002.m2ts"


def test_main_playlist_accepts_uppercase_disc_extensions(tmp_path):
    """原盘扩展名大小写由制作/解包工具决定，Linux 上也必须正常识别。"""
    disc = tmp_path / "Movie"
    playlist_dir = disc / "BDMV" / "PLAYLIST"
    stream_dir = disc / "BDMV" / "STREAM"
    playlist_dir.mkdir(parents=True)
    stream_dir.mkdir()
    playlist = _mpls(("00001", 0, 45_000 * 60))
    (playlist_dir / "00001.MPLS").write_bytes(playlist)
    stream = stream_dir / "00001.M2TS"
    stream.write_bytes(b"main")

    selected = read_main_playlist(disc)
    assert selected is not None and selected.path.suffix == ".MPLS"
    assert disc_main_stream(disc) == stream


def _coding_info(coding_type: int, language: str) -> bytes:
    lang = language.encode("ascii")
    if coding_type in {0x03, 0x04, *range(0x80, 0x87), 0xA1, 0xA2}:
        return bytes([coding_type, 0]) + lang  # coding type + format/rate + ISO 639-2
    if coding_type == 0x90:
        return bytes([coding_type]) + lang + b"\0"
    if coding_type == 0x92:
        return bytes([coding_type, 1]) + lang  # coding type + char code + ISO 639-2
    return bytes([coding_type])


def clpi_bytes(streams: list[tuple[int, int, str]]) -> bytes:
    """构造只含一个 ProgramInfo sequence 的最小测试 CLPI。"""
    entries = bytearray()
    for pid, coding_type, language in streams:
        info = _coding_info(coding_type, language)
        entries.extend(pid.to_bytes(2, "big"))
        entries.append(len(info))
        entries.extend(info)

    body = bytearray(b"\0\1")  # reserved + number_of_program_sequences
    body.extend((0).to_bytes(4, "big"))  # spn_program_sequence_start
    body.extend((0x100).to_bytes(2, "big"))  # program_map_pid
    body.extend(bytes([len(streams), 0]))  # number_of_streams + number_of_groups
    body.extend(entries)

    offset = 32
    header = bytearray(offset)
    header[:8] = b"HDMV0200"
    header[12:16] = offset.to_bytes(4, "big")
    return bytes(header) + len(body).to_bytes(4, "big") + bytes(body)


def test_parse_clpi_reads_audio_and_subtitle_languages_by_pid() -> None:
    parsed = parse_clpi_languages(
        clpi_bytes(
            [
                (0x1100, 0x86, "eng"),
                (0x1101, 0xA1, "zho"),
                (0x1200, 0x90, "zho"),
                (0x1201, 0x92, "fra"),
                (0x1400, 0x91, "jpn"),  # 菜单交互图形，不是影片字幕
            ]
        )
    )

    assert parsed.audio == {0x1100: "eng", 0x1101: "zho"}
    assert parsed.subtitles == {0x1200: "zho", 0x1201: "fra"}


def test_parse_clpi_rejects_truncated_program_info() -> None:
    payload = clpi_bytes([(0x1200, 0x90, "zho")])
    with pytest.raises(ClpiParseError, match="长度越界"):
        parse_clpi_languages(payload[:-1])


def test_parse_clpi_drops_conflicting_language_for_same_pid() -> None:
    parsed = parse_clpi_languages(clpi_bytes([(0x1200, 0x90, "eng"), (0x1200, 0x90, "zho")]))
    assert parsed.subtitles == {}


def test_read_clpi_falls_back_to_backup_copy(tmp_path) -> None:
    """主 CLPI 损坏时仍使用蓝光 BACKUP 副本，不把语言误降级为未知。"""
    stream = tmp_path / "BDMV" / "STREAM" / "00001.m2ts"
    primary = tmp_path / "BDMV" / "CLIPINF" / "00001.clpi"
    backup = tmp_path / "BDMV" / "BACKUP" / "CLIPINF" / "00001.clpi"
    stream.parent.mkdir(parents=True)
    primary.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    stream.write_bytes(b"movie")
    primary.write_bytes(b"broken")
    backup.write_bytes(clpi_bytes([(0x1200, 0x90, "zho")]))

    assert read_clpi_languages(stream) == ClpiLanguages(audio={}, subtitles={0x1200: "zho"})


def test_probe_only_keeps_stream_pid_when_parsing_mpegts() -> None:
    payload = {
        "format": {},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "dts",
                "id": "0x1100",
                "tags": {},
            },
            {
                "codec_type": "subtitle",
                "codec_name": "hdmv_pgs_subtitle",
                "id": 0x1200,
                "tags": {},
            },
        ],
    }
    regular_spec = _parse_probe(payload)
    assert "pid" not in regular_spec.audio_streams[0]
    assert "pid" not in regular_spec.subtitle_streams[0]

    spec = _parse_probe(payload, include_mpegts_pids=True)
    assert spec.audio_streams[0]["pid"] == 0x1100
    assert spec.subtitle_streams[0]["pid"] == 0x1200


def test_enrich_matches_pid_preserves_order_and_existing_language() -> None:
    spec = MediaSpec(
        resolution="2160p",
        video_codec="hevc",
        hdr="HDR10",
        bit_depth=10,
        duration_seconds=7200,
        bit_rate=80_000_000,
        audio_streams=[
            {"codec": "dts", "pid": 0x1101, "language": "und"},
            {"codec": "truehd", "pid": 0x1100, "language": "jpn"},
        ],
        # CLPI 顺序与 ffprobe 相反，且多一条 PID；不得靠 enumerate 错配。
        subtitle_streams=[
            {"codec": "hdmv_pgs_subtitle", "pid": 0x1201, "language": None},
            {"codec": "hdmv_pgs_subtitle", "pid": 0x1200, "language": None},
            {"codec": "hdmv_pgs_subtitle", "pid": 0x12FF, "language": None},
        ],
    )
    languages = ClpiLanguages(
        audio={0x1100: "eng", 0x1101: "zho"},
        subtitles={0x1200: "zho", 0x1201: "eng"},
    )

    enriched = enrich_spec_with_clpi(spec, languages)

    assert [stream["language"] for stream in enriched.audio_streams] == ["zho", "jpn"]
    assert [stream["language"] for stream in enriched.subtitle_streams] == [
        "eng",
        "zho",
        None,
    ]
    assert enriched.audio_streams[0]["language_source"] == "clpi"
    assert enriched.audio_streams[1]["language_source"] == "ffprobe"
    assert all(
        stream["clpi_language_version"] == CLPI_LANGUAGE_VERSION
        for stream in [*enriched.audio_streams, *enriched.subtitle_streams]
    )
    assert streams_have_clpi_metadata(enriched.audio_streams, enriched.subtitle_streams)


def test_clpi_chinese_language_prevents_automatic_duplicate_generation() -> None:
    row = LibraryFile(
        id=1,
        library_id=1,
        media_item_id=1,
        file_path="/movies/1917/BDMV",
        source="scanned",
        subtitle_streams=[{"codec": "hdmv_pgs_subtitle", "language": "zho"}],
    )
    assert _has_target_subtitle(row, "chi")


# ---------------------------------------------------------------------------
# 原盘播放（docs/design/disc-playback.md）：循环诱饵、章节标记、EP_map 关键帧
# ---------------------------------------------------------------------------


def _mpls_with_marks(items: list[tuple[str, int, int]], marks: list[tuple[int, int, int]]) -> bytes:
    """带 PlayListMark 小节的 MPLS。marks 元素为 (mark_type, play_item_id, timestamp)。"""
    body = bytearray(b"\0\0" + len(items).to_bytes(2, "big") + b"\0\0")
    for clip_id, in_time, out_time in items:
        core = (
            clip_id.encode("ascii")
            + b"M2TS"
            + b"\0\0"
            + b"\0"
            + in_time.to_bytes(4, "big")
            + out_time.to_bytes(4, "big")
        )
        body += len(core).to_bytes(2, "big") + core
    playlist_section = len(body).to_bytes(4, "big") + body
    mark_body = bytearray(len(marks).to_bytes(2, "big"))
    for mark_type, item_id, timestamp in marks:
        mark_body += (
            b"\0"
            + bytes([mark_type])
            + item_id.to_bytes(2, "big")
            + timestamp.to_bytes(4, "big")
            + b"\0\0"
            + b"\0\0\0\0"
        )
    mark_section = len(mark_body).to_bytes(4, "big") + mark_body
    playlist_offset = 20
    mark_offset = playlist_offset + len(playlist_section)
    header = (
        b"MPLS0100"
        + playlist_offset.to_bytes(4, "big")
        + mark_offset.to_bytes(4, "big")
        + b"\0" * 4
    )
    return bytes(header + playlist_section + mark_section)


def test_looping_decoy_playlist_is_rejected(tmp_path):
    """防拷贝诱饵：同一剪辑循环几百次、时长虚高，必须让位给真正的主片（BDInfo 同口径）。"""
    disc = tmp_path / "Movie"
    playlist_dir = disc / "BDMV" / "PLAYLIST"
    stream_dir = disc / "BDMV" / "STREAM"
    playlist_dir.mkdir(parents=True)
    stream_dir.mkdir()
    (stream_dir / "00001.m2ts").write_bytes(b"main")
    (stream_dir / "00002.m2ts").write_bytes(b"decoy")
    real = _mpls(("00001", 0, 45_000 * 7200))
    decoy = _mpls(*[("00002", 0, 45_000 * 60)] * 300)  # 300 次循环 → 18000 秒
    (playlist_dir / "00800.mpls").write_bytes(real)
    (playlist_dir / "00152.mpls").write_bytes(decoy)

    assert parse_mpls_playlist(decoy).has_loops
    assert not parse_mpls_playlist(real).has_loops
    selected = read_main_playlist(disc)
    assert selected is not None
    assert selected.path.name == "00800.mpls"
    assert selected.duration_seconds == 7200


def test_same_clip_with_different_in_time_is_not_a_loop():
    """同一剪辑不同入点（无缝分支盘常见）不是循环。"""
    playlist = parse_mpls_playlist(
        _mpls(("00001", 0, 45_000 * 60), ("00001", 45_000 * 60, 45_000 * 120))
    )
    assert not playlist.has_loops


def test_playlist_marks_become_chapters_on_playlist_timeline():
    """章节起点 = 前面各段累计时长 + (标记时间 - 所属段 IN_time)；link point 不算章节。"""
    in1, out1 = 45_000 * 10, 45_000 * 70  # 60 秒
    in2, out2 = 45_000 * 5, 45_000 * 125  # 120 秒
    data = _mpls_with_marks(
        [("00001", in1, out1), ("00002", in2, out2)],
        [
            (1, 0, in1),  # 0 ms
            (1, 0, in1 + 45_000 * 30),  # 30 s
            (2, 0, in1 + 45_000 * 40),  # link point，忽略
            (1, 1, in2 + 45_000 * 15),  # 60 + 15 = 75 s
            (1, 9, in2),  # 越界 PlayItem，忽略
        ],
    )
    playlist = parse_mpls_playlist(data)
    assert playlist.marks == (0, 30_000, 75_000)
    assert playlist.chapters() == [
        {"start_ms": 0, "end_ms": None, "title": None},
        {"start_ms": 30_000, "end_ms": None, "title": None},
        {"start_ms": 75_000, "end_ms": None, "title": None},
    ]
    record = disc_playlist_record(playlist)
    assert record["version"] == DISC_PLAYLIST_VERSION
    assert record["clips"] == [
        {"id": "00001", "in": in1, "out": out1},
        {"id": "00002", "in": in2, "out": out2},
    ]
    assert not disc_playlist_stale(record)
    assert disc_playlist_stale(None)
    assert disc_playlist_stale({"version": 0, "clips": []})


def test_truncated_mpls_raises_mpls_error_not_clpi_error():
    """截断的 MPLS 必须抛 MplsParseError——read_main_playlist 只兜这一种。"""
    data = _mpls(("00001", 0, 45_000))
    with pytest.raises(MplsParseError):
        parse_mpls_playlist(data[:30])


def _clpi_with_ep_map(streams: dict[int, list[int]]) -> bytes:
    """构造只含 CPI/EP_map 的最小 CLPI；streams 为 PID → 关键帧 PTS（45 kHz）。

    粗表按 PTS 的高位分桶（每个粗条目覆盖 2^18 个 45 kHz 单位 ≈ 5.8 秒），
    细表给低位；拼回来的 PTS 与输入相差不超过 255（低 8 位丢失）。
    """
    entries = []
    tables = bytearray()
    entry_area = 2 + 12 * len(streams)  # reserved+count + 每流 12 字节
    for pid, pts_list in streams.items():
        coarse: list[tuple[int, int]] = []
        fine: list[int] = []
        for pts in sorted(pts_list):
            pts_coarse = pts >> 18
            if not coarse or coarse[-1][1] != pts_coarse:
                coarse.append((len(fine), pts_coarse))
            fine.append((pts >> 8) & 0x7FF)
        table = bytearray()
        fine_start = 4 + 8 * len(coarse)
        table += fine_start.to_bytes(4, "big")
        for ref, pts_coarse in coarse:
            table += ((ref << 46) | (pts_coarse << 32) | 0).to_bytes(8, "big")
        for pts_fine in fine:
            table += ((pts_fine << 17) | 0).to_bytes(4, "big")
        start_addr = entry_area + len(tables)
        raw = (pid << 80) | (1 << 66) | (len(coarse) << 50) | (len(fine) << 32) | start_addr
        entries.append(raw.to_bytes(12, "big"))
        tables += table
    ep_map = b"\0" + bytes([len(streams)]) + b"".join(entries) + tables
    cpi_body = b"\0\x01" + ep_map  # 12 位保留 + cpi_type=1
    cpi = len(cpi_body).to_bytes(4, "big") + cpi_body
    cpi_offset = 28
    header = (
        b"HDMV0200"
        + (0).to_bytes(4, "big")  # sequence_info
        + (0).to_bytes(4, "big")  # program_info
        + cpi_offset.to_bytes(4, "big")
        + (0).to_bytes(4, "big")  # clip_mark
        + (0).to_bytes(4, "big")  # ext_data
    )
    return header + cpi


def test_clpi_ep_map_keyframes_prefers_primary_video_pid():
    from movieclaw_api.services.library.bluray import parse_clpi_keyframes

    primary = [45_000 * 11 + 256 * k for k in range(0, 2000, 37)]  # 跨多个粗表桶
    enhancement = [45_000 * 11 + 1_000_000]
    data = _clpi_with_ep_map({0x1015: enhancement, 0x1011: primary})
    parsed = parse_clpi_keyframes(data)
    assert len(parsed) == len(primary)
    # 低 8 位丢失：每个值与输入相差不超过 255
    assert all(0 <= want - got <= 255 for want, got in zip(primary, parsed, strict=True))
    assert enhancement[0] not in parsed


def test_clpi_without_cpi_yields_no_keyframes():
    from movieclaw_api.services.library.bluray import parse_clpi_keyframes

    header = b"HDMV0200" + b"\0" * 20
    assert parse_clpi_keyframes(header) == []
    with pytest.raises(ClpiParseError):
        parse_clpi_keyframes(
            b"HDMV0200" + b"\0" * 8 + (28).to_bytes(4, "big") + b"\0" * 8 + b"\0\0\0\x10"
        )


# ---------------------------------------------------------------------------
# 播放源解析器（services/playback/disc_source.py）
# ---------------------------------------------------------------------------


def _make_disc(tmp_path, name: str, clips: list[tuple[str, int, int]], *, keyframes=None):
    """落一张最小原盘：STREAM 里的 m2ts 是占位字节，CLIPINF 带合成 EP_map。"""
    disc = tmp_path / name
    (disc / "BDMV" / "PLAYLIST").mkdir(parents=True)
    (disc / "BDMV" / "STREAM").mkdir()
    (disc / "BDMV" / "CLIPINF").mkdir()
    for clip_id, _, _ in clips:
        (disc / "BDMV" / "STREAM" / f"{clip_id}.m2ts").write_bytes(b"m2ts")
        if keyframes is not None:
            (disc / "BDMV" / "CLIPINF" / f"{clip_id}.clpi").write_bytes(
                _clpi_with_ep_map({0x1011: keyframes[clip_id]})
            )
    (disc / "BDMV" / "PLAYLIST" / "00001.mpls").write_bytes(_mpls(*clips))
    return disc


def test_disc_source_single_clip_from_ledger_record_without_touching_playlists(tmp_path):
    from movieclaw_api.services.playback.disc_source import disc_source_for_file

    disc = _make_disc(tmp_path, "Single", [("00001", 45_000 * 10, 45_000 * 70)])
    row = LibraryFile(
        library_id=1,
        file_path=str(disc),
        container="bluray",
        disc_playlist=disc_playlist_record(read_main_playlist(disc)),
    )
    # 台账清单齐全时，浏览态（read_disc=False）也能解析，且不读 PLAYLIST 目录
    (disc / "BDMV" / "PLAYLIST" / "00001.mpls").unlink()
    source = disc_source_for_file(row, read_disc=False)
    assert source is not None
    clip = source.single_clip
    assert clip is not None and clip.path == disc / "BDMV" / "STREAM" / "00001.m2ts"
    assert source.duration_s == 60
    assert source.playlist_name == "00001.mpls"


def test_disc_source_falls_back_to_reading_the_disc_only_when_allowed(tmp_path):
    from movieclaw_api.services.playback.disc_source import disc_source_for_file

    disc = _make_disc(tmp_path, "Legacy", [("00001", 0, 45_000 * 30)])
    row = LibraryFile(library_id=1, file_path=str(disc), container="bluray", disc_playlist=None)
    assert disc_source_for_file(row, read_disc=False) is None
    source = disc_source_for_file(row)
    assert source is not None and source.single_clip is not None
    # 非蓝光原盘一律 None
    assert (
        disc_source_for_file(LibraryFile(library_id=1, file_path="/x.mkv", container="mkv")) is None
    )


def test_disc_source_concat_list_and_keyframes_follow_playlist_timeline(tmp_path):
    from movieclaw_api.services.playback.disc_source import disc_source_for_file

    in1, out1 = 45_000 * 10, 45_000 * 70  # 60 秒
    in2, out2 = 45_000 * 5, 45_000 * 65  # 60 秒
    keyframes = {
        # 第一段：IN 前一个（丢弃）、IN 处、IN+20s、OUT 处（丢弃：右开区间）
        "00001": [in1 - 45_000, in1, in1 + 45_000 * 20, out1],
        # 第二段：IN 处、IN+30s
        "00002": [in2, in2 + 45_000 * 30],
    }
    disc = _make_disc(
        tmp_path / "it's",
        "Multi",
        [("00001", in1, out1), ("00002", in2, out2)],
        keyframes=keyframes,
    )
    row = LibraryFile(
        library_id=1,
        file_path=str(disc),
        container="bluray",
        disc_playlist=disc_playlist_record(read_main_playlist(disc)),
    )
    source = disc_source_for_file(row)
    assert source is not None and source.single_clip is None
    assert len(source.clips) == 2

    text = source.concat_list()
    lines = text.splitlines()
    assert lines[0] == "ffconcat version 1.0"
    # 路径里的单引号按 concat 规则转义
    escaped_dir = str(disc).replace("'", "'\\''")
    assert lines[1] == f"file '{escaped_dir}/BDMV/STREAM/00001.m2ts'"
    assert lines[2] == "inpoint 10.000000"
    assert lines[3] == "outpoint 70.000000"
    assert lines[4] == "duration 60.000000"
    assert lines[6] == "inpoint 5.000000"

    index = source.keyframe_index()
    assert index is not None
    # EP_map 低 8 位截断（≤ 255/45000 秒），按 0.01 秒容差比对
    expected = [0.0, 20.0, 60.0, 90.0]
    assert [round(t, 2) for t in index.times_s] == expected
    interval = source.keyframe_interval_s()
    assert interval is not None and abs(interval - 120 / 4) < 0.01


def test_disc_source_keyframes_missing_when_any_clip_lacks_clpi(tmp_path):
    from movieclaw_api.services.playback.disc_source import disc_source_for_file

    disc = _make_disc(tmp_path, "NoClpi", [("00001", 0, 45_000 * 30)])
    row = LibraryFile(
        library_id=1,
        file_path=str(disc),
        container="bluray",
        disc_playlist=disc_playlist_record(read_main_playlist(disc)),
    )
    source = disc_source_for_file(row)
    assert source is not None
    assert source.keyframe_index() is None
    assert source.keyframe_interval_s() is None
