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

抽取是长时间 IO（大文件分钟级，媒体库在 NFS 上时还要走一遍网络），纪律如下：

1. **一个文件只通读一遍**：异步入口按文件单飞，一趟 ffmpeg 用多路输出把
   这个文件所有还没缓存的轨一起抽出来（与 Jellyfin 的
   ExtractAllExtractableSubtitles 同一做法）。逐轨抽等于一部带 N 条字幕的
   Remux 被从头读 N 遍；播放器请求、字幕预检、AI 生成同时要同一个文件的
   不同轨，也只跑一个进程。
2. **全局串行**：整文件通读同一时刻只放行一个。几个 ffmpeg 并发读 NFS 只会
   平分带宽，谁都读不完，一起撞超时，已读的全部作废（2026-09 NAS 实测：
   UI 测试批量打开详情页，一天白读几百 GB）。
3. **超时按体积估**：固定 120 秒读不完一部 60 GB 的 Remux，超时即白读；
   按保守吞吐估算上限，只拿它兜住真正卡死的进程。
4. **失败要记住**：超时或 ffmpeg 报错的轨记下当时视频的 mtime，视频没换就
   不再重抽——否则每次打开播放器都把同一个读不出来的文件再通读一遍。
5. **可取消**：子进程可取消，取消时连同整个进程组一起回收；只有最后一个
   等待者离开才真的取消，避免播放器与预检互相误杀。
6. **不留残片**：先写临时文件再原子替换，失败/超时/取消都不会把半成品留成
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

#: 抽取要通读整个容器，大文件是分钟级，比探测慢得多。异步入口以它为下限按
#: 体积放宽（见 ``_extract_timeout``）；同步入口与内嵌字体抽取仍直接用它。
EXTRACT_TIMEOUT = 120.0
#: 估算通读耗时的保守吞吐：千兆网上的 NFS、繁忙的机械盘阵列都跑得到。
_ASSUMED_READ_BYTES_PER_SEC = 20 * 1024 * 1024
#: 体积估算的封顶：再大的文件也不该让一个卡死的 ffmpeg 挂上几个小时。
_MAX_EXTRACT_TIMEOUT = 3600.0
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
    """同一视频文件的共享抽取任务（一趟抽出全部缺缓存的轨）及当前等待者数量。"""

    task: asyncio.Task[None]
    waiters: int = 0


#: 单条轨的身份：同一个视频的同一条轨、同一个产物路径即同一件活。
#: 失败记忆与后台调度按它记账。
_JobKey = tuple[str, int, str]

# 播放器请求、字幕预检与 AI 生成可能同时要同一个文件的轨（同一条或不同条）；
# 按视频路径共享一个任务，既只通读一遍，也让最后一个请求离开时能取消
# 仍在进行的 ffmpeg。
_EXTRACTION_JOBS: dict[str, _ExtractionJob] = {}
# 整文件通读的全局闸门（与创建它的事件循环绑定，测试里每个用例一个新循环）。
_READ_GATE: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
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


def _file_specs(file: LibraryFile) -> list[tuple[int, _ExtractionSpec]]:
    """这个文件所有能抽的内封轨——一趟通读顺手全抽，下次换轨直接命中缓存。"""
    specs = []
    for index in range(len(file.subtitle_streams or [])):
        spec = _extraction_spec(file, index)
        if spec is not None:
            specs.append((index, spec))
    return specs


def _job_key(spec: _ExtractionSpec, index: int) -> _JobKey:
    return (str(spec.video), index, str(spec.out_path))


def _extract_timeout(video: Path) -> float:
    """按体积估算通读上限：下限 EXTRACT_TIMEOUT，封顶 _MAX_EXTRACT_TIMEOUT。"""
    try:
        size = video.stat().st_size
    except OSError:
        return EXTRACT_TIMEOUT
    return min(_MAX_EXTRACT_TIMEOUT, max(EXTRACT_TIMEOUT, size / _ASSUMED_READ_BYTES_PER_SEC))


def _read_gate() -> asyncio.Semaphore:
    """整文件通读的全局闸门：同一时刻只放行一个抽取进程。"""
    global _READ_GATE
    loop = asyncio.get_running_loop()
    if _READ_GATE is None or _READ_GATE[0] is not loop:
        _READ_GATE = (loop, asyncio.Semaphore(1))
    return _READ_GATE[1]


def _remember_failure(spec: _ExtractionSpec, index: int) -> None:
    """记下这条轨在当前视频版本上抽不出来；视频换了（mtime 变）自然作废。"""
    stamp = _video_stamp(spec)
    if stamp is not None:
        _FAILED_EXTRACTIONS[_job_key(spec, index)] = stamp


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


def _codec_args(spec: _ExtractionSpec) -> list[str]:
    # ASS/PGS 用 copy 保住格式；文本轨统一转 SRT，抹平 mov_text 等差异。
    return ["-c:s", "copy"] if spec.fmt in ("ass", "sup") else ["-c:s", "srt"]


def _extract_command(spec: _ExtractionSpec, index: int, tmp_path: Path) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-i", str(spec.video),
        "-map", f"0:s:{index}",
        *_codec_args(spec),
        str(tmp_path),
    ]


def _batch_command(video: Path, outputs: list[tuple[int, _ExtractionSpec, Path]]) -> list[str]:
    """一次读入、多路输出：每条轨一组 ``-map … 输出文件``，ffmpeg 只通读一遍。"""
    argv = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video)]
    for index, spec, tmp_path in outputs:
        argv += ["-map", f"0:s:{index}", *_codec_args(spec), str(tmp_path)]
    return argv


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

    同一个文件的调用共享一个任务。调用方取消只释放自己的等待者；没有其它
    等待者时才取消底层任务，避免播放器请求与字幕预检互相误杀。上次已经
    失败过（且视频没换）的轨直接返回 None，不再通读一遍。
    """
    spec = _extraction_spec(file, index)
    if spec is None:
        return None
    cached = _cached_track(spec)
    if cached is not None:
        return cached
    if extraction_failed(file, index):
        return None
    return await _shared_extract(_file_specs(file), spec, index)


async def _shared_extract(
    batch: list[tuple[int, _ExtractionSpec]], spec: _ExtractionSpec, index: int
) -> ExtractedTrack | None:
    """单飞入口：同一个文件的并发调用共享一趟 ffmpeg，完事后各取各的轨。

    ``batch`` 在调用方手里就算好（不在后台任务里碰 ORM 对象）。
    """
    key = str(spec.video)
    job = _EXTRACTION_JOBS.get(key)
    if job is None or job.task.done():
        task = asyncio.create_task(_extract_batch(batch))
        job = _ExtractionJob(task=task)
        _EXTRACTION_JOBS[key] = job
        task.add_done_callback(lambda done: _forget_extraction_job(key, done))
    job.waiters += 1
    try:
        # 请求取消不能直接取消共享任务；finally 会在最后一个等待者离开时
        # 负责取消它，并由子进程协程完成 SIGTERM/SIGKILL 清理。
        await asyncio.shield(job.task)
        return _cached_track(spec)
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
    return spec is not None and _known_failed(spec, index)


def _known_failed(spec: _ExtractionSpec, index: int) -> bool:
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
    batch = _file_specs(file)

    async def _run() -> None:
        # spec/batch 在调度时已算好：后台任务不再触碰 ORM 对象，避免请求的
        # 会话关闭后读属性抛 DetachedInstanceError。
        try:
            produced = await _shared_extract(batch, spec, index)
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


async def _extract_batch(batch: list[tuple[int, _ExtractionSpec]]) -> None:
    """排队过全局闸门，一趟 ffmpeg 抽出这个文件所有还缺缓存的轨。"""
    async with _read_gate():
        # 排队期间可能已有别人抽完，也可能刚被判了失败：到手再筛一遍。
        pending = [
            (index, spec)
            for index, spec in batch
            if _cached_track(spec) is None and not _known_failed(spec, index)
        ]
        if not pending or not _can_extract(pending[0][1]):
            return
        if await _run_extraction(pending) or len(pending) == 1:
            return
        # 多轨一趟失败，多半是某一条坏轨连累了整趟：退回逐轨抽，好轨照常出
        # 产物、坏轨单独记失败。多读几遍，但只发生在罕见的坏片上。
        logger.warning("多轨一次抽取失败，改为逐轨重试：%s", pending[0][1].video)
        for item in pending:
            await _run_extraction([item])


async def _run_extraction(pending: list[tuple[int, _ExtractionSpec]]) -> bool:
    """跑一趟 ffmpeg 抽出 pending 的全部轨，产物原子落缓存。

    返回 False 仅表示多轨一趟时 ffmpeg 报错退出（产物已清掉、未记失败，交给
    调用方逐轨重试）；其余结局（成功、超时、单轨失败）都已就地记账。
    """
    video = pending[0][1].video
    outputs = [(index, spec, _new_tmp_path(spec.out_path)) for index, spec in pending]
    timeout = _extract_timeout(video)

    def _discard(*, remember: bool) -> None:
        for index, spec, tmp_path in outputs:
            _cleanup(tmp_path)
            if remember:
                _remember_failure(spec, index)

    started_at = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *_batch_command(video, outputs),
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        _discard(remember=True)
        logger.warning("内封字幕抽取进程启动失败：%s（%s）", video, exc)
        return True

    communicate = asyncio.create_task(proc.communicate())
    try:
        _, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_async_process(proc, communicate))
        _discard(remember=False)
        logger.info("内封字幕抽取已取消：%s（%d 条轨）", video, len(outputs))
        raise
    except TimeoutError:
        await _terminate_async_process(proc, communicate)
        # 超时也要记住：不记的话每次打开播放器都会把这个文件再白读一遍。
        _discard(remember=True)
        logger.warning(
            "内封字幕抽取超时（%.0f 秒）：%s（%d 条轨），视频文件不变就不再重试",
            timeout, video, len(outputs),
        )
        return True

    if proc.returncode != 0 and len(outputs) > 1:
        _discard(remember=False)
        return False
    for index, spec, tmp_path in outputs:
        _finish_extraction(spec, index, tmp_path, proc.returncode, stderr, started_at)
        if _cached_track(spec) is None:
            _remember_failure(spec, index)
    return True


def _forget_extraction_job(key: str, task: asyncio.Task[None]) -> None:
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
