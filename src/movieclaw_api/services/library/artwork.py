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

import os
import time
from collections import OrderedDict
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


#: 目录列举的复用缓存：``{目录: {小写文件名: 路径}}``，``None`` 表示列不出来。
#: 一次调用内共享（见 ``find_artwork`` 的 ``cache`` 参数）。
DirListing = dict[Path, "dict[str, Path] | None"]

#: 进程级目录列举缓存：目录 -> (目录 inode 指纹, 列举结果)。见 ``dir_listing``。
#: 用 OrderedDict 当 LRU：满了**淘汰最久没用的那条**，而不是整体清空。
#: 库一大（几万个条目目录）时两者差别很大——整体清空意味着每攒满一次上限，
#: 连正在被反复浏览的那几十个热目录也一起丢掉，下一屏海报又要全列一遍。
_DIR_CACHE: OrderedDict[Path, tuple[tuple[int, int, int], dict[str, Path]]] = OrderedDict()
#: 缓存目录数上限。一条约等于「一个目录的文件名表」，几十字节到几 KB 不等。
_DIR_CACHE_MAX = 4096
#: 目录刚被改过的这段时间内不信任缓存：部分文件系统的 mtime 只有秒级精度，
#: "同一秒内先读后改"会让指纹看起来没变。留出这个窗口，代价只是刚落盘的
#: 目录多列几次。
_DIR_QUIET_SECONDS = 2.0


def _dir_fingerprint(stat: os.stat_result) -> tuple[int, int, int]:
    """目录的「内容有没有变过」指纹。

    目录的 mtime/ctime 在其中新增、删除、改名任一条目时都会变；本函数的
    使用者只关心目录里**有哪些名字**，因此这三项一致即可复用上次的列举。
    单个文件内容被改写不动目录 mtime——也确实与列举结果无关。
    """
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


def dir_listing(directory: Path, cache: DirListing | None = None) -> dict[str, Path] | None:
    """目录里的文件：小写文件名 → 真实路径。列不出来（挂载断了）为 None。

    一次 listdir 代替几十次 stat：图片接口每张图都要走这里，网络挂载上
    stat 一次就是一次往返。文件名按小写匹配（与 Jellyfin 一致，海报叫
    ``Poster.JPG`` 也认）。

    两层缓存，都只为省磁盘 IO，都不会让用户看到过期的目录内容：

    1. ``cache``：调用方传进来的一次性字典，同一次请求内共享；
    2. 进程级缓存：先 stat 一次目录，指纹（mtime/ctime/size）与上次一致就
       直接复用上次的列举结果。**这不是按时间过期的缓存**——目录里增删改名
       任何一个文件都会改掉目录自己的 mtime，指纹随之失效，所以用户刚拷进去
       的海报下一次请求就能看到。省下的是「一次 getdents + 逐条目判类型」，
       换成「一次 stat」。家用 NAS 的电视端滚一屏海报，原本每张图都要把
       影片目录重列一遍，现在只剩每张图一次 stat。

    用 ``os.scandir`` 而不是 ``Path.iterdir()`` + ``is_file()``：后者对目录里
    每个条目都要多一次 stat（一个 30 集的季目录 ≈ 90 个文件 = 90 次），而
    scandir 能直接用 ``getdents`` 已经带回来的类型位判断，绝大多数文件系统上
    一次目录读取就够。类型位缺失（部分网络挂载）时它自己回落到 stat，
    因此永远不会比原来更慢。
    """
    if cache is not None and directory in cache:
        return cache[directory]
    names = _listing_uncached(directory)
    if cache is not None:
        cache[directory] = names
    return names


def _listing_uncached(directory: Path) -> dict[str, Path] | None:
    try:
        dir_stat = os.stat(directory)
    except OSError:
        _DIR_CACHE.pop(directory, None)
        return None
    fingerprint = _dir_fingerprint(dir_stat)
    hit = _DIR_CACHE.get(directory)
    if hit is not None and hit[0] == fingerprint:
        _DIR_CACHE.move_to_end(directory)  # 命中即刷新 LRU 位置
        return hit[1]
    try:
        with os.scandir(directory) as entries:
            names = {
                entry.name.lower(): Path(entry.path) for entry in entries if entry.is_file()
            }
    except OSError:
        _DIR_CACHE.pop(directory, None)
        return None
    # 目录刚变过就先不落缓存（见 _DIR_QUIET_SECONDS）
    if time.time() - dir_stat.st_mtime >= _DIR_QUIET_SECONDS:
        _DIR_CACHE[directory] = (fingerprint, names)
        _DIR_CACHE.move_to_end(directory)
        while len(_DIR_CACHE) > _DIR_CACHE_MAX:
            _DIR_CACHE.popitem(last=False)
    return names


def forget_dir_listings() -> None:
    """丢弃进程级目录缓存（测试用；生产靠目录指纹自动失效）。"""
    _DIR_CACHE.clear()


def _listing(directory: Path, cache: DirListing) -> dict[str, Path] | None:
    return dir_listing(directory, cache)


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


def find_artwork(
    entry_dir: Path,
    kind: str,
    own_files: Iterable[Path],
    *,
    cache: DirListing | None = None,
) -> Path | None:
    """按规则找一张 ``kind`` 图（poster / fanart / thumb）；没有返回 None。

    ``own_files`` 是条目自己的视频文件（可在 ``entry_dir`` 的子目录里，如剧集
    的季目录）；sidecar 按各文件主干匹配，目录级图按归属判定。同步磁盘 IO
    （每个涉及的目录列一次），调用方自行决定是否进线程池。

    ``cache``：调用方跨多次调用共享的目录列举缓存。详情页要连着找 poster 与
    fanart，不共享的话同一批目录要列两遍——剧集的季目录一列就是几十个文件。
    """
    suffixes, dir_names = _KINDS[kind]
    own_files = list(own_files)
    if cache is None:
        cache = {}
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
