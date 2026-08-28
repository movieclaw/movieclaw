"""转码会话管理与进程契约（docs/design/web-player.md §4）。

一个会话 = 一个 ffmpeg 进程 + 一个分片目录 + 一条心跳。会话状态放内存：
生产是**单进程 uvicorn**（``main.py`` 自持 ``uvicorn.Config``，无 workers），
不需要持久化、不需要跨进程同步；进程一走会话本就该消失。

五条进程契约（§4.2，一条都不能省）
--------------------------------
1. **必须 asyncio 子进程**。单进程 async 服务里一个阻塞调用会卡死全部用户的
   搜索、订阅、扫描。
2. **必须 start_new_session=True**，让 ffmpeg 起在独立进程组。
3. **停止时 killpg 整组**，SIGTERM 后给 3 秒再 SIGKILL。``entrypoint.sh`` 的
   ``trap shutdown`` 只 kill API 进程，**不会连坐孙子进程**；而设置页重启
   （退出码 42）与应用内更新都会重启后端。不做这条，每次重启都会留下满负荷
   烧 GPU、持续写盘的孤儿 ffmpeg。
4. **stderr 必须持续读取**。管道写满会阻塞进程本身——表现为转码莫名卡死。
5. **启动时清残留**。不能假设上次是干净退出的（与「台账自愈」同思路）。

与文档的一处偏离
----------------
§4.5 写的是并发超限「排队并明确告知」。实现改为**立即拒绝并告知当前占用**：
在 HTTP 请求里排队会让用户对着转圈等一个不知道多久的位置，不如直接说
「2/2 已满」让他停掉别的播放——这是更诚实的交互。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.playback.ffmpeg_args import (
    LIVE_PLAYLIST_NAME,
    PLAYLIST_NAME,
    SEGMENT_PATTERN,
    TranscodeCommand,
    build_hls_command,
)
from movieclaw_api.services.playback.remote_signing import issue_remote_grant
from movieclaw_api.services.playback.remote_worker import (
    RemoteWorkerUnavailable,
    get_remote_worker_registry,
)
from movieclaw_events import new_ulid
from movieclaw_playback.decide import PlaybackPlan, PlaybackTier
from movieclaw_playback.hls_vod import SegmentPlan

logger = logging.getLogger("movieclaw_api.playback.session")

#: 会话空闲回收窗口。用户关页面不会发任何信号，超时回收是唯一可靠兜底。
#:
#: 取三分钟而不是「心跳间隔（15 秒）的几倍」：**浏览器不保证心跳按时发得
#: 出来**。页面切到后台后定时器会被节流到一分钟一次，挂久了整页冻结，此时
#: 一个只是暂停着等用户回来的会话会被判成「没人要了」而回收，用户切回来看
#: 到的是永远转不完的圈。三分钟能盖住绝大多数「切个标签页回来接着看」，
#: 代价是页面被强杀（崩溃/断网）时多留两分钟的 ffmpeg——正常关闭走的是
#: pagehide 里的显式 DELETE，不受这个窗口影响。
#:
#: 网页播放器另有兜底：心跳收到 404 会带着当前位置原地重开会话
#: （components/player/video-player.tsx 的「掉线自愈」）。
SESSION_IDLE_TIMEOUT_S = 180.0
#: 回收巡检间隔。
REAP_INTERVAL_S = 15.0
#: 等 playlist 出现的上限。playlist 先行——会话起来后 ffmpeg 立刻写出
#: index.m3u8，不等分片；超过这个时间还没有，基本是命令本身有问题。
PLAYLIST_WAIT_TIMEOUT_S = 30.0
#: VOD 模式的快速失败窗口：客户端列表由服务端按 segment_plan 生成，根本不
#: 依赖 ffmpeg 的 live.m3u8——起播路径上等它出现是白等（实测 0.3~0.8 秒）。
#: spawn 后只守这么一小段，专抓「命令本身有错、进程秒退」（参数错/文件不
#: 存在通常 100~300ms 内退出），能给用户一句带 stderr 的明确报错；窗口过
#: 后进程还活着就放行，更晚的死亡由 ensure_segment 的 process-dead 分支
#: 兜住（表现为分片 404 → 前端降档回路）。
VOD_FAST_FAIL_WINDOW_S = 0.3
#: 盘上至少要留的余量。低于它一律拒绝新会话——转码分片与 SQLite 同卷，
#: 盘满会让数据库写不进去，整个应用不可用。
MIN_FREE_BYTES = 2 * 1024**3
#: 低水位急停后，剩余空间回到这个线以上才恢复被暂停的转码。两档水位拉开
#: 距离是为了不在临界值附近反复停/走（迟滞回差）。
RESUME_FREE_BYTES = 2 * MIN_FREE_BYTES
#: stderr 保留的行数，供诊断面板与日志使用。
_STDERR_KEEP_LINES = 40


def _segment_index_from_name(name: str) -> int | None:
    """从安全的分片文件名取编号；其它 HLS 产物返回 None。"""
    if not (name.startswith("seg") and name.endswith(".m4s")):
        return None
    number = name[3:-4]
    if len(number) != 5 or not number.isdigit():
        return None
    return int(number)


def _remote_job_failure_message(job_state: dict[str, Any]) -> str:
    """把 Worker 终态消息拼成可读的会话错误，保留实际 ffmpeg 线索。"""
    error = job_state.get("error")
    message = (
        error.strip()
        if isinstance(error, str) and error.strip()
        else "远程 Worker 未产出请求的分片"
    )
    exit_code = job_state.get("exit_code")
    if (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and "退出码" not in message
    ):
        message += f"；ffmpeg 退出码：{exit_code}"
    stderr_tail = job_state.get("stderr_tail")
    if isinstance(stderr_tail, str) and stderr_tail.strip():
        message += f"；ffmpeg stderr：{stderr_tail.strip()[-1000:]}"
    return message


class SessionLimitError(RuntimeError):
    """并发已满。message 面向用户，中文。"""


class DiskQuotaError(RuntimeError):
    """磁盘配额不足。message 面向用户，中文。"""


class SessionStartError(RuntimeError):
    """会话启动失败（ffmpeg 起不来或迟迟不产出 playlist）。"""


@dataclass(frozen=True)
class RemoteArtifactUpload:
    """一次远程 HLS 产物上传的脱敏结果，供播放诊断使用。"""

    name: str
    status: int
    received_bytes: int
    content_length: int | None
    transfer_encoding: str | None
    occurred_at_ms: int


@dataclass
class TranscodeSession:
    """一个在跑的转码/直通会话。"""

    id: str
    file_id: int
    member_id: int
    tier: PlaybackTier
    directory: Path
    start_ms: int
    plan: PlaybackPlan
    process: asyncio.subprocess.Process | None = None
    state: str = "spawning"  # spawning | ready | failed | stopped
    last_ping: float = field(default_factory=time.monotonic)
    created_at: float = field(default_factory=time.monotonic)
    error: str | None = None
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_KEEP_LINES))
    #: 被磁盘低水位哨兵 SIGSTOP 挂起中。挂起的进程不响应 SIGTERM，
    #: 终止前必须先 SIGCONT（见 ``_terminate``）。
    disk_paused: bool = False
    #: VOD 模式（§12）：非 None 表示播放列表由服务端按关键帧表预生成，
    #: seek 由分片请求驱动（ensure_segment），ffmpeg 可在会话内多次重启。
    segment_plan: SegmentPlan | None = None
    #: 本轮 ffmpeg 的 -start_number：它正从这个分片号往后转
    head_segment: int = 0
    #: 跨轮次累计的已完成分片号。分片文件在会话目录里从不删除，重启只是换
    #: ffmpeg 的起点——没有这份台账，seek 回看过的区间会把「文件还在、但
    #: 不在当前轮次 live.m3u8 里」的分片误判未就绪，白白再重启一轮。
    completed_segments: set[int] = field(default_factory=set)
    #: 重启 ffmpeg 需要的原始参数（VOD 模式）
    source_path: str = ""
    hw_backend: str | None = None
    #: 外置 Worker 会话不持有本地 PID，但仍把分片落在 NAS 的会话目录中；
    #: 这样现有 VOD playlist、Range 取流、配额和清理逻辑都能继续复用。
    remote: bool = False
    #: 当前 job 所属的 Worker；断线时用它快速判定远程分片等待应失败。
    remote_worker_id: str | None = None
    #: 当前这一轮下发给 Worker 的 job id；与 session id 分离，避免 seek 重启时
    #: 旧 ffmpeg 的迟到 finished 消息误伤新一轮任务。
    remote_job_id: str | None = None
    #: 远程 seek 重启的控制面切换窗口。此时旧 job 已取消、新 Worker 尚未写回，
    #: 分片等待不能把临时的 ``worker_id=None`` 当成 Worker 断线。
    remote_restarting: bool = False
    remote_source_url: str = ""
    remote_artifact_base_url: str = ""
    remote_artifact_suffix: str = ""
    #: 最近的远程产物上传记录。只保留文件名、状态和字节数，不保留签名 URL。
    remote_uploads: deque[RemoteArtifactUpload] = field(
        default_factory=lambda: deque(maxlen=24)
    )
    #: 上传中断的分片号。后续请求再次需要它时，重启远程任务补这一段。
    remote_failed_segments: set[int] = field(default_factory=set)
    #: 最近一次分片供给请求，用于把「卡在哪里」直接显示给用户。
    last_requested_segment: int | None = None
    last_requested_at_ms: int | None = None
    last_served_segment: int | None = None
    last_served_at_ms: int | None = None
    last_segment_wait_ms: int | None = None
    last_segment_status: int | None = None
    #: 上次解析 live.m3u8 时的 (mtime_ns, size)。一次 seek 会有好几个并发的
    #: 分片请求各自轮询就绪，不做门控的话每个请求每 50ms 都把两小时片近两千
    #: 行的列表全量重解析一遍——文件没变就跳过，解析只在 ffmpeg 真写了新
    #: 内容时发生一次。
    _playlist_sig: tuple[int, int] | None = None
    #: 远程 Worker 产物目录的 mtime。上传端点用 os.replace 原子落盘，每次新产物
    #: 都会更新目录 mtime；用它门控扫描，避免每个等待请求每 50ms glob 整个目录。
    _remote_artifact_dir_mtime_ns: int | None = None
    #: 正在 ensure_segment 里等待的分片号 → 并发请求数。重启判定必须看
    #: **全体**在等的请求而不是只看自己：iOS 的 AVPlayer 会并行请求相邻的
    #: 几个分片，逐个自查会互相杀（详见 _maybe_restart_for 的注释）。
    pending_segments: dict[int, int] = field(default_factory=dict)
    #: 分片号 → 首个等待者挂号的时刻（monotonic）。落后于转码头的请求要
    #: 熬过宽限期才有资格触发重启——见 _maybe_restart_for 的探测宽限注释。
    pending_since: dict[int, float] = field(default_factory=dict)
    #: VOD 每次 seek 重启递增。旧播放器请求即使没有及时取消，也不能在新轮次
    #: 里继续占住等待窗口或再次触发一次反向重启。
    restart_generation: int = 0
    #: 最近一轮真正下发的目标分片；同一目标的并行请求可以继续等，其余旧请求
    #: 在看到代次变化后立即结束。
    restart_target: int | None = None
    #: 首个分片已供出（供起播计时打点用，只打一次）
    first_segment_served: bool = False
    _stderr_task: asyncio.Task | None = None
    _restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def playlist_path(self) -> Path:
        """ffmpeg 写的列表：VOD 模式下是内部进度追踪（live.m3u8），客户端
        拿到的 index.m3u8 由服务端按 segment_plan 生成。"""
        if self.segment_plan is not None:
            return self.directory / LIVE_PLAYLIST_NAME
        return self.directory / PLAYLIST_NAME

    @property
    def is_transcoding(self) -> bool:
        """档 3/4 吃 CPU/GPU，档 1/2 吃 IO——两者并发上限必须分开算。"""
        return self.tier >= PlaybackTier.HARDWARE_TRANSCODE

    def touch(self) -> None:
        self.last_ping = time.monotonic()

    def record_remote_upload(
        self,
        name: str,
        *,
        status: int,
        received_bytes: int,
        content_length: int | None,
        transfer_encoding: str | None,
    ) -> None:
        """记录远程产物结果，并把失败的分片加入待补片台账。"""
        self.remote_uploads.append(
            RemoteArtifactUpload(
                name=name,
                status=status,
                received_bytes=received_bytes,
                content_length=content_length,
                transfer_encoding=transfer_encoding,
                occurred_at_ms=int(time.time() * 1000),
            )
        )
        index = _segment_index_from_name(name)
        if index is None:
            return
        if 200 <= status < 300:
            self.remote_failed_segments.discard(index)
        elif status >= 400:
            self.remote_failed_segments.add(index)

    def size_bytes(self) -> int:
        if not self.directory.exists():
            return 0
        return sum(f.stat().st_size for f in self.directory.rglob("*") if f.is_file())


class TranscodeSessionManager:
    """全局会话表。单进程内唯一实例（``get_session_manager()``）。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or Path(get_settings().transcode_dir)
        self._sessions: dict[str, TranscodeSession] = {}
        self._reaper: asyncio.Task | None = None

    @property
    def cache_root(self) -> Path:
        """分片缓存根目录。开会话前按它的剩余空间推导配额（limits.py）。"""
        return self._root

    # -- 生命周期 ---------------------------------------------------------

    def cleanup_orphans(self) -> int:
        """删掉根目录下的全部残留会话目录，返回清理数量。

        会话状态只在内存里，所以启动时目录里的任何东西都是上次退出留下的垃圾
        （契约 5）。**不能假设上次是干净退出的**。
        """
        if not self._root.exists():
            return 0
        removed = 0
        for child in self._root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("清理了 %d 个上次退出遗留的转码目录", removed)
        return removed

    def start_reaper(self) -> None:
        """启动心跳巡检。应用 lifespan 里调一次。"""
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        """停掉全部会话与巡检任务。后端退出前必须走到这里（契约 3）。"""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for session_id in list(self._sessions):
            await self.stop(session_id)

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(REAP_INTERVAL_S)
                await self.reap()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 巡检不能因单次异常停摆
                logger.exception("转码会话巡检异常")

    async def reap(self) -> int:
        """回收超时无心跳的会话，返回回收数量。顺带跑一遍磁盘水位哨兵。"""
        await self._enforce_disk_watermark()
        now = time.monotonic()
        stale = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_ping > SESSION_IDLE_TIMEOUT_S
        ]
        for sid in stale:
            logger.info("会话 %s 超过 %.0f 秒无心跳，回收", sid, SESSION_IDLE_TIMEOUT_S)
            await self.stop(sid)
        return len(stale)

    # -- 会话操作 ---------------------------------------------------------

    async def start(
        self,
        plan: PlaybackPlan,
        *,
        source_path: str,
        member_id: int,
        start_ms: int = 0,
        hw_backend: str | None = None,
        max_transcode: int = 2,
        max_remux: int = 4,
        quota_bytes: int | None = None,
        segment_plan: SegmentPlan | None = None,
        use_remote: bool = False,
        remote_base_url: str | None = None,
    ) -> TranscodeSession:
        """起一个会话。playlist 出现即返回，不等全部分片转完。

        ``segment_plan`` 非 None 走 VOD 模式（§12）：客户端列表由服务端按
        它生成，ffmpeg 从 ``start_ms`` 所在的分片边界起转、编号接上全片
        规划；seek 由分片请求驱动（``ensure_segment``），会话内可重启。
        """
        if plan.tier is PlaybackTier.DIRECT_PLAY:
            raise ValueError("档 0 是原文件直出，不需要会话")

        await self.reap()  # 顺手清掉超时的，给新会话腾位置
        transcoding = plan.tier >= PlaybackTier.HARDWARE_TRANSCODE
        self._check_capacity(transcoding, max_transcode, max_remux)
        self._check_disk(quota_bytes)

        session_id = new_ulid()
        head_segment = 0
        if segment_plan is not None:
            head_segment = segment_plan.segment_for(start_ms / 1000)
            # 起播点对齐到分片边界：VOD 列表的时间轴是文件绝对时间，客户端
            # 想到哪就 seek 到哪，服务端只按边界供片
            start_ms = int(segment_plan.boundaries[head_segment] * 1000)
        session = TranscodeSession(
            id=session_id,
            file_id=plan.file_id,
            member_id=member_id,
            tier=plan.tier,
            # 目录名就用会话 id：排查问题时看一眼盘上的目录就知道是哪个会话
            directory=self._root / session_id,
            start_ms=start_ms,
            plan=plan,
            segment_plan=segment_plan,
            head_segment=head_segment,
            source_path=source_path,
            hw_backend=hw_backend,
            remote=use_remote,
        )
        session.directory.mkdir(parents=True, exist_ok=True)
        self._sessions[session.id] = session
        try:
            if use_remote:
                if not remote_base_url:
                    raise SessionStartError("远程转码地址未配置")
                # 远程 Worker 在首个分片前不可用时，当前会话必须失败。远程硬件
                # 计划可能只因远程能力才越过了软件转码同意门槛，不能在这里绕过
                # 决策层偷偷启动 libx264；播放器下一次请求会带 failed_tiers，
                # 再由统一降档逻辑决定是否展示 consent 或使用本地软转。
                await self._spawn_remote(session, remote_base_url)
            else:
                command = build_hls_command(
                    plan,
                    source_path=source_path,
                    session_dir=session.directory,
                    start_ms=start_ms,
                    hw_backend=hw_backend,
                    start_number=head_segment if segment_plan is not None else None,
                )
                await self._spawn(session, command)
        except BaseException:
            # 捕 BaseException 而不只是 Exception：客户端在起播途中断开连接
            # （关页面/切集）时，uvicorn 会**取消**本请求协程，抛出来的是
            # CancelledError（BaseException）。只捕 Exception 的话会话留在
            # 表里、ffmpeg 继续空转到超时回收——「关了播放 ffmpeg 还在跑」
            # 的一条服务端来路。清理后原样重抛，取消语义不变。
            with contextlib.suppress(Exception):
                await self.stop(session.id)
            raise
        return session

    async def _spawn_remote(self, session: TranscodeSession, base_url: str) -> None:
        """向远程 Worker 下发一次 ffmpeg 任务，不在 Worker 创建媒体临时文件。"""
        registry = get_remote_worker_registry()
        base = base_url.rstrip("/")
        job_id = new_ulid()
        source_token = await issue_remote_grant(
            session_id=session.id, file_id=session.file_id, kind="source"
        )
        artifact_token = await issue_remote_grant(
            session_id=session.id,
            file_id=session.file_id,
            kind="artifact",
            attempt_id=job_id,
        )
        token_suffix = f"?token={quote(artifact_token, safe='')}"
        source_url = (
            f"{base}/api/v1/transcode-worker/sessions/{session.id}/source"
            f"?token={quote(source_token, safe='')}"
        )
        artifact_base = (
            f"{base}/api/v1/transcode-worker/sessions/{session.id}/artifacts"
        )
        command = build_hls_command(
            session.plan,
            source_path=source_url,
            session_dir=session.directory,
            start_ms=session.start_ms,
            hw_backend=session.hw_backend,
            start_number=session.head_segment if session.segment_plan is not None else None,
            output_base_url=artifact_base,
            output_url_suffix=token_suffix,
        )
        session.remote_source_url = source_url
        session.remote_artifact_base_url = artifact_base
        session.remote_artifact_suffix = token_suffix
        session.remote_job_id = job_id
        registry.create_job_waiter(job_id)
        try:
            worker_id = await registry.dispatch(
                job_id,
                {
                    "file_id": session.file_id,
                    "attempt_id": job_id,
                    "start_ms": session.start_ms,
                    "ffmpeg_args": command.argv[1:],
                },
                backend=session.hw_backend or "videotoolbox",
            )
            session.remote_worker_id = worker_id
            event = await registry.wait_job_event(job_id)
            if event.get("type") != "job.accepted":
                error = str(event.get("error") or "远程 Worker 拒绝任务")
                raise SessionStartError(error)
            if session.segment_plan is None:
                await self._wait_for_remote_playlist(session, job_id)
            session.state = "ready"
            session.touch()
            logger.info(
                "远程转码会话已接单：session=%s worker=%s backend=%s",
                session.id,
                worker_id,
                session.hw_backend or "videotoolbox",
            )
        except RemoteWorkerUnavailable as exc:
            session.error = str(exc)
            raise SessionStartError(f"远程转码不可用：{exc}") from exc
        finally:
            registry.remove_job_waiter(job_id)

    async def _wait_for_remote_playlist(
        self, session: TranscodeSession, job_id: str
    ) -> None:
        """非 VOD 会话等待 Worker 上传 playlist，避免客户端首个请求竞态 404。"""
        deadline = time.monotonic() + PLAYLIST_WAIT_TIMEOUT_S
        registry = get_remote_worker_registry()
        while time.monotonic() < deadline:
            if session.state == "stopped":
                raise SessionStartError("远程转码会话已结束")
            if session.playlist_path.exists():
                return
            state = registry.job_state(job_id)
            if state and state.get("type") in {"job.failed", "job.finished"}:
                session.error = _remote_job_failure_message(state)
                raise SessionStartError(session.error)
            if not registry.worker_online(session.remote_worker_id):
                session.error = "远程 Worker 已断开连接"
                raise SessionStartError(session.error)
            await asyncio.sleep(0.05)
        session.error = "远程 Worker 超时未上传播放列表"
        raise SessionStartError(session.error)

    def _check_capacity(self, transcoding: bool, max_transcode: int, max_remux: int) -> None:
        """两个独立信号量：转码按 CPU/GPU 算，直通按 IO 算。

        机械盘上两路 4K remux 就能打满随机读，而 CPU 几乎是空的——瓶颈不在
        算力，合成一个上限会误判。
        """
        active = [s for s in self._sessions.values() if s.state in ("spawning", "ready")]
        if transcoding:
            used = sum(1 for s in active if s.is_transcoding)
            if used >= max_transcode:
                raise SessionLimitError(
                    f"当前转码会话已满（{used}/{max_transcode}），"
                    "请稍候或停止其它正在转码的播放。"
                )
        else:
            used = sum(1 for s in active if not s.is_transcoding)
            if used >= max_remux:
                raise SessionLimitError(
                    f"当前直通播放已达上限（{used}/{max_remux}），请稍候或停止其它播放。"
                )

    def _check_disk(self, quota_bytes: int | None) -> None:
        """写入前先查盘（§4.6）。**不要指望 LRU 跑得比写入快。**"""
        self._root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self._root).free
        if free < MIN_FREE_BYTES:
            raise DiskQuotaError(
                f"磁盘剩余空间不足（{free / 1024**3:.1f} GB），已拒绝新的转码会话。"
                "转码缓存与数据库同在 data 目录，写满会导致整个应用不可用。"
                "请清理磁盘后重试。"
            )
        if quota_bytes is not None and self.usage_bytes() >= quota_bytes:
            raise DiskQuotaError(
                f"转码缓存已达配额上限（{quota_bytes / 1024**3:.1f} GB，"
                "按磁盘剩余空间自动设定）。请稍候——正在播放的会话结束后会"
                "自动清理，清理磁盘腾出空间也会自动调高配额。"
            )

    async def _enforce_disk_watermark(self) -> None:
        """磁盘低水位急停：空间见底时 SIGSTOP 所有在写的会话，回升后放行。

        开会话时的 ``_check_disk`` 只拦得住**新**会话；已经在跑的 ffmpeg 即使
        有 readrate 限速也仍在持续写盘，放任不管的结局是写满整卷、SQLite
        直接不可用（2026-08-23 真实发生过，一晚 200 GB）。SIGSTOP/SIGCONT
        对整个进程组生效，比杀掉重起便宜得多——空间恢复后播放无感续走。
        """
        registry = get_remote_worker_registry()
        writing = [
            s
            for s in self._sessions.values()
            if (
                s.process is not None
                and s.process.returncode is None
            )
            or (
                s.remote
                and s.remote_job_id is not None
                and (
                    registry.job_state(s.remote_job_id) is None
                    or registry.job_state(s.remote_job_id).get("type")
                    not in {"job.failed", "job.finished"}
                )
            )
        ]
        if not any(writing):
            return
        try:
            free = shutil.disk_usage(self._root).free
        except OSError:
            return
        if free < MIN_FREE_BYTES:
            for session in writing:
                if session.disk_paused:
                    continue
                paused = (
                    await registry.pause(session.remote_job_id)
                    if session.remote and session.remote_job_id is not None
                    else self._signal_group(session, signal.SIGSTOP)
                )
                if paused:
                    session.disk_paused = True
                    logger.warning(
                        "磁盘剩余 %.1f GB 已低于安全水位，暂停会话 %s 的转码写入（%s）",
                        free / 1024**3,
                        session.id,
                        "远程 Worker" if session.remote else "本地 ffmpeg",
                    )
        elif free >= RESUME_FREE_BYTES:
            for session in writing:
                if not session.disk_paused:
                    continue
                resumed = (
                    await registry.resume(session.remote_job_id)
                    if session.remote and session.remote_job_id is not None
                    else self._signal_group(session, signal.SIGCONT)
                )
                if resumed:
                    session.disk_paused = False
                    logger.info(
                        "磁盘空间已恢复（剩余 %.1f GB），继续会话 %s 的转码（%s）",
                        free / 1024**3,
                        session.id,
                        "远程 Worker" if session.remote else "本地 ffmpeg",
                    )

    def _signal_group(self, session: TranscodeSession, sig: signal.Signals) -> bool:
        """给会话的整个进程组发信号。进程已不在时返回 False。"""
        process = session.process
        if process is None or process.returncode is not None:
            return False
        try:
            os.killpg(os.getpgid(process.pid), sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def _spawn(self, session: TranscodeSession, command: TranscodeCommand) -> None:
        # 契约 1+2：异步子进程 + 独立进程组
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        session.process = process
        # 契约 4：stderr 持续读取，否则管道写满会把 ffmpeg 卡死
        session._stderr_task = asyncio.create_task(self._drain_stderr(session))

        try:
            await self._wait_for_playlist(session)
        except SessionStartError:
            session.state = "failed"
            raise
        session.state = "ready"
        session.touch()

    async def _drain_stderr(self, session: TranscodeSession) -> None:
        assert session.process is not None and session.process.stderr is not None
        try:
            async for raw in session.process.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    session.stderr_tail.append(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("会话 %s 的 stderr 读取中断", session.id, exc_info=True)

    async def _wait_for_playlist(self, session: TranscodeSession) -> None:
        """等 index.m3u8 出现。ffmpeg 写出 playlist 就算会话可用——
        分片按需生成，客户端边拉边转（首帧延迟的关键）。

        VOD 模式（segment_plan 非 None）只守 ``VOD_FAST_FAIL_WINDOW_S`` 的
        快速失败窗口：客户端列表不依赖 ffmpeg，窗口过后进程活着就放行，让
        ffmpeg 启动与「响应传回 + 播放器初始化 + 列表请求」并行——起播与
        seek 重启各省几百毫秒（常数注释里有完整取舍）。"""
        vod = session.segment_plan is not None
        deadline = time.monotonic() + (
            VOD_FAST_FAIL_WINDOW_S if vod else PLAYLIST_WAIT_TIMEOUT_S
        )
        while time.monotonic() < deadline:
            if session.playlist_path.exists():
                return
            process = session.process
            if process is not None and process.returncode is not None:
                tail = " / ".join(list(session.stderr_tail)[-5:])
                session.error = tail or f"ffmpeg 退出码 {process.returncode}"
                raise SessionStartError(f"转码进程启动失败：{session.error}")
            await asyncio.sleep(0.05)
        if vod:
            return
        session.error = "ffmpeg 超时未产出播放列表"
        raise SessionStartError(session.error)

    # -- VOD 分片按需供给（§12） -----------------------------------------

    #: 请求的分片超前**已产出头**这么多段就重启 ffmpeg 直奔目标，而不是干等
    #: 它顺序转过去。原来照抄 Jellyfin 的 6（24 秒），实测是「快进 +10 秒
    #: 卡顿」的主因：readrate 1.5 限速下等转码追 n 段差距要 n×4/1.5 秒
    #: （最长 16 秒），而杀掉重启直奔实测 1~3 秒就出片——超前不足一段才值得
    #: 等。重启的代价（丢掉当前轮的前向缓冲）在快进场景本来就不心疼：用户
    #: 明确要去新位置，旧缓冲多半用不上。
    _RESTART_AHEAD_SEGMENTS = 1
    #: 落后于转码头的分片请求要等这么久才有资格触发重启（探测宽限）。
    #: iOS 的 AVPlayer 在 JS seek 落地之前会从列表头狂打 seg00000（真机
    #: 实测 35 连发）——立刻理会就是把转码器劫持回第 0 段、正经起播点
    #: 反而挨饿。探测请求在 seek 落定后由客户端自行取消，熬不过宽限期；
    #: 真正的用户回拖会一直等着，宽限一到照常重启直奔。
    _PROBE_GRACE_S = 3.0
    #: 请求相差这么多分片时，较远且较新的请求视为用户跳转，不能被列表头
    #: 的旧探测请求拖到 30 秒超时。普通播放器的并行预取通常只有 1~3 片。
    _LARGE_SEEK_GAP_SEGMENTS = 4
    #: 单个分片的等待上限。局域网起播 + burst 下正常几百毫秒就好；超时说明
    #: ffmpeg 卡死或存储极慢，让客户端拿 404 重试比挂着请求强
    _SEGMENT_WAIT_S = 30.0

    async def ensure_segment(self, session: TranscodeSession, index: int) -> Path | None:
        """确保 VOD 会话的第 index 个分片就绪，返回文件路径；超时返回 None。

        这是 seek 的唯一入口：客户端按预生成列表直接请求任意分片，这里判断
        「等它转过来」还是「杀掉重启直奔目标」。"""
        plan = session.segment_plan
        if plan is None or not (0 <= index < plan.count):
            return None
        target = session.directory / (SEGMENT_PATTERN % index)
        waited_from = time.monotonic()
        head_before = session.head_segment
        generation = session.restart_generation
        deadline = waited_from + self._SEGMENT_WAIT_S
        # 登记「我在等这一片」。客户端断开时 uvicorn 会取消本协程，finally
        # 兜底注销——挂号表漏项会让重启目标算错，宁可多算不可漏算。
        session.pending_segments[index] = session.pending_segments.get(index, 0) + 1
        session.pending_since.setdefault(index, waited_from)
        session.last_requested_segment = index
        session.last_requested_at_ms = int(time.time() * 1000)
        try:
            result = await self._await_segment(
                session,
                index,
                target,
                waited_from,
                head_before,
                deadline,
                generation,
            )
            session.last_segment_status = 200 if result is not None else 404
            session.last_segment_wait_ms = int(
                (time.monotonic() - waited_from) * 1000
            )
            if result is not None:
                session.last_served_segment = index
                session.last_served_at_ms = int(time.time() * 1000)
            if result is not None and not session.first_segment_served:
                session.first_segment_served = True
                # 起播链路的最后一公里：「会话就绪」只等到 playlist，画面
                # 能动还要等首个分片转出来。首帧慢但「会话就绪」各段都快时，
                # 差值就在这里（ffmpeg 起转到首片落盘 + 客户端发现延迟）
                logger.info(
                    "首片供给：session=%s seg=%05d 距会话创建 %.1f 秒（本次请求等待 %d 毫秒）",
                    session.id, index,
                    time.monotonic() - session.created_at,
                    int((time.monotonic() - waited_from) * 1000),
                )
            return result
        finally:
            remaining = session.pending_segments.get(index, 1) - 1
            if remaining <= 0:
                session.pending_segments.pop(index, None)
                session.pending_since.pop(index, None)
            else:
                session.pending_segments[index] = remaining

    async def _await_segment(
        self,
        session: TranscodeSession,
        index: int,
        target: Path,
        waited_from: float,
        head_before: int,
        deadline: float,
        generation: int,
    ) -> Path | None:
        while time.monotonic() < deadline:
            # 会话可能在等待期间被显式结束（用户退出的 DELETE 会删目录）。
            # 不查这条就会对着已删除的目录重启 ffmpeg，No such file or
            # directory 一路冒成 500。
            if session.state == "stopped":
                return None
            if session.restart_generation != generation:
                # 先允许同一分片的并发请求继续等；其余请求属于旧播放头，
                # 立即返回 404 让播放器丢弃，不能再把新的 seek 拖回去。
                if self._segment_ready(session, index):
                    return target
                if session.restart_target != index:
                    logger.info(
                        "放弃过期分片等待：session=%s seg=%05d 当前目标=%s",
                        session.id,
                        index,
                        session.restart_target,
                    )
                    return None
                generation = session.restart_generation
            # 就绪检查必须排在远程状态检查**之前**：分片一旦落盘就与 Worker
            # 在不在线无关了。远程 job 整片转完后用户合上 Mac，缓存里的分片
            # 仍然完全可播；先查在线状态会把这些请求逐个打成 failed，等于把
            # 已经转好的整部片子作废。
            if self._segment_ready(session, index):
                waited_s = time.monotonic() - waited_from
                if waited_s > 1.0:
                    # seek/快进的卡顿感直接对应这里的等待——用户报「不丝滑」时
                    # 这一行给出量化：等了多久、走的是重启直奔还是顺序追赶
                    logger.info(
                        "分片就绪：session=%s seg=%05d 等待 %.1f 秒（%s）",
                        session.id, index, waited_s,
                        "重启直奔" if session.head_segment != head_before else "顺序追赶",
                    )
                return target
            # seek 重启会先清空旧 job/worker，再异步下发新 job。这个短窗口
            # 内不检查旧状态，否则等待者会把正常切换误报成 Worker 断线。
            if session.remote and not session.remote_restarting:
                registry = get_remote_worker_registry()
                job_state = registry.job_state(session.remote_job_id or "")
                if job_state and job_state.get("type") in {"job.failed", "job.finished"}:
                    session.state = "failed"
                    session.error = _remote_job_failure_message(job_state)
                    return None
                if not registry.worker_online(session.remote_worker_id):
                    session.state = "failed"
                    session.error = "远程 Worker 已断开连接"
                    return None
            try:
                await self._maybe_restart_for(session, index)
            except SessionStartError:
                # 重启失败（目录被删/命令有误）按「拿不到分片」处理：客户端
                # 收 404 走它自己的重试与降档，比 500 刷屏有意义
                logger.warning(
                    "分片重启失败：session=%s seg=%05d：%s", session.id, index, session.error
                )
                return None
            if session.state == "failed":
                return None
            # 50ms 轮询：这决定「分片写完 → 客户端拿到」的发现延迟，seek 的
            # 尾巴上省的就是这几十毫秒。解析已被 _sync_completed 的签名门控
            # 挡住，轮询本身只剩 stat 调用，再快也没有收益。
            await asyncio.sleep(0.05)
        logger.warning(
            "分片等待超时：session=%s seg=%05d（转码进程可能卡死或存储过慢）",
            session.id,
            index,
        )
        return None

    def _segment_ready(self, session: TranscodeSession, index: int) -> bool:
        """判断分片是否可以交给播放器。

        本地 ffmpeg 直接写会话目录，文件存在不代表写入已经完成，所以仍以
        ``live.m3u8`` 台账或进程退出作为完成信号。远程 Worker 的上传端点先写
        随机临时文件，完整接收后才原子替换目标文件并返回 201；远程目标存在
        本身就是完整写入信号，不能再依赖另一条可能被 Worker 取消的 playlist
        上传，否则播放器会对着已经落盘的分片等待到超时。"""
        target = session.directory / (SEGMENT_PATTERN % index)
        if not target.exists():
            return False
        if session.remote:
            # 远程上传端点允许请求体为空时仍完成 HTTP 请求；空文件不能交给
            # 播放器，否则播放器会把一个已经返回的坏分片当成持续缓存。
            try:
                if not target.is_file() or target.stat().st_size <= 0:
                    return False
            except OSError:
                return False
            session.completed_segments.add(index)
            return True
        self._sync_completed(session)
        if index in session.completed_segments:
            return True
        process = session.process
        return process is not None and process.returncode is not None

    def _sync_completed(self, session: TranscodeSession) -> None:
        """把已完成分片并入跨轮次台账。

        按 (mtime_ns, size) 门控：文件没变就不重解析（理由见字段注释）。
        重启会删掉旧列表重写，新文件的签名必然不同，门控自然失效。

        远程产物由上传端点原子替换，目录里的 ``segNNNNN.m4s`` 都是完整文件；
        先扫描目录，使远程 playlist 上传失败时仍能正确计算连续产出头。"""
        if session.remote:
            try:
                directory_mtime_ns = session.directory.stat().st_mtime_ns
            except OSError:
                return
            if directory_mtime_ns == session._remote_artifact_dir_mtime_ns:
                return
            session._remote_artifact_dir_mtime_ns = directory_mtime_ns
            for artifact in session.directory.glob("seg*.m4s"):
                name = artifact.name
                index = _segment_index_from_name(name)
                if index is None or not artifact.is_file():
                    continue
                try:
                    if artifact.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                session.completed_segments.add(index)
            # 远程文件已经是完整产物，playlist 只是可选诊断信息；不再解析它，
            # 避免 499 的 live.m3u8 把不存在的分片误记进完成台账。
            return
        try:
            stat = session.playlist_path.stat()
        except OSError:
            return
        sig = (stat.st_mtime_ns, stat.st_size)
        if sig == session._playlist_sig:
            return
        try:
            text = session.playlist_path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            # 本地 ffmpeg 写的是 `seg00001.m4s`，远程 HLS muxer 上传的
            # live.m3u8 可能写绝对 HTTPS URL 并带 artifact token；两者都要
            # 归一到安全的文件名后再记账，否则远程 VOD 会一直等到整片结束。
            filename = urlsplit(line).path.rsplit("/", 1)[-1]
            if filename.endswith(".m4s") and filename.startswith("seg"):
                try:
                    session.completed_segments.add(int(filename[3:8]))
                except ValueError:
                    continue
        session._playlist_sig = sig

    async def _maybe_restart_for(self, session: TranscodeSession, index: int) -> None:
        """请求的分片在当前转码轮次覆盖不到时，杀掉 ffmpeg 从**全体等待者的
        最小分片号**重启。

        为什么必须看全体而不是只看自己（2026-08-25 真机事故，iPhone PWA）：
        hls.js 串行取分片，但 iOS 的 AVPlayer 会**并行**请求相邻的几个分片。
        逐个自查的旧逻辑在阈值收紧到 1 之后会互相杀——转码器刚从 N 起步，
        N+2 的请求嫌它超前就杀掉直奔 N+2，N 的请求发现自己落后又杀回 N，
        一片都没转完就再次被杀，循环到 30 秒超时 404，AVPlayer 报解码错误、
        一路降档到底（软转/烧录每片编得慢，并发窗口大，必炸）。

        以最小号为准两种场景都正确：并行预取时最小号必然紧贴转码头（覆盖内
        → 全员等待，绝不杀）；真正的快进只有一个远端请求（照旧立杀直奔）。
        重启起点也取最小号——往前杀会把还在等的更早请求永远饿死。"""
        plan = session.segment_plan
        assert plan is not None
        async with session._restart_lock:
            # 排队等锁期间会话可能已被结束；stopped 后目录已删，绝不能再拉进程
            if session.state == "stopped":
                return
            # 低水位暂停期间不能为 seek 新开一轮 ffmpeg，否则刚暂停旧任务又会
            # 立刻启动新任务继续写盘；等空间回升后下一轮循环会正常处理 seek。
            if session.disk_paused:
                return
            produced = self._highest_produced(session)
            # 全体等待者中最小的未完成分片号（已完成的不算——它们各自的
            # 循环马上会拿到文件退出等待）。落后于转码头的等待者分两档看：
            # 只要还有覆盖范围内（≥ head）的等待者，落后者一律不算数——
            # AVPlayer 在 JS seek 落地前会从列表头狂打 seg00000（真机实测
            # 35 连发），立刻理会就是把转码器劫持回第 0 段、正经起播点反而
            # 挨饿；真正的用户回拖不会再有前方请求，此时落后者熬过探测
            # 宽限期（挡住探测的余波）就照常重启直奔。
            now = time.monotonic()
            waiting = [
                i for i in session.pending_segments if i not in session.completed_segments
            ]
            failed_uploads = [
                i
                for i in waiting
                if session.remote and i in session.remote_failed_segments
            ]
            retry_failed = bool(failed_uploads)
            if retry_failed:
                # 后续分片可能已经落盘，不能再用「连续产出头」推断这个分片
                # 会自然出现；它已经明确失败，必须从缺口重启补传。
                wanted = min(failed_uploads)
            else:
                in_coverage = [i for i in waiting if i >= session.head_segment]
            if not retry_failed and in_coverage:
                # 浏览器在用户 seek 后经常还挂着一个「当前头 + 1」的旧请求。
                # 只取最小值会让真正的远端目标（例如 366）等这个旧请求 30 秒
                # 才有机会触发重启。相差明显时选较新的远端目标；近邻预取仍然
                # 保持最小值，避免 iOS 并行请求造成重启风暴。
                far = [
                    i for i in in_coverage
                    if i > produced + self._RESTART_AHEAD_SEGMENTS
                ]
                if far and max(far) - min(waiting) >= self._LARGE_SEEK_GAP_SEGMENTS:
                    wanted = max(
                        far,
                        key=lambda i: session.pending_since.get(i, 0.0),
                    )
                else:
                    wanted = min(in_coverage)
            elif not retry_failed:
                aged = [
                    i
                    for i in waiting
                    if now - session.pending_since.get(i, now) >= self._PROBE_GRACE_S
                ]
                if not aged:
                    return  # 在等的只有宽限期内的头部探测，不理会
                wanted = min(aged)
            behind = wanted < session.head_segment
            ahead = wanted > produced + self._RESTART_AHEAD_SEGMENTS
            process_dead = session.process is not None and session.process.returncode is not None
            # 进程还活着且全体等待者都在覆盖范围内：等它转过来即可
            if not (
                retry_failed
                or behind
                or ahead
                or (process_dead and not self._segment_ready(session, index))
            ):
                return
            index = wanted
            logger.info(
                "转码重启直奔分片：session=%s seg=%05d（原因=%s 当前头=%d 已产出到=%d 在等=%s）",
                session.id,
                index,
                "上传失败补片" if retry_failed else "seek/追赶",
                session.head_segment,
                produced,
                sorted(session.pending_segments),
            )
            session.restart_generation += 1
            session.restart_target = index
            if session.remote:
                await self._restart_remote(session, index)
                return
            # seek 重启走快杀（SIGKILL），不给 SIGTERM 收尾机会：优雅退出是
            # ffmpeg 把当前分片写完再走，转码压力大时要一两秒——而这段时间
            # 用户正对着转圈等新位置的画面。没写完的分片不进列表、重启后会被
            # 覆盖，立杀没有任何损失（Jellyfin 的 seek 路径同款）。
            await self._terminate(session, graceful=False)
            session.head_segment = index
            session.start_ms = int(plan.boundaries[index] * 1000)
            session.state = "spawning"
            session.error = None
            command = build_hls_command(
                session.plan,
                source_path=session.source_path,
                session_dir=session.directory,
                start_ms=session.start_ms,
                hw_backend=session.hw_backend,
                start_number=index,
            )
            # 旧轮次的清单先并入台账再删：这一轮转出的分片下次回看直接可用
            self._sync_completed(session)
            session.playlist_path.unlink(missing_ok=True)
            await self._spawn(session, command)

    async def _restart_remote(self, session: TranscodeSession, index: int) -> None:
        """远程 seek：停止旧任务后，从目标分片重新下发 ffmpeg。"""
        registry = get_remote_worker_registry()
        session.remote_restarting = True
        try:
            old_job_id = session.remote_job_id
            if old_job_id is not None:
                if session.disk_paused:
                    await registry.resume(old_job_id)
                    session.disk_paused = False
                # seek 重启与普通退出不同：旧分片已经失去交付价值，直接杀掉
                # 远端 ffmpeg，避免旧任务继续读源并和新轮次并发上传。
                await registry.cancel(old_job_id, force=True)
                registry.remove_job(old_job_id)
            session.remote_worker_id = None
            session.remote_job_id = None
            # stop() 会先把状态写成 stopped，再等待重启锁；不要在这个窗口
            # 里重新创建一个刚被用户关闭的远程任务。
            if session.state == "stopped":
                return
            assert session.segment_plan is not None
            session.head_segment = index
            session.start_ms = int(session.segment_plan.boundaries[index] * 1000)
            session.remote_failed_segments.discard(index)
            session.state = "spawning"
            session.error = None
            self._sync_completed(session)
            session.playlist_path.unlink(missing_ok=True)
            job_id = new_ulid()
            artifact_token = await issue_remote_grant(
                session_id=session.id,
                file_id=session.file_id,
                kind="artifact",
                attempt_id=job_id,
            )
            session.remote_artifact_suffix = f"?token={quote(artifact_token, safe='')}"
            command = build_hls_command(
                session.plan,
                source_path=session.remote_source_url,
                session_dir=session.directory,
                start_ms=session.start_ms,
                hw_backend=session.hw_backend,
                start_number=index,
                output_base_url=session.remote_artifact_base_url,
                output_url_suffix=session.remote_artifact_suffix,
            )
            session.remote_job_id = job_id
            registry.create_job_waiter(job_id)
            try:
                worker_id = await registry.dispatch(
                    job_id,
                    {
                        "file_id": session.file_id,
                        "attempt_id": job_id,
                        "start_ms": session.start_ms,
                        "ffmpeg_args": command.argv[1:],
                    },
                    backend=session.hw_backend or "videotoolbox",
                )
                session.remote_worker_id = worker_id
                event = await registry.wait_job_event(job_id)
                if event.get("type") != "job.accepted":
                    error = str(event.get("error") or "远程 Worker 拒绝 seek 任务")
                    session.state = "failed"
                    session.error = error
                    raise SessionStartError(error)
                session.state = "ready"
                session.touch()
            except BaseException as exc:
                # dispatch 成功后，超时、拒绝和异常都可能发生在新 job 已经
                # 占住 Worker 槽位之后。若这里只移除 waiter，Worker 仍会
                # 继续跑旧轮次，后续 seek 也会因为本地残留映射无法正确选
                # 空闲 Worker。清理要覆盖 failed 事件和接单超时两条路径。
                with contextlib.suppress(BaseException):
                    await registry.cancel(job_id, force=True)
                registry.remove_job(job_id)
                if session.remote_job_id == job_id:
                    session.remote_worker_id = None
                    session.remote_job_id = None
                session.state = "failed"
                session.error = str(exc)
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, RemoteWorkerUnavailable):
                    raise SessionStartError(f"远程 seek 不可用：{exc}") from exc
                if isinstance(exc, SessionStartError):
                    raise
                raise SessionStartError(f"远程 seek 启动失败：{exc}") from exc
            finally:
                registry.remove_job_waiter(job_id)
        finally:
            session.remote_restarting = False

    def _highest_produced(self, session: TranscodeSession) -> int:
        """从转码头起**连续**产出到的分片号；一个都没写完时为 head-1。

        必须数连续段而不是 `max(≥ head)`：台账是跨轮次的，重启到低位后
        （比如 seek 回第 0 段）上一轮留下的高编号分片全都 ≥ head，取 max 会
        把 produced 顶到上一轮的尾巴上——后果是请求「上一轮尾巴 + 1」时
        ahead 判定失灵，既不重启也等不来，30 秒超时 404 循环，播放器表现为
        永远缓冲。ffmpeg 在一轮内是从 head 顺序写的，连续段就是本轮进度
        （恰好与上一轮无缝衔接的旧分片也算——它们本来就直接可服务）。"""
        self._sync_completed(session)
        produced = session.head_segment - 1
        while (produced + 1) in session.completed_segments:
            produced += 1
        return produced

    def get(self, session_id: str, *, member_id: int | None = None) -> TranscodeSession | None:
        """取会话。``member_id`` 非空时校验归属——会话是私人资源。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if member_id is not None and session.member_id != member_id:
            return None
        return session

    def ping(self, session_id: str, *, member_id: int | None = None) -> bool:
        session = self.get(session_id, member_id=member_id)
        if session is None:
            return False
        session.touch()
        return True

    async def stop(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        # 先置状态再抢重启锁：正在排队等锁的 VOD 分片重启会在临界区入口看到
        # stopped 直接放弃。终止必须与 _maybe_restart_for 互斥——不互斥的话，
        # 一次「杀旧进程 → 拉新进程」的重启可能在本次 killpg **之后**才把新
        # ffmpeg 拉起来，而会话已经不在表里，那个进程从此没人管（孤儿
        # ffmpeg，「关了播放 ffmpeg 还在跑」的一条来路）。
        session.state = "stopped"
        async with session._restart_lock:
            # 锁内再置一次：恰好在临界区里的那次重启会经由 _spawn 把状态写回
            # spawning/ready，把外面置的 stopped 盖掉——不重申终态的话，还挂着
            # 的分片等待者下一拍又能通过状态检查再拉起一个 ffmpeg。
            session.state = "stopped"
            await self._terminate(session)
        shutil.rmtree(session.directory, ignore_errors=True)
        return True

    async def stop_for_file(self, file_id: int, member_id: int) -> int:
        """停掉同一成员对同一文件的其它会话。

        seek 到已转区间之外要起新会话，此时旧会话必须先杀掉——不然用户连拖
        五下进度条就有五个 ffmpeg 在跑，NAS 直接躺平（§4.4）。
        """
        victims = [
            sid
            for sid, s in self._sessions.items()
            if s.file_id == file_id and s.member_id == member_id
        ]
        started_at = time.monotonic()
        for sid in victims:
            await self.stop(sid)
        if victims:
            # 换字幕烧录/换音轨/seek 重开会话都要先走这里，SIGTERM 的收尾
            # 等待（最多 3 秒）会整段计入用户感知的切换延迟——「换轨慢」时
            # 先看这一行，接近 3000 毫秒就该考虑对重开场景直接 SIGKILL
            logger.info(
                "杀掉同文件旧会话 %d 个耗时 %d 毫秒（file_id=%s）",
                len(victims), int((time.monotonic() - started_at) * 1000), file_id,
            )
        return len(victims)

    async def _terminate(self, session: TranscodeSession, *, graceful: bool = True) -> None:
        """契约 3：杀整个进程组。默认 SIGTERM 后给 3 秒再 SIGKILL。

        ``graceful=False`` 直接 SIGKILL——seek 重启专用：用户在等画面，
        SIGTERM 让 ffmpeg 收尾写完当前分片纯属白等（调用处有完整理由）。
        """
        if session.remote:
            # 远程 Worker 没有可供 NAS killpg 的 PID；控制面 stop 与本地
            # SIGTERM/SIGKILL 对应。即使 Worker 已断线，也要释放 NAS 的占用台账。
            registry = get_remote_worker_registry()
            if session.remote_job_id is not None:
                if session.disk_paused:
                    await registry.resume(session.remote_job_id)
                    session.disk_paused = False
                await registry.cancel(session.remote_job_id)
                registry.remove_job(session.remote_job_id)
            session.remote_worker_id = None
            session.remote_job_id = None
            return
        process = session.process
        if process is None or process.returncode is not None:
            await self._cancel_stderr(session)
            return
        # 被低水位哨兵挂起的进程不会处理 SIGTERM（信号排队到 SIGCONT 之后），
        # 不解冻直接杀只能等 3 秒超时走 SIGKILL——白等
        if session.disk_paused:
            self._signal_group(session, signal.SIGCONT)
            session.disk_paused = False
        first_signal = signal.SIGTERM if graceful else signal.SIGKILL
        try:
            os.killpg(os.getpgid(process.pid), first_signal)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                if graceful:
                    process.terminate()
                else:
                    process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0 if graceful else 2.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=2.0)
        await self._cancel_stderr(session)

    async def _cancel_stderr(self, session: TranscodeSession) -> None:
        task = session._stderr_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        session._stderr_task = None

    # -- 观测 -------------------------------------------------------------

    def usage_bytes(self) -> int:
        """当前转码缓存占盘。设置页展示用，也是配额判定的依据。"""
        if not self._root.exists():
            return 0
        return sum(f.stat().st_size for f in self._root.rglob("*") if f.is_file())

    def active(self) -> list[TranscodeSession]:
        return list(self._sessions.values())


_manager: TranscodeSessionManager | None = None


def get_session_manager() -> TranscodeSessionManager:
    global _manager
    if _manager is None:
        _manager = TranscodeSessionManager()
    return _manager


def reset_session_manager() -> None:
    """测试用：丢掉全局实例。调用方负责先 shutdown。"""
    global _manager
    _manager = None
