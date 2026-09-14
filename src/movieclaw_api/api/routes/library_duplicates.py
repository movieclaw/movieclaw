"""重复文件接口（docs/design/library-duplicate-files.md §4 / §9）。

媒体库管理页「重复文件」标签的数据面。**检测不在这里发生**：重复关系由
``POST /scan`` 起的后台任务算一轮、落进 ``library_duplicate_unit``，这里只读那份
结论（第一版每次 GET 现算全库，万级媒体库打开即卡，返工记录见设计文档 §9）。

读：一次请求给三样——扫描状态（扫过没有 / 正在跑到哪了）、分档摘要（放心清 /
建议清 / 要你决定，后者再按取舍类型分组）、本页条目明细（``limit=0`` 不带）。
写：四个动作——起一轮扫描；一个单元 / 一季的「留这个 / 整季留这个版本 / 都留着」；
一整档或一组的「都按建议清理 / 都留着」。

路由前缀 ``/libraries/duplicate-files`` 与 ``/libraries/{library_id}`` 有路径
歧义，必须在 ``libraries.router`` **之前**注册（见 api/router.py，与回收站同）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_admin
from movieclaw_api.api.routes.library_recycle import PAGE_LIMIT_MAX, _audio_label, _summary
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.library import (
    DuplicateFilesData,
    DuplicateFileView,
    DuplicateGroupView,
    DuplicateItemView,
    DuplicateResolveAllPayload,
    DuplicateResolvePayload,
    DuplicateScanStateView,
    DuplicateSeasonView,
    DuplicateUnitView,
    DuplicateVersionView,
    FileOriginView,
    TrashedBatchFailureView,
    TrashedBatchResultView,
    TrashedItemRefView,
    TrashedLibraryRefView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library import duplicate_scan
from movieclaw_api.services.library.duplicates import (
    DupFile,
    DupItem,
    ResolveOutcome,
    resolve_unit,
)
from movieclaw_api.services.media_server_notify import notify_media_server_refresh
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

router = APIRouter(prefix="/libraries/duplicate-files", tags=["libraries"])

# 一次批量最多处理的文件数：同步执行（回收站是同盘 rename），与回收站批量同一上限
BATCH_LIMIT = 500


def _file_view(f: DupFile) -> DuplicateFileView:
    row = f.row
    return DuplicateFileView(
        id=row.id,  # type: ignore[arg-type]
        file_name=f.file_name,
        file_path=row.file_path,
        quality_label=f.quality_label,
        size_bytes=row.size_bytes,
        bit_rate=row.bit_rate,
        resolution=row.resolution,
        media_source=row.media_source,
        hdr=row.hdr,
        video_codec=row.video_codec,
        audio_label=_audio_label(row.audio_streams),
        origin=FileOriginView(**f.origin),
        version_key=f.version_key,
        suggested=f.suggested,
        suggest_reason=f.suggest_reason,
        kept_at=row.kept_at,
    )


def _item_view(d: DupItem) -> DuplicateItemView:
    item = d.item
    return DuplicateItemView(
        library=TrashedLibraryRefView(id=d.library.id, name=d.library.name),  # type: ignore[arg-type]
        media_item=TrashedItemRefView(
            id=item.id,  # type: ignore[arg-type]
            title=item.title,
            year=item.year,
            kind=MediaKind(item.kind),
            poster_url=(
                f"{get_settings().tmdb_image_base_url.rstrip('/')}/w185{item.poster_path}"
                if item.poster_path
                else None
            ),
        ),
        seasons=[
            DuplicateSeasonView(
                season_number=s.season_number,
                bucket=s.bucket,
                uniform=s.uniform,
                versions=[
                    DuplicateVersionView(
                        key=v.key,
                        quality_label=v.quality_label,
                        origin_label=v.origin_label,
                        episodes=v.episodes,
                        bytes=v.bytes,
                        suggested=v.suggested,
                    )
                    for v in s.versions
                ],
                units=[
                    DuplicateUnitView(
                        season_number=u.season_number,
                        episode_number=u.episode_number,
                        bucket=u.bucket,
                        files=[_file_view(f) for f in u.files],
                    )
                    for u in s.units
                ],
            )
            for s in d.seasons
        ],
    )


def _group_view(g: duplicate_scan.GroupStats) -> DuplicateGroupView:
    return DuplicateGroupView(
        key=g.key, label=g.label, hint=g.hint, units=g.units, files=g.files, bytes=g.bytes
    )


def _scan_view(state: duplicate_scan.ScanState) -> DuplicateScanStateView:
    return DuplicateScanStateView(
        status=state.status,
        job_id=state.job_id,
        message=state.message,
        percent=state.percent,
        scanned_at=state.scanned_at,
        upgrading_units=state.upgrading_units,
        keep_old_items=state.keep_old_items,
    )


def _trigger(principal: Principal) -> dict:
    return {"kind": "member", "id": principal.member_id, "label": principal.name}


def _result(outcome: ResolveOutcome) -> TrashedBatchResultView:
    return TrashedBatchResultView(
        done=outcome.done,
        failed=[TrashedBatchFailureView(id=i, file_name=n, error=e) for i, n, e in outcome.failed],
        remaining=outcome.remaining,
    )


async def _after_cleanup(
    session: AsyncSession, outcome: ResolveOutcome, background_tasks: BackgroundTasks
) -> None:
    if outcome.done:
        await LibraryRepository(session).refresh_stats(list(outcome.library_ids))
        background_tasks.add_task(notify_media_server_refresh)


@router.get(
    "",
    response_model=ApiResponse[DuplicateFilesData],
    summary="重复文件：扫描状态 + 三档摘要 + 本页条目",
    operation_id="library.duplicates.list",
    dependencies=[Depends(require_admin)],
)
async def list_duplicate_files(
    tier: Annotated[str | None, Query(description="只看某一档：safe / suggested / review")] = None,
    review_kind: Annotated[
        str | None,
        Query(description="tier=review 时只看某一种取舍：resolution / hdr / unknown / same_tier"),
    ] = None,
    q: Annotated[str | None, Query(description="按片名 / 剧名搜索")] = None,
    library_id: Annotated[int | None, Query(description="只看某个库")] = None,
    media_item_id: Annotated[int | None, Query(description="只看某个条目（详情页入口）")] = None,
    limit: Annotated[
        int, Query(ge=0, le=PAGE_LIMIT_MAX, description="本页条目数；0 = 只要摘要")
    ] = 20,
    offset: Annotated[int, Query(ge=0, description="跳过的条目数")] = 0,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DuplicateFilesData]:
    if tier is not None and tier not in duplicate_scan.TIER_LABELS:
        raise BadRequestException("tier 只能是 safe / suggested / review")
    if review_kind is not None and review_kind not in duplicate_scan.REVIEW_KIND_LABELS:
        raise BadRequestException("review_kind 只能是 resolution / hdr / unknown / same_tier")
    state = await duplicate_scan.scan_state(session)
    summary = await duplicate_scan.summarize(session, library_id=library_id)
    total_items, items = await duplicate_scan.list_duplicate_items(
        session,
        tier=tier,
        review_kind=review_kind,
        library_id=library_id,
        media_item_id=media_item_id,
        q=q,
        limit=limit,
        offset=offset,
    )
    return ok(
        DuplicateFilesData(
            scan=_scan_view(state),
            tiers=[_group_view(g) for g in summary.tiers],
            review_groups=[_group_view(g) for g in summary.review_groups],
            total_units=summary.total_units,
            total_files=summary.total_files,
            total_bytes=summary.total_bytes,
            total_items=total_items,
            items=[_item_view(d) for d in items],
        )
    )


@router.post(
    "/scan",
    response_model=ApiResponse[dict],
    status_code=202,
    summary="开始扫描重复文件（可恢复后台作业，结论落库供页面读取）",
    operation_id="library.duplicates.scan",
    openapi_extra={"x-cli-job": {"id_path": "job_id", "wait_op": "jobs.wait"}},
)
async def start_duplicate_scan(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """跨全部媒体库算一轮重复关系，结论落 ``library_duplicate_unit``。

    和「扫描媒体库」一样是用户按一次、后台跑一会儿的事：检测要给每个候选
    文件各 stat 一次、各跑一遍发布名解析，不能挂在打开页面的请求线上。媒体库
    扫描结束会自动排一份，这里是手动入口。同时最多一份在跑。"""

    created = await duplicate_scan.enqueue_duplicate_scan_job(
        session,
        actor_kind=principal.kind,
        actor_name=principal.name,
        actor_id=str(principal.member_id) if principal.member_id else None,
        origin="user",
    )
    return ok(
        {"started": True, "job_id": created.job.id, "created": created.created},
        message=(
            "已开始扫描重复文件，可在任务中心继续观察" if created.created else "重复扫描正在进行中"
        ),
    )


@router.post(
    "/resolve",
    response_model=ApiResponse[TrashedBatchResultView],
    summary="一个单元 / 一季的决定：留这个 / 整季留这个版本 / 都留着",
    operation_id="library.duplicates.resolve",
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def resolve_duplicates(
    payload: DuplicateResolvePayload,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TrashedBatchResultView]:
    chosen = [
        payload.keep_file_id is not None,
        payload.keep_version is not None,
        payload.keep_all,
    ]
    if sum(chosen) != 1:
        raise BadRequestException("keep_file_id / keep_version / keep_all 三选一，必须且只能给一个")
    if payload.keep_version is not None and "|" not in payload.keep_version:
        raise BadRequestException("keep_version 必须是列表接口返回的 version_key（含「|」）")
    keep: int | str = (
        "all"
        if payload.keep_all
        else payload.keep_file_id
        if payload.keep_file_id is not None
        else payload.keep_version  # type: ignore[assignment]
    )
    try:
        outcome = await resolve_unit(
            session,
            media_item_id=payload.media_item_id,
            season_number=payload.season_number,
            episode_number=payload.episode_number,
            keep=keep,
            trigger=_trigger(principal),
        )
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    # 做完决定的单元不再是"待处理"：删掉它的结论行，摘要数字立刻变小，
    # 不必为一个决定重扫整库（真正的修正等下一轮扫描）。
    # 一个文件都没清成（权限、文件被占用……）时不能删：那不是"做完了决定"，
    # 而是决定没执行成功。删了它这个单元就从列表和摘要里一起消失，用户看到
    # 一句报错、再刷新发现条目没了、文件却还在，只能重扫整库才找得回来。
    if outcome.done or not outcome.failed:
        await duplicate_scan.forget_units(
            session,
            media_item_id=payload.media_item_id,
            season_number=payload.season_number,
            episode_number=payload.episode_number,
        )
    if payload.keep_all:
        return ok(_result(outcome), message=f"已标记「都留着」：{outcome.done} 个文件不再列为重复")
    await _after_cleanup(session, outcome, background_tasks)
    return ok(
        _result(outcome), message=_summary("移入回收站", outcome.done, len(outcome.failed), 0)
    )


@router.post(
    "/resolve-all",
    response_model=ApiResponse[TrashedBatchResultView],
    summary="一整档 / 一组一起决定：都按建议清理，或都留着",
    operation_id="library.duplicates.resolve-all",
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def resolve_all_duplicates(
    payload: DuplicateResolveAllPayload,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TrashedBatchResultView]:
    if payload.review_kind is not None and payload.tier != "review":
        raise BadRequestException("review_kind 只在 tier=review 时有意义")
    outcome = await duplicate_scan.resolve_group(
        session,
        tier=payload.tier,
        review_kind=payload.review_kind,
        library_id=payload.library_id,
        keep_all=payload.keep_all,
        trigger=_trigger(principal),
        batch_limit=BATCH_LIMIT,
    )
    if payload.keep_all:
        return ok(_result(outcome), message=f"已标记「都留着」：{outcome.done} 个文件不再列为重复")
    await _after_cleanup(session, outcome, background_tasks)
    if not outcome.done and not outcome.failed and not outcome.remaining:
        # 这一组的结论全过期了（扫描之后跑过入库 / 洗版 / 另一轮扫描）。不按过期
        # 结论删文件是铁律，所以这里什么也没做——直接告诉用户该重扫，而不是回一句
        # 「已移入回收站 0 个文件」让人以为自己点错了
        return ok(_result(outcome), message="这一组的结果已经过期（库里有过改动），请重新扫描")
    return ok(
        _result(outcome),
        message=_summary("移入回收站", outcome.done, len(outcome.failed), outcome.remaining),
    )
