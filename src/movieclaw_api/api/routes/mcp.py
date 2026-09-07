"""MCP 服务管理接口（「设置 → MCP 服务」页的后端，docs/design/mcp-server.md §6.1）。

这里是**管理面**：进业务 OpenAPI，挂在管理区（成员一律 403），因而 CLI 也自动
长出 `mclaw mcp …` 命令。协议面（`/mcp/<slug>`）在 movieclaw_mcp 包里，不进 spec。

令牌的签发与轮换额外挂 ``require_admin_session``——**人在浏览器里**才能签发凭证，
这是 docs/design/device-auth.md §8 已确立的红线：泄漏的令牌无法自我复制，
Agent 也不能给自己造一个 MCP 端点。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from mcp.types import Tool

from movieclaw_api.api.deps import require_admin_session
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.schemas.mcp import (
    EndpointCreatedView,
    EndpointCreateRequest,
    EndpointUpdateRequest,
    EndpointView,
    PreviewRequest,
    PreviewView,
    SelfCheckView,
    ServiceView,
    StatusView,
    ToggleRequest,
    ToolCommand,
    ToolParameter,
    ToolPreview,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import mcp_endpoints
from movieclaw_api.settings import AppServerSetting, get_setting_store
from movieclaw_api.settings.mcp import McpEndpoint
from movieclaw_mcp.catalog import available_services, operations_by_domain, tools_by_name
from movieclaw_mcp.selfcheck import self_check
from movieclaw_mcp.tools import build_tools, describe_domain

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _tool_bytes(services: list[str], *, expand: bool) -> int:
    """工具定义的大致体积：管理页拿它告诉用户「这套工具面要占多少上下文」。"""
    tools = build_tools(services, expand=expand)
    return sum(
        len(json.dumps(
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema},
            ensure_ascii=False,
        ).encode("utf-8"))
        for t in tools
    )


async def _base_url() -> tuple[str, bool]:
    """端点地址的前缀。未配置外部访问地址时回落到相对路径，并告诉前端去配。"""
    external = (await get_setting_store().get(AppServerSetting)).external_url.strip()
    return (external.rstrip("/"), True) if external else ("", False)


def _to_view(endpoint: McpEndpoint, base_url: str) -> EndpointView:
    known = set(available_services())
    live = [s for s in endpoint.services if s in known]
    return EndpointView(
        id=endpoint.id,
        slug=endpoint.slug,
        name=endpoint.name,
        description=endpoint.description,
        services=live,
        missing_services=[s for s in endpoint.services if s not in known],
        expand_tools=endpoint.expand_tools,
        enabled=endpoint.enabled,
        token_hint=endpoint.token_hint,
        timeout_seconds=endpoint.timeout_seconds,
        tool_count=len(build_tools(live, expand=endpoint.expand_tools)),
        url=f"{base_url}/mcp/{endpoint.slug}",
        created_at=endpoint.created_at,
        last_used_at=endpoint.last_used_at,
    )


@router.get(
    "/status",
    response_model=ApiResponse[StatusView],
    summary="MCP 总开关、端点清单与可选服务目录",
    operation_id="mcp.status",
)
async def get_status() -> ApiResponse[StatusView]:
    config = await mcp_endpoints.get_config()
    base_url, configured = await _base_url()
    by_domain = operations_by_domain()
    services = [
        ServiceView(
            domain=domain,
            description=describe_domain(domain),
            command_count=len(ops),
            expanded_bytes=_tool_bytes([domain], expand=True),
            collapsed_bytes=_tool_bytes([domain], expand=False),
        )
        for domain, ops in by_domain.items()
    ]
    return ok(
        StatusView(
            enabled=config.enabled,
            base_url=base_url,
            external_url_configured=configured,
            endpoints=[_to_view(e, base_url) for e in config.endpoints],
            services=services,
        )
    )


@router.put(
    "/status",
    response_model=ApiResponse[StatusView],
    summary="开启或关闭 MCP 服务（关闭后所有端点一律 404）",
    operation_id="mcp.toggle",
)
async def toggle(payload: ToggleRequest) -> ApiResponse[StatusView]:
    await mcp_endpoints.set_global_enabled(payload.enabled)
    return await get_status()


@router.post(
    "/endpoints",
    response_model=ApiResponse[EndpointCreatedView],
    summary="新建 MCP 端点（令牌明文仅本次返回）",
    operation_id="mcp.endpoints.create",
    dependencies=[Depends(require_admin_session)],
)
async def create_endpoint(payload: EndpointCreateRequest) -> ApiResponse[EndpointCreatedView]:
    endpoint, token = await mcp_endpoints.create_endpoint(
        name=payload.name,
        slug=payload.slug,
        services=payload.services,
        description=payload.description,
        expand_tools=payload.expand_tools,
        timeout_seconds=payload.timeout_seconds,
    )
    base_url, _ = await _base_url()
    return ok(EndpointCreatedView(endpoint=_to_view(endpoint, base_url), token=token))


@router.put(
    "/endpoints/{endpoint_id}",
    response_model=ApiResponse[EndpointView],
    summary="修改端点的名称、服务、工具模式或启停状态",
    operation_id="mcp.endpoints.update",
)
async def update_endpoint(
    endpoint_id: str, payload: EndpointUpdateRequest
) -> ApiResponse[EndpointView]:
    endpoint = await mcp_endpoints.update_endpoint(
        endpoint_id,
        name=payload.name,
        description=payload.description,
        services=payload.services,
        expand_tools=payload.expand_tools,
        enabled=payload.enabled,
        timeout_seconds=payload.timeout_seconds,
    )
    base_url, _ = await _base_url()
    return ok(_to_view(endpoint, base_url))


@router.post(
    "/endpoints/{endpoint_id}/token",
    response_model=ApiResponse[EndpointCreatedView],
    summary="轮换端点令牌（旧令牌即刻失效，新明文仅本次返回）",
    operation_id="mcp.endpoints.rotate-token",
    dependencies=[Depends(require_admin_session)],
)
async def rotate_token(endpoint_id: str) -> ApiResponse[EndpointCreatedView]:
    endpoint, token = await mcp_endpoints.rotate_token(endpoint_id)
    base_url, _ = await _base_url()
    return ok(EndpointCreatedView(endpoint=_to_view(endpoint, base_url), token=token))


@router.post(
    "/endpoints/{endpoint_id}/check",
    response_model=ApiResponse[SelfCheckView],
    summary="自检端点：协议握手、工具面与一次只读调用",
    operation_id="mcp.endpoints.check",
)
async def check_endpoint(request: Request, endpoint_id: str) -> ApiResponse[SelfCheckView]:
    config = await mcp_endpoints.get_config()
    endpoint = next((e for e in config.endpoints if e.id == endpoint_id), None)
    if endpoint is None:
        raise NotFoundException("端点不存在或已被删除")
    base_url, _ = await _base_url()
    result = await self_check(request.app, endpoint, external_url=base_url)
    if not config.enabled:
        result.warnings.insert(0, "MCP 总开关是关闭的，所有端点对外一律 404。")
    return ok(SelfCheckView(**vars(result)))


@router.delete(
    "/endpoints/{endpoint_id}",
    response_model=ApiResponse[dict],
    summary="删除端点（地址与令牌一并作废）",
    operation_id="mcp.endpoints.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_endpoint(endpoint_id: str) -> ApiResponse[dict]:
    await mcp_endpoints.delete_endpoint(endpoint_id)
    return ok({"deleted": True})


def _schema_type(schema: dict) -> str:
    """把 JSON Schema 压成一个人能一眼读懂的类型标签（如 ``integer[]``、``string``）。

    Optional 字段在 spec 里是 ``anyOf: [T, null]``，取非 null 的那支即可。
    """
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"]
        return _schema_type(options[0]) if options else "any"
    kind = schema.get("type") or ("enum" if schema.get("enum") else "any")
    if kind == "array":
        return f"{_schema_type(schema.get('items') or {})}[]"
    return str(kind)


def _tool_commands(tool: Tool) -> list[ToolCommand]:
    """折叠模式下这个服务工具覆盖的命令清单。

    折叠工具的名字就是服务域名，且不会出现在 ``tools_by_name()`` 里（那是展开模式的
    索引），据此区分两种形态。展开模式返回空列表。
    """
    ops = operations_by_domain().get(tool.name)
    if not ops or tool.name in tools_by_name():
        return []
    return [
        ToolCommand(
            name=op.command,
            summary=op.summary or op.operation_id,
            params=[
                f"{name}*" if name in op.required else name for name in op.arg_locations
            ],
            dangerous=op.dangerous or "",
            is_job=op.is_job,
        )
        for op in ops
    ]


def _tool_preview(tool: Tool) -> ToolPreview:
    """SDK 的 Tool → 管理页要展示的形态（含参数明细）。"""
    schema = tool.input_schema or {}
    required = set(schema.get("required") or [])
    operation = tools_by_name().get(tool.name)
    parameters = [
        ToolParameter(
            name=name,
            type=_schema_type(prop or {}),
            required=name in required,
            description=(prop or {}).get("description", ""),
            location=(operation.arg_locations.get(name, "") if operation else ""),
            options=[str(v) for v in ((prop or {}).get("enum") or [])][:40],
        )
        for name, prop in (schema.get("properties") or {}).items()
    ]
    description = tool.description or ""
    commands = _tool_commands(tool)
    # 折叠工具的 description 是「一行说明 + 整份命令清单」，那份清单已经结构化成
    # commands 了；详情页再重复一遍就是几百行散文。这里只留说明本身。
    if commands:
        description = description.split("\n", 1)[0]
    return ToolPreview(
        name=tool.name,
        summary=description.split("。")[0][:60],
        description=description,
        service=operation.domain if operation else tool.name,
        read_only=bool(tool.annotations and tool.annotations.read_only_hint),
        destructive=bool(tool.annotations and tool.annotations.destructive_hint),
        parameters=parameters,
        commands=commands,
    )


@router.post(
    "/endpoints/preview",
    response_model=ApiResponse[PreviewView],
    summary="试算：给定服务集合会暴露哪些工具、占多少上下文",
    operation_id="mcp.endpoints.preview",
)
async def preview(payload: PreviewRequest) -> ApiResponse[PreviewView]:
    tools = build_tools(payload.services, expand=payload.expand_tools)
    by_domain = operations_by_domain()
    known = set(by_domain)
    commands = sum(len(by_domain[d]) for d in payload.services if d in known)
    return ok(
        PreviewView(
            tool_count=len(tools),
            command_count=commands,
            approx_bytes=_tool_bytes(payload.services, expand=payload.expand_tools),
            tools=[
                _tool_preview(t)
                for t in tools
            ],
        )
    )
