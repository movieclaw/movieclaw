"""spec 版本偏斜的刷新与回退路径（docs/design/cli.md §2.1 / §3.1，P1 验证标准）。

场景：老 CLI（内置基线较旧）× 新服务器。CLI 从响应头发现指纹不一致后
拉取 /spec 写缓存，下次调用装载缓存；缓存损坏时回退内置基线并自愈。
测试直接驱动 spec_loader 的刷新函数（同一段生产代码路径），服务器是
真实 uvicorn。
"""

from __future__ import annotations

import json

import pytest
from tests.cli.conftest import pair_cli_token

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core import http as http_mod
from movieclaw_cli.gen import spec_loader


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "cli-home"
    home.mkdir()
    monkeypatch.setenv("MOVIECLAW_CONFIG_DIR", str(home))
    monkeypatch.setattr(http_mod, "last_seen_spec_hash", None)
    monkeypatch.setattr(http_mod, "last_seen_server", None)
    return home


def _pair_and_store_token(live_server: str, admin: dict[str, str]) -> None:
    """刷新 /spec 需要授权，先走设备流拿一枚令牌落进隔离的配置目录。"""
    cfg.save_token(live_server, pair_cli_token(live_server, admin))


class _Settings:
    """maybe_refresh 只需要 make_api 与 debug 两个成员，够用即可。"""

    debug = False

    def make_api(self, server: str) -> http_mod.Api:
        return http_mod.Api(server, timeout=10)


def test_refresh_writes_cache_and_next_load_uses_it(isolated_home, live_server, admin) -> None:
    _pair_and_store_token(live_server, admin)

    # 装载内置基线后，模拟「服务端指纹与本地不一致」（老 CLI × 新服务器）
    spec_loader.load_active(None)
    http_mod.last_seen_spec_hash = "0000000000000000"
    http_mod.last_seen_server = live_server

    spec_loader.maybe_refresh(_Settings())

    spec, from_cache = spec_loader.load_active(live_server)
    assert from_cache, "刷新后应存在该服务器的缓存 spec"
    assert "paths" in spec


def test_no_refresh_when_hash_matches(isolated_home, live_server, admin) -> None:
    _pair_and_store_token(live_server, admin)
    spec_loader.load_active(None)
    http_mod.last_seen_spec_hash = spec_loader.active_spec_hash
    http_mod.last_seen_server = live_server

    spec_loader.maybe_refresh(_Settings())

    _spec, from_cache = spec_loader.load_active(live_server)
    assert not from_cache, "指纹一致不应产生缓存"


def test_corrupt_cache_falls_back_to_baseline(isolated_home, live_server) -> None:
    path = spec_loader._cache_path(live_server)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是合法的 spec", encoding="utf-8")

    spec, from_cache = spec_loader.load_active(live_server)
    assert not from_cache, "损坏缓存必须回退内置基线"
    assert "paths" in spec
    assert not path.exists(), "损坏缓存应被清除（下次偏斜检测重新拉取，自愈）"


def test_refresh_failure_is_silent(isolated_home) -> None:
    """服务器不可达时刷新静默跳过，不影响本次命令结果。"""
    http_mod.last_seen_spec_hash = "0000000000000000"
    http_mod.last_seen_server = "http://127.0.0.1:9"
    spec_loader.maybe_refresh(_Settings())  # 不抛异常即通过
    assert not spec_loader._cache_path("http://127.0.0.1:9").exists()


def test_cached_spec_json_is_valid(isolated_home, live_server, admin) -> None:
    _pair_and_store_token(live_server, admin)
    spec_loader.load_active(None)
    http_mod.last_seen_spec_hash = "0000000000000000"
    http_mod.last_seen_server = live_server
    spec_loader.maybe_refresh(_Settings())

    cached = json.loads(spec_loader._cache_path(live_server).read_text(encoding="utf-8"))
    assert cached["paths"], "缓存必须是完整可用的 OpenAPI spec"


def test_known_bad_hash_stops_refetch_loop(isolated_home, live_server, admin) -> None:
    """缓存 spec 建树失败后登记坏指纹：同一服务端版本不再每次调用都重拉。"""
    _pair_and_store_token(live_server, admin)
    spec_loader.load_active(None)
    http_mod.last_seen_spec_hash = "0000000000000000"
    http_mod.last_seen_server = live_server

    spec_loader.mark_spec_bad(live_server, "0000000000000000")
    spec_loader.maybe_refresh(_Settings())

    assert not spec_loader._cache_path(live_server).exists(), "坏指纹在册时不应重新拉取"
