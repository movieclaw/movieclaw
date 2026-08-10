from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.subscription import (
    ActivityView,
    DispatchPreviewView,
    DownloadUnitView,
    GrabPayload,
    GrabResultView,
    MediaBrief,
    PipelineHealthView,
    PreparePayload,
    PrepareView,
    ResolveCandidateView,
    SearchNowView,
    SeasonOverview,
    SubscriptionCreatePayload,
    SubscriptionDetailView,
    SubscriptionDownloadView,
    SubscriptionPausePayload,
    SubscriptionUpdatePayload,
    SubscriptionView,
)
from movieclaw_api.services.media_discover import get_tmdb_client
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_db.engine import get_session
from movieclaw_media.library import ResolveStatus
from movieclaw_media.models import MediaKind

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def _service(session: AsyncSession) -> SubscriptionService:
    library = MediaLibraryService(session, get_tmdb_client())
    return SubscriptionService(session, library)


@router.post(
    "/prepare",
    response_model=ApiResponse[PrepareView],
    summary="订阅预检：建档条目、返回季集结构与库存；歧义时返回候选清单",
    operation_id="sub.prepare",
)
async def prepare_subscription(
    payload: PreparePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PrepareView]:
    """幂等预检。TMDB 入口直接建档；豆瓣入口先收敛（命中→ready，
    歧义→candidates 让用户确认后以 tmdb_id 重新 prepare，未收录→not_found）。"""
    service = _service(session)

    if payload.source == "douban":
        if not payload.title:
            raise BadRequestException("豆瓣入口预检必须携带标题")
        library = MediaLibraryService(session, get_tmdb_client())
        resolution, item = await library.resolve_douban(
            payload.kind, payload.title, year=payload.year, douban_id=payload.douban_id
        )
        if resolution.status is ResolveStatus.NOT_FOUND:
            return ok(
                PrepareView(status="not_found"),
                message="TMDB 未收录该条目，暂无法订阅",
            )
        if resolution.status is ResolveStatus.AMBIGUOUS:
            return ok(
                PrepareView(
                    status="ambiguous",
                    candidates=[ResolveCandidateView.from_model(c) for c in resolution.candidates],
                ),
                message="找到多个可能的条目，请确认是哪一部",
            )
        assert item is not None
        tmdb_id = item.tmdb_id
    else:
        if payload.tmdb_id is None:
            raise BadRequestException("TMDB 入口预检必须携带 tmdb_id")
        tmdb_id = payload.tmdb_id

    item, seasons, existing = await service.prepare(
        payload.kind, tmdb_id, douban_id=payload.douban_id
    )
    # 库存概览（媒体库 L3 联通）：季选择器每行显示"库里已有 x 集"。
    # 已播/在位口径走仓储层唯一实现（与海报墙缺集统计、工单生成同源）
    from movieclaw_db.repositories import MediaItemRepository
    from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

    assert item.id is not None
    owned = await LibraryFileRepository(session).owned_units(item.id)
    aired_units = await MediaItemRepository(session).aired_units_many(
        [item.id], include_specials=True
    )
    aired_by_season: dict[int, int] = {}
    for season_number, _episode in aired_units.get(item.id, set()):
        aired_by_season[season_number] = aired_by_season.get(season_number, 0) + 1
    return ok(
        PrepareView(
            status="ready",
            media=MediaBrief.from_model(item),
            seasons=[
                SeasonOverview.from_row(
                    s, aired_count=aired_by_season.get(s.season_number, 0), owned_units=owned
                )
                for s in seasons
            ],
            existing_subscription_id=existing.id if existing else None,
            movie_owned=payload.kind == MediaKind.MOVIE and (0, 0) in owned,
        )
    )


@router.post(
    "",
    response_model=ApiResponse[SubscriptionDetailView],
    summary="创建订阅（生成初始工单；同条目重复订阅幂等返回已有）",
    operation_id="sub.create",
)
async def create_subscription(
    payload: SubscriptionCreatePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    """创建订阅。"立即踢一次缺口搜索"由 service 层统一触发，路由不用管。"""
    service = _service(session)
    subscription = await service.create(
        payload.kind,
        payload.tmdb_id,
        selected_seasons=payload.selected_seasons,
        follow_future=payload.follow_future,
        rule_set_id=payload.rule_set_id,
        library_id=payload.library_id,
        douban_id=payload.douban_id,
        quality_policy=(
            payload.quality_policy.model_dump() if payload.quality_policy is not None else None
        ),
    )
    assert subscription.id is not None
    sub, item, wanted = await service.detail(subscription.id)
    return ok(
        SubscriptionDetailView.from_detail(sub, item, wanted),
        message="已加入订阅，正在搜索资源",
    )


@router.get(
    "/dispatch-preview",
    response_model=ApiResponse[DispatchPreviewView],
    summary="投递路由预检：按类型与目标库预演下载会落到哪、能否自动入库",
    operation_id="sub.dispatch-preview",
)
async def dispatch_preview(
    kind: str = Query(description="movie / tv"),
    library_id: int | None = Query(default=None, description="目标库；缺省走收藏范围路由"),
    tmdb_id: int | None = Query(
        default=None, description="TMDB 条目 ID；缺省库时据此按收藏范围路由选库"
    ),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DispatchPreviewView]:
    """创建订阅前调用：与真实投递同源的三级兜底 + 映射守门判定，
    配置有问题（映射不覆盖/无下载器/无库根）在订阅那一刻就亮出来；
    未手选库时返回收藏范围路由结论（预选库 + 中文理由徽标）。"""
    from movieclaw_api.services.subscription import preview_dispatch_route

    preview = await preview_dispatch_route(
        session, kind=kind, library_id=library_id, tmdb_id=tmdb_id
    )
    return ok(DispatchPreviewView(**preview))


@router.get(
    "/pipeline-health",
    response_model=ApiResponse[PipelineHealthView],
    summary="订阅链路体检：逐库预演「投递 → 转移 → 入库」，联合约束一次亮清",
    operation_id="sub.health",
)
async def pipeline_health_check(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PipelineHealthView]:
    """订阅设定页与订阅列表警示横幅的数据源。判定与真实投递/搬运同一批
    原语（兜底顺序、映射覆盖、同盘检测），不存在体检与执行的口径漂移。"""
    from movieclaw_api.services.subscription import pipeline_health

    return ok(PipelineHealthView(**await pipeline_health(session)))


@router.get(
    "",
    response_model=ApiResponse[list[SubscriptionView]],
    summary="订阅列表（含工单进度）",
    operation_id="sub.list",
)
async def list_subscriptions(
    kind: str | None = Query(default=None, description="movie / tv，缺省全部"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[SubscriptionView]]:
    service = _service(session)
    rows = await service.list_with_progress(kind=kind)
    return ok([SubscriptionView.from_model(s, m, c) for s, m, c in rows])


@router.get(
    "/{subscription_id}",
    response_model=ApiResponse[SubscriptionDetailView],
    summary="订阅详情（含工单明细）",
    operation_id="sub.show",
)
async def get_subscription(
    subscription_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    service = _service(session)
    sub, item, wanted = await service.detail(subscription_id)
    return ok(SubscriptionDetailView.from_detail(sub, item, wanted))


@router.get(
    "/{subscription_id}/downloads",
    response_model=ApiResponse[list[SubscriptionDownloadView]],
    summary="订阅在途种子的实时下载进度（速度/ETA，详情页轮询用）",
    operation_id="sub.downloads",
)
async def list_subscription_downloads(
    subscription_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[SubscriptionDownloadView]]:
    """纯读快照：逐个到可用下载器查询在途种子的当前状态，不落库不改工单。
    只在详情页打开且存在在途工单时被轮询（约 5 秒一次），单订阅种子数很少，
    对下载器的压力可忽略。"""
    from movieclaw_api.services.download_progress import subscription_download_snapshot
    from movieclaw_db.repositories import SubscriptionRepository

    # 订阅不存在时给 404 而不是空列表——只做存在性检查即可，
    # 没必要为此把整份工单明细（service.detail）拉出来
    if await SubscriptionRepository(session).get(subscription_id) is None:
        raise NotFoundException(f"订阅不存在：#{subscription_id}")
    rows = await subscription_download_snapshot(session, subscription_id)
    return ok([SubscriptionDownloadView(**row) for row in rows])


@router.get(
    "/{subscription_id}/activities",
    response_model=ApiResponse[list[ActivityView]],
    summary="订阅活动时间线（系统对该订阅做过的每个动作，时间倒序）",
    operation_id="sub.activities",
)
async def list_subscription_activities(
    subscription_id: int,
    limit: int = Query(default=100, ge=1, le=500, description="返回条数上限（时间倒序）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ActivityView]]:
    service = _service(session)
    rows = await service.activities(subscription_id, limit=limit)
    return ok([ActivityView.from_model(r) for r in rows])


@router.patch(
    "/{subscription_id}",
    response_model=ApiResponse[SubscriptionDetailView],
    summary="修改订阅（季选择/追新/规则组，diff 重算工单）",
    operation_id="sub.update",
)
async def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdatePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    service = _service(session)
    await service.update(
        subscription_id,
        selected_seasons=payload.selected_seasons,
        follow_future=payload.follow_future,
        rule_set_id=payload.rule_set_id,
        # library_id 要区分「未传=不变」与「显式 null=清除指定、回默认库路由」，
        # 用 model_fields_set 判断调用方是否真的带了这个字段
        library_id=payload.library_id if "library_id" in payload.model_fields_set else ...,
        quality_policy=(
            payload.quality_policy.model_dump()
            if payload.quality_policy is not None
            else None
        )
        if "quality_policy" in payload.model_fields_set
        else ...,
    )
    sub, item, wanted = await service.detail(subscription_id)
    return ok(SubscriptionDetailView.from_detail(sub, item, wanted), message="订阅已调整")


@router.post(
    "/{subscription_id}/search-now",
    response_model=ApiResponse[SearchNowView],
    summary="立即搜索：缺口工单跳过冷却重新排队，随即触发一轮缺口搜索",
    operation_id="sub.search-now",
)
async def search_subscription_now(
    subscription_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SearchNowView]:
    """只重置"本来就能搜"的缺口（未定档/待播出/在途的不碰）；订阅暂停中、
    或没有可搜缺口时给可读中文错误。"""
    service = _service(session)
    reset = await service.search_now(subscription_id)
    return ok(SearchNowView(reset_count=reset), message=f"{reset} 个缺口已重新排队，正在搜索")


@router.post(
    "/{subscription_id}/grab",
    response_model=ApiResponse[GrabResultView],
    summary="手动选种：把一条搜索结果直接投给本订阅（跳过规则组过滤）",
    operation_id="sub.grab",
)
async def grab_subscription_torrent(
    subscription_id: int,
    payload: GrabPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[GrabResultView]:
    """身份匹配不跳过：种子必须确认属于本条目且覆盖至少一个缺口。
    投递复用自动管线的 dispatch（三级兜底/救援巡检/活动记录全部继承）。"""
    from movieclaw_api.services.subscription.manual_grab import grab_manual

    covered = await grab_manual(
        session,
        subscription_id,
        site_id=payload.site_id,
        torrent_id=payload.torrent_id,
        title=payload.title,
        subtitle=payload.subtitle,
        category=payload.category,
        attrs=payload.attrs,
        download_url=payload.download_url,
        size_bytes=payload.size_bytes,
        seeders=payload.seeders,
        is_free=payload.is_free,
        hit_and_run=payload.hit_and_run,
        imdb_id=payload.imdb_id,
        douban_id=payload.douban_id,
        publish_time=payload.publish_time,
    )
    units = [
        DownloadUnitView(season_number=w.season_number, episode_number=w.episode_number)
        for w in covered
    ]
    return ok(GrabResultView(units=units), message=f"已投递，覆盖 {len(units)} 个追踪单元")


@router.patch(
    "/{subscription_id}/pause",
    response_model=ApiResponse[SubscriptionDetailView],
    summary="暂停 / 恢复订阅",
    operation_id="sub.pause",
)
async def pause_subscription(
    subscription_id: int,
    payload: SubscriptionPausePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    service = _service(session)
    await service.set_paused(subscription_id, payload.paused)
    sub, item, wanted = await service.detail(subscription_id)
    message = "已暂停，匹配与搜索将跳过该订阅" if payload.paused else "已恢复追踪"
    return ok(SubscriptionDetailView.from_detail(sub, item, wanted), message=message)


@router.delete(
    "/{subscription_id}",
    response_model=ApiResponse[dict],
    summary="删除订阅（不影响已下载内容）",
    operation_id="sub.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_subscription(
    subscription_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = _service(session)
    await service.delete(subscription_id)
    return ok({}, message="已取消订阅")
