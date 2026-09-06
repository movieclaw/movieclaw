"""本地美术图定位的**唯一规则**（Kodi / Jellyfin 命名规范）。

Web 的 artwork 接口、Jellyfin 图片接口、本地条目的资产生成（thumbs.py）都从
这里找图——三处只维护一份规则，选出来的一定是同一张，DTO 报的长宽比才和
播放器实际取到的图一致。

规则照抄 Jellyfin ``LocalImageProvider``（docs/design/library-other-kind.md 1.5.3）：

1. **文件自己的图**：``<主干>-poster.jpg`` 这类带视频文件名主干前缀的 sidecar，
   精确匹配，永远优先——它明确写着归谁；
2. **目录级的图**：不带前缀的 ``poster.jpg`` / ``fanart.jpg``，只在目录**归这个
   条目**时才认——目录里的视频全是它的（单片一目录、多版本同目录、剧集的
   季目录结构都算）。混放目录里的 ``poster.jpg`` 是目录自己的图，谁都不该拿，
   否则一个演员文件夹下散放的几部片会共用排序第一那部的海报（串图）。

三种图各自的候选名（按优先级）：

- poster（Primary）：``<主干>.jpg`` 精确同名 → ``-poster`` / ``-folder`` /
  ``-cover`` / ``-default`` / ``-movie`` → 目录级 ``poster`` / ``folder`` /
  ``cover`` / ``default`` / ``movie``；
- fanart（Backdrop）：``-fanart`` / ``-backdrop`` → 目录级 ``fanart`` /
  ``backdrop`` / ``background``；
- thumb（Thumb，横版）：``-landscape`` / ``-thumb`` → 目录级 ``landscape`` /
  ``thumb``。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from movieclaw_api.services.library.layout import SCAN_VIDEO_EXTS

ART_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# kind → (文件自己的 sidecar 后缀, 目录级文件名)；"" = 与视频精确同名
_KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "poster": (
        ("", "-poster", "-folder", "-cover", "-default", "-movie"),
        ("poster", "folder", "cover", "default", "movie"),
    ),
    "fanart": (("-fanart", "-backdrop"), ("fanart", "backdrop", "background")),
    "thumb": (("-landscape", "-thumb"), ("landscape", "thumb")),
}


def _listing(directory: Path, cache: dict[Path, dict[str, Path] | None]) -> dict[str, Path] | None:
    """目录里的文件：小写文件名 → 真实路径。列不出来（挂载断了）为 None。

    一次 listdir 代替几十次 stat：图片接口每张图都要走这里，网络挂载上
    stat 一次就是一次往返。文件名按小写匹配（与 Jellyfin 一致，海报叫
    ``Poster.JPG`` 也认）。
    """
    if directory in cache:
        return cache[directory]
    try:
        names = {entry.name.lower(): entry for entry in directory.iterdir() if entry.is_file()}
    except OSError:
        names = None
    cache[directory] = names
    return names


def dir_art_owned(entry_dir: Path, own_files: Iterable[Path]) -> bool:
    """目录级美术图是否归这个条目：目录里直接躺着的视频文件全是它的。

    目录列不出来（挂载断了）按「不归」处理，宁可少一张图也不串图。
    """
    return _owned(entry_dir, list(own_files), {})


def _owned(entry_dir: Path, own_files: list[Path], cache: dict) -> bool:
    listing = _listing(entry_dir, cache)
    if listing is None:
        return False
    own = {p.name.lower() for p in own_files if p.parent == entry_dir}
    return all(name in own or Path(name).suffix not in SCAN_VIDEO_EXTS for name in listing)


def find_artwork(entry_dir: Path, kind: str, own_files: Iterable[Path]) -> Path | None:
    """按规则找一张 ``kind`` 图（poster / fanart / thumb）；没有返回 None。

    ``own_files`` 是条目自己的视频文件（可在 ``entry_dir`` 的子目录里，如剧集
    的季目录）；sidecar 按各文件主干匹配，目录级图按归属判定。同步磁盘 IO
    （每个涉及的目录列一次），调用方自行决定是否进线程池。
    """
    suffixes, dir_names = _KINDS[kind]
    own_files = list(own_files)
    cache: dict[Path, dict[str, Path] | None] = {}
    for video in own_files:
        listing = _listing(video.parent, cache)
        if not listing:
            continue
        stem = video.stem.lower()
        for suffix in suffixes:
            for ext in ART_EXTS:
                found = listing.get(f"{stem}{suffix}{ext}")
                if found is not None:
                    return found
    listing = _listing(entry_dir, cache)
    if not listing or not _owned(entry_dir, own_files, cache):
        return None
    for name in dir_names:
        for ext in ART_EXTS:
            found = listing.get(f"{name}{ext}")
            if found is not None:
                return found
    return None
