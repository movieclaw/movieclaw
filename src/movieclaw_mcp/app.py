"""MCP 端点的 ASGI 调度层（docs/design/mcp-server.md §4.3）。

挂在 ``/mcp`` 下，一个端点一个地址：``POST /mcp/<slug>``。请求进来的顺序是

    Origin 校验 → 方法校验 → 端点查找 → 令牌校验 → 交给官方 SDK

**鉴权在进 SDK 之前完成**：SDK 只负责它擅长的协议编解码，「谁可以进来」始终是
我们自己的判断，与全站「默认拒绝」的立场一致。

为什么每个请求现建一个 Server 与会话管理器：SDK 的 ``StreamableHTTPSessionManager``
明确规定 ``run()`` 一个实例只能进一次，且它内部是一个 anyio 任务组——任务组必须在
创建它的那个任务里退出。端点是运行期增删的，把管理器的生命周期挂到应用 lifespan
上就要跨任务进出，那是纯粹的隐患。无状态模式下管理器本来就不持有跨请求状态，
现建现用最省事也最稳；构造开销是纯内存的对象创建，与随后那次 API 调用比可以忽略。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent
from starlette.types import Receive, Scope, Send

from movieclaw_api import __version__
from movieclaw_api.services import mcp_endpoints
from movieclaw_api.settings import AppServerSetting, get_setting_store
from movieclaw_api.settings.mcp import McpEndpoint
from movieclaw_mcp.dispatch import call_operation
from movieclaw_mcp.tools import build_tools, resolve_tool

logger = logging.getLogger("movieclaw_mcp")

#: 每个端点的并发上限：一个跑飞的客户端不该把连接池和 CPU 吃满。
_MAX_CONCURRENCY = 4
_semaphores: dict[str, asyncio.Semaphore] = {}

#: ``tools/list`` 的缓存提示：工具面只在管理员改配置时才变，五分钟内不必重复拉。
#: 作用域是 private——每个端点的工具面不同，共享缓存会串味。
_TOOLS_TTL_MS = 5 * 60 * 1000


def _semaphore(endpoint_id: str) -> asyncio.Semaphore:
    if endpoint_id not in _semaphores:
        _semaphores[endpoint_id] = asyncio.Semaphore(_MAX_CONCURRENCY)
    return _semaphores[endpoint_id]


async def _send_json(
    send: Send,
    status: int,
    payload: dict[str, Any],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json; charset=utf-8"), *(headers or [])],
    })
    await send({"type": "http.response.body", "body": body})


async def _allowed_origins(host_header: str) -> set[str]:
    """本端点认可的 Origin 集合。

    浏览器发起的跨源请求一定带 Origin，DNS 重绑定攻击也就一定带；而 Claude Code、
    Cursor 这类原生客户端不带 Origin。所以「无 Origin 放行、有 Origin 必须匹配」
    既挡住了攻击，又不会误伤正常客户端。

    刻意**不用** SDK 自带的 Host 白名单：它的空白名单会拒绝一切，自部署用户从局域网
    IP 访问就会撞上 421，而 Host 头本身在我们这里没有额外的安全价值——真正的闸是
    端点令牌，攻击者的页面拿不到它。
    """
    origins = {f"http://{host_header}", f"https://{host_header}"}
    for host in ("localhost", "127.0.0.1"):
        origins.update({f"http://{host}", f"https://{host}"})
    external = (await get_setting_store().get(AppServerSetting)).external_url.strip()
    if external:
        parts = urlsplit(external)
        if parts.scheme and parts.netloc:
            origins.add(f"{parts.scheme}://{parts.netloc}")
    return origins


def build_server(app: FastAPI, endpoint: McpEndpoint) -> Server:
    """为一个端点构造 SDK 的低阶 Server：工具面与调度都回调进我们自己的代码。"""

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: Any
    ) -> ListToolsResult:
        return ListToolsResult(
            tools=build_tools(endpoint.services, expand=endpoint.expand_tools),
            ttl_ms=_TOOLS_TTL_MS,
            cache_scope="private",
        )

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        try:
            operation, arguments = resolve_tool(
                endpoint.services,
                expand=endpoint.expand_tools,
                name=params.name,
                arguments=dict(params.arguments or {}),
            )
        except ValueError as exc:
            # 定位不到工具/命令是模型可以自己纠正的错误，回成结果而不是抛协议错误
            return CallToolResult(
                content=[TextContent(type="text", text=str(exc))], is_error=True
            )

        async with _semaphore(endpoint.id):
            try:
                result = await call_operation(
                    app, endpoint.id, operation, arguments,
                    timeout=float(endpoint.timeout_seconds),
                )
            except ValueError as exc:  # 参数拼装失败（缺必填、参数名不认识）
                return CallToolResult(
                    content=[TextContent(type="text", text=str(exc))], is_error=True
                )
            except TimeoutError:
                return CallToolResult(
                    content=[TextContent(
                        type="text",
                        text=f"调用超时（{endpoint.timeout_seconds} 秒）。长任务应改用提交后"
                             "轮询的命令，或让管理员调大这个端点的超时。",
                    )],
                    is_error=True,
                )
        await mcp_endpoints.touch(endpoint.id)
        return result

    return Server(
        f"movieclaw/{endpoint.slug}",
        version=__version__,
        title=endpoint.name or endpoint.slug,
        instructions=(
            "movieclaw 是一套自部署的影视媒体管理系统：发现影视、订阅追更、搜索并下载 PT "
            "资源、整理入库、管理媒体库与播放。这里的工具直接操作用户自己的实例，"
            "改动会立刻生效——涉及删除的操作先向用户确认。"
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _slug_of(scope: Scope) -> str:
    """从请求路径里取出端点标识。

    两种 ASGI 形态都要成立：老一些的 Starlette 把挂载前缀从 ``path`` 里截掉，
    新版则保留完整 ``path`` 而把前缀放进 ``root_path``。只认其中一种，换个版本
    就会全线 404——这类静默失效比报错难查得多。
    """
    path = scope.get("path", "")
    root = scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root):]
    return path.strip("/")


class McpDispatcher:
    """``/mcp/<slug>`` 的 ASGI 入口。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        slug = _slug_of(scope)

        origin = headers.get("origin")
        if origin and origin not in await _allowed_origins(headers.get("host", "")):
            logger.warning("拒绝来源不明的 MCP 请求：origin=%s slug=%s", origin, slug)
            await _send_json(send, 403, {"error": "请求来源不被允许"})
            return

        if scope["method"] != "POST":
            # 现行 MCP 传输只用 POST：旧客户端的 GET 长连接与 DELETE 会话终止一律 405
            await _send_json(
                send, 405, {"error": "MCP 端点只接受 POST"}, [(b"allow", b"POST")]
            )
            return

        endpoint = await mcp_endpoints.lookup_endpoint(slug) if slug else None
        if endpoint is None:
            await _send_json(send, 404, {"error": "MCP 端点不存在或已停用"})
            return

        scheme, _, raw_token = headers.get("authorization", "").partition(" ")
        token = raw_token.strip()
        if scheme.lower() != "bearer" or not mcp_endpoints.verify_token(endpoint, token):
            await _send_json(
                send, 401, {"error": "令牌无效或已轮换，请向管理员索取新的端点令牌"},
                [(b"www-authenticate", b'Bearer realm="movieclaw-mcp"')],
            )
            return

        server = build_server(self._app, endpoint)
        manager = StreamableHTTPSessionManager(
            app=server,
            json_response=True,   # 恒用单个 JSON 体应答，不开 SSE 流（设计文档 §4.8）
            stateless=True,       # 无会话：每个请求自带全部上下文
            # Host/Origin 由本调度层在上面判过了（语义更准、也不会误伤局域网访问），
            # 这里关掉 SDK 自带的那道，避免两套白名单各判各的。
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)


def register(app: FastAPI) -> None:
    """把 MCP 调度层挂到应用上（根命名空间，不进业务 OpenAPI）。"""
    app.mount("/mcp", McpDispatcher(app))
