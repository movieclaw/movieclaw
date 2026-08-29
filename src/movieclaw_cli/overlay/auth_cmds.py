"""登录 / 登出 / 状态（精选命令）。

``login`` 走设备授权流程（docs/design/device-auth.md §6.1）：CLI 出示一段
配对码，人在浏览器里核对并批准，令牌通过兑换直接回到本进程——**从不显示
在屏幕上，也就不会进剪贴板、shell 历史或 Agent 的上下文**。

刻意没有密码登录：密码只在浏览器里用。这条不是洁癖——一旦 CLI 接受
``--password``，第三方 Agent 替用户跑命令时管理员密码就会进模型上下文。
"""

from __future__ import annotations

import os
import socket
import sys
import time

import click

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.output import emit

#: 轮询兑换的兜底上限。服务端会下发 expires_in，这里只防「服务端给了个
#: 离谱的大数」导致命令挂死——CLI 的零交互原则不允许无限等待。
_MAX_WAIT_SECONDS = 15 * 60


def _default_client_name() -> str:
    """默认设备名：``mclaw@<主机名>``。

    它会显示在网页的审批卡和设备列表上，是用户判断「这是不是我那台机器」
    以及日后决定吊销哪台的依据，所以要能认得出来。
    """
    try:
        host = socket.gethostname() or "unknown-host"
    except OSError:  # pragma: no cover - 主机名不可读是极端环境
        host = "unknown-host"
    return f"mclaw@{host}"


@click.command(name="login", short_help="配对本机并保存授权")
@click.option("--server", help="movieclaw 服务器地址，如 http://192.168.1.10:3000")
@click.option(
    "--name",
    "client_name",
    help="本机在网页设备列表里的名字，默认 mclaw@<主机名>",
)
@click.pass_obj
def login(settings, server: str | None, client_name: str | None):
    """把这台机器配对到 movieclaw，并保存授权。

    示例：

        mclaw login --server http://192.168.1.10:3000

    命令会显示一段配对码，请在浏览器里打开 movieclaw 的「设置 → 设备」，
    核对配对码后批准。配对成功后服务器地址会记入当前上下文，之后的命令
    无需再指定 --server。
    """
    target = cfg.resolve_server(server or settings.server, settings.context)

    # 零交互原则（docs/design/cli.md §5.1）：配对必须有人在浏览器里点批准，
    # 非 TTY 下执行它只会静默挂到超时。与其挂住，不如立刻说清该怎么办。
    if not sys.stdin.isatty():
        raise CliError(
            "非交互环境无法完成配对：配对需要有人在浏览器里批准",
            exit_code=ExitCode.USAGE,
            hint="请在有终端的机器上执行 mclaw login；无人值守场景（CI / 容器）"
            "改为在网页「设置 → 设备」创建令牌后，用环境变量 MOVIECLAW_TOKEN 注入",
        )

    api = settings.make_api(server=target)
    try:
        health = api.request("GET", "/health") or {}
        version = health.get("version", "未知版本")
        click.echo(f"✓ 已连接 movieclaw {version}", err=True)

        grant = api.request(
            "POST",
            "/auth/device/authorize",
            json_body={"client_type": "cli", "client_name": client_name or _default_client_name()},
        )
        click.echo("", err=True)
        click.echo(f"请在浏览器打开：{grant['verification_uri']}", err=True)
        click.echo(f"核对配对码：      {grant['user_code']}", err=True)
        click.echo("", err=True)

        token = _await_grant(api, grant)
    finally:
        api.close()

    cfg.save_token(target, token["token"])
    cfg.save_context(target)
    click.echo(f"✓ 已授权：{token['client_name']}", err=True)
    click.echo(f"  凭证已写入 {cfg.config_dir() / 'credentials'}（仅本用户可读）", err=True)


def _await_grant(api, grant: dict) -> dict:
    """按服务端下发的节奏轮询兑换，直到拿到令牌或得到确定的失败结论。

    三种终态都不重试：被拒绝、已过期、配对码不存在——继续轮询只会刷屏。
    """
    interval = max(1, int(grant.get("interval") or 2))
    deadline = time.monotonic() + min(int(grant.get("expires_in") or 300), _MAX_WAIT_SECONDS)
    click.echo("等待批准…（在浏览器里点「批准接入」后本命令会自动继续）", err=True)

    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            data, response = api.request_raw(
                "POST", "/auth/device/token", json_body={"device_code": grant["device_code"]}
            )
        except CliError as exc:
            # 服务端把「已拒绝 / 已过期 / 不存在」统一成 400，都是终态
            if exc.exit_code == ExitCode.BUSINESS:
                raise CliError(
                    exc.message,
                    exit_code=ExitCode.AUTH,
                    hint="重新执行 mclaw login 发起新的配对",
                ) from exc
            raise
        if response.status_code == 200 and data:
            return data
        # 202 等待批准、429 轮询过快：后者按服务端要求退避一拍
        if response.status_code == 429:
            interval += 1

    raise CliError(
        "配对超时：没有等到批准",
        exit_code=ExitCode.AUTH,
        hint="确认已在浏览器的「设置 → 设备」里批准，然后重新执行 mclaw login",
    )


@click.command(name="logout", short_help="删除本机保存的授权")
@click.pass_obj
def logout(settings):
    """删除本机保存的令牌。

    这只清本地凭证，**不会吊销服务端的令牌**——吊销是人在浏览器里的动作
    （docs/design/device-auth.md §4.4）。想彻底停用这台机器，请到网页的
    「设置 → 设备」里吊销它。
    """
    target = cfg.resolve_server(settings.server, settings.context)
    cfg.delete_token(target)
    click.echo(f"已删除本机保存的授权：{target}", err=True)
    click.echo(
        "提示：服务端的令牌仍然有效。要彻底停用这台机器，请到网页「设置 → 设备」里吊销。",
        err=True,
    )


@click.command(name="status", short_help="查看服务器与授权状态")
@click.option(
    "-o",
    "--output",
    "output_override",
    type=click.Choice(["table", "json", "yaml"]),
    default=None,
    help="输出格式（覆盖全局设置）",
)
@click.pass_obj
def status(settings, output_override: str | None):
    """一眼看部署状态：服务健康、当前身份、凭证来源、spec 版本偏斜。

    ``credential`` 这一项是排障的关键：「我明明配对过了」十次里有九次是
    因为 $HOME 不同（sudo / launchd / systemd / 容器）读到了别的配置目录。
    把凭证到底从哪儿来的打出来，一眼就能看出问题。
    """
    from movieclaw_cli.gen import spec_loader

    target = cfg.resolve_server(settings.server, settings.context)
    api = settings.make_api(server=target)
    try:
        health = api.request("GET", "/health")
        try:
            me = api.request("GET", "/auth/me")
            identity = (me or {}).get("nickname") or (me or {}).get("username") or "已登录"
        except CliError as exc:
            if exc.exit_code != ExitCode.AUTH:
                raise
            identity = "未授权（mclaw login）"
    finally:
        api.close()
    if os.environ.get(cfg.ENV_TOKEN):
        credential = f"环境变量 {cfg.ENV_TOKEN}"
    elif cfg.load_token(target):
        credential = str(cfg.config_dir() / "credentials")
    else:
        credential = "无（mclaw login）"
    server_hash = (health or {}).get("spec_hash")
    cli_hash = spec_loader.active_spec_hash
    emit(
        {
            "server": target,
            "service": (health or {}).get("service"),
            "status": (health or {}).get("status"),
            "environment": (health or {}).get("environment"),
            "identity": identity,
            "credential": credential,
            "cli_spec_hash": cli_hash,
            "server_spec_hash": server_hash,
            "spec_in_sync": (server_hash == cli_hash) if server_hash else None,
        },
        output=output_override or settings.output,
        quiet=settings.quiet,
    )
