"""HEVC MP4 直出时 hev1 → hvc1 标签修正（issue #430）。

用纯 Python 拼最小 ISO BMFF 结构，不依赖 ffmpeg：只要盒子层级和 stsd /
hvcC 的定长字段对，解析器就该找到那 4 个字节。
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from movieclaw_playback.mp4_sample_entry import (
    _patch_cache,
    apply_byte_patches,
    hev1_to_hvc1_patches,
)


def _box(kind: bytes, payload: bytes, *, large: bool = False) -> bytes:
    if large:
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _hvcc(num_of_arrays: int) -> bytes:
    # 22 字节定长字段 + numOfArrays；数组本体对解析无关，省略
    return _box(b"hvcC", bytes(22) + bytes([num_of_arrays]))


def _sample_entry(kind: bytes, *children: bytes) -> bytes:
    # SampleEntry 8 字节 + VisualSampleEntry 70 字节定长字段
    return _box(kind, bytes(8) + bytes(70) + b"".join(children))


def _stsd(*entries: bytes) -> bytes:
    return _box(b"stsd", struct.pack(">II", 0, len(entries)) + b"".join(entries))


def _trak(stsd: bytes) -> bytes:
    stbl = _box(b"stbl", _box(b"stts", bytes(8)) + stsd + _box(b"stco", bytes(8)))
    return _box(b"trak", _box(b"tkhd", bytes(84)) + _box(b"mdia", _box(b"minf", stbl)))


def _mp4(*traks: bytes, mdat: bytes = b"", moov_first: bool = True) -> bytes:
    ftyp = _box(b"ftyp", b"isom" + bytes(4) + b"isomiso2mp41")
    moov = _box(b"moov", _box(b"mvhd", bytes(100)) + b"".join(traks))
    mdat_box = _box(b"mdat", mdat)
    return ftyp + (moov + mdat_box if moov_first else mdat_box + moov)


def _write(tmp_path: Path, data: bytes, name: str = "a.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _expect_patch_points_at_hev1(path: Path, patches: tuple) -> None:
    assert len(patches) == 1
    offset, data = patches[0]
    assert data == b"hvc1"
    assert path.read_bytes()[offset : offset + 4] == b"hev1"


def test_finds_hev1_entry_with_parameter_sets(tmp_path: Path) -> None:
    path = _write(tmp_path, _mp4(_trak(_stsd(_sample_entry(b"hev1", _hvcc(3))))))
    _expect_patch_points_at_hev1(path, hev1_to_hvc1_patches(path))


def test_moov_at_end_is_reached_by_skipping_mdat(tmp_path: Path) -> None:
    data = _mp4(
        _trak(_stsd(_sample_entry(b"hev1", _hvcc(1)))),
        mdat=b"\xff" * 65536,
        moov_first=False,
    )
    path = _write(tmp_path, data)
    _expect_patch_points_at_hev1(path, hev1_to_hvc1_patches(path))


def test_hvc1_file_needs_no_patch(tmp_path: Path) -> None:
    path = _write(tmp_path, _mp4(_trak(_stsd(_sample_entry(b"hvc1", _hvcc(1))))))
    assert hev1_to_hvc1_patches(path) == ()


def test_in_band_only_hev1_is_left_alone(tmp_path: Path) -> None:
    # numOfArrays == 0：参数集只在带内，打 hvc1 标签反而会让严格解码器找不到 SPS
    path = _write(tmp_path, _mp4(_trak(_stsd(_sample_entry(b"hev1", _hvcc(0))))))
    assert hev1_to_hvc1_patches(path) == ()


def test_audio_track_and_second_video_entry(tmp_path: Path) -> None:
    audio = _trak(_stsd(_box(b"mp4a", bytes(28))))
    video = _trak(_stsd(_sample_entry(b"hev1", _hvcc(1)), _sample_entry(b"hev1", _hvcc(1))))
    path = _write(tmp_path, _mp4(audio, video))
    patches = hev1_to_hvc1_patches(path)
    assert len(patches) == 2
    raw = path.read_bytes()
    assert all(raw[at : at + 4] == b"hev1" for at, _ in patches)


def test_largesize_box_header(tmp_path: Path) -> None:
    ftyp = _box(b"ftyp", b"isom" + bytes(4) + b"isom")
    moov = _box(b"moov", _trak(_stsd(_sample_entry(b"hev1", _hvcc(1)))), large=True)
    path = _write(tmp_path, ftyp + moov)
    _expect_patch_points_at_hev1(path, hev1_to_hvc1_patches(path))


def test_non_mp4_and_truncated_files_yield_nothing(tmp_path: Path) -> None:
    assert hev1_to_hvc1_patches(_write(tmp_path, b"\x1aE\xdf\xa3" + bytes(64), "a.mkv")) == ()
    broken = _mp4(_trak(_stsd(_sample_entry(b"hev1", _hvcc(1)))))[:-40]
    assert hev1_to_hvc1_patches(_write(tmp_path, broken, "b.mp4")) == ()
    assert hev1_to_hvc1_patches(tmp_path / "missing.mp4") == ()


def test_result_is_cached_until_file_changes(tmp_path: Path) -> None:
    _patch_cache.clear()
    path = _write(tmp_path, _mp4(_trak(_stsd(_sample_entry(b"hev1", _hvcc(1))))))
    first = hev1_to_hvc1_patches(path)
    assert first and len(_patch_cache) == 1
    # 同一文件再问命中缓存；文件换成 hvc1（大小相同）后缓存键变化，重新解析。
    # 内核文件时间戳是粗粒度时钟，连续两次写可能落在同一 tick，显式推一下 mtime
    assert hev1_to_hvc1_patches(path) is first
    path.write_bytes(path.read_bytes().replace(b"hev1", b"hvc1"))
    os.utime(path, ns=(0, path.stat().st_mtime_ns + 1_000_000))
    assert hev1_to_hvc1_patches(path) == ()


def test_apply_byte_patches_across_chunk_boundary() -> None:
    patches = ((6, b"hvc1"),)
    # 补丁横跨 [0,8) 与 [8,16) 两块：每块只改落在自己范围内的那部分
    first = apply_byte_patches(b"\x00" * 8, 0, patches)
    second = apply_byte_patches(b"\x00" * 8, 8, patches)
    assert first == b"\x00" * 6 + b"hv"
    assert second == b"c1" + b"\x00" * 6
    untouched = b"\x00" * 8
    assert apply_byte_patches(untouched, 16, patches) is untouched
