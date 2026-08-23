"""Jellyfin 兼容路由总装与应用挂载。

- 所有路由 `include_in_schema=False`：这是协议模仿层，不进业务 OpenAPI，
  也不受业务鉴权守护测试约束（它有自己的 token 体系与守护测试）；
- 同时注册 `/emby` 前缀别名（非 Jellyfin 行为，纯为个别客户端探测兜底）；
- JellyfinError 由本层的异常处理器渲染（四形态语义），不走业务统一处理器。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from movieclaw_jellyfin.errors import JellyfinError, render_error
from movieclaw_jellyfin.routes.common import require_enabled
from movieclaw_jellyfin.routes.images import router as images_router
from movieclaw_jellyfin.routes.library import router as library_router
from movieclaw_jellyfin.routes.misc import router as misc_router
from movieclaw_jellyfin.routes.persons import router as persons_router
from movieclaw_jellyfin.routes.playback import router as playback_router
from movieclaw_jellyfin.routes.playstate import router as playstate_router
from movieclaw_jellyfin.routes.search import router as search_router
from movieclaw_jellyfin.routes.system import router as system_router
from movieclaw_jellyfin.routes.users import router as users_router

# Jellyfin 命名空间的首段（大小写归一化中间件据此识别本层请求）
NAMESPACE_PREFIXES = {
    "system",
    "users",
    "userviews",
    "useritems",
    "userplayeditems",
    "userfavoriteitems",
    "items",
    "persons",
    "search",
    "videos",
    "shows",
    "sessions",
    "livestreams",
    "playingitems",
    "branding",
    "quickconnect",
    "plugins",
    "library",
    "displaypreferences",
    "emby",
}


# 本层接口会消费的 query 参数名（规范形态）。中间件把任意大小写的同名键
# 归一化到这里的写法——对齐 ASP.NET IQueryCollection 的 OrdinalIgnoreCase。
_KNOWN_QUERY_KEYS = [
    "parentId",
    "includeItemTypes",
    "excludeItemTypes",
    "recursive",
    "startIndex",
    "limit",
    "sortBy",
    "sortOrder",
    "fields",
    "filters",
    "searchTerm",
    "genres",
    "years",
    "officialRatings",
    "ids",
    "isPlayed",
    "isFavorite",
    "enableUserData",
    "enableImages",
    "imageTypeLimit",
    "enableImageTypes",
    "enableTotalRecordCount",
    "mediaTypes",
    "seasonId",
    "season",
    "isSpecialSeason",
    "seriesId",
    "personIds",
    "groupItems",
    "static",
    "container",
    "mediaSourceId",
    # 字幕接口的 route 段同名覆盖参数（jellyfin-subtitle.md §4.4；
    # mediaSourceId/format 已在列）——漏登记 PascalCase 客户端就取不到
    "itemId",
    "index",
    "startPositionTicks",
    "ApiKey",
    "api_key",
    "playSessionId",
    "tag",
    "imageIndex",
    "datePlayed",
    "positionTicks",
    "userId",
    "format",
    # DisplayPreferences 的客户端标识参数（issue #124）
    "client",
    # /Persons 与 /Search/Hints 的参数（PersonsController.cs / SearchController.cs）
    "nameStartsWith",
    "nameLessThan",
    "nameStartsWithOrGreater",
    "personTypes",
    "excludePersonTypes",
    "appearsInItemId",
    "includePeople",
    "includeMedia",
    "includeGenres",
    "includeStudios",
    "includeArtists",
    "isMovie",
    "isSeries",
    "isNews",
    "isKids",
    "isSports",
]

# 注册顺序即匹配顺序：字面路径的模块在参数路径之前
_SUB_ROUTERS = [
    system_router,
    users_router,
    misc_router,
    playstate_router,
    playback_router,
    images_router,
    search_router,
    persons_router,
    library_router,
]


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_enabled)])
    for sub in _SUB_ROUTERS:
        router.include_router(sub)
    return router


def register(app: FastAPI) -> None:
    """把兼容层挂到 FastAPI 应用（根路径 + /emby 别名）。"""
    router = build_router()
    app.include_router(router, include_in_schema=False)
    app.include_router(router, prefix="/emby", include_in_schema=False)

    @app.exception_handler(JellyfinError)
    async def jellyfin_error_handler(_request: Request, exc: JellyfinError) -> Response:
        return render_error(exc)

    # 大小写归一化（设计文档 9.1）：ASP.NET 的路由与 query 键都天生大小写
    # 不敏感，而 Starlette 两者都敏感。命中 Jellyfin 命名空间的请求：
    # ① 路径：按"本层路由模板字面段的小写 → 规范"映射逐段替换（参数段如
    #    GUID 不在映射里、原样保留）；带参数后缀的段（stream.{container}）
    #    按字面前缀归一化；
    # ② query：按"本层已知参数名的小写 → 规范"映射重写键（PascalCase
    #    客户端会发 ?ParentId=&Static=true，取不到就等于进不了库也播不了片）。
    # 只扫本层路由/参数，避免业务同名小写段（如 items）污染规范形态。
    literal_map: dict[str, str] = {"emby": "emby"}
    prefix_map: dict[str, str] = {}
    for sub in _SUB_ROUTERS:
        for route in sub.routes:
            template = getattr(route, "path", "")
            for segment in template.split("/"):
                if not segment:
                    continue
                if "{" not in segment:
                    literal_map.setdefault(segment.lower(), segment)
                elif not segment.startswith("{"):
                    prefix = segment.split("{", 1)[0]
                    prefix_map.setdefault(prefix.lower(), prefix)

    query_key_map = {k.lower(): k for k in _KNOWN_QUERY_KEYS}

    def _normalize_segment(segment: str) -> str:
        if not segment:
            return segment
        lowered = segment.lower()
        if lowered in literal_map:
            return literal_map[lowered]
        for pfx_lower, pfx in prefix_map.items():
            if lowered.startswith(pfx_lower):
                return pfx + segment[len(pfx) :]
        return segment

    def _normalize_query(raw: bytes) -> bytes:
        from urllib.parse import parse_qsl, urlencode

        try:
            pairs = parse_qsl(raw.decode("latin-1"), keep_blank_values=True)
        except ValueError:
            return raw
        rewritten = [(query_key_map.get(k.lower(), k), v) for k, v in pairs]
        return urlencode(rewritten).encode("latin-1")

    class JellyfinCaseNormalizer:
        """纯 ASGI 中间件（不要改回 BaseHTTPMiddleware：它会让取流的断连检测
        失效并给每个 body 块加一跳转发，见 movieclaw_api/middleware.py）。"""

        def __init__(self, inner: ASGIApp) -> None:
            self.inner = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.inner(scope, receive, send)
                return
            path = scope.get("path", "")
            parts = path.split("/")
            in_namespace = len(parts) > 1 and parts[1].lower() in NAMESPACE_PREFIXES
            if not in_namespace:
                await self.inner(scope, receive, send)
                return
            scope["path"] = "/".join(_normalize_segment(p) for p in parts)
            raw_query = scope.get("query_string", b"")
            if raw_query:
                scope["query_string"] = _normalize_query(raw_query)

            # 命名空间内未实现的路径/方法（/LiveStreams/Open、master.m3u8 等）会
            # 落到业务统一处理器，返回 RESOURCE_NOT_FOUND 业务信封——这是最容易
            # 暴露"不是真 Jellyfin"的地方。路由未匹配（scope 无 route）时改按
            # 协议形态应答：404/405 空 body，对齐 ASP.NET 终结点兜底
            swallow_body = False

            async def send_normalized(message: Message) -> None:
                nonlocal swallow_body
                if message["type"] == "http.response.start":
                    if message["status"] in (404, 405) and "route" not in scope:
                        swallow_body = True
                        await send(
                            {
                                "type": "http.response.start",
                                "status": message["status"],
                                "headers": [],
                            }
                        )
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                        return
                elif swallow_body and message["type"] == "http.response.body":
                    return
                await send(message)

            await self.inner(scope, receive, send_normalized)

    app.add_middleware(JellyfinCaseNormalizer)
