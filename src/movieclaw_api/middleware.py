"""全局 HTTP 中间件：spec 指纹头与访问日志。

两者都以**纯 ASGI 中间件**实现，而不是 ``@app.middleware("http")``
（``BaseHTTPMiddleware``）。原因是 2026-08 NAS 现场事故：

- ``BaseHTTPMiddleware`` 会把响应体的每一块经 anyio 内存流再转发一跳，
  视频取流/整文件下载按块计费的 CPU 开销直接翻倍；
- 更要命的是它的 ``receive`` 包装会让 Starlette 1.x 的
  ``Request.is_disconnected()`` 永远返回 False（预取消的 CancelScope 在到达
  服务器 receive 前就被中间件内部的检查点取消），任何依赖它停读盘的响应都会
  在客户端断开后把文件读到底。

纯 ASGI 中间件只包一层 ``send``，不碰 ``receive``、不复制 body，对流式响应
零开销。
"""

import logging
import time

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from movieclaw_api.core.config import Settings
from movieclaw_api.services import foreground
from movieclaw_api.spec_state import SPEC_HASH_HEADER, get_spec_hash

logger = logging.getLogger("movieclaw_api.access")


class SpecHashHeaderMiddleware:
    """所有 API 响应携带 spec 指纹头，CLI 借此零成本发现版本偏斜。"""

    def __init__(self, app: ASGIApp, *, api_prefix: str) -> None:
        self.app = app
        self.api_prefix = api_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.api_prefix):
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                spec_hash = get_spec_hash(scope["app"])
                headers.append(
                    (SPEC_HASH_HEADER.lower().encode("latin-1"), spec_hash.encode("latin-1"))
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)


class AccessLogMiddleware:
    """访问日志：响应头发出时记一条（含耗时），异常记 500。

    对流式响应（取流/SSE）与 BaseHTTPMiddleware 时代语义一致：在响应头就绪时
    记录，不等 body 传完。
    """

    def __init__(self, app: ASGIApp, *, enabled: bool) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        started_at = time.perf_counter()

        async def send_logged(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_code = message["status"]
                duration_ms = (time.perf_counter() - started_at) * 1000
                log = logger.info
                if status_code >= 500:
                    log = logger.error
                elif status_code >= 400:
                    log = logger.warning
                log(
                    "method=%s path=%s status_code=%s duration_ms=%.2f message=%s",
                    method,
                    path,
                    status_code,
                    duration_ms,
                    "request completed",
                )
            await send(message)

        try:
            await self.app(scope, receive, send_logged)
        except Exception:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.error(
                "method=%s path=%s status_code=%s duration_ms=%.2f message=%s",
                method,
                path,
                500,
                duration_ms,
                "request failed",
            )
            raise


class ForegroundPressureMiddleware:
    """维护「在途且还没开始响应」的 API 请求数——后台重任务让路的压力信号。

    响应头一发出就减掉：取流 / SSE 的 body 会流几分钟，但它们的重活在头之前就
    算完了，不该把后台饿死。健康探针不计（容器每 30 秒探一次）。计数在
    ``finally`` 里兜底：客户端在响应头之前断开、处理器抛异常，都不能留下一个
    永远减不掉的 1——那会让后台从此永远在等一个不存在的前台。
    """

    HEALTH_PATH_SUFFIX = "/health"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").endswith(self.HEALTH_PATH_SUFFIX):
            await self.app(scope, receive, send)
            return
        foreground.request_started()
        responding = False

        async def send_counted(message: Message) -> None:
            nonlocal responding
            if message["type"] == "http.response.start" and not responding:
                responding = True
                foreground.request_responding()
            await send(message)

        try:
            await self.app(scope, receive, send_counted)
        finally:
            if not responding:
                foreground.request_responding()


def register_middlewares(app: FastAPI, settings: Settings) -> None:
    # add_middleware 后注册者在外层：访问日志最外层，与原 @app.middleware 顺序一致
    app.add_middleware(SpecHashHeaderMiddleware, api_prefix=settings.api_v1_prefix)
    app.add_middleware(AccessLogMiddleware, enabled=settings.access_log_enabled)
    # 最外层：从请求进门到响应头出门都算在途，不受访问日志开关影响
    app.add_middleware(ForegroundPressureMiddleware)
