#!/usr/bin/env python3
"""在磁盘上生成一棵**真实形态**的媒体库目录树（压测磁盘 IO 用）。

与 ``seed_library_dataset.py`` 的分工
------------------------------------
那个脚本直接往 SQLite 灌台账行，测的是**读库**的成本；它不落任何文件，因此
测不到媒体库真正贵的那一半——NFO、海报、目录列举这些**落在媒体盘上的随机
小 IO**。家用 NAS 上这一半才是硬盘噪音与卡顿的来源，所以需要一棵真文件树。

生成的形态照搬 TMM / Kodi 整理过的库（NAS 上最常见的形态）::

    <root>/movies/<片名> (年)/<片名> (年).mkv
                              movie.nfo  poster.jpg  fanart.jpg
    <root>/tv/<剧名> (年)/tvshow.nfo  poster.jpg  fanart.jpg
                         Season 01/<剧名> - S01E01.mkv
                                   <剧名> - S01E01.nfo
                                   <剧名> - S01E01-thumb.jpg

视频文件是**稀疏文件**（truncate 出大小、不占实际块）：识别链只看文件名与
NFO，探测链没有 ffprobe 时本就跳过，几百 GB 的真片子对本压测毫无增益。

用法::

    python scripts/perf/seed_media_tree.py /tmp/iolab/media
    LAB_MOVIES=2000 LAB_SHOWS=200 python scripts/perf/seed_media_tree.py /tmp/iolab/media

配套的 ``bench_disk_io.py`` 会拿这棵树建库、扫描、再跑只读场景。
"""

from __future__ import annotations

import os
import random
import struct
import sys
import zlib
from pathlib import Path

#: 规模由环境变量给，默认是「中等家庭库」：400 部电影 + 40 部剧 × 3 季 × 10 集
MOVIE_COUNT = int(os.environ.get("LAB_MOVIES", "400"))
SHOW_COUNT = int(os.environ.get("LAB_SHOWS", "40"))
SEASONS = int(os.environ.get("LAB_SEASONS", "3"))
EPISODES = int(os.environ.get("LAB_EPISODES", "10"))

#: TMDB id 的分配基址：假 TMDB（bench_disk_io）按同一规则反推标题，
#: 两边只靠这两个常数对齐，不需要共享数据文件
MOVIE_TMDB_BASE = 100000
SHOW_TMDB_BASE = 200000

# 标题素材：中文双词拼接 + 序号后缀。序号保证全局唯一（同名条目会被识别链
# 合并成一个，那样就测不出「几百个条目」的真实体量了）
_WORDS_A = ["深海", "北方", "黄昏", "破晓", "长安", "迷雾", "孤星", "银河", "荒原", "蓝色"]
_WORDS_B = ["列车", "旅人", "回声", "边境", "档案", "之城", "往事", "计划", "山脉", "密码"]


def movie_title(index: int) -> str:
    return f"{_WORDS_A[index % 10]}{_WORDS_B[(index // 10) % 10]}{index:03d}"


def show_title(index: int) -> str:
    return f"{_WORDS_B[index % 10]}{_WORDS_A[(index // 7) % 10]}剧{index:02d}"


def movie_year(index: int) -> int:
    return 1990 + (index % 35)


def show_year(index: int) -> int:
    return 2005 + (index % 20)


def png(width: int, height: int, seed: int) -> bytes:
    """最小合法 PNG（纯色），几 KB 量级。

    够 Pillow 解码做派生缩略图即可——压测关心的是「打开几个文件、读几次盘」，
    不是编码器吞吐；真海报几百 KB 反而会让 Pillow 的耗时淹没 IO 信号。
    """
    rnd = random.Random(seed)
    r, g, b = rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)
    raw = b"".join(b"\x00" + bytes([r, g, b]) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def _movie_nfo(index: int) -> bytes:
    title, year = movie_title(index), movie_year(index)
    actors = "".join(
        f"  <actor><name>演员{k}</name><role>角色{k}</role>"
        f"<tmdbid>{9000 + k}</tmdbid></actor>\n"
        for k in range(12)
    )
    return (
        f"<movie>\n  <title>{title}</title>\n"
        f"  <originaltitle>{title} EN</originaltitle>\n"
        f'  <uniqueid type="tmdb">{MOVIE_TMDB_BASE + index}</uniqueid>\n'
        f"  <year>{year}</year>\n  <premiered>{year}-05-01</premiered>\n"
        f"  <runtime>{90 + index % 60}</runtime>\n"
        f"  <plot>这是 {title} 的剧情简介，用于合成媒体库压测。</plot>\n"
        f"  <genre>剧情</genre>\n  <genre>悬疑</genre>\n"
        f"  <director>导演{index % 30}</director>\n{actors}</movie>\n"
    ).encode()


def _tvshow_nfo(index: int) -> bytes:
    title, year = show_title(index), show_year(index)
    return (
        f"<tvshow>\n  <title>{title}</title>\n"
        f'  <uniqueid type="tmdb">{SHOW_TMDB_BASE + index}</uniqueid>\n'
        f"  <year>{year}</year>\n  <premiered>{year}-01-01</premiered>\n"
        f"  <plot>{title} 的剧集简介。</plot>\n  <genre>剧情</genre>\n</tvshow>\n"
    ).encode()


def build(root: Path) -> None:
    """在 ``root`` 下生成 movies/ 与 tv/ 两棵树（幂等：重复跑只是原样覆盖）。"""
    poster = png(300, 450, 1)
    fanart = png(640, 360, 2)
    still = png(300, 170, 3)

    for index in range(MOVIE_COUNT):
        title, year = movie_title(index), movie_year(index)
        entry = root / "movies" / f"{title} ({year})"
        _sparse(entry / f"{title} ({year}).mkv", 1024 * 64)
        _write(entry / "movie.nfo", _movie_nfo(index))
        _write(entry / "poster.jpg", poster)
        _write(entry / "fanart.jpg", fanart)

    for index in range(SHOW_COUNT):
        title, year = show_title(index), show_year(index)
        entry = root / "tv" / f"{title} ({year})"
        _write(entry / "tvshow.nfo", _tvshow_nfo(index))
        _write(entry / "poster.jpg", poster)
        _write(entry / "fanart.jpg", fanart)
        for season in range(1, SEASONS + 1):
            season_dir = entry / f"Season {season:02d}"
            for episode in range(1, EPISODES + 1):
                stem = f"{title} - S{season:02d}E{episode:02d}"
                _sparse(season_dir / f"{stem}.mkv", 1024 * 32)
                _write(
                    season_dir / f"{stem}.nfo",
                    (
                        f"<episodedetails>\n  <title>第{episode}集</title>\n"
                        f"  <season>{season}</season>\n  <episode>{episode}</episode>\n"
                        f"  <aired>{year}-0{season}-{episode:02d}</aired>\n"
                        f"  <plot>{stem} 的分集简介。</plot>\n</episodedetails>\n"
                    ).encode(),
                )
                _write(season_dir / f"{stem}-thumb.jpg", still)


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    target = Path(sys.argv[1])
    build(target)
    files = sum(1 for path in target.rglob("*") if path.is_file())
    print(
        f"已生成：{target}\n"
        f"  电影 {MOVIE_COUNT} 部 / 剧集 {SHOW_COUNT} 部 × {SEASONS} 季 × {EPISODES} 集\n"
        f"  共 {files} 个文件"
    )


if __name__ == "__main__":
    main()
