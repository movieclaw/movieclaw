"""远程转码 Worker 的控制面注册表。

控制面使用 WebSocket，数据面仍使用普通 HTTPS：

* WebSocket 只发送任务参数、停止命令和状态，不承载媒体数据；
* 源文件由 Worker 通过带签名的 HTTP(S) URL Range 读取；
* HLS 分片由 ffmpeg 直接 HTTP PUT 回 NAS，避免 Worker 产生临时文件。

注册表是进程内状态，和现有播放会话一样要求单进程 uvicorn。Worker 断线后
不会自动恢复旧任务，避免两个 Worker 同时向同一分片写入；播放器下一次请求
会收到失败并走现有降档/重试回路。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket

from movieclaw_api.services.playback.remote_config import (
    RemoteTranscodeRuntimeConfig,
    remote_transcode_issues,
)
from movieclaw_api.services.playback.remote_config import (
    effective_remote_transcode_config as _effective_remote_transcode_config,
)

logger = logging.getLogger("movieclaw_api.playback.remote_worker")

REMOTE_WORKER_PROTOCOL_VERSION = 1
WORKER_IDLE_TIMEOUT_S = 45.0
JOB_ACCEPT_TIMEOUT_S = 8.0
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SUPPORTED_BACKENDS = frozenset({"videotoolbox"})


class RemoteWorkerUnavailable(RuntimeError):
    """没有可接单的远程 Worker。"""


@dataclass(frozen=True)
class WorkerCapabilities:
    """Worker 在 hello 中声明的能力快照。"""

    backends: tuple[str, ...] = ()
    encoders: tuple[str, ...] = ()
    ffmpeg_version: str | None = None
    platform: str | None = None
    max_jobs: int = 1


@dataclass
class WorkerConnection:
    """一个在线 WebSocket Worker 及其正在执行的任务。"""

    worker_id: str
    websocket: WebSocket
    capabilities: WorkerCapabilities
    # Worker 实际连上来的根地址（scheme://host[:port][根路径]），由控制面
    # 握手时从请求本身推断，Worker 不需要上报、用户也不需要填。它天然是
    # 「这台 Worker 一定够得着」的地址——它刚刚就是从那儿连进来的。
    observed_base_url: str = ""
    worker_version: str | None = None
    arch: str | None = None
    connected_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    jobs: set[str] = field(default_factory=set)
    draining: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, payload: dict[str, Any]) -> None:
        """串行发送控制消息，防止并发 stop/start 交叉写 WebSocket。"""
        async with self.send_lock:
            await self.websocket.send_json(payload)


class RemoteWorkerRegistry:
    """进程级 Worker 注册表与任务路由器。"""

    def __init__(self) -> None:
        # 能力探测通过 asyncio.to_thread() 执行，而 Worker 连接在事件循环线程
        # 中注册/注销；这里不能只依赖 asyncio.Lock，否则后台探测会并发遍历
        # 正在变化的 dict。锁只保护短暂的内存台账，不包住任何网络 await。
        self._lock = threading.RLock()
        self._workers: dict[str, WorkerConnection] = {}
        self._job_workers: dict[str, str] = {}
        self._job_attempts: dict[str, str] = {}
        self._job_events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._job_states: dict[str, dict[str, Any]] = {}

    # -- Worker 生命周期 -------------------------------------------------

    async def register(
        self,
        websocket: WebSocket,
        hello: dict[str, Any],
        *,
        observed_base_url: str = "",
    ) -> WorkerConnection:
        """校验 hello 并登记 Worker；同 ID 的旧连接会被替换。"""
        raw_worker_id = hello.get("worker_id")
        if not isinstance(raw_worker_id, str):
            raise ValueError("worker_id 必须是字符串")
        worker_id = raw_worker_id.strip()
        if not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker_id 为空或包含不支持的字符")
        capabilities = self._parse_capabilities(hello.get("capabilities"))
        connection = WorkerConnection(
            worker_id=worker_id,
            websocket=websocket,
            capabilities=capabilities,
            observed_base_url=observed_base_url,
            worker_version=str(hello.get("worker_version"))
            if hello.get("worker_version")
            else None,
            arch=str(hello["capabilities"].get("arch"))
            if isinstance(hello.get("capabilities"), dict)
            and hello["capabilities"].get("arch")
            else None,
            draining=bool(hello.get("draining", False)),
        )
        with self._lock:
            previous = self._workers.get(worker_id)
            if previous is not None:
                logger.warning("远程 Worker %s 重复连接，关闭旧连接", worker_id)
                self._mark_jobs_lost(previous, "远程 Worker 连接已替换")
            self._workers[worker_id] = connection
        if previous is not None:
            await self._close_quietly(previous.websocket, code=1012, reason="连接已替换")
        logger.info(
            "远程转码 Worker 已上线：%s（平台=%s ffmpeg=%s 并发=%d 后端=%s）",
            worker_id,
            capabilities.platform or "未知",
            capabilities.ffmpeg_version or "未知",
            capabilities.max_jobs,
            ",".join(capabilities.backends) or "无",
        )
        return connection

    async def unregister(self, connection: WorkerConnection) -> None:
        """断开 Worker，并让等待中的会话尽快看到失败。"""
        with self._lock:
            current = self._workers.get(connection.worker_id)
            if current is not connection:
                return
            self._workers.pop(connection.worker_id, None)
            lost_jobs = self._mark_jobs_lost(connection, "远程 Worker 已断开连接")
        # 带上被判失败的任务 ID：Worker 崩在半路时，用户先看到的是「播放失败」，
        # 而这一行是把那次失败和这台 Worker 的掉线对上号的唯一凭据。
        logger.warning(
            "远程转码 Worker 已离线：%s，%s",
            connection.worker_id,
            f"进行中的任务已判失败={','.join(lost_jobs)}" if lost_jobs else "无进行中的任务",
        )

    async def shutdown(self) -> None:
        """应用退出时关闭 Worker 连接；任务会先由会话管理器停止。"""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            self._job_workers.clear()
            self._job_attempts.clear()
            self._job_events.clear()
            self._job_states.clear()
        for connection in workers:
            await self._close_quietly(connection.websocket, code=1001, reason="服务端关闭")

    async def disconnect_all(self, reason: str) -> None:
        """配置变更时断开所有 Worker，使旧令牌不再保持有效连接。"""
        with self._lock:
            workers = list(self._workers.values())
            for connection in workers:
                self._mark_jobs_lost(connection, reason)
            self._workers.clear()
            self._job_workers.clear()
            self._job_attempts.clear()
        for connection in workers:
            await self._close_quietly(connection.websocket, code=1012, reason=reason)

    # -- 能力与状态 ------------------------------------------------------

    def has_capable_worker(self, backend: str = "videotoolbox") -> bool:
        """同步查询是否有能执行指定后端的空闲 Worker。"""
        if not remote_worker_enabled():
            return False
        return self._select_worker(backend) is not None

    def worker_online(self, worker_id: str | None) -> bool:
        """会话等待分片时判断它所属的 Worker 是否仍在线。"""
        if not worker_id:
            return False
        with self._lock:
            connection = self._workers.get(worker_id)
            return connection is not None and self._is_fresh(connection)

    def snapshot(self) -> list[dict[str, Any]]:
        """返回不含令牌的状态，供管理员诊断页使用。"""
        with self._lock:
            return [
                {
                    "worker_id": connection.worker_id,
                    "worker_version": connection.worker_version,
                    "arch": connection.arch,
                    "platform": connection.capabilities.platform,
                    "ffmpeg_version": connection.capabilities.ffmpeg_version,
                    "backends": list(connection.capabilities.backends),
                    "max_jobs": connection.capabilities.max_jobs,
                    "active_jobs": len(connection.jobs),
                    "jobs": [
                        {
                            "job_id": job_id,
                            "type": self._job_states.get(job_id, {}).get("type"),
                            "out_time_ms": self._job_states.get(job_id, {}).get(
                                "out_time_ms"
                            ),
                            "speed": self._job_states.get(job_id, {}).get("speed"),
                            "phase": self._job_states.get(job_id, {}).get("phase"),
                        }
                        for job_id in sorted(connection.jobs)
                    ],
                    "draining": connection.draining,
                    "last_seen_seconds": max(0.0, time.monotonic() - connection.last_seen),
                    "online": self._is_fresh(connection),
                }
                for connection in self._workers.values()
            ]

    # -- 任务控制 --------------------------------------------------------

    def create_job_waiter(self, job_id: str) -> None:
        """创建一次性任务事件队列；重复调用会丢弃旧队列。"""
        with self._lock:
            self._job_events[job_id] = asyncio.Queue(maxsize=8)
            self._job_states[job_id] = {"type": "job.pending"}

    def remove_job_waiter(self, job_id: str) -> None:
        with self._lock:
            self._job_events.pop(job_id, None)

    def remove_job(self, job_id: str) -> None:
        """彻底移除已结束会话的状态，避免运行很久后字典持续增长。"""
        with self._lock:
            self._job_events.pop(job_id, None)
            self._job_states.pop(job_id, None)
        self._release_job(job_id)

    def job_state(self, job_id: str) -> dict[str, Any] | None:
        """取任务最近状态，供分片等待循环快速失败。"""
        with self._lock:
            return self._job_states.get(job_id)

    async def wait_job_event(
        self, job_id: str, *, timeout: float = JOB_ACCEPT_TIMEOUT_S
    ) -> dict[str, Any]:
        """等待 Worker 回应 accepted/failed。"""
        with self._lock:
            queue = self._job_events.get(job_id)
        if queue is None:
            raise RemoteWorkerUnavailable("远程任务等待器不存在")
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError as exc:
            raise RemoteWorkerUnavailable("远程 Worker 接单超时") from exc

    def reserve(
        self,
        job_id: str,
        *,
        backend: str,
        attempt_id: str | None = None,
    ) -> WorkerConnection:
        """选一个空闲 Worker 并占住槽位，返回它的连接。

        为什么先占位再下发、而不是一个 ``dispatch`` 打包做完：任务里的源地址
        和产物上传地址要用**这台** Worker 连上来的地址拼（``observed_base_url``），
        所以必须先确定是谁接单。占位和选人在同一把锁里完成，不会出现「按 A 的
        地址拼 URL，任务却发给了 B」。占位后如果拼装或发送失败，调用方必须调
        ``release_job``（``start_job`` 失败时会自己调）。
        """
        with self._lock:
            if job_id in self._job_workers:
                raise RemoteWorkerUnavailable("远程任务已存在")
            connection = self._select_worker(backend)
            if connection is None:
                raise RemoteWorkerUnavailable("没有在线且空闲的 Apple VideoToolbox Worker")
            self._job_workers[job_id] = connection.worker_id
            self._job_attempts[job_id] = (
                attempt_id if isinstance(attempt_id, str) and attempt_id else job_id
            )
            connection.jobs.add(job_id)
        return connection

    def release_job(self, job_id: str) -> None:
        """占位之后拼装失败时归还槽位。"""
        self._release_job(job_id)

    async def start_job(
        self,
        connection: WorkerConnection,
        job_id: str,
        payload: dict[str, Any],
    ) -> str:
        """向已占位的 Worker 下发任务；发送失败自动归还槽位。"""
        try:
            await connection.send({"type": "job.start", "job_id": job_id, **payload})
        except Exception as exc:  # noqa: BLE001
            self._release_job(job_id)
            logger.warning("下发远程任务失败：worker=%s job=%s", connection.worker_id, job_id)
            raise RemoteWorkerUnavailable("无法向远程 Worker 下发任务") from exc
        return connection.worker_id

    async def cancel(self, job_id: str, *, force: bool = False) -> None:
        """通知 Worker 停止任务；消息发送失败时仍释放本地占用。

        seek 重启时旧分片已经没有交付价值，``force`` 让新版 Worker 直接杀掉
        ffmpeg，避免它和新轮次短暂并行读源、写上传；不传时保持普通停止的优雅
        退出。字段是可选的，旧版 Worker 会按原有优雅停止处理。
        """
        with self._lock:
            worker_id = self._job_workers.get(job_id)
            connection = self._workers.get(worker_id) if worker_id else None
        if connection is not None:
            try:
                message = {"type": "job.stop", "job_id": job_id}
                if force:
                    message["force"] = True
                await connection.send(message)
            except Exception:  # noqa: BLE001
                logger.info("远程 Worker 停止消息发送失败，按断线处理：job=%s", job_id)
        self._release_job(job_id)

    async def pause(self, job_id: str) -> bool:
        """暂停远程 ffmpeg，但保留任务映射，供磁盘低水位保护使用。"""
        return await self._send_job_control(job_id, "job.pause")

    async def resume(self, job_id: str) -> bool:
        """恢复被磁盘低水位暂停的远程 ffmpeg。"""
        return await self._send_job_control(job_id, "job.resume")

    async def _send_job_control(self, job_id: str, message_type: str) -> bool:
        """向当前任务所属 Worker 发送控制消息，不释放任务槽位。"""
        with self._lock:
            worker_id = self._job_workers.get(job_id)
            connection = self._workers.get(worker_id) if worker_id else None
            if connection is None or not self._is_fresh(connection):
                return False
        try:
            await connection.send({"type": message_type, "job_id": job_id})
        except Exception:  # noqa: BLE001
            logger.info(
                "远程 Worker 控制消息发送失败：type=%s job=%s",
                message_type,
                job_id,
            )
            return False
        return True

    def publish_job_event(self, job_id: str, message: dict[str, Any]) -> None:
        """把 Worker 的状态消息交给正在启动/重启的会话。"""
        with self._lock:
            self._job_states[job_id] = message
            queue = self._job_events.get(job_id)
            # 进度是诊断状态，不应占用启动阶段的 accepted/failed 等待队列；
            # 否则首个 progress 恰好先到时，会被会话误判为接单失败。
            if queue is None or message.get("type") == "job.progress":
                return
            if queue.full():
                # 队列只用于 accepted/failed 等少量控制消息；丢掉最旧事件避免
                # 一个异常 Worker 堵住整个事件循环。
                queue.get_nowait()
            queue.put_nowait(message)

    async def handle_message(
        self, connection: WorkerConnection, message: dict[str, Any]
    ) -> None:
        """处理 Worker 上行消息。未知消息只记日志，不中断连接。"""
        with self._lock:
            connection.last_seen = time.monotonic()
        message_type = str(message.get("type", ""))
        job_id = str(message.get("job_id", ""))
        if message_type == "worker.heartbeat":
            await connection.send({"type": "worker.heartbeat.ack"})
            return
        if message_type == "worker.draining":
            with self._lock:
                connection.draining = True
            logger.info("远程转码 Worker 进入排空状态：%s", connection.worker_id)
            return
        if message_type == "worker.ready":
            with self._lock:
                connection.draining = False
            logger.info("远程转码 Worker 恢复接单：%s", connection.worker_id)
            return
        if message_type == "worker.goodbye":
            logger.info("远程转码 Worker 主动断开：%s", connection.worker_id)
            return
        if (
            message_type in {"job.accepted", "job.progress", "job.failed", "job.finished"}
            and job_id
        ):
            # 任务状态只能由实际被选中的 Worker 上报；共享 Worker 令牌下，
            # 未做这层校验的另一台 Worker 可以伪造别人的完成/失败消息。
            with self._lock:
                is_owner = self._job_workers.get(job_id) == connection.worker_id
                expected_attempt = self._job_attempts.get(job_id, job_id)
                message_attempt = message.get("attempt_id", expected_attempt)
                is_current_attempt = message_attempt == expected_attempt
            if not is_owner or not is_current_attempt:
                return
            self.publish_job_event(job_id, message)
            if message_type in {"job.failed", "job.finished"}:
                self._release_job(job_id)
            return
        logger.debug(
            "忽略远程 Worker 未知消息：worker=%s type=%s",
            connection.worker_id,
            message_type,
        )

    # -- 内部 ------------------------------------------------------------

    def _select_worker(self, backend: str) -> WorkerConnection | None:
        with self._lock:
            candidates = [
                connection
                for connection in self._workers.values()
                if backend in connection.capabilities.backends
                and len(connection.jobs) < connection.capabilities.max_jobs
                and not connection.draining
                and self._is_fresh(connection)
            ]
            if not candidates:
                return None
            return min(candidates, key=lambda item: (len(item.jobs), item.connected_at))

    def _release_job(self, job_id: str) -> None:
        with self._lock:
            worker_id = self._job_workers.pop(job_id, None)
            self._job_attempts.pop(job_id, None)
            if worker_id:
                connection = self._workers.get(worker_id)
                if connection is not None:
                    connection.jobs.discard(job_id)

    def _mark_jobs_lost(self, connection: WorkerConnection, error: str) -> list[str]:
        """把某条连接上的任务统一标为失败，并释放 Worker 槽位。"""
        with self._lock:
            lost_jobs = [
                job_id
                for job_id, worker_id in self._job_workers.items()
                if worker_id == connection.worker_id
            ]
            for job_id in lost_jobs:
                self._job_workers.pop(job_id, None)
                self._job_attempts.pop(job_id, None)
                connection.jobs.discard(job_id)
                self.publish_job_event(
                    job_id,
                    {"type": "job.failed", "job_id": job_id, "error": error},
                )
        return lost_jobs

    @staticmethod
    def _is_fresh(connection: WorkerConnection) -> bool:
        return time.monotonic() - connection.last_seen <= WORKER_IDLE_TIMEOUT_S

    @staticmethod
    def _parse_capabilities(raw: Any) -> WorkerCapabilities:
        raw = raw if isinstance(raw, dict) else {}
        backends = tuple(
            item
            for item in raw.get("backends", [])
            if isinstance(item, str) and item in _SUPPORTED_BACKENDS
        )
        encoders = tuple(
            item for item in raw.get("encoders", []) if isinstance(item, str)
        )
        if "videotoolbox" in backends and "h264_videotoolbox" not in encoders:
            backends = tuple(item for item in backends if item != "videotoolbox")
        try:
            max_jobs = max(1, min(4, int(raw.get("max_jobs", 1))))
        except (TypeError, ValueError):
            max_jobs = 1
        return WorkerCapabilities(
            backends=backends,
            encoders=encoders,
            ffmpeg_version=str(raw.get("ffmpeg_version"))
            if raw.get("ffmpeg_version")
            else None,
            platform=str(raw.get("platform")) if raw.get("platform") else None,
            max_jobs=max_jobs,
        )

    @staticmethod
    async def _close_quietly(websocket: WebSocket, *, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=reason)


_registry = RemoteWorkerRegistry()


def get_remote_worker_registry() -> RemoteWorkerRegistry:
    """取进程级远程 Worker 注册表。"""
    return _registry


def reset_remote_worker_registry() -> None:
    """测试用：清空同步状态；调用方负责先关闭实际 WebSocket。"""
    with _registry._lock:
        _registry._workers.clear()
        _registry._job_workers.clear()
        _registry._job_attempts.clear()
        _registry._job_events.clear()
        _registry._job_states.clear()


def remote_worker_enabled() -> bool:
    """开关打开、且（若填了）覆盖地址合法时，远程硬件才对决策层可见。

    地址本身不是前置条件：默认取自 Worker 连上来的地址（observed_base_url）。
    """
    config = effective_remote_transcode_config()
    return config.enabled and not remote_transcode_issues(config)


def effective_remote_transcode_config() -> RemoteTranscodeRuntimeConfig:
    """供同步调用方读取网页配置的运行时快照。"""
    return _effective_remote_transcode_config()


def remote_worker_available(backend: str = "videotoolbox") -> bool:
    """供播放决策/执行层同步查询远程硬件是否在线且有空闲槽位。"""
    return _registry.has_capable_worker(backend)
