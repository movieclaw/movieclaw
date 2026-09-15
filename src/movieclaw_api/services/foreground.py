"""前台压力信号与后台让路（docs/design/library.md「对账」；刷流带宽哨兵的同款思路）。

后台重任务（对账、扫描、补探、重复扫描）与前台 API 跑在同一个进程、用同一个
数据库，彼此之间没有任何优先级。NAS 真机上一轮对账把每条碰库的请求拖慢 10–40 倍
整整一分钟，用户正好打开媒体库就是「正在加载」十几秒；重复扫描更是一次占死 57 秒。
刷流下载早就有一个正确的先例——上行受压时秒级让路——这里把同样的思路搬到
数据库与 CPU 上：**前台有请求在算，后台就等一等**。

压力信号刻意只取「在途且还没开始响应」的 API 请求数：

- 在响应头发出的那一刻就减掉，所以取流、SSE、长轮询这类连着几分钟的响应不会把
  后台饿死——它们的重活在头之前就算完了；
- 健康探针不计：容器每 30 秒探一次，那 0.7ms 不该让任何任务停一步；
- 只数一个整数，不算 p95：延迟统计要窗口、要采样，而"有没有人在等"这一个比特
  已经足以让后台在页面加载的那一两秒里退开。

让路本身是**协作式**的：任务在自己的批次边界调 ``yield_to_foreground``，前台清空
就立刻继续，前台一直不空也最多等 ``max_wait`` 秒（不能让持续轮询的客户端把
一轮扫描拖成无限长）。任务不知道自己被限速了，也不需要知道。
"""

from __future__ import annotations

import asyncio
import time

#: 在途且还没开始响应的 API 请求数（由 ForegroundPressureMiddleware 维护）
_inflight = 0
#: 让路时每次小睡多久：够细，前台一清空后台就能接上；又不至于忙等
_YIELD_STEP_SECONDS = 0.05
#: 一次让路最多等多久：持续有请求（轮询密集的客户端）时后台也要能推进
DEFAULT_MAX_WAIT_SECONDS = 2.0


def inflight_requests() -> int:
    return _inflight


def request_started() -> None:
    global _inflight
    _inflight += 1


def request_responding() -> None:
    """响应头已发出（或请求已结束）：这一路的重活算完了，不再算压力。"""
    global _inflight
    _inflight = max(0, _inflight - 1)


async def yield_to_foreground(*, max_wait: float = DEFAULT_MAX_WAIT_SECONDS) -> float:
    """前台有在途请求就让一步；返回实际等了多少秒（调用方可累计进日志）。

    前台空闲时是一次整数比较、零开销，可以放在逐文件的循环里。
    """
    if _inflight <= 0:
        return 0.0
    started = time.monotonic()
    deadline = started + max_wait
    while _inflight > 0 and time.monotonic() < deadline:
        await asyncio.sleep(_YIELD_STEP_SECONDS)
    return time.monotonic() - started


def _reset_for_tests() -> None:
    global _inflight
    _inflight = 0
