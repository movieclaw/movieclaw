"""工具面渲染：端点的服务集合 × 展开开关 → MCP 工具清单（设计文档 §4.1）。

两种形态，共用同一份操作元数据（catalog.py），因此「列出来的」与「能调的」
永远一致：

- **展开**（默认）：一条命令一个工具，参数从 spec 生成 JSON Schema。
  工具名就是 ``operation_id`` 把点号与连字符换成下划线。
- **折叠**：一个服务一个工具，参数是 ``command`` 枚举 + ``params`` 对象；
  命令清单与每条命令的字段写进 description，模型据此填 params。

排序一律按工具名字典序：``tools/list`` 的顺序确定，客户端缓存与提示词缓存
才有命中的可能（MCP 规范的 SHOULD）。
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations

from movieclaw_api.services.mclaw_tool import domain_description
from movieclaw_mcp.catalog import Operation, operations_by_domain

#: Agent 服务目录里没有的域的一行说明。
#: ``mclaw_tool._DOMAIN_LINES`` 只覆盖对 Agent 开放的域，fs / logs / members 被它显式
#: 排除（理由是 Agent 专属的：Agent 有 bash，不需要 logs 与 fs；建号改密不该由对话代劳），
#: 而 MCP 端点由管理员逐个勾选、勾了就是明示授权，两者都开放。守护测试禁止往
#: ``_DOMAIN_LINES`` 里塞被排除的域，所以补充文案落在这里。
_EXTRA_DOMAIN_LINES = {
    "fs": "服务器目录浏览（列出某个目录下有哪些子目录，配媒体库根路径时确认路径）",
    "logs": "系统日志（按天查看后端运行日志，排查故障用）",
    "members": "家庭成员账号（建号、改能力开关与可见范围、重置密码、启停）"
                "——注意这是账号治理面，开放前想清楚",
}


def describe_domain(domain: str) -> str:
    """服务域的一行说明；管理页的服务卡片与折叠模式的工具描述共用这一份。"""
    return (
        domain_description(domain)
        or _EXTRA_DOMAIN_LINES.get(domain)
        or f"movieclaw 的 {domain} 服务"
    )


#: 提交后台任务的操作，在描述里补一句怎么跟进——直连 API 不像 CLI 那样会替你
#: 等待，模型必须知道下一步是 jobs_wait（设计文档 §4.2）。
_JOB_HINT = "（提交后立即返回 job_id，用 jobs_wait 跟进进度）"

#: 危险操作在描述里也明说一次。注解（destructiveHint）只有部分客户端会读，
#: 而描述是模型一定会看到的。
_DANGEROUS_HINT = {
    "destructive": "⚠ 破坏性操作：会删除数据或磁盘文件，执行前先向用户确认。",
    "confirm": "⚠ 需要谨慎：会清除配置或记录，执行前先向用户确认。",
}


def _annotations(op: Operation) -> ToolAnnotations:
    """从 spec 推导工具注解——支持注解的客户端会据此在执行前向人确认。"""
    return ToolAnnotations(
        read_only_hint=op.read_only,
        destructive_hint=op.dangerous == "destructive",
        idempotent_hint=op.method in ("put", "delete"),
        # 这些域的数据来自外部世界（TMDB / 豆瓣 / PT 站点），结果不由本机状态决定
        open_world_hint=op.domain in ("discover", "search", "site", "net"),
    )


def _tool_description(op: Operation) -> str:
    parts = [op.summary or op.operation_id]
    if hint := _DANGEROUS_HINT.get(op.dangerous or ""):
        parts.append(hint)
    if op.is_job:
        parts.append(_JOB_HINT)
    if op.description:
        parts.append(op.description)
    return " ".join(parts)


def _input_schema(op: Operation) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(op.arg_schemas),
        # 不允许多余字段：模型编出一个不存在的参数时，客户端侧就能拦下来，
        # 而不是发到服务端换回一个 422。
        "additionalProperties": False,
    }
    if op.required:
        schema["required"] = list(op.required)
    if op.defs:
        schema["$defs"] = op.defs
    return schema


def _expanded_tools(domains: list[str]) -> list[Tool]:
    tools: list[Tool] = []
    by_domain = operations_by_domain()
    for domain in domains:
        for op in by_domain.get(domain, ()):
            tools.append(
                Tool(
                    name=op.tool_name,
                    description=_tool_description(op),
                    input_schema=_input_schema(op),
                    annotations=_annotations(op),
                )
            )
    return sorted(tools, key=lambda t: t.name)


def _command_line(op: Operation) -> str:
    """折叠模式描述里的一行：命令名 + 摘要 + 参数名（必填带 *）。"""
    args = ", ".join(
        f"{name}*" if name in op.required else name for name in op.arg_locations
    )
    danger = " ⚠" if op.dangerous else ""
    return f"  {op.command}{danger} — {op.summary or op.operation_id}" + (
        f"（{args}）" if args else ""
    )


def _collapsed_tools(domains: list[str]) -> list[Tool]:
    tools: list[Tool] = []
    by_domain = operations_by_domain()
    for domain in domains:
        ops = by_domain.get(domain, ())
        if not ops:
            continue
        headline = describe_domain(domain)
        lines = [headline, "", "可用命令（params 里填对应字段）："]
        lines.extend(_command_line(op) for op in ops)
        if any(op.dangerous for op in ops):
            lines.append("带 ⚠ 的命令会删除数据或配置，执行前先向用户确认。")
        if any(op.is_job for op in ops):
            lines.append("提交后台任务的命令立即返回 job_id，用 jobs 服务的 wait 跟进。")
        tools.append(
            Tool(
                name=domain,
                description="\n".join(lines),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "enum": [op.command for op in ops],
                            "description": "要执行的命令，取值见上面的清单",
                        },
                        "params": {
                            "type": "object",
                            "description": "该命令的参数，字段名见清单里括号内的列表",
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                annotations=ToolAnnotations(
                    read_only_hint=all(op.read_only for op in ops),
                    destructive_hint=any(op.dangerous == "destructive" for op in ops),
                    open_world_hint=domain in ("discover", "search", "site", "net"),
                ),
            )
        )
    return sorted(tools, key=lambda t: t.name)


def build_tools(services: list[str], *, expand: bool) -> list[Tool]:
    """按端点配置渲染工具清单。未知的服务域静默忽略（升级后某域消失不至于让端点整个坏掉）。"""
    known = operations_by_domain()
    domains = sorted({s for s in services if s in known})
    return _expanded_tools(domains) if expand else _collapsed_tools(domains)


def resolve_tool(
    services: list[str], *, expand: bool, name: str, arguments: dict[str, Any]
) -> tuple[Operation, dict[str, Any]]:
    """``tools/call`` 的入参 → (目标操作, 扁平参数)。

    两种模式的差别只在这一步：展开模式工具名即操作，折叠模式要从 ``command``
    再定位一次。定位失败一律抛 ValueError，由调度层转成 MCP 错误结果。
    """
    known = operations_by_domain()
    domains = {s for s in services if s in known}
    if expand:
        for domain in domains:
            for op in known[domain]:
                if op.tool_name == name:
                    return op, dict(arguments)
        raise ValueError(f"这个端点没有名为 {name} 的工具")

    if name not in domains:
        raise ValueError(f"这个端点没有名为 {name} 的服务")
    command = arguments.get("command")
    if not command:
        raise ValueError("缺少 command 参数：要执行的命令名，取值见工具描述里的清单")
    for op in known[name]:
        if op.command == command:
            params = arguments.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params 必须是一个对象（命令的参数字典）")
            return op, dict(params)
    raise ValueError(f"服务 {name} 没有名为 {command} 的命令，可用命令见工具描述")
