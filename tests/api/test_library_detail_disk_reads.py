"""详情页的本地读盘口径（services/library/items）。

这批用例守的是「按目录批处理」这次改写：外挂字幕发现原本每个文件列一次
所在目录，一部 30 集的剧就把同一个季目录列 30 遍。改成按目录归组之后，
**结果必须与逐文件时完全一致**，同时目录列举次数只与目录数有关、与文件数无关。
"""

from __future__ import annotations

import os
from pathlib import Path

from movieclaw_api.services.library.artwork import forget_dir_listings
from movieclaw_api.services.library.items import _external_subtitles_many


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_matches_stem_and_language_suffix(tmp_path: Path) -> None:
    forget_dir_listings()
    video = _touch(tmp_path / "Movie.mkv")
    _touch(tmp_path / "Movie.srt")
    _touch(tmp_path / "Movie.chs.srt")
    _touch(tmp_path / "Movie.eng.ass")
    # 不该算进来的：别的片子的字幕、同名但不是字幕的伴生文件
    _touch(tmp_path / "Other.srt")
    _touch(tmp_path / "Movie.nfo")
    _touch(tmp_path / "MovieExtra.srt")

    found = _external_subtitles_many([video])
    assert found[video] == ["Movie.chs.srt", "Movie.eng.ass", "Movie.srt"]


def test_each_video_only_gets_its_own(tmp_path: Path) -> None:
    forget_dir_listings()
    a = _touch(tmp_path / "A.mkv")
    b = _touch(tmp_path / "B.mkv")
    _touch(tmp_path / "A.chs.srt")
    _touch(tmp_path / "B.srt")

    found = _external_subtitles_many([a, b])
    assert found[a] == ["A.chs.srt"]
    assert found[b] == ["B.srt"]


def test_one_listing_per_directory_regardless_of_file_count(tmp_path: Path, monkeypatch) -> None:
    forget_dir_listings()
    season = tmp_path / "Show" / "Season 01"
    videos = []
    for ep in range(1, 11):
        videos.append(_touch(season / f"Show - S01E{ep:02d}.mkv"))
        _touch(season / f"Show - S01E{ep:02d}.chs.srt")
    # 缓存不该把这次列举吞掉：目录刚建好，本来就不满足入缓存的条件
    calls: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path):  # noqa: ANN001
        calls.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    found = _external_subtitles_many(videos)

    assert len(calls) == 1, "同一个季目录只该列一次"
    for ep, video in enumerate(videos, start=1):
        assert found[video] == [f"Show - S01E{ep:02d}.chs.srt"]


def test_unreadable_directory_reads_as_no_subtitles(tmp_path: Path) -> None:
    forget_dir_listings()
    missing = tmp_path / "gone" / "A.mkv"
    assert _external_subtitles_many([missing]) == {missing: []}
