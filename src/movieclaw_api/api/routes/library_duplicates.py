"""重复文件接口（docs/design/library-duplicate-files.md §4）。

媒体库管理页「重复文件」标签的数据面：跨库圈出多文件单元，分成「一模一样」
「不同版本」两堆，按条目分页；三个写动作——一个单元 / 一季的「留这个 /
整季留这个版本 / 都留着」，以及整堆按建议清理。检测与清理都在
``services.library.duplicates``，这里只做视图拼装。

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
    DuplicateBucketStats,
    DuplicateFilesData,
    DuplicateFileView,
    DuplicateItemView,
    DuplicateResolveAllPayload,
    DuplicateResolvePayload,
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
from movieclaw_api.services.library.duplicates import (
    DupFile,
    DupItem,
    DuplicateReport,
    ResolveOutcome,
    detect_duplicates,
    resolve_all,
    resolve_unit,
)
from movieclaw_api.services.media_server_notify import notify_media_server_refresh
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

router = APIRouter(prefix="/libraries/duplicate-files", tags=["libraries"])

# 整堆清理一次最多处理的文件数：同步执行（回收站是同盘 rename），与回收站批量同一上限
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


def _data(report: DuplicateReport) -> DuplicateFilesData:
    return DuplicateFilesData(
        identical=DuplicateBucketStats(
            units=report.identical.units, files=report.identical.files, bytes=report.identical.bytes
        ),
        versions=DuplicateBucketStats(
            units=report.versions.units, files=report.versions.files, bytes=report.versions.bytes
        ),
        upgrading_units=report.upgrading_units,
        keep_old_items=report.keep_old_items,
        total_items=report.total_items,
        items=[_item_view(d) for d in report.items],
    )


def _trigger(principal: Principal) -> dict:
    return {"kind": "member", "id": principal.member_id, "label": principal.name}


def _result(outcome: ResolveOutcome) -> TrashedBatchResultView:
    return TrashedBatchResultView(
        done=outcome.done,
        failed=[TrashedBatchFailureView(id=i, file_name=n, error=e) for i, n, e in outcome.failed],
        remaining=outcome.remaining,
    )


@router.get(
    "",
    response_model=ApiResponse[DuplicateFilesData],
    summary="重复文件：多文件单元分「一模一样 / 不同版本」两堆，按条目分页",
    operation_id="library.duplicates.list",
    dependencies=[Depends(require_admin)],
)
async def list_duplicate_files(
    q: Annotated[str | None, Query(description="按片名 / 剧名 / 文件名搜索")] = None,
    library_id: Annotated[int | None, Query(description="只看某个库")] = None,
    media_item_id: Annotated[int | None, Query(description="只看某个条目（详情页入口）")] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_LIMIT_MAX, description="本页条目数")] = 20,
    offset: Annotated[int, Query(ge=0, description="跳过的条目数")] = 0,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DuplicateFilesData]:
    report = await detect_duplicates(
        session, library_id=library_id, media_item_id=media_item_id, q=q, limit=limit, offset=offset
    )
    return ok(_data(report))


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
    if payload.keep_all:
        return ok(_result(outcome), message=f"已标记「都留着」：{outcome.done} 个文件不再列为重复")
    if outcome.done:
        await LibraryRepository(session).refresh_stats(list(outcome.library_ids))
        background_tasks.add_task(notify_media_server_refresh)
    return ok(
        _result(outcome), message=_summary("移入回收站", outcome.done, len(outcome.failed), 0)
    )


@router.post(
    "/resolve-all",
    response_model=ApiResponse[TrashedBatchResultView],
    summary="整堆按「建议保留」清理（一模一样 / 不同版本）",
    operation_id="library.duplicates.resolve-all",
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def resolve_all_duplicates(
    payload: DuplicateResolveAllPayload,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TrashedBatchResultView]:
    outcome = await resolve_all(
        session,
        bucket=payload.bucket,
        library_id=payload.library_id,
        trigger=_trigger(principal),
        batch_limit=BATCH_LIMIT,
    )
    if outcome.done:
        await LibraryRepository(session).refresh_stats(list(outcome.library_ids))
        background_tasks.add_task(notify_media_server_refresh)
    return ok(
        _result(outcome),
        message=_summary("移入回收站", outcome.done, len(outcome.failed), outcome.remaining),
    )
