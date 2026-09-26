"""UDP 自动发现的应答地址选择（movieclaw_jellyfin.udp._resolve_address）。

重点场景：Docker 桥接部署下探测到的是容器内网 IP，此时应改用用户在
「设置 → 网络 → 外部访问地址」填的地址，而不是把 172.x 回给播放器。
"""

from __future__ import annotations

import logging

import pytest

from movieclaw_api.settings import AppServerSetting
from movieclaw_jellyfin import udp


class _FakeStore:
    def __init__(self, external_url: str) -> None:
        self._setting = AppServerSetting(external_url=external_url)

    async def get(self, schema):
        assert schema is AppServerSetting
        return self._setting


@pytest.fixture
def env(monkeypatch):
    """probe=探测出的本机 IP；external=外部访问地址。返回一个设置函数。"""

    def configure(*, probe: str | None, external: str = "") -> None:
        monkeypatch.setattr(udp, "_probe_outbound_ip", lambda _client_ip: probe)
        monkeypatch.setattr(udp, "get_setting_store", lambda: _FakeStore(external))

    return configure


async def test_published_server_url_wins(env):
    env(probe="172.17.0.3", external="http://192.168.1.10:3000")
    address = await udp._resolve_address("192.168.1.50", 3000, "http://nas.lan:3000/")
    assert address == "http://nas.lan:3000"


async def test_lan_probe_used_directly_even_with_external_url(env):
    """host 网络/裸机：探测到的局域网 IP 优先，不被（可能是公网域名的）外部访问地址覆盖。"""
    env(probe="192.168.1.10", external="https://movie.example.com")
    address = await udp._resolve_address("192.168.1.50", 3000, "")
    assert address == "http://192.168.1.10:3000"


async def test_docker_bridge_falls_back_to_external_url(env, caplog):
    env(probe="172.17.0.3", external="http://192.168.1.10:3000/")
    with caplog.at_level(logging.WARNING, logger="movieclaw_jellyfin.udp"):
        address = await udp._resolve_address("192.168.1.50", 3000, "")
    assert address == "http://192.168.1.10:3000"
    assert not caplog.records


async def test_docker_bridge_without_config_still_answers_with_warning(env, caplog):
    env(probe="172.17.0.3")
    with caplog.at_level(logging.WARNING, logger="movieclaw_jellyfin.udp"):
        address = await udp._resolve_address("192.168.1.50", 3000, "")
    assert address == "http://172.17.0.3:3000"
    assert "外部访问地址" in caplog.text


async def test_probe_failure_skips_reply(env):
    env(probe=None, external="http://192.168.1.10:3000")
    assert await udp._resolve_address("192.168.1.50", 3000, "") is None
