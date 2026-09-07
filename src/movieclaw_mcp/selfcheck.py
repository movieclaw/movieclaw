"""端点自检：这个端点现在到底能不能用（docs/design/mcp-server.md §7）。

管理页配完端点，用户此前只能去开一个客户端试——试不通也不知道断在哪一环。
这里就地把整条链路走一遍，用的是**真实的协议往返**（SDK 客户端 + 内存传输，
不发网络请求），因此能覆盖三件各自会独立坏掉的事：

1. **协议层**：SDK 装配是否正常、握手协商到哪个版本；
2. **工具面**：这个端点的服务集合是否真能渲染出工具（域消失、schema 生成出错
   都会在这里现形）；
3. **调度链路**：随便挑一个只读工具真调一次，确认参数拼装、内部令牌签发、
   打到本机 API 这一串是通的。

刻意不校验令牌：调用方是已经过管理员鉴权的管理面，令牌是给外部客户端的闸，
在这里插一道只会让「配好了但自检说不通」这种假警报出现。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mcp.client import Client

from movieclaw_api.settings.mcp import McpEndpoint

logger = logging.getLogger("movieclaw_mcp.selfcheck")


@dataclass
class SelfCheckResult:
    ok: bool
    protocol_version: str = ""
    tool_count: int = 0
    elapsed_ms: int = 0
    #: 试调的那个工具：(工具名, 是否成功, 结果摘要)
    probe_tool: str = ""
    probe_ok: bool = False
    probe_message: str = ""
    message: str = ""
    #: 自检通过、但仍会影响外部客户端接入的情况（如没配外部地址）
    warnings: list[str] = field(default_factory=list)


def _pick_probe_tool(tools: list[Any]) -> Any | None:
    """挑一个拿来试调的工具：只读、且不需要必填参数。

    只读是为了自检本身不改动任何状态；无必填参数是因为我们不该替用户编造 id。
    找不到就只验证到工具面这一层——这不算失败，只是没能验到调度链路。
    """
    for tool in tools:
        required = (tool.input_schema or {}).get("required") or []
        if tool.annotations and tool.annotations.read_only_hint and not required:
            return tool
    return None


async def self_check(app: Any, endpoint: McpEndpoint, *, external_url: str = "") -> SelfCheckResult:
    """跑一次自检。异常一律收敛成结果对象——自检本身不该把管理页打挂。"""
    from movieclaw_mcp.app import build_server

    started = time.perf_counter()
    warnings: list[str] = []
    if not endpoint.enabled:
        warnings.append("端点当前已停用，外部客户端会收到 404。")
    if not external_url:
        warnings.append("还没配置外部访问地址，端点地址只有相对路径，外部客户端连不上。")
    if endpoint.missing_services if hasattr(endpoint, "missing_services") else False:
        warnings.append("配置里有当前版本已不存在的服务，已被忽略。")

    try:
        async with Client(build_server(app, endpoint)) as client:
            listed = await client.list_tools()
            protocol = str(client.protocol_version or "")
            probe = _pick_probe_tool(listed.tools)
            result = SelfCheckResult(
                ok=True,
                protocol_version=protocol,
                tool_count=len(listed.tools),
                warnings=warnings,
                message="协议握手与工具面正常",
            )
            if probe is not None:
                called = await client.call_tool(probe.name, {})
                text = next((b.text for b in called.content if getattr(b, "text", None)), "")
                result.probe_tool = probe.name
                result.probe_ok = not called.is_error
                result.probe_message = text[:160]
                if called.is_error:
                    result.ok = False
                    result.message = f"工具 {probe.name} 调用失败"
                else:
                    result.message = "协议、工具面与调用链路都正常"
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "MCP 端点自检 slug=%s ok=%s 工具=%d 用时=%dms",
                endpoint.slug, result.ok, result.tool_count, result.elapsed_ms,
            )
            return result
    except Exception as exc:  # noqa: BLE001 - 自检要如实报出任何一环的故障
        logger.warning("MCP 端点自检失败 slug=%s：%s", endpoint.slug, exc, exc_info=True)
        return SelfCheckResult(
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            message=f"自检未能完成：{exc}",
            warnings=warnings,
        )
