"""管理员密码离线重置入口（`python -m movieclaw_api.reset_password`）的测试。

主用例刻意用**真子进程**跑重置命令，而不是在进程内直接调服务函数：这条路径
的价值恰恰在于"服务没跑、独立进程、只有 data 目录"也能把密码改掉，只有真的
起一个进程才能守住"导入链能独立解析、自己能装配好数据库与加密器"这两点——
进程内调用会被测试里已经初始化好的全局单例掩盖。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.reset_password import (
    _check_data_dir,
    _database_file,
    _read_new_password,
    _validate,
)
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box

_AUTH = "/api/v1/auth"
_ADMIN = {"username": "admin", "password": "old-pass-1234"}
_NEW_PASSWORD = "brand-new-pass-5678"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _reset_globals() -> None:
    """清空跨 app 实例的进程级单例，保证前后两次启动互不串味。"""
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


@contextmanager
def _app() -> Iterator[TestClient]:
    """起一次完整生命周期的应用（模拟"重启服务"）。"""
    _reset_globals()
    from movieclaw_api.app import create_app

    with TestClient(create_app()) as client:
        yield client
    _reset_globals()


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """一份已建号的独立数据目录，作为"用户忘记密码"的现场。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'movieclaw.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    _reset_globals()
    yield tmp_path
    _reset_globals()


def _run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    """在子进程里执行离线重置命令，环境变量沿用当前用例的数据目录。"""
    env = {**os.environ, "PYTHONPATH": str(_PROJECT_ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "movieclaw_api.reset_password", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=_PROJECT_ROOT,
        timeout=180,
    )


def _bootstrap_admin() -> None:
    with _app() as client:
        assert client.post(f"{_AUTH}/bootstrap", json=_ADMIN).status_code == 200


# ---------------------------------------------------------------------------
# 主流程：忘记密码 → 离线重置 → 用新密码登录
# ---------------------------------------------------------------------------


def test_reset_lets_admin_log_in_again_without_touching_config(data_dir: Path) -> None:
    """重置后新密码可登录、旧密码失效，且用户名与昵称等配置原样保留。"""
    with _app() as client:
        assert client.post(f"{_AUTH}/bootstrap", json=_ADMIN).status_code == 200
        # 改个昵称，作为"其余配置未被覆写"的探针（重置只该动密码哈希一个字段）
        assert client.put(f"{_AUTH}/profile", json={"nickname": "家里的老王"}).status_code == 200

    result = _run_tool("--password", _NEW_PASSWORD)
    assert result.returncode == 0, result.stderr

    with _app() as client:
        old = client.post(f"{_AUTH}/login", json=_ADMIN)
        assert old.status_code == 401, "旧密码必须失效"

        new = client.post(
            f"{_AUTH}/login", json={"username": _ADMIN["username"], "password": _NEW_PASSWORD}
        )
        assert new.status_code == 200
        assert new.json()["data"]["username"] == "admin"
        assert new.json()["data"]["nickname"] == "家里的老王"


def test_reset_takes_effect_without_restarting_the_service(data_dir: Path) -> None:
    """服务不重启也能用新密码登录——认证读的是库，不吃本进程的配置缓存。

    这是最容易回归、也最伤用户的一条：吃缓存的话用户会以为"重置没生效"。
    """
    with _app() as client:
        assert client.post(f"{_AUTH}/bootstrap", json=_ADMIN).status_code == 200
        # 先失败登录一次，确保管理员配置已经被读进本进程缓存
        assert client.post(f"{_AUTH}/login", json={**_ADMIN, "password": "wrong"}).status_code

        assert _run_tool("--password", _NEW_PASSWORD).returncode == 0

        # 同一个运行中的应用实例，未重启
        resp = client.post(
            f"{_AUTH}/login", json={"username": _ADMIN["username"], "password": _NEW_PASSWORD}
        )
        assert resp.status_code == 200


def test_reset_invalidates_existing_sessions_after_restart(data_dir: Path) -> None:
    """重置会轮换会话签名密钥：别处已登录的会话在服务重启后失效。"""
    with _app() as client:
        assert client.post(f"{_AUTH}/bootstrap", json=_ADMIN).status_code == 200
        stolen_cookie = client.cookies["movieclaw_session"]
        assert client.get(f"{_AUTH}/me").status_code == 200

    assert _run_tool("--password", _NEW_PASSWORD).returncode == 0

    with _app() as client:
        client.cookies.set("movieclaw_session", stolen_cookie)
        assert client.get(f"{_AUTH}/me").status_code == 401


def test_generated_password_is_printed_when_not_a_tty(data_dir: Path) -> None:
    """非交互执行（NAS 一键跑、docker exec 不带 -it）时随机生成并打印新密码。"""
    _bootstrap_admin()

    result = _run_tool()
    assert result.returncode == 0, result.stderr

    # 从输出里把新密码抠出来，验证它确实能登录
    line = next(line for line in result.stdout.splitlines() if "新密码：" in line)
    generated = line.split("新密码：", 1)[1].strip()
    assert len(generated) >= 8

    with _app() as client:
        resp = client.post(
            f"{_AUTH}/login", json={"username": _ADMIN["username"], "password": generated}
        )
        assert resp.status_code == 200


def test_show_only_prints_username(data_dir: Path) -> None:
    """--show 用于"连用户名也忘了"，必须只读不写。"""
    _bootstrap_admin()

    result = _run_tool("--show")
    assert result.returncode == 0, result.stderr
    assert "admin" in result.stdout

    with _app() as client:
        assert client.post(f"{_AUTH}/login", json=_ADMIN).status_code == 200


def test_refuses_when_not_initialized(data_dir: Path) -> None:
    """还没建过号：提示去网页走引导，而不是在这里凭空造一个管理员。"""
    with _app():
        pass  # 只跑一次迁移，建出空库

    result = _run_tool("--password", _NEW_PASSWORD)
    assert result.returncode == 1
    assert "尚未初始化" in result.stderr


def test_refuses_when_data_dir_missing(tmp_path, monkeypatch) -> None:
    """跑错目录（没有数据库文件）时给出可行动的中文指引，而不是一串堆栈。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'nope.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    _reset_globals()

    result = _run_tool("--password", _NEW_PASSWORD)
    _reset_globals()

    assert result.returncode == 1
    assert "未找到数据库文件" in result.stderr
    assert "docker exec" in result.stderr


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_database_file_only_parses_sqlite() -> None:
    assert _database_file("sqlite+aiosqlite:///./data/movieclaw.db") == Path("./data/movieclaw.db")
    assert _database_file("postgresql+asyncpg://u:p@host/db") is None


def test_check_data_dir_skips_non_sqlite(monkeypatch) -> None:
    """外接数据库时不做文件存在性检查（检查不了，也不该拦）。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    get_settings.cache_clear()
    try:
        assert _check_data_dir() is None
    finally:
        get_settings.cache_clear()


def test_supplied_password_wins_over_generation() -> None:
    assert _read_new_password("explicit-pass") == ("explicit-pass", False)


def test_generates_password_when_stdin_is_not_a_tty() -> None:
    password, generated = _read_new_password(None)
    assert generated is True
    assert len(password) >= 8


@pytest.mark.parametrize("bad", ["short", "x" * 129])
def test_validate_rejects_out_of_range_password(bad: str) -> None:
    with pytest.raises(SystemExit):
        _validate(bad)
