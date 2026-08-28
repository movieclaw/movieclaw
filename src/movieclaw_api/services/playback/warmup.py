"""起播预热（docs/design/web-player.md §6.10）。

「首次起播卡缓冲、重进就好」的另一半解法：起播链路上有两件**首次昂贵、
之后有缓存**的探测——关键帧采样（决策要用，三段共约 90 秒码流的读取）与
默认内封字幕的抽取（要通读整个容器）。把它们提前到**用户还在条目详情页
看简介**的那几秒里后台做掉，点播放时缓存直接命中，首播路径只剩「起
ffmpeg、等首片」一件事——Emby 快就快在它把这些全做在入库时，我们至少
要做到「详情页时段」。

约束：

- **只预热文件数很少的条目**（电影/单文件）。剧集详情页猜不到用户要播
  哪一集，把几十个文件全预热是几 GB 的无谓 IO；剧集连播场景第二集起本来
  就有缓存与页缓存，收益也小。
- **同一条目并发触发只做一次**（防止详情页反复刷新叠加探测 IO）；探测
  自身的结果缓存（media_probe / embedded_subs）保证真正的幂等。
- 关键帧探测放线程池，字幕抽取使用可取消的异步子进程；全部吞掉预热异常——
  预热失败的后果只是回到「首播现场探测」的旧行为，绝不能影响详情页本身。
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from movieclaw_api.services.media_probe import probe_keyframe_interval
from movieclaw_api.services.playback.embedded_subs import (
    embedded_subtitle_format,
    extract_embedded_subtitle_async,
)
from movieclaw_db.models import LibraryFile

logger = logging.getLogger("movieclaw_api.playback.warmup")

#: 超过这个文件数的条目不预热（剧集整季详情页）。
_MAX_FILES = 4

#: 正在预热的条目 id 集合（进程内去重）。
_in_flight: set[int] = set()
# 保存任务句柄：用户真正开始播放时，详情页遗留的字幕预热应立即退出。
_tasks: dict[int, asyncio.Task[None]] = {}


def _pick_subtitle_index(file: LibraryFile) -> int | None:
    """挑要预热的字幕轨：默认轨优先，否则第一条网页端支持的轨。

    与前端 pickInitialSubtitle 的选择逻辑同向（默认轨大概率就是起播时
    被自动选中的那条）；猜错的代价只是预热了另一条轨、真正那条回到现场
    抽取——不会更糟。
    """
    streams = file.subtitle_streams or []
    fallback: int | None = None
    for index, raw in enumerate(streams):
        if not isinstance(raw, dict):
            continue
        if embedded_subtitle_format(raw.get("codec")) is None:
            continue
        if raw.get("default"):
            return index
        if fallback is None:
            fallback = index
    return fallback


async def _warm_file(file: LibraryFile) -> None:
    """预热一个文件；阻塞探测在线程池，字幕抽取保持可取消。"""
    interval = await asyncio.to_thread(
        probe_keyframe_interval, file.file_path, file.duration_seconds
    )
    subtitle_index = _pick_subtitle_index(file)
    if subtitle_index is not None:
        await extract_embedded_subtitle_async(file, subtitle_index)
    logger.debug(
        "起播预热完成：file_id=%s 关键帧间隔=%s 字幕轨=%s",
        file.id, interval, subtitle_index,
    )


def schedule(media_item_id: int, files: list[LibraryFile]) -> None:
    """后台预热一个条目的在位文件；文件多（剧集）或已在预热中则跳过。"""
    if not files or len(files) > _MAX_FILES:
        return
    if media_item_id in _in_flight:
        return
    _in_flight.add(media_item_id)

    async def run() -> None:
        try:
            for file in files:
                result = _warm_file(file)
                # 保留对旧版同步探测替身的兼容；正式实现是可取消的协程。
                if inspect.isawaitable(result):
                    await result
        except Exception:  # noqa: BLE001 — 预热失败绝不能影响详情页与播放
            logger.debug("起播预热失败：media_item_id=%s", media_item_id, exc_info=True)
        finally:
            _in_flight.discard(media_item_id)
            _tasks.pop(media_item_id, None)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:  # 没有事件循环（同步上下文）时跳过
        _in_flight.discard(media_item_id)
        return
    _tasks[media_item_id] = task


def cancel(media_item_id: int) -> None:
    """取消该条目仍在进行的详情页预热，避免与正式播放抢 IO/进程槽位。"""
    task = _tasks.get(media_item_id)
    if task is not None and not task.done():
        task.cancel()
