"""影视发现接口：片单、稳定引用详情，以及统一搜索入口中的影视垂直。

路由保持薄：业务编排交给 TitleDiscoveryService，这里负责鉴权、库存投影
和把上游错误翻译成统一中文 API 异常。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_admin, require_login, require_subscribe_capability
from movieclaw_api.exceptions import (
    AppException,
    BadRequestException,
    NotFoundException,
    UpstreamServiceException,
    UpstreamUnreachableException,
)
from movieclaw_api.schemas.discover import (
    DiscoveredPersonDetailsView,
    DiscoveredTitleDetailsView,
    DiscoveryCollectionListView,
    DiscoveryCollectionTitlesView,
    DiscoveryFilterOptionsView,
    DiscoveryPageSectionView,
    DiscoveryPageView,
    DiscoveryPresentation,
    DiscoverySort,
    DiscoveryTitlePageView,
    TitleSearchPayload,
    TitleSearchView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.discover_library import DiscoverLibraryProjectionService
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.services.title_discovery import (
    TitleDiscoveryService,
    get_title_discovery_service,
)
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.search_history_repo import SearchHistoryRepository
from movieclaw_media import (
    DoubanError,
    DoubanNetworkError,
    DoubanNotFoundError,
    MediaKind,
    MediaSearchItem,
    MediaSource,
    TmdbError,
    TmdbNetworkError,
    TmdbNotFoundError,
)

logger = logging.getLogger("movieclaw_api.discover")

router = APIRouter(prefix="/discover", tags=["discover"])
ui_router = APIRouter(prefix="/ui/discovery", tags=["ui"])
search_router = APIRouter(prefix="/search", tags=["search"])


async def get_projection(
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> DiscoverLibraryProjectionService:
    """库存投影服务（按主体可见库过滤——成员的"已入库"徽标与深链
    只反映他看得见的库，避免点进去 404）。"""
    return DiscoverLibraryProjectionService(
        session, visible_library_ids=await visible_library_ids(session, principal)
    )


def _translate(exc: TmdbError | DoubanError) -> AppException:
    """上游影视数据错误 → API 统一异常（message 已是面向用户的中文）。

    网络级不可达（含熔断快速失败）给结构化的 UPSTREAM_UNREACHABLE：
    前端据此渲染「原因说明 + 跳转网络设置」的引导错误态。
    """
    if isinstance(exc, (TmdbNotFoundError, DoubanNotFoundError)):
        return NotFoundException(str(exc))
    if isinstance(exc, TmdbNetworkError):
        return UpstreamUnreachableException(
            str(exc),
            service="tmdb",
            hint="到「设置 → 网络」为 TMDB 配置代理或镜像地址，保存后用连通性测试验证",
        )
    if isinstance(exc, DoubanNetworkError):
        return UpstreamUnreachableException(
            str(exc),
            service="douban",
            hint="到「设置 → 网络」为豆瓣配置代理或反代地址，保存后用连通性测试验证",
        )
    return UpstreamServiceException(str(exc))


# ---------------------------------------------------------------------------
# 影视发现领域 API（OpenAPI 与 mclaw 共用同一组语义化 operation_id）
# ---------------------------------------------------------------------------


@router.get(
    "/collections",
    response_model=ApiResponse[DiscoveryCollectionListView],
    summary="列出指定来源中可浏览的电影或剧集片单",
    operation_id="discover.list-collections",
)
async def list_discovery_collections(
    media_type: MediaKind = Query(description="片单媒体类型：movie / tv"),
    provider: MediaSource = Query(description="片单数据来源：tmdb / douban"),
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
) -> ApiResponse[DiscoveryCollectionListView]:
    """返回热门、热映、在播、高分、地区和类型等可浏览片单。

    响应中的 ``collection_ref`` 是稳定引用；浏览某个片单时应原样传给
    ``browse-collection``，不要自行拆分或拼接来源、类型与片单 ID。
    """
    try:
        collections = service.list_collections(media_type, provider)
    except (TmdbError, DoubanError) as exc:
        raise _translate(exc) from exc
    return ok(
        DiscoveryCollectionListView(
            provider=provider,
            media_type=media_type,
            collections=collections,
        )
    )


@router.get(
    "/collections/{collection_ref}/titles",
    response_model=ApiResponse[DiscoveryCollectionTitlesView],
    summary="浏览指定片单中的电影或剧集",
    operation_id="discover.browse-collection",
)
async def browse_discovery_collection(
    collection_ref: str,
    limit: int = Query(20, ge=1, le=500, description="最多返回多少条；完整榜单可调大"),
    page: int = Query(1, ge=1, le=500, description="TMDB 原生页码；豆瓣片单恒为第 1 页"),
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
    projection: DiscoverLibraryProjectionService = Depends(get_projection),
) -> ApiResponse[DiscoveryCollectionTitlesView]:
    """读取 ``list-collections`` 返回的一个片单，并附带当前主体可见的库存摘要。"""
    try:
        result = await service.browse_collection(
            collection_ref,
            limit=limit,
            page=page,
            projection=projection,
        )
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    except (TmdbError, DoubanError) as exc:
        raise _translate(exc) from exc
    return ok(result)


@router.get(
    "/filters",
    response_model=ApiResponse[DiscoveryFilterOptionsView],
    summary="获取组合发现可用的类型选项",
    operation_id="discover.filter-options",
)
async def get_discovery_filter_options(
    media_type: MediaKind = Query(description="筛选的媒体类型：movie / tv"),
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
) -> ApiResponse[DiscoveryFilterOptionsView]:
    try:
        return ok(await service.filter_options(media_type))
    except TmdbError as exc:
        raise _translate(exc) from exc


@router.get(
    "/titles",
    response_model=ApiResponse[DiscoveryTitlePageView],
    summary="按类型、地区、年份、评分、片长和排序组合发现影视",
    operation_id="discover.filter-titles",
)
async def filter_discovery_titles(
    media_type: MediaKind = Query(description="筛选的媒体类型：movie / tv"),
    genre_ids: list[int] = Query(default=[], description="类型 ID；多个值按任一匹配"),
    origin_country: str | None = Query(
        default=None,
        min_length=2,
        max_length=2,
        pattern="^[A-Za-z]{2}$",
        description="制片国家/地区代码",
    ),
    year: int | None = Query(default=None, ge=1874, le=2100),
    rating_gte: float | None = Query(default=None, ge=0, le=10),
    runtime_lte: int | None = Query(default=None, ge=1, le=600),
    sort: DiscoverySort = Query(default=DiscoverySort.POPULAR),
    page: int = Query(default=1, ge=1, le=500),
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
    projection: DiscoverLibraryProjectionService = Depends(get_projection),
) -> ApiResponse[DiscoveryTitlePageView]:
    try:
        result = await service.discover_titles(
            media_type,
            genre_ids=genre_ids,
            origin_country=origin_country.upper() if origin_country else None,
            year=year,
            rating_gte=rating_gte,
            runtime_lte=runtime_lte,
            sort=sort,
            page=page,
            projection=projection,
        )
    except TmdbError as exc:
        raise _translate(exc) from exc
    return ok(result)


@search_router.post(
    "/titles",
    response_model=ApiResponse[TitleSearchView],
    summary="按片名搜索 TMDB、豆瓣或全部影视来源",
    operation_id="search.titles",
)
async def search_titles(
    payload: TitleSearchPayload,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
) -> ApiResponse[TitleSearchView]:
    """一次调用完成单来源或双来源搜索；双来源单边失败时仍返回另一边结果。"""
    try:
        outcome = await service.search_titles(payload.query, payload.provider)
    except (TmdbError, DoubanError) as exc:
        raise _translate(exc) from exc
    history_id = None
    if payload.save_history:
        member_id = principal.member_id if principal.member_id is not None else 0
        history_id = await _record_media_history(
            session,
            payload.query,
            outcome.raw_items,
            member_id=member_id,
        )
    return ok(
        TitleSearchView(
            query=payload.query,
            titles=outcome.titles,
            providers=outcome.providers,
            history_id=history_id,
        )
    )


@router.get(
    "/titles/{title_ref}",
    response_model=ApiResponse[DiscoveredTitleDetailsView],
    summary="获取影视条目的完整资料、演职员、图片和相关推荐",
    operation_id="discover.get-title-details",
)
async def get_discovered_title_details(
    title_ref: str,
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
    projection: DiscoverLibraryProjectionService = Depends(get_projection),
) -> ApiResponse[DiscoveredTitleDetailsView]:
    """读取搜索或片单结果返回的 ``title_ref``；调用方不需要另传来源或媒体类型。"""
    try:
        result = await service.get_title_details(title_ref, projection=projection)
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    except (TmdbError, DoubanError) as exc:
        raise _translate(exc) from exc
    return ok(result)


@router.get(
    "/people/{tmdb_person_id}",
    response_model=ApiResponse[DiscoveredPersonDetailsView],
    summary="获取 TMDB 影人的完整影视履历及本地状态",
    operation_id="discover.get-person-details",
    openapi_extra={"x-cli-hidden": True},
)
async def get_discovered_person_details(
    tmdb_person_id: int,
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
    projection: DiscoverLibraryProjectionService = Depends(get_projection),
) -> ApiResponse[DiscoveredPersonDetailsView]:
    """返回参演与幕后作品的去重全集；库存状态按当前主体可见媒体库投影。"""
    try:
        result = await service.get_person_details(tmdb_person_id, projection=projection)
    except TmdbError as exc:
        raise _translate(exc) from exc
    return ok(result)


@ui_router.get(
    "/{media_type}",
    response_model=ApiResponse[DiscoveryPageView],
    summary="发现页展示编排（分区引用与呈现方式）",
    operation_id="ui.discovery.get",
    openapi_extra={"x-cli-hidden": True},
)
async def get_discovery_page(
    media_type: MediaKind,
    provider: MediaSource = Query(default=MediaSource.TMDB, description="页面数据来源"),
    service: TitleDiscoveryService = Depends(get_title_discovery_service),
) -> ApiResponse[DiscoveryPageView]:
    """Web 专用轻量协议：只决定 Hero/排名行/普通行，不携带任何片单条目。"""
    try:
        collections = service.list_collections(media_type, provider)
    except (TmdbError, DoubanError) as exc:
        raise _translate(exc) from exc
    sections = []
    for collection in collections:
        if collection.collection_ref.endswith(":featured-weekly"):
            presentation = DiscoveryPresentation.HERO
        elif collection.is_ranked:
            presentation = DiscoveryPresentation.RANKED_ROW
        else:
            presentation = DiscoveryPresentation.POSTER_ROW
        sections.append(
            DiscoveryPageSectionView(
                collection_ref=collection.collection_ref,
                title=collection.name,
                presentation=presentation,
                preview_limit=collection.default_limit,
                supports_full_listing=collection.supports_full_listing,
            )
        )
    return ok(
        DiscoveryPageView(
            provider=provider,
            media_type=media_type,
            sections=sections,
        )
    )


async def _record_media_history(
    session: AsyncSession,
    keyword: str,
    results: list[MediaSearchItem],
    member_id: int = 0,
) -> int | None:
    """媒体搜索落历史 + 回写结果快照。辅助功能：任何失败只记日志。

    与种子搜索不同，本端点在请求内就拿到了完整结果，历史与快照可在同一个
    请求级 session 里一次写完，无需独立会话。
    """
    try:
        repo = SearchHistoryRepository(session)
        history_id = await repo.record(keyword, vertical="media", member_id=member_id)
        if history_id is None:
            return None
        payload = json.dumps(
            {
                "total": len(results),
                "items": [item.model_dump(mode="json") for item in results],
            },
            ensure_ascii=False,
        )
        await repo.save_snapshot(history_id, payload)
        return history_id
    except Exception:  # noqa: BLE001 —— 历史写入失败不能拖垮搜索本身
        logger.warning("媒体搜索历史写入失败（不影响本次搜索结果）", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 院线地区（发现页页脚就地设置，docs/design/scrape-customization.md §2.1）
# ---------------------------------------------------------------------------


class DiscoverRegionView(BaseModel):
    """当前生效的院线地区（含名称，页脚直接展示）。"""

    region: str
    can_edit: bool = Field(description="当前用户是否可修改（管理员）")


class DiscoverRegionPayload(BaseModel):
    region: str = Field(min_length=2, max_length=2, description="ISO 3166-1 地区码")


@router.get(
    "/region",
    response_model=ApiResponse[DiscoverRegionView],
    summary="读取「正在热映/即将上映」的院线地区",
    operation_id="discover.region.show",
)
async def get_discover_region(
    principal: Principal = Depends(require_login),
) -> ApiResponse[DiscoverRegionView]:
    from movieclaw_api.services.scrape_config import effective_region

    return ok(DiscoverRegionView(region=effective_region(), can_edit=principal.is_admin))


@router.put(
    "/region",
    response_model=ApiResponse[DiscoverRegionView],
    summary="切换院线地区（选择即保存，立即生效）",
    operation_id="discover.region.set",
    dependencies=[Depends(require_admin)],
)
async def set_discover_region(payload: DiscoverRegionPayload) -> ApiResponse[DiscoverRegionView]:
    from movieclaw_api.services.media_discover import reset_media_service
    from movieclaw_api.services.scrape_config import effective_region, save_discover_prefs
    from movieclaw_api.settings import DiscoverPreferencesSetting

    await save_discover_prefs(DiscoverPreferencesSetting(region=payload.region))
    # 地区绑死在发现页服务构造期：重建单例，下次请求即按新地区拉榜单
    reset_media_service()
    return ok(DiscoverRegionView(region=effective_region(), can_edit=True))
