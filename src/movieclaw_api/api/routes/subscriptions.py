from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import (
    require_admin,
    require_login,
    require_subscribe_capability,
)
from movieclaw_api.exceptions import (
    AppException,
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    UpstreamServiceException,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.subscription import (
    ActivityView,
    DispatchPreviewView,
    DownloadUnitView,
    GrabPayload,
    GrabResultView,
    MediaBrief,
    PipelineHealthView,
    PrepareView,
    ResolveCandidateView,
    SearchNowView,
    SeasonOverview,
    SubscriptionCreatePayload,
    SubscriptionCreateView,
    SubscriptionDetailView,
    SubscriptionDownloadView,
    SubscriptionFollowFuturePayload,
    SubscriptionTargetPreviewPayload,
    SubscriptionTrackingState,
    SubscriptionTrackingStatePayload,
    SubscriptionUpdatePayload,
    SubscriptionView,
    TodayArrivalView,
    UpgradeRunPayload,
    UpgradeRunView,
)
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.media_discover import get_tmdb_client
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.title_discovery import (
    get_title_discovery_service,
    parse_title_ref,
)
from movieclaw_db.engine import get_session
from movieclaw_db.repositories import LibraryFileRepository, MediaItemRepository
from movieclaw_media import DoubanError, TmdbError
from movieclaw_media.library import ResolveStatus
from movieclaw_media.models import MediaKind, MediaSource

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

# 权限分层（docs/design/member-management.md §2.2/§3.2）：
# - 读（列表/详情/时间线）：登录即可（路由器挂载时已注入 require_login）；
# - 写（发起/修改/追踪状态/退出/立即搜索）：路由级挂成员订阅能力开关；
# - 运维（投递预检/链路体检/下载明细/手动选种）：暴露落盘路径、种子与
#   下载器细节，路由级挂 require_admin。


def _service(session: AsyncSession) -> SubscriptionService:
    library = MediaLibraryService(session, get_tmdb_client())
    return SubscriptionService(session, library)


async def _prepare_resolved_target(
    *,
    kind: MediaKind,
    tmdb_id: int,
    douban_id: str | None,
    service: SubscriptionService,
    session: AsyncSession,
    suggested_season: int | None = None,
) -> PrepareView:
    """把已消歧的目标投影成订阅表单需要的季集、库存与现有订阅状态。"""
    item, seasons, existing = await service.prepare(kind, tmdb_id, douban_id=douban_id)

    assert item.id is not None
    owned = await LibraryFileRepository(session).owned_units(item.id)
    aired_units = await MediaItemRepository(session).aired_units_many(
        [item.id], include_specials=True
    )
    aired_by_season: dict[int, int] = {}
    for season_number, _episode in aired_units.get(item.id, set()):
        aired_by_season[season_number] = aired_by_season.get(season_number, 0) + 1
    return PrepareView(
        status="ready",
        media=MediaBrief.from_model(item),
        seasons=[
            SeasonOverview.from_row(
                row,
                aired_count=aired_by_season.get(row.season_number, 0),
                owned_units=owned,
            )
            for row in seasons
        ],
        existing_subscription_id=existing.id if existing else None,
        movie_owned=kind == MediaKind.MOVIE and (0, 0) in owned,
        # 只有确实存在于台账的季才提交给前端预勾选，避免收敛结果与季表不同步时
        # 弹层勾中一个渲染不出来的季
        suggested_seasons=(
            [suggested_season]
            if suggested_season is not None
            and any(row.season_number == suggested_season for row in seasons)
            else []
        ),
    )


async def _preview_title_ref(
    title_ref: str,
    *,
    service: SubscriptionService,
    session: AsyncSession,
) -> PrepareView:
    """解析 Discover 引用并完成订阅目标预览，不把来源组合规则泄漏给调用方。"""
    try:
        provider, kind, external_id = parse_title_ref(title_ref)
        if provider is MediaSource.TMDB:
            assert kind is not None
            return await _prepare_resolved_target(
                kind=kind,
                tmdb_id=int(external_id),
                douban_id=None,
                service=service,
                session=session,
            )

        details = await get_title_discovery_service().get_title_details(title_ref)
        kind = details.title.media_type
        if kind is None:
            raise BadRequestException("豆瓣条目缺少电影/剧集类型，暂时无法创建订阅")
        library = MediaLibraryService(session, get_tmdb_client())
        # 别名与首播日期是收敛证据：豆瓣把剧集按季拆条目，只有靠它们才能把
        # 「中餐厅 第十季」对上 TMDB 的《中餐厅》S10。详情本来就已拉回，不额外请求
        resolution, item = await library.resolve_douban(
            kind,
            details.title.title,
            year=details.title.release_year,
            douban_id=external_id,
            aliases=details.metadata.aliases,
            released=details.metadata.released,
        )
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    except (TmdbError, DoubanError) as exc:
        raise UpstreamServiceException(str(exc)) from exc

    if resolution.status is ResolveStatus.NOT_FOUND:
        return PrepareView(status="not_found")
    if resolution.status is ResolveStatus.AMBIGUOUS:
        return PrepareView(
            status="ambiguous",
            candidates=[
                ResolveCandidateView.from_model(candidate, kind=kind)
                for candidate in resolution.candidates
            ],
        )
    assert item is not None
    return await _prepare_resolved_target(
        kind=kind,
        tmdb_id=item.tmdb_id,
        douban_id=external_id,
        suggested_season=resolution.suggested_season,
        service=service,
        session=session,
    )


@router.post(
    "/title-preview",
    response_model=ApiResponse[PrepareView],
    summary="预览订阅目标的季集、库存和现有订阅状态",
    operation_id="ui.subscriptions.preview-title",
    dependencies=[Depends(require_subscribe_capability)],
    openapi_extra={"x-cli-hidden": True},
)
async def preview_subscription_title(
    payload: SubscriptionTargetPreviewPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PrepareView]:
    """Web 表单内部能力；公开 CLI 直接使用一体化的 ``subscriptions create``。"""
    service = _service(session)
    return ok(await _preview_title_ref(payload.title_ref, service=service, session=session))


@router.post(
    "",
    response_model=ApiResponse[SubscriptionCreateView],
    summary="从 Discover 影视条目创建订阅并生成初始追踪工单",
    operation_id="subscriptions.create",
)
async def create_subscription(
    payload: SubscriptionCreatePayload,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionCreateView]:
    """消费 Discover ``title_ref`` 完成来源解析、建档、路由预检与订阅创建。

    同一条目已有订阅时不会重复创建工单，成员会加入现有共享订阅。管理员可
    指定过滤规则与目标媒体库；成员调用时这两项由系统默认路由决定。

    剧集必须给出 ``selected_seasons``（或开启 ``follow_future`` 只追以后播出的
    新集），两者都缺时订阅不会期望任何内容，接口直接 400。
    """
    service = _service(session)
    prepared = await _preview_title_ref(payload.title_ref, service=service, session=session)
    if prepared.status == "not_found":
        raise NotFoundException("TMDB 未收录该条目，暂时无法订阅")
    if prepared.status == "ambiguous":
        raise AppException(
            status_code=409,
            code="SUBSCRIPTION_TARGET_AMBIGUOUS",
            message="该豆瓣条目对应多个可能的 TMDB 条目，请选择候选 title_ref 后重试",
            details=[candidate.model_dump(mode="json") for candidate in prepared.candidates],
        )
    assert prepared.media is not None
    kind = prepared.media.kind
    douban_id = prepared.media.douban_id
    if payload.source_title_ref:
        try:
            source_provider, _source_kind, source_id = parse_title_ref(payload.source_title_ref)
        except ValueError as exc:
            raise BadRequestException(str(exc)) from exc
        if source_provider is not MediaSource.DOUBAN:
            raise BadRequestException("source_title_ref 仅接受原始豆瓣 title_ref")
        douban_id = source_id

    is_member = not principal.is_admin
    download_routing = None
    if principal.is_admin:
        from movieclaw_api.services.subscription import preview_dispatch_route

        download_routing = DispatchPreviewView(
            **await preview_dispatch_route(
                session,
                kind=kind.value,
                library_id=payload.library_id,
                tmdb_id=prepared.media.tmdb_id,
            )
        )
    subscription = await service.create(
        kind,
        prepared.media.tmdb_id,
        selected_seasons=payload.selected_seasons,
        follow_future=payload.follow_future,
        rule_set_id=None if is_member else payload.rule_set_id,
        library_id=None if is_member else payload.library_id,
        douban_id=douban_id,
        member_id=principal.member_id if is_member else None,
    )
    assert subscription.id is not None
    sub, item, wanted = await service.detail(subscription.id)
    resource_timings = await service.resource_timings(subscription.id)
    return ok(
        SubscriptionCreateView(
            subscription=SubscriptionDetailView.from_detail(sub, item, wanted, resource_timings),
            download_routing=download_routing,
        ),
        message="已加入订阅，正在搜索缺失资源",
    )


@router.get(
    "/download-routing-preview",
    response_model=ApiResponse[DispatchPreviewView],
    summary="预览订阅资源的下载目标与自动入库路径",
    operation_id="subscriptions.preview-download-routing",
    dependencies=[Depends(require_admin)],
)
async def dispatch_preview(
    kind: str = Query(description="movie / tv"),
    library_id: int | None = Query(default=None, description="目标库；缺省走收藏范围路由"),
    tmdb_id: int | None = Query(
        default=None, description="TMDB 条目 ID；缺省库时据此按收藏范围路由选库"
    ),
    title: str | None = Query(
        default=None, description="条目标题；仅用于渲染条目目录预览（entry_dir）"
    ),
    year: int | None = Query(default=None, description="条目年份；仅用于条目目录预览"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DispatchPreviewView]:
    """按真实下载规则预演目标下载器、保存路径和最终媒体库，不产生任务。"""
    from movieclaw_api.services.subscription import preview_dispatch_route

    preview = await preview_dispatch_route(
        session, kind=kind, library_id=library_id, tmdb_id=tmdb_id, title=title, year=year
    )
    return ok(DispatchPreviewView(**preview))


@router.get(
    "/automation-readiness",
    response_model=ApiResponse[PipelineHealthView],
    summary="检查订阅自动搜索、下载、转移与入库链路是否就绪",
    operation_id="subscriptions.check-automation-readiness",
    dependencies=[Depends(require_admin)],
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
    summary="列出当前账号可见的电影和剧集订阅",
    operation_id="subscriptions.list",
)
async def list_subscriptions(
    kind: str | None = Query(default=None, description="movie / tv，缺省全部"),
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[SubscriptionView]]:
    """超管看全部；成员只看"自己发起的 + 自己关注的"（§3.5）。"""
    service = _service(session)
    rows = await service.list_with_progress(
        kind=kind,
        member_id=None if principal.is_admin else principal.member_id,
    )
    # 收录信息必须以元数据季骨架 + 媒体库实际在位文件为准，不能复用工单进度：
    # 创建订阅前已经在库的集不会生成工单，用工单数会把真实库存漏掉。
    tv_item_ids = list(
        {item.id for _sub, item, _counts in rows if item.kind == "tv" and item.id is not None}
    )
    media_repo = MediaItemRepository(session)
    seasons_by_item = await media_repo.list_seasons_many(tv_item_ids)
    aired_by_item = await media_repo.aired_units_many(tv_item_ids, include_specials=True)
    owned_by_item = await LibraryFileRepository(session).owned_units_many(tv_item_ids)

    views: list[SubscriptionView] = []
    for sub, item, counts in rows:
        item_id = item.id or -1
        aired = aired_by_item.get(item_id, set())
        owned = owned_by_item.get(item_id, set())
        collection = [
            SeasonOverview.from_row(
                season,
                aired_count=sum(1 for unit in aired if unit[0] == season.season_number),
                owned_units=owned,
            )
            for season in seasons_by_item.get(item_id, [])
        ]
        views.append(SubscriptionView.from_model(sub, item, counts, collection))
    # 「洗版中」批量派生（与详情页同口径）：海报墙据此给完结剧亮青点
    from movieclaw_api.services.subscription import upgrading_counts

    upgrading = await upgrading_counts(
        session, [sub.id for sub, _item, _counts in rows if sub.id is not None]
    )
    for view in views:
        view.progress.upgrading = upgrading.get(view.id, 0)
    return ok(views)


@router.get(
    "/today-arrivals",
    response_model=ApiResponse[list[TodayArrivalView]],
    summary="列出最近一次可能入库的订阅内容",
    operation_id="subscriptions.list-today-arrivals",
)
async def list_today_arrivals(
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[TodayArrivalView]]:
    """首页专用聚合：成员沿用“自己发起 + 自己关注”的订阅可见边界。

    今天没有安排时自动回退到一周内最近的那一天（``days_ahead`` > 0），
    因此返回空数组只意味着一周内确实无事可预告。
    """
    candidates = await _service(session).today_arrivals(
        member_id=None if principal.is_admin else principal.member_id
    )
    return ok(
        [
            TodayArrivalView.from_models(
                candidate.subscription,
                candidate.media,
                candidate.wanted,
                next_probe_at=candidate.next_probe_at,
                release_to_import_minutes=candidate.release_to_import_minutes,
                download_to_import_minutes=candidate.download_to_import_minutes,
                expected_day=candidate.expected_day,
                days_ahead=candidate.days_ahead,
            )
            for candidate in candidates
        ]
    )


@router.get(
    "/{subscription_id}",
    response_model=ApiResponse[SubscriptionDetailView],
    summary="获取一条订阅的设置、进度和缺失资源明细",
    operation_id="subscriptions.get",
)
async def get_subscription(
    subscription_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    service = _service(session)
    sub, item, wanted = await service.detail(subscription_id)
    resource_timings = await service.resource_timings(subscription_id)
    # 洗版派生状态需要规则组 spec（解析失败按未配置处理，不阻塞详情页）
    from movieclaw_db.models import RuleSet
    from movieclaw_matcher import RuleSetSpec

    rule_spec = None
    rule_set = await session.get(RuleSet, sub.rule_set_id)
    if rule_set is not None:
        try:
            rule_spec = RuleSetSpec.model_validate(rule_set.spec or {})
        except ValueError:
            rule_spec = None
    return ok(SubscriptionDetailView.from_detail(sub, item, wanted, resource_timings, rule_spec))


@router.get(
    "/{subscription_id}/active-downloads",
    response_model=ApiResponse[list[SubscriptionDownloadView]],
    summary="列出一条订阅当前正在进行的下载及实时进度",
    operation_id="subscriptions.list-active-downloads",
    dependencies=[Depends(require_admin)],
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
    summary="按时间倒序列出一条订阅的活动记录",
    operation_id="subscriptions.list-activities",
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
    summary="修改订阅的选季、自动续订、过滤规则或目标媒体库",
    operation_id="subscriptions.update",
)
async def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdatePayload,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    service = _service(session)
    await service.assert_can_manage(
        subscription_id, None if principal.is_admin else principal.member_id
    )
    await service.update(
        subscription_id,
        selected_seasons=payload.selected_seasons,
        follow_future=payload.follow_future,
        rule_set_id=payload.rule_set_id,
        # library_id 要区分「未传=不变」与「显式 null=清除指定、回默认库路由」，
        # 用 model_fields_set 判断调用方是否真的带了这个字段
        library_id=payload.library_id if "library_id" in payload.model_fields_set else ...,
    )
    sub, item, wanted = await service.detail(subscription_id)
    resource_timings = await service.resource_timings(subscription_id)
    return ok(
        SubscriptionDetailView.from_detail(sub, item, wanted, resource_timings),
        message="订阅已调整",
    )


@router.post(
    "/{subscription_id}/upgrade-runs",
    response_model=ApiResponse[UpgradeRunView],
    summary="触发一轮洗版：逐集体检并把可洗单元排入立即搜索",
    operation_id="subscriptions.upgrade-run",
)
async def run_subscription_upgrade(
    subscription_id: int,
    payload: UpgradeRunPayload,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[UpgradeRunView]:
    """「一轮洗版」（quality-upgrade.md §13.2）：可选换组 → 物化存量单元 →
    补快照 → 逐集体检 → 可洗单元立即排期 → 踢搜索。同步返回体检报告。"""
    from movieclaw_api.services.subscription import run_upgrade_round

    service = _service(session)
    await service.assert_can_manage(
        subscription_id, None if principal.is_admin else principal.member_id
    )
    report = await run_upgrade_round(session, subscription_id, rule_set_id=payload.rule_set_id)
    return ok(UpgradeRunView.model_validate(report), message=report["summary"])


@router.post(
    "/{subscription_id}/missing-resource-searches",
    response_model=ApiResponse[SearchNowView],
    summary="立即为一条订阅搜索目前缺失且已经可搜索的资源",
    operation_id="subscriptions.search-missing-resources",
)
async def search_subscription_now(
    subscription_id: int,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SearchNowView]:
    """剧集只重置"本来就能搜"的缺口（未定档/待播出/在途的不碰）；电影是
    用户强制——未上映/未定档也搜，搜完自动恢复上映档期调度。订阅暂停中、
    或没有可搜缺口时给可读中文错误。"""
    service = _service(session)
    await service.assert_can_manage(
        subscription_id, None if principal.is_admin else principal.member_id
    )
    reset = await service.search_now(subscription_id)
    return ok(SearchNowView(reset_count=reset), message=f"{reset} 个缺口已重新排队，正在搜索")


@router.post(
    "/{subscription_id}/selected-torrent-downloads",
    response_model=ApiResponse[GrabResultView],
    summary="下载为一条订阅人工选中的种子搜索结果",
    operation_id="subscriptions.download-selected-torrent",
    dependencies=[Depends(require_admin)],
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


async def _set_tracking_state(
    subscription_id: int,
    *,
    state: SubscriptionTrackingState,
    principal: Principal,
    session: AsyncSession,
) -> ApiResponse[SubscriptionDetailView]:
    """追踪状态变更的唯一实现。"""
    service = _service(session)
    await service.assert_can_manage(
        subscription_id, None if principal.is_admin else principal.member_id
    )
    paused = state is SubscriptionTrackingState.PAUSED
    await service.set_paused(subscription_id, paused)
    sub, item, wanted = await service.detail(subscription_id)
    resource_timings = await service.resource_timings(subscription_id)
    message = "已暂停，资源匹配与搜索将跳过该订阅" if paused else "已恢复追踪"
    return ok(
        SubscriptionDetailView.from_detail(sub, item, wanted, resource_timings),
        message=message,
    )


@router.patch(
    "/{subscription_id}/tracking-state",
    response_model=ApiResponse[SubscriptionDetailView],
    summary="把一条订阅的追踪状态明确设置为 active 或 paused",
    operation_id="subscriptions.set-tracking-state",
)
async def set_subscription_tracking_state(
    subscription_id: int,
    payload: SubscriptionTrackingStatePayload,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    return await _set_tracking_state(
        subscription_id,
        state=payload.state,
        principal=principal,
        session=session,
    )


@router.patch(
    "/{subscription_id}/follow-future",
    response_model=ApiResponse[SubscriptionDetailView],
    summary="开启或关闭一条剧集订阅的自动续订",
    operation_id="subscriptions.set-follow-future",
)
async def set_subscription_follow_future(
    subscription_id: int,
    payload: SubscriptionFollowFuturePayload,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubscriptionDetailView]:
    """详情页直接操作入口；电影订阅没有自动续订语义。"""
    service = _service(session)
    await service.assert_can_manage(
        subscription_id, None if principal.is_admin else principal.member_id
    )
    await service.set_follow_future(subscription_id, payload.enabled)
    sub, item, wanted = await service.detail(subscription_id)
    resource_timings = await service.resource_timings(subscription_id)
    message = "已开启自动续订" if payload.enabled else "已关闭自动续订"
    return ok(
        SubscriptionDetailView.from_detail(sub, item, wanted, resource_timings),
        message=message,
    )


@router.delete(
    "/{subscription_id}/following",
    response_model=ApiResponse[dict],
    summary="成员停止关注一条订阅而不影响其他正在追踪的成员",
    operation_id="subscriptions.unsubscribe",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def unsubscribe_from_subscription(
    subscription_id: int,
    principal: Principal = Depends(require_subscribe_capability),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    if principal.is_admin or principal.member_id is None:
        raise ForbiddenException("管理员没有成员关注关系；永久删除请使用 subscriptions delete")
    message = await _service(session).unsubscribe(
        subscription_id,
        member_id=principal.member_id,
    )
    return ok({}, message=message)


@router.delete(
    "/{subscription_id}",
    response_model=ApiResponse[dict],
    summary="管理员永久删除一条订阅及其追踪工单（不删除已下载内容）",
    operation_id="subscriptions.delete",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_subscription(
    subscription_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """管理员永久删除共享订阅；成员必须使用语义明确的取消关注接口。"""
    message = await _service(session).delete_permanently(subscription_id)
    return ok({}, message=message)
