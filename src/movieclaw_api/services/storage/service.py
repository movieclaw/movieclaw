"""缓存管理：按登记表统计占用、执行清理（docs/design/cache-management.md §4）。

两件事：

- ``usage()``：给面板的状态——上一次的快照（每个登记目录的体积与条目数、data/
  所在磁盘的总量/剩余、未登记目录）+ 后台是否正在重算。递归统计大目录（几万张
  刮削图）可能要几秒到几十秒，**因此这个入口从不阻塞**：打开页面永远立刻拿到
  上次的结果与它的统计时刻，要不要重算由用户点「刷新」决定（清理动作会把快照
  标脏，下次读取时自动在后台重算）。重算在线程池里跑，同时只跑一个，前端据
  ``computing`` 轮询，新数据到了再替换页面上的旧数据。
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
class UsageState:
    """面板一次读取拿到的全部东西。

    - ``usage``：上一次统计的结果，从未统计过时为空（前端显示骨架屏）；
    - ``computing``：后台是否正在统计，前端据此显示进行中状态并轮询；
    - ``error``：上一次统计失败的原因（失败时旧快照与旧时间原样保留）。
    """

    usage: StorageUsage | None
    computing: bool
    error: str | None


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


#: 上一次统计的结果；没有 TTL，一直用到用户点刷新或清理动作把它标脏为止。
_snapshot: StorageUsage | None = None
#: 正在跑的后台统计任务（同时只跑一个）
_task: asyncio.Task[None] | None = None
#: 快照需要重算：初始为真（进程起来后第一次打开面板自动算一次），清理后置真。
_stale: bool = True
_error: str | None = None


async def usage(*, refresh: bool = False) -> UsageState:
    """读取当前状态——**从不阻塞**，永远立刻返回。

    需要重算时（用户点了刷新、进程内还没算过、清理后标脏）在后台起一个任务，
    本次调用带着旧快照与 ``computing=True`` 直接返回；前端继续显示旧数据并轮询，
    新数据落地后再整体替换，不会让页面卡在加载态。
    """
    global _task, _stale, _error
    if (refresh or _stale) and _task is None:
        _stale = False
        _error = None
        _task = asyncio.create_task(_recompute())
    return UsageState(usage=_snapshot, computing=_task is not None, error=_error)


async def _recompute() -> None:
    """后台统计一次并替换快照；失败只记日志，旧快照保持可用。"""
    global _snapshot, _task, _error
    try:
        _snapshot = await asyncio.to_thread(compute_usage)
    except Exception as exc:  # noqa: BLE001 —— 统计失败不该让面板不可用
        _error = f"统计数据目录占用失败：{exc}"
        logger.warning("统计数据目录占用失败：%s", exc, exc_info=True)
    finally:
        _task = None


async def wait_for_usage(*, refresh: bool = False) -> StorageUsage | None:
    """触发（可选强制）统计并等到后台任务结束——需要同步拿结果的地方与测试用。"""
    await usage(refresh=refresh)
    task = _task
    if task is not None:
        await asyncio.wait([task])
    return _snapshot


def invalidate() -> None:
    """把快照标脏：不丢弃旧数据（页面不会闪成空白），下次读取时在后台重算。"""
    global _stale
    _stale = True


def reset_for_tests() -> None:
    global _snapshot, _task, _stale, _error
    _snapshot = None
    _task = None
    _stale = True
    _error = None


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
