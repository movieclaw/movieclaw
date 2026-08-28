"""BDMV CLPI 语言解析与 PID 回填回归。"""

from __future__ import annotations

import pytest

from movieclaw_api.services.library.bluray import (
    CLPI_LANGUAGE_VERSION,
    ClpiLanguages,
    ClpiParseError,
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
