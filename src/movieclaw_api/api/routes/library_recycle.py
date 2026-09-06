"""回收站分区接口（docs/design/library-recycle-bin.md §3）。

媒体库管理页「回收站」标签的数据面：跨库汇总全部 ``state=trashed`` 的台账行，
**按条目分组**分页（一部剧无论多少集一行），并提供按 id / 按筛选的批量
恢复与清理。

与单文件的 ``/libraries/{id}/items/{item}/files/{file}/restore|purge``
（``routes/libraries.py``）共用 ``services.library.recycle`` 的四个公共函数，
这里只做聚合与批处理，不碰文件系统。

路由前缀 ``/libraries/trashed-files`` 与 ``/libraries/{library_id}`` 有路径
歧义，本模块的路由必须在 ``libraries.router`` **之前**注册（见 api/router.py）。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from pathlib import PurePath
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import ColumnElement, and_, case, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.api.deps import require_admin
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.library import (
    TrashedBatchFailureView,
    TrashedBatchResultView,
    TrashedFilesData,
    TrashedFileView,
    TrashedItemRefView,
    TrashedItemView,
    TrashedLibraryCountView,
    TrashedLibraryRefView,
    TrashedPurgePayload,
    TrashedQualityView,
    TrashedReasonCountView,
    TrashedRestorePayload,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.library.recycle import purge_file, restore_file
from movieclaw_api.services.media_server_notify import notify_media_server_refresh
from movieclaw_db.engine import get_session
from movieclaw_db.models import FileState, LibraryFile, MediaEpisode, MediaItem, utcnow
from movieclaw_db.models.library import Library
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.library.recycle_bin")

router = APIRouter(prefix="/libraries/trashed-files", tags=["libraries"])

# 单页最多的条目数（分页单位是条目，不是文件）
PAGE_LIMIT_MAX = 100
# 按筛选批量清理一次最多处理的文件数：同步执行，几百个 unlink 毫秒级；
# 超出的部分由前端提示"还有 N 个，再点一次"（设计 §3.2）
BATCH_LIMIT = 500
# 摘要行「N 个将在 24 小时内自动清理」的窗口
DUE_SOON = timedelta(hours=24)

# 音轨「编码」的紧凑写法，与条目详情页音轨行行首的格式色块同一套
_AUDIO_CODEC_TOKENS = {
    "aac": "AAC",
    "ac3": "AC3",
    "eac3": "EAC3",
    "truehd": "TrueHD",
    "dts": "DTS",
    "flac": "FLAC",
    "opus": "Opus",
    "mp3": "MP3",
    "vorbis": "Vorbis",
}
# 这些 profile 只是编码内部档次，单独展示反而让人困惑；只有 DTS-HD MA 这类
# "比 codec 更有信息量"的 profile 才顶替 codec（与详情页同一判断）
_GENERIC_PROFILES = {"lc", "main", "high", "baseline", "main 10"}
_CHANNEL_LABELS = {1: "单声道", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1"}


# ---------------------------------------------------------------------------
# 筛选条件（列表、聚合、批量清理三处共用同一份，保证"摘要的数字 = 清理的范围"）
# ---------------------------------------------------------------------------


def _reason_expr() -> ColumnElement[str | None]:
    """审计快照里的 reason（JSON 路径取值，SQLite / Postgres 通用）。"""
    return LibraryFile.trash_context["reason"].as_string()  # type: ignore[index]


def _group_key() -> ColumnElement[int]:
    """分组键：条目 id；未识别的待回收文件（media_item_id 为空）按行各自成组。"""
    return func.coalesce(LibraryFile.media_item_id, -LibraryFile.id)


def _conditions(
    q: str | None, library_id: int | None, reason: str | None
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = [LibraryFile.state == FileState.TRASHED]
    if q:
        needle = f"%{q.strip()}%"
        conds.append(
            or_(
                LibraryFile.file_path.ilike(needle),  # type: ignore[union-attr]
                MediaItem.title.ilike(needle),  # type: ignore[union-attr]
                MediaItem.original_title.ilike(needle),  # type: ignore[union-attr]
            )
        )
    if library_id is not None:
        conds.append(LibraryFile.library_id == library_id)
    if reason:
        conds.append(_reason_expr() == reason)
    return conds


def _from_files(stmt: Any) -> Any:
    """统一的 FROM：台账行左连条目（搜索片名需要；未识别行没有条目也要进列表）。"""
    return stmt.select_from(LibraryFile).outerjoin(
        MediaItem, MediaItem.id == LibraryFile.media_item_id
    )


# ---------------------------------------------------------------------------
# 视图拼装
# ---------------------------------------------------------------------------


def _audio_label(streams: list | None) -> str | None:
    """首条音轨 → 「编码 声道」（如 ``DTS-HD MA 5.1`` / ``AAC 2.0``）；未探测为 None。"""
    if not streams:
        return None
    stream = next((s for s in streams if s.get("default")), streams[0])
    profile = stream.get("profile")
    codec = stream.get("codec")
    if profile and str(profile).lower() not in _GENERIC_PROFILES:
        name: str | None = str(profile)
    elif codec:
        name = _AUDIO_CODEC_TOKENS.get(str(codec).lower(), str(codec).upper())
    else:
        name = None
    layout = str(stream.get("channel_layout") or "").split("(")[0].strip()
    channels = stream.get("channels")
    if layout[:1].isdigit():
        chan: str | None = layout
    elif isinstance(channels, int):
        chan = _CHANNEL_LABELS.get(channels, f"{channels} 声道")
    else:
        chan = None
    return " ".join(part for part in (name, chan) if part) or None


def _tier(row: LibraryFile) -> str:
    """品质档位「分辨率 片源」，两者都没探到时写「未知规格」。"""
    return " ".join(part for part in (row.resolution, row.media_source) if part) or "未知规格"


def _file_view(row: LibraryFile, episode_title: str | None) -> TrashedFileView:
    ctx = row.trash_context or {}
    return TrashedFileView(
        id=row.id,  # type: ignore[arg-type]  # 落库后必有主键
        file_name=PurePath(row.file_path).name,
        file_path=row.file_path,
        trash_original_path=row.trash_original_path,
        kept_in_place=row.trash_original_path is None,
        size_bytes=row.size_bytes,
        resolution=row.resolution,
        media_source=row.media_source,
        hdr=row.hdr,
        video_codec=row.video_codec,
        bit_depth=row.bit_depth,
        audio_label=_audio_label(row.audio_streams),
        release_group=row.release_group,
        season_number=row.season_number,
        episode_number=row.episode_number,
        episode_title=episode_title,
        trashed_at=row.trashed_at,
        purge_after=row.purge_after,
        reason=ctx.get("reason"),
        note=ctx.get("note"),
        last_error=ctx.get("last_error"),
    )


def _item_view(
    key: int,
    rows: list[LibraryFile],
    library: Library,
    item: MediaItem | None,
    episode_titles: dict[tuple[int, int, int], str],
) -> TrashedItemView:
    """一组待回收文件 → 条目行：剧集按季集、电影多版本按大小降序；组内汇总去重。"""
    if item is not None and item.kind == MediaKind.TV.value:
        rows = sorted(rows, key=lambda r: (r.season_number, r.episode_number, r.id or 0))
    else:
        rows = sorted(rows, key=lambda r: (-r.size_bytes, r.id or 0))

    reasons: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    notes: set[str] = set()
    hdr: set[str] = set()
    codecs: set[str] = set()
    audios: set[str] = set()
    groups: set[str] = set()
    trigger_label: str | None = None
    latest_trashed = None
    for row in rows:
        ctx = row.trash_context or {}
        reasons[str(ctx.get("reason") or "unknown")] += 1
        tiers[_tier(row)] += 1
        if ctx.get("note"):
            notes.add(str(ctx["note"]))
        if row.hdr:
            hdr.add(row.hdr)
        if row.video_codec:
            codecs.add(row.video_codec)
        if label := _audio_label(row.audio_streams):
            audios.add(label)
        if row.release_group:
            groups.add(row.release_group)
        if row.trashed_at and (latest_trashed is None or row.trashed_at > latest_trashed):
            latest_trashed = row.trashed_at
            trigger_label = str((ctx.get("trigger") or {}).get("label") or "") or None

    purge_times = [r.purge_after for r in rows if r.purge_after is not None]
    files = [
        _file_view(
            row,
            episode_titles.get((row.media_item_id or 0, row.season_number, row.episode_number)),
        )
        for row in rows
    ]
    return TrashedItemView(
        key=str(key),
        library=TrashedLibraryRefView(id=library.id, name=library.name),  # type: ignore[arg-type]
        media_item=(
            None
            if item is None
            else TrashedItemRefView(
                id=item.id,  # type: ignore[arg-type]
                title=item.title,
                year=item.year,
                kind=MediaKind(item.kind),
                poster_url=(
                    f"{get_settings().tmdb_image_base_url.rstrip('/')}/w185{item.poster_path}"
                    if item.poster_path
                    else None
                ),
            )
        ),
        seasons=sorted({r.season_number for r in rows if r.season_number > 0})
        if item is not None and item.kind == MediaKind.TV.value
        else [],
        file_count=len(rows),
        total_bytes=sum(r.size_bytes for r in rows),
        earliest_purge_after=min(purge_times) if purge_times else None,
        latest_purge_after=max(purge_times) if purge_times else None,
        reasons=dict(reasons),
        note=next(iter(notes)) if len(notes) == 1 else None,
        trigger_label=trigger_label,
        latest_trashed_at=latest_trashed,
        quality=TrashedQualityView(
            tiers=dict(tiers),
            hdr=sorted(hdr),
            video_codecs=sorted(codecs),
            audio_labels=sorted(audios),
            release_groups=sorted(groups),
        ),
        files=files,
    )


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ApiResponse[TrashedFilesData],
    summary="回收站：全部待回收文件，按条目分组分页（含摘要与分面计数）",
    operation_id="library.recycle.list",
    dependencies=[Depends(require_admin)],
)
async def list_trashed_files(
    q: Annotated[str | None, Query(description="按片名 / 剧名 / 文件名搜索")] = None,
    library_id: Annotated[int | None, Query(description="只看某个库")] = None,
    reason: Annotated[
        str | None,
        Query(description="只看某种原因（upgrade_replaced / upgrade_refuted / manual …）"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_LIMIT_MAX, description="本页条目数")] = 20,
    offset: Annotated[int, Query(ge=0, description="跳过的条目数")] = 0,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TrashedFilesData]:
    """两步查询：先按分组键分组、按「组内最早到期」排序分页出本页条目，再取这些
    条目的全部待回收文件拼视图。聚合口径与筛选一致（见 ``TrashedFilesData``）。"""
    conds = _conditions(q, library_id, reason)
    now = utcnow()
    key = _group_key()

    # —— 摘要（全部筛选） ——
    totals = (
        await session.execute(
            _from_files(
                select(
                    func.count(LibraryFile.id),
                    func.count(func.distinct(key)),
                    func.coalesce(func.sum(LibraryFile.size_bytes), 0),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    and_(
                                        LibraryFile.purge_after.is_not(None),  # type: ignore[union-attr]
                                        LibraryFile.purge_after <= now + DUE_SOON,  # type: ignore[operator]
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (LibraryFile.trash_original_path.is_(None), 1),  # type: ignore[union-attr]
                                else_=0,
                            )
                        ),
                        0,
                    ),
                )
            ).where(*conds)
        )
    ).one()
    total_files, total_items, total_bytes, due_soon, kept = (int(v or 0) for v in totals)

    # —— 分面计数：库胶囊不受库筛选影响，原因胶囊不受原因筛选影响 ——
    by_library_rows = (
        await session.execute(
            _from_files(select(Library.id, Library.name, func.count(LibraryFile.id)))
            .join(Library, Library.id == LibraryFile.library_id)
            .where(*_conditions(q, None, reason))
            .group_by(Library.id, Library.name, Library.sort_order)
            .order_by(Library.sort_order, Library.id)
        )
    ).all()
    reason_key = func.coalesce(_reason_expr(), "unknown")
    by_reason_rows = (
        await session.execute(
            _from_files(select(reason_key, func.count(LibraryFile.id)))
            .where(*_conditions(q, library_id, None))
            .group_by(reason_key)
            .order_by(func.count(LibraryFile.id).desc())
        )
    ).all()

    # —— 本页条目键：最早到期升序（全组不自动清理的排最后），再按最近进入降序 ——
    earliest = func.min(LibraryFile.purge_after)
    latest_trashed = func.max(LibraryFile.trashed_at)
    page_rows = (
        await session.execute(
            _from_files(select(key.label("k"), earliest.label("e"), latest_trashed.label("t")))
            .where(*conds)
            .group_by(key)
            .order_by(
                case((earliest.is_(None), 1), else_=0), earliest.asc(), latest_trashed.desc(), key
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    keys = [int(r[0]) for r in page_rows]

    items: list[TrashedItemView] = []
    if keys:
        item_ids = [k for k in keys if k > 0]
        orphan_ids = [-k for k in keys if k < 0]
        member = []
        if item_ids:
            member.append(LibraryFile.media_item_id.in_(item_ids))  # type: ignore[union-attr]
        if orphan_ids:
            member.append(LibraryFile.id.in_(orphan_ids))  # type: ignore[union-attr]
        file_rows = list(
            (await session.execute(_from_files(select(LibraryFile)).where(*conds, or_(*member))))
            .scalars()
            .all()
        )
        grouped: dict[int, list[LibraryFile]] = {}
        for row in file_rows:
            grouped.setdefault(row.media_item_id or -(row.id or 0), []).append(row)

        media_items = (
            {
                m.id: m
                for m in (
                    await session.execute(select(MediaItem).where(MediaItem.id.in_(item_ids)))  # type: ignore[union-attr]
                ).scalars()
            }
            if item_ids
            else {}
        )
        libraries = {
            lib.id: lib
            for lib in (
                await session.execute(
                    select(Library).where(
                        Library.id.in_({r.library_id for r in file_rows})  # type: ignore[union-attr]
                    )
                )
            ).scalars()
        }
        tv_ids = [i for i, m in media_items.items() if m.kind == MediaKind.TV.value]
        episode_titles: dict[tuple[int, int, int], str] = {}
        if tv_ids:
            for mid, season, episode, name in (
                await session.execute(
                    select(
                        MediaEpisode.media_item_id,
                        MediaEpisode.season_number,
                        MediaEpisode.episode_number,
                        MediaEpisode.name,
                    ).where(MediaEpisode.media_item_id.in_(tv_ids))  # type: ignore[union-attr]
                )
            ).all():
                if name:
                    episode_titles[(mid, season, episode)] = name

        for k in keys:
            rows = grouped.get(k)
            if not rows:
                continue  # 分页与取行之间被清理掉了：跳过即可，下一次轮询自然消失
            library = libraries[rows[0].library_id]
            items.append(
                _item_view(k, rows, library, media_items.get(k) if k > 0 else None, episode_titles)
            )

    return ok(
        TrashedFilesData(
            total_files=total_files,
            total_items=total_items,
            total_bytes=total_bytes,
            due_within_24h=due_soon,
            kept_in_place=kept,
            by_library=[
                TrashedLibraryCountView(library_id=int(lid), name=str(name), count=int(cnt))
                for lid, name, cnt in by_library_rows
            ],
            by_reason=[
                TrashedReasonCountView(reason=str(r), count=int(cnt)) for r, cnt in by_reason_rows
            ],
            items=items,
        )
    )


# ---------------------------------------------------------------------------
# 批量恢复 / 清理
# ---------------------------------------------------------------------------


async def _set_last_error(session: AsyncSession, row: LibraryFile, error: str) -> None:
    """把失败原因写回审计快照（JSON 列整体替换才会被 SQLAlchemy 视为已修改）。"""
    row.trash_context = {**(row.trash_context or {}), "last_error": error}
    row.updated_at = utcnow()
    await session.commit()


async def _run_batch(
    session: AsyncSession, ids: list[int], *, action: str
) -> tuple[int, list[TrashedBatchFailureView], set[int]]:
    """逐文件执行、单独提交：一个失败不回滚已成功的。返回 (成功数, 失败清单, 涉及的库 id)。"""
    done = 0
    failed: list[TrashedBatchFailureView] = []
    library_ids: set[int] = set()
    for file_id in ids:
        row = await session.get(LibraryFile, file_id)
        if row is None:
            failed.append(
                TrashedBatchFailureView(
                    id=file_id, file_name=f"#{file_id}", error="文件记录不存在（可能已被清理）"
                )
            )
            continue
        file_name = PurePath(row.file_path).name
        if row.state != FileState.TRASHED:
            failed.append(
                TrashedBatchFailureView(id=file_id, file_name=file_name, error="不在待回收状态")
            )
            continue
        library_ids.add(row.library_id)
        if action == "purge":
            succeeded = await purge_file(session, row)
            error = "清理失败：目录内还有其他在案文件（需先处理），或文件权限不足——详见服务日志"
        else:
            succeeded = await restore_file(session, row)
            error = "恢复失败：原路径已有同名文件，或文件已不存在——可稍后清理该记录"
        if succeeded:
            await session.commit()
            done += 1
        else:
            await _set_last_error(session, row, error)
            failed.append(TrashedBatchFailureView(id=file_id, file_name=file_name, error=error))
    return done, failed, library_ids


def _summary(verb: str, done: int, failed: int, remaining: int) -> str:
    text = f"已{verb} {done} 个文件"
    if failed:
        text += f"，{failed} 个失败"
    if remaining:
        text += f"，还有 {remaining} 个未处理（再执行一次即可）"
    return text


@router.post(
    "/purge",
    response_model=ApiResponse[TrashedBatchResultView],
    summary="批量立即清理待回收文件（按 id 或按筛选；真删磁盘）",
    operation_id="library.recycle.purge",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def purge_trashed_files(
    payload: TrashedPurgePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TrashedBatchResultView]:
    """``ids`` 用于所选 / 条目行 / 单个文件，``filter`` 用于「立即清理全部」（服务端按筛选
    重新查一遍 id，不信任前端传来的数量）。做种中断的确认由前端负责。"""
    if (payload.ids is None) == (payload.filter is None):
        raise BadRequestException("需要且只能提供 ids 或 filter 之一")
    remaining = 0
    if payload.ids is not None:
        ids = list(dict.fromkeys(payload.ids))
    else:
        flt = payload.filter
        assert flt is not None
        conds = _conditions(flt.q, flt.library_id, flt.reason)
        # 与列表同一顺序（最先到期的先清），单次最多 BATCH_LIMIT 个
        candidate_rows = (
            await session.execute(
                _from_files(select(LibraryFile.id))
                .where(*conds)
                .order_by(
                    case((LibraryFile.purge_after.is_(None), 1), else_=0),  # type: ignore[union-attr]
                    LibraryFile.purge_after.asc(),  # type: ignore[union-attr]
                    LibraryFile.id,
                )
            )
        ).all()
        all_ids = [int(r[0]) for r in candidate_rows]
        ids, remaining = all_ids[:BATCH_LIMIT], max(0, len(all_ids) - BATCH_LIMIT)
    done, failed, _libs = await _run_batch(session, ids, action="purge")
    result = TrashedBatchResultView(done=done, failed=failed, remaining=remaining)
    return ok(result, message=_summary("清理", done, len(failed), remaining))


@router.post(
    "/restore",
    response_model=ApiResponse[TrashedBatchResultView],
    summary="批量恢复待回收文件为在位版本",
    operation_id="library.recycle.restore",
    dependencies=[Depends(require_admin)],
)
async def restore_trashed_files(
    payload: TrashedRestorePayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TrashedBatchResultView]:
    """恢复是可逆动作，不设「按筛选恢复全部」（设计 §2.5）。恢复后文件重新计入在位
    库存，顺带重算所涉库的统计快照并通知媒体服务器刷新。"""
    if not payload.ids:
        raise BadRequestException("ids 不能为空")
    ids = list(dict.fromkeys(payload.ids))
    done, failed, library_ids = await _run_batch(session, ids, action="restore")
    if done:
        await LibraryRepository(session).refresh_stats(library_ids)
        background_tasks.add_task(notify_media_server_refresh)
    result = TrashedBatchResultView(done=done, failed=failed)
    return ok(result, message=_summary("恢复", done, len(failed), 0))
