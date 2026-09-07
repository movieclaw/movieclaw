from fastapi import FastAPI

from movieclaw_api import __version__
from movieclaw_api.api.router import api_router
from movieclaw_api.core.config import get_settings
from movieclaw_api.core.logging import configure_logging
from movieclaw_api.handlers import register_exception_handlers
from movieclaw_api.lifespan import build_lifespan
from movieclaw_api.middleware import register_middlewares


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_retention_days)

    # API 文档只在本地开发环境开放：生产部署（APP_ENV 非 local）关闭 /docs、
    # /redoc 与 openapi.json，避免向匿名访问者暴露完整接口面。
    docs_enabled = settings.app_env == "local"

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        openapi_url=f"{settings.api_v1_prefix}/openapi.json" if docs_enabled else None,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        lifespan=build_lifespan(settings),
    )

    register_exception_handlers(app)
    register_middlewares(app, settings)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    # Jellyfin 兼容播放接口（docs/design/jellyfin-compat.md）：根路径命名空间，
    # 不进业务 OpenAPI，自带 token 体系与错误形态
    from movieclaw_jellyfin.router import register as register_jellyfin

    register_jellyfin(app)

    # MCP 服务端点（docs/design/mcp-server.md）：同样是根路径命名空间，
    # 自带令牌体系与 JSON-RPC 错误形态，不进业务 OpenAPI（也就不会变成 CLI 命令）
    from movieclaw_mcp import register as register_mcp

    register_mcp(app)
    return app
