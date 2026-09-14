"""根路径落在什么挂载上——本地盘还是网络挂载（docs/design/library.md「对账」）。

实时监控（inotify）在网络挂载上收不到远端的任何变更：NFS / SMB 的客户端内核
不会为服务器那头的改动产生 fs 事件。库开着「实时监控」、根却在 NFS 上，就是
一个**看起来有保障、其实全靠每几小时一次的定期对账兜底**的状态——用户以为
新文件会秒进库，实际要等对账。这是产品诚实度问题，比性能问题更该先修。

这里只回答一个问题：一条路径落在哪种挂载上。判定读 ``/proc/mounts``（容器里
看到的就是容器自己的挂载表，与 movieclaw 视角的路径一致），取**最长前缀**的
挂载点，按文件系统类型归类。读不到（非 Linux、权限）就是 ``unknown``——不猜。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Literal

MountKind = Literal["local", "network", "unknown"]

#: 网络 / 远端文件系统：内核不会为远端变更产生 inotify 事件。fuse.* 一律按网络
#: 算（rclone / sshfs / davfs 都走 fuse，本地 fuse 文件系统极少见于媒体库根）
_NETWORK_FSTYPES = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "ncpfs", "afs", "9p", "davfs", "sshfs"}
_NETWORK_FSTYPE_PREFIXES = ("fuse", "nfs")

MOUNTS_FILE = "/proc/mounts"


def _classify(fstype: str) -> MountKind:
    if fstype in _NETWORK_FSTYPES or fstype.startswith(_NETWORK_FSTYPE_PREFIXES):
        return "network"
    return "local"


def _unescape(field: str) -> str:
    """/proc/mounts 里空格等字符以八进制转义（``\\040``）。"""
    out = []
    i = 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def parse_mounts(text: str) -> list[tuple[str, str]]:
    """``/proc/mounts`` 文本 → ``[(挂载点, 文件系统类型)]``，按挂载点长度降序（最长前缀优先）。"""
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        entries.append((_unescape(parts[1]), parts[2]))
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    return entries


def mount_kind_of(path: str, mounts: list[tuple[str, str]]) -> MountKind:
    """路径落在哪种挂载上：取最长前缀的挂载点。挂载表为空（读不到）→ unknown。"""
    if not mounts:
        return "unknown"
    target = PurePosixPath(path)
    for mount_point, fstype in mounts:
        mp = PurePosixPath(mount_point)
        if target == mp or mp in target.parents:
            return _classify(fstype)
    return "unknown"


@lru_cache(maxsize=1)
def _mounts_snapshot(_mtime_key: float) -> list[tuple[str, str]]:
    try:
        with open(MOUNTS_FILE, encoding="utf-8", errors="replace") as fh:
            return parse_mounts(fh.read())
    except OSError:
        return []


def system_mounts() -> list[tuple[str, str]]:
    """当前进程看到的挂载表。挂载表变化极少，按文件 mtime 做缓存键，一分钟粒度兜底。"""
    try:
        key = os.stat(MOUNTS_FILE).st_mtime
    except OSError:
        key = -1.0
    return _mounts_snapshot(key)


def is_network_root(path: str) -> bool:
    return mount_kind_of(path, system_mounts()) == "network"


def library_on_network_mount(root_paths: list[str]) -> bool:
    """库的任一根落在网络挂载上就算——一个根收不到事件，"实时"就不成立。"""
    return any(is_network_root(root) for root in root_paths)
