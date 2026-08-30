"""客户端地址判定测试（``api/client_address.py``）。

这块代码唯一的职责是回答「这个地址能不能用来区分是哪台机器」。答错的代价
是实打实的：答成能，审批卡会把一个所有设备共用的网关地址摆出来当身份线索，
限流也会把整个局域网塞进同一个计数桶；答成不能，就白白丢掉一条真线索。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from movieclaw_api.api import client_address as module


@pytest.fixture(autouse=True)
def _isolated_route_table(tmp_path, monkeypatch):
    """把路由表指向一个不存在的文件，默认「没有网关」。

    换的是数据源而不是函数本身：真正的解析逻辑（小端十六进制）也得跑到，
    把 default_gateways 整个替换掉就等于绕开了它。
    """
    monkeypatch.setattr(module, "_ROUTE_TABLE", tmp_path / "no-route")
    module.default_gateways.cache_clear()
    yield
    module.default_gateways.cache_clear()


def _write_route(path, gateway_hex: str):
    """写一张只有一条默认路由的 ``/proc/net/route``。"""
    path.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        f"eth0\t00000000\t{gateway_hex}\t0003\t0\t0\t0\t00000000\t0\t0\t0\n",
        encoding="utf-8",
    )


def _request(host: str | None):
    client = None if host is None else SimpleNamespace(host=host)
    return SimpleNamespace(client=client)


def test_lan_address_identifies_a_machine():
    assert module.client_address(_request("192.168.1.42")) == "192.168.1.42"


def test_public_address_identifies_a_machine():
    assert module.client_address(_request("203.0.113.7")) == "203.0.113.7"


def test_loopback_is_not_a_client_identity():
    """看到环回说明前面那层反代没转发 X-Forwarded-For，我们看到的是它自己。"""
    assert module.client_address(_request("127.0.0.1")) == ""
    assert module.client_address(_request("::1")) == ""


def test_bridge_gateway_is_not_a_client_identity(tmp_path, monkeypatch):
    """容器桥接网络把源地址 NAT 成网关，全网设备长得一模一样。"""
    route = tmp_path / "route"
    _write_route(route, "010011AC")  # 小端 → 172.17.0.1
    monkeypatch.setattr(module, "_ROUTE_TABLE", route)
    module.default_gateways.cache_clear()
    assert module.client_address(_request("172.17.0.1")) == ""
    # 同网段里真实的另一台机器仍然算数
    assert module.client_address(_request("172.17.0.5")) == "172.17.0.5"


def test_unspecified_and_missing_and_garbage_are_all_empty():
    assert module.client_address(_request("0.0.0.0")) == ""
    assert module.client_address(_request(None)) == ""
    assert module.client_address(_request("")) == ""
    # TestClient 之类给的是非 IP 字符串
    assert module.client_address(_request("testclient")) == ""


def test_default_gateways_parses_little_endian_route_table(tmp_path, monkeypatch):
    """``/proc/net/route`` 的网关字段是小端十六进制，读反了会认错网关。

    ``010011AC`` 按小端读是 172.17.0.1；按大端读会得到 1.0.17.172——一个不
    存在的地址，判定就整个失效了。
    """
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        # 非默认路由（目的地址不是全 0）不算网关
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_ROUTE_TABLE", route)
    module.default_gateways.cache_clear()
    assert module.default_gateways() == frozenset({"172.17.0.1"})


def test_default_gateways_is_empty_when_route_table_is_absent():
    """非 Linux（本地开发的 macOS）上没有这个文件，不该炸。

    路由表已由 autouse fixture 指向一个不存在的路径。
    """
    assert module.default_gateways() == frozenset()
