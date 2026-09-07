"""MCP 服务管理接口（「设置 → MCP 服务」页的后端，docs/design/mcp-server.md §6.1）。

这里是**管理面**：进业务 OpenAPI，挂在管理区（成员一律 403），因而 CLI 也自动
长出 `mclaw mcp …` 命令。协议面（`/mcp/<slug>`）在 movieclaw_mcp 包里，不进 spec。

令牌的签发与轮换额外挂 ``require_admin_session``——**人在浏览器里**才能签发凭证，
这是 docs/design/device-auth.md §8 已确立的红线：泄漏的令牌无法自我复制，
Agent 也不能给自己造一个 MCP 端点。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from movieclaw_api.api.deps import require_admin_session
from movieclaw_api.schemas.mcp import (
    EndpointCreatedView,
    EndpointCreateRequest,
    EndpointUpdateRequest,
    EndpointView,
    PreviewRequest,
    PreviewView,
    ServiceView,
    StatusView,
    ToggleRequest,
    ToolPreview,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import mcp_endpoints
from movieclaw_api.settings import AppServerSetting, get_setting_store
from movieclaw_api.settings.mcp import McpEndpoint
from movieclaw_mcp.catalog import available_services, operations_by_domain
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
                ToolPreview(
                    name=t.name,
                    description=t.description,
                    read_only=bool(t.annotations and t.annotations.read_only_hint),
                    destructive=bool(t.annotations and t.annotations.destructive_hint),
                )
                for t in tools
            ],
        )
    )
