"""movieclaw_net 出口层单元测试：代理路由决策、熔断状态机、配置热切换。"""

from __future__ import annotations

import pytest

from movieclaw_net import (
    CircuitOpenError,
    EgressConfig,
    EgressScope,
    ProxyMode,
    apply_egress_config,
    egress_transport,
    reset_all_breakers,
    resolve_proxy_url,
)
from movieclaw_net.breaker import CircuitBreaker


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例都从默认配置与空熔断表开始，避免模块级状态串味。"""
    apply_egress_config(EgressConfig())
    reset_all_breakers()
    yield
    apply_egress_config(EgressConfig())
    reset_all_breakers()


# ---------------------------------------------------------------------------
# 代理路由决策
# ---------------------------------------------------------------------------


def test_off_mode_never_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    apply_egress_config(EgressConfig(proxy_mode=ProxyMode.OFF, proxy_services=frozenset({"tmdb"})))
    assert resolve_proxy_url("tmdb") is None


def test_manual_mode_routes_only_enabled_services():
    apply_egress_config(
        EgressConfig(
            proxy_mode=ProxyMode.MANUAL,
            proxy_url="socks5://192.168.1.2:7891",
            proxy_services=frozenset({"tmdb", "site:mteam"}),
        )
    )
    assert resolve_proxy_url("tmdb") == "socks5://192.168.1.2:7891"
    assert resolve_proxy_url("site:mteam") == "socks5://192.168.1.2:7891"
    # 未勾选的服务直连
    assert resolve_proxy_url("douban") is None
    assert resolve_proxy_url("site:chdbits") is None


def test_env_mode_reads_environment(monkeypatch):
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:8080")
    apply_egress_config(EgressConfig(proxy_mode=ProxyMode.ENV, proxy_services=frozenset({"tmdb"})))
    assert resolve_proxy_url("tmdb") == "http://10.0.0.1:8080"
    monkeypatch.delenv("HTTPS_PROXY")
    assert resolve_proxy_url("tmdb") is None


def test_lan_scope_never_proxies():
    apply_egress_config(
        EgressConfig(
            proxy_mode=ProxyMode.MANUAL,
            proxy_url="http://127.0.0.1:7890",
            proxy_services=frozenset({"media_server"}),
        )
    )
    # 即使被（误）勾选，LAN 范围也强制直连
    assert resolve_proxy_url("media_server", EgressScope.LAN) is None


# ---------------------------------------------------------------------------
# 熔断状态机
# ---------------------------------------------------------------------------


def _tick(monkeypatch, value: float) -> None:
    monkeypatch.setattr("movieclaw_net.breaker.time.monotonic", lambda: value)


def test_breaker_opens_after_threshold_and_fast_fails(monkeypatch):
    _tick(monkeypatch, 100.0)
    breaker = CircuitBreaker("tmdb", failure_threshold=3, cooldown_seconds=60)
    for _ in range(2):
        breaker.record_failure()
    breaker.before_request()  # 未达阈值仍放行
    breaker.record_failure()  # 第 3 次连续失败 → open
    with pytest.raises(CircuitOpenError):
        breaker.before_request()


def test_breaker_half_open_probe_then_close(monkeypatch):
    _tick(monkeypatch, 100.0)
    breaker = CircuitBreaker("tmdb", failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()
    with pytest.raises(CircuitOpenError):
        breaker.before_request()
    # 冷却期结束：放行一个探测请求，并发的第二个仍快速失败
    _tick(monkeypatch, 161.0)
    breaker.before_request()
    with pytest.raises(CircuitOpenError):
        breaker.before_request()
    breaker.record_success()
    breaker.before_request()  # 已闭合，正常放行


def test_breaker_probe_failure_reopens(monkeypatch):
    _tick(monkeypatch, 100.0)
    breaker = CircuitBreaker("tmdb", failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        breaker.record_failure()
    _tick(monkeypatch, 161.0)
    breaker.before_request()  # half-open 探测
    breaker.record_failure()  # 探测失败：一次即重开，重新计冷却
    with pytest.raises(CircuitOpenError):
        breaker.before_request()


def test_http_status_counts_as_success():
    breaker = CircuitBreaker("tmdb", failure_threshold=2)
    breaker.record_failure()
    breaker.record_success()  # 拿到任意 HTTP 响应都算线路通
    breaker.record_failure()
    breaker.before_request()  # 连续计数被清零过，未达阈值


# ---------------------------------------------------------------------------
# transport 配置热切换
# ---------------------------------------------------------------------------


def test_transport_follows_breaker_registry_reset():
    """回归：配置保存会 reset_all_breakers()，已存在的 transport 必须跟随新状态。

    曾经 transport 在构造期缓存熔断器实例，注册表清空后仍攥着打开的旧熔断器，
    导致用户修好代理、保存配置后业务请求依旧被熔断快速失败。
    """
    from movieclaw_net import get_breaker

    egress_transport("tmdb")  # 构造 transport（旧实现会在此缓存熔断器实例）
    breaker = get_breaker("tmdb")
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()  # 打开熔断
    assert breaker.is_open
    reset_all_breakers()
    # transport 下一次请求取到的是注册表里的新熔断器（闭合态），不会快速失败
    fresh = get_breaker("tmdb")
    assert fresh is not breaker
    fresh.before_request()  # 不抛 CircuitOpenError 即为通过


@pytest.mark.asyncio
async def test_breaker_counts_proxy_failures():
    """回归：代理故障必须计入熔断。

    httpx.ProxyError 不是 NetworkError 的子类，而是 TransportError 下与之平级的
    分支。_NETWORK_FAILURES 漏掉它时，走 SOCKS5 的用户（节点掉线、分流规则
    REJECT、认证失败全归 ProxyError）整个熔断形同虚设——每个请求都要等满超时，
    正是熔断本该消灭的那种体验。
    """
    import httpx

    from movieclaw_net import get_breaker
    from movieclaw_net.egress import current_epoch

    assert not issubclass(httpx.ProxyError, httpx.NetworkError)  # 漏写的根源

    class _ProxyRefusing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ProxyError("host unreachable")

    transport = egress_transport("tmdb")
    transport._inner = _ProxyRefusing()
    transport._inner_epoch = current_epoch()
    request = httpx.Request("GET", "https://example.com/")
    for _ in range(3):
        with pytest.raises(httpx.ProxyError):
            await transport.handle_async_request(request)
    assert get_breaker("tmdb").is_open


def test_transport_rebuilds_inner_on_config_change():
    transport = egress_transport("tmdb")
    inner_before = transport._ensure_inner()
    # 配置未变：不重建
    assert transport._ensure_inner() is inner_before
    apply_egress_config(
        EgressConfig(
            proxy_mode=ProxyMode.MANUAL,
            proxy_url="http://127.0.0.1:7890",
            proxy_services=frozenset({"tmdb"}),
        )
    )
    inner_after = transport._ensure_inner()
    assert inner_after is not inner_before
    # 旧连接进退役列表，随 transport 一起关闭
    assert inner_before in transport._retired
