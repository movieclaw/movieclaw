"""应用设置的业务服务（routes/app_config 的实现层）。

职责：
- 配置读写：外部访问地址的校验与落库（保存即生效，纯落库数据）；
- 重启调度：优雅停机后以约定退出码 ``RESTART_EXIT_CODE``（42）退出进程，
  告知守护方「这是重启请求」——Docker 镜像的 entrypoint 内置重启循环，
  见到 42 只重启后端进程，前端保持运行（窗口内 API 反代短暂不可用，发起
  重启的页面本就在轮询等待），也不依赖用户的 restart 策略；
  其他退出码仍走「整容器退出」，保持故障外显。源码部署需 systemd 等守护
  （42 非 0，Restart=on-failure 即可覆盖），否则退出后须手动再启动。

优雅停机为什么不用信号：uvicorn 用 ``signal.signal`` 注册停机处理器，在
uvloop 的 C 事件循环下信号处理可能长时间得不到执行（实测偶发悬挂）。
故 main.run 启动时把 Server 实例注册进来，重启直接置 ``should_exit``
（与收到 SIGTERM 的处理路径殊途同归），完全绕开信号投递。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import uvicorn

from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.app_config import AppConfigPayload, AppConfigView
from movieclaw_api.settings import AppServerSetting, get_setting_store

logger = logging.getLogger("movieclaw_api.app_config")

# 重启前的缓冲时间：留给 HTTP 响应写回客户端，避免前端拿不到「已开始重启」的确认
_RESTART_DELAY_SECONDS = 1.0
# 优雅停机的等待窗口：超过此时长仍未退出则强制退出，保证重启永不悬空
_FORCE_EXIT_SECONDS = 10.0

# 「设置页请求的应用重启」的约定退出码，entrypoint 的重启循环据此区分
# 重启请求（原地拉起新进程）与真故障/停机（整容器退出）。
RESTART_EXIT_CODE = 42
# 「应用内更新/回退后的全量重启」约定码：entrypoint 见到它会把前端一并重启，
# 并重新解析代码来源（data 卷上的 overlay 指针可能已切换，见 docker/entrypoint.sh）。
FULL_RESTART_EXIT_CODE = 43


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


async def build_config_view() -> AppConfigView:
    """装配设置页所需的配置视图。"""
    setting = await get_setting_store().get(AppServerSetting)
    return AppConfigView(external_url=setting.external_url)


def _validate_payload(payload: AppConfigPayload) -> None:
    """保存前校验，错误信息中文直达前端。"""
    url = payload.external_url.strip()
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise BadRequestException(
                "外部访问地址必须是完整的 http(s) 地址，如 http://192.168.1.10:3000"
            )


async def save_config(payload: AppConfigPayload) -> AppConfigView:
    """校验并保存应用设置（保存即生效）。"""
    _validate_payload(payload)
    setting = AppServerSetting(
        # 规范化：去掉尾部斜杠，后续拼接路径时不用再处理
        external_url=payload.external_url.strip().rstrip("/"),
    )
    await get_setting_store().set(setting)
    return AppConfigView(external_url=setting.external_url)


# ---------------------------------------------------------------------------
# 重启
# ---------------------------------------------------------------------------


# main.run 启动时注册的 uvicorn Server 实例（开发热重载模式下为 None）
_uvicorn_server: uvicorn.Server | None = None
# 本次进程退出应使用的重启约定码（42 后端 / 43 全量）；None = 非重启退出
_restart_exit_code: int | None = None


def register_uvicorn_server(server: uvicorn.Server) -> None:
    """由 main.run 在启动前调用，把 Server 实例交给重启服务。"""
    global _uvicorn_server
    _uvicorn_server = server


def restart_exit_code() -> int | None:
    """main.run 在 Server 停机后查询：本次退出应使用的重启约定码（非重启为 None）。"""
    return _restart_exit_code


def _request_graceful_exit(exit_code: int) -> None:
    """请求优雅停机：优先置 Server.should_exit，未注册实例时退回 SIGTERM。"""
    global _restart_exit_code
    _restart_exit_code = exit_code
    logger.info("正在按请求重启应用（约定码 %d）：优雅停机后由容器入口拉起新进程……", exit_code)
    if _uvicorn_server is not None:
        # 与 uvicorn 收到 SIGTERM 后的处理路径殊途同归，但不经过信号投递
        _uvicorn_server.should_exit = True
    else:
        # 开发热重载模式（reload 子进程里拿不到 Server 实例）：退回信号方案
        os.kill(os.getpid(), signal.SIGTERM)


def _terminate_self(exit_code: int) -> None:
    """触发优雅停机；超时未退出则以重启约定退出码强制退出。"""
    _request_graceful_exit(exit_code)
    # 优雅停机成功时进程直接消失，走不到下面；只有停机被拖住才会兜底
    time.sleep(_FORCE_EXIT_SECONDS)
    logger.warning("优雅停机超时（%.0f 秒），强制退出进程以完成重启", _FORCE_EXIT_SECONDS)
    logging.shutdown()  # 强制退出不走解释器清理，先冲刷日志缓冲，保住上面这行警告
    os._exit(exit_code)


def schedule_restart(exit_code: int = RESTART_EXIT_CODE) -> None:
    """调度一次应用重启：延迟片刻（先让响应回到前端）后优雅退出进程。

    exit_code 决定 entrypoint 的处理方式：42 保持当前代码来源、只重启后端
    （前端保持运行；默认，设置页重启），43 前后端全量重启并重新解析代码来源
    （应用内更新/回退用）。
    """
    timer = threading.Timer(_RESTART_DELAY_SECONDS, _terminate_self, args=(exit_code,))
    timer.daemon = True
    timer.start()
