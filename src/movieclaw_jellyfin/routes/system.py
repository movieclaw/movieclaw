"""系统身份接口（设计文档 3.2）：/System/Info/Public、/System/Ping、/System/Endpoint。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from movieclaw_api.settings.schemas import get_jellyfin_compat
from movieclaw_jellyfin.identity import PRODUCT_NAME, reported_version
from movieclaw_jellyfin.security import require_device

router = APIRouter()


async def _local_address(request: Request) -> str:
    """published_server_url 优先；否则回显客户端实际访问的 scheme+Host。

    Next/reverse proxy 会把 Host 改成内部上游地址，因此优先读取标准转发头；
    多级代理产生逗号列表时取最外层客户端首先访问的值。
    """
    setting = await get_jellyfin_compat()
    if setting.published_server_url:
        return setting.published_server_url.rstrip("/")
    forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    host = forwarded_host or request.headers.get("Host") or request.url.netloc
    scheme = forwarded_proto or request.url.scheme or "http"
    return f"{scheme}://{host}"


@router.get("/System/Info/Public")
async def system_info_public(request: Request) -> JSONResponse:
    setting = await get_jellyfin_compat()
    return JSONResponse(
        {
            "LocalAddress": await _local_address(request),
            "ServerName": setting.server_name,
            "Version": reported_version(request),
            "ProductName": PRODUCT_NAME,
            "OperatingSystem": "",
            "Id": setting.server_id,
            "StartupWizardCompleted": True,
        }
    )


@router.get("/System/Ping")
@router.post("/System/Ping")
async def system_ping() -> JSONResponse:
    # 产品名固定 "Jellyfin Server"（带引号的 JSON 字符串），不是服务器名
    return JSONResponse(PRODUCT_NAME)


@router.get("/System/Endpoint", dependencies=[Depends(require_device)])
async def system_endpoint() -> JSONResponse:
    return JSONResponse({"IsLocal": True, "IsInNetwork": True})


@router.get("/System/Info", dependencies=[Depends(require_device)])
async def system_info(request: Request) -> JSONResponse:
    """完整 SystemInfo：复用 Public 字段 + 少量兼容字段（P2 兜底）。"""
    public = await system_info_public(request)
    import json

    body = json.loads(public.body)
    body.update(
        {
            "HasPendingRestart": False,
            "IsShuttingDown": False,
            "SupportsLibraryMonitor": False,
            "WebSocketPortNumber": request.url.port or 80,
            "CompletedInstallations": [],
        }
    )
    return JSONResponse(body)
