"""媒体库实时监控（L4）：watchdog 文件事件 → 去抖批处理 → 增量扫描。

设计（吸收 moviebot 的三个实现细节，见设计文档第 1 节核实备注）：
- **事件只进队列**：watchdog 的回调跑在它自己的观察者线程里，绝不做任何
  IO/识别，只把"哪个库、哪个条目有动静"投进 asyncio 队列（线程安全经
  call_soon_threadsafe 桥接）。投递前先在观察者线程侧做同键合并：下载
  写入的 modified 风暴每秒可达上千条，逐条唤醒事件循环本身就是开销，
  合并窗口远小于消费侧去抖窗口，不会让去抖误判安静；
- **去抖批处理**：下载器/整理器落盘会在短时间产生大量事件，消费者收到
  首个事件后等待安静窗口（3s，最长 30s 兜底）再触发一次增量扫描；
- **范围扫描**：事件路径能定位到库根下的第一级条目，扫描就只遍历这些
  条目子树（scan_library 的 scope_paths），不再为一个文件事件遍历整库
  ——网络挂载/机械盘大库的稳态成本从 O(整库) 降到 O(变动条目)。范围
  解析存疑时扫描侧自动退回整库，多扫永远安全；
- **写入完成检测**：不追踪单文件的写入进度（moviebot 在事件线程里
  sleep 轮询是反面教训）——去抖窗口天然给了写入落定的时间，且增量扫描
  遇到仍在写入的文件下轮对账会再补。

生命周期：应用启动时 start——初始建 watch 放后台任务执行，**不阻塞应用
启动**（网络挂载上递归建 watch 可达分钟级，曾把整个 FastAPI startup 拖到
超时，见 issue #162）；库增删改后调用 ``refresh_watches`` **差量**调整监听
（只为变化的根付建 watch 的成本，不动的库零开销）；关闭时 stop。watchdog
缺失或平台不支持时优雅降级——只靠 6 小时对账任务兜底，功能不缺失只是
不实时。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("movieclaw_api.library_watch")

# 去抖参数：首个事件后等安静 3 秒；持续有事件时最长 30 秒必触发一次
_QUIET_SECONDS = 3.0
_MAX_WAIT_SECONDS = 30.0

# 观察者线程侧的同键合并窗口：必须远小于 _QUIET_SECONDS，否则持续写入
# 期间消费者会把"事件被合并掉"误判成"已经安静"
_COALESCE_SECONDS = 0.5

# 一批事件涉及的条目数超过该值就退回整库扫描：批量改名/迁移场景下逐条目
# 扫描的总成本反而高于一次整库遍历
_SCOPE_LIMIT = 64

# 扫描进行中收到的事件：轮询等扫描结束再补触发的间隔与放弃上限
_RESCAN_POLL_SECONDS = 2.0
_RESCAN_MAX_POLLS = 900  # 约 30 分钟，超时放弃（对账任务兜底）


def _is_relevant_event(event, watched: frozenset[str] | set[str] | None = None) -> bool:  # noqa: ANN001
    """该文件事件是否值得触发扫描（观察者线程调用，只做纯判定）。

    ``watched``：该库入账对象的扩展名集合（能力档案 ``media_exts``）+ 字幕。
    **按库给、不取全库并集**：影视库目录里出现 jpg 是刮削器在写海报，取并集
    就会把它当成图片库的新内容触发扫描，正是下面第 2 条防的自激。缺省
    （旧调用方/测试）按视频口径。

    两层过滤，都是防「系统自己产生的事件又触发扫描」的自激：

    1. 只读事件（打开 / 未写关闭）忽略：Linux inotify 连"读文件"都会
       上报，而扫描的补探阶段正是用 ffprobe **读**库里的文件。不滤掉就是
       「扫描 → 读文件事件 → 再扫描」的循环——探测失败的行每轮保持
       NULL 重试，循环永不收敛（实测线上每 ~10 秒一轮无限扫描，
       扫描进行中的状态在前端时隐时现）；
    2. 视频/strm/**字幕**之外的文件变动忽略：刮削/整库刷新写进库目录的
       NFO 和图片、下载器的 .!qB 半成品，都不是扫描的盘点对象，触发扫描
       只会让「正在扫描」在刷新/下载期间反复闪现。下载完成时改名缀上
       视频扩展名，那一刻自然收到 moved/created 事件。字幕文件自
       外挂字幕台账（jellyfin-subtitle.md §2.1）起是盘点对象——用户把
       .srt 丢进目录要即时可见；秒过时的重发现只做内存匹配 + 对少数字幕
       文件 stat，且服务端只 stat 不 open，不构成读事件自激。

    目录事件只留移动/删除——整个条目目录被挪走/删掉时收不到目录内
    逐文件的事件；目录的新建/修改则必有后续的文件事件跟进，不必抢跑。
    """
    from watchdog.events import EVENT_TYPE_CLOSED_NO_WRITE, EVENT_TYPE_OPENED

    from movieclaw_api.services.library.layout import SCAN_VIDEO_EXTS, SUBTITLE_EXTS

    if event.event_type in (EVENT_TYPE_OPENED, EVENT_TYPE_CLOSED_NO_WRITE):
        return False
    if event.is_directory:
        return event.event_type in ("moved", "deleted")
    # moved 事件的语义看终点：改名成视频（下载完成）要触发，视频被改走
    # （旧路径消失）同样要触发——起点终点任一是视频扩展名即算数。
    # strm 与视频同权：网盘工具重新生成 strm 树时台账要跟着对齐
    if watched is None:
        watched = SCAN_VIDEO_EXTS | SUBTITLE_EXTS
    paths = (getattr(event, "dest_path", "") or "", event.src_path or "")
    return any(Path(os.fsdecode(p)).suffix.lower() in watched for p in paths if p)


def _modified_video_path(event) -> str | None:  # noqa: ANN001
    """视频文件的 modified 事件返回其路径（内容变化重探的点名依据，
    jellyfin-subtitle.md §2.4）；其余事件返回 None。

    只认 modified：created/moved 走正常入账（本就会探测），deleted 走
    丢失标记。strm 不点名——它无媒体流，重探无意义。
    """
    from movieclaw_api.services.library.layout import VIDEO_EXTS

    if event.is_directory or event.event_type != "modified":
        return None
    path = os.fsdecode(event.src_path or "")
    if path and Path(path).suffix.lower() in VIDEO_EXTS:
        return path
    return None


def _scope_for(root: str, path: str) -> str | None:
    """事件路径 → 库根下第一级条目的绝对路径（范围扫描的最小单位）。

    返回 None 表示定位不到条目（事件落在根自身，或路径不在根下）——
    消费侧据此退回整库扫描。第一级条目是文件还是目录在这里不区分，
    扫描侧解析范围时会核实（非现存目录一律整库，见 scan._resolve_scope）。
    """
    prefix = root.rstrip(os.sep) + os.sep
    if not path.startswith(prefix):
        return None
    first = path[len(prefix) :].split(os.sep, 1)[0]
    return prefix + first if first else None


def _event_scopes(root: str, event) -> set[str | None]:  # noqa: ANN001
    """一条事件涉及的条目范围集合（moved 事件起点终点可能属于不同条目）。"""
    paths = [os.fsdecode(event.src_path or "")]
    dest = os.fsdecode(getattr(event, "dest_path", "") or "")
    if dest:
        paths.append(dest)
    return {_scope_for(root, p) for p in paths if p}


class _PendingScan:
    """去抖批次里单个库累计的扫描诉求（全量标志 + 条目范围 + 重探点名）。"""

    __slots__ = ("full", "reprobe", "scopes")

    def __init__(self) -> None:
        self.full = False
        self.scopes: set[str] = set()
        self.reprobe: set[str] = set()

    def add(self, scope: str | None, modified_path: str | None) -> None:
        if scope is None:
            self.full = True
        else:
            self.scopes.add(scope)
        if modified_path is not None:
            self.reprobe.add(modified_path)

    def merge(self, other: _PendingScan) -> None:
        self.full = self.full or other.full
        self.scopes.update(other.scopes)
        self.reprobe.update(other.reprobe)

    def scope_paths(self) -> set[str] | None:
        """交给 scan_library 的范围；None = 整库。"""
        if self.full or not self.scopes or len(self.scopes) > _SCOPE_LIMIT:
            return None
        return set(self.scopes)


class LibraryWatcher:
    """库根路径的文件事件监听器（进程级单例，见 init_library_watcher）。"""

    def __init__(self) -> None:
        self._observer = None
        # (library_id, 根路径) → (handler, ObservedWatch)：差量重建的台账。
        # 仅在持有 _refresh_lock 的工作线程里读写
        self._entries: dict[tuple[int, str], tuple[object, object]] = {}
        # 库 id → 该库监听的扩展名集合（handler 创建时快照进闭包）
        self._watched_exts: dict[int, frozenset[str]] = {}
        # 队列元素 (library_id, 条目范围|None=整库, 内容被修改的视频路径|None)
        self._queue: asyncio.Queue[tuple[int, str | None, str | None]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consumer: asyncio.Task | None = None
        # 初始建 watch 的后台任务：不阻塞应用启动（issue #162）
        self._startup: asyncio.Task | None = None
        self._available = True
        self._closed = False
        # 同键合并的最近投递时刻（观察者的单一 dispatch 线程读写，无需锁）
        self._recent_keys: dict[tuple, float] = {}
        # 扫描进行中收到的事件：库 id → 待补触发的诉求 / 等待任务
        self._rescan_pending: dict[int, _PendingScan] = {}
        self._rescan_tasks: dict[int, asyncio.Task] = {}
        # 串行化重建：连续两次库编辑各自触发 refresh，并发重建会互踩 _observer
        self._refresh_lock = asyncio.Lock()

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._consumer = asyncio.create_task(self._consume())
        # 初始建 watch 放后台：网络挂载上的递归建 watch 可达分钟级，不能
        # 拖住 FastAPI startup（entrypoint 有就绪超时）；期间对账任务兜底
        self._startup = asyncio.create_task(self._startup_refresh())

    async def _startup_refresh(self) -> None:
        try:
            await self.refresh_watches()
        except Exception:  # noqa: BLE001 -- 后台任务无人 await，异常必须就地落日志
            logger.exception("媒体库实时监听初始化失败（定期对账仍会兜底发现新文件）")

    async def stop(self) -> None:
        self._closed = True
        for task in (self._consumer, self._startup, *self._rescan_tasks.values()):
            if task is not None:
                task.cancel()
        self._consumer = None
        self._startup = None
        self._rescan_tasks.clear()
        self._rescan_pending.clear()
        # 与后台重建互斥：重建正在工作线程里装配观察者时直接停引用会漏掉
        # 刚装好的 watch（关停竞态）；拿到锁再停则必停到最终状态
        async with self._refresh_lock:
            self._stop_observer()

    def _stop_observer(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        self._entries.clear()

    async def refresh_watches(self) -> None:
        """按当前库配置**差量**调整监听（库增删改根路径后调用）。

        磁盘部分（recursive 建 watch、网络挂载上的 is_dir、拆 watch 的
        join）都是阻塞调用，放线程池执行——inotify 对大库的递归建 watch
        可达数秒到数十秒，在事件循环里跑会冻住所有请求。差量意味着只有
        新增/移除/失活的根付这个成本，未变的库零开销。
        """
        if not self._available or self._closed:
            return
        try:
            import watchdog  # noqa: F401 -- 仅探测可用性，实际使用在 _apply_watches
        except ImportError:
            self._available = False
            logger.warning("未安装 watchdog，媒体库实时监控不可用——仅靠定期对账发现新文件")
            return

        from sqlmodel import select

        from movieclaw_db.engine import get_database
        from movieclaw_db.models import Library

        db = get_database()
        async with db.session() as session:
            libraries = list((await session.execute(select(Library))).scalars().all())
        # 关闭了实时监控的库（realtime_watch=false，SMB/NFS 网络挂载的典型
        # 选择）不进清单：差量重建会自动拆掉它已有的监听，其新文件由定期
        # 对账与手动扫描发现
        roots = [
            (library.id, str(Path(root)))
            for library in libraries
            if library.id is not None and library.realtime_watch
            for root in library.root_paths
        ]
        # 每库的监听扩展名口径（图片库只认图片，影视/其他库只认视频+strm）
        from movieclaw_api.services.library.layout import SUBTITLE_EXTS
        from movieclaw_api.services.library.profile import profile_of

        self._watched_exts = {
            library.id: frozenset(profile_of(library).media_exts | SUBTITLE_EXTS)
            for library in libraries
            if library.id is not None
        }
        async with self._refresh_lock:
            added, removed = await asyncio.to_thread(self._apply_watches, roots)
        if added or removed:
            logger.info(
                "媒体库实时监控已更新：新增 %d 个、移除 %d 个根路径，共监听 %d 个",
                added,
                removed,
                len(self._entries),
            )

    def _apply_watches(self, roots: list[tuple[int, str]]) -> tuple[int, int]:
        """（工作线程）差量调整监听，返回 (新增数, 移除数)。

        失活检测：根被卸载/删除后 inotify emitter 线程会悄悄死掉，
        watchdog 没有公开的检活 API，只能读观察者的内部表——拿不到就
        当作存活（宁可漏检，由对账兜底，也不能误拆好的监听）。
        """
        from watchdog.observers import Observer

        if self._closed:
            return (0, 0)
        if self._observer is None:
            observer = Observer()
            observer.daemon = True
            observer.start()
            self._observer = observer
        observer = self._observer

        desired = dict.fromkeys(roots)  # 去重且保序
        added = removed = 0
        # 先拆：不再需要的 key、以及 emitter 已死的 key（后者随后按需重建）
        for key, (handler, watch) in list(self._entries.items()):
            if key in desired and self._watch_alive(observer, watch):
                continue
            self._detach_entry(observer, key, handler, watch)
            removed += 1
        # 再挂：新出现的 key（含刚被拆掉的失活 key）
        for key in desired:
            if self._closed:
                break
            if key in self._entries:
                continue
            library_id, root = key
            if not os.path.isdir(root):
                continue  # 根路径未就绪：不告警刷屏，对账任务会持续兜底
            handler = self._make_handler(library_id, root)
            # 同一根被多个库共用时 watchdog 只允许一个 watch，把 handler
            # 挂到既有 watch 上；unschedule 只在最后一个引用拆除时执行
            shared = next(
                (w for (_lib, r), (_h, w) in self._entries.items() if r == root), None
            )
            try:
                if shared is not None:
                    observer.add_handler_for_watch(handler, shared)
                    watch = shared
                else:
                    # recursive 建 watch 在本线程同步走完整棵树（watchdog 的
                    # BaseThread.start 会在调用线程里跑 on_thread_start），
                    # 大树/网络挂载慢在这里；超 inotify watch 上限也在这里抛
                    watch = observer.schedule(handler, root, recursive=True)
            except OSError as exc:
                logger.warning("监听根路径失败（%s）：%s", root, exc)
                continue
            self._entries[key] = (handler, watch)
            added += 1
        return (added, removed)

    def _detach_entry(self, observer, key, handler, watch) -> None:  # noqa: ANN001
        """（工作线程）摘掉一个 (库, 根) 的 handler；最后一个引用时拆 watch。"""
        self._entries.pop(key, None)
        # KeyError 一律吞掉：watch 可能已随 emitter 死亡被 watchdog 清理
        with contextlib.suppress(KeyError):
            observer.remove_handler_for_watch(handler, watch)
        still_used = any(w is watch for _h, w in self._entries.values())
        if not still_used:
            with contextlib.suppress(KeyError):
                observer.unschedule(watch)

    @staticmethod
    def _watch_alive(observer, watch) -> bool:  # noqa: ANN001
        try:
            emitter = observer._emitter_for_watch.get(watch)  # noqa: SLF001
        except AttributeError:  # watchdog 内部结构变化：当作存活
            return True
        return emitter is not None and emitter.is_alive()

    def _make_handler(self, library_id: int, root: str):  # noqa: ANN202
        from watchdog.events import FileSystemEventHandler

        watcher = self
        watched = self._watched_exts.get(library_id)

        class _Handler(FileSystemEventHandler):
            """事件回调（观察者 dispatch 线程）：判定 + 合并 + 投递，不做业务。"""

            def on_any_event(self, event) -> None:  # noqa: ANN001
                if not _is_relevant_event(event, watched):
                    return
                modified = _modified_video_path(event)
                for scope in _event_scopes(root, event):
                    if watcher._should_enqueue((library_id, scope, modified)):
                        watcher._enqueue_threadsafe(library_id, scope, modified)

        return _Handler()

    # -- 事件通道 ----------------------------------------------------------

    def _should_enqueue(self, key: tuple) -> bool:
        """（观察者 dispatch 线程）同键合并：短窗口内重复事件不再跨线程投递。

        所有 handler 都在观察者唯一的 dispatch 线程回调，无需加锁。
        """
        now = time.monotonic()
        last = self._recent_keys.get(key)
        if last is not None and now - last < _COALESCE_SECONDS:
            return False
        if len(self._recent_keys) > 512:
            self._recent_keys = {
                k: t for k, t in self._recent_keys.items() if now - t < _COALESCE_SECONDS
            }
        self._recent_keys[key] = now
        return True

    def _enqueue_threadsafe(
        self, library_id: int, scope: str | None, modified_path: str | None
    ) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._queue.put_nowait, (library_id, scope, modified_path))

    async def _consume(self) -> None:
        """去抖消费：首事件后等安静窗口，汇总本批涉及的库做范围/整库扫描。"""
        while True:
            library_id, scope, modified_path = await self._queue.get()
            pending: dict[int, _PendingScan] = {}
            pending.setdefault(library_id, _PendingScan()).add(scope, modified_path)
            deadline = asyncio.get_running_loop().time() + _MAX_WAIT_SECONDS
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                timeout = min(_QUIET_SECONDS, max(remaining, 0))
                try:
                    library_id, scope, modified_path = await asyncio.wait_for(
                        self._queue.get(), timeout
                    )
                except TimeoutError:
                    break  # 安静窗口达成
                pending.setdefault(library_id, _PendingScan()).add(scope, modified_path)
                if asyncio.get_running_loop().time() >= deadline:
                    break  # 兜底：持续有事件也要触发
            for library_id in sorted(pending):
                await self._dispatch_scan(library_id, pending[library_id])

    async def _dispatch_scan(self, library_id: int, request: _PendingScan) -> None:
        from movieclaw_api.services.library.scan import is_scanning

        if is_scanning(library_id):
            # 扫描进行中落盘的文件本轮扫描未必看得到（遍历可能已过其目录），
            # 不能丢弃——记下诉求，等扫描结束补触发一次。补扫仍走本消费
            # 链路的范围扫描，不会放大成本；等不到结束就放弃（对账兜底）
            self._defer_until_scan_done(library_id, request)
            return
        logger.info("检测到媒体库 #%s 根路径变更，触发增量扫描", library_id)
        try:
            # 文件事件只需同步台账；历史规格补探由用户手动扫描触发，
            # 不让一次播放/下载相关的目录事件变成整库 ffprobe。
            # 本批 modified 点名的视频例外：事件本身就是变更信号，
            # 扫描会对它们 stat 确认后重探（jellyfin-subtitle.md §2.4）
            from movieclaw_api.services.library.scan import scan_library

            await scan_library(
                library_id,
                backfill_existing_specs=False,
                reprobe_paths=request.reprobe or None,
                scope_paths=request.scope_paths(),
            )
        except Exception:  # noqa: BLE001 -- 监控消费绝不崩
            logger.exception("实时监控触发的扫描失败：库 #%s", library_id)

    def _defer_until_scan_done(self, library_id: int, request: _PendingScan) -> None:
        existing = self._rescan_pending.get(library_id)
        if existing is not None:
            existing.merge(request)
        else:
            self._rescan_pending[library_id] = request
        task = self._rescan_tasks.get(library_id)
        if task is not None and not task.done():
            return
        self._rescan_tasks[library_id] = asyncio.create_task(
            self._requeue_after_scan(library_id)
        )

    async def _requeue_after_scan(self, library_id: int) -> None:
        from movieclaw_api.services.library.scan import is_scanning

        for _ in range(_RESCAN_MAX_POLLS):
            await asyncio.sleep(_RESCAN_POLL_SECONDS)
            if not is_scanning(library_id):
                break
        else:
            logger.warning(
                "媒体库 #%s 长时间处于扫描中，事件补扫已放弃（对账任务会兜底）",
                library_id,
            )
            self._rescan_pending.pop(library_id, None)
            self._rescan_tasks.pop(library_id, None)
            return
        request = self._rescan_pending.pop(library_id, None)
        self._rescan_tasks.pop(library_id, None)
        if request is None:
            return
        # 回投队列而不是直接扫：让去抖窗口把补扫与新到事件汇成一批。
        # 消费侧按库聚合范围集与点名集，点名路径挂哪个范围上都等价
        scope_paths = request.scope_paths()
        scopes: list[str | None] = sorted(scope_paths) if scope_paths else [None]
        for scope in scopes:
            self._queue.put_nowait((library_id, scope, None))
        for modified in sorted(request.reprobe):
            self._queue.put_nowait((library_id, scopes[0], modified))


_watcher: LibraryWatcher | None = None


def get_library_watcher() -> LibraryWatcher | None:
    return _watcher


async def init_library_watcher() -> None:
    """启动进程级监听单例（lifespan 调用）；建 watch 在后台进行，立即返回。"""
    global _watcher
    _watcher = LibraryWatcher()
    await _watcher.start()


async def close_library_watcher() -> None:
    global _watcher
    if _watcher is not None:
        await _watcher.stop()
        _watcher = None
