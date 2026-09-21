"""内封字幕轨的按需抽取（docs/design/web-player.md §6.2）。

**为什么是中性模块**：从容器里抽一条内封字幕轨，是「读媒体文件」这件事本身，
既不属于播放层也不属于生产端。网页播放器要它把字幕旁挂下发，AI 字幕生成要它
拿到参考文本——两边要的是**同一条轨、同一条 ffmpeg 命令、同一份产物**。
此前两边各写一套（``services/playback/embedded_subs`` 与 ``subtitle_gen/
extract``），缓存目录还不同，同一个 16 GB 的 MKV 被通读两遍（issue #432）。
收拢到这里之后谁先要谁触发，另一边直接命中缓存；也让生产端不必 import
播放层（subtitle-ai-translate.md §7 的分层守护）。

**为什么保留原格式**：ASS 转 VTT 会丢掉特效与排版，番剧字幕直接崩。因此
ASS/SSA 原样 copy 出来交 JASSUB，纯文本轨才转 SRT。生成端要的纯文本由
pysubs2 从 ASS 里取（``plaintext``），不需要为它再抽一份 SRT。

抽取是长时间 IO（大文件分钟级），三条纪律：

1. **单飞**：同一条轨并发只跑一个 ffmpeg。详情页预热、播放器请求、字幕预检
   会同时命中同一条轨，各抽各的等于把最贵的一步做了 N 遍。
2. **可取消**：异步入口使用可取消的子进程，取消时连同整个进程组一起回收；
   只有最后一个等待者离开才真的取消，避免预热与播放互相误杀。
3. **不留残片**：先写临时文件再原子替换，失败/超时/取消都不会把半成品留成
   下一次的「缓存命中」。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from movieclaw_api.core.config import get_settings
from movieclaw_db.models import LibraryFile

logger = logging.getLogger("movieclaw_api.media_extract")

#: 抽取要通读整个容器，大文件是分钟级，比探测慢得多。
EXTRACT_TIMEOUT = 120.0
# 先给 ffmpeg 一个正常退出窗口，超时或取消后再强制杀掉整个进程组。
_PROCESS_TERM_TIMEOUT = 2.0
_PROCESS_KILL_TIMEOUT = 5.0

#: 纯文本轨：抽成 SRT，播放服务层再按请求转 VTT 交 ``<track>``。
_TEXT_CODECS = frozenset({"subrip", "srt", "mov_text", "text", "webvtt", "vtt"})
#: 特效轨：原样 copy 出来交 JASSUB，转格式就毁了。
_ASS_CODECS = frozenset({"ass", "ssa"})
#: 蓝光位图轨：原样 copy 成 .sup（HDMV PGS 的标准封装，ffmpeg 的 sup muxer），
#: 交前端 libbitsub 在 canvas 上渲染——与 Jellyfin 10.9+ 的做法一致。
#: 绝不烧录（硬边界 1），也绝不 OCR（错字比没字幕更糟）。
_PGS_CODECS = frozenset({"hdmv_pgs_subtitle", "pgssub", "pgs"})

#: 能进文本管线（pysubs2 解析）的产物格式；sup 是二进制位图，只能渲染。
TEXT_FORMATS = frozenset({"srt", "ass"})


@dataclass(frozen=True)
class ExtractedTrack:
    """一条抽取完成的内封字幕轨产物。"""

    path: Path
    format: str  # srt/ass/sup（小写）


@dataclass(frozen=True)
class _ExtractionSpec:
    """一次字幕抽取的固定输入与缓存位置。"""

    fmt: str
    video: Path
    out_path: Path


@dataclass
class _ExtractionJob:
    """同一字幕轨的共享抽取任务及当前等待者数量。"""

    task: asyncio.Task[ExtractedTrack | None]
    waiters: int = 0


#: 抽取任务的身份：同一个视频的同一条轨、同一个产物路径即同一件活。
_JobKey = tuple[str, int, str]

# 详情页预热、播放器请求与字幕预检可能同时命中同一条内封轨；共享任务既避免
# 重复读盘，也让最后一个请求离开时能取消仍在进行的 ffmpeg。
_EXTRACTION_JOBS: dict[_JobKey, _ExtractionJob] = {}
# 预检发起的后台抽取：用户关掉对话框也要把产物抽完落缓存，所以它自己就是
# 一个等待者，不随请求取消；这里只为「已经在抽了吗」提供同步答案。
_BACKGROUND_TASKS: dict[_JobKey, asyncio.Task[None]] = {}

# 抽取失败过的轨 → 当时视频的 mtime_ns。**失败结论必须记住**：前端在轮询，
# 不记就会每隔两三秒催起一个新的 ffmpeg 去读同一个坏轨，一条读不出来的轨
# 足以把 CPU 吃满。只有视频本体变了（洗版、重新压制）才值得再试一次。
_FAILED_EXTRACTIONS: dict[_JobKey, int] = {}


def cache_dir() -> Path:
    """抽取产物目录（播放与 AI 字幕生成共用）。

    中间品不进媒体库目录，根目录来自配置（缓存管理面板按登记表统计/清理它，
    见 services/storage/registry.py）。
    """
    return Path(get_settings().playback_subs_cache_dir)


def subtitle_format(codec: str | None) -> str | None:
    """内封轨 codec → 抽取后的文件格式；不支持的轨（VobSub 等）返回 None。"""
    normalized = (codec or "").lower()
    if normalized in _ASS_CODECS:
        return "ass"
    if normalized in _TEXT_CODECS:
        return "srt"
    if normalized in _PGS_CODECS:
        return "sup"
    return None


def track_codec(file: LibraryFile, index: int) -> str | None:
    """取第 index 条内封字幕轨的 codec；越界或未探测返回 None。

    数组下标与 ffmpeg 的 ``0:s:<k>`` 同源——都是「第 k 条字幕流」，因此可以
    直接用。绝不能换成绝对流序号，那个会被视频/音频/附件流搅乱。
    """
    streams = file.subtitle_streams or []
    if not 0 <= index < len(streams):
        return None
    raw = streams[index]
    return raw.get("codec") if isinstance(raw, dict) else None


def _extraction_spec(file: LibraryFile, index: int) -> _ExtractionSpec | None:
    fmt = subtitle_format(track_codec(file, index))
    if fmt is None:
        return None
    video = Path(file.file_path)
    return _ExtractionSpec(
        fmt=fmt,
        video=video,
        out_path=cache_dir() / f"{file.id}.s{index}.{fmt}",
    )


def _job_key(spec: _ExtractionSpec, index: int) -> _JobKey:
    return (str(spec.video), index, str(spec.out_path))


def _is_fresh(out_path: Path, video: Path) -> bool:
    """产物比视频新且非空即可复用。

    抽取要通读整个容器，不能每次点开字幕都重来一遍；而只有视频本体变了
    （洗版、改名归并）才需要重抽。
    """
    try:
        return (
            out_path.is_file()
            and out_path.stat().st_size > 0
            and out_path.stat().st_mtime_ns > video.stat().st_mtime_ns
        )
    except OSError:
        return False  # stat 失败按未缓存处理，走正常抽取


def _cached_track(spec: _ExtractionSpec) -> ExtractedTrack | None:
    if not _is_fresh(spec.out_path, spec.video):
        return None
    return ExtractedTrack(path=spec.out_path, format=spec.fmt)


def cached_track(file: LibraryFile, index: int) -> ExtractedTrack | None:
    """只看缓存：有可复用的产物就返回，否则 None（绝不起 ffmpeg）。

    预检用它判断「这次能不能立刻给出结论」——没有缓存就转后台抽取 + 轮询，
    而不是把请求挂在那里等分钟级的通读（issue #432）。
    """
    spec = _extraction_spec(file, index)
    if spec is None:
        return None
    return _cached_track(spec)


def _can_extract(spec: _ExtractionSpec) -> bool:
    if shutil.which("ffmpeg") is None:
        logger.warning(
            "系统中未找到 ffmpeg，无法抽取内封字幕轨——请安装 ffmpeg，"
            "或为该影片放置外挂字幕文件（官方 Docker 镜像已内置 ffmpeg）"
        )
        return False
    return spec.video.is_file()


def _new_tmp_path(out_path: Path) -> Path:
    # 先写临时文件再原子替换：失败、超时或取消都不会把半成品当成缓存。
    # 临时文件保留正式后缀，ffmpeg 才能按扩展名选对 muxer。
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return out_path.with_name(f".{out_path.stem}.{uuid.uuid4().hex}.part{out_path.suffix}")


def _extract_command(spec: _ExtractionSpec, index: int, tmp_path: Path) -> list[str]:
    # ASS/PGS 用 copy 保住格式；文本轨统一转 SRT，抹平 mov_text 等差异。
    codec_args = ["-c:s", "copy"] if spec.fmt in ("ass", "sup") else ["-c:s", "srt"]
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-i", str(spec.video),
        "-map", f"0:s:{index}",
        *codec_args,
        str(tmp_path),
    ]


def _finish_extraction(
    spec: _ExtractionSpec,
    index: int,
    tmp_path: Path,
    returncode: int | None,
    stderr: bytes,
    started_at: float,
) -> ExtractedTrack | None:
    try:
        valid = tmp_path.is_file() and tmp_path.stat().st_size > 0
    except OSError:
        valid = False
    if returncode != 0 or not valid:
        _cleanup(tmp_path)
        logger.warning(
            "内封字幕抽取失败：%s 轨 %d（%s）",
            spec.video, index, stderr.decode(errors="replace")[:200],
        )
        return None
    try:
        tmp_path.replace(spec.out_path)
    except OSError as exc:
        _cleanup(tmp_path)
        logger.warning("内封字幕缓存写入失败：%s（%s）", spec.out_path, exc)
        return None
    logger.info(
        "内封字幕抽取完成：%s 轨 %d → %s 耗时 %.1f 秒",
        spec.video.name, index, spec.fmt, time.monotonic() - started_at,
    )
    return ExtractedTrack(path=spec.out_path, format=spec.fmt)


def extract_track(file: LibraryFile, index: int) -> ExtractedTrack | None:
    """阻塞地抽出内封轨，供既有同步/离线调用使用。

    Web 请求使用下面的异步入口；这里仍保留同步 API，避免影响扫描脚本和已有
    调用者，但进程同样独立成组，超时会清理，不会遗留 ``.part`` 文件。
    """
    spec = _extraction_spec(file, index)
    if spec is None:
        return None
    cached = _cached_track(spec)
    if cached is not None:
        return cached
    if not _can_extract(spec):
        return None

    tmp_path = _new_tmp_path(spec.out_path)
    started_at = time.monotonic()
    try:
        proc = subprocess.Popen(
            _extract_command(spec, index, tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        _cleanup(tmp_path)
        logger.warning("内封字幕抽取进程启动失败：%s（%s）", spec.video, exc)
        return None

    try:
        _, stderr = proc.communicate(timeout=EXTRACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        _terminate_sync_process(proc)
        _cleanup(tmp_path)
        logger.warning(
            "内封字幕抽取超时（%.0f 秒）：%s 轨 %d", EXTRACT_TIMEOUT, spec.video, index
        )
        return None
    return _finish_extraction(spec, index, tmp_path, proc.returncode, stderr, started_at)


async def extract_track_async(file: LibraryFile, index: int) -> ExtractedTrack | None:
    """异步抽出内封轨，并在请求取消时回收对应的 ffmpeg 进程。

    同一条轨的调用共享一个任务。调用方取消只释放自己的等待者；没有其它
    等待者时才取消底层任务，避免详情页预热与播放器请求互相误杀。
    """
    spec = _extraction_spec(file, index)
    if spec is None:
        return None
    cached = _cached_track(spec)
    if cached is not None:
        return cached
    return await _shared_extract(spec, index)


async def _shared_extract(spec: _ExtractionSpec, index: int) -> ExtractedTrack | None:
    """单飞入口：同一条轨的并发调用共享一个 ffmpeg 进程。"""
    key = _job_key(spec, index)
    job = _EXTRACTION_JOBS.get(key)
    if job is None or job.task.done():
        task = asyncio.create_task(_extract_async_uncached(spec, index))
        job = _ExtractionJob(task=task)
        _EXTRACTION_JOBS[key] = job
        task.add_done_callback(lambda done: _forget_extraction_job(key, done))
    job.waiters += 1
    try:
        # 请求取消不能直接取消共享任务；finally 会在最后一个等待者离开时
        # 负责取消它，并由子进程协程完成 SIGTERM/SIGKILL 清理。
        return await asyncio.shield(job.task)
    finally:
        job.waiters -= 1
        if job.waiters == 0 and not job.task.done():
            job.task.cancel()


def _video_stamp(spec: _ExtractionSpec) -> int | None:
    try:
        return spec.video.stat().st_mtime_ns
    except OSError:
        return None


def extraction_failed(file: LibraryFile, index: int) -> bool:
    """这条轨上一次抽取是否已经失败过（且视频本体没换）。

    预检据此在轮询里**立刻给出错误**，而不是一遍遍重试一条读不出来的轨。
    视频换了就忘掉旧结论——洗版之后值得再试一次。
    """
    spec = _extraction_spec(file, index)
    if spec is None:
        return False
    key = _job_key(spec, index)
    stamp = _FAILED_EXTRACTIONS.get(key)
    if stamp is None:
        return False
    current = _video_stamp(spec)
    if current is not None and current != stamp:
        _FAILED_EXTRACTIONS.pop(key, None)
        return False
    return True


def schedule_extraction(file: LibraryFile, index: int) -> bool:
    """后台抽取一条内封轨，不等它完成；返回「是否正在抽」。

    预检专用（issue #432）：大文件通读是分钟级，把 HTTP 请求挂在那里等，
    iPhone Safari 约 60 秒就掐断连接并显示浏览器原话 ``Load failed``，而
    服务端照跑到底——用户看到的是失败，机器的活一点没省。改成这里起后台
    任务、接口立刻回「正在读取」之后，前端轮询等结论即可。

    后台任务自己持有等待者，**用户关掉对话框也会把产物抽完落缓存**——这趟
    昂贵的通读只做一次，之后无论播放器还是预检都直接命中。返回 False 的三种
    情况：已有缓存、轨不支持、上次已经失败过（都不该再起进程）。
    """
    spec = _extraction_spec(file, index)
    if spec is None:
        return False
    if _cached_track(spec) is not None:
        return False
    if extraction_failed(file, index):
        return False
    key = _job_key(spec, index)
    running = _BACKGROUND_TASKS.get(key)
    if running is not None and not running.done():
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有事件循环（同步上下文）时不调度
        return False

    async def _run() -> None:
        # spec 在调度时已算好：后台任务不再触碰 ORM 对象，避免请求的会话
        # 关闭后读属性抛 DetachedInstanceError。
        try:
            produced = await _shared_extract(spec, index)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 后台抽取失败只影响下一次预检
            logger.warning(
                "内封字幕后台抽取失败：%s 轨 %d", spec.video, index, exc_info=True
            )
            produced = None
        # 记住成败：失败不记，前端轮询会每隔两三秒把同一条坏轨再抽一遍。
        if produced is None:
            stamp = _video_stamp(spec)
            if stamp is not None:
                _FAILED_EXTRACTIONS[key] = stamp
        else:
            _FAILED_EXTRACTIONS.pop(key, None)

    task = loop.create_task(_run(), name=f"subtitle-extract-{spec.out_path.name}")
    _BACKGROUND_TASKS[key] = task
    task.add_done_callback(lambda done: _forget_background_task(key, done))
    return True


def _signal_process_group(pid: int | None, sig: signal.Signals) -> None:
    """给独立进程组发信号；进程已退出时按幂等处理。"""
    if pid is None:
        return
    with contextlib.suppress(OSError):
        os.killpg(pid, sig)


def _terminate_sync_process(proc: subprocess.Popen[bytes]) -> None:
    _signal_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.communicate(timeout=_PROCESS_TERM_TIMEOUT)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_PROCESS_KILL_TIMEOUT)


async def _terminate_async_process(
    proc: asyncio.subprocess.Process,
    communicate: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    _signal_process_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(communicate), _PROCESS_TERM_TIMEOUT)
        return
    except (TimeoutError, OSError, asyncio.CancelledError):
        _signal_process_group(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError, OSError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(communicate), _PROCESS_KILL_TIMEOUT)


async def _extract_async_uncached(
    spec: _ExtractionSpec, index: int
) -> ExtractedTrack | None:
    # 共享任务创建后再次检查，避免并发调用之间有一个刚刚完成缓存写入。
    cached = _cached_track(spec)
    if cached is not None:
        return cached
    if not _can_extract(spec):
        return None

    tmp_path = _new_tmp_path(spec.out_path)
    started_at = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *_extract_command(spec, index, tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        _cleanup(tmp_path)
        logger.warning("内封字幕抽取进程启动失败：%s（%s）", spec.video, exc)
        return None

    communicate = asyncio.create_task(proc.communicate())
    try:
        _, stderr = await asyncio.wait_for(asyncio.shield(communicate), EXTRACT_TIMEOUT)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_async_process(proc, communicate))
        _cleanup(tmp_path)
        logger.info("内封字幕抽取已取消：%s 轨 %d", spec.video, index)
        raise
    except TimeoutError:
        await _terminate_async_process(proc, communicate)
        _cleanup(tmp_path)
        logger.warning(
            "内封字幕抽取超时（%.0f 秒）：%s 轨 %d", EXTRACT_TIMEOUT, spec.video, index
        )
        return None
    return _finish_extraction(spec, index, tmp_path, proc.returncode, stderr, started_at)


def _forget_extraction_job(key: _JobKey, task: asyncio.Task[ExtractedTrack | None]) -> None:
    job = _EXTRACTION_JOBS.get(key)
    if job is not None and job.task is task:
        _EXTRACTION_JOBS.pop(key, None)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.exception()


def _forget_background_task(key: _JobKey, task: asyncio.Task[None]) -> None:
    if _BACKGROUND_TASKS.get(key) is task:
        _BACKGROUND_TASKS.pop(key, None)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.exception()


def _cleanup(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
