#!/usr/bin/env python3
"""MCP 端点的连通性自检：用官方客户端连一个真实端点，列工具并调一次。

用法（地址与令牌从「设置 → MCP 服务」拿）：

    python scripts/mcp_client_demo.py http://127.0.0.1:8099/mcp/home mcp_xxxxxxxx
    python scripts/mcp_client_demo.py <url> <token> --call subscriptions_list --args '{"limit": 5}'

它做三件事，任何一步失败都会指出是哪一环的问题：

1. **握手**（server/discover）——确认协议版本与服务端身份；
2. **列工具**（tools/list）——确认这个端点开放了什么，以及工具定义有多大；
3. **调一次工具**（tools/call）——确认调用真的落到了业务接口上。

依赖官方 SDK（``pip install mcp``）。它默认走 ``mode="auto"``：先按现行协议
（2026-07-28）发 server/discover，服务端不认才回落到旧代握手——所以这个脚本
同时也验证了「新旧两代都能接」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager

from mcp.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


@asynccontextmanager
async def authed_transport(url: str, token: str):
    """带 Bearer 头的 Streamable HTTP 传输。

    SDK 的 ``StreamableHTTPTransport`` 自身不收请求头，但 ``Transport`` 本质就是
    「产出读写流的异步上下文管理器」，所以这里自带一个配好头的 httpx 客户端即可——
    全是公开 API，不碰私有实现。
    """
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with streamable_http_client(url, http_client=http_client) as streams:
        yield streams


def _preview(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字符）"


async def main(url: str, token: str, call: str | None, arguments: dict) -> int:
    print(f"→ 连接 {url}")
    async with Client(authed_transport(url, token)) as client:
        info = client.server_info
        print(f"✓ 握手成功：协议 {client.protocol_version}"
              + (f" · 服务端 {info.name} {info.version}" if info else ""))

        result = await client.list_tools()
        print(f"✓ tools/list：{len(result.tools)} 个工具"
              + (f" · 缓存提示 ttlMs={result.ttl_ms} scope={result.cache_scope}"
                 if result.ttl_ms else ""))
        for tool in result.tools[:8]:
            marks = []
            if tool.annotations and tool.annotations.read_only_hint:
                marks.append("只读")
            if tool.annotations and tool.annotations.destructive_hint:
                marks.append("破坏性")
            flag = f" [{'/'.join(marks)}]" if marks else ""
            print(f"    {tool.name}{flag} — {(tool.description or '').splitlines()[0][:60]}")
        if len(result.tools) > 8:
            print(f"    …… 还有 {len(result.tools) - 8} 个")

        target = call or result.tools[0].name
        print(f"\n→ tools/call {target} {json.dumps(arguments, ensure_ascii=False)}")
        called = await client.call_tool(target, arguments)
        status = "失败" if called.is_error else "成功"
        print(f"✓ 调用{status}")
        for block in called.content:
            if getattr(block, "text", None):
                print("    " + _preview(block.text).replace("\n", "\n    "))
        if called.structured_content:
            keys = list(called.structured_content)
            print(f"    structuredContent 字段：{keys}")
        return 1 if called.is_error else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="连一个 movieclaw MCP 端点做连通性自检")
    parser.add_argument("url", help="端点地址，如 http://127.0.0.1:8099/mcp/home")
    parser.add_argument("token", help="端点令牌（mcp_ 开头）")
    parser.add_argument("--call", help="要调用的工具名；缺省调列表里的第一个")
    parser.add_argument("--args", default="{}", help="工具参数（JSON 对象）")
    ns = parser.parse_args()
    raise SystemExit(asyncio.run(main(ns.url, ns.token, ns.call, json.loads(ns.args))))
