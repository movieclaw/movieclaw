"""工具调用 → 本机 API（docs/design/mcp-server.md §4.2）。

一次 ``tools/call`` 的全过程都在这里：把模型给的扁平参数按 spec 的落点拼成
path / query / body，现签一枚短时令牌，走**进程内 ASGI**打到自己的 FastAPI 上，
再把统一响应体整形成 ``CallToolResult``。

为什么走 ASGITransport 而不是直接调处理器函数：这样请求会完整经过既有的鉴权、
参数校验、中间件与统一错误体——**授权判定只此一份**，MCP 不开后门。开销是进程内
函数调用级别，没有网络跳。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from mcp.types import CallToolResult, TextContent

from movieclaw_api.services import auth as auth_service
from movieclaw_mcp.catalog import Operation

logger = logging.getLogger("movieclaw_mcp.dispatch")

#: 单次结果回给模型的字节上限。媒体库这类列表接口动辄上万条，直连没有 CLI 那层
#: 截断，必须自己兜住——否则一次调用就能把客户端的上下文撑爆。
MAX_RESULT_BYTES = 24_000

#: 进程内调用的基址。ASGITransport 不发真实网络请求，host 只用于填 Host 头。
_BASE_URL = "http://mcp.internal"


def build_request(
    op: Operation, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any], Any]:
    """扁平参数 → (路径, query, body)。缺必填路径参数直接报错，不发出去试。"""
    path = op.path
    query: dict[str, Any] = {}
    body: dict[str, Any] = {}

    unknown = set(arguments) - set(op.arg_locations)
    if unknown:
        raise ValueError(
            f"这些参数不属于 {op.tool_name}：{', '.join(sorted(unknown))}；"
            f"可用参数：{', '.join(op.arg_locations) or '（无）'}"
        )

    for name, location in op.arg_locations.items():
        if name not in arguments:
            if name in op.required and location == "path":
                raise ValueError(f"缺少必填参数 {name}")
            continue
        value = arguments[name]
        if location == "path":
            path = path.replace("{" + name + "}", str(value))
        elif location == "query":
            # 布尔要转成 FastAPI 认的字面量；None 一律不发（等价于「不传」）
            if value is None:
                continue
            query[name] = str(value).lower() if isinstance(value, bool) else value
        else:
            body[name] = value

    if "{" in path:
        missing = [
            n for n, loc in op.arg_locations.items()
            if loc == "path" and n not in arguments
        ]
        raise ValueError(f"缺少必填的路径参数：{', '.join(missing)}")

    # 非对象请求体（catalog 里记成单个 body 参数）原样发出
    body_slots = list(op.arg_locations.values()).count("body")
    if body_slots == 1 and "body" in op.arg_locations:
        return path, query, arguments.get("body")
    return path, query, (body or None)


def _truncate(text: str) -> tuple[str, bool]:
    """超长结果截断，并明确告诉模型下一步怎么拿全（而不是让它以为数据就这些）。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_RESULT_BYTES:
        return text, False
    clipped = encoded[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
    return (
        clipped
        + "\n\n…（结果过长已截断。这不是全部数据：用 limit / offset 参数分页，"
        "或加上更精确的过滤条件后重试）",
        True,
    )


async def call_operation(
    app: Any,
    endpoint_id: str,
    op: Operation,
    arguments: dict[str, Any],
    *,
    timeout: float,
) -> CallToolResult:
    """执行一次工具调用，返回 MCP 结果。

    错误一律以 ``is_error=True`` 的**结果**返回而不是抛异常：模型看得到原因才能
    自己纠正（少传了参数、id 不存在、权限不足），而抛异常只会变成一句协议层错误。
    """
    path, query, body = build_request(op, arguments)
    token = await auth_service.issue_mcp_token(endpoint_id)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url=_BASE_URL, timeout=timeout
    ) as client:
        response = await client.request(
            op.method.upper(),
            path,
            params=query or None,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text[:500]}

    logger.info(
        "MCP 工具调用 endpoint=%s tool=%s %s %s → %s",
        endpoint_id, op.tool_name, op.method.upper(), path, response.status_code,
    )

    if response.status_code >= 400:
        # 统一错误体的 message 本来就是写给非开发者看的中文，原样回给模型最有用
        message = payload.get("message") or f"请求失败（HTTP {response.status_code}）"
        details = payload.get("details")
        text = message if not details else f"{message}\n{json.dumps(details, ensure_ascii=False)}"
        return CallToolResult(
            content=[TextContent(type="text", text=_truncate(text)[0])],
            structured_content=payload if isinstance(payload, dict) else None,
            is_error=True,
        )

    data = payload.get("data") if isinstance(payload, dict) else payload
    text, truncated = _truncate(json.dumps(data, ensure_ascii=False, indent=None))
    result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        # 结构化结果只在没被截断时给：截断过的 JSON 已经不是合法数据了，
        # 塞进 structured_content 会让客户端拿到一个残缺对象还以为是完整的。
        structured_content=(
            {"data": data} if not truncated and isinstance(data, (dict, list)) else None
        ),
        is_error=False,
    )
    return result
