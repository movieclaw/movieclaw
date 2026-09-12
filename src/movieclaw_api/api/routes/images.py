"""通用图片代理接口（带本地磁盘缓存）与刮削图片资产直出。"""

from stat import S_ISREG

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_login
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.image_cache import get_image_cache
from movieclaw_api.services.image_variants import (
    ImageVariant,
    get_image_variant_service,
    source_version_of,
)
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/images", tags=["images"])


@router.get(
    "/proxy",
    response_class=FileResponse,
    summary="代理并缓存远程图片",
    operation_id="images.proxy",
    openapi_extra={"x-cli-hidden": True},
)
async def proxy_image(
    url: str = Query(min_length=1, max_length=2048),
    variant: ImageVariant | None = Query(default=None),
) -> FileResponse:
    """前端所有远程图片的统一入口：命中读本地缓存，未命中回源抓取后落盘。

    域名安全（SSRF 防护）、类型和体积校验在 ImageProxy 服务层完成。
    图床 URL 对应的内容事实上不可变，浏览器侧直接给一年 immutable 缓存。
    """
    cached = await get_image_cache().get_or_fetch(url)
    if variant is not None:
        cached = await get_image_variant_service().get_or_create(
            cached.path,
            source_key=f"remote:{url}",
            source_version=cached.version,
            variant=variant,
        )
    return FileResponse(
        cached.path,
        media_type=cached.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get(
    "/assets/{path:path}",
    response_class=FileResponse,
    summary="刮削图片资产直出（data/metadata/images 下的本地文件）",
    operation_id="images.asset",
    openapi_extra={"x-cli-hidden": True},
)
async def get_metadata_asset(
    path: str,
    variant: ImageVariant | None = Query(default=None),
    v: str | None = Query(default=None, max_length=32),
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """海报/剧照等刮削资产的服务通道（docs/design/metadata.md 6.1）。

    路径限定在资产根目录内（防目录穿越）。force 刷新会原地覆盖同名文件，
    故不给 immutable，一天后重新校验即可。

    资产按条目 id 分目录（``<media_item_id>/poster.jpg``），条目 id 是自增整数
    可猜——所以这里还要按库可见范围判一次：条目落在主体不可浏览的库里就 404，
    否则「看不见库」的成员靠猜 id 也能把库里的海报/抓帧图翻个遍
    （docs/design/library-access.md 2.5）。

    超管会话不做这层校验：超管对全部库都有管理权，「仅管理」只是把库从自己的
    浏览面（首页 / 海报墙 / Jellyfin）摘掉，不是对超管保密。活动页「全部」口径
    本来就给超管看范围外记录的片名，海报同级放行；否则那些行会请求到 404，
    海报位一直空着（实测踩过）。
    """
    from movieclaw_api.services.library.access import assert_item_visible
    from movieclaw_api.services.media_scrape import resolve_asset_path

    # 越权判定（含 resolve）结果按相对路径缓存，见 resolve_asset_path
    target = resolve_asset_path(path)
    if target is None:
        raise NotFoundException("图片资产不存在")
    # 一次 stat 走完「存在吗 + 是文件吗 + 版本戳 + 能不能永久缓存」四问：
    # 这四问原本各 stat 一次，而海报墙一屏就是上百个这样的请求
    try:
        stat = target.stat()
    except OSError:
        raise NotFoundException("图片资产不存在") from None
    if not S_ISREG(stat.st_mode):
        raise NotFoundException("图片资产不存在")
    head = path.split("/", 1)[0]
    if head.isdigit() and principal.kind != "admin":
        await assert_item_visible(session, principal, int(head))
    if variant is not None:
        cached = await get_image_variant_service().get_or_create(
            target,
            source_key=f"asset:{path}",
            source_version=source_version_of(stat),
            variant=variant,
        )
        # 业务 URL 携带的 v 与当前文件版本一致时才可永久缓存；手写的错误 v
        # 仍给一天缓存，避免同一 URL 在换图后长期停留旧派生图。
        immutable = v == str(int(stat.st_mtime))
        cache_control = (
            "public, max-age=31536000, immutable"
            if immutable
            else "public, max-age=86400"
        )
        return FileResponse(
            cached.path,
            media_type=cached.content_type,
            headers={"Cache-Control": cache_control},
        )
    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
