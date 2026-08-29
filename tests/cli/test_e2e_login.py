"""端到端冒烟：真实 uvicorn + 真实子进程 CLI。

覆盖设备配对主链路（docs/design/device-auth.md §6.1）——
`mclaw login` 出示配对码 → 人在网页批准 → 令牌回到本进程并落盘 →
后续命令无需再指定 --server；以及 status 的凭证来源、logout 的语义边界。

login 需要 TTY（零交互原则：配对必须有人批准，非 TTY 下不允许挂起等待），
因此这里用 pty 作为子进程的标准输入，测的是用户真正会跑的那条路径。
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
import time

import httpx
import pytest


def _spawn_login(cli_home, live_server: str, *extra: str) -> subprocess.Popen:
    """在 pty 上起一个 mclaw login 子进程（不阻塞，等测试去批准）。"""
    master, slave = pty.openpty()
    env = {
        **os.environ,
        "MOVIECLAW_CONFIG_DIR": str(cli_home),
        "MOVIECLAW_SERVER": "",
        "MOVIECLAW_TOKEN": "",
        "MOVIECLAW_CONTEXT": "",
    }
    env = {k: v for k, v in env.items() if v != ""}
    proc = subprocess.Popen(
        [sys.executable, "-m", "movieclaw_cli", "login", "--server", live_server, *extra],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    os.close(slave)
    proc._pty_master = master  # type: ignore[attr-defined]  # 由调用方在 wait 后关闭
    return proc


def _approve_pending(live_server: str, admin: dict[str, str], *, timeout: float = 15.0) -> dict:
    """以管理员会话轮询待批准列表并批准第一条，返回那条请求。"""
    with httpx.Client(base_url=live_server, timeout=5) as client:
        client.post("/api/v1/auth/login", json=admin)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = client.get("/api/v1/auth/devices/requests").json()["data"]
            if pending:
                client.post(f"/api/v1/auth/devices/requests/{pending[0]['user_code']}/approve")
                return pending[0]
            time.sleep(0.2)
    raise AssertionError("等待 mclaw login 发起配对请求超时")


def test_pairing_then_list_subscriptions(run_mclaw, cli_home, live_server, admin) -> None:
    proc = _spawn_login(cli_home, live_server)
    request = _approve_pending(live_server, admin)
    _out, err = proc.communicate(timeout=30)
    os.close(proc._pty_master)  # type: ignore[attr-defined]

    assert proc.returncode == 0, err
    assert "已授权" in err
    # 配对码必须显示给用户核对；令牌绝不出现在任何输出里
    assert request["user_code"] in err
    assert "mclaw_" not in err, "令牌明文泄漏到了终端输出"

    # 配对已把服务器记入上下文，后续命令无需 --server
    result = run_mclaw("subscriptions", "list", "-o", "json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    history = run_mclaw("search", "history", "list", "--limit", "5", "-o", "json")
    assert history.returncode == 0, history.stderr
    assert json.loads(history.stdout) == []


def test_pairing_uses_hostname_by_default_and_honours_name(cli_home, live_server, admin) -> None:
    """设备名是用户日后决定「吊销哪台」的依据，必须能认得出来。"""
    proc = _spawn_login(cli_home, live_server, "--name", "书房的 Mac")
    _approve_pending(live_server, admin)
    _out, err = proc.communicate(timeout=30)
    os.close(proc._pty_master)  # type: ignore[attr-defined]
    assert proc.returncode == 0, err

    with httpx.Client(base_url=live_server, timeout=5) as client:
        client.post("/api/v1/auth/login", json=admin)
        devices = client.get("/api/v1/auth/tokens").json()["data"]
    assert [d["name"] for d in devices] == ["书房的 Mac"]
    assert devices[0]["client_type"] == "cli"


def test_denied_pairing_exits_3(cli_home, live_server, admin) -> None:
    """被拒绝是终态：立即以认证失败退出，不继续轮询刷屏。"""
    proc = _spawn_login(cli_home, live_server)
    with httpx.Client(base_url=live_server, timeout=5) as client:
        client.post("/api/v1/auth/login", json=admin)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            pending = client.get("/api/v1/auth/devices/requests").json()["data"]
            if pending:
                client.post(f"/api/v1/auth/devices/requests/{pending[0]['user_code']}/deny")
                break
            time.sleep(0.2)
        else:  # pragma: no cover - 只在服务器异常时触发
            pytest.fail("等待配对请求超时")

    _out, err = proc.communicate(timeout=30)
    os.close(proc._pty_master)  # type: ignore[attr-defined]
    assert proc.returncode == 3, err
    assert "拒绝" in err


def test_status_shows_identity_and_credential_source(
    run_mclaw, cli_home, live_server, admin
) -> None:
    proc = _spawn_login(cli_home, live_server)
    _approve_pending(live_server, admin)
    proc.communicate(timeout=30)
    os.close(proc._pty_master)  # type: ignore[attr-defined]

    result = run_mclaw("status", "-o", "json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["server"] == live_server
    assert payload["cli_spec_hash"]
    # 「我明明配对过了」的排障靠这一项：凭证到底从哪儿读的
    assert payload["credential"] == str(cli_home / "credentials")


def test_logout_clears_local_credential_only(run_mclaw, cli_home, live_server, admin) -> None:
    """logout 只清本地：吊销是人在浏览器里的动作，CLI 拿令牌调不动。"""
    proc = _spawn_login(cli_home, live_server)
    _approve_pending(live_server, admin)
    proc.communicate(timeout=30)
    os.close(proc._pty_master)  # type: ignore[attr-defined]

    logout = run_mclaw("logout")
    assert logout.returncode == 0, logout.stderr
    assert "设备" in logout.stderr  # 明确告诉用户去哪儿真正吊销

    result = run_mclaw("subscriptions", "list")
    assert result.returncode == 3  # 本地凭证已清

    # 服务端令牌仍在——这正是提示要说清楚的那件事
    with httpx.Client(base_url=live_server, timeout=5) as client:
        client.post("/api/v1/auth/login", json=admin)
        assert len(client.get("/api/v1/auth/tokens").json()["data"]) == 1


def test_login_refuses_non_interactive(run_mclaw, live_server) -> None:
    """零交互原则：非 TTY 下配对必然挂起等批准，因此直接以用法错误失败。"""
    result = run_mclaw("login", "--server", live_server)
    assert result.returncode == 2
    assert "MOVIECLAW_TOKEN" in result.stderr
