r"""CLI 本地配置与凭证（docs/design/device-auth.md §6.2）。

**凭证必须落在每用户的固定位置**——这是「任何终端、任何应用触发都能连」的
前提：用户配一次，之后从 Dock 启动的 GUI 应用、cron、systemd 服务里跑
mclaw 都要能自动带上授权。

两个文件、职责分离（配置可进 dotfiles 同步，凭证绝不）：

    <配置目录>/config.toml     多上下文配置（服务器地址、默认输出）
    <配置目录>/credentials     设备令牌（JSON，0600，按服务器地址存）

配置目录按平台取（与 gh / gcloud 同款惯例）：

    Linux / macOS   $XDG_CONFIG_HOME/movieclaw，缺省 ~/.config/movieclaw
    Windows         %APPDATA%\movieclaw

**凭证只在用户级，绝不放机器级**：那份令牌等价管理员，全机器可读意味着本机
任何进程、任何其他用户都能拿到全权。机器级配置只允许放服务器地址，给
「NAS 上装一次全家都能用」这种场景兜底：

    Linux / macOS   /etc/movieclaw/config.toml
    Windows         %PROGRAMDATA%\movieclaw\config.toml

服务器地址解析优先级（与 gcloud/kubectl 一致）：
    --server 标志 > MOVIECLAW_SERVER 环境变量 > 用户级上下文
    > 机器级上下文 > 报错并列出找过的路径
环境变量是 Agent/CI 的主通道，可完全不落盘。
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from movieclaw_cli.core.errors import CliError, ExitCode

ENV_APPDATA = "APPDATA"
ENV_PROGRAMDATA = "PROGRAMDATA"
ENV_XDG_CONFIG = "XDG_CONFIG_HOME"

ENV_SERVER = "MOVIECLAW_SERVER"
ENV_TOKEN = "MOVIECLAW_TOKEN"
ENV_CONTEXT = "MOVIECLAW_CONTEXT"
ENV_CONFIG_DIR = "MOVIECLAW_CONFIG_DIR"


def config_dir() -> Path:
    """用户级配置目录。

    ``MOVIECLAW_CONFIG_DIR`` 是逃生舱：sudo、launchd、systemd、容器里的
    ``$HOME`` 各不相同，凭证会「凭空消失」；显式指定目录能一句话解决。
    """
    if override := os.environ.get(ENV_CONFIG_DIR):
        return Path(override)
    if sys.platform == "win32" and (appdata := os.environ.get(ENV_APPDATA)):
        return Path(appdata) / "movieclaw"
    if xdg := os.environ.get(ENV_XDG_CONFIG):
        return Path(xdg) / "movieclaw"
    return Path.home() / ".config" / "movieclaw"


def system_config_path() -> Path:
    """机器级配置。只读、只允许放服务器地址，绝不放凭证。"""
    if sys.platform == "win32":
        return Path(os.environ.get(ENV_PROGRAMDATA, "C:/ProgramData")) / "movieclaw" / "config.toml"
    return Path("/etc/movieclaw/config.toml")


def _config_path() -> Path:
    return config_dir() / "config.toml"


def _credentials_path() -> Path:
    return config_dir() / "credentials"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise CliError(
            f"配置文件格式错误：{path}（{exc}）",
            exit_code=ExitCode.USAGE,
            hint="修正该文件或删除后重新执行 mclaw login",
        ) from exc
    except OSError:
        # 机器级配置可能不可读（权限/挂载），当它不存在处理即可，不该让命令失败
        return {}


def load_config() -> dict[str, Any]:
    """用户级配置叠在机器级之上。

    机器级只提供上下文与默认上下文的兜底（NAS 上管理员配一次，全家可用），
    用户级的同名上下文覆盖它。凭证永远不从这里来。
    """
    system = _read_toml(system_config_path())
    user = _read_toml(_config_path())
    merged: dict[str, Any] = {
        "contexts": {**(system.get("contexts") or {}), **(user.get("contexts") or {})},
    }
    if current := user.get("current_context") or system.get("current_context"):
        merged["current_context"] = current
    return merged


def save_config(config: dict[str, Any]) -> None:
    """写回配置。结构固定且简单，手写 TOML 序列化避免引入写库依赖。

    字符串值用 json.dumps 序列化——JSON 字符串转义是合法的 TOML 基本
    字符串，含引号/反斜杠的值不会写出损坏的配置文件。
    """
    lines: list[str] = []
    if current := config.get("current_context"):
        lines.append(f"current_context = {json.dumps(current, ensure_ascii=False)}")
        lines.append("")
    for name, ctx in (config.get("contexts") or {}).items():
        lines.append(f"[contexts.{name}]")
        for key, value in ctx.items():
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        lines.append("")
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def resolve_server(flag_server: str | None, flag_context: str | None) -> str:
    """按优先级解析目标服务器地址；解析不出来时给出可行动的中文指引。"""
    if flag_server:
        return flag_server.rstrip("/")
    if env := os.environ.get(ENV_SERVER):
        return env.rstrip("/")
    config = load_config()
    contexts = config.get("contexts") or {}
    name = flag_context or os.environ.get(ENV_CONTEXT) or config.get("current_context")
    if name:
        ctx = contexts.get(name)
        if ctx is None:
            raise CliError(
                f"上下文不存在：{name}",
                exit_code=ExitCode.USAGE,
                hint=f"可用上下文：{', '.join(contexts) or '（无）'}；"
                "或用 mclaw login --server <地址> 新建",
            )
        return str(ctx["server"]).rstrip("/")
    # 「我明明登录过了」是这套凭证机制最常见的投诉，根因通常是 $HOME 不同
    # （sudo / launchd / systemd / 容器）导致读的不是同一个配置目录。
    # 因此这里把找过的路径原样列出来，让人一眼看出读的是哪儿。
    raise CliError(
        "未指定 movieclaw 服务器地址",
        exit_code=ExitCode.USAGE,
        hint="三种方式任选：mclaw login --server http://<主机>:3000 登录并记住；"
        "或设置环境变量 MOVIECLAW_SERVER；或加 --server 标志。"
        f"（已查找：{_config_path()}、{system_config_path()}）",
    )


_CONTEXT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def save_context(server: str, name: str = "default") -> None:
    """登录成功后把服务器记进上下文；首个上下文自动设为当前。"""
    if not _CONTEXT_NAME_RE.match(name):
        raise CliError(
            f"上下文名不合法：{name}",
            exit_code=ExitCode.USAGE,
            hint="只允许字母、数字、连字符与下划线（TOML 表名约束）",
        )
    config = load_config()
    contexts = config.setdefault("contexts", {})
    contexts[name] = {"server": server}
    config.setdefault("current_context", name)
    save_config(config)


# ---------------------------------------------------------------------------
# 设备令牌（docs/design/device-auth.md §6.2）
# ---------------------------------------------------------------------------


def _check_permissions(path: Path) -> None:
    """凭证文件权限过宽就拒绝加载，并说清怎么修（同 ssh 对私钥的做法）。

    自部署用户遇到权限问题的第一反应是 ``chmod -R 777``；那之后这枚等价
    管理员的令牌就对本机所有用户可读了。静默继续用比报错危险得多。
    """
    if sys.platform == "win32":  # NTFS ACL 不是 POSIX 位，这里不做判断
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CliError(
            f"凭证文件权限过宽：{path}（{stat.filemode(mode)}）",
            exit_code=ExitCode.USAGE,
            hint=f"这枚令牌等价管理员，不能让同机其他用户读到。执行：chmod 600 {path}",
        )


def _load_credentials() -> dict[str, Any]:
    path = _credentials_path()
    if not path.is_file():
        return {}
    _check_permissions(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_token(server: str) -> str | None:
    """读取该服务器的设备令牌。没有配对过则返回 None。"""
    entry = _load_credentials().get(server)
    return entry.get("token") if isinstance(entry, dict) else None


def _write_credentials(creds: dict[str, Any]) -> None:
    """凭证落盘：0600 打开写临时文件，再原子替换。

    两点都是必需的：先以 0600 打开而不是「写完再 chmod」，不留可读窗口；
    临时文件 + ``os.replace`` 原子替换，因为多个 Agent 并发跑很正常，
    直接truncate 后写会在中途留下一个损坏的凭证文件。
    """
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".credentials-")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600) if hasattr(os, "fchmod") else tmp.chmod(0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(creds, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_token(server: str, token: str) -> None:
    creds = _load_credentials()
    creds[server] = {"token": token}
    _write_credentials(creds)


def delete_token(server: str) -> None:
    creds = _load_credentials()
    if server in creds:
        del creds[server]
        _write_credentials(creds)
