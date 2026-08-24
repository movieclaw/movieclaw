"""管理员密码离线重置入口（忘记密码时的唯一找回通道）。

    python -m movieclaw_api.reset_password            # 重置密码
    python -m movieclaw_api.reset_password --show     # 只看用户名，不做修改

为什么是"离线命令"而不是网页上的「忘记密码」
--------------------------------------------
自托管软件没有可信的第三方来证明"你是账号主人"：没有强制绑定的邮箱/手机，
真做邮件找回就得先要求每个部署者配好 SMTP——对着一台家里的 NAS，这既做不到
也没必要。所以身份证明换一个更硬的东西：**能直接访问 ``data/`` 目录，就是
这台机器的主人**。

这与主密钥文件（``data/.secret_key``）的信任边界完全一致：能碰数据目录的人
本来就能解密全部配置、直接改数据库，多一个改密入口不降低任何安全性；反过来，
碰不到数据目录的人（比如公网上的攻击者）也就摸不到这条路径。Jellyfin、
Vaultwarden、Gitea 的密码找回都是同款思路。

⚠️ 与之对应的红线写在 ``services.auth.reset_admin_password`` 上：那个函数不
校验原密码，**绝不可挂到任何 HTTP 路由上**。

保全配置
--------
整个过程只覆写"管理员密码哈希"这一个字段，用户名、昵称、站点、下载器、媒体库
等其余所有配置与数据原样不动。

成员（非超管）忘记密码不需要本命令：超管在「设置 → 成员管理」里点一下重置即可。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import secrets
import sys
from pathlib import Path

# 密码长度下限与网页首次引导保持一致（schemas/auth.py 的 BootstrapRequest）
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


async def _bootstrap_runtime() -> None:
    """装配离线运行所需的最小内核：数据库引擎 → 加密器 → 配置存储。

    刻意**不跑数据库迁移**：本命令只改一个字段，不该在用户着急找回密码时顺手
    动表结构；表结构升级仍由应用正常启动时自动完成（见 lifespan.py）。
    """
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.settings import init_setting_store
    from movieclaw_db.crypto import init_secret_box
    from movieclaw_db.engine import init_db

    settings = get_settings()
    init_db(settings.database_url)
    init_secret_box(settings.master_key, Path(settings.secret_key_file))
    init_setting_store()


def _database_file(database_url: str) -> Path | None:
    """从 SQLite 连接串里取出数据库文件路径；非 SQLite 返回 None（不做存在性检查）。"""
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url[len(prefix) :])


def _check_data_dir() -> str | None:
    """数据库文件不存在时，返回一条可行动的中文提示（说明大概率跑错了地方）。"""
    from movieclaw_api.core.config import get_settings

    db_file = _database_file(get_settings().database_url)
    if db_file is None or db_file.is_file():
        return None
    return (
        f"未找到数据库文件：{db_file.resolve()}\n"
        "说明当前不在 movieclaw 的数据目录下。请确认：\n"
        "  · Docker 部署：在宿主机执行 docker exec -it movieclaw "
        "python -m movieclaw_api.reset_password\n"
        "  · 源码部署：先 cd 到项目根目录（data/ 的上一级）再执行本命令"
    )


def _read_new_password(supplied: str | None) -> tuple[str, bool]:
    """确定新密码，返回 (密码, 是否为随机生成)。

    三种来源，按"越不容易泄露越优先"排布：
    - ``--password``：脚本化场景；会留在 shell 历史里，帮助文本已提示；
    - 交互终端（``docker exec -it``）：getpass 两次输入，不回显、不进历史；
    - 非交互（NAS 图形界面的一键执行、``docker exec`` 不带 -it）：随机生成
      并打印——总比让用户在管道里裸传密码强。
    """
    if supplied is not None:
        return supplied, False
    if not sys.stdin.isatty():
        return secrets.token_urlsafe(12), True

    while True:
        first = getpass.getpass("请输入新密码（输入时不显示）：")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(f"密码至少 {MIN_PASSWORD_LENGTH} 位，请重新输入。", file=sys.stderr)
            continue
        if first != getpass.getpass("请再输入一次确认："):
            print("两次输入不一致，请重新输入。", file=sys.stderr)
            continue
        return first, False


def _validate(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"密码太短：至少需要 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise SystemExit(f"密码太长：最多 {MAX_PASSWORD_LENGTH} 位")


async def _run(args: argparse.Namespace) -> int:
    if hint := _check_data_dir():
        print(hint, file=sys.stderr)
        return 1

    await _bootstrap_runtime()

    from movieclaw_api.services import auth as auth_service
    from movieclaw_db.engine import dispose_db

    try:
        account = await auth_service.get_admin_account()
        if not account.password_hash:
            print(
                "系统尚未初始化：还没有创建过管理员账号。\n请直接打开网页按引导创建，无需本命令。",
                file=sys.stderr,
            )
            return 1

        if args.show:
            print(f"当前管理员用户名：{account.username}")
            return 0

        new_password, generated = _read_new_password(args.password)
        _validate(new_password)

        await auth_service.reset_admin_password(new_password)

        print("✅ 管理员密码已重置，所有配置与数据未受影响。")
        print(f"   用户名：{account.username}")
        if generated:
            print(f"   新密码：{new_password}")
            print("   （随机生成，请立即登录后到「个人信息」里改成自己的密码）")
        print(
            "\n提示：为让其他设备上已登录的会话彻底失效，请重启一次服务"
            "（docker restart movieclaw）。\n"
            "      不重启也能用新密码登录。"
        )
        return 0
    finally:
        await dispose_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m movieclaw_api.reset_password",
        description="重置 movieclaw 管理员密码（忘记密码时使用，不影响任何配置）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  docker exec -it movieclaw python -m movieclaw_api.reset_password\n"
            "  docker exec -it movieclaw python -m movieclaw_api.reset_password --show\n"
        ),
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="只显示当前管理员用户名（连用户名也忘了时用），不修改任何内容",
    )
    parser.add_argument(
        "--password",
        help="直接指定新密码（会留在 shell 历史里；建议省略本参数，改用交互式输入）",
    )
    args = parser.parse_args(argv)

    # 只让警告及以上的日志露面：正常路径下的输出全部由 print 负责，
    # 面向的是不看日志的普通部署者
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
