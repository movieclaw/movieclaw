"""内置工具 bash / read / write / edit 的行为测试。"""

from __future__ import annotations

import pytest

from movieclaw_agent.tools import builtin_tools


def toolmap(workdir):
    return {t.name: t for t in builtin_tools(workdir)}


def test_builtin_set_names(tmp_path):
    assert set(toolmap(tmp_path)) == {"bash", "read", "write", "edit"}


# -- write / read ----------------------------------------------------------


async def test_write_then_read_roundtrip(tmp_path):
    tools = toolmap(tmp_path)
    out = await tools["write"].handler({"path": "notes/a.txt", "content": "第一行\n第二行"})
    assert "已创建" in out
    # 相对路径落在工作目录内，父目录自动创建
    assert (tmp_path / "notes" / "a.txt").read_text() == "第一行\n第二行"
    back = await tools["read"].handler({"path": "notes/a.txt"})
    assert back == "第一行\n第二行"


async def test_write_overwrite_reports(tmp_path):
    tools = toolmap(tmp_path)
    await tools["write"].handler({"path": "a.txt", "content": "old"})
    out = await tools["write"].handler({"path": "a.txt", "content": "new"})
    assert "已覆盖" in out


async def test_read_missing_file_raises(tmp_path):
    tools = toolmap(tmp_path)
    with pytest.raises(ValueError, match="文件不存在"):
        await tools["read"].handler({"path": "nope.txt"})


async def test_read_directory_raises(tmp_path):
    tools = toolmap(tmp_path)
    (tmp_path / "sub").mkdir()
    with pytest.raises(ValueError, match="是目录"):
        await tools["read"].handler({"path": "sub"})


async def test_read_image_rejected(tmp_path):
    tools = toolmap(tmp_path)
    (tmp_path / "p.png").write_bytes(b"\x89PNG\r\n")
    with pytest.raises(ValueError, match="不支持读取图片"):
        await tools["read"].handler({"path": "p.png"})


async def test_read_offset_limit_and_continuation(tmp_path):
    tools = toolmap(tmp_path)
    content = "\n".join(f"line{i}" for i in range(1, 11))  # 10 行
    await tools["write"].handler({"path": "big.txt", "content": content})
    out = await tools["read"].handler({"path": "big.txt", "offset": 3, "limit": 2})
    assert "line3\nline4" in out
    # 未读完 → 附继续读的 offset 提示
    assert "offset=5" in out


# -- edit ------------------------------------------------------------------


async def test_edit_multiple_replacements(tmp_path):
    tools = toolmap(tmp_path)
    await tools["write"].handler({"path": "c.py", "content": "a = 1\nb = 2\nc = 3\n"})
    out = await tools["edit"].handler(
        {
            "path": "c.py",
            "edits": [
                {"oldText": "a = 1", "newText": "a = 10"},
                {"oldText": "c = 3", "newText": "c = 30"},
            ],
        }
    )
    assert "2 处替换" in out
    assert (tmp_path / "c.py").read_text() == "a = 10\nb = 2\nc = 30\n"


async def test_edit_non_unique_oldtext_rejected(tmp_path):
    tools = toolmap(tmp_path)
    await tools["write"].handler({"path": "d.txt", "content": "x\nx\ny"})
    with pytest.raises(ValueError, match="匹配到 2 处"):
        await tools["edit"].handler(
            {"path": "d.txt", "edits": [{"oldText": "x", "newText": "z"}]}
        )
    # 校验失败 → 文件保持原样（全有或全无）
    assert (tmp_path / "d.txt").read_text() == "x\nx\ny"


async def test_edit_missing_oldtext_rejected(tmp_path):
    tools = toolmap(tmp_path)
    await tools["write"].handler({"path": "e.txt", "content": "hello"})
    with pytest.raises(ValueError, match="未在文件中找到"):
        await tools["edit"].handler(
            {"path": "e.txt", "edits": [{"oldText": "world", "newText": "z"}]}
        )


async def test_edit_overlapping_rejected(tmp_path):
    tools = toolmap(tmp_path)
    await tools["write"].handler({"path": "f.txt", "content": "abcdef"})
    with pytest.raises(ValueError, match="重叠"):
        await tools["edit"].handler(
            {
                "path": "f.txt",
                "edits": [
                    {"oldText": "abcd", "newText": "X"},
                    {"oldText": "cdef", "newText": "Y"},
                ],
            }
        )


# -- bash ------------------------------------------------------------------


async def test_bash_stdout_and_cwd(tmp_path):
    tools = toolmap(tmp_path)
    (tmp_path / "marker.txt").write_text("")
    out = await tools["bash"].handler({"command": "ls"})
    assert "marker.txt" in out


async def test_bash_nonzero_exit_reported_not_raised(tmp_path):
    tools = toolmap(tmp_path)
    # 命令跑完但失败：退出码进结果供模型阅读，不算工具异常
    out = await tools["bash"].handler({"command": "echo boom >&2; exit 3"})
    assert "boom" in out
    assert "退出码：3" in out


async def test_bash_timeout_raises(tmp_path):
    tools = toolmap(tmp_path)
    with pytest.raises(ValueError, match="超时"):
        await tools["bash"].handler({"command": "sleep 5", "timeout": 0.3})


async def test_bash_timeout_kills_descendants(tmp_path):
    """超时按进程组整组击杀：命令派生的子进程不能留成孤儿继续跑。"""
    import subprocess

    tools = toolmap(tmp_path)
    marker = "movieclaw-test-reap-timeout"
    with pytest.raises(ValueError, match="超时"):
        await tools["bash"].handler(
            {"command": f"sleep 987654 # {marker}", "timeout": 0.3}
        )
    check = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert check.stdout.strip() == "", f"超时后仍有残留进程：{check.stdout}"


async def test_bash_cancel_kills_process_group(tmp_path):
    """取消（用户停止/停机）同样整组收割，子进程不残留。

    docs/design/agent-runtime-resilience.md §4.4（修缺口 E）。
    """
    import asyncio
    import subprocess
    import time

    tools = toolmap(tmp_path)
    marker = "movieclaw-test-reap-cancel"
    task = asyncio.create_task(
        tools["bash"].handler({"command": f"sleep 987654 # {marker} & wait"})
    )
    # 等命令真正跑起来（后台 sleep 属于同一进程组）
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        probe = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        if probe.stdout.strip():
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("测试命令未在期限内启动")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # SIGKILL 后给内核一点回收时间
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        leftover = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        if leftover.stdout.strip() == "":
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"取消后仍有残留进程：{leftover.stdout}")
