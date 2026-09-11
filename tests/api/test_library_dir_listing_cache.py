"""目录列举缓存（services/library/artwork.dir_listing）的正确性护栏。

这层缓存是为家用 NAS 省随机小 IO 加的：电视端滚一屏海报，原本每张图都要把
影片目录重列一遍。它**不按时间过期**，靠目录自己的 (mtime, ctime, size) 指纹
判断有没有变——所以最该守住的是「用户刚往目录里放/删/改名一个文件，下一次
请求必须看到」。这些用例就钉这一条。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from movieclaw_api.services.library.artwork import (
    _DIR_QUIET_SECONDS,
    dir_listing,
    find_artwork,
    forget_dir_listings,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    forget_dir_listings()
    yield
    forget_dir_listings()


def _settled(directory: Path) -> None:
    """把目录的 mtime 往回拨，越过「刚改过就不缓存」的静默窗口。

    直接 sleep 也行，但那会让每个用例白等两秒。
    """
    old = time.time() - _DIR_QUIET_SECONDS * 2
    os.utime(directory, (old, old))


def test_listing_is_cached_while_directory_unchanged(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    _settled(tmp_path)
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg"}

    calls: list[Path] = []
    real_scandir = os.scandir

    def counting_scandir(path):  # noqa: ANN001
        calls.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    # 目录没变：后续调用只 stat 一次目录，不再真去列它
    for _ in range(5):
        assert set(dir_listing(tmp_path) or {}) == {"a.jpg"}
    assert calls == []


def test_new_file_is_visible_immediately(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    _settled(tmp_path)
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg"}
    # 用户往条目目录里拷了一张海报：下一次请求必须看得到
    (tmp_path / "poster.jpg").write_bytes(b"x")
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg", "poster.jpg"}


def test_deleted_file_disappears_immediately(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "poster.jpg").write_bytes(b"x")
    _settled(tmp_path)
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg", "poster.jpg"}
    (tmp_path / "poster.jpg").unlink()
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg"}


def test_renamed_file_is_visible_immediately(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    _settled(tmp_path)
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg"}
    (tmp_path / "a.jpg").rename(tmp_path / "b.jpg")
    assert set(dir_listing(tmp_path) or {}) == {"b.jpg"}


def test_freshly_touched_directory_is_not_cached(tmp_path: Path) -> None:
    """mtime 只有秒级精度的文件系统上，「刚改过」的目录不可信，不入缓存。"""
    (tmp_path / "a.jpg").write_bytes(b"x")
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg"}
    # 目录刚被改过（没越过静默窗口）→ 上一行不该留下缓存，本行必须重新列
    (tmp_path / "b.jpg").write_bytes(b"x")
    assert set(dir_listing(tmp_path) or {}) == {"a.jpg", "b.jpg"}


def test_missing_directory_reads_as_none(tmp_path: Path) -> None:
    absent = tmp_path / "gone"
    assert dir_listing(absent) is None
    absent.mkdir()
    (absent / "a.jpg").write_bytes(b"x")
    assert set(dir_listing(absent) or {}) == {"a.jpg"}


def test_replaced_poster_is_picked_up_through_find_artwork(tmp_path: Path) -> None:
    """端到端口径：换掉条目目录里的海报，下一次定位必须给出新文件。"""
    video = tmp_path / "A.mp4"
    video.write_bytes(b"x")
    (tmp_path / "poster.jpg").write_bytes(b"x")
    _settled(tmp_path)
    assert find_artwork(tmp_path, "poster", [video]) == tmp_path / "poster.jpg"
    # 用户把 poster.jpg 换成了精确同名的 A.jpg（优先级更高的那一档）
    (tmp_path / "A.jpg").write_bytes(b"x")
    assert find_artwork(tmp_path, "poster", [video]) == tmp_path / "A.jpg"
