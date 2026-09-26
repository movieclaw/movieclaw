"""UDP 7359 自动发现应答（设计文档 3.1）。

收到含 "who is JellyfinServer?"（大小写不敏感）的报文时，单播回源一个
JSON。地址选择顺序：

1. ``published_server_url`` 显式配置（高级项，无界面入口）；
2. UDP connect() 探测出口 IP——不在 Docker 内部网段就直接用（host 网络/裸机）；
3. 探测结果疑似 Docker 内网时，改用「设置 → 网络 → 外部访问地址」
   （``app.server.external_url``）——桥接部署下容器内拿不到宿主局域网 IP，
   这是用户在界面上唯一能填、也最接近真实可达地址的配置；
4. 以上都没有：仍用探测 IP 应答并写告警（宁可给个可能错的地址让用户看到
   问题，也不无声失败；与 Jellyfin 的"取不到就沉默"不同，属有意偏离）。

外部访问地址只在第 3 步兜底、不前置：它可能是公网域名，host 网络下探测
出的局域网 IP 对局域网播放器更直接，不应被它覆盖。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket

from movieclaw_api.settings import AppServerSetting, get_setting_store
from movieclaw_api.settings.schemas import get_jellyfin_compat

logger = logging.getLogger("movieclaw_jellyfin.udp")

DISCOVERY_PORT = 7359
_MAGIC = "who is jellyfinserver?"

_protocol_transport: asyncio.DatagramTransport | None = None


def _probe_outbound_ip(client_ip: str) -> str | None:
    """UDP connect（不发包）读内核路由面向该客户端的出口地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((client_ip, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def _looks_like_docker_bridge(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr in ipaddress.ip_network("172.16.0.0/12")


async def _resolve_address(client_ip: str, http_port: int, published_server_url: str) -> str | None:
    """按模块文档的顺序选出应答给该客户端的服务器地址；None = 无法确定，跳过应答。"""
    if published_server_url:
        return published_server_url.rstrip("/")
    ip = _probe_outbound_ip(client_ip)
    if ip is None:
        logger.warning("自动发现：无法确定面向 %s 的本机地址，跳过应答", client_ip)
        return None
    if not _looks_like_docker_bridge(ip):
        return f"http://{ip}:{http_port}"
    external_url = (await get_setting_store().get(AppServerSetting)).external_url.strip()
    if external_url:
        return external_url.rstrip("/")
    logger.warning(
        "自动发现探测到的地址 %s 疑似 Docker 内部网段，播放器可能无法连接。"
        "请在「设置 → 网络 → 外部访问地址」填写局域网内可访问的地址"
        "（如 http://192.168.1.10:3000），或改用 host 网络模式部署",
        ip,
    )
    return f"http://{ip}:{http_port}"


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, http_port: int) -> None:
        self._http_port = http_port
        # 事件循环只持任务弱引用，不自持会被 GC 掉在飞的应答
        self._tasks: set[asyncio.Task] = set()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        if _MAGIC not in text.lower():
            return
        task = asyncio.get_running_loop().create_task(self._respond(addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _respond(self, addr: tuple[str, int]) -> None:
        try:
            setting = await get_jellyfin_compat()
            address = await _resolve_address(addr[0], self._http_port, setting.published_server_url)
            if address is None:
                return
            payload = {
                "Address": address,
                "Id": setting.server_id,
                "Name": setting.server_name,
                # 对齐真 Jellyfin：该字段不省略（UDP 响应不走 null 省略约定）
                "EndpointAddress": None,
            }
            self._transport.sendto(json.dumps(payload).encode("utf-8"), addr)
            logger.debug("已应答自动发现请求：%s ← %s", addr[0], address)
        except Exception:
            logger.exception("自动发现应答失败")


async def start_discovery(http_port: int) -> None:
    """绑定 UDP 7359 开始监听。端口被占/无权限时告警并跳过（发现是锦上添花）。"""
    global _protocol_transport
    setting = await get_jellyfin_compat()
    if not setting.enabled:
        logger.info("Jellyfin 兼容层已关闭，跳过自动发现监听")
        return
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(http_port),
            local_addr=("0.0.0.0", DISCOVERY_PORT),
        )
        _protocol_transport = transport
        logger.info("Jellyfin 自动发现已监听 UDP %d", DISCOVERY_PORT)
    except OSError as exc:
        logger.warning(
            "无法监听 UDP %d（%s），局域网自动发现不可用；播放器手动填地址不受影响",
            DISCOVERY_PORT,
            exc,
        )


def stop_discovery() -> None:
    global _protocol_transport
    if _protocol_transport is not None:
        _protocol_transport.close()
        _protocol_transport = None
