"""MCP 服务端的契约与守护测试（docs/design/mcp-server.md §9）。

目录名刻意叫 ``mcp_server`` 而不是 ``mcp``：``tests/`` 不是包，pytest 会把它塞进
sys.path，叫 ``mcp`` 的话会把官方 SDK 那个包整个盖住（表现为 import 时报
「No module named 'mcp.server'」，很难查）。

分四组：
- **协议冒烟**：新旧两代各一遍，确认「接进来确实能用」，并钉死两个我们自己选的
  形态——恒 JSON 应答（不开 SSE）、tools/list 带缓存提示；
- **鉴权与可见性**：无令牌/错令牌 401，端点停用/总开关关/地址不存在一律 404；
- **工具面**：选中的服务 ⇔ 暴露的工具严格一致，不该出现的操作一个都不许漏进来，
  工具名唯一且形态安全；
- **参数映射**：遍历 spec 全量操作，确认扁平参数能拼回正确的请求——这是直连模式
  最脆弱的一处，必须钉在 CI 上。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_mcp.catalog import (
    TOOL_NAME_PATTERN,
    Operation,
    operation_index,
    operations_by_domain,
    tools_by_name,
)
from movieclaw_mcp.dispatch import MAX_RESULT_BYTES, build_request, call_operation
from movieclaw_mcp.tools import build_tools, resolve_tool

_ADMIN = {"username": "admin", "password": "s3cret-pass"}
_MCP = "/api/v1/mcp"
_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _make_endpoint(client: TestClient, **overrides: Any) -> tuple[str, str]:
    """开总开关并建一个端点，返回 (slug, 令牌明文)。"""
    client.put(f"{_MCP}/status", json={"enabled": True})
    payload = {
        "name": "测试端点",
        "slug": "probe",
        "services": ["subscriptions", "jobs"],
        "expand_tools": True,
    } | overrides
    created = client.post(f"{_MCP}/endpoints", json=payload)
    assert created.status_code == 200, created.text
    body = created.json()["data"]
    return body["endpoint"]["slug"], body["token"]


def _rpc(
    client: TestClient,
    slug: str,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    **extra: Any,
):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    } | extra.pop("headers", {})
    return client.post(
        f"/mcp/{slug}",
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 1, "method": method,
            "params": {**(params or {}), "_meta": _META},
        },
    )


# ---------------------------------------------------------------------------
# 协议冒烟
# ---------------------------------------------------------------------------


def test_modern_protocol_round_trip(client: TestClient) -> None:
    """现行 2026-07-28：发现 → 列工具 → 调工具，一路走通。"""
    slug, token = _make_endpoint(client)

    discover = _rpc(client, slug, token, "server/discover")
    assert discover.status_code == 200
    assert "2026-07-28" in discover.json()["result"]["supportedVersions"]

    listed = _rpc(client, slug, token, "tools/list")
    assert listed.status_code == 200
    result = listed.json()["result"]
    names = [t["name"] for t in result["tools"]]
    assert "subscriptions_list" in names and "jobs_wait" in names
    # 顺序确定：客户端与提示词缓存才有命中的可能
    assert names == sorted(names)
    # 缓存提示：工具面只在管理员改配置时才变
    assert result["ttlMs"] > 0 and result["cacheScope"] == "private"

    called = _rpc(
        client, slug, token, "tools/call",
        {"name": "subscriptions_list", "arguments": {}},
        headers={"Mcp-Name": "subscriptions_list"},
    )
    assert called.status_code == 200
    payload = called.json()["result"]
    assert payload["isError"] is False
    assert payload["structuredContent"] == {"data": []}


def test_responses_are_always_plain_json(client: TestClient) -> None:
    """恒用单个 JSON 体应答，永不开 SSE 流（设计文档 §4.8）。

    这个形态是我们自己选的（json_response=True），不是协议强制——所以必须有守护，
    免得哪天配置漂了变成 SSE，反代那边才发现。
    """
    slug, token = _make_endpoint(client)
    for method, params in (
        ("tools/list", None),
        ("tools/call", {"name": "subscriptions_list", "arguments": {}}),
    ):
        response = _rpc(
            client, slug, token, method, params, headers={"Mcp-Name": "subscriptions_list"}
        )
        assert response.headers["content-type"].startswith("application/json")
        assert "text/event-stream" not in response.headers.get("content-type", "")


def test_legacy_initialize_still_answered(client: TestClient) -> None:
    """旧代客户端（2025-06-18 那批）的握手照样应答——SDK 负责，我们只守着不退化。"""
    slug, token = _make_endpoint(client)
    response = client.post(
        f"/mcp/{slug}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "legacy", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-06-18"


# ---------------------------------------------------------------------------
# 鉴权与可见性
# ---------------------------------------------------------------------------


def test_auth_and_visibility(client: TestClient) -> None:
    slug, token = _make_endpoint(client)

    assert client.post(f"/mcp/{slug}", json={}).status_code == 401
    bad = client.post(f"/mcp/{slug}", headers={"Authorization": "Bearer wrong"}, json={})
    assert bad.status_code == 401
    # 现行传输只用 POST：旧客户端的 GET 长连接与 DELETE 会话终止一律 405
    got = client.get(f"/mcp/{slug}", headers={"Authorization": f"Bearer {token}"})
    assert got.status_code == 405
    missing = client.post(
        "/mcp/does-not-exist", headers={"Authorization": f"Bearer {token}"}, json={}
    )
    assert missing.status_code == 404

    # 浏览器跨源请求（DNS 重绑定的形态）：带 Origin 且不认识 → 403
    forged = client.post(
        f"/mcp/{slug}",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example.com"},
        json={},
    )
    assert forged.status_code == 403


def test_disabled_endpoint_and_global_switch_return_404(client: TestClient) -> None:
    """停用与总开关关闭都表现为「这里什么都没有」，而不是 403——对外完全隐身。"""
    slug, token = _make_endpoint(client)
    endpoint_id = client.get(f"{_MCP}/status").json()["data"]["endpoints"][0]["id"]

    client.put(f"{_MCP}/endpoints/{endpoint_id}", json={"enabled": False})
    assert _rpc(client, slug, token, "tools/list").status_code == 404

    client.put(f"{_MCP}/endpoints/{endpoint_id}", json={"enabled": True})
    assert _rpc(client, slug, token, "tools/list").status_code == 200

    client.put(f"{_MCP}/status", json={"enabled": False})
    assert _rpc(client, slug, token, "tools/list").status_code == 404


def test_rotate_token_invalidates_the_old_one(client: TestClient) -> None:
    slug, token = _make_endpoint(client)
    endpoint_id = client.get(f"{_MCP}/status").json()["data"]["endpoints"][0]["id"]

    rotated = client.post(f"{_MCP}/endpoints/{endpoint_id}/token")
    fresh = rotated.json()["data"]["token"]
    assert fresh != token
    assert _rpc(client, slug, token, "tools/list").status_code == 401
    assert _rpc(client, slug, fresh, "tools/list").status_code == 200


def test_endpoint_views_never_leak_the_token(client: TestClient) -> None:
    _make_endpoint(client)
    body = client.get(f"{_MCP}/status").text
    assert "token_hash" not in body
    view = client.get(f"{_MCP}/status").json()["data"]["endpoints"][0]
    assert view["token_hint"].endswith("****")
    assert "token" not in view


def test_management_input_validation(client: TestClient) -> None:
    client.put(f"{_MCP}/status", json={"enabled": True})
    base = {"name": "x", "services": ["subscriptions"]}
    def _create(**overrides: Any) -> int:
        return client.post(f"{_MCP}/endpoints", json=base | overrides).status_code

    assert _create(slug="Bad Slug") == 400
    assert _create(slug="ok", services=["nope"]) == 400
    assert _create(slug="ok", services=[]) == 400
    assert _create(slug="taken") == 200
    # 地址标识唯一
    assert _create(slug="taken") == 409


def test_self_check_walks_the_whole_chain(client: TestClient) -> None:
    """自检要真的跑一遍协议 + 工具面 + 一次只读调用，而不是只看配置。"""
    _make_endpoint(client)
    endpoint_id = client.get(f"{_MCP}/status").json()["data"]["endpoints"][0]["id"]

    result = client.post(f"{_MCP}/endpoints/{endpoint_id}/check").json()["data"]
    assert result["ok"] is True
    assert result["protocol_version"] == "2026-07-28"
    assert result["tool_count"] > 0
    # 试调的必须是只读且不需要必填参数的工具——自检不该改任何状态
    assert result["probe_tool"] and result["probe_ok"] is True
    assert result["elapsed_ms"] >= 0


def test_self_check_reports_what_would_block_a_client(client: TestClient) -> None:
    """自检本身通过、但外部客户端仍连不上的情况要如实说出来。"""
    _make_endpoint(client)
    endpoint_id = client.get(f"{_MCP}/status").json()["data"]["endpoints"][0]["id"]

    # 没配外部访问地址：端点地址只有相对路径
    warnings = client.post(f"{_MCP}/endpoints/{endpoint_id}/check").json()["data"]["warnings"]
    assert any("外部访问地址" in w for w in warnings)

    client.put(f"{_MCP}/endpoints/{endpoint_id}", json={"enabled": False})
    client.put(f"{_MCP}/status", json={"enabled": False})
    result = client.post(f"{_MCP}/endpoints/{endpoint_id}/check").json()["data"]
    assert result["ok"] is True  # 配置本身没问题
    assert any("总开关" in w for w in result["warnings"])
    assert any("停用" in w for w in result["warnings"])


def test_collapsed_preview_lists_commands_as_data(client: TestClient) -> None:
    """折叠模式的试算要给出结构化命令清单，而不是把散文描述原样丢给页面。

    协议面给模型的 description 是「一行说明 + 几十行命令清单」；管理页照搬那段文本
    就是一堵散文墙。这里守住两件事：清单以 commands 数组给出，且 description 只剩
    那一行说明。
    """
    services = ["library"]
    expected = {op.command for op in operations_by_domain()["library"]}

    body = client.post(
        f"{_MCP}/endpoints/preview", json={"services": services, "expand_tools": False}
    ).json()["data"]
    tool = body["tools"][0]

    assert tool["name"] == "library"
    assert {c["name"] for c in tool["commands"]} == expected
    assert "\n" not in tool["description"]
    # 必填参数在命令行里带 * 后缀，页面据此不用再查一次 schema
    assert any(p.endswith("*") for c in tool["commands"] for p in c["params"])
    assert any(c["dangerous"] for c in tool["commands"])

    # 展开模式没有这一层：一命令一工具，commands 必须为空
    expanded = client.post(
        f"{_MCP}/endpoints/preview", json={"services": services, "expand_tools": True}
    ).json()["data"]
    assert all(t["commands"] == [] for t in expanded["tools"])


# ---------------------------------------------------------------------------
# 工具面
# ---------------------------------------------------------------------------


def test_tool_surface_matches_selected_services() -> None:
    """选中的服务 ⇔ 暴露的工具，两种模式都严格一致。"""
    services = ["subscriptions", "jobs"]
    by_domain = operations_by_domain()
    expected = {op.tool_name for domain in services for op in by_domain[domain]}

    expanded = build_tools(services, expand=True)
    assert {t.name for t in expanded} == expected

    collapsed = build_tools(services, expand=False)
    assert {t.name for t in collapsed} == set(services)
    # 折叠模式下每个命令都要在描述里列出来，否则模型无从知道 command 能填什么
    for tool in collapsed:
        commands = {op.command for op in by_domain[tool.name]}
        assert commands == set(tool.input_schema["properties"]["command"]["enum"])


def test_excluded_operations_never_reach_the_tool_surface() -> None:
    """三类操作一个都不许漏进来：上传类、会话递归类、MCP 自身的管理面。"""
    index = operation_index()
    assert "auth.avatar.upload" not in index          # 上传类：MCP 客户端没有服务端文件系统
    assert "appearance.backdrops.upload" not in index
    assert "session.start" not in index               # 防 Agent 递归拉起 Agent
    assert "session.retry" not in index
    assert not [op for op in index.values() if op.domain == "mcp"]  # 端点不能增删端点
    # 而正常的只读会话操作应当保留
    assert "session.list" in index


def test_tool_names_are_unique_and_client_safe() -> None:
    """`.`/`-` 归一成 `_` 之后可能撞名；撞了必须改名而不是静默覆盖。"""
    index = operation_index()
    assert len(tools_by_name()) == len(index)
    for op in index.values():
        assert TOOL_NAME_PATTERN.match(op.tool_name), f"{op.operation_id} 的工具名不合法"


def test_tool_annotations_follow_the_spec_metadata() -> None:
    tools = {t.name: t for t in build_tools(["subscriptions", "libraries", "library"], expand=True)}
    assert tools["subscriptions_list"].annotations.read_only_hint is True
    destructive = tools.get("library_items_delete")
    assert destructive is not None and destructive.annotations.destructive_hint is True
    assert "⚠" in destructive.description


# ---------------------------------------------------------------------------
# 参数映射（直连模式最脆弱的一处）
# ---------------------------------------------------------------------------


def _sample(schema: dict[str, Any]) -> Any:
    """按 schema 造一个能用的示例值（只覆盖 spec 里真实出现的类型）。"""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            if option.get("type") != "null":
                return _sample(option)
    kind = schema.get("type")
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.5
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return schema.get("enum", ["x"])[0] if schema.get("enum") else "x"


def test_every_operation_can_build_a_request() -> None:
    """遍历全量操作：必填参数给示例值，请求必须能拼出来且路径不留占位符。"""
    for op in operation_index().values():
        arguments = {name: _sample(op.arg_schemas[name]) for name in op.required}
        path, query, body = build_request(op, arguments)
        assert "{" not in path, f"{op.operation_id} 的路径参数没填满：{path}"
        assert path.startswith("/api/"), f"{op.operation_id} 的路径不对：{path}"
        for name in op.required:
            location = op.arg_locations[name]
            if location == "query":
                assert name in query
            elif location == "body":
                assert body is not None and (name in body or op.arg_locations[name] == "body")


def test_both_modes_build_the_same_request() -> None:
    """同一条命令、同一组参数，展开与折叠必须落到同一个请求上。"""
    services = ["subscriptions"]
    arguments = {"subscription_id": 42, "follow_future": False}

    op_expanded, args_expanded = resolve_tool(
        services, expand=True, name="subscriptions_update", arguments=arguments
    )
    op_collapsed, args_collapsed = resolve_tool(
        services, expand=False, name="subscriptions",
        arguments={"command": "update", "params": arguments},
    )
    assert op_expanded.operation_id == op_collapsed.operation_id
    assert build_request(op_expanded, args_expanded) == build_request(op_collapsed, args_collapsed)


def test_unknown_tool_and_command_are_reported_not_raised() -> None:
    with pytest.raises(ValueError, match="没有名为"):
        resolve_tool(["subscriptions"], expand=True, name="library_scan", arguments={})
    with pytest.raises(ValueError, match="没有名为"):
        resolve_tool(["subscriptions"], expand=False, name="subscriptions",
                     arguments={"command": "nope", "params": {}})


# ---------------------------------------------------------------------------
# 结果整形
# ---------------------------------------------------------------------------


def _stub_operation(path: str, method: str = "get") -> Operation:
    return Operation(
        operation_id="stub.op", tool_name="stub_op", domain="stub", command="op",
        method=method, path=path, summary="", description="",
        arg_locations={}, arg_schemas={}, required=(),
    )


@pytest.mark.asyncio
async def test_oversized_results_are_truncated_with_a_hint(monkeypatch) -> None:
    """大响应必须截断并说清「这不是全部数据」——否则模型会拿半份数据当结论。"""
    app = FastAPI()

    @app.get("/api/v1/stub")
    async def _stub() -> dict[str, Any]:
        return {"success": True, "data": [{"name": "x" * 200} for _ in range(500)]}

    monkeypatch.setattr(
        "movieclaw_mcp.dispatch.auth_service.issue_mcp_token",
        lambda _endpoint_id: _async_value("token"),
    )
    result = await call_operation(app, "e1", _stub_operation("/api/v1/stub"), {}, timeout=5)
    text = result.content[0].text
    assert len(text.encode("utf-8")) < MAX_RESULT_BYTES + 200
    assert "截断" in text and "limit" in text
    # 截断过的 JSON 已经不是合法数据，不能塞进 structuredContent 冒充完整结果
    assert result.structured_content is None
    assert result.is_error is False


@pytest.mark.asyncio
async def test_business_errors_come_back_as_readable_tool_errors(monkeypatch) -> None:
    """业务错误原样把中文 message 回给模型，让它自己纠正，而不是抛协议错误。"""
    app = FastAPI()

    @app.get("/api/v1/stub")
    async def _stub() -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=404,
            content={"success": False, "code": "NOT_FOUND", "message": "订阅不存在或已被删除"},
        )

    monkeypatch.setattr(
        "movieclaw_mcp.dispatch.auth_service.issue_mcp_token",
        lambda _endpoint_id: _async_value("token"),
    )
    result = await call_operation(app, "e1", _stub_operation("/api/v1/stub"), {}, timeout=5)
    assert result.is_error is True
    assert "订阅不存在" in result.content[0].text


async def _async_value(value: str) -> str:
    return value
