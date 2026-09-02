"""内置工具集 —— Agent 的第一期基础能力：bash / read / write / edit。

模块组织约定（后续扩展工具照此模式）：
- 每个工具一个 make_xxx_tool() 工厂，返回 AgentTool——**声明（参数 schema +
  给模型看的提示词）与执行器绑在同一处**，改行为时提示词与实现不会漂移；
- 工厂接收 workdir（工作目录）等运行时依赖，不读全局状态，便于测试注入；
- handler 里用 raise ValueError(中文说明) 表达业务失败，runner 会把异常
  转成失败结果回喂模型（不中断 loop）。
"""

from __future__ import annotations

from pathlib import Path

from movieclaw_agent.toolkit import AgentTool
from movieclaw_agent.tools.bash import make_bash_tool
from movieclaw_agent.tools.files import make_edit_tool, make_read_tool, make_write_tool
from movieclaw_agent.tools.mclaw import make_mclaw_tool
from movieclaw_agent.tools.media_ui import make_media_ui_tool


def builtin_tools(workdir: Path | None = None) -> list[AgentTool]:
    """构建内置工具集（bash/read/write/edit）。workdir 是 bash 的 cwd 与
    相对路径的解析基准。

    注意：产品操作的授权令牌**不在这里注入**——bash 环境拿不到凭证是刻意
    设计（泄漏面收窄），mclaw 专用工具由 make_mclaw_tool 单独构建并携带令牌。
    """
    wd = (workdir or Path.cwd()).resolve()
    return [
        make_bash_tool(wd),
        make_read_tool(wd),
        make_write_tool(wd),
        make_edit_tool(wd),
    ]


__all__ = ["AgentTool", "builtin_tools", "make_mclaw_tool", "make_media_ui_tool"]
