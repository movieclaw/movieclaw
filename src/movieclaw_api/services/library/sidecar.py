"""附属文件（sidecar）判定：整理 / 转移 / 回收三条链路的唯一口径。

附属文件是"跟着主视频走"的伴生产物——字幕、单文件 NFO、分集剧照、播放器
预览索引。主文件改名或搬走时它们必须一起动，否则会以旧名字留在原地变成
无人认领的垃圾（回收时同理，漏删就是残留）。

本模块把判定收敛到一处。此前 organize / transfer / recycle 各写了一份，
已经出现三处分叉：

- 前缀形态：``-thumb.`` 只在 organize 里补过，transfer / recycle 仍只认
  ``主文件名.``——同一个 ``foo-thumb.jpg``，整理时会跟着走，转移时留在旧
  目录，回收时漏删；
- 扩展名排除集：transfer 写的是硬编码字面量，**漏了 ``.strm``**——`foo.mkv`
  旁边的 `foo.strm` 是独立的网盘占位版本，却会被当成附属一起搬走；
- 新形态要加三遍，漏一处就再分叉一次。

判定规则（三条，全部满足才算附属）：

1. 同目录、是文件、不是主文件本身；
2. 扩展名不在 ``SIDECAR_SKIP_EXTS``——同名不同容器的视频（含 ``.iso`` 原盘
   镜像、``.strm`` 占位）是**独立版本**不是附属，绝不能被连带处理；
3. 文件名匹配下列任一形态：
   - ``主文件名.``  开头 —— 通例（``foo.zh.srt`` / ``foo.nfo``）；
   - ``主文件名-thumb.`` 开头 —— Kodi/Emby 分集剧照约定（``foo-thumb.jpg``）；
   - ``主文件名-<数字>[-<数字>…].bif`` —— Emby/Jellyfin 的 trickplay 预览
     索引（``foo-320-10.bif``，中缀是"宽度-间隔"，随配置变化，写不成固定
     前缀，所以单独用模式匹配）。

返回值统一是"尾巴"（含分隔符本身，如 ``.zh.srt`` / ``-thumb.jpg`` /
``-320-10.bif``），调用方拼到新主文件名后面即得目标名。
"""

from __future__ import annotations

import re
from pathlib import Path

from movieclaw_api.services.library.layout import SCAN_VIDEO_EXTS

# 同名不同容器的视频是独立版本不是附属：.iso 原盘镜像、.strm 网盘占位同理
SIDECAR_SKIP_EXTS = SCAN_VIDEO_EXTS | {".iso"}

# 固定前缀形态：``主文件名`` 之后紧跟的这些串
_PREFIX_SUFFIXES = (".", "-thumb.")

# 中缀可变、写不成固定前缀的形态，改用整段尾巴的模式匹配。
# trickplay bif：``-320-10.bif``（宽度-间隔）、``-320.bif``（只有宽度）
_TAIL_PATTERNS = (re.compile(r"^-\d+(?:-\d+)*\.bif$", re.IGNORECASE),)


def sidecar_tail(main: Path, entry: Path) -> str | None:
    """``entry`` 是 ``main`` 的附属文件时返回要跟随的尾巴，否则 ``None``。

    只做名字与扩展名判定，不碰磁盘（``entry`` 是否为文件由调用方按各自的
    遍历方式确认——有的调用方手上已经有 ``iterdir`` 的结果，再 stat 一次
    是白花的系统调用）。
    """
    if entry == main:
        return None
    if entry.suffix.lower() in SIDECAR_SKIP_EXTS:
        return None
    name = entry.name
    stem = main.stem
    if not name.startswith(stem):
        return None
    tail = name[len(stem) :]
    if any(name.startswith(stem + suffix) for suffix in _PREFIX_SUFFIXES):
        return tail
    if any(pattern.match(tail) for pattern in _TAIL_PATTERNS):
        return tail
    return None


def find_sidecars(main: Path) -> list[tuple[Path, str]]:
    """``main`` 同目录下的全部附属文件，返回 ``(路径, 尾巴)`` 列表（按名排序）。

    原盘目录（无扩展名）没有附属概念，返回空。目录不可读时按"没有附属"
    处理——整理/转移的主流程不该因为一次 ``iterdir`` 失败整轮中断。
    """
    if main.is_dir() or not main.suffix:
        return []
    try:
        entries = sorted(main.parent.iterdir())
    except OSError:
        return []
    found: list[tuple[Path, str]] = []
    for entry in entries:
        if not entry.is_file():
            continue
        tail = sidecar_tail(main, entry)
        if tail is not None:
            found.append((entry, tail))
    return found
