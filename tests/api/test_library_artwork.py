"""本地美术图定位规则（services/library/artwork.py）：

- 文件自己的 ``<主干>-poster`` 精确匹配优先，永远归它；
- 目录级 ``poster.jpg`` 只在目录归这个条目（目录里的视频全是它的）时才认；
  混放目录里不串图；
- 剧集：文件在季目录里、海报在剧目录下，目录级图归它；
- 文件直接躺在库根下时也能拿到自己的 sidecar。
"""

from __future__ import annotations

from pathlib import Path

from movieclaw_api.services.library.artwork import dir_art_owned, find_artwork
from movieclaw_api.services.library.items import local_item_artwork
from movieclaw_db.models import FileSource, LibraryFile


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _row(path: Path) -> LibraryFile:
    return LibraryFile(
        library_id=1, file_path=str(path), size_bytes=1, container="mp4", source=FileSource.SCANNED
    )


def test_sidecar_by_stem_wins_over_dir_level(tmp_path: Path) -> None:
    video = _touch(tmp_path / "A" / "A.mp4")
    _touch(tmp_path / "A" / "poster.jpg")
    own = _touch(tmp_path / "A" / "A-poster.jpg")
    assert find_artwork(video.parent, "poster", [video]) == own
    # 精确同名 <主干>.jpg 是 Jellyfin 语义里的 Primary，排最前
    same = _touch(tmp_path / "A" / "A.jpg")
    assert find_artwork(video.parent, "poster", [video]) == same
    assert find_artwork(video.parent, "fanart", [video]) is None
    fan = _touch(tmp_path / "A" / "A-fanart.jpg")
    assert find_artwork(video.parent, "fanart", [video]) == fan
    thumb = _touch(tmp_path / "A" / "A-thumb.png")
    assert find_artwork(video.parent, "thumb", [video]) == thumb


def test_dir_level_art_only_when_dir_belongs_to_item(tmp_path: Path) -> None:
    folder = tmp_path / "演员"
    a = _touch(folder / "AAA-001.mp4")
    b = _touch(folder / "BBB-002.mp4")
    a_poster = _touch(folder / "AAA-001-poster.jpg")
    _touch(folder / "poster.jpg")
    # 混放目录：A 拿自己的，B 没有自己的也不能拿目录级/别人的
    assert not dir_art_owned(folder, [b])
    assert find_artwork(folder, "poster", [a]) == a_poster
    assert find_artwork(folder, "poster", [b]) is None
    # 同一条目的多版本同目录：目录归它，目录级图可用
    assert dir_art_owned(folder, [a, b])
    assert find_artwork(folder, "poster", [b, a]) == a_poster  # 先各文件 sidecar
    a_poster.unlink()
    assert find_artwork(folder, "poster", [a, b]) == folder / "poster.jpg"


def test_dir_level_art_for_series_with_season_dirs(tmp_path: Path) -> None:
    show = tmp_path / "剧 (2020)"
    ep = _touch(show / "Season 1" / "S01E01.mkv")
    poster = _touch(show / "poster.jpg")
    # 剧目录下没有直接躺着的视频，目录归它
    assert find_artwork(show, "poster", [ep]) == poster


def test_local_item_artwork_for_bare_file_under_root(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    video = _touch(root / "clip.mp4")
    _touch(root / "other.mp4")
    _touch(root / "poster.jpg")  # 库根下的目录级图不归任何条目
    assert local_item_artwork([root], [_row(video)], "poster") is None
    own = _touch(root / "clip-poster.jpg")
    assert local_item_artwork([root], [_row(video)], "poster") == own


def test_artwork_names_match_case_insensitively(tmp_path: Path) -> None:
    """与 Jellyfin 一致：``Poster.JPG`` 也认（目录只列一次、按小写名匹配）。"""
    video = _touch(tmp_path / "B" / "B.mkv")
    art = _touch(tmp_path / "B" / "B-Poster.JPG")
    assert find_artwork(video.parent, "poster", [video]) == art
