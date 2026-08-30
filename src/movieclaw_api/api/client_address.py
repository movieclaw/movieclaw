"""取请求的客户端地址，并对「取不到」的情况诚实收口。

## 这个值是怎么来的

链路是 `客户端 → docker 端口映射 → 容器内 nginx:3000 → uvicorn:8000`。

uvicorn 的 ``ProxyHeadersMiddleware`` 默认就是开的（``proxy_headers=True``，
``forwarded_allow_ips`` 默认 ``127.0.0.1``，可用 ``FORWARDED_ALLOW_IPS``
环境变量覆盖）。nginx 从 loopback 连上来，落在信任名单里，中间件于是按
``X-Forwarded-For`` 改写 ``scope["client"]``。而 nginx 发的是
``$proxy_add_x_forwarded_for``，即「客户端自己带的 XFF, nginx 看到的
$remote_addr」。中间件从右往左跳过可信项取第一个不可信的，拿到的正是
**nginx 看到的那个地址**。

所以 ``request.client.host`` 已经是「nginx 眼里的对端」，不是 nginx 自己。
这里不再自己解析一遍 XFF：一来会和中间件打架，二来自己取左端会引入伪造面
——客户端伪造的条目永远在左边，中间件的从右往左策略取不到它们。

## 为什么还要判定

桥接网络的容器经常看不到真实客户端地址：Docker 会把源地址 NAT 成网桥网关
（最常见的 ``172.17.0.1``），Docker Desktop 更是全部流量都这样。这时**每台
设备看起来都一模一样**，这个值既不能用来认人，也不能用来限流。

审批卡上写着「如果这不是你刚发起的操作，选择拒绝」，让人照着一个对所有设备
都相同的数字做安全判断，比不显示更糟。所以这类地址一律当作「取不到」，
由上层决定怎么呈现和怎么限流。

想拿到真实地址，得让容器能直接看到客户端连接：用 host 网络，或在外层反代
上把 movieclaw 容器所在的地址加进 ``FORWARDED_ALLOW_IPS``。
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from functools import lru_cache
from pathlib import Path

from fastapi import Request

#: Linux 路由表。非 Linux（本地开发的 macOS）上不存在，读不到就当没有网关。
_ROUTE_TABLE = Path("/proc/net/route")


@lru_cache(maxsize=1)
def default_gateways() -> frozenset[str]:
    """本机（容器内）的 IPv4 默认网关集合。

    ``/proc/net/route`` 里目的地址为全 0 的那几行就是默认路由，网关字段是
    **小端**十六进制。进程生命周期内缓存：容器跑着的时候默认路由不会变，
    真变了也会连带重启。

    只看 IPv4：容器里 IPv6 通常直接禁用，而 NAT 改写源地址这件事本身也是
    IPv4 桥接网络特有的。
    """
    try:
        lines = _ROUTE_TABLE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()

    gateways: set[str] = set()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            raw = int(fields[2], 16)
        except ValueError:
            continue
        if raw == 0:
            continue
        try:
            gateways.add(socket.inet_ntoa(struct.pack("<L", raw)))
        except (OSError, struct.error):  # pragma: no cover - 字段畸形
            continue
    return frozenset(gateways)


def is_identifiable(host: str) -> bool:
    """这个地址能不能用来区分「是哪一台机器」。

    三类当作不能：

    * 环回：说明前面有个本地反代没转发 ``X-Forwarded-For``，我们看到的是它；
    * 未指定地址（``0.0.0.0`` / ``::``）：不是任何一台机器；
    * 等于本机默认网关：桥接网络 NAT 的典型结果，全网设备都长这样。

    最后一条会误伤一种情况——请求真的来自 Docker 宿主机本身（它在网桥上
    就是网关地址）。但那种情况下这个地址同样区分不出是宿主机上的哪个程序，
    当作「取不到」并不损失信息。
    """
    text = host.strip()
    if not text:
        return False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        # 不是合法 IP（Unix socket 之类）就没法拿来认人
        return False
    if address.is_loopback or address.is_unspecified:
        return False
    return text not in default_gateways()


def client_address(request: Request) -> str:
    """返回可用于区分设备的客户端地址；判定为不可信时返回空串。

    调用方拿到空串时**不要**编一个占位地址填上去：界面要如实说「无法确定」，
    限流也要换一条不依赖地址的路径，否则所有设备会共用同一个计数桶，
    一台机器刷屏就能把整个局域网锁在门外。
    """
    client = request.client
    if client is None:
        return ""
    host = client.host or ""
    return host if is_identifiable(host) else ""


__all__ = ["client_address", "default_gateways", "is_identifiable"]
