from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_login
from movieclaw_api.exceptions import BadRequestException, ForbiddenException
from movieclaw_api.schemas.downloader import (
    DownloaderLimitsUpdate,
    DownloaderLimitsView,
    DownloaderPayload,
    DownloaderStatusUpdate,
    DownloaderView,
    DownloadSubmitPayload,
    DownloadSubmitView,
    DownloadTaskDeleteView,
    DownloadTaskListView,
    DownloadTaskReplaceView,
    DownloadTaskSourceView,
    DownloadTaskView,
    ManualDownloadCandidateView,
    ManualDownloadTargetPayload,
    ManualDownloadTargetView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.downloader_config import (
    DownloaderConfigService,
    verify_downloader,
)
from movieclaw_api.services.library.access import assert_library_visible
from movieclaw_api.services.library.config import LibraryConfigService
from movieclaw_api.services.torrent_submit import (
    anchor_manual_download,
    resolve_manual_target,
    submit_torrent,
    translate_save_path,
)
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/downloaders", tags=["downloaders"])

# 一键下载单独一个路由器（docs/design/member-management.md §3.2）：配置面
# 整组保持管理员专属，仅 /submit 按 allow_direct_download 能力放行成员——
# 不为一条路由把整组配置面降级为逐路由防守。挂载见 api/router.py。
submit_router = APIRouter(prefix="/downloaders", tags=["downloaders"])


def _mappings_to_rows(payload: DownloaderPayload) -> list[dict[str, str]] | None:
    """把请求体里的路径映射转成落库的字典列表（校验已在 schema 完成）。"""
    if payload.path_mappings is None:
        return None
    return [m.model_dump() for m in payload.path_mappings]


@router.post(
    "/resolve-target",
    response_model=ApiResponse[ManualDownloadTargetView],
    summary="识别手动搜索结果并预演媒体库与监听导入投递目录",
    operation_id="dl.resolve-target",
)
async def resolve_download_target(
    payload: ManualDownloadTargetPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ManualDownloadTargetView]:
    """手动下载弹窗的只读预检。

    先用标题、年份与副标题收敛出唯一 TMDB 身份，再复用订阅链路的
    ``preview_dispatch_route``：媒体库收藏范围负责选择库，监听导入规则负责
    给出实际投递源目录。未收敛时明确返回候选或未找到，不把不确定的种子
    静默放进默认库。
    """
    resolution = await resolve_manual_target(
        kind=payload.kind, title=payload.title, year=payload.year, subtitle=payload.subtitle
    )
    candidates = [
        ManualDownloadCandidateView(
            tmdb_id=candidate.tmdb_id,
            title=candidate.title,
            year=candidate.year,
            episode_count=candidate.episode_count,
        )
        for candidate in resolution.candidates
    ]
    candidate_ids = {candidate.tmdb_id for candidate in resolution.candidates}
    if resolution.tmdb_id is not None:
        candidate_ids.add(resolution.tmdb_id)
    tmdb_id = (
        payload.selected_tmdb_id if payload.selected_tmdb_id is not None else resolution.tmdb_id
    )
    if payload.selected_tmdb_id is not None and payload.selected_tmdb_id not in candidate_ids:
        raise BadRequestException("确认的 TMDB 条目不在本次识别候选中，请重新识别后再选择")
    if tmdb_id is None:
        status = "ambiguous" if candidates else "not_found"
        message = (
            "找到多个可能的影视条目，请确认后再选择自动入库目录"
            if candidates
            else "未能可靠识别该资源；请手选保存目录或补充准确的标题和年份"
        )
        return ok(ManualDownloadTargetView(status=status, candidates=candidates), message=message)

    # 与订阅预检/真实投递共享同一套「路由 → 监听规则 → 路径映射」判断，
    # 不能在此重算目录，否则多个入口会出现不同归宿。
    from movieclaw_api.services.subscription import preview_dispatch_route

    # 条目目录预览用识别到的 TMDB 标题，而不是种子标题——真实投递也是按
    # TMDB 身份建档后推导目录的，两边必须一致
    picked = next((c for c in resolution.candidates if c.tmdb_id == tmdb_id), None)
    preview = await preview_dispatch_route(
        session,
        kind=payload.kind,
        library_id=None,
        tmdb_id=tmdb_id,
        downloader_id=payload.downloader_id,
        title=picked.title if picked else payload.title,
        year=picked.year if picked else payload.year,
    )
    return ok(
        ManualDownloadTargetView(
            status="ready",
            tmdb_id=tmdb_id,
            candidates=candidates,
            **preview,
        ),
        message="已识别资源并预演自动入库目录",
    )


@submit_router.post(
    "/submit",
    response_model=ApiResponse[DownloadSubmitView],
    summary="把一条搜索结果种子提交到下载器（缺省默认下载器，可指定分流）",
    operation_id="dl.submit",
)
async def submit_download(
    payload: DownloadSubmitPayload,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloadSubmitView]:
    """手动下载：带站点登录态取回 .torrent → 提交给默认下载器。

    保存目录取值：调用方手选的 save_path 最优先；否则与订阅同一决策——
    选了库且库有监听导入规则 → 规则源目录（完成后监听导入接管、规范命名
    进库）；选库无规则 → 库推导路径（主根/标题 (年份)，直接下载进库原地
    收纳——扫描的完整性检测保证半成品不入账）；未选库 → 下载器默认目录。
    提交幂等：种子已在下载器中不视为错误，data.already_exists=true。
    """
    from movieclaw_api.services.library.routing import resolve_save_path

    if not principal.is_admin:
        # 成员版强制自动路由（§2.2）：手选目录/指定下载器都是管理员的事——
        # 前端成员弹窗不提供这两项，这里是绕过前端也拦得住的服务端强制
        if payload.save_path is not None:
            raise ForbiddenException("成员的一键下载不能手选保存目录，将按目标库自动选择")
        if payload.downloader_id is not None:
            raise ForbiddenException("成员的一键下载不能指定下载器，将使用默认下载器")
        # 智能入库按收藏范围路由，可能选中成员不可见的库并回显库名/路由理由，
        # 与成员的库可见性隔离冲突；成员端也不提供该入口，服务端一并拦住
        if payload.auto_route:
            raise ForbiddenException("成员的一键下载不支持智能入库，请选择可见的媒体库")
        if payload.library_id is not None:
            await assert_library_visible(session, principal, payload.library_id)

    library = None
    derived_path = None
    entry_level = False  # 条目级目录才允许锚下载线索
    manual_item = None
    route_reason: str | None = None
    if payload.auto_route:
        # 预检结果只作展示；真正提交时必须服务端按 TMDB 锚重新建档和路由，
        # 避免前端参数被篡改/配置变化后仍投到过期目录。
        from movieclaw_api.services.library.routing import route_for_item
        from movieclaw_api.services.media_discover import get_tmdb_client
        from movieclaw_api.services.media_library import MediaLibraryService
        from movieclaw_api.services.scrape_config import assign_scrape_library
        from movieclaw_media.models import MediaKind

        if payload.media_kind is None or payload.tmdb_id is None or not payload.title:
            # schema 校验已保证；此处显式再守一道，避免校验演进后带残缺身份建档
            raise BadRequestException("智能入库必须提供媒体类型、TMDB ID 和标题")
        manual_item = await MediaLibraryService(session, get_tmdb_client()).ensure_media_item(
            MediaKind(payload.media_kind), payload.tmdb_id, extra_aliases=[payload.title]
        )
        route = await route_for_item(session, payload.media_kind, manual_item)
        library = route.library
        route_reason = route.reason
        if library is None:
            raise BadRequestException("没有可用的目标媒体库，无法启用自动入库；请先创建媒体库")
        # 目标库要等条目建好才路由得出来，所以归属库在这里回填（设计文档 §14）
        if assign_scrape_library(manual_item, library):
            session.add(manual_item)
        decision = await resolve_save_path(
            session,
            library,
            kind=payload.media_kind,
            title=manual_item.title,
            year=manual_item.year,
            item=manual_item,
        )
        derived_path = decision.path
        entry_level = decision.entry_level
    elif payload.save_path is not None:
        # 用户在下载弹窗里手选了目录：完全尊重（映射守门仍在 submit_torrent）；
        # 手选目录不是条目级，不锚下载线索
        derived_path = payload.save_path
    elif payload.library_id is not None:
        # 三级兜底与订阅投递同一实现（library_routing.resolve_save_path）
        library = await LibraryConfigService(session).get(payload.library_id)
        decision = await resolve_save_path(
            session, library, kind=library.kind, title=payload.title, year=payload.year
        )
        derived_path = decision.path
        entry_level = decision.entry_level

    result, row = await submit_torrent(
        session,
        site_id=payload.site_id,
        download_url=payload.download_url,
        tags=["movieclaw-manual"],
        save_path=derived_path,
        # 线索只锚**条目级**目录；锚到库主根/监听目录会波及目录下所有文件
        subtitle=payload.subtitle if entry_level else None,
        downloader_id=payload.downloader_id,
    )
    assert row.id is not None  # 落库记录必有主键
    if manual_item is not None:
        assert manual_item.id is not None and library is not None and library.id is not None
        await anchor_manual_download(
            session,
            info_hash=result.info_hash,
            media_item_id=manual_item.id,
            library_id=library.id,
            downloader_id=row.id,
            download_name=result.name or None,
            save_path=derived_path,
            site_id=payload.site_id,
            torrent_id=payload.torrent_id,
        )
    view = DownloadSubmitView(
        info_hash=result.info_hash,
        name=result.name,
        already_exists=result.already_exists,
        downloader_id=row.id,
        downloader_name=row.name,
        # 回显下载器视角的实际保存目录（有路径映射时与本地视角不同），
        # 跨容器部署配错映射能在提交结果里第一时间看出来。成员不暴露路径。
        save_path=(
            None
            if not principal.is_admin
            else translate_save_path(derived_path or row.save_path, row.path_mappings)
        ),
    )
    if result.already_exists:
        message = "该种子已在下载器中，未重复添加"
    elif library is not None:
        message = f"已提交到「{row.name}」，入库到「{library.name}」"
        if route_reason:
            message += f"（{route_reason}）"
    else:
        message = f"已提交到「{row.name}」"
    return ok(view, message=message)


@router.get(
    "",
    response_model=ApiResponse[list[DownloaderView]],
    summary="列出已配置的下载器及连接状态",
    operation_id="dl.list",
)
async def list_downloaders(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[DownloaderView]]:
    service = DownloaderConfigService(session)
    rows = await service.list_all()
    return ok([DownloaderView.from_model(r) for r in rows])


@router.get(
    "/tasks",
    response_model=ApiResponse[DownloadTaskListView],
    summary="汇总所有下载器的活跃任务及订阅关联",
    operation_id="dl.tasks",
    openapi_extra={"x-cli-hidden": True},
)
async def list_download_tasks(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloadTaskListView]:
    """任务中心实时快照；单台下载器故障会降级为来源状态，不拖垮整页。"""
    from movieclaw_api.services.download_tasks import download_task_snapshot

    snapshot = await download_task_snapshot(session)
    return ok(
        DownloadTaskListView(
            items=[DownloadTaskView(**item) for item in snapshot["items"]],
            sources=[DownloadTaskSourceView(**source) for source in snapshot["sources"]],
        )
    )


@router.get(
    "/{downloader_id}/limits",
    response_model=ApiResponse[DownloaderLimitsView],
    summary="读取下载器的全局限速与任务队列上限",
    operation_id="dl.limits",
)
async def get_downloader_limits(
    downloader_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderLimitsView]:
    """实时读自下载器（限速、备用限速档、队列上限），不在本地落库。

    队列上限（qB 的最大活动种子数 / Transmission 的下载与做种队列）决定
    同时活动的任务数——刷流做种多时任务会撞上上限进排队，从这里可核对。
    """
    from movieclaw_api.services.download_tasks import get_downloader_limits as read_limits

    limits = await read_limits(session, downloader_id)
    return ok(DownloaderLimitsView(**limits.model_dump()))


@router.put(
    "/{downloader_id}/limits",
    response_model=ApiResponse[DownloaderLimitsView],
    summary="设置下载器的全局限速与任务队列上限",
    operation_id="dl.limits.set",
)
async def update_downloader_limits(
    downloader_id: int,
    payload: DownloaderLimitsUpdate,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderLimitsView]:
    """写入后回读生效值返回（下载器可能钳制/忽略部分字段）。

    限速传 null 表示取消限速；队列相关字段传 null 表示保持现状。
    """
    from movieclaw_api.services.download_tasks import set_downloader_limits
    from movieclaw_downloader.models import DownloaderLimits

    applied = await set_downloader_limits(
        session, downloader_id, DownloaderLimits(**payload.model_dump())
    )
    return ok(DownloaderLimitsView(**applied.model_dump()), message="下载器限制已更新")


@router.post(
    "/{downloader_id}/torrents/{info_hash}/replace",
    response_model=ApiResponse[DownloadTaskReplaceView],
    summary="立即为无进度的订阅任务寻找同品质替代源",
    operation_id="dl.torrent.replace",
    openapi_extra={"x-cli-hidden": True},
)
async def replace_stalled_subscription_download(
    background_tasks: BackgroundTasks,
    downloader_id: int,
    info_hash: str = Path(
        min_length=40,
        max_length=64,
        pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$",
        description="BT v1（40 位）或 v2（64 位）infohash",
    ),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloadTaskReplaceView]:
    """任务中心手动抢跑；请求立即返回，真实跨站搜索在后台执行。"""
    from movieclaw_api.services.subscription import request_replacement, run_replacement_search

    normalized_hash = info_hash.lower()
    attempt_id = await request_replacement(
        session,
        downloader_id=downloader_id,
        info_hash=normalized_hash,
    )
    background_tasks.add_task(run_replacement_search, attempt_id, force=True)
    return ok(
        DownloadTaskReplaceView(
            downloader_id=downloader_id,
            info_hash=normalized_hash,
            attempt_id=attempt_id,
        ),
        message="已开始跨站寻找同品质替代源；旧任务会保留到新源产生真实进度",
    )


@router.delete(
    "/{downloader_id}/torrents/{info_hash}",
    response_model=ApiResponse[DownloadTaskDeleteView],
    summary="从指定下载器删除种子任务（可选删除数据文件）",
    operation_id="dl.torrent.delete",
    openapi_extra={"x-cli-hidden": True, "x-cli-dangerous": "destructive"},
)
async def delete_download_task_from_downloader(
    downloader_id: int,
    info_hash: str = Path(
        min_length=40,
        max_length=64,
        pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$",
        description="BT v1（40 位）或 v2（64 位）infohash",
    ),
    delete_files: bool = Query(
        default=False,
        description="是否同时删除下载器任务对应的数据文件；默认仅移除任务",
    ),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloadTaskDeleteView]:
    """删除下载器实时任务；只有显式选择时才同时删除数据文件。"""
    from movieclaw_api.services.download_tasks import delete_download_task

    normalized_hash = info_hash.lower()
    await delete_download_task(
        session,
        downloader_id=downloader_id,
        info_hash=normalized_hash,
        delete_files=delete_files,
    )
    return ok(
        DownloadTaskDeleteView(
            downloader_id=downloader_id,
            info_hash=normalized_hash,
            delete_files=delete_files,
        ),
        message=("已删除种子任务和数据文件" if delete_files else "已删除种子任务，数据文件已保留"),
    )


@router.get(
    "/{downloader_id}",
    response_model=ApiResponse[DownloaderView],
    summary="获取单个下载器详情",
    operation_id="dl.show",
)
async def get_downloader(
    downloader_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    service = DownloaderConfigService(session)
    return ok(DownloaderView.from_model(await service.get(downloader_id)))


@router.post(
    "",
    response_model=ApiResponse[DownloaderView],
    summary="添加一个下载器（保存后异步测试连接）",
    operation_id="dl.add",
)
async def create_downloader_config(
    payload: DownloaderPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    """保存下载器连接信息（状态置 pending），并在后台异步测试连通性。

    接口立即返回，调用方可轮询下载器详情（CLI：mclaw dl show <id>）观察 status 变化：
    pending → verifying → active（可用）/ failed（见 last_error）。
    """
    service = DownloaderConfigService(session)
    row = await service.create(
        name=payload.name,
        client_type=payload.client_type,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        save_path=payload.save_path,
        path_mappings=_mappings_to_rows(payload),
        enabled=payload.enabled,
    )
    assert row.id is not None  # 落库后必有主键
    row = await service.start_verification(row.id)
    background_tasks.add_task(verify_downloader, row.id)
    return ok(DownloaderView.from_model(row), message="已保存，正在测试连接")


@router.put(
    "/{downloader_id}",
    response_model=ApiResponse[DownloaderView],
    summary="更新下载器配置（更新后重新测试连接）",
    operation_id="dl.update",
)
async def update_downloader_config(
    downloader_id: int,
    payload: DownloaderPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    service = DownloaderConfigService(session)
    await service.update(
        downloader_id,
        name=payload.name,
        client_type=payload.client_type,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        save_path=payload.save_path,
        path_mappings=_mappings_to_rows(payload),
        enabled=payload.enabled,
    )
    row = await service.start_verification(downloader_id)
    background_tasks.add_task(verify_downloader, downloader_id)
    return ok(DownloaderView.from_model(row), message="已更新，正在测试连接")


@router.patch(
    "/{downloader_id}/status",
    response_model=ApiResponse[DownloaderView],
    summary="启用 / 停用下载器",
    operation_id="dl.status.set",
)
async def set_downloader_status(
    downloader_id: int,
    payload: DownloaderStatusUpdate,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    service = DownloaderConfigService(session)
    row = await service.set_enabled(downloader_id, payload.enabled)
    return ok(DownloaderView.from_model(row))


@router.post(
    "/{downloader_id}/default",
    response_model=ApiResponse[DownloaderView],
    summary="设为默认下载器",
    operation_id="dl.default.set",
)
async def set_default_downloader(
    downloader_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    """把该下载器设为默认（一键下载不选目标时投给它），同时取消其他台的默认。

    注意：其他记录的 is_default 也会随之变化，调用方应重新读取列表。"""
    service = DownloaderConfigService(session)
    row = await service.set_default(downloader_id)
    return ok(DownloaderView.from_model(row), message="已设为默认下载器")


@router.post(
    "/{downloader_id}/verify",
    response_model=ApiResponse[DownloaderView],
    summary="手动重新测试连接",
    operation_id="dl.verify",
)
async def reverify_downloader(
    downloader_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    """手动触发一次连接测试（如下载器重启后想确认恢复）。

    行为：不存在 → 404；已在测试中 → 409；否则同步占位为 VERIFYING
    并在后台重新测试。"""
    service = DownloaderConfigService(session)
    row = await service.start_verification(downloader_id)
    background_tasks.add_task(verify_downloader, downloader_id)
    return ok(DownloaderView.from_model(row), message="已重新发起连接测试")


@router.delete(
    "/{downloader_id}",
    response_model=ApiResponse[dict],
    summary="删除下载器配置",
    operation_id="dl.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_downloader_config(
    downloader_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = DownloaderConfigService(session)
    await service.delete(downloader_id)
    return ok({}, message="已删除")
