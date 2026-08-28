"""应用设置的业务服务（routes/app_config 的实现层）。

职责：
- 配置读写：外部访问地址的校验与落库（保存即生效，纯落库数据）；
- 对外端口：读写 data 卷上的端口设置文件，并以全量重启（43）让它生效；
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
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import uvicorn

from movieclaw_api.core.config import get_settings
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
    """装配设置页所需的配置视图（落库配置 + 对外端口的运行时状态）。"""
    setting = await get_setting_store().get(AppServerSetting)
    port, source, rejected = read_web_port_state()
    return AppConfigView(
        external_url=setting.external_url,
        web_port=port,
        web_port_source=source,
        web_port_default=DEFAULT_WEB_PORT,
        web_port_rejected=rejected,
        web_port_configurable=_web_port_managed(),
    )


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
    # 系统外部访问地址变化后，重新计算远程转码的回退地址；如果远程转码配置了
    # 专用 BASE_URL，则有效地址保持不变。下一次播放都会读取刷新后的快照。
    from movieclaw_api.services.playback.remote_config import (
        refresh_remote_transcode_config,
    )

    await refresh_remote_transcode_config()
    return await build_config_view()


# ---------------------------------------------------------------------------
# 对外端口
# ---------------------------------------------------------------------------
#
# 事实源是 data 卷上的一个纯文本文件（默认 data/config/web-port），不是数据库。
# 理由：真正消费这个值的是 docker/entrypoint.sh 和 Docker HEALTHCHECK 两个
# **不读数据库**的 shell；而且新端口绑不上时 entrypoint 必须能就地把它废弃并
# 回落（否则用户改错端口就再也进不来了）——数据库里再存一份必然出现两套真相。
#
# 解析口径（文件 > MOVIECLAW_WEB_PORT 环境变量 > 3000）与
# docker/resolve-web-port.sh 严格一致，改一处必须同步改另一处。

DEFAULT_WEB_PORT = 3000
# 容器内后端固定监听 8000，且它是 Next 反代的目标（构建时固化）。对外端口撞上
# 它会让前端起不来、反代链路自指，直接在保存时拦掉。
_RESERVED_WEB_PORTS = {8000}


def _web_port_file() -> Path:
    return Path(get_settings().web_port_file)


def _parse_port(raw: str | None) -> int | None:
    """把一段文本解析成合法端口；非数字/越界一律 None（按「未配置」处理）。"""
    text = (raw or "").strip()
    if not text.isdigit() or len(text) > 5:
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def _read_port_file(path: Path) -> int | None:
    try:
        return _parse_port(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _web_port_managed() -> bool:
    """当前部署形态下改端口是否有意义。

    判据是 entrypoint 导出的 MOVIECLAW_CODE_SOURCE：有它 = 前端进程由容器入口
    托管，改设置 + 全量重启才能真正换端口。源码部署下前端是开发者自己起的，
    应用无从干预，设置页据此把入口置灰而不是给一个不生效的开关。
    """
    return bool(os.environ.get("MOVIECLAW_CODE_SOURCE"))


def read_web_port_state() -> tuple[int, str, int | None]:
    """返回（当前生效端口, 来源, 上次被自动废弃的端口）。"""
    path = _web_port_file()
    port = _read_port_file(path)
    if port is not None:
        source = "setting"
    else:
        env_port = _parse_port(os.environ.get("MOVIECLAW_WEB_PORT"))
        port, source = (env_port, "env") if env_port is not None else (DEFAULT_WEB_PORT, "default")
    return port, source, _read_port_file(path.with_name(path.name + ".rejected"))


async def save_web_port(port: int | None) -> AppConfigView:
    """写入对外端口设置并调度全量重启；port 为 None/0 表示恢复默认。

    重启必须走 43（全量）而不是 42：42 只重启后端、前端进程原地不动，端口是
    前端监听的，不重启前端就不会变。
    """
    if not _web_port_managed():
        raise BadRequestException(
            "当前部署形态不支持应用内修改端口：前端进程不由容器入口托管，"
            "请直接调整启动命令或反向代理"
        )
    if port is not None and port != 0:
        if not 1 <= port <= 65535:
            raise BadRequestException("端口需在 1~65535 之间")
        if port in _RESERVED_WEB_PORTS:
            raise BadRequestException(
                f"{port} 是容器内后端占用的端口，换一个（前端与后端不能同口）"
            )

    path = _web_port_file()
    current, _, _ = read_web_port_state()
    rejected = path.with_name(path.name + ".rejected")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if port is None or port == 0:
            path.unlink(missing_ok=True)
        else:
            # 先写临时文件再原子替换：容器恰在此刻被拉走时，entrypoint 下次
            # 启动读到的要么是旧值要么是新值，不会是半截内容
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(f"{port}\n", encoding="utf-8")
            tmp.replace(path)
        # 用户重新设了端口 = 上一次的失败记录已经翻篇，清掉外显
        rejected.unlink(missing_ok=True)
    except OSError as exc:
        raise BadRequestException(f"端口设置写入失败：{exc}") from exc

    view = await build_config_view()
    # 只有实际生效的端口变了才重启。清除设置后回落到的默认值恰好等于当前端口
    # 时（很常见），重启是纯粹的打扰。
    if view.web_port != current:
        logger.info("对外端口已改为 %d（原 %d），即将全量重启应用使其生效", view.web_port, current)
        schedule_restart(FULL_RESTART_EXIT_CODE)
    return view


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
