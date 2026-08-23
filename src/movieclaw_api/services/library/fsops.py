"""文件落位原语：防覆盖改名（renameat2 RENAME_NOREPLACE）。

入库/整理的落位统一走「先在目标目录备好内容，再 rename 发布到最终名」，
而不是直接 ``os.link`` 到最终名。两个原因：

1. **防覆盖**：``os.rename`` 会静默覆盖已存在的目标；RENAME_NOREPLACE 让
   rename 在目标已存在时原子失败（EEXIST），与旧实现借 ``os.link`` 的
   EEXIST 语义完全等价——「计划检查后、执行前恰有同名文件落地」的并发
   覆盖窗口依然被堵死；
2. **极空间兼容（issue #103）**：极空间的存储是自研 FUSE 文件系统
   （zfuse.zfsv*），极影视的媒体索引只感知 create/write/rename 这类目录
   元数据变更，硬链接新增的目录项**不产生索引事件**——直接 link 到最终名
   的文件在极影视里永远"不存在"。rename 必然更新目录元数据、会被索引
   感知（NAStool/MoviePilot 对极空间的适配同一原理，MoviePilot #667）。

回退语义：RENAME_NOREPLACE 需要文件系统支持（ext4/btrfs/xfs/overlayfs 均
支持；FUSE 类——恰好包括极空间——可能不支持，返回 EINVAL/ENOSYS/ENOTSUP）。
回退路径是「先查存在再普通 rename」，留下单次系统调用宽度的竞态窗口；
**绝不回退到 os.link**——那会在极空间上重新引入本模块要修的索引盲区。
"""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path

# renameat2 的 flag 与 dirfd 常量（Linux 内核 ABI，不随 libc 版本变化）
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100

# glibc >= 2.28 提供 renameat2 封装（运行镜像 Debian trixie 为 2.41）。
# 拿不到符号时置 None，永远走回退路径——功能不缺失，只是防覆盖不再原子
try:
    _libc = ctypes.CDLL(None, use_errno=True)
    _renameat2 = _libc.renameat2
except (OSError, AttributeError):  # pragma: no cover -- 非 Linux/远古 libc
    _renameat2 = None

# 「文件系统不支持 NOREPLACE」的 errno：EINVAL（FUSE 未实现 RENAME2）、
# ENOSYS（旧内核无此系统调用）、ENOTSUP（Linux 上与 EOPNOTSUPP 同值 95）
_UNSUPPORTED_ERRNOS = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}


def rename_no_replace(src: Path, dst: Path) -> None:
    """原子防覆盖改名：目标已存在抛 FileExistsError，其余错误原样抛 OSError。

    src 与 dst 必须在同一文件系统（跨设备抛 EXDEV，与 os.rename 一致）。
    """
    if _renameat2 is not None:
        res = _renameat2(
            _AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE
        )
        if res == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), str(src), None, str(dst))
        if err not in _UNSUPPORTED_ERRNOS:
            raise OSError(err, os.strerror(err), str(src), None, str(dst))
        # 落到这里 = 文件系统不支持 NOREPLACE，走下方回退
    if dst.exists():
        raise FileExistsError(
            errno.EEXIST, os.strerror(errno.EEXIST), str(src), None, str(dst)
        )
    os.rename(src, dst)
