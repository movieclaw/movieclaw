"""bash 工具：在工作目录执行 shell 命令。

落地细节（按参考提示词推定）：
- 输出截断：保留**末尾** 2000 行或 50KB（先到为准）——命令输出的结论
  通常在尾部；被截断时完整输出落盘到临时文件，路径附在结果里，模型可
  继续用 read 工具翻阅；
- 超时：参考定义为「可选、无默认」，但无限挂起会卡死整个 agent 运行，
  这里给 300 秒默认上限，模型可用 timeout 参数覆盖（提示词如实描述）；
- stdout / stderr 分开呈现，非零退出码显式标注——退出码非零不算工具
  失败（is_error），命令「跑完但失败」是模型该阅读的观察结果。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import tempfile
from pathlib import Path

from movieclaw_agent.toolkit import AgentTool
from movieclaw_llm import ToolDefinition

_MAX_LINES = 2000
_MAX_BYTES = 50_000
_DEFAULT_TIMEOUT = 300.0

_DESCRIPTION = (
    "在工作目录执行 bash 命令，返回 stdout 与 stderr。"
    f"输出超过 {_MAX_LINES} 行或 50KB 时只保留末尾部分，"
    "完整输出会保存到临时文件（路径附在结果中，可用 read 工具查看）。"
    f"默认超时 {_DEFAULT_TIMEOUT:.0f} 秒，可用 timeout 参数覆盖。"
)


def make_bash_tool(workdir: Path, extra_env: dict[str, str] | None = None) -> AgentTool:
    """extra_env：附加到子进程环境的变量，与进程环境合并、同名覆盖。

    产品授权令牌刻意**不**走这里（bash 里 `env` 不该看到凭证）——
    mclaw 操作有专用工具（tools/mclaw.py），令牌只注入那边的子进程。"""

    async def handler(args: dict) -> str:
        command: str = args["command"]
        timeout = float(args.get("timeout") or _DEFAULT_TIMEOUT)
        proc = await _spawn_reapable(
            asyncio.create_subprocess_shell(
                command,
                cwd=workdir,
                env={**os.environ, **(extra_env or {})},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 独立进程组：超时/取消时按组击杀，命令派生的子进程（管道、
                # 后台 &、包装脚本）不会成为继续运行的孤儿
                start_new_session=True,
            )
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            _kill_process_group(proc)
            await proc.communicate()
            raise ValueError(f"命令执行超时（{timeout:.0f} 秒），已终止") from None
        except asyncio.CancelledError:
            # 用户停止运行 / 服务停机：不能让子进程留成孤儿继续跑
            _kill_process_group(proc)
            raise

        sections: list[str] = []
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        if stdout.strip():
            sections.append(_truncate_tail(stdout, label="stdout"))
        if stderr.strip():
            sections.append("[stderr]\n" + _truncate_tail(stderr, label="stderr"))
        if proc.returncode != 0:
            sections.append(f"[退出码：{proc.returncode}]")
        return "\n".join(sections) or "（命令无输出，退出码 0）"

    return AgentTool(
        definition=ToolDefinition(
            name="bash",
            description=_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 bash 命令"},
                    "timeout": {
                        "type": "number",
                        "description": f"超时秒数（可选，默认 {_DEFAULT_TIMEOUT:.0f} 秒）",
                    },
                },
                "required": ["command"],
            },
        ),
        handler=handler,
    )


async def _spawn_reapable(factory) -> asyncio.subprocess.Process:
    """启动子进程；创建期间被取消时，进程一旦建成立即整组击杀。

    直接 ``await create_subprocess_*`` 的话，取消恰好落在创建过程中会拿不到
    进程句柄——子进程可能已经 fork 出来，从此无人收割。shield 让创建自身
    完成，取消到达时挂回调兜底击杀。"""
    creation = asyncio.ensure_future(factory)
    try:
        return await asyncio.shield(creation)
    except asyncio.CancelledError:
        creation.add_done_callback(_kill_late_arrival)
        raise


def _kill_late_arrival(creation: asyncio.Future) -> None:
    if not creation.cancelled() and creation.exception() is None:
        _kill_process_group(creation.result())


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL 整个进程组（子进程以 start_new_session 启动，组号即其 pid）。

    只杀 proc 本身会漏掉它派生的子进程；进程已自行退出（ProcessLookupError）
    则视为收割完成。"""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _truncate_tail(text: str, *, label: str) -> str:
    """保留末尾 2000 行 / 50KB；截断时把完整输出落盘并附路径。"""
    lines = text.splitlines()
    truncated = text
    if len(lines) > _MAX_LINES:
        truncated = "\n".join(lines[-_MAX_LINES:])
    if len(truncated.encode(errors="replace")) > _MAX_BYTES:
        truncated = truncated.encode(errors="replace")[-_MAX_BYTES:].decode(errors="replace")
    if truncated is text:
        return text
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", prefix=f"movieclaw-agent-{label}-", delete=False
    ) as f:
        f.write(text)
        full_path = f.name
    return f"（输出过长已截断，仅保留末尾部分；完整输出已保存到 {full_path}）\n" + truncated
