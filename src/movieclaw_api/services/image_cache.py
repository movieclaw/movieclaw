"""远程图片的本地磁盘缓存 —— 静态资源统一收口的核心。

前端所有远程图片（TMDB 海报、豆瓣剧照、PT 站种子详情图……）都经
/images/proxy 走到这里：首次访问由 ImageProxy 回源抓取并落盘，之后同一
URL 的访问直接读本地文件，不再消耗外网流量，也不受图床限速/失联影响。

设计要点：
- 缓存目录默认 data/cache/images，与 SQLite、uploads 同在 data/ 下，
  Docker 部署只需挂载 data 一个卷即可整体持久化（丢了也只是重新回源）。
- 缓存键 = sha256(原始 URL)。URL 含域名，不同图床的同名路径天然不冲突；
  按哈希前两位分片子目录，避免单目录文件数过大拖慢文件系统。
- 每个条目两个文件：<hash>（图片字节）和 <hash>.json（源 URL、Content-Type、
  抓取时间，便于排查"这张缓存是哪来的"）。以 .json 的存在作为条目有效的
  标志；写入先落临时文件再原子 rename（先内容后元数据），进程中途崩溃
  不会留下"看似有效实则残缺"的条目。
- 同一 URL 的并发请求 singleflight 去重，只有一个真正回源，其余等它落盘。
- 容量控制：写入量累计到一档阈值后同步触发一次清理，按 mtime 从最旧的
  条目开始删除，降到上限的 90% 为止；命中时刷新内容文件 mtime，等效 LRU。
- 命中路径按「家用 NAS 的磁盘最怕随机小 IO」调过（见 ``_read_hit``）：
  元数据在进程内记一份、mtime 刷新按小时节流，一次海报墙滚动因此不再
  产生上百次元数据读与上百次 inode 写。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.image_proxy import ImageProxy, get_image_proxy

logger = logging.getLogger("movieclaw_api.image_cache")


@dataclass(frozen=True)
class CachedImage:
    """一条已落盘的缓存：路由层用 FileResponse 直接把 path 发回浏览器。"""

    path: Path
    content_type: str
    # 内容写入版本；派生缓存把它纳入键，原图被重新回源后不会继续命中旧缩略图。
    version: str = "0"


CacheProducer = Callable[[], Awaitable[tuple[bytes, str]]]

#: 命中时 mtime 至少这么旧才值得刷新（秒）。mtime 只服务于 LRU 淘汰排序，
#: 「一小时内被用过」和「一分钟前被用过」对淘汰决策没有区别，却是一次实打实
#: 的 inode 写——海报墙滚一屏上百张图，每张都刷一次就是上百次随机写。
_MTIME_REFRESH_SECONDS = 3600.0

#: 进程内记住的条目元数据条数上限（每条不到 200 字节）。满了整体清空：
#: 这是纯加速缓存，丢了只是回到读 .json 的老路，不值得为它引入 LRU。
_HOT_META_MAX = 4096


class ImageCache:
    """按 URL 哈希落盘的图片缓存，回源由 ImageProxy 完成。"""

    def __init__(self, cache_dir: Path, proxy: ImageProxy, *, max_bytes: int) -> None:
        self._dir = cache_dir
        self._proxy = proxy
        self._max_bytes = max_bytes
        # 每写入约 1/10 容量（上限 64MB）检查一次总量，避免每次写入都全目录扫描
        self._purge_interval = max(1, min(64 * 1024 * 1024, max_bytes // 10))
        self._bytes_since_purge = 0
        self._inflight: dict[str, asyncio.Task[CachedImage]] = {}
        # 内容文件的 (mtime_ns, 大小) -> (content_type, version)：见 _read_hit
        self._hot: dict[str, tuple[int, int, str, str]] = {}

    def _entry_paths(self, cache_key: str) -> tuple[Path, Path]:
        """缓存键 -> (内容文件, 元数据文件)；原图的键仍是完整 URL。"""
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        content = self._dir / digest[:2] / digest
        return content, content.with_suffix(".json")

    # ---- 磁盘操作（均为同步函数，调用方用 asyncio.to_thread 包装） ----------

    def _read_hit(
        self, cache_key: str, content_path: Path, meta_path: Path
    ) -> tuple[str, str] | None:
        """命中则返回类型/版本，未命中返回 None。

        一次命中原本要三次磁盘操作：读 ``.json``、stat 内容文件、``utime``
        刷新 mtime。海报墙一屏上百张图，这就是上百次随机读加上百次 inode 写
        ——后者尤其贵，读还能被页缓存挡下来，写迟早要落盘，NAS 上还会顺带
        把硬盘唤醒。这里收敛成：

        1. 内容文件 stat 一次（既是存在性校验，也拿到 mtime 判断要不要刷新）；
        2. ``(mtime_ns, 大小)`` 与进程内记录一致就直接复用元数据，不读
           ``.json``——内容被重新写过时这对值必变，缓存自然失效；
        3. mtime 足够新就不写 ``utime``（阈值见 ``_MTIME_REFRESH_SECONDS``）。

        元数据文件仍然是条目有效性的提交标志：只有在需要读它的那一次
        （首次命中、或内容被换过）才校验它存在且可解析。
        """
        try:
            stat = content_path.stat()
        except OSError:
            return None
        hot = self._hot.get(cache_key)
        if hot is not None and hot[0] == stat.st_mtime_ns and hot[1] == stat.st_size:
            content_type, version = hot[2], hot[3]
        else:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                content_type = str(meta["content_type"])
                version = str(meta.get("version") or meta.get("fetched_at") or "0")
            except (OSError, ValueError, KeyError):
                return None
            if len(self._hot) >= _HOT_META_MAX:
                self._hot.clear()
            self._hot[cache_key] = (stat.st_mtime_ns, stat.st_size, content_type, version)
        if time.time() - stat.st_mtime >= _MTIME_REFRESH_SECONDS:
            with contextlib.suppress(OSError):
                os.utime(content_path)
        return content_type, version

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _store(
        self,
        cache_key: str,
        content_path: Path,
        meta_path: Path,
        data: bytes,
        ct: str,
        metadata: dict[str, str] | None,
    ) -> str:
        content_path.parent.mkdir(parents=True, exist_ok=True)
        # 先写内容、后写元数据：元数据文件的出现即"条目已完整"的提交点
        self._atomic_write(content_path, data)
        version = str(time.time_ns())
        meta = {
            "cache_key": cache_key,
            "content_type": ct,
            "fetched_at": int(time.time()),
            "version": version,
            **(metadata or {}),
        }
        self._atomic_write(meta_path, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        return version

    def _purge_if_over_limit(self) -> None:
        """总量超上限时按 mtime 淘汰最旧条目，直到降至上限的 90%。"""
        entries: list[tuple[float, int, Path]] = []  # (mtime, 条目总字节, 内容文件路径)
        total = 0
        for meta_path in self._dir.glob("*/*.json"):
            content_path = meta_path.with_suffix("")
            try:
                stat = content_path.stat()
                size = stat.st_size + meta_path.stat().st_size
            except OSError:
                continue
            entries.append((stat.st_mtime, size, content_path))
            total += size
        if total <= self._max_bytes:
            return
        entries.sort()
        target = int(self._max_bytes * 0.9)
        removed = 0
        for _mtime, size, content_path in entries:
            if total <= target:
                break
            # 先删元数据（撤销"有效"标志），再删内容，保证残留状态永远可识别
            meta_path = content_path.with_suffix(".json")
            meta_path.unlink(missing_ok=True)
            content_path.unlink(missing_ok=True)
            total -= size
            removed += 1
        # 被淘汰的条目在进程内记的元数据一并作废（键与路径不是一一可逆的，
        # 整体清空最省事；清理本身就罕见，重建元数据只是多读几次 .json）
        self._hot.clear()
        logger.info("图片缓存超过容量上限，已清理最久未访问的 %d 个条目", removed)

    # ---- 对外接口 -----------------------------------------------------------

    async def get_or_fetch(self, url: str) -> CachedImage:
        """命中直接返回本地文件；未命中回源下载并落盘（同 URL 并发只回源一次）。"""
        return await self.get_or_create(
            url,
            lambda: self._proxy.fetch(url),
            metadata={"url": url},
        )

    async def get_or_create(
        self,
        cache_key: str,
        producer: CacheProducer,
        *,
        metadata: dict[str, str] | None = None,
    ) -> CachedImage:
        """缓存任意图片产物；远程原图与缩略派生图共享 singleflight 和 LRU。

        ``cache_key`` 必须完整描述产物身份；``producer`` 只在未命中时执行。
        这让图片变体服务无需再造一套落盘、并发去重和容量清理机制。
        """
        content_path, meta_path = self._entry_paths(cache_key)
        hit = await asyncio.to_thread(self._read_hit, cache_key, content_path, meta_path)
        if hit is not None:
            content_type, version = hit
            return CachedImage(content_path, content_type, version)

        key = content_path.name
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._produce_and_store(
                    cache_key,
                    content_path,
                    meta_path,
                    producer,
                    metadata,
                )
            )
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        # shield：浏览器中途取消某个 <img> 请求时，不连累共享同一产物的其他请求
        return await asyncio.shield(task)

    async def _produce_and_store(
        self,
        cache_key: str,
        content_path: Path,
        meta_path: Path,
        producer: CacheProducer,
        metadata: dict[str, str] | None,
    ) -> CachedImage:
        data, content_type = await producer()
        version = await asyncio.to_thread(
            self._store,
            cache_key,
            content_path,
            meta_path,
            data,
            content_type,
            metadata,
        )
        self._bytes_since_purge += len(data)
        if self._bytes_since_purge >= self._purge_interval:
            self._bytes_since_purge = 0
            await asyncio.to_thread(self._purge_if_over_limit)
        return CachedImage(content_path, content_type, version)


_cache: ImageCache | None = None


def get_image_cache() -> ImageCache:
    """取得进程级缓存单例（目录与容量来自配置，回源走共享的 ImageProxy）。"""
    global _cache
    if _cache is None:
        settings = get_settings()
        _cache = ImageCache(
            Path(settings.image_cache_dir),
            get_image_proxy(),
            max_bytes=settings.image_cache_max_mb * 1024 * 1024,
        )
    return _cache


def reset_image_cache() -> None:
    """仅供测试清理单例。"""
    global _cache
    _cache = None
