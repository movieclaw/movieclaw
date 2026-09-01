"""统一网络出口：按「服务标签」决定代理路由，并内建熔断的 httpx transport 工厂。

背景与定位
----------
本项目依赖大量外部服务（TMDB、豆瓣、各 PT 站、LLM、图床……），部署环境的
网络不可预测：有的能直连、有的要走代理、有的要换镜像。此前每个客户端各自
``httpx.AsyncClient(...)``，代理/熔断策略无处统一落地。本模块把「出口」收成
一个口子：

- 上层（movieclaw_media / movieclaw_tracker / movieclaw_llm / movieclaw_api）
  构造客户端时注入 ``egress_transport("服务标签")`` 返回的 transport；
- 代理路由按服务标签查当前配置（用户在「设置 → 网络」里维护）；
- 熔断器包在 transport 里，全链路自动获得快速失败能力。

为什么选 transport 注入而不是统一建客户端：各域客户端的请求头/超时/限流
各有讲究，且本就为测试预留了 ``transport`` 注入口；而 httpx 的代理恰好是
transport 级参数——注入点天然重合，域包几乎零改动。

服务标签约定
------------
    tmdb / douban / image / llm / media_server / site:<site_id>

配置热更新：``apply_egress_config()`` 递增 epoch，transport 在下一次请求时
发现 epoch 变化即按新配置重建内层连接——**改代理无需重启、无需重建单例**。
"""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from movieclaw_net.breaker import CircuitBreaker, get_breaker

logger = logging.getLogger("movieclaw_net.egress")

# 熔断器只统计这些"线路不通"级别的失败；HTTP 状态码错误不计入。
# ProxyError 必须单列：它不是 NetworkError 的子类，而是 TransportError 下与之
# 平级的分支。漏掉它意味着**代理故障一次都不计数**——对走 SOCKS5 的用户
# （节点掉线、分流规则 REJECT、认证失败全归 ProxyError）整个熔断形同虚设，
# 每个请求都得等满超时，正是熔断本该消灭的那种体验。
_NETWORK_FAILURES = (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError)

# 允许的代理协议：http(s) 与 socks5。
# 注意 socks5 与 socks5h 在 httpx 下**行为完全一致**：httpcore 把目标主机名
# 原样以 ATYP=DOMAINNAME 发给代理（见 socksio.utils.encode_address），域名
# 一律由代理端解析，本地不做 DNS。保留 socks5h 只为兼容用户从别处抄来的
# 地址，改写成它并不会改变解析行为，不必拿它去试被污染的域名。
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def browser_tls_context() -> ssl.SSLContext:
    """构造与 curl/浏览器同特征的 TLS 上下文，规避按指纹拦截的 CDN 机器人检测。

    背景：豆瓣图床迁至腾讯 EdgeOne 后，部分边缘节点的机器人检测按 TLS
    ClientHello 指纹（JA3）拦截 Python ssl 的默认配置——症状是请求头再像
    浏览器也返回 200 + JS 挑战页（text/html）而非图片。实测同一容器、同一
    出口 IP 下，仅把密码套件从 Python 默认换成 OpenSSL 默认（``DEFAULT``，
    与 curl 的 ClientHello 特征一致）即可正常放行。证书校验保持开启不变。
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT")
    return ctx


class EgressScope(StrEnum):
    """出口范围：LAN（内网服务）在任何配置下都不走代理。"""

    WAN = "wan"
    LAN = "lan"


class ProxyMode(StrEnum):
    OFF = "off"  # 全部直连
    ENV = "env"  # 代理地址取自环境变量（HTTPS_PROXY 等），Docker 部署最顺手
    MANUAL = "manual"  # 用户在设置页手填代理地址


@dataclass(frozen=True)
class EgressConfig:
    """出口层的生效配置（由 movieclaw_api 从配置域加载后灌入）。

    默认值即「未配置过」的行为：跟随环境变量，TMDB、图片回源与 GitHub 更新
    走代理——这是国内部署最常见的诉求（TMDB / GitHub 被墙），同时不影响
    PT 站直连更快的现实。
    """

    proxy_mode: ProxyMode = ProxyMode.ENV
    proxy_url: str = ""
    proxy_services: frozenset[str] = field(
        default_factory=lambda: frozenset({"tmdb", "image", "github"})
    )


_config = EgressConfig()
_epoch = 0  # 配置代次：transport 据此判断是否需要按新配置重建内层连接


def apply_egress_config(config: EgressConfig) -> None:
    """应用新的出口配置（启动时与设置保存后调用）。"""
    global _config, _epoch
    _config = config
    _epoch += 1
    logger.info(
        "网络出口配置已生效：模式=%s，走代理的服务=%s",
        config.proxy_mode,
        sorted(config.proxy_services) or "（无）",
    )


def get_egress_config() -> EgressConfig:
    return _config


def current_epoch() -> int:
    return _epoch


def env_proxy_url() -> str | None:
    """从环境变量取代理地址（大小写都认，优先级从专到泛）。

    公开导出：设置页需要把探测到的环境变量代理展示给用户确认。
    """
    for name in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        value = os.environ.get(name) or os.environ.get(name.lower())
        if value:
            return value
    return None


def resolve_proxy_url(service: str, scope: EgressScope = EgressScope.WAN) -> str | None:
    """解析某服务当前应使用的代理地址；None = 直连。"""
    if scope is EgressScope.LAN:
        return None
    cfg = _config
    if cfg.proxy_mode is ProxyMode.OFF:
        return None
    if service not in cfg.proxy_services:
        return None
    if cfg.proxy_mode is ProxyMode.MANUAL:
        return cfg.proxy_url or None
    return env_proxy_url()


class EgressTransport(httpx.AsyncBaseTransport):
    """带「代理路由 + 熔断 + 配置热更新」的 transport。

    内层是标准 ``httpx.AsyncHTTPTransport``；每次请求前对比配置 epoch，
    发现配置变了就用新代理地址重建内层。被替换的旧内层不立即关闭
    （可能仍有在途请求），挂在退役列表里随本 transport 一起关闭——
    配置变更是低频操作，这点驻留可忽略。
    """

    def __init__(
        self,
        service: str,
        *,
        scope: EgressScope = EgressScope.WAN,
        http2: bool = False,
        use_breaker: bool = True,
        verify: ssl.SSLContext | bool = True,
    ) -> None:
        self._service = service
        self._scope = scope
        self._http2 = http2
        self._use_breaker = use_breaker
        self._verify = verify
        self._inner: httpx.AsyncHTTPTransport | None = None
        self._inner_epoch = -1
        self._retired: list[httpx.AsyncHTTPTransport] = []

    def _ensure_inner(self) -> httpx.AsyncHTTPTransport:
        if self._inner is None or self._inner_epoch != current_epoch():
            proxy = resolve_proxy_url(self._service, self._scope)
            if self._inner is not None:
                self._retired.append(self._inner)
                logger.info(
                    "服务 %s 的出口配置变更，已切换连接（代理=%s）", self._service, proxy or "直连"
                )
            self._inner = httpx.AsyncHTTPTransport(
                proxy=proxy, http2=self._http2, verify=self._verify
            )
            self._inner_epoch = current_epoch()
        return self._inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        inner = self._ensure_inner()
        # 熔断器每次请求都从注册表现取，而不是构造期缓存实例：
        # reset_all_breakers()（配置保存/测试隔离）清空注册表后，
        # 已存在的 transport 必须立刻跟随新状态，不能攥着旧熔断器不放
        breaker: CircuitBreaker | None = get_breaker(self._service) if self._use_breaker else None
        if breaker is not None:
            breaker.before_request()
        try:
            response = await inner.handle_async_request(request)
        except _NETWORK_FAILURES:
            if breaker is not None:
                breaker.record_failure()
            raise
        if breaker is not None:
            breaker.record_success()
        return response

    async def aclose(self) -> None:
        for transport in self._retired:
            await transport.aclose()
        self._retired.clear()
        if self._inner is not None:
            await self._inner.aclose()
            self._inner = None


def egress_transport(
    service: str,
    *,
    scope: EgressScope = EgressScope.WAN,
    http2: bool = False,
    use_breaker: bool = True,
    verify: ssl.SSLContext | bool = True,
) -> EgressTransport:
    """构造某服务标签的出口 transport（客户端构造时注入 ``transport=``）。

    同一服务的多个 transport（如 PT 站的临时客户端）共享同一个熔断器，
    失败统计与快速失败行为全进程一致。连通性测试等"必须真发请求"的场景
    传 ``use_breaker=False`` 绕过熔断。
    """
    return EgressTransport(
        service, scope=scope, http2=http2, use_breaker=use_breaker, verify=verify
    )
