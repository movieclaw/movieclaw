import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from movieclaw_api.app import create_app
from movieclaw_api.core.config import get_settings
from movieclaw_api.core.logging import configure_logging
from movieclaw_api.services.app_config import (
    FULL_RESTART_EXIT_CODE,
    register_uvicorn_server,
    restart_exit_code,
)


def app() -> FastAPI:
    return create_app()


def run() -> None:
    settings = get_settings()
    # 启动 uvicorn 前先装配好根 logger：让「Started server process」等启动日志
    # 也进入按天落盘的日志文件（设置页「系统日志」的数据来源）。
    configure_logging(settings.log_level, settings.log_dir, settings.log_retention_days)
    # 监听端口来自 APP_PORT 环境变量（默认 8000），不提供应用内配置：
    # 用户视角的访问入口是前端（3000 / compose 端口映射），后端端口只在
    # 容器内被 Next 反代，应用内改它对外部访问没有意义。
    port = settings.port
    # 两个分支的公共 uvicorn 参数：
    # - log_config=None：不让 uvicorn 接管日志配置，它的 logger 直接向根 logger
    #   传播，与业务日志走同一套「控制台 + 按天落盘」Handler、同一格式。
    # - access_log=False：uvicorn 自带的访问日志与 middleware.py 的重复，且后者
    #   更详细（含耗时）并受 APP_ACCESS_LOG_ENABLED 开关控制，故关掉前者。
    if settings.reload:
        # 开发热重载：沿用 uvicorn 的 reloader 进程模型（reloader 父进程 +
        # 应用子进程），拿不到应用子进程里的 Server 实例，设置页重启走
        # SIGTERM 退回路径即可——开发场景本就有热重载兜底。
        #
        # reload_dirs 必须显式钉在源码树，不能用 uvicorn 的默认值：默认监听
        # 当前工作目录，而 dev.sh 从仓库根目录启动，按天落盘的日志（data/logs）
        # 正好落在监听范围内——写日志触发 watchfiles 检测、检测本身又打一行
        # 日志，形成自激循环。实测零请求空闲状态下日志 25 秒涨 24KB（约
        # 83MB/天），"change detected" 把真实日志彻底淹没（不会真的反复重启，
        # 但日志没法看了，磁盘也白涨）。
        # 取包目录的上一级即 <仓库>/src：所有后端一方包都在这一层，可编辑安装
        # （pip install -e）下恒成立，覆盖 dev.sh 与直接跑 movieclaw-api 两种起法。
        src_root = Path(__file__).resolve().parent.parent
        uvicorn.run(
            "movieclaw_api.main:app",
            factory=True,
            host=settings.host,
            port=port,
            reload=True,
            reload_dirs=[str(src_root)],
            log_config=None,
            access_log=False,
        )
        return

    # 生产：自持 Server 实例并注册给重启服务（services/app_config）。
    # 设置页重启由此直接置 should_exit 优雅停机（不经信号投递，绕开 uvloop 下
    # signal.signal 处理器可能长时间不执行的问题），停机后以约定退出码 42
    # 告知 entrypoint「这是重启请求」——由其只重启后端（前端保持运行），
    # 反代链路重新验证健康后恢复监督；
    # 其他退出码（含 docker stop 的信号路径）仍是整容器退出。
    config = uvicorn.Config(
        "movieclaw_api.main:app",
        factory=True,
        host=settings.host,
        port=port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    register_uvicorn_server(server)
    server.run()
    exit_code = restart_exit_code()
    if exit_code == FULL_RESTART_EXIT_CODE:
        # 回退暂存的数据库恢复只能在这里做：服务已停、DB 引擎已关闭，且重启
        # 后跑的是旧版本代码（不认识本机制）。无暂存时本调用是空操作。
        from movieclaw_api.services.app_update import execute_pending_restore

        execute_pending_restore()
    if exit_code is not None:
        sys.exit(exit_code)


if __name__ == "__main__":
    run()
