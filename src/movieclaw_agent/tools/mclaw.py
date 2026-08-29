"""mclaw 工具：模型操作 movieclaw 产品本身的唯一入口。

设计（docs/design/agent-cli-integration.md）：
- 独立工具而非 bash 捎带——模型在「选工具」层直接看见产品操作入口，
  description 携带一级服务目录（由 API 层从 spec 渲染后传入，防漂移）；
- 参数面最小：args 单字符串 + 可选 timeout。mclaw 自身就是完整的参数体系
  （--help 齐全、错误带 hint），工具层不重复建模；
- shlex 解析后以 argv 直接执行，不经 shell——无管道/变量展开，注入面为零；
  令牌只注入本工具子进程，bash 环境里拿不到（泄漏面收窄）；
- handler 硬闸：会话开始/继续/重试/跟随（递归）、login/logout 与 --server 直接拒绝；
- 退出码语义标注：结果尾部按退出码契约附一行中文解读，模型从工具结果
  本身就能学会正确的下一步（运行时教学，不占 description）。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from pathlib import Path

from movieclaw_agent.toolkit import AgentTool
from movieclaw_agent.tools.bash import _truncate_tail
from movieclaw_llm import ToolDefinition

_DEFAULT_TIMEOUT = 300.0

#: mclaw 二进制的位置。镜像里固定装在 /usr/local/bin/mclaw；
#: 开发机上用 MOVIECLAW_CLI_BIN 指向 `go build` 的产物即可。
_CLI_BIN_ENV = "MOVIECLAW_CLI_BIN"
_DEFAULT_CLI_BIN = "/usr/local/bin/mclaw"


def _cli_binary() -> str:
    """定位 mclaw 可执行文件。

    找不到时给出可读结论：这是部署产物不完整（镜像没带 CLI），不是模型
    用错了参数——错误文案要让自部署用户知道该重新部署而不是改配置。
    """
    path = os.environ.get(_CLI_BIN_ENV) or _DEFAULT_CLI_BIN
    if not Path(path).is_file():
        found = shutil.which("mclaw")
        if found:
            return found
        raise ValueError(
            f"找不到 mclaw 可执行文件（{path}）。这通常是镜像或安装包不完整，"
            f"请更新到新版镜像后重新部署；开发环境可用 {_CLI_BIN_ENV} 指定路径。"
        )
    return path

# 退出码 → 给模型的下一步指引（与 cli/internal/clierr 的退出码契约一致）
_EXIT_CODE_NOTES = {
    1: "业务错误——原因与建议见 stderr",
    2: "用法错误——先执行对应命令的 --help 纠正参数再试",
    3: "授权失效——不要重试，直接向用户报告",
    4: "无法连接服务器——网络问题，可稍后重试",
    5: "该操作需要确认——核对无误后在参数末尾加 --yes 重跑",
    6: "等待超时，任务仍在后台执行——用对应查询命令轮询进度",
    7: "结果有歧义——stdout 是候选清单，选定后按 stderr 提示重跑",
}

# 硬闸：这些子命令在工具里没有任何合法用途
_BLOCKED = {
    "login": "授权已自动配置，不需要也不允许执行 login/logout（会破坏凭证状态）。",
    "logout": "授权已自动配置，不需要也不允许执行 login/logout（会破坏凭证状态）。",
}

_PROTOCOL = """\
movieclaw 的官方命令行工具。用于从 TMDB 和豆瓣发现实时热点、最新、热映/在播、热门和\
高分电影/剧集，搜索并下载 PT 资源，持续订阅追更并自动整理入库，管理本地媒体库；也可\
查看任务进度，以及配置资源站点、下载器、规则、消息渠道、AI 模型、网络和应用更新。查询\
或变更 movieclaw 产品状态都用本工具完成（不要通过 bash 调用，bash 环境没有授权）。授权\
已自动配置，永远不需要 login。

{service_map}

使用协议：
- 常用链路：search titles 找片/找剧 → subscriptions create 订阅；search torrents 搜 PT 种子 → \
download 投递；search library-items 查已有库存，discover 浏览榜单，library 管理已入库内容；\
订阅会持续追踪，并在出现符合规则的新资源后自动搜索、下载和整理入库。
- 输出即数据：stdout 是 JSON（默认），stderr 是过程提示与错误原因。
- 参数拿不准就先 --help（域级与命令级都有，含示例），不要凭记忆猜参数或取值。
- 列表默认有条数上限、长字段有截断；下结论前确认数据没有被截断（--limit 可调）。
- 带 ⚠ 的命令需要 --yes 确认。其中 library items delete 会删除磁盘上的媒体文件：\
必须先用只读命令查清将删除的具体条目、向用户复述并取得本轮明确同意后才能执行；\
用户泛泛说「清理/整理」不构成删除文件的同意。其余 ⚠ 命令（删配置、清记录）在用户\
任务明确要求时可直接 --yes。
- 扫描/整理/元数据刷新默认阻塞等待完成；预计超过 4 分钟的任务用 --no-wait 启动后\
轮询进度，或调大本工具的 timeout 参数。"""


def build_description(service_map: str) -> str:
    """组装完整工具描述：静态使用协议 + 动态一级服务目录。"""
    return _PROTOCOL.format(service_map=service_map.strip())


def make_mclaw_tool(
    workdir: Path,
    extra_env: dict[str, str],
    service_map: str,
) -> AgentTool:
    """构建 mclaw 工具。

    extra_env：MOVIECLAW_SERVER / MOVIECLAW_TOKEN——只进本工具的子进程；
    service_map：一级服务目录文本，由调用方从 spec 渲染（见
    movieclaw_api.services.mclaw_tool），保证与真实命令面同版。
    """

    async def handler(args: dict) -> str:
        raw: str = args["args"]
        timeout = float(args.get("timeout") or _DEFAULT_TIMEOUT)
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            raise ValueError(f"参数串无法解析（引号不配对？）：{exc}") from None
        if not argv:
            raise ValueError('args 不能为空；例如 args="subscriptions list" 或 args="--help"')
        blocked_action = next((arg for arg in argv if arg in _BLOCKED), None)
        if note := _BLOCKED.get(blocked_action or ""):
            raise ValueError(note)
        if any(arg == "--server" or arg.startswith("--server=") for arg in argv):
            raise ValueError("不允许覆盖服务器地址；mclaw 已绑定当前 movieclaw 实例")
        session_action = next(
            (
                argv[index + 1]
                for index, arg in enumerate(argv[:-1])
                if arg == "session"
            ),
            None,
        )
        if session_action in {"start", "retry", "follow"}:
            raise ValueError(
                f"不能在工具里调用 session {session_action}（禁止递归或嵌套跟随）；"
                "请直接在当前会话完成任务"
            )
        full_transcript = session_action == "get-transcript"

        proc = await asyncio.create_subprocess_exec(
            _cli_binary(),
            *argv,
            cwd=workdir,
            env={**os.environ, **extra_env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ValueError(
                f"命令执行超时（{timeout:.0f} 秒），已终止；"
                "长任务可用 --no-wait 启动后轮询，或调大 timeout 参数"
            ) from None

        sections: list[str] = []
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        if stdout.strip():
            sections.append(stdout if full_transcript else _truncate_tail(stdout, label="stdout"))
        if stderr.strip():
            sections.append("[stderr]\n" + _truncate_tail(stderr, label="stderr"))
        code = proc.returncode or 0
        if code != 0:
            note = _EXIT_CODE_NOTES.get(code, "")
            sections.append(f"[退出码 {code}{'：' + note if note else ''}]")
        return "\n".join(sections) or "（命令无输出，退出码 0）"

    return AgentTool(
        definition=ToolDefinition(
            name="mclaw",
            description=build_description(service_map),
            parameters={
                "type": "object",
                "properties": {
                    "args": {
                        "type": "string",
                        "description": "mclaw 后面的完整参数串（不含 mclaw 本身），"
                        "如 'discover list-collections --media-type movie --provider tmdb'、"
                        "'subscriptions list' 或 "
                        "'search torrents \"沙丘2\" --resolution 2160p'",
                    },
                    "timeout": {
                        "type": "number",
                        "description": f"超时秒数（可选，默认 {_DEFAULT_TIMEOUT:.0f}；"
                        "等待长任务时适当调大）",
                    },
                },
                "required": ["args"],
            },
        ),
        handler=handler,
    )
