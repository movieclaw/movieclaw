"""缓存管理：按登记表统计占用、执行清理（docs/design/cache-management.md §4）。

两件事：

- ``usage()``：给面板的一次性快照——每个登记目录的体积与条目数、data/ 所在磁盘
  的总量/剩余、未登记目录。递归统计大目录（几万张刮削图）可能要几秒到几十秒，
  因此放线程池跑、结果在进程内缓存 ``_TTL`` 秒，并发请求 singleflight 只算一次；
  面板显示「统计于 N 分钟前」并提供手动刷新。
- ``clean()``：按 key + 模式清理。只删登记目录**里面的直接子项**，永远不删目录
  本身；先问登记项的 ``busy`` 探测把正在使用的条目摘出去，再按模式决定删哪些
  （``all`` 全部、``orphans`` 只删 ``orphans`` 探测认定的孤儿）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services.storage import registry
from movieclaw_api.services.storage.registry import DataDir, Group

logger = logging.getLogger("movieclaw_api.storage")

#: 快照有效期。清理动作会主动作废快照，所以这只是「用户反复切标签」的防抖。
_TTL = 120.0


@dataclass(frozen=True)
class DirUsage:
    key: str
    title: str
    summary: str
    description: str
    path: str
    group: str
    rebuild_cost: str
    clearable: bool
    orphan_aware: bool
    exists: bool
    bytes: int
    entries: int


@dataclass(frozen=True)
class UnregisteredEntry:
    path: str
    bytes: int


@dataclass(frozen=True)
class StorageUsage:
    data_root: str
    disk_total: int
    disk_used: int
    disk_free: int
    #: 登记为派生物缓存的目录合计（面板里「可回收」那一段）
    cache_bytes: int
    #: 用户数据与系统状态合计（不可回收）
    data_bytes: int
    dirs: list[DirUsage]
    unregistered: list[UnregisteredEntry]
    computed_at: int


@dataclass(frozen=True)
class CleanResult:
    key: str
    mode: str
    removed: int
    skipped_busy: int
    freed_bytes: int


# ---------------------------------------------------------------------------
# 体积统计
# ---------------------------------------------------------------------------


def size_of(path: Path) -> int:
    """文件或目录的总字节数。不跟随符号链接（``models/ner/current`` 指向的目录
    只在其真实位置算一次），统计途中的 OSError 一律跳过。"""
    try:
        st = path.lstat()
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return st.st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def _entry_count(path: Path) -> int:
    if not path.is_dir():
        return 1 if path.exists() else 0
    try:
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0


def _dir_usage(spec: DataDir, path: Path) -> DirUsage:
    total = size_of(path)
    if spec.key == "database":
        total += sum(size_of(p) for p in registry.sqlite_sidecars(path))
    return DirUsage(
        key=spec.key,
        title=spec.title,
        summary=spec.summary,
        description=spec.description,
        path=str(path),
        group=spec.group.value,
        rebuild_cost=spec.rebuild_cost.value,
        clearable=spec.clearable,
        orphan_aware=spec.orphan_aware,
        exists=path.exists(),
        bytes=total,
        entries=_entry_count(path),
    )


def compute_usage() -> StorageUsage:
    """（阻塞）完整统计一次。"""
    root = registry.data_root().resolve()
    dirs = [_dir_usage(spec, path) for spec, path in registry.resolved()]
    unregistered = [
        UnregisteredEntry(path=str(p), bytes=size_of(p)) for p in registry.unregistered_entries()
    ]
    try:
        disk = shutil.disk_usage(root if root.exists() else root.parent)
        total, used, free = disk.total, disk.used, disk.free
    except OSError:
        total = used = free = 0
    return StorageUsage(
        data_root=str(root),
        disk_total=total,
        disk_used=used,
        disk_free=free,
        cache_bytes=sum(d.bytes for d in dirs if d.group == Group.CACHE.value),
        data_bytes=sum(d.bytes for d in dirs if d.group == Group.DATA.value)
        + sum(u.bytes for u in unregistered),
        dirs=dirs,
        unregistered=unregistered,
        computed_at=int(time.time()),
    )


_snapshot: StorageUsage | None = None
_inflight: asyncio.Task[StorageUsage] | None = None


async def usage(*, refresh: bool = False) -> StorageUsage:
    """带 TTL 缓存与 singleflight 的统计入口。"""
    global _snapshot, _inflight
    if _snapshot is not None and not refresh and time.time() - _snapshot.computed_at < _TTL:
        return _snapshot
    if _inflight is None:

        async def run() -> StorageUsage:
            global _snapshot, _inflight
            try:
                snapshot = await asyncio.to_thread(compute_usage)
                _snapshot = snapshot
                return snapshot
            finally:
                _inflight = None

        _inflight = asyncio.create_task(run())
    return await asyncio.shield(_inflight)


def invalidate() -> None:
    global _snapshot
    _snapshot = None


def reset_for_tests() -> None:
    global _snapshot, _inflight
    _snapshot = None
    _inflight = None


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

_clean_lock = asyncio.Lock()


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


async def clean(key: str, mode: str) -> CleanResult:
    """清理一个登记目录里的条目。``mode`` 为 ``all`` 或 ``orphans``。"""
    spec = registry.find(key)
    if spec is None:
        raise NotFoundException(f"没有名为 {key} 的登记目录")
    if mode == "all" and not spec.clearable:
        raise BadRequestException(f"「{spec.title}」不允许整体清空")
    if mode == "orphans" and spec.orphans is None:
        raise BadRequestException(f"「{spec.title}」不支持按孤儿条目清理")
    if mode not in ("all", "orphans"):
        raise BadRequestException(f"未知的清理模式：{mode}")

    async with _clean_lock:
        path = spec.resolve(get_settings()).resolve()
        entries = await asyncio.to_thread(registry_children, path)
        busy = await spec.busy(entries) if spec.busy is not None else set()
        if mode == "orphans":
            assert spec.orphans is not None
            targets = await spec.orphans(entries)
        else:
            targets = set(entries)
        targets -= busy

        removed = 0
        freed = 0
        for entry in sorted(targets):
            size = await asyncio.to_thread(size_of, entry)
            try:
                await asyncio.to_thread(_remove, entry)
            except OSError as exc:
                logger.warning("清理「%s」时删除 %s 失败：%s", spec.title, entry, exc)
                continue
            removed += 1
            freed += size
        invalidate()

    logger.info(
        "缓存清理完成：%s（%s）删除 %d 项、释放 %d 字节，跳过正在使用的 %d 项",
        spec.title,
        "全部" if mode == "all" else "孤儿",
        removed,
        freed,
        len(busy),
    )
    return CleanResult(
        key=key,
        mode=mode,
        removed=removed,
        skipped_busy=len(busy),
        freed_bytes=freed,
    )


def registry_children(path: Path) -> list[Path]:
    """登记目录的直接子项（清理的最小单位）；目录不存在时为空。"""
    if not path.is_dir():
        return []
    try:
        return sorted(path.iterdir())
    except OSError:
        return []
