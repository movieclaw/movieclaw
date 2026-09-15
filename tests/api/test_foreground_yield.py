"""后台让路（services/foreground.py）：前台有请求在算，后台批次就等一等。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from movieclaw_api.middleware import ForegroundPressureMiddleware
from movieclaw_api.services import foreground


@pytest.fixture(autouse=True)
def _clean():
    foreground._reset_for_tests()
    yield
    foreground._reset_for_tests()


@pytest.mark.asyncio
async def test_yield_is_free_when_idle_and_waits_while_busy():
    """前台空闲：立刻返回；前台在算：等到它算完，最多等 max_wait。"""
    assert await foreground.yield_to_foreground() == 0.0

    foreground.request_started()
    loop = asyncio.get_running_loop()
    loop.call_later(0.12, foreground.request_responding)  # 前台 120ms 后开始响应
    waited = await foreground.yield_to_foreground(max_wait=5.0)
    assert 0.1 <= waited < 1.0, waited

    # 前台一直不空：到 max_wait 就继续，不能被持续轮询的客户端拖成无限长
    foreground.request_started()
    waited = await foreground.yield_to_foreground(max_wait=0.15)
    assert 0.15 <= waited < 0.5, waited
    foreground.request_responding()


@pytest.mark.asyncio
async def test_middleware_counts_only_requests_still_computing():
    """请求在算时计数；响应头一发出就减掉（流式响应不会把后台饿死）；健康探针不计。"""
    app = FastAPI()
    gate = asyncio.Event()
    seen: list[int] = []

    @app.get("/api/v1/slow")
    async def slow():
        seen.append(foreground.inflight_requests())
        await gate.wait()
        return {"ok": True}

    @app.get("/api/v1/stream")
    async def stream():
        async def body():
            # 头已经发出、body 还在慢慢流：这段时间不算压力
            seen.append(foreground.inflight_requests())
            yield b"x"

        return StreamingResponse(body())

    @app.get("/api/v1/health")
    async def health():
        seen.append(foreground.inflight_requests())
        return {"status": "ok"}

    app.add_middleware(ForegroundPressureMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        task = asyncio.create_task(client.get("/api/v1/slow"))
        await asyncio.sleep(0.05)
        assert foreground.inflight_requests() == 1
        gate.set()
        assert (await task).status_code == 200
        assert foreground.inflight_requests() == 0

        assert (await client.get("/api/v1/stream")).status_code == 200
        assert seen[-1] == 0  # body 阶段已经不计
        assert foreground.inflight_requests() == 0

        assert (await client.get("/api/v1/health")).status_code == 200
        assert seen[-1] == 0  # 探针本身不计
