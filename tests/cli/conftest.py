"""CLI 测试基建。

两类夹具：
- run_mclaw：以子进程方式运行真实的 `python -m movieclaw_cli`，
  配置目录隔离到临时目录——测的是用户真正执行的东西（含退出码）；
- live_server：后台线程起真实 uvicorn（临时库、随机端口），
  端到端测试走真实 HTTP，与生产链路零差异。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

_ADMIN = {"username": "admin", "password": "s3cret-pass"}


@pytest.fixture
def cli_home(tmp_path: Path) -> Path:
    """隔离的 CLI 配置目录（config.toml / credentials 落这里）。"""
    home = tmp_path / "cli-home"
    home.mkdir()
    return home


@pytest.fixture
def run_mclaw(cli_home: Path):
    def _run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "MOVIECLAW_CONFIG_DIR": str(cli_home),
            # 隔离环境变量通道，避免宿主机配置串进测试
            "MOVIECLAW_SERVER": "",
            "MOVIECLAW_TOKEN": "",
            "MOVIECLAW_CONTEXT": "",
            **(env_extra or {}),
        }
        env = {k: v for k, v in env.items() if v != ""}
        return subprocess.run(
            [sys.executable, "-m", "movieclaw_cli", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    return _run


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 uvicorn 服务器（临时数据库 + 已完成管理员初始化）。"""
    import uvicorn

    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # 订阅路由构造服务时要求 TMDB Key 存在；空库列表不会发起真实 TMDB 请求
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/api/v1/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn 测试服务器 15 秒内未就绪")

    # 完成首次初始化，测试直接可登录
    resp = httpx.post(f"{base_url}/api/v1/auth/bootstrap", json=_ADMIN, timeout=5)
    assert resp.status_code == 200, resp.text

    yield base_url

    server.should_exit = True
    thread.join(timeout=10)
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


@pytest.fixture
def admin() -> dict[str, str]:
    return dict(_ADMIN)


# ---------------------------------------------------------------------------
# 设备配对助手（docs/design/device-auth.md §6.1）
# ---------------------------------------------------------------------------
#
# CLI 已经没有密码登录：令牌只能由人在浏览器里批准后签发。测试要拿到一枚
# 令牌，就得走完整的设备流——发起 → 用管理员会话批准 → 兑换。这既是取巧的
# 最短路径，也顺带保证测试用的凭证与真实用户拿到的是同一种东西。


def pair_cli_token(live_server: str, admin: dict[str, str], *, name: str = "pytest") -> str:
    """走一遍设备流，返回令牌明文。"""
    with httpx.Client(base_url=live_server, timeout=10) as client:
        client.post("/api/v1/auth/login", json=admin)
        grant = client.post(
            "/api/v1/auth/device/authorize",
            json={"client_type": "cli", "client_name": name},
        ).json()["data"]
        client.post(f"/api/v1/auth/devices/requests/{grant['user_code']}/approve")
        redeemed = client.post(
            "/api/v1/auth/device/token", json={"device_code": grant["device_code"]}
        )
        return redeemed.json()["data"]["token"]


def store_cli_credentials(cli_home: Path, server: str, token: str) -> None:
    """把令牌与上下文写进 CLI 的配置目录，等价于用户跑完 mclaw login。

    走 cfg 的写入函数而不是手写文件，凭证格式才只有一个事实源。
    """
    from movieclaw_cli.core import config as cfg

    previous = os.environ.get(cfg.ENV_CONFIG_DIR)
    os.environ[cfg.ENV_CONFIG_DIR] = str(cli_home)
    try:
        cfg.save_token(server, token)
        cfg.save_context(server)
    finally:
        if previous is None:
            os.environ.pop(cfg.ENV_CONFIG_DIR, None)
        else:
            os.environ[cfg.ENV_CONFIG_DIR] = previous
