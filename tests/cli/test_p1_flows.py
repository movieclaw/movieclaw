"""P1 端到端流程：危险确认、写操作、PAT Bearer 通道。"""

from __future__ import annotations

import json

import httpx
from tests.cli.conftest import pair_cli_token, store_cli_credentials


def _login(cli_home, live_server, admin) -> None:
    """等价于用户跑完 mclaw login：走设备流拿令牌并落进 CLI 配置目录。"""
    store_cli_credentials(cli_home, live_server, pair_cli_token(live_server, admin))


def test_dangerous_without_yes_exits_5(run_mclaw) -> None:
    """零交互原则：非 TTY 下危险操作缺 --yes 直接退出码 5，且不发请求
    （无需配置服务器也能得到明确指引）。"""
    result = run_mclaw("subscriptions", "delete", "1")
    assert result.returncode == 5
    assert "--yes" in result.stderr


def test_dangerous_with_yes_reaches_server(run_mclaw, cli_home, live_server, admin) -> None:
    _login(cli_home, live_server, admin)
    result = run_mclaw("subscriptions", "delete", "999", "--yes")
    assert result.returncode == 1  # 到达服务器：订阅不存在的业务错误
    assert "错误" in result.stderr


def test_write_operation_with_body_flags(run_mclaw, cli_home, live_server, admin) -> None:
    _login(cli_home, live_server, admin)
    result = run_mclaw("auth", "profile", "update", "--nickname", "新昵称", "-o", "json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["nickname"] == "新昵称"
    assert "已更新" in result.stderr  # 服务端 message 透传到 stderr


def test_missing_required_body_flag_exits_2_with_hint(
    run_mclaw, cli_home, live_server, admin
) -> None:
    _login(cli_home, live_server, admin)
    result = run_mclaw("auth", "profile", "update")
    assert result.returncode == 2
    assert "--nickname" in result.stderr
    assert "--input" in result.stderr  # hint 提到整体替代形态


def test_pat_token_channel(run_mclaw, cli_home, live_server, admin, tmp_path) -> None:
    """令牌全链路：网页上配对签发 → 全新环境仅凭 MOVIECLAW_TOKEN 调用成功
    → 吊销后立即 401（退出码 3）。这正是产品内 Agent 工作区的调用形态。

    签发与吊销走 HTTP + 会话 Cookie 而不是 CLI：凭证管理面只认浏览器会话
    （docs/design/device-auth.md §8），CLI 拿令牌调不动，这正是设计意图。
    """
    session = httpx.Client(base_url=live_server)
    session.post("/api/v1/auth/login", json=admin)
    grant = session.post(
        "/api/v1/auth/device/authorize",
        json={"client_type": "cli", "client_name": "ci"},
    ).json()["data"]
    session.post(f"/api/v1/auth/devices/requests/{grant['user_code']}/approve")
    payload = session.post(
        "/api/v1/auth/device/token", json={"device_code": grant["device_code"]}
    ).json()["data"]
    token = payload["token"]
    assert token.startswith("mclaw_")

    fresh_home = tmp_path / "fresh-cli-home"
    fresh_home.mkdir()
    env = {
        "MOVIECLAW_CONFIG_DIR": str(fresh_home),  # 无任何本地凭证
        "MOVIECLAW_SERVER": live_server,
        "MOVIECLAW_TOKEN": token,
    }
    result = run_mclaw("subscriptions", "list", "-o", "json", env_extra=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    token_id = session.get("/api/v1/auth/tokens").json()["data"][0]["id"]
    assert session.delete(f"/api/v1/auth/tokens/{token_id}").status_code == 200
    result = run_mclaw("subscriptions", "list", env_extra=env)
    assert result.returncode == 3
    session.close()
