"""movieclaw 的 MCP 服务端（docs/design/mcp-server.md）。

把产品的服务目录以 Model Context Protocol 开放给外部 AI 客户端：管理员在设置页
建端点、勾服务，端点就有那些工具。协议编解码交给官方 SDK，我们只做三件事——
工具面渲染（tools.py）、调用调度（dispatch.py）、以及地址与鉴权（app.py）。

与产品内 Agent 的关系：两条独立的路。Agent 走 mclaw 命令行，MCP 直连本机 API，
互不依赖，共用的只有 spec 这一份事实来源。
"""

from movieclaw_mcp.app import register

__all__ = ["register"]
