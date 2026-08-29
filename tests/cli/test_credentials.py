"""CLI 凭证层：路径解析与四个工程陷阱（docs/design/device-auth.md §6.2/§6.3）。

「任何终端、任何应用触发都能连」的真正难点不在协议，在这四件事——
配置目录怎么定、$HOME 不一致怎么排障、权限过宽怎么办、并发写会不会写坏。
每一个都会表现成用户口中的「我明明配对过了」。
"""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError, ExitCode

_SERVER = "http://10.1.1.5:3000"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "cli-home"
    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(target))
    monkeypatch.delenv(cfg.ENV_SERVER, raising=False)
    monkeypatch.delenv(cfg.ENV_CONTEXT, raising=False)
    return target


# ---------------------------------------------------------------------------
# 配置目录：每用户固定位置
# ---------------------------------------------------------------------------


def test_config_dir_prefers_explicit_override(home) -> None:
    """逃生舱最优先：sudo / launchd / 容器里 $HOME 不同时，一句话解决。"""
    assert cfg.config_dir() == home


def test_config_dir_follows_xdg_on_posix(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(cfg.ENV_CONFIG_DIR, raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv(cfg.ENV_XDG_CONFIG, str(tmp_path / "xdg"))
    assert cfg.config_dir() == tmp_path / "xdg" / "movieclaw"


def test_config_dir_uses_appdata_on_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(cfg.ENV_CONFIG_DIR, raising=False)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv(cfg.ENV_APPDATA, str(tmp_path / "AppData"))
    assert cfg.config_dir() == tmp_path / "AppData" / "movieclaw"


# ---------------------------------------------------------------------------
# 地址解析：机器级兜底 + 排障信息
# ---------------------------------------------------------------------------


def test_system_config_supplies_server_when_user_has_none(home, tmp_path, monkeypatch) -> None:
    """机器级只提供地址，给「NAS 上管理员配一次，全家可用」兜底。"""
    system = tmp_path / "etc-movieclaw.toml"
    system.write_text(
        'current_context = "nas"\n\n[contexts.nas]\nserver = "http://nas.local:3000"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "system_config_path", lambda: system)
    assert cfg.resolve_server(None, None) == "http://nas.local:3000"


def test_user_context_overrides_system_one(home, tmp_path, monkeypatch) -> None:
    system = tmp_path / "etc-movieclaw.toml"
    system.write_text('[contexts.default]\nserver = "http://nas.local:3000"\n', encoding="utf-8")
    monkeypatch.setattr(cfg, "system_config_path", lambda: system)
    cfg.save_context(_SERVER)
    assert cfg.resolve_server(None, None) == _SERVER


def test_missing_server_error_lists_the_paths_it_searched(home, monkeypatch) -> None:
    """「我明明配对过了」十次里九次是读错了配置目录，所以要把路径打出来。"""
    monkeypatch.setattr(cfg, "system_config_path", lambda: Path("/etc/movieclaw/config.toml"))
    with pytest.raises(CliError) as excinfo:
        cfg.resolve_server(None, None)
    assert excinfo.value.exit_code is ExitCode.USAGE
    assert str(home / "config.toml") in (excinfo.value.hint or "")
    assert "/etc/movieclaw/config.toml" in (excinfo.value.hint or "")


def test_unreadable_system_config_does_not_break_commands(home, tmp_path, monkeypatch) -> None:
    """机器级配置可能不可读（权限/挂载），当它不存在处理，不能让命令失败。"""
    system = tmp_path / "etc-movieclaw.toml"
    system.write_text('[contexts.nas]\nserver = "http://nas:3000"\n', encoding="utf-8")
    system.chmod(0o000)
    monkeypatch.setattr(cfg, "system_config_path", lambda: system)
    cfg.save_context(_SERVER)
    try:
        assert cfg.resolve_server(None, None) == _SERVER
    finally:
        system.chmod(0o600)


# ---------------------------------------------------------------------------
# 令牌读写：权限、原子性
# ---------------------------------------------------------------------------


def test_token_roundtrip_and_file_is_owner_only(home) -> None:
    cfg.save_token(_SERVER, "mclaw_abc")
    assert cfg.load_token(_SERVER) == "mclaw_abc"

    path = home / "credentials"
    mode = path.stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO), stat.filemode(mode)


def test_tokens_are_keyed_by_server(home) -> None:
    """多服务器各存各的：切上下文不该串号。"""
    cfg.save_token(_SERVER, "mclaw_a")
    cfg.save_token("http://other:3000", "mclaw_b")
    assert cfg.load_token(_SERVER) == "mclaw_a"
    assert cfg.load_token("http://other:3000") == "mclaw_b"

    cfg.delete_token(_SERVER)
    assert cfg.load_token(_SERVER) is None
    assert cfg.load_token("http://other:3000") == "mclaw_b"


def test_over_permissive_credentials_are_refused(home) -> None:
    """自部署用户会 chmod -R 777；静默继续用比报错危险得多。"""
    cfg.save_token(_SERVER, "mclaw_abc")
    path = home / "credentials"
    path.chmod(0o644)

    with pytest.raises(CliError) as excinfo:
        cfg.load_token(_SERVER)
    assert excinfo.value.exit_code is ExitCode.USAGE
    assert "chmod 600" in (excinfo.value.hint or "")


def test_concurrent_writes_never_leave_a_broken_file(home) -> None:
    """多个 Agent 并发跑是常态：写必须原子，不能留下半截 JSON。"""
    cfg.save_token(_SERVER, "mclaw_seed")
    path = home / "credentials"

    def writer(index: int) -> None:
        cfg.save_token(f"http://host-{index}:3000", f"mclaw_{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(32)))

    # 任何时刻读到的都必须是完整 JSON；且没有留下临时文件
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[_SERVER]["token"] == "mclaw_seed"
    assert not [p for p in home.iterdir() if p.name.startswith(".credentials-")]


def test_corrupt_credentials_file_does_not_crash(home) -> None:
    """凭证文件被手工改坏时按「没有凭证」处理，用户重新配对即可。"""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "credentials"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("{ 这不是 JSON")
    assert cfg.load_token(_SERVER) is None
