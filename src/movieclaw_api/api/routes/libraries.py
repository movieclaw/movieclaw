from __future__ import annotations

import asyncio
from pathlib import Path, PurePath
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.api.deps import require_admin, require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, ConflictException, NotFoundException
from movieclaw_api.schemas.library import (
    ActorView,
    ArtworkCandidatesView,
    ArtworkCandidateView,
    ArtworkSelectPayload,
    AudioStreamView,
    ClaimBatchPayload,
    ClaimPayload,
    DetachPayload,
    DirectorView,
    IdentityReviewDecision,
    ItemDeleteResultView,
    LastOrganizeView,
    LastScanView,
    LibraryFileView,
    LibraryIndexEntryView,
    LibraryItemDetailView,
    LibraryItemView,
    LibraryPayload,
    LibraryReorderPayload,
    LibrarySearchGroupView,
    LibraryView,
    LocalMetaView,
    MediaSourceAnnotationCandidateView,
    MediaSourceAnnotationPayload,
    MetadataRefreshView,
    MissingClearPayload,
    MissingFileView,
    MissingItemView,
    OrganizePreviewView,
    OrganizeRenameView,
    OrganizeSidecarView,
    OrganizeSkipView,
    OrganizeStartView,
    PathReconcilePayload,
    PathReconcilePreviewView,
    RedownloadPayload,
    RefreshActiveView,
    ReidentifyGroupView,
    ReidentifyOutcomeView,
    ReidentifyPreviewView,
    ReidentifyResultView,
    RestorePayload,
    ReviewGroupView,
    ReviewItemView,
    ReviewResolvePayload,
    ScanProgressView,
    ScanResultView,
    SeasonEpisodesView,
    SubtitleCueView,
    SubtitleDeleteResultView,
    SubtitlePreviewView,
    SubtitleStreamView,
    TransferMoveView,
    TransferPayload,
    TransferPreviewView,
    TransferSkipView,
    TransferStartView,
    TransferStatusView,
    UnidentifiedCandidateView,
    UnidentifiedClearPayload,
    UnidentifiedFileView,
    UnidentifiedGroupView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import jobs, media_scrape
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library import claim as library_claim
from movieclaw_api.services.library import source_annotation
from movieclaw_api.services.library.access import (
    assert_library_visible,
    visible_library_ids,
)
from movieclaw_api.services.library.config import LibraryConfigService
from movieclaw_api.services.library.items import (
    build_item_detail,
    build_library_index,
    build_library_wall,
    build_season_episodes,
    delete_item_files,
    delete_single_file,
    episode_view,
    find_episode_thumb,
    local_item_artwork,
)
from movieclaw_api.services.library.items import (
    search_library_items as search_visible_library_items,
)
from movieclaw_api.services.library.layout import entry_dir_of
from movieclaw_api.services.library.organize import (
    build_organize_plan,
    enqueue_organize_job,
    is_organizing,
    last_organize,
    organize_progress,
)
from movieclaw_api.services.library.scan import (
    PHASE_LABELS,
    ScanPhase,
    busy_phase,
    enqueue_scan_job,
    last_scan,
    preview_reidentify,
    preview_root_path_reconcile,
    reidentify_item,
    request_stop_scan,
    scan_progress,
)
from movieclaw_api.services.library.subtitle_preview import (
    SubtitlePreviewError,
    SubtitleTrackNotFound,
    load_subtitle_preview,
)
from movieclaw_api.services.library.subtitles import (
    delete_external_subtitle,
    match_subtitle_filename,
    parse_subtitle_tokens,
)
from movieclaw_api.services.library.transfer import (
    assert_transferable,
    build_transfer_plan,
    enqueue_transfer_job,
    is_transferring,
    last_transfer,
    transfer_state,
)
from movieclaw_api.services.media_discover import get_tmdb_client
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.media_server_notify import notify_media_server_refresh
from movieclaw_api.services.playback import warmup as playback_warmup
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.title_discovery import parse_title_ref
from movieclaw_db.engine import get_database, get_session
from movieclaw_db.models import (
    ACTIVE_JOB_STATUSES,
    FileState,
    Job,
    JobStatus,
    LibraryFile,
    MediaItem,
    MediaItemPerson,
    MediaSeason,
    Person,
    Subscription,
)
from movieclaw_db.repositories import MediaItemRepository
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.genres import COUNTRY_NAMES, MOVIE_GENRES, REGION_PRESETS, TV_GENRES
from movieclaw_media.models import MediaKind, MediaSource

router = APIRouter(prefix="/libraries", tags=["libraries"])
search_router = APIRouter(prefix="/search", tags=["search"])


def _assignment_target(title_ref: str) -> tuple[MediaKind, int]:
    """把 Discover 引用收敛为人工身份指定所需的 TMDB 锚点。"""
    try:
        provider, kind, external_id = parse_title_ref(title_ref)
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    if provider is not MediaSource.TMDB or kind is None:
        raise BadRequestException("文件身份指定只接受 Discover 返回的 TMDB title_ref")
    return kind, int(external_id)


def _job_origin(client_name: object) -> str:
    """从统一客户端头识别 Web/CLI；直接调用路由的测试对象安全退回 Web。"""
    if isinstance(client_name, str) and client_name.lower() in {"web", "cli", "agent"}:
        return client_name.lower()
    return "web"


async def require_library_visible(
    library_id: int,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """成员库可见性依赖（docs/design/member-management.md §3.6）。

    挂在带 {library_id} 路径参数的**浏览类**路由上；白名单外的库对成员
    返回 404（与"库不存在"不可区分，不泄露存在性）。管理类路由已挂
    require_admin，管理员不受限，无需本依赖。
    """
    await assert_library_visible(session, principal, library_id)
    return principal


@router.get(
    "/{library_id}/cover",
    summary="库封面拼贴（氛围光货架，服务端渲染）",
    operation_id="libraries.cover",
    dependencies=[Depends(require_library_visible)],
    openapi_extra={"x-cli-hidden": True},
)
async def get_library_cover(library_id: int, request: Request) -> Response:
    """服务端渲染的库封面（与 Jellyfin 兼容层同一张图，docs/design/jellyfin-compat.md 5.6）。

    ETag=素材指纹：库内容不变时浏览器 304 秒回；变了自动重渲。前端直接
    <img> 引用，替代原先客户端 CSS 拼装的货架（渲染更快且双端一致）。
    """
    from movieclaw_api.services.library.cover import ensure_library_cover

    result = await ensure_library_cover(library_id)
    if result is None:
        raise NotFoundException("该库还没有可用的封面素材（无海报资产）")
    path, key = result
    etag = f'"{key}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


# 卡片要回答「在扫吗」和「上次扫的结果」，后者可能被若干条已取消/无结论的
# 作业挡在前面，所以往回多看几条。两条取数路径（单库查、列表批量查）必须
# 用同一个深度，否则同一个库在首页和详情页会显示不同的「上次扫描」
_SCAN_JOB_LOOKBACK = 10


def _scan_progress_view(library_id: int) -> ScanProgressView | None:
    """进行中扫描的实时状态（阶段 + 进度）；没在扫返回 None。

    与 ``is_scanning`` 读同一份状态，因此 ``scanning=true`` 时这里必不为
    None——前端可以放心按阶段渲染文案。
    """
    state = scan_progress(library_id)
    if state is None:
        return None
    return ScanProgressView(phase=state.phase, processed=state.processed, total=state.total)


def _queued_scan_view() -> ScanProgressView:
    """刚排上后台任务、还没真正开跑时的占位进度。

    建库/改根路径的响应会乐观地回 ``scanning=true``（后台任务紧随其后），
    这里把阶段一并填上，保证"在扫描但没有进度"的空档从不出现在接口上。
    """
    return ScanProgressView(phase=ScanPhase.WALKING, processed=0, total=0)


def _last_scan_view(library_id: int) -> LastScanView | None:
    """把进程内的最近扫描记录转成接口视图；没扫过返回 None。"""
    record = last_scan(library_id)
    if record is None:
        return None
    finished_at, summary = record
    return LastScanView(
        finished_at=finished_at,
        scanned=summary.scanned,
        identified=summary.identified,
        unidentified=summary.unidentified,
        marked_missing=summary.marked_missing,
        cleared_missing=summary.cleared_missing,
        removed_root_marked_missing=summary.removed_root_marked_missing,
        removed_root_cleared=summary.removed_root_cleared,
        removed_root_conflicts=summary.removed_root_conflicts,
        deferred=summary.deferred,
        retried=summary.retried,
        cancelled=summary.cancelled,
        errors=list(summary.errors),
    )


def _job_last_scan_view(job: Job) -> LastScanView | None:
    """从 Job 结论或取消检查点恢复最近扫描视图。"""
    if job.finished_at is None:
        return None
    if job.result:
        payload = job.result
    elif job.status is JobStatus.CANCELLED:
        progress = job.progress or {}
        payload = progress.get("details") if isinstance(progress.get("details"), dict) else {}
    else:
        return None
    return LastScanView(
        finished_at=job.finished_at,
        scanned=int(payload.get("scanned") or 0),
        identified=int(payload.get("identified") or 0),
        unidentified=int(payload.get("unidentified") or 0),
        marked_missing=int(payload.get("marked_missing") or 0),
        cleared_missing=int(payload.get("cleared_missing") or 0),
        removed_root_marked_missing=int(payload.get("removed_root_marked_missing") or 0),
        removed_root_cleared=int(payload.get("removed_root_cleared") or 0),
        removed_root_conflicts=int(payload.get("removed_root_conflicts") or 0),
        deferred=int(payload.get("deferred") or 0),
        retried=int(payload.get("retried") or 0),
        cancelled=job.status is JobStatus.CANCELLED or bool(payload.get("cancelled")),
        errors=list(payload.get("errors") or []),
    )


def _first_job_last_scan(rows: list[Job]) -> LastScanView | None:
    for row in rows:
        view = _job_last_scan_view(row)
        if view is not None:
            return view
    return None


async def _persistent_scan_views(
    session: AsyncSession, library_id: int
) -> tuple[bool, ScanProgressView | None, LastScanView | None]:
    """合并进程内细粒度状态与 Job 台账，服务重启时库卡片不丢进度。"""
    rows = await jobs.list_jobs(
        session,
        resource_type="library",
        resource_id=library_id,
        job_type="library.scan",
        limit=_SCAN_JOB_LOOKBACK,
    )
    return _scan_views_from_jobs(library_id, rows)


def _scan_views_from_jobs(
    library_id: int, rows: list[Job]
) -> tuple[bool, ScanProgressView | None, LastScanView | None]:
    """由「该库最近若干个 library.scan 作业」推出卡片要显示的扫描状态。

    与取数分离，是为了让列表页能批量取一次再逐库套用（见 list_libraries），
    而单库详情页仍走 ``_persistent_scan_views`` 各查各的——两条路径共用本
    函数，口径不会分叉。
    """
    legacy_progress = _scan_progress_view(library_id)
    legacy_last = _last_scan_view(library_id)
    active = next(
        (
            row
            for row in rows
            if row.status in ACTIVE_JOB_STATUSES and row.status is not JobStatus.BLOCKED
        ),
        None,
    )
    if active is not None:
        if legacy_progress is not None:
            progress_view = legacy_progress
        else:
            progress = active.progress or {}
            phase = str(progress.get("phase") or ScanPhase.WALKING.value)
            if phase not in {
                ScanPhase.WALKING.value,
                ScanPhase.INGESTING.value,
                ScanPhase.PROBING.value,
                ScanPhase.ASSETS.value,
            }:
                phase = ScanPhase.WALKING.value
            progress_view = ScanProgressView(
                phase=phase,
                processed=int(progress.get("current") or 0),
                total=int(progress.get("total") or 0),
            )
        previous = _first_job_last_scan([row for row in rows if row is not active])
        return True, progress_view, legacy_last or previous

    job_last = _first_job_last_scan(rows)
    if legacy_progress is not None:
        return True, legacy_progress, legacy_last or job_last
    if legacy_last is not None and (
        job_last is None or legacy_last.finished_at >= job_last.finished_at
    ):
        return False, None, legacy_last
    return False, None, job_last


def _metadata_refresh_view(library_id: int) -> MetadataRefreshView | None:
    """整库元数据刷新的实时状态；没在刷返回 None。

    随库列表一并返回，媒体库首页的卡片因此不必额外请求就能显示刷新进度
    （此前只有单库页拿得到，首页卡片对刷新一无所知）。
    """

    state = media_scrape.library_refresh_state(library_id)
    if state is None:
        if media_scrape.is_library_refreshing(library_id):
            return MetadataRefreshView(refreshing=True)
        return None
    return MetadataRefreshView(
        refreshing=media_scrape.is_library_refreshing(library_id),
        processed=state.processed,
        total=state.total,
        failed=state.failed,
        stopping=state.stopping,
        active=[
            RefreshActiveView(media_item_id=item_id, title=title, phase=phase)
            for item_id, (title, phase) in list(state.active.items())
        ],
    )


def _organize_progress_view(library_id: int) -> ScanProgressView | None:
    """进行中整理的实时进度；没在整理返回 None。"""
    progress = organize_progress(library_id)
    if progress is None:
        return None
    processed, total = progress
    # 整理只有一个阶段，但字段照样如实填上（前端按 phase 取文案，不做特例）
    return ScanProgressView(phase="organizing", processed=processed, total=total)


def _last_organize_view(library_id: int) -> LastOrganizeView | None:
    """把进程内的最近整理记录转成接口视图；没整理过返回 None。"""
    record = last_organize(library_id)
    if record is None:
        return None
    finished_at, summary = record
    return LastOrganizeView(
        finished_at=finished_at,
        renamed=summary.renamed,
        sidecars_renamed=summary.sidecars_renamed,
        entry_assets_moved=summary.entry_assets_moved,
        already_ok=summary.already_ok,
        skipped=summary.skipped,
        removed_dirs=summary.removed_dirs,
        errors=list(summary.errors),
    )


async def _persistent_organize_views(
    session: AsyncSession, library_id: int
) -> tuple[bool, ScanProgressView | None, LastOrganizeView | None]:
    """用 Job 补齐重启窗口与持久化结论；运行期细节仍优先读领域状态。"""
    legacy_progress = _organize_progress_view(library_id)
    legacy_last = _last_organize_view(library_id)
    if legacy_progress is not None:
        return True, legacy_progress, legacy_last
    latest = await jobs.latest_job_for_resource(
        session, "library", library_id, job_type="library.organize"
    )
    if latest is None:
        return False, None, legacy_last
    if latest.status in ACTIVE_JOB_STATUSES:
        progress = latest.progress or {}
        return (
            True,
            ScanProgressView(
                phase="organizing",
                processed=int(progress.get("current") or 0),
                total=int(progress.get("total") or 0),
            ),
            legacy_last,
        )
    if latest.result and latest.finished_at is not None:
        result = latest.result
        return (
            False,
            None,
            LastOrganizeView(
                finished_at=latest.finished_at,
                renamed=int(result.get("renamed") or 0),
                sidecars_renamed=int(result.get("sidecars_renamed") or 0),
                entry_assets_moved=int(result.get("entry_assets_moved") or 0),
                already_ok=int(result.get("already_ok") or 0),
                skipped=int(result.get("skipped") or 0),
                removed_dirs=int(result.get("removed_dirs") or 0),
                errors=list(result.get("errors") or []),
            ),
        )
    return False, None, legacy_last


async def _persistent_metadata_refresh_view(
    session: AsyncSession, library_id: int
) -> MetadataRefreshView | None:
    legacy = _metadata_refresh_view(library_id)
    if legacy is not None:
        return legacy
    latest = await jobs.latest_job_for_resource(
        session, "library", library_id, job_type="library.metadata.refresh"
    )
    if latest is None or latest.status not in ACTIVE_JOB_STATUSES:
        return None
    progress = latest.progress or {}
    details = progress.get("details") if isinstance(progress.get("details"), dict) else {}
    return MetadataRefreshView(
        refreshing=True,
        processed=int(progress.get("current") or 0),
        total=int(progress.get("total") or 0),
        failed=int(details.get("failed") or 0),
        stopping=latest.status is JobStatus.CANCELLING,
        active=[],
    )


@router.get(
    "",
    response_model=ApiResponse[list[LibraryView]],
    summary="列出全部媒体库（含库存统计，可按类型过滤）",
    operation_id="library.list",
)
async def list_libraries(
    kind: str | None = Query(default=None, description="movie / tv，缺省全部"),
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[LibraryView]]:
    service = LibraryConfigService(session)
    rows = await service.list_all(kind=kind)
    # 成员：按可见性白名单过滤，并抹掉落盘路径（成员不该知道服务器目录结构）
    visible = await visible_library_ids(session, principal)
    if visible is not None:
        rows = [r for r in rows if r.id in visible]
    # 逐库 await 一次 list_jobs 就是一次 N+1：79 个库 = 79 次 join 查询，
    # 且随 job 台账增长持续变慢。这里一次批量取回再逐库套用，口径与单库页
    # 共用 _scan_views_from_jobs，不会分叉。
    library_ids = [row.id for row in rows if row.id is not None]
    scan_jobs = await jobs.list_jobs_by_resource(
        session,
        resource_type="library",
        resource_ids=library_ids,
        job_type="library.scan",
        limit_per_resource=_SCAN_JOB_LOOKBACK,
    )
    scan_views = {
        library_id: _scan_views_from_jobs(library_id, scan_jobs.get(str(library_id), []))
        for library_id in library_ids
    }
    views = [
        LibraryView.from_model(
            r,
            scanning=scan_views.get(r.id, (False, None, None))[0],
            scan_progress=scan_views.get(r.id, (False, None, None))[1],
            last_scan=scan_views.get(r.id, (False, None, None))[2],
            organizing=is_organizing(r.id or -1),
            organize_progress=_organize_progress_view(r.id or -1),
            last_organize=_last_organize_view(r.id or -1),
            metadata_refresh=_metadata_refresh_view(r.id or -1),
        )
        for r in rows
    ]
    if not principal.is_admin:
        for view in views:
            view.root_paths = []
            view.primary_root = None
    return ok(views)


@router.get(
    "/routing-options",
    response_model=ApiResponse[dict],
    summary="列出媒体库路由规则可用的媒体类型、地区预设和类型标签",
    operation_id="library.list-routing-options",
    dependencies=[Depends(require_admin)],
)
async def routing_options() -> ApiResponse[dict]:
    """genre ID↔中文名与区域预设的唯一真相源在后端（movieclaw_media.genres），
    前端不自带常量表——两处各维护一份迟早漂移。"""

    return ok(
        {
            "movie_genres": [{"id": k, "label": v} for k, v in MOVIE_GENRES.items()],
            "tv_genres": [{"id": k, "label": v} for k, v in TV_GENRES.items()],
            "region_presets": REGION_PRESETS,
            "country_names": COUNTRY_NAMES,
        }
    )


async def _group_by_entry_dir(
    session: AsyncSession, rows: list[LibraryFile]
) -> list[UnidentifiedGroupView]:
    """把台账行按**条目目录**聚合成组（待识别清单与已忽略清单共用）。

    一部剧几十集逐集列出会把清单刷爆、也让人无从下手——同一个条目目录下
    的文件本就是同一部作品，聚成一组、一次处理整组才是用户实际要做的事。
    组内的失败原因与候选取第一条有值的（同目录同结论）。
    """
    libraries = {lib.id: lib for lib in await LibraryConfigService(session).list_all()}
    groups: dict[str, UnidentifiedGroupView] = {}
    for row in rows:
        library = libraries.get(row.library_id)
        path = Path(row.file_path)
        roots = [Path(p) for p in library.root_paths] if library else []
        entry = entry_dir_of(roots, path)
        key = str(entry) if entry is not None else row.file_path
        file_view = UnidentifiedFileView(
            id=row.id,  # type: ignore[arg-type]
            library_id=row.library_id,
            library_name=library.name if library else "?",
            file_path=row.file_path,
            size_bytes=row.size_bytes,
            season_number=row.season_number,
            episode_number=row.episode_number,
            reason=row.unidentified_reason,
            code=row.unidentified_code,
            candidates=[
                UnidentifiedCandidateView(**c) for c in (row.unidentified_candidates or [])
            ],
        )
        group = groups.get(key)
        if group is None:
            groups[key] = UnidentifiedGroupView(
                key=key,
                label=entry.name if entry is not None else path.name,
                library_id=row.library_id,
                library_name=file_view.library_name,
                file_count=1,
                total_size_bytes=row.size_bytes,
                reason=file_view.reason,
                code=file_view.code,
                candidates=file_view.candidates,
                files=[file_view],
            )
            continue
        group.file_count += 1
        group.total_size_bytes += row.size_bytes
        group.files.append(file_view)
        group.reason = group.reason or file_view.reason
        group.code = group.code or file_view.code
        group.candidates = group.candidates or file_view.candidates
    return list(groups.values())


@router.get(
    "/identification/unidentified-files",
    response_model=ApiResponse[list[UnidentifiedGroupView]],
    summary="待识别清单（按条目目录分组，不含已忽略，可按库过滤）",
    operation_id="library.identification.list-unidentified-files",
    dependencies=[Depends(require_admin)],
)
async def list_unidentified(
    library_id: int | None = Query(default=None, description="按库过滤；不传=全部库"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[UnidentifiedGroupView]]:
    repo = LibraryFileRepository(session)
    rows = await repo.list_unidentified(library_id=library_id)
    return ok(await _group_by_entry_dir(session, rows))


@router.get(
    "/identification/ignored-files",
    response_model=ApiResponse[list[UnidentifiedGroupView]],
    summary="已忽略清单（用户说过「别再问」的文件，可恢复）",
    operation_id="library.identification.list-ignored-files",
    dependencies=[Depends(require_admin)],
)
async def list_ignored(
    library_id: int | None = Query(default=None, description="按库过滤；不传=全部库"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[UnidentifiedGroupView]]:
    """被忽略的文件不再参与识别，也不占待识别清单——但记录始终保留。

    识别器一直在变强（本地集数佐证、路径 tmdbid 标记都是后加的），当初
    认不出的东西以后未必认不出，所以忽略必须可反悔：在这里恢复即可让它
    重新参与下一次扫描的识别。
    """
    repo = LibraryFileRepository(session)
    rows = await repo.list_ignored(library_id=library_id)
    return ok(await _group_by_entry_dir(session, rows))


@router.get(
    "/identification/review-cases",
    response_model=ApiResponse[list[ReviewGroupView]],
    summary="身份复核清单（识别器升级后的新旧结论分歧，可按库过滤）",
    operation_id="library.identification.list-review-cases",
    dependencies=[Depends(require_admin)],
)
async def list_identity_review(
    library_id: int | None = Query(default=None, description="按库过滤；不传=全部库"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ReviewGroupView]]:
    """识别器升级后，扫描复核发现「现挂身份」与「新识别结论」不一致的文件。

    身份没有被自动改动（新识别器也可能错，静默翻案会把改对的东西改错），
    在这里摆出两个身份让用户拍板。与待识别清单同理，按条目目录聚合——
    同目录同分歧的几十集聚成一条，一次拍板整组生效。
    """

    base = get_settings().tmdb_image_base_url.rstrip("/")
    repo = LibraryFileRepository(session)
    rows = await repo.list_review(library_id=library_id)
    libraries = {lib.id: lib for lib in await LibraryConfigService(session).list_all()}
    item_ids = {row.media_item_id for row in rows if row.media_item_id is not None}
    items: dict[int, MediaItem] = {}
    if item_ids:
        found = (
            (await session.execute(select(MediaItem).where(MediaItem.id.in_(item_ids))))  # type: ignore[union-attr]
            .scalars()
            .all()
        )
        items = {i.id: i for i in found if i.id is not None}

    groups: dict[tuple, ReviewGroupView] = {}
    for row in rows:
        suggestion = row.review_suggestion or {}
        current_item = items.get(row.media_item_id or -1)
        if current_item is None or not suggestion.get("media_item_id"):
            continue  # 数据残缺（条目被删等）：跳过，重识别通道兜底
        library = libraries.get(row.library_id)
        path = Path(row.file_path)
        roots = [Path(p) for p in library.root_paths] if library else []
        entry = entry_dir_of(roots, path)
        entry_key = str(entry) if entry is not None else row.file_path
        key = (entry_key, row.media_item_id, suggestion["media_item_id"])
        group = groups.get(key)
        if group is None:
            groups[key] = ReviewGroupView(
                key=f"{entry_key}::{row.media_item_id}->{suggestion['media_item_id']}",
                label=entry.name if entry is not None else path.name,
                library_id=row.library_id,
                library_name=library.name if library else "?",
                file_count=1,
                total_size_bytes=row.size_bytes,
                file_ids=[row.id],  # type: ignore[list-item]
                current=ReviewItemView(
                    media_item_id=current_item.id,  # type: ignore[arg-type]
                    tmdb_id=current_item.tmdb_id,
                    title=current_item.title,
                    year=current_item.year,
                    poster_url=(
                        f"{base}/w185{current_item.poster_path}"
                        if current_item.poster_path
                        else None
                    ),
                ),
                suggestion=ReviewItemView(
                    media_item_id=suggestion["media_item_id"],
                    tmdb_id=suggestion.get("tmdb_id"),
                    title=suggestion.get("title") or "?",
                    year=suggestion.get("year"),
                    poster_url=(
                        f"{base}/w185{suggestion['poster_path']}"
                        if suggestion.get("poster_path")
                        else None
                    ),
                ),
            )
            continue
        group.file_count += 1
        group.total_size_bytes += row.size_bytes
        group.file_ids.append(row.id)  # type: ignore[arg-type]
    return ok(list(groups.values()))


@router.post(
    "/identification/review-decisions",
    response_model=ApiResponse[dict],
    summary="决定身份复核结果：采纳建议或维持当前身份",
    operation_id="library.identification.resolve-review",
    dependencies=[Depends(require_admin)],
)
async def resolve_identity_review(
    payload: ReviewResolvePayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """对复核清单里的文件拍板（实现见 services/library/claim.resolve_review）。"""

    resolved, title, displaced = await library_claim.resolve_review(
        session,
        payload.file_ids,
        accept=payload.decision is IdentityReviewDecision.ACCEPT_SUGGESTION,
    )
    # 改挂后旧条目可能一个文件都不剩，连同图片资产清掉，不在库里留空壳
    if displaced:
        background_tasks.add_task(media_scrape.cleanup_orphan_items, sorted(displaced))
    message = (
        f"{resolved} 个文件已改挂为《{title}》"
        if payload.decision is IdentityReviewDecision.ACCEPT_SUGGESTION and title
        else f"{resolved} 个文件维持现有身份，不再提醒"
    )
    return ok({"resolved": resolved}, message=message)


@search_router.get(
    "/library-items",
    response_model=ApiResponse[list[LibrarySearchGroupView]],
    summary="按关键词搜索已入库条目（跨全部媒体库，标题/原名匹配，按库分组）",
    operation_id="search.library-items",
)
async def search_library_items(
    keyword: str = Query(
        ..., min_length=1, max_length=100, description="搜索关键词（标题或原名的子串，忽略大小写）"
    ),
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[LibrarySearchGroupView]]:
    """搜索页「媒体库」垂直的数据源：回答「这部片我有没有」。

    只搜已识别入库的条目（待识别文件没有可靠标题，去待识别清单处理）；
    本地查询毫秒级返回。刻意不写入搜索历史——搜自己的库是翻家底，
    不是一次对外搜索，历史里混进它只会淹没真正要回放的记录。
    成员的结果按库可见性白名单过滤。
    """
    matched = await search_visible_library_items(session, keyword)
    libraries = await LibraryConfigService(session).list_all()
    visible = await visible_library_ids(session, principal)
    if visible is not None:
        libraries = [lib for lib in libraries if lib.id in visible]
    # 分组顺序沿用库列表的顺序（与媒体库首页一致），空组不出现
    return ok(
        [
            LibrarySearchGroupView(
                library_id=lib.id,  # type: ignore[arg-type]
                library_name=lib.name,
                kind=MediaKind(lib.kind),
                items=matched[lib.id],
            )
            for lib in libraries
            if lib.id in matched
        ]
    )


@router.get(
    "/{library_id}",
    response_model=ApiResponse[LibraryView],
    summary="获取单个媒体库详情",
    operation_id="library.get",
    dependencies=[Depends(require_library_visible)],
)
async def get_library(
    library_id: int,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LibraryView]:
    service = LibraryConfigService(session)
    row = await service.get(library_id)
    scanning, scan_view, last_scan_view = await _persistent_scan_views(session, library_id)
    organizing, organize_view, last_organize_view = await _persistent_organize_views(
        session, library_id
    )
    # 字段口径与列表接口保持一致（stats/metadata_refresh 一个不缺）。
    # stats 已随 Library 行读出，不再为一次详情请求扫描整张库存台账。
    view = LibraryView.from_model(
        row,
        scanning=scanning,
        scan_progress=scan_view,
        last_scan=last_scan_view,
        organizing=organizing,
        organize_progress=organize_view,
        last_organize=last_organize_view,
        metadata_refresh=await _persistent_metadata_refresh_view(session, library_id),
    )
    if not principal.is_admin:
        # 成员不暴露服务器目录结构（与列表接口同一口径）
        view.root_paths = []
        view.primary_root = None
    return ok(view)


@router.post(
    "",
    response_model=ApiResponse[LibraryView],
    summary="创建媒体库（该类型首个库自动成为默认，并自动开始首次扫描）",
    operation_id="library.create",
    dependencies=[Depends(require_admin)],
)
async def create_library(
    payload: LibraryPayload,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LibraryView]:
    service = LibraryConfigService(session)
    row = await service.create(
        name=payload.name,
        kind=payload.kind,
        root_paths=payload.root_paths,
        match_rules=payload.match_rules,
        auto_clear_missing=payload.auto_clear_missing,
        realtime_watch=payload.realtime_watch,
        scrape_overrides=payload.scrape_overrides,
    )
    # 建库即扫描：根路径下的存量文件立刻开始识别入账，不用用户再手动点一次
    assert row.id is not None
    await enqueue_scan_job(session, row.id, row.name, origin=_job_origin(client_name))
    return ok(
        LibraryView.from_model(row, scanning=True, scan_progress=_queued_scan_view()),
        message=f"已创建媒体库「{row.name}」，正在扫描存量文件",
    )


async def _assert_not_busy(session: AsyncSession, library_name: str, library_id: int) -> None:
    """扫描/整理/转移期间锁定库的编辑与删除——这些任务都在按当前根路径
    批量读写台账，此刻改根路径或删库会让进行中的任务写入过期配置。"""
    phase = busy_phase(library_id)
    if phase is not None:
        raise ConflictException(
            f"「{library_name}」{PHASE_LABELS[phase]}，暂不能编辑或删除；请等当前任务完成"
        )
    if is_organizing(library_id):
        raise ConflictException(
            f"「{library_name}」正在整理文件名，暂不能编辑或删除；请等待整理完成"
        )
    if is_transferring(library_id):
        raise ConflictException(f"「{library_name}」正在转移条目，暂不能编辑或删除；请等待转移完成")
    active = await jobs.list_jobs(
        session,
        active_only=True,
        resource_type="library",
        resource_id=library_id,
        limit=20,
    )
    labels = {
        "library.scan": "扫描媒体库",
        "library.organize": "整理文件名",
        "library.transfer": "转移条目",
        "library.metadata.refresh": "刷新元数据",
    }
    blocking = next((row for row in active if row.job_type in labels), None)
    if blocking is not None:
        raise ConflictException(
            f"「{library_name}」正在{labels[blocking.job_type]}，暂不能编辑或删除；"
            "可到活动页查看进度"
        )


async def _quiesce_scan_for_mutation(
    session: AsyncSession, library_name: str, library_id: int
) -> None:
    """根路径变更/删库会取代旧扫描，先协作取消并等到安全边界。

    持久化 Job 接口会立即返回，用户很可能建库后马上修正路径或删除误建的
    库。让这类操作永远撞 409 是执行模型泄漏；但直接改又会让旧扫描按过期
    根路径写台账。这里最多等五秒让逐文件扫描收口，长单元仍明确提示稍后重试。
    """
    phase = busy_phase(library_id)
    if phase is ScanPhase.REIDENTIFYING:
        raise ConflictException(f"「{library_name}」正在重新识别条目，请等待完成后再操作")
    active = await jobs.list_jobs(
        session,
        active_only=True,
        job_type="library.scan",
        resource_type="library",
        resource_id=library_id,
        limit=10,
    )
    for row in active:
        await jobs.request_cancel(
            session,
            row.id,
            requested_by="媒体库配置变更",
            reason="媒体库配置已变更或已删除，按旧配置进行的扫描不再适用，系统自动取消",
        )
    if phase is not None and not active:
        request_stop_scan(library_id)  # 文件监听/定时对账触发的兼容扫描
    if not active and phase is None:
        return

    deadline = asyncio.get_running_loop().time() + 5.0
    db = get_database()
    while asyncio.get_running_loop().time() < deadline:
        async with db.session() as check_session:
            remaining = await jobs.list_jobs(
                check_session,
                active_only=True,
                job_type="library.scan",
                resource_type="library",
                resource_id=library_id,
                limit=1,
            )
        if not remaining and busy_phase(library_id) is None:
            return
        await asyncio.sleep(0.05)
    raise ConflictException(
        f"「{library_name}」的扫描正在安全停止，请稍后重试；可到活动页查看进度"
    )


@router.put(
    "/display-order",
    response_model=ApiResponse[dict],
    summary="重排媒体库展示顺序（决定首页卡片与「最近添加」分区的排列）",
    operation_id="library.reorder",
    dependencies=[Depends(require_admin)],
)
async def reorder_libraries(
    payload: LibraryReorderPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """按给定 id 顺序重写全部库的展示顺序。列表必须包含且仅包含现存的
    全部库 id——部分排序语义不明确，库列表变化过就让前端刷新后重试。

    必须注册在 ``PUT /{library_id}`` 之前：FastAPI 的路径参数按通配段匹配、
    到校验层才转 int，注册在后面的话 ``/order`` 会被它抢走并报 422。"""
    service = LibraryConfigService(session)
    await service.reorder(payload.ordered_ids)
    return ok({}, message="媒体库顺序已更新")


@router.put(
    "/{library_id}",
    response_model=ApiResponse[LibraryView],
    summary="更新媒体库（类型创建后不可改；变更根路径时要求库空闲）",
    operation_id="library.update",
    dependencies=[Depends(require_admin)],
)
async def update_library(
    library_id: int,
    payload: LibraryPayload,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LibraryView]:
    service = LibraryConfigService(session)
    before = await service.get(library_id)
    # ``service.update`` 在同一 ORM 会话里原地修改实体；先取不可变快照，后台
    # 扫描才能知道这次编辑真正替换的是哪些根，而不是读到更新后的新根列表。
    previous_root_paths = list(before.root_paths)
    roots_changed = previous_root_paths != [p.strip() for p in payload.root_paths if p.strip()]
    # 扫描/整理依赖根路径，只有真的改路径才需要锁库；改展示名称、收藏规则
    # 或下轮扫描策略不触碰当前任务正在使用的路径与台账，允许即时保存。
    if roots_changed:
        await _quiesce_scan_for_mutation(session, before.name, library_id)
        await _assert_not_busy(session, before.name, library_id)
    row = await service.update(
        library_id,
        name=payload.name,
        root_paths=payload.root_paths,
        match_rules=payload.match_rules,
        auto_clear_missing=payload.auto_clear_missing,
        realtime_watch=payload.realtime_watch,
        scrape_overrides=payload.scrape_overrides,
    )
    # 根路径变了就自动补扫：新目录的存量立刻入账，移除目录下的文件标记 missing
    if roots_changed:
        # 这轮扫描额外按 inode 对账旧根遗留台账：根路径只是换了挂载别名/软链接
        # 入口时，原行随迁而不是把同一文件再入账一遍。普通手动扫描不做该
        # 对账，避免为已移除根路径下的历史记录反复触发文件系统访问。
        await enqueue_scan_job(
            session,
            library_id,
            row.name,
            origin=_job_origin(client_name),
            reconcile_root_change=True,
            previous_root_paths=previous_root_paths,
        )
        return ok(
            LibraryView.from_model(row, scanning=True, scan_progress=_queued_scan_view()),
            message="已更新，正在按新的根路径重新扫描",
        )
    return ok(LibraryView.from_model(row), message="已更新")


@router.post(
    "/{library_id}/default-selection",
    response_model=ApiResponse[LibraryView],
    summary="设为该类型的默认库",
    operation_id="library.set-default",
    dependencies=[Depends(require_admin)],
)
async def set_default_library(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LibraryView]:
    """把该库设为其类型的默认库（订阅/手动下载不选库时用它），
    同 kind 其他库的默认标记随之取消，调用方应重新读取列表。"""
    service = LibraryConfigService(session)
    row = await service.set_default(library_id)
    return ok(LibraryView.from_model(row), message=f"「{row.name}」已设为默认库")


@router.delete(
    "/{library_id}",
    response_model=ApiResponse[dict],
    summary="删除媒体库（不动磁盘文件；其订阅回落到该类型默认库；扫描/整理中锁定）",
    operation_id="library.delete",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_library(
    library_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:

    service = LibraryConfigService(session)
    row = await service.get(library_id)
    await _quiesce_scan_for_mutation(session, row.name, library_id)
    await _assert_not_busy(session, row.name, library_id)
    # 删库前记下涉及的条目：库删掉后台账行随之级联消失，届时就查不到了
    affected = [
        i
        for i in (
            await session.execute(
                select(LibraryFile.media_item_id)
                .where(
                    LibraryFile.library_id == library_id,
                    LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
                )
                .distinct()
            )
        )
        .scalars()
        .all()
        if i is not None
    ]
    await service.delete(library_id)
    # 孤儿清理放后台：删几百个资产目录是纯磁盘活，不该拖住删库这一次请求
    background_tasks.add_task(media_scrape.cleanup_orphan_items, affected)
    return ok({}, message="已删除（磁盘上的媒体文件未受影响）")


# ---------------------------------------------------------------------------
# 库存（L3）：扫描 / 条目聚合 / 待识别认领
# ---------------------------------------------------------------------------


@router.post(
    "/{library_id}/scan",
    response_model=ApiResponse[ScanResultView],
    summary="扫描该库的根路径，把存量文件识别入账（后台执行）",
    operation_id="library.scan.start",
    dependencies=[Depends(require_admin)],
    openapi_extra={
        "x-cli-job": {"id_path": "job_id", "wait_op": "jobs.wait"},
    },
    status_code=202,
)
async def start_scan(
    library_id: int,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ScanResultView]:
    """增量扫描：已在台账的文件秒过；新文件走 NFO → 文件名解析 → TMDB
    识别链，认不出的进「待识别」清单。扫描绝不移动/改名/删除存量文件。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    phase = busy_phase(library_id)
    if phase is not None:
        raise ConflictException(f"「{library.name}」{PHASE_LABELS[phase]}，请等待完成")
    if is_organizing(library_id):
        raise ConflictException(f"「{library.name}」正在整理文件名，请等待整理完成后再扫描")
    if is_transferring(library_id):
        raise ConflictException(f"「{library.name}」正在转移条目，请等待转移完成后再扫描")
    created = await enqueue_scan_job(
        session, library_id, library.name, origin=_job_origin(client_name)
    )
    return ok(
        ScanResultView(
            started=True,
            message=f"已开始扫描「{library.name}」",
            job_id=created.job.id,
            created=created.created,
        ),
        message=(
            f"已开始扫描「{library.name}」，可在活动页继续观察"
            if created.created
            else f"「{library.name}」的扫描已在活动页进行中"
        ),
    )


def _normalise_reconcile_root(path: str) -> str:
    """校验管理员修复入口的绝对根路径，保持与媒体库配置相同的尾斜杠语义。"""
    cleaned = path.strip()
    if not cleaned.startswith("/"):
        raise BadRequestException(f"根路径必须是绝对路径：{path}")
    return cleaned if cleaned == "/" else cleaned.rstrip("/")


def _validated_reconcile_roots(
    library,
    payload: PathReconcilePayload,
) -> tuple[str, str]:
    """确保历史修复不会把仍在配置中的根误当成旧根，也不扫描任意目录。"""
    old_root = _normalise_reconcile_root(payload.old_root)
    new_root = _normalise_reconcile_root(payload.new_root)
    current_roots = {_normalise_reconcile_root(root) for root in library.root_paths}
    if old_root in current_roots:
        raise BadRequestException("旧根路径仍在当前媒体库配置中，不能作为已移除根修复")
    if new_root not in current_roots:
        raise BadRequestException("目标根路径必须是当前媒体库已配置的根路径")
    return old_root, new_root


@router.post(
    "/{library_id}/path-reconciliation-preview",
    response_model=ApiResponse[PathReconcilePreviewView],
    summary="预览历史根路径迁移修复（只读，不扫描、不修改台账）",
    operation_id="workflow.library.reconcile-paths.preview",
    dependencies=[Depends(require_admin)],
    # CLI 必须经精选层的「预览 → --yes」工作流，不能让生成命令绕过确认。
    openapi_extra={"x-cli-hidden": True},
)
async def preview_path_reconcile(
    library_id: int,
    payload: PathReconcilePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PathReconcilePreviewView]:
    """让管理员在执行历史台账收口前确认影响面；预览绝不操作磁盘或数据库。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    old_root, new_root = _validated_reconcile_roots(library, payload)
    preview = await preview_root_path_reconcile(
        session, library, old_root=old_root, new_root=new_root
    )
    return ok(PathReconcilePreviewView(**preview.__dict__))


@router.post(
    "/{library_id}/path-reconciliations",
    response_model=ApiResponse[ScanResultView],
    summary="执行历史根路径迁移修复（重新扫描新根并收口旧路径台账）",
    operation_id="workflow.library.reconcile-paths.start",
    dependencies=[Depends(require_admin)],
    openapi_extra={
        "x-cli-job": {"id_path": "job_id", "wait_op": "jobs.wait"},
        "x-cli-hidden": True,
    },
    status_code=202,
)
async def start_path_reconcile(
    library_id: int,
    payload: PathReconcilePayload,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ScanResultView]:
    """通过持久化扫描作业执行修复，复用库锁且可在任务中心观察或停止。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    old_root, new_root = _validated_reconcile_roots(library, payload)
    await _assert_not_busy(session, library.name, library_id)
    created = await enqueue_scan_job(
        session,
        library_id,
        library.name,
        origin=_job_origin(client_name),
        reconcile_root_change=True,
        previous_root_paths=[old_root],
        reconcile_new_root_paths=[new_root],
    )
    return ok(
        ScanResultView(
            started=True,
            message=f"已开始修复「{library.name}」的历史根路径台账",
            job_id=created.job.id,
            created=created.created,
        ),
        message=(
            "已开始重新扫描并收口旧路径台账；仅修改数据库记录，不会删除磁盘文件"
            if created.created
            else f"「{library.name}」的扫描已在活动页进行中"
        ),
    )


@router.post(
    "/{library_id}/scan/stop",
    response_model=ApiResponse[dict],
    summary="停止进行中的扫描（已入账的保留，剩余文件下次扫描继续）",
    operation_id="library.scan.stop",
    dependencies=[Depends(require_admin)],
)
async def stop_scan(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    active = await jobs.list_jobs(
        session,
        active_only=True,
        job_type="library.scan",
        resource_type="library",
        resource_id=library_id,
        limit=1,
    )
    if active:
        job, accepted = await jobs.request_cancel(
            session, active[0].id, requested_by="媒体库扫描停止入口"
        )
        if job is not None and accepted:
            return ok(
                {"job_id": job.id},
                message=f"正在停止「{library.name}」的扫描（当前单位处理完即停下）",
            )
    if not request_stop_scan(library_id):
        # 重识别占着同一把库级锁但不可中途停止，得说清楚是什么在跑，
        # 不能笼统回一句"没有扫描"让用户以为界面在骗人
        phase = busy_phase(library_id)
        if phase is not None:
            raise ConflictException(f"「{library.name}」{PHASE_LABELS[phase]}，该任务不能中途停止")
        raise ConflictException(f"「{library.name}」当前没有进行中的扫描")
    return ok({}, message=f"正在停止「{library.name}」的扫描（当前单位处理完即停下）")


# ---------------------------------------------------------------------------
# 元数据刷新（docs/design/metadata.md 4.2/4.3）：整库 / 单条目
# ---------------------------------------------------------------------------


@router.post(
    "/{library_id}/metadata/refresh",
    response_model=ApiResponse[dict],
    summary="整库刷新元数据：全部已识别条目重新刮削（可恢复后台作业）",
    operation_id="library.metadata.refresh-library",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-job": {"id_path": "job_id", "wait_op": "jobs.wait"}},
    status_code=202,
)
async def start_metadata_refresh(
    library_id: int,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """遍历库内全部已识别条目逐个重刮 TMDB（文本全量覆盖、图片缺失补下）。
    只写元数据表，与扫描/整理互不冲突；升级后给存量条目回填档案就靠它。"""

    service = LibraryConfigService(session)
    library = await service.get(library_id)
    created = await media_scrape.enqueue_library_metadata_refresh_job(
        session, library_id, library.name, origin=_job_origin(client_name)
    )
    return ok(
        {"started": True, "job_id": created.job.id, "created": created.created},
        message=(
            f"已开始刷新「{library.name}」的元数据，可在任务中心继续观察"
            if created.created
            else f"「{library.name}」的元数据刷新已在进行中"
        ),
    )


@router.post(
    "/{library_id}/metadata/refresh/stop",
    response_model=ApiResponse[dict],
    summary="停止进行中的整库元数据刷新（已刷完的保留）",
    operation_id="library.metadata.stop-refresh",
    dependencies=[Depends(require_admin)],
)
async def stop_metadata_refresh(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:

    service = LibraryConfigService(session)
    library = await service.get(library_id)
    active = await jobs.list_jobs(
        session,
        active_only=True,
        job_type="library.metadata.refresh",
        resource_type="library",
        resource_id=library_id,
        limit=1,
    )
    if active:
        await jobs.request_cancel(session, active[0].id, requested_by="媒体库页面")
    elif not media_scrape.request_stop_library_refresh(library_id):
        raise ConflictException(f"「{library.name}」当前没有进行中的元数据刷新")
    return ok({}, message=f"正在停止「{library.name}」的元数据刷新（当前条目刷完即停下）")


@router.get(
    "/{library_id}/metadata/refresh/progress",
    response_model=ApiResponse[MetadataRefreshView],
    summary="整库元数据刷新的实时状态（进度 + 正在处理哪几部、各在什么阶段）",
    operation_id="library.metadata.get-refresh-status",
    dependencies=[Depends(require_admin)],
)
async def metadata_refresh_progress(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[MetadataRefreshView]:
    """与库列表里的 metadata_refresh 同一份状态；单库页用它做 2 秒级的
    阶段刷新（库列表 10 秒一轮的节奏跟不上阶段变化）。"""
    await LibraryConfigService(session).get(library_id)  # 404 检查
    legacy = _metadata_refresh_view(library_id)
    if legacy is not None:
        return ok(legacy)
    latest = await jobs.latest_job_for_resource(
        session, "library", library_id, job_type="library.metadata.refresh"
    )
    if latest is None or latest.status not in ACTIVE_JOB_STATUSES:
        return ok(MetadataRefreshView(refreshing=False))
    progress = latest.progress or {}
    details = progress.get("details") if isinstance(progress.get("details"), dict) else {}
    return ok(
        MetadataRefreshView(
            refreshing=True,
            processed=int(progress.get("current") or 0),
            total=int(progress.get("total") or 0),
            failed=int(details.get("failed") or 0),
            stopping=latest.status is JobStatus.CANCELLING,
            active=[],
        )
    )


@router.post(
    "/{library_id}/items/{media_item_id}/metadata/refresh",
    response_model=ApiResponse[dict],
    summary="刷新单个条目的元数据（强制重刮 TMDB，可恢复后台作业）",
    operation_id="library.items.refresh-metadata",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-job": {"id_path": "job_id", "wait_op": "jobs.wait"}},
    status_code=202,
)
async def refresh_item_metadata(
    library_id: int,
    media_item_id: int,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:

    await LibraryConfigService(session).get(library_id)  # 404 检查
    item, _rows = await _item_rows(session, library_id, media_item_id)  # 404 检查
    created = await media_scrape.enqueue_item_metadata_refresh_job(
        session,
        library_id=library_id,
        media_item_id=media_item_id,
        title=item.title,
        origin=_job_origin(client_name),
    )
    return ok(
        {"started": True, "job_id": created.job.id, "created": created.created},
        message=(
            f"已开始刷新《{item.title}》的元数据，可在任务中心继续观察"
            if created.created
            else f"《{item.title}》的元数据刷新已在进行中"
        ),
    )


@router.get(
    "/{library_id}/items/{media_item_id}/artwork/candidates",
    response_model=ApiResponse[ArtworkCandidatesView],
    summary="条目的候选海报/背景图列表（选图前先看这里）",
    operation_id="library.artwork.list-candidates",
    dependencies=[Depends(require_admin)],
)
async def list_artwork_candidates_route(
    library_id: int,
    media_item_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ArtworkCandidatesView]:
    """TMDB 全量候选图，按与自动选图一致的规则排序（背景无文字优先、
    海报中文优先），首张即当前自动策略会选的那张。"""

    await LibraryConfigService(session).get(library_id)  # 404 检查
    await _item_rows(session, library_id, media_item_id)  # 404 检查
    (
        posters,
        backdrops,
        current_poster,
        current_backdrop,
    ) = await media_scrape.list_artwork_candidates(media_item_id)
    meta = await MediaItemRepository(session).get_metadata(media_item_id)
    return ok(
        ArtworkCandidatesView(
            posters=[ArtworkCandidateView(**p) for p in posters],
            backdrops=[ArtworkCandidateView(**b) for b in backdrops],
            current_poster=current_poster,
            current_backdrop=current_backdrop,
            poster_locked=bool(meta and meta.poster_locked),
            backdrop_locked=bool(meta and meta.backdrop_locked),
        )
    )


@router.post(
    "/{library_id}/items/{media_item_id}/artwork/select",
    response_model=ApiResponse[dict],
    summary="选定海报/背景（当场落盘并覆盖媒体目录；此后刷新不再覆盖）",
    operation_id="library.artwork.select",
    dependencies=[Depends(require_admin)],
)
async def select_artwork_route(
    library_id: int,
    media_item_id: int,
    payload: ArtworkSelectPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """手动选图即加锁：自动策略与 force 刷新都不会再动这张图。
    ``file_path=null`` 解锁恢复自动选图。同步执行（单张图，秒级）。"""

    await LibraryConfigService(session).get(library_id)  # 404 检查
    await _item_rows(session, library_id, media_item_id)  # 404 检查
    await media_scrape.select_artwork(media_item_id, kind=payload.kind, file_path=payload.file_path)
    label = "海报" if payload.kind == "poster" else "背景图"
    message = (
        f"已恢复{label}的自动选图（下次刷新元数据时重新挑选）"
        if payload.file_path is None
        else f"{label}已更换，此后刷新不会覆盖"
    )
    return ok({"locked": payload.file_path is not None}, message=message)


# ---------------------------------------------------------------------------
# 整理（存量规范化）：预览 / 执行
# ---------------------------------------------------------------------------


@router.post(
    "/{library_id}/file-organization-preview",
    response_model=ApiResponse[OrganizePreviewView],
    summary="预览整理计划：每个文件改成什么名、哪些跳过及原因（只读，不动磁盘）",
    operation_id="workflow.library.organize-files.preview",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-hidden": True},
)
async def preview_organize(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[OrganizePreviewView]:
    """按刮削结果计算规范命名计划。纯只读——真正执行前用户在前端逐条
    确认；执行接口会重新计算计划，预览与执行之间的磁盘变化不会造成误改。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    plan = await build_organize_plan(session, library)
    return ok(
        OrganizePreviewView(
            total=plan.total,
            already_ok=plan.already_ok,
            renames=[
                OrganizeRenameView(
                    file_id=a.file_id,
                    media_item_id=a.media_item_id,
                    title=a.title,
                    year=a.year,
                    source_path=a.source_path,
                    target_path=a.target_path,
                    source_rel=a.source_rel,
                    target_rel=a.target_rel,
                    size_bytes=a.size_bytes,
                    sidecars=[
                        OrganizeSidecarView(source_path=s.source_path, target_path=s.target_path)
                        for s in a.sidecars
                    ],
                )
                for a in plan.renames
            ],
            skips=[OrganizeSkipView(file_path=s.file_path, reason=s.reason) for s in plan.skips],
            entry_assets=[
                OrganizeSidecarView(source_path=a.source_path, target_path=a.target_path)
                for a in plan.entry_assets
            ],
        )
    )


@router.post(
    "/{library_id}/file-organizations",
    response_model=ApiResponse[OrganizeStartView],
    summary="开始整理：按规范命名批量改名归位（可恢复后台作业）",
    operation_id="workflow.library.organize-files.start",
    dependencies=[Depends(require_admin)],
    openapi_extra={
        "x-cli-job": {"id_path": "job_id", "wait_op": "jobs.wait"},
        "x-cli-hidden": True,
    },
    status_code=202,
)
async def start_organize(
    library_id: int,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[OrganizeStartView]:
    """执行时重新计算计划并逐文件「改名 → 台账随迁」。改名直接发生在
    磁盘上、无法一键撤销——调用方必须先用预览接口确认影响面再调用。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    if is_organizing(library_id):
        raise ConflictException(f"「{library.name}」正在整理中，请等待完成")
    phase = busy_phase(library_id)
    if phase is not None:
        raise ConflictException(f"「{library.name}」{PHASE_LABELS[phase]}，请等待完成后再整理")
    # 用户确认后再按磁盘现场重算一次，并把这一份精确计划写进 Job。这样
    # 重启发生在“改名成功、台账提交前”时，处理器知道该补哪一行台账。
    plan = await build_organize_plan(session, library)
    created = await enqueue_organize_job(session, library, plan, origin=_job_origin(client_name))
    message = (
        f"已开始整理「{library.name}」，可在任务中心继续观察"
        if created.created
        else f"「{library.name}」的整理作业已在进行中"
    )
    return ok(
        OrganizeStartView(
            started=True,
            message=message,
            job_id=created.job.id,
            created=created.created,
        ),
        message=message,
    )


@router.get(
    "/{library_id}/items",
    response_model=ApiResponse[list[LibraryItemView]],
    summary="库内媒体条目的库存聚合（单库海报墙数据源）",
    operation_id="library.items.list",
    dependencies=[Depends(require_library_visible)],
)
async def list_library_items(
    library_id: int,
    # 这三个参数用 Annotated 写法（而非 `= Query(...)`）：函数被直接调用时
    # 拿到的是真实默认值而不是 Query 对象——测试与内部调用都走这条路
    sort: Annotated[
        Literal["title", "added_at", "probing"],
        Query(description="排序：title=按标题 / added_at=最近入账优先 / probing=待补探优先"),
    ] = "title",
    limit: Annotated[
        int | None, Query(ge=1, le=200, description="本页条目数；不给则返回整库")
    ] = None,
    offset: Annotated[int, Query(ge=0, description="跳过的条目数（滚动加载翻页用）")] = 0,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[LibraryItemView]]:

    await LibraryConfigService(session).get(library_id)  # 404 检查
    return ok(await build_library_wall(session, library_id, sort=sort, limit=limit, offset=offset))


@router.get(
    "/{library_id}/item-ids",
    response_model=ApiResponse[list[int]],
    summary="库内条目 id 集合（前端判定「已入库」用）",
    operation_id="ui.library.items.ids",
    openapi_extra={"x-cli-hidden": True},
    dependencies=[Depends(require_library_visible)],
)
async def list_library_item_ids(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[int]]:
    """只回 id 的轻量清单：海报墙分页后，前端仍需知道「哪些条目已在库」——
    单库页据此把已入库的订阅从「追踪中」里剔掉。一列整型，几千条也就几十 KB。"""

    await LibraryConfigService(session).get(library_id)  # 404 检查
    rows = await session.execute(
        select(LibraryFile.media_item_id)
        .where(
            LibraryFile.library_id == library_id,
            LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
        )
        .distinct()
    )
    return ok([i for i in rows.scalars().all() if i is not None])


@router.get(
    "/{library_id}/item-index",
    response_model=ApiResponse[list[LibraryIndexEntryView]],
    summary="海报墙的 A-Z 首字母索引（按标题排序下的分档与起始位置）",
    operation_id="ui.library.items.index",
    openapi_extra={"x-cli-hidden": True},
    dependencies=[Depends(require_library_visible)],
)
async def list_library_item_index(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[LibraryIndexEntryView]]:
    """索引条数据：每档的条目数与起始 offset，只回非空档。

    与 ``/items?sort=title`` 共用同一份拼音排序，因此 offset 直接可用——
    前端点「S」就是拉 ``?sort=title&offset=<该档 offset>``。
    """

    await LibraryConfigService(session).get(library_id)  # 404 检查
    buckets = await build_library_index(session, library_id)
    return ok(
        [
            LibraryIndexEntryView(initial=initial, count=count, offset=offset)
            for initial, count, offset in buckets
        ]
    )


# ---------------------------------------------------------------------------
# 条目详情页：详情 / 本地美术图 / 真实删除 / 重新识别
# ---------------------------------------------------------------------------


def _trash_note(row: LibraryFile) -> str | None:
    """待回收行的展示句：审计快照的 note + 触发方 label（都是快照，免 join）。"""
    if row.state != FileState.TRASHED or not row.trash_context:
        return None
    note = str(row.trash_context.get("note") or "")
    trigger = row.trash_context.get("trigger") or {}
    label = str(trigger.get("label") or "")
    if note and label:
        return f"{note}（{label}）"
    return note or label or None


async def _item_rows(
    session: AsyncSession, library_id: int, media_item_id: int
) -> tuple[MediaItem, list[LibraryFile]]:
    """条目 + 它在该库的全部台账行；条目不存在或不在库中抛 404。"""
    item = await session.get(MediaItem, media_item_id)
    if item is None:
        raise NotFoundException("媒体条目不存在（可能已被删除）")
    rows = list(
        (
            await session.execute(
                select(LibraryFile)
                .where(
                    LibraryFile.library_id == library_id,
                    LibraryFile.media_item_id == media_item_id,
                )
                .order_by(LibraryFile.season_number, LibraryFile.episode_number, LibraryFile.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise NotFoundException(f"「{item.title}」在该媒体库中没有库存文件")
    return item, rows


def _file_view(row: LibraryFile, external_subs: list[str]) -> LibraryFileView:
    """台账行 → 详情页文件视图：内封字幕轨与外挂字幕文件合并成一份清单。"""
    subtitles = [
        SubtitleStreamView(
            codec=stream.get("codec"),
            language=stream.get("language"),
            title=stream.get("title"),
            forced=bool(stream.get("forced")),
            default=bool(stream.get("default")),
        )
        for stream in (row.subtitle_streams or [])
    ]
    stem = PurePath(row.file_path).stem
    for name in external_subs:
        extra = match_subtitle_filename(stem, name)
        parsed = parse_subtitle_tokens(extra or "")
        subtitles.append(
            SubtitleStreamView(
                codec=PurePath(name).suffix.lstrip(".").lower() or None,
                language=parsed["language"],
                title=parsed["title"],
                forced=parsed["forced"],
                default=parsed["default"],
                external=True,
                file_name=name,
            )
        )
    return LibraryFileView(
        id=row.id,  # type: ignore[arg-type]  # 落库后必有主键
        file_path=row.file_path,
        file_name=PurePath(row.file_path).name,
        size_bytes=row.size_bytes,
        container=row.container,
        resolution=row.resolution,
        video_codec=row.video_codec,
        hdr=row.hdr,
        bit_depth=row.bit_depth,
        duration_seconds=row.duration_seconds,
        bit_rate=row.bit_rate,
        frame_rate=row.frame_rate,
        color_space=row.color_space,
        media_source=row.media_source,
        media_source_manual=row.media_source_manual,
        release_group=row.release_group,
        source=row.source,
        season_number=row.season_number,
        episode_number=row.episode_number,
        missing=row.state == FileState.MISSING,
        state=row.state,
        purge_after=row.purge_after,
        trash_note=_trash_note(row),
        audio_streams=(
            None
            if row.audio_streams is None
            else [
                AudioStreamView(
                    codec=stream.get("codec"),
                    profile=stream.get("profile"),
                    channels=stream.get("channels"),
                    channel_layout=stream.get("channel_layout"),
                    language=stream.get("language"),
                    title=stream.get("title"),
                    default=bool(stream.get("default")),
                )
                for stream in row.audio_streams
            ]
        ),
        subtitle_streams=subtitles,
        added_at=row.created_at,
    )


@router.get(
    "/{library_id}/items/{media_item_id}",
    response_model=ApiResponse[LibraryItemDetailView],
    summary="条目详情：基本信息 + NFO 本地刮削元数据 + 逐文件真实介质规格",
    operation_id="library.items.get",
    dependencies=[Depends(require_library_visible)],
)
async def get_library_item(
    library_id: int,
    media_item_id: int,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LibraryItemDetailView]:
    """媒体库条目详情页的数据源。规格来自 ffprobe 对文件本体的探测，
    简介/评分/演职员本地 NFO 优先、TMDB 兜底（经持久缓存）。没探测过
    音轨的行**不在此补探**——探测只发生在入库/扫描环节（浏览不碰媒体
    文件本体，云盘挂载上读文件就是流量与延迟），前端对"尚未探测"的
    文件提示用户重新扫描补齐。"""

    service = LibraryConfigService(session)
    library = await service.get(library_id)
    item, rows = await _item_rows(session, library_id, media_item_id)
    # 起播预热：用户在详情页看简介的这几秒，正好把关键帧采样与默认字幕
    # 抽掉——点播放时缓存直接命中，首播不再现场探测（§6.10）。后台任务，
    # 失败无感；剧集（文件多）在 warmup 内部自动跳过。
    playback_warmup.schedule(
        media_item_id, [row for row in rows if row.state == FileState.IN_PLACE]
    )
    bundle = await build_item_detail(session, library, item, rows)

    base = get_settings().tmdb_image_base_url.rstrip("/")
    art_base = f"/libraries/{library_id}/items/{media_item_id}/artwork"
    # 图片优先级与元数据同构：条目目录美术图 > 本地刮削资产 > TMDB 图床。
    # 本地两层的 URL 都带 ?v=<mtime> 版本戳：换图是**原地覆盖同一路径**，
    # 不带版本浏览器会拿缓存里的旧图，用户看到"换了没生效"（实测踩过）
    meta_row = await MediaItemRepository(session).get_metadata(media_item_id)
    if bundle.has_local_poster:
        poster_url = f"{art_base}?kind=poster&v={bundle.local_poster_version}"
    elif meta_row is not None and meta_row.poster_file:
        poster_version = media_scrape.asset_version(meta_row.poster_file)
        poster_url = f"/images/assets/{meta_row.poster_file}?v={poster_version}"
    else:
        poster_url = f"{base}/w500{item.poster_path}" if item.poster_path else None
    if bundle.has_local_fanart:
        backdrop_url = f"{art_base}?kind=fanart&v={bundle.local_fanart_version}"
    elif meta_row is not None and meta_row.backdrop_file:
        backdrop_version = media_scrape.asset_version(meta_row.backdrop_file)
        backdrop_url = f"/images/assets/{meta_row.backdrop_file}?v={backdrop_version}"
    else:
        # w1280 而非 original：作为全站沉浸背景铺视口足够清晰，体积小一个
        # 数量级——首次访问的背景切换等待从"原图下载"变成秒级
        backdrop_url = f"{base}/w1280{item.backdrop_path}" if item.backdrop_path else None
    local_meta = None
    if bundle.local_meta is not None:
        # Web 与 Jellyfin 共用 person 关系表：导演头像和人物链接不能再从
        # directors: string[] 猜。旧条目尚未刷新、没有关系行时保留姓名兜底。
        director_rows = (
            await session.execute(
                select(MediaItemPerson, Person)
                .join(Person, Person.id == MediaItemPerson.person_id)
                .where(
                    MediaItemPerson.media_item_id == media_item_id,
                    MediaItemPerson.department == "director",
                )
                .order_by(MediaItemPerson.credit_order, MediaItemPerson.id)
            )
        ).all()
        local_meta = LocalMetaView(
            plot=bundle.local_meta.plot,
            rating=bundle.local_meta.rating,
            runtime_minutes=bundle.local_meta.runtime_minutes,
            genres=bundle.local_meta.genres,
            directors=bundle.local_meta.directors,
            director_credits=[
                DirectorView(
                    name=person.name,
                    thumb_url=(
                        f"{base}/w300{person.profile_path}" if person.profile_path else None
                    ),
                    tmdb_person_id=person.tmdb_person_id,
                )
                for _link, person in director_rows
            ],
            actors=[
                ActorView(
                    name=a.name, role=a.role, thumb_url=a.thumb, tmdb_person_id=a.tmdb_person_id
                )
                for a in bundle.local_meta.actors
            ],
            nfo_name=bundle.local_meta.nfo_name,
            source=bundle.local_meta.source,
        )
    seasons: list[int] = []
    if item.kind == "tv":
        assert item.id is not None
        meta_seasons = (
            (
                await session.execute(
                    select(MediaSeason.season_number).where(MediaSeason.media_item_id == item.id)
                )
            )
            .scalars()
            .all()
        )
        owned_seasons = {row.season_number for row in rows}
        # 特别季 0 只在库里真有文件时出现（订阅口径也不追特别季）
        seasons = sorted({s for s in meta_seasons if s > 0} | owned_seasons)

    assert item.id is not None
    file_views = [_file_view(row, bundle.external_subtitles.get(row.id or -1, [])) for row in rows]
    entry_dirs = bundle.entry_dirs
    if not principal.is_admin:
        # 成员不暴露落盘路径：文件行只留文件名与规格，条目目录整个不给
        for fv in file_views:
            fv.file_path = ""
        entry_dirs = []
    return ok(
        LibraryItemDetailView(
            media_item_id=item.id,
            kind=MediaKind(item.kind),
            tmdb_id=item.tmdb_id,
            imdb_id=item.imdb_id,
            douban_id=item.douban_id,
            title=item.title,
            original_title=item.original_title,
            year=item.year,
            poster_url=poster_url,
            backdrop_url=backdrop_url,
            local_meta=local_meta,
            entry_dirs=entry_dirs,
            files=file_views,
            file_count=len(rows),
            total_size_bytes=sum(row.size_bytes for row in rows),
            seasons=seasons,
            # 刮削状态服务端说了算：前端据此在任意时刻（含刚打开页面）
            # 知道这部片还在刮、正在做什么，不依赖发起刷新的那个标签页还开着
            scraping=media_scrape.is_scraping(media_item_id),
            scraping_phase=media_scrape.scraping_phase(media_item_id),
        )
    )


@router.get(
    "/{library_id}/items/{media_item_id}/episodes",
    response_model=ApiResponse[SeasonEpisodesView],
    summary="剧集条目一季的分集清单（集名/简介/剧照 + 拥有状态，分集横滚区数据源）",
    operation_id="library.items.list-episodes",
    dependencies=[Depends(require_library_visible)],
)
async def list_item_episodes(
    library_id: int,
    media_item_id: int,
    season_number: int = Query(ge=0, description="季号（0=特别篇）"),
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SeasonEpisodesView]:
    """并集"元数据里的集 ∪ 库里实有的集"：缺集也列出（owned=false 置灰）。
    集名/日期来自库内季集结构，简介/剧照本地分集刮削优先、TMDB 分季兜底。
    随集带回当前观看者的进度（进度条/已看对勾的数据源）。"""
    service = LibraryConfigService(session)
    await service.get(library_id)  # 404 检查
    item, rows = await _item_rows(session, library_id, media_item_id)
    episodes = await build_season_episodes(
        session,
        item,
        rows,
        season_number,
        member_id=principal.member_id if principal.member_id is not None else 0,
    )
    return ok(
        SeasonEpisodesView(
            season_number=season_number,
            episodes=[episode_view(e) for e in episodes],
        )
    )


@router.get(
    "/files/{file_id}/thumb",
    response_class=FileResponse,
    summary="分集本地缩略图（视频同名 -thumb.jpg，Kodi 惯例）",
    operation_id="ui.library.files.thumb",
    openapi_extra={"x-cli-hidden": True},
)
async def get_file_thumb(
    file_id: int,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """路径由台账行推导（客户端只给 id），不存在路径注入面。"""
    row = await session.get(LibraryFile, file_id)
    if row is None:
        raise NotFoundException("台账文件不存在")
    # 成员按文件归属库做可见性判定（不可见与不存在同样 404）
    if row.library_id is not None:
        await assert_library_visible(session, principal, row.library_id)
    thumb = find_episode_thumb(Path(row.file_path))
    if thumb is None:
        raise NotFoundException("该文件没有本地缩略图")
    return FileResponse(thumb, headers={"Cache-Control": "private, max-age=3600"})


@router.get(
    "/files/{file_id}/subtitles/preview",
    response_model=ApiResponse[SubtitlePreviewView],
    summary="预览一条外挂或文本内封字幕的时间轴内容",
    operation_id="ui.library.files.preview-subtitles",
    openapi_extra={"x-cli-hidden": True},
)
async def preview_file_subtitle(
    file_id: int,
    track: str = Query(description="中性轨引用：embedded:<序号> / external:<文件名>"),
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubtitlePreviewView]:
    """用户主动点击字幕徽章时才读取文件本体。

    轨引用必须命中该台账行：外挂文件名不能越过台账白名单，内封流序号
    不能越界；成员仍按文件所属库执行可见性判定。文本内封轨首次预览会
    调用 ffmpeg 抽取，结果沿用字幕生成缓存，后续打开无需重复通读视频。
    """

    row = await session.get(LibraryFile, file_id)
    if row is None:
        raise NotFoundException("台账文件不存在")
    await assert_library_visible(session, principal, row.library_id)
    try:
        preview = await load_subtitle_preview(row, track)
    except SubtitleTrackNotFound as exc:
        raise NotFoundException(str(exc)) from exc
    except SubtitlePreviewError as exc:
        raise BadRequestException(str(exc)) from exc

    cues = [
        SubtitleCueView(start_ms=start, end_ms=end, text=text)
        for start, end, text in preview.events
    ]
    return ok(
        SubtitlePreviewView(
            track=track,
            format=preview.format,
            event_count=len(cues),
            cues=cues,
        )
    )


@router.delete(
    "/files/{file_id}/subtitles",
    response_model=ApiResponse[SubtitleDeleteResultView],
    summary="删除该文件的一个外挂字幕（含 AI 生成的字幕；内封轨在容器内，不可删）",
    operation_id="library.subtitles.delete",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def delete_file_subtitle(
    file_id: int,
    filename: str = Query(description="外挂字幕文件名（不含目录），必须属于该视频"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SubtitleDeleteResultView]:
    """真删磁盘上的字幕文件（不进回收），调用方必须先把将删除的文件列给
    用户并取得本轮明确同意——Web 端由确认框承担，CLI 由 --yes 承担。

    只对外挂字幕开放：内封轨要删得重封装视频文件，不属于本接口的语义。
    """
    row = await session.get(LibraryFile, file_id)
    if row is None:
        raise NotFoundException("台账文件不存在")
    result = await delete_external_subtitle(session, row, filename)
    return ok(
        SubtitleDeleteResultView(path=result.path, freed_bytes=result.freed_bytes),
        message=f"已删除字幕文件：{filename}",
    )


@router.get(
    "/{library_id}/items/{media_item_id}/artwork",
    response_class=FileResponse,
    summary="条目目录里的本地美术图（poster/fanart，Kodi/Emby 命名惯例）",
    operation_id="library.artwork.download",
    dependencies=[Depends(require_library_visible)],
)
async def get_item_artwork(
    library_id: int,
    media_item_id: int,
    kind: Literal["poster", "fanart"] = Query(
        default="poster", description="poster=海报 / fanart=背景图"
    ),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """路径完全由服务端从台账推导（客户端只给 id），不存在路径注入面。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    _, rows = await _item_rows(session, library_id, media_item_id)
    roots = [Path(p) for p in library.root_paths]
    art = await asyncio.to_thread(local_item_artwork, roots, rows, kind)
    if art is not None:
        # 本地文件可能被用户替换，给短缓存而非 immutable
        return FileResponse(art, headers={"Cache-Control": "private, max-age=3600"})
    raise NotFoundException("条目目录里没有本地美术图")


@router.delete(
    "/{library_id}/items/{media_item_id}",
    response_model=ApiResponse[ItemDeleteResultView],
    summary="从磁盘彻底删除条目（整个刮削目录：视频+NFO+海报+字幕一起清除）",
    operation_id="library.items.delete",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def delete_library_item(
    library_id: int,
    media_item_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ItemDeleteResultView]:
    """全站唯一会删磁盘文件的接口——与「忽略/清理记录」（只动台账）截然
    不同，调用方必须先向用户明确确认再调用（CLI 已强制 --yes）。删除失败的文件保留台账行。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    await _assert_not_busy(session, library.name, library_id)
    item, rows = await _item_rows(session, library_id, media_item_id)
    all_rows = await LibraryFileRepository(session).list_by_library(library_id)
    result = await delete_item_files(session, library, media_item_id, rows, all_rows)

    # 通知下游媒体服务器刷新库（未配置时空转；失败只告警不阻断）

    background_tasks.add_task(notify_media_server_refresh)
    # 条目在所有库都没文件了、也没订阅 → 连同图片资产一并清掉，不留孤儿

    background_tasks.add_task(media_scrape.cleanup_orphan_items, [media_item_id])

    view = ItemDeleteResultView(
        removed_paths=result.removed_paths,
        rows_deleted=result.rows_deleted,
        freed_bytes=result.freed_bytes,
        errors=result.errors,
    )
    if result.errors:
        message = f"「{item.title}」部分删除失败：{'；'.join(result.errors)}"
    else:
        message = f"「{item.title}」已从磁盘彻底删除"
    return ok(view, message=message)


@router.delete(
    "/{library_id}/items/{media_item_id}/files/{file_id}",
    response_model=ApiResponse[ItemDeleteResultView],
    summary="从磁盘删除条目的单个文件（含同名 NFO/字幕/图片附属文件）",
    operation_id="library.items.delete-file",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def delete_library_file(
    library_id: int,
    media_item_id: int,
    file_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ItemDeleteResultView]:
    """条目删除的文件级姊妹（多版本洗掉一个 / 删某集重下）——同样会真删
    磁盘，调用方必须先向用户明确确认。该文件是条目在本库的最后一个文件时
    升级为整条目删除（不留只剩 NFO/海报的空刮削目录），确认界面须告知。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    await _assert_not_busy(session, library.name, library_id)
    item, rows = await _item_rows(session, library_id, media_item_id)
    row = next((r for r in rows if r.id == file_id), None)
    if row is None:
        raise NotFoundException(f"台账文件不存在或不属于「{item.title}」：id={file_id}")
    file_name = PurePath(row.file_path).name
    all_rows = await LibraryFileRepository(session).list_by_library(library_id)
    result = await delete_single_file(session, library, row, rows, all_rows)

    # 与整条目删除同一套善后：通知媒体服务器刷新；条目在所有库都没文件了
    # 且没订阅时连同图片资产一并清掉
    background_tasks.add_task(notify_media_server_refresh)
    background_tasks.add_task(media_scrape.cleanup_orphan_items, [media_item_id])

    view = ItemDeleteResultView(
        removed_paths=result.removed_paths,
        rows_deleted=result.rows_deleted,
        freed_bytes=result.freed_bytes,
        errors=result.errors,
    )
    if result.errors:
        message = f"「{file_name}」删除失败：{'；'.join(result.errors)}"
    elif len(rows) == 1:
        message = f"「{item.title}」的最后一个文件已删除，整个条目已从磁盘清除"
    else:
        message = f"「{file_name}」已从磁盘删除"
    return ok(view, message=message)


@router.post(
    "/{library_id}/items/{media_item_id}/files/{file_id}/restore",
    response_model=ApiResponse[dict],
    summary="把待回收的文件恢复为在位版本",
    operation_id="library.items.restore-file",
    dependencies=[Depends(require_admin)],
)
async def restore_library_file(
    library_id: int,
    media_item_id: int,
    file_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """回收机制的「恢复」（library-file-recycle.md §7）：文件移回原路径、
    状态复位在位；原路径已被占用（如新版本同名）时恢复失败并保持待回收。"""
    from movieclaw_api.services.library.recycle import restore_file

    item, rows = await _item_rows(session, library_id, media_item_id)
    row = next((r for r in rows if r.id == file_id), None)
    if row is None:
        raise NotFoundException(f"台账文件不存在或不属于「{item.title}」：id={file_id}")
    if row.state != FileState.TRASHED:
        raise BadRequestException("该文件不在待回收状态，无需恢复")
    if not await restore_file(session, row):
        raise ConflictException("恢复失败：原路径已有同名文件，或文件已不存在——可稍后清理该记录")
    await session.commit()
    return ok({}, message=f"「{PurePath(row.file_path).name}」已恢复为在位版本")


@router.post(
    "/{library_id}/items/{media_item_id}/files/{file_id}/purge",
    response_model=ApiResponse[dict],
    summary="立即清理一个待回收的文件（真删磁盘）",
    operation_id="library.items.purge-file",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def purge_library_file(
    library_id: int,
    media_item_id: int,
    file_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """回收机制的「立即清理」：不等保留期，删物理文件 + 删台账行。
    做种保护形态（原地待回收）的清理可能中断做种——确认弹窗由前端负责，
    调用方必须先向用户明确确认。"""
    from movieclaw_api.services.library.recycle import purge_file

    item, rows = await _item_rows(session, library_id, media_item_id)
    row = next((r for r in rows if r.id == file_id), None)
    if row is None:
        raise NotFoundException(f"台账文件不存在或不属于「{item.title}」：id={file_id}")
    if row.state != FileState.TRASHED:
        raise BadRequestException("该文件不在待回收状态；删除在位文件请用「删除文件」")
    file_name = PurePath(row.file_path).name
    if not await purge_file(session, row):
        raise ConflictException(
            f"清理「{file_name}」失败：目录内还有其他在案文件（需先处理），或文件权限不足——详见服务日志"
        )
    await session.commit()
    return ok({}, message=f"「{file_name}」已清理")


@router.post(
    "/{library_id}/items/{media_item_id}/reidentification-preview",
    response_model=ApiResponse[ReidentifyPreviewView],
    summary="修正识别结果（预览）：重走识别链只出结论，不改台账",
    operation_id="library.items.preview-reidentification",
    dependencies=[Depends(require_admin)],
)
async def preview_reidentify_item(
    library_id: int,
    media_item_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ReidentifyPreviewView]:
    """界面上「修正识别结果」的第一阶段：重跑识别链，把结论摆给用户看。

    识别错挂时机器往往是**高置信地错**——同一条链重跑大概率复现同一个错
    答案，所以这里只出结论、**一行台账都不改**，由用户拍板：采纳某组结论
    （走 ``library.identification.assign-files-to-title``）、自己搜一个条目、或标为
    非独立作品（走 ``library.identification.mark-files-as-extras``）。关掉面板则
    台账零改动。

    只读，因此不占库级锁、扫描进行中也能开——结论过时了大不了拍板时
    按那一刻的台账走。
    """
    library = await LibraryConfigService(session).get(library_id)
    item, rows = await _item_rows(session, library_id, media_item_id)
    preview = await preview_reidentify(session, library, media_item_id, rows)

    base = get_settings().tmdb_image_base_url.rstrip("/")

    def poster(path: str | None) -> str | None:
        return f"{base}/w185{path}" if path else None

    view = ReidentifyPreviewView(
        current=ReviewItemView(
            media_item_id=media_item_id,
            tmdb_id=item.tmdb_id,
            title=item.title,
            year=item.year,
            poster_url=poster(item.poster_path),
        ),
        movie=preview.movie,
        groups=[
            ReidentifyGroupView(
                key=group.key,
                outcome=ReidentifyOutcomeView(
                    media_item_id=group.outcome.media_item_id,
                    tmdb_id=group.outcome.tmdb_id,
                    title=group.outcome.title,
                    year=group.outcome.year,
                    poster_url=poster(group.outcome.poster_path),
                    source=group.outcome.source,
                    same_as_current=group.outcome.media_item_id == media_item_id,
                    reason=group.outcome.reason,
                    code=group.outcome.code,
                    candidates=[UnidentifiedCandidateView(**c) for c in group.outcome.candidates],
                ),
                file_ids=group.file_ids,
                file_count=group.file_count,
                total_size_bytes=group.total_size_bytes,
                sample_names=group.sample_names,
            )
            for group in preview.groups
        ],
        skipped_missing=preview.skipped_missing,
        pinned_identity=preview.pinned_identity,
        unreachable=preview.unreachable,
        search_seed=preview.search_seed,
    )
    return ok(view)


@router.post(
    "/{library_id}/items/{media_item_id}/reidentifications",
    response_model=ApiResponse[ReidentifyResultView],
    summary="重新识别条目：全部在位文件重走识别链（NFO → 名称解析 → TMDB 收敛）",
    operation_id="library.items.reidentify",
    dependencies=[Depends(require_admin)],
)
async def reidentify_library_item(
    library_id: int,
    media_item_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ReidentifyResultView]:
    """识别器随版本升级在变强，错挂的条目由此翻案。同步执行（单条目
    只有少量 TMDB 查询，秒级返回），结果当场回给用户。"""
    service = LibraryConfigService(session)
    library = await service.get(library_id)
    await _assert_not_busy(session, library.name, library_id)
    await _item_rows(session, library_id, media_item_id)  # 404 检查

    summary = await reidentify_item(library_id, media_item_id)
    if summary.errors:
        raise ConflictException("；".join(summary.errors))

    if summary.kept_on_error == summary.total and summary.total > 0:
        message = "TMDB 查询失败（网络不通或接口异常），已保留当前身份；修复网络后可再试"
    elif summary.changed and summary.new_media_item_id is not None:
        message = f"已重新识别为「{summary.new_title}」"
    elif summary.changed:
        message = "文件被识别为多个不同条目，请回库存页查看"
    elif summary.identified == summary.total and summary.total > 0:
        message = "重新识别完成：结果与当前一致"
        if summary.pinned_identity:
            message += (
                "（身份由目录名的 tmdbid 标记或 NFO 指定——若确属错挂，"
                "请先修改/删除该标记或 NFO 后重试，或人工认领）"
            )
    else:
        message = "未能识别，文件已进入「待识别」清单，可人工认领"
    if summary.unidentified and summary.identified:
        message += f"；{summary.unidentified} 个文件未识别，已进入待识别清单"
    if summary.kept_on_error and summary.kept_on_error != summary.total:
        message += f"；{summary.kept_on_error} 个文件因 TMDB 查询失败保留原身份"
    return ok(
        ReidentifyResultView(
            total=summary.total,
            identified=summary.identified,
            unidentified=summary.unidentified,
            skipped_missing=summary.skipped_missing,
            kept_on_error=summary.kept_on_error,
            changed=summary.changed,
            new_media_item_id=summary.new_media_item_id,
            new_title=summary.new_title,
            pinned_identity=summary.pinned_identity,
            message=message,
        ),
        message=message,
    )


@router.get(
    # 两段路径：单段会被更早注册的 GET /{library_id} 先匹配（路径参数 422）
    "/media-source-annotations/candidates",
    response_model=ApiResponse[list[MediaSourceAnnotationCandidateView]],
    summary="整季片源标注的预览：列出将被标注的文件（片源未知或既有人工标注）",
    operation_id="library.items.list-media-source-annotation-candidates",
    dependencies=[Depends(require_admin)],
)
async def list_media_source_annotation_candidates(
    media_item_id: int,
    season_number: int = Query(ge=0, description="季号；电影传 0"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[MediaSourceAnnotationCandidateView]]:
    rows = await source_annotation.list_annotation_candidates(
        session, media_item_id=media_item_id, season_number=season_number
    )
    return ok(
        [
            MediaSourceAnnotationCandidateView(
                file_id=row.id,  # type: ignore[arg-type]  # 落库后必有主键
                file_name=PurePath(row.file_path).name,
                episode_number=row.episode_number,
                size_bytes=row.size_bytes,
                media_source=row.media_source,
                media_source_manual=row.media_source_manual,
            )
            for row in rows
        ]
    )


@router.post(
    "/media-source-annotations",
    response_model=ApiResponse[dict],
    summary="整季人工标注片源：把「无法确认」的洗版单元变为可判定",
    operation_id="library.items.annotate-media-source",
    dependencies=[Depends(require_admin)],
)
async def annotate_item_media_source(
    payload: MediaSourceAnnotationPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """docs/design/media-source-annotation.md：只动片源未知与既有人工标注的
    在位文件，并同步刷新对应订阅工单的质量快照出处维度。不触发搜索——
    前端标注成功后重跑一轮 upgrade-runs 完成排期。定位走 body（与
    missing-record-clearances 同风格）：订阅侧入口拿不到库 id，条目 id 已足够。"""
    item = await session.get(MediaItem, payload.media_item_id)
    if item is None:
        raise NotFoundException(f"条目不存在：id={payload.media_item_id}")
    result = await source_annotation.annotate_media_source(
        session,
        media_item_id=payload.media_item_id,
        season_number=payload.season_number,
        media_source=payload.media_source,
    )
    label = "最低档（人工标注）" if payload.media_source == "user-lowest" else payload.media_source
    return ok(
        result,
        message=(
            f"已将 {result['files']} 个文件的片源标注为 {label}"
            f"，并刷新 {result['snapshots']} 个单元的质量快照"
        ),
    )


# ---------------------------------------------------------------------------
# 条目转移：分错库的作品换个库（磁盘目录 + 台账一起搬）
# ---------------------------------------------------------------------------


async def _transfer_context(
    session: AsyncSession, library_id: int, media_item_id: int, target_library_id: int
):
    """转移三接口共用的前置：取源库/目标库/条目/台账行 + 合法性校验。"""
    service = LibraryConfigService(session)
    source = await service.get(library_id)
    target = await service.get(target_library_id)
    assert_transferable(source, target)
    item, rows = await _item_rows(session, library_id, media_item_id)
    return source, target, item, rows


@router.get(
    "/{library_id}/items/{media_item_id}/transfer-preview",
    response_model=ApiResponse[TransferPreviewView],
    summary="预览条目转移：哪些目录/文件搬到目标库的什么位置（只读，不动磁盘）",
    operation_id="library.items.preview-transfer",
    dependencies=[Depends(require_admin)],
)
async def preview_transfer(
    library_id: int,
    media_item_id: int,
    target_library_id: Annotated[int, Query(description="转移目标库 id（须与当前库同类型）")],
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TransferPreviewView]:
    """纯只读：算出条目目录会搬到哪、总体积多大、是否跨盘（跨盘要复制、
    耗时且断硬链）。``blocked`` 非空说明有阻断问题，执行接口会直接拒绝。"""
    source, target, item, rows = await _transfer_context(
        session, library_id, media_item_id, target_library_id
    )
    plan = await build_transfer_plan(session, source, target, item, rows)
    return ok(
        TransferPreviewView(
            target_library_id=target_library_id,
            target_library_name=target.name,
            target_root=target.primary_root or "",
            moves=[
                TransferMoveView(
                    source_path=m.source_path,
                    target_path=m.target_path,
                    is_dir=m.is_dir,
                    size_bytes=m.size_bytes,
                    file_count=len(m.file_ids),
                )
                for m in plan.moves
            ],
            skips=[TransferSkipView(file_path=s.file_path, reason=s.reason) for s in plan.skips],
            total_bytes=plan.total_bytes,
            missing_count=len(plan.missing_file_ids),
            cross_device=plan.cross_device,
            blocked=plan.blocked,
        )
    )


@router.post(
    "/{library_id}/items/{media_item_id}/transfers",
    response_model=ApiResponse[TransferStartView],
    summary="转移条目到另一个媒体库：整个条目目录搬到目标库主根，台账随迁",
    operation_id="library.items.transfer",
    dependencies=[Depends(require_admin)],
    openapi_extra={
        "x-cli-dangerous": "confirm",
        "x-cli-job": {"id_path": "job_id", "wait_op": "jobs.wait"},
    },
    status_code=202,
)
async def transfer_library_item(
    library_id: int,
    media_item_id: int,
    payload: TransferPayload,
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TransferStartView]:
    """分错库的补救通道（如韩剧被路由进了「大陆华语剧」）。执行时**重新
    计算**计划——预览到确认之间磁盘可能已变化，永远以最新状态为准。

    搬运直接发生在磁盘上、无法一键撤销，调用方必须先用预览接口把影响面
    摆给用户确认。后台执行，进度与结论走 transfer/status。"""
    source, target, item, rows = await _transfer_context(
        session, library_id, media_item_id, payload.target_library_id
    )
    plan = await build_transfer_plan(session, source, target, item, rows)
    if plan.blocked:
        raise ConflictException("；".join(plan.blocked))
    if not plan.moves and not plan.missing_file_ids:
        detail = "；".join(s.reason for s in plan.skips) or "台账为空"
        raise BadRequestException(f"「{item.title}」没有可转移的内容：{detail}")
    created = await enqueue_transfer_job(
        session,
        plan,
        target_library_name=target.name,
        origin=_job_origin(client_name),
    )
    message = (
        f"已开始把「{item.title}」转移到「{target.name}」，可在任务中心继续观察"
        if created.created
        else f"「{item.title}」的转移作业已在进行中"
    )
    if plan.cross_device:
        message += "（跨盘转移需要完整复制文件，耗时取决于体积）"
    return ok(
        TransferStartView(
            started=True,
            message=message,
            job_id=created.job.id,
            created=created.created,
        ),
        message=message,
    )


@router.get(
    "/{library_id}/item-transfer-status",
    response_model=ApiResponse[TransferStatusView],
    summary="条目转移的实时进度与最近一次结论",
    operation_id="library.items.get-transfer-status",
    dependencies=[Depends(require_admin)],
)
async def get_transfer_status(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TransferStatusView]:
    """源库与目标库两侧查到的是同一份状态（转移期间两侧都占着任务位），
    前端弹窗轮询这一个接口即可从"进行中"一路走到结论页。"""
    await LibraryConfigService(session).get(library_id)  # 404 检查
    state = transfer_state(library_id)
    if state is not None:
        return ok(
            TransferStatusView(
                running=True,
                media_item_id=state.media_item_id,
                title=state.title,
                target_library_id=state.target_library_id,
                processed=state.processed,
                total=state.total,
            )
        )
    latest = await jobs.latest_job_for_resource(
        session, "library", library_id, job_type="library.transfer"
    )
    if latest is not None:
        progress = latest.progress or {}
        details = progress.get("details") if isinstance(progress.get("details"), dict) else {}
        raw_plan = latest.input_data.get("plan")
        plan = raw_plan if isinstance(raw_plan, dict) else {}
        if latest.status in ACTIVE_JOB_STATUSES:
            return ok(
                TransferStatusView(
                    running=True,
                    media_item_id=int(details.get("media_item_id") or 0) or None,
                    title=str(plan.get("title") or latest.subject or ""),
                    target_library_id=int(details.get("target_library_id") or 0) or None,
                    processed=int(progress.get("current") or 0),
                    total=int(progress.get("total") or 0),
                )
            )
        if latest.result:
            result = latest.result
            return ok(
                TransferStatusView(
                    running=False,
                    media_item_id=int(result.get("media_item_id") or 0) or None,
                    title=str(result.get("title") or latest.subject or ""),
                    target_library_id=int(result.get("target_library_id") or 0) or None,
                    processed=len(result.get("moved_paths") or []),
                    total=len(result.get("moved_paths") or []),
                    finished_at=latest.finished_at,
                    target_library_name=str(result.get("target_library_name") or ""),
                    moved_paths=list(result.get("moved_paths") or []),
                    files_relocated=int(result.get("files_relocated") or 0),
                    bytes_moved=int(result.get("bytes_moved") or 0),
                    removed_dirs=int(result.get("removed_dirs") or 0),
                    subscription_moved=bool(result.get("subscription_moved")),
                    errors=list(result.get("errors") or []),
                )
            )
    last = last_transfer(library_id)
    if last is None:
        return ok(TransferStatusView(running=False))
    finished_at, summary = last
    return ok(
        TransferStatusView(
            running=False,
            media_item_id=summary.media_item_id,
            title=summary.title,
            target_library_id=summary.target_library_id,
            processed=len(summary.moved_paths),
            total=len(summary.moved_paths),
            finished_at=finished_at,
            target_library_name=summary.target_library_name,
            moved_paths=summary.moved_paths,
            files_relocated=summary.files_relocated,
            bytes_moved=summary.bytes_moved,
            removed_dirs=summary.removed_dirs,
            subscription_moved=summary.subscription_moved,
            errors=summary.errors,
        )
    )


@router.get(
    "/{library_id}/missing",
    response_model=ApiResponse[list[MissingItemView]],
    summary="缺失清单：文件已不在磁盘的库存，按条目聚合",
    operation_id="library.missing.list",
    dependencies=[Depends(require_admin)],
)
async def list_missing(
    library_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[MissingItemView]]:

    service = LibraryConfigService(session)
    await service.get(library_id)  # 404 检查
    rows = await LibraryFileRepository(session).list_missing(library_id)
    rows = [r for r in rows if r.media_item_id is not None]  # 未识别的缺失行不进清单
    items_by_id: dict[int, MediaItem] = {}
    if rows:
        found = (
            await session.execute(
                select(MediaItem).where(MediaItem.id.in_({r.media_item_id for r in rows}))  # type: ignore[attr-defined]
            )
        ).scalars()
        items_by_id = {i.id: i for i in found if i.id is not None}
    grouped: dict[int, tuple[MediaItem, list[LibraryFile]]] = {}
    for file in rows:
        item = items_by_id.get(file.media_item_id or -1)
        if item is not None:
            grouped.setdefault(item.id, (item, []))[1].append(file)  # type: ignore[arg-type]
    if not grouped:
        return ok([])

    # 有订阅的条目要标出来：清理记录后订阅可能把它重新下回来
    subs = await session.execute(
        select(Subscription).where(Subscription.media_item_id.in_(grouped.keys()))  # type: ignore[union-attr]
    )
    sub_by_item = {s.media_item_id: s.id for s in subs.scalars().all()}

    base = get_settings().tmdb_image_base_url.rstrip("/")
    views = [
        MissingItemView(
            media_item_id=item.id,  # type: ignore[arg-type]
            kind=MediaKind(item.kind),
            tmdb_id=item.tmdb_id,
            title=item.title,
            year=item.year,
            poster_url=f"{base}/w500{item.poster_path}" if item.poster_path else None,
            subscription_id=sub_by_item.get(item.id),
            files=[
                MissingFileView(
                    id=f.id,  # type: ignore[arg-type]
                    file_path=f.file_path,
                    season_number=f.season_number,
                    episode_number=f.episode_number,
                    size_bytes=f.size_bytes,
                )
                for f in sorted(files, key=lambda f: (f.season_number, f.episode_number))
            ],
        )
        for item, files in grouped.values()
    ]
    views.sort(key=lambda v: v.title)
    return ok(views)


@router.post(
    "/missing-record-clearances",
    response_model=ApiResponse[dict],
    summary="清理缺失记录（只删台账，绝不动磁盘）；不带 media_item_id 清整库",
    operation_id="library.missing.clear-records",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def clear_missing(
    payload: MissingClearPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = LibraryConfigService(session)
    await service.get(payload.library_id)  # 404 检查
    cleared = await LibraryFileRepository(session).delete_missing(
        payload.library_id, media_item_id=payload.media_item_id
    )
    if cleared:
        await LibraryRepository(session).refresh_stats([payload.library_id])
    return ok({"cleared": cleared}, message=f"已清理 {cleared} 条缺失记录（磁盘未动）")


@router.post(
    "/identification/unidentified-file-ignores",
    response_model=ApiResponse[dict],
    summary="忽略一个媒体库内的全部待识别文件（可恢复，不动磁盘）",
    operation_id="library.identification.ignore-all-unidentified-files",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def clear_unidentified(
    payload: UnidentifiedClearPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = LibraryConfigService(session)
    await service.get(payload.library_id)  # 404 检查
    repo = LibraryFileRepository(session)
    rows = await repo.list_unidentified(library_id=payload.library_id)
    cleared = await repo.mark_ignored([row.id for row in rows if row.id is not None])
    if cleared:
        await LibraryRepository(session).refresh_stats([payload.library_id])
    return ok(
        {"cleared": cleared},
        message=f"已忽略 {cleared} 个待识别文件（磁盘未动；可在「已忽略」里恢复）",
    )


@router.post(
    "/missing-redownloads",
    response_model=ApiResponse[dict],
    summary="重新下载缺失内容：缺失单元交回订阅管线（无订阅则按缺失季创建）",
    operation_id="library.missing.redownload",
    dependencies=[Depends(require_admin)],
)
async def redownload_missing(
    payload: RedownloadPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:

    service = LibraryConfigService(session)
    await service.get(payload.library_id)  # 404 检查
    item = await session.get(MediaItem, payload.media_item_id)
    if item is None:
        raise NotFoundException("媒体条目不存在")
    rows = await LibraryFileRepository(session).list_missing(
        payload.library_id, media_item_id=payload.media_item_id
    )
    if not rows:
        raise BadRequestException("该条目没有缺失文件")
    units = {(r.season_number, r.episode_number) for r in rows}
    subscriptions = SubscriptionService(session, MediaLibraryService(session, get_tmdb_client()))
    subscription, requeued = await subscriptions.redownload_missing_units(
        MediaKind(item.kind), item, units, library_id=payload.library_id
    )
    return ok(
        {"subscription_id": subscription.id, "requeued": requeued},
        message=f"《{item.title}》的 {len(units)} 个缺失单元已交给订阅管线补回",
    )


@router.post(
    "/identification/files/{file_id}/title-assignment",
    response_model=ApiResponse[dict],
    summary="把一个文件明确关联到 Discover 影视条目",
    operation_id="library.identification.assign-file-to-title",
    dependencies=[Depends(require_admin)],
)
async def claim_file(
    file_id: int,
    payload: ClaimPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    target_kind, tmdb_id = _assignment_target(payload.title_ref)
    item, _, displaced = await library_claim.claim_files(
        session,
        [file_id],
        tmdb_id=tmdb_id,
        target_kind=target_kind,
        explicit_unit=(payload.season_number, payload.episode_number),
    )
    # 一次入库刮削的资产补齐（图片 + 媒体目录镜像），后台执行
    background_tasks.add_task(media_scrape.ensure_assets, item.id)
    # 改挂（修正识别结果）时旧条目可能已被腾空，连同图片资产清掉
    if displaced:
        background_tasks.add_task(media_scrape.cleanup_orphan_items, sorted(displaced))
    return ok({}, message=f"已认领为《{item.title}》")


@router.post(
    "/identification/file-title-assignments",
    response_model=ApiResponse[dict],
    summary="把多个文件一次关联到同一个 Discover 影视条目",
    operation_id="library.identification.assign-files-to-title",
    dependencies=[Depends(require_admin)],
)
async def claim_files_batch(
    payload: ClaimBatchPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """一次认领一整组（通常是一部剧的几十集），与单个认领共用
    services/library/claim.claim_files（季集号沿用文件名解析结果）。"""

    target_kind, tmdb_id = _assignment_target(payload.title_ref)
    item, claimed, displaced = await library_claim.claim_files(
        session,
        payload.file_ids,
        tmdb_id=tmdb_id,
        target_kind=target_kind,
    )
    # 一次入库刮削的资产补齐（图片 + 媒体目录镜像），后台执行
    background_tasks.add_task(media_scrape.ensure_assets, item.id)
    # 改挂（修正识别结果）时旧条目可能已被腾空，连同图片资产清掉
    if displaced:
        background_tasks.add_task(media_scrape.cleanup_orphan_items, sorted(displaced))
    return ok(
        {"claimed": claimed},
        message=f"{claimed} 个文件已认领为《{item.title}》",
    )


@router.post(
    "/identification/files/{file_id}/ignore",
    response_model=ApiResponse[dict],
    summary="忽略一个待识别文件：以后扫描不再过问（不动磁盘）",
    operation_id="library.identification.ignore-file",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def ignore_file(
    file_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """忽略 = 打标记，不是删记录。

    删记录曾是这里的实现，但扫描器判定"新文件"就看台账有没有这条路径——
    删了下轮扫描就当新文件重走识别链，认不出照样回清单（对活跃的库连几
    分钟都撑不住，用户看到的是"忽略了又自己回来"）。
    """
    repo = LibraryFileRepository(session)
    row = await session.get(LibraryFile, file_id)
    if row is None:
        raise NotFoundException(f"台账记录不存在：id={file_id}")
    await repo.mark_ignored([file_id])
    await LibraryRepository(session).refresh_stats([row.library_id])
    return ok({}, message="已忽略，之后扫描不再过问（磁盘文件未受影响；可在「已忽略」里恢复）")


@router.post(
    "/identification/files/mark-as-extras",
    response_model=ApiResponse[dict],
    summary="标为「非独立作品」：摘掉身份锚并忽略（花絮/预告类，不动磁盘）",
    operation_id="library.identification.mark-files-as-extras",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def detach_files(
    payload: DetachPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """花絮、预告、片段被识别链**高置信错挂**到别的影片时的出口。

    这类文件用户要表达的不是"改挂到条目 Y"，而是"它根本不该是个条目"。
    单打忽略标记不够——``ignored_at`` 只让扫描不再过问，身份锚还在、条目
    照旧出现在库存里，所以这里连锚一起摘掉。

    与「删除影片」划清界限：**不动磁盘一个字节**，行也始终保留，在「已忽略」
    清单里一键恢复即可重新参与识别。
    """
    repo = LibraryFileRepository(session)
    library_ids = set(
        (
            await session.execute(
                select(LibraryFile.library_id).where(LibraryFile.id.in_(payload.file_ids))  # type: ignore[attr-defined]
            )
        ).scalars()
    )
    detached, displaced = await repo.detach_and_ignore(payload.file_ids)
    if detached == 0:
        raise NotFoundException("这些台账记录都不存在（可能已被处理）")
    await LibraryRepository(session).refresh_stats(library_ids)
    # 摘锚后旧条目往往一个文件都不剩，连同图片资产清掉，不在库里留空壳
    if displaced:
        background_tasks.add_task(media_scrape.cleanup_orphan_items, sorted(displaced))
    return ok(
        {"detached": detached},
        message=(
            f"{detached} 个文件已标为非独立作品，不再占用库存"
            "（磁盘文件未受影响；可在「已忽略」里恢复）"
        ),
    )


@router.post(
    "/identification/ignored-file-restorations",
    response_model=ApiResponse[dict],
    summary="恢复已忽略的文件：重新参与识别",
    operation_id="library.identification.restore-files",
    dependencies=[Depends(require_admin)],
)
async def restore_ignored_files(
    payload: RestorePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """把忽略过的文件放回待识别清单，下次扫描重走识别链。

    识别器一直在变强，当初认不出的以后未必认不出——忽略必须可反悔。
    """
    repo = LibraryFileRepository(session)
    library_ids = set(
        (
            await session.execute(
                select(LibraryFile.library_id).where(LibraryFile.id.in_(payload.file_ids))  # type: ignore[attr-defined]
            )
        ).scalars()
    )
    restored = await repo.restore_ignored(payload.file_ids)
    if restored == 0:
        raise NotFoundException("这些记录都不存在或本来就没被忽略")
    await LibraryRepository(session).refresh_stats(library_ids)
    return ok(
        {"restored": restored},
        message=f"{restored} 个文件已恢复，重新扫描即可再试识别",
    )
