"""HTTP 客户端封装（docs/design/cli.md §8.3）。

职责：认证注入（Bearer 设备令牌）、统一超时、
`ApiResponse{success,code,message,data}` 信封拆解、错误 → 中文 CliError
（带退出码与 hint）映射、文件上传/下载、spec 版本偏斜检测（读响应头
X-Movieclaw-Spec-Hash）。业务命令层只拿到拆好信封的 data。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError, ExitCode

API_PREFIX = "/api/v1"
SPEC_HASH_HEADER = "X-Movieclaw-Spec-Hash"

# 最近一次请求观察到的 (服务器, 服务端 spec 指纹)——供偏斜刷新流程在命令
# 执行完后统一读取（gen/spec_loader.maybe_refresh），避免每个命令自行处理。
last_seen_spec_hash: str | None = None
last_seen_server: str | None = None

# 测试注入点：进程内测试用 httpx.MockTransport 替换真实网络（生产恒为 None）
default_transport: httpx.BaseTransport | None = None


class Api:
    """面向单个 movieclaw 服务器的同步 HTTP 客户端。"""

    def __init__(
        self,
        server: str,
        *,
        timeout: float = 30.0,
        debug: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.server = server
        self.debug = debug
        self.last_message: str | None = None
        headers = {"Accept": "application/json", "X-MovieClaw-Client": "cli"}
        # 凭证只有 Bearer 一条通道（docs/design/device-auth.md §6.2）。
        # 环境变量优先于落盘令牌：产品内 Agent 工作区注入的短时效令牌、
        # 以及 CI 里注入的令牌都走这条，且完全不落盘。
        if token := self.token_for(server):
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=server,
            timeout=timeout,
            headers=headers,
            transport=transport or default_transport,
        )

    @staticmethod
    def token_for(server: str) -> str | None:
        """解析该服务器要用的令牌：环境变量 > 本地凭证。"""
        return os.environ.get(cfg.ENV_TOKEN) or cfg.load_token(server)

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        upload_path: Path | None = None,
    ) -> Any:
        """发起请求并拆信封，返回 data 字段。错误统一抛 CliError。"""
        data, _ = self.request_raw(
            method, path, params=params, json_body=json_body, upload_path=upload_path
        )
        return data

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        upload_path: Path | None = None,
    ) -> tuple[Any, httpx.Response]:
        """同 request，但额外返回原始响应（登录要读 Set-Cookie）。"""
        files = None
        if upload_path is not None:
            files = {"file": (upload_path.name, upload_path.read_bytes())}
        response = self._send(method, path, params=params, json_body=json_body, files=files)
        return self._parse(response), response

    def stream_sse(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        last_event_id: int | None = None,
    ):
        """订阅一个 SSE 端点，产出 SseEvent（生成器；调用方 for 循环消费）。

        错误统一归一为 CliError(NETWORK)：连接失败、流中途断开、长时间无
        数据（read 超时 120s，防半开连接永久挂死——服务端有心跳/事件节奏，
        正常流不会触发）。调用方据 NETWORK 退出码决定重连（Agent 流用
        last_event_id 续传）或如实报告不完整（搜索流）。
        """
        from movieclaw_cli.core.sse import parse_sse_lines

        url = f"{API_PREFIX}{path}"
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        try:
            with self._client.stream(
                "GET", url, params=params, headers=headers, timeout=httpx.Timeout(30, read=120)
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    self._parse(response)  # 统一错误映射（会抛 CliError）
                yield from parse_sse_lines(response.iter_lines())
        except httpx.ConnectError as exc:
            raise CliError(
                f"无法连接 movieclaw 服务器：{self.server}",
                exit_code=ExitCode.NETWORK,
                hint="确认服务已启动、地址正确（含端口）",
            ) from exc
        except httpx.TimeoutException as exc:
            raise CliError(
                "事件流超过 120 秒没有任何数据，已断开",
                exit_code=ExitCode.NETWORK,
                hint="网络可能不稳或服务假死；可重试",
            ) from exc
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
            raise CliError(
                f"事件流中断：{exc}",
                exit_code=ExitCode.NETWORK,
                hint="网络可能不稳或服务已重启；可重试",
            ) from exc

    def download(self, path: str, target: Path, *, params: dict[str, Any] | None = None) -> None:
        """文件直出端点：把响应内容写到本地文件。"""
        response = self._send("GET", path, params=params)
        if response.is_error:
            self._parse(response)  # 统一走错误映射（会抛 CliError）
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{API_PREFIX}{path}"
        if self.debug:
            print(f"[debug] {method} {self.server}{url} params={params}", file=sys.stderr)
        try:
            response = self._client.request(method, url, params=params, json=json_body, files=files)
        except httpx.ConnectError as exc:
            raise CliError(
                f"无法连接 movieclaw 服务器：{self.server}",
                exit_code=ExitCode.NETWORK,
                hint="确认服务已启动、地址正确（含端口）；"
                "地址来源优先级：--server > MOVIECLAW_SERVER > 当前上下文",
            ) from exc
        except httpx.TimeoutException as exc:
            raise CliError(
                f"请求超时（{self.server}{url}）",
                exit_code=ExitCode.NETWORK,
                hint="可用 --timeout 调大超时时间",
            ) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # 兜底所有传输层异常（UnsupportedProtocol / ReadError /
            # RemoteProtocolError / ProxyError…），绝不向用户裸抛 traceback
            raise CliError(
                f"与服务器通信失败：{exc}",
                exit_code=ExitCode.NETWORK,
                hint=f"检查服务器地址格式（须含 http:// 前缀，当前为 {self.server}）与网络状况",
            ) from exc
        if self.debug:
            print(f"[debug] -> {response.status_code}", file=sys.stderr)
        if spec_hash := response.headers.get(SPEC_HASH_HEADER):
            global last_seen_spec_hash, last_seen_server
            last_seen_spec_hash = spec_hash
            last_seen_server = self.server
        return response

    def _parse(self, response: httpx.Response) -> Any:
        self.last_message = None
        if response.status_code == 401:
            # 优先透传服务端的具体原因（如「用户名或密码错误」），
            # 只在拿不到时才用通用话术
            try:
                message = (response.json() or {}).get("message")
            except ValueError:
                message = None
            raise CliError(
                message or "未登录或会话已过期",
                exit_code=ExitCode.AUTH,
                hint="请先执行 mclaw login（或检查 MOVIECLAW_TOKEN 是否有效）",
            )
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise CliError(
                f"服务器返回了无法解析的响应（HTTP {response.status_code}）",
                exit_code=ExitCode.BUSINESS,
                hint="确认 --server 指向的是 movieclaw 服务而非其他程序",
            ) from exc
        # 统一信封：success 字段存在即为 ApiResponse/ErrorResponse
        if isinstance(payload, dict) and "success" in payload:
            if payload.get("success"):
                self.last_message = payload.get("message")
                return payload.get("data")
            raise CliError(
                payload.get("message") or f"请求失败（HTTP {response.status_code}）",
                exit_code=ExitCode.BUSINESS,
                code=payload.get("code"),
                details=payload.get("details"),
            )
        # 非信封 JSON（如 /health）原样返回
        if response.is_error:
            raise CliError(
                f"请求失败（HTTP {response.status_code}）",
                exit_code=ExitCode.BUSINESS,
                details=payload,
            )
        return payload
