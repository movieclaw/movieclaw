"""媒体库浏览接口（设计文档 §5）：Views / Items / Shows / Resume / NextUp / Latest。

路由注册顺序即匹配顺序：/Items 下的字面路径（Latest/Root/Filters…）必须先于
/Items/{item_id} 注册。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from movieclaw_db.engine import get_database
from movieclaw_db.models import Library
from movieclaw_jellyfin.catalog import (
    DtoContext,
    DtoOptions,
    ItemBundle,
    episode_dto,
    item_ids_with_files,
    library_view_dto,
    list_libraries,
    load_bundles,
    load_library_stats,
    movie_dto,
    season_dto,
    series_dto,
)
from movieclaw_jellyfin.errors import not_found, not_found_message
from movieclaw_jellyfin.ids import (
    FIXED_ROOT,
    EntityKind,
    decode_guid,
    is_empty_guid,
    library_guid,
)
from movieclaw_jellyfin.routes.common import (
    dto_context,
    dto_options,
    parse_bool,
    parse_comma,
    parse_pipe,
    query_result,
)
from movieclaw_jellyfin.security import require_device

router = APIRouter(dependencies=[Depends(require_device)])

# (type, bundle, season, episode)：查询管线里的一条候选
Entry = tuple[str, ItemBundle, int, int]


async def _items_with_persons(
    session: AsyncSession, person_guids: list[str]
) -> set[int]:
    """personIds 过滤：解出人物 id → 反查参演/执导的条目集合。"""
    from sqlalchemy import select as sa_select

    from movieclaw_db.models import MediaItemPerson

    ids = [
        r.entity_id
        for r in (decode_guid(g) for g in person_guids)
        if r is not None and r.kind == EntityKind.PERSON
    ]
    if not ids:
        return set()
    rows = (
        await session.execute(
            sa_select(MediaItemPerson.media_item_id).where(
                MediaItemPerson.person_id.in_(ids)
            )
        )
    ).scalars()
    return set(rows)


async def _cover_tag(library_id: int) -> str | None:
    """库封面拼贴的版本 key（惰性渲染，素材不变零成本）。"""
    from movieclaw_api.services.library.cover import ensure_library_cover

    result = await ensure_library_cover(library_id)
    return result[1] if result else None


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


@router.get("/UserViews")
@router.get("/Users/{user_id}/Views")
async def user_views(user_id: str | None = None) -> JSONResponse:
    ctx = await dto_context()
    async with get_database().session() as session:
        libraries = await list_libraries(session)
        stats = await load_library_stats(session)
    dtos = [
        library_view_dto(ctx, lib, stats.get(lib.id), await _cover_tag(lib.id))
        for lib in libraries
    ]
    return JSONResponse(query_result(dtos, len(dtos)))


def _refresh_status(library_id: int) -> tuple[str, float | None]:
    """把本库的扫描/元数据刷新任务线映射到 Jellyfin 的三态语义（LibraryManager.cs:1586-1589）。

    真实现：有进度值 → Active，在队列里 → Queued，其余 → Idle；RefreshProgress
    是 0~100 的百分数，非 Active 时为 null（省略输出）。我们两条任务线
    （scan 扫描 + media_scrape 整库元数据刷新）任一在跑即 Active——分母未知的
    遍历阶段报 0.0（客户端画不确定态转圈，与自家前端约定一致）；元数据刷新
    "已启动但状态尚未就绪"的间隙映射为 Queued。
    """
    from movieclaw_api.services import media_scrape
    from movieclaw_api.services.library.scan import scan_progress

    for state in (scan_progress(library_id), media_scrape.library_refresh_state(library_id)):
        if state is not None:
            if state.total > 0:
                return "Active", round(state.processed / state.total * 100, 1)
            return "Active", 0.0
    if media_scrape.is_library_refreshing(library_id):
        return "Queued", None
    return "Idle", None


@router.get("/Library/VirtualFolders")
async def library_virtual_folders() -> JSONResponse:
    """媒体库物理结构（LibraryStructureController.cs:58-63）。Infuse 在 GroupingOptions 之后必调。

    真 Jellyfin 返回**裸数组**的 VirtualFolderInfo：库名、物理路径、类型与库配置。
    我们把 Library.root_paths 原样给出（第一个为主根），LibraryOptions 按真实现的
    实体默认值给一份静态子集——客户端只读，我们不开放库管理写端点。
    """
    async with get_database().session() as session:
        libraries = await list_libraries(session)
    infos = []
    for lib in libraries:
        roots = [str(p) for p in (lib.root_paths or [])]
        status, progress = _refresh_status(lib.id)
        infos.append(
            {
                "Name": lib.name,
                "Locations": roots,
                "CollectionType": "movies" if lib.kind == "movie" else "tvshows",
                "ItemId": library_guid(lib.id),
                "RefreshStatus": status,
                "LibraryOptions": {
                    "Enabled": True,
                    "EnablePhotos": False,
                    "EnableRealtimeMonitor": True,
                    "EnableChapterImageExtraction": False,
                    "ExtractChapterImagesDuringLibraryScan": False,
                    "EnableTrickplayImageExtraction": False,
                    "ExtractTrickplayImagesDuringLibraryScan": False,
                    "PathInfos": [{"Path": p} for p in roots],
                    "SaveLocalMetadata": False,
                    "EnableAutomaticSeriesGrouping": False,
                    "EnableEmbeddedTitles": False,
                    "EnableEmbeddedExtrasTitles": False,
                    "EnableEmbeddedEpisodeInfos": False,
                    "AutomaticRefreshIntervalDays": 0,
                    "SeasonZeroDisplayName": "Specials",
                    "DisabledLocalMetadataReaders": [],
                    "DisabledSubtitleFetchers": [],
                    "SubtitleFetcherOrder": [],
                },
            }
        )
        # 对齐真实现：RefreshProgress 仅 Active 时输出（可空 double 的 null 省略约定）
        if progress is not None:
            infos[-1]["RefreshProgress"] = progress
    return JSONResponse(infos)


@router.get("/UserViews/GroupingOptions")
@router.get("/Users/{user_id}/GroupingOptions")
async def user_view_grouping_options(user_id: str | None = None) -> JSONResponse:
    """可分组视图清单（UserViewsController.cs:128-155）。Infuse 握手必调，缺了会中断添加流程。

    真 Jellyfin 取用户根目录下 CollectionType ∈ {movies, tvshows, null} 的文件夹
    （UserView.IsEligibleForGrouping）——我们的库只有 movies/tvshows 两种，全部符合。
    注意返回形状是**裸数组**（不套 QueryResult），元素仅 Name + Id（Guid "N" 格式，
    与 library_guid 的 32 位 hex 一致），按 Name 排序。
    """
    async with get_database().session() as session:
        libraries = await list_libraries(session)
    options = sorted(
        ({"Name": lib.name, "Id": library_guid(lib.id)} for lib in libraries),
        key=lambda option: option["Name"],
    )
    return JSONResponse(options)


# ---------------------------------------------------------------------------
# /Items 查询管线
# ---------------------------------------------------------------------------


def _entry_played(entry: Entry) -> bool:
    kind, bundle, season, _ = entry
    if kind in ("Movie", "Episode"):
        st = bundle.state(entry[2], entry[3])
        return bool(st and st.played)
    units = [u for u in bundle.units if kind != "Season" or u[0] == season]
    if not units:
        return True
    return all(bool((st := bundle.state(*u)) and st.played) for u in units)


def _entry_resumable(entry: Entry) -> bool:
    kind, bundle, season, episode = entry
    if kind not in ("Movie", "Episode"):
        return False
    st = bundle.state(season, episode)
    return bool(st and st.position_ms > 0)


def _entry_favorite(entry: Entry) -> bool:
    kind, bundle, season, episode = entry
    if kind == "Series":
        st = bundle.state(-1, -1)
    elif kind == "Season":
        st = bundle.state(season, -1)
    else:
        st = bundle.state(season, episode)
    return bool(st and st.is_favorite)


def _search_match(bundle: ItemBundle, term: str) -> bool:
    lowered = term.lower()
    candidates = [bundle.item.title, bundle.item.original_title]
    candidates.extend(bundle.item.aliases or [])
    return any(lowered in (c or "").lower() for c in candidates)


def _build_entries(
    bundles: dict[int, ItemBundle],
    types: set[str],
    *,
    season_scope: int | None = None,
    episode_scope: int | None = None,
) -> list[Entry]:
    entries: list[Entry] = []
    for bundle in bundles.values():
        if bundle.item.kind == "movie":
            if "Movie" in types and (0, 0) in bundle.files:
                entries.append(("Movie", bundle, 0, 0))
            continue
        if "Series" in types:
            entries.append(("Series", bundle, 0, 0))
        if "Season" in types:
            for season in sorted({s for s, _ in bundle.units}):
                if season_scope is None or season == season_scope:
                    entries.append(("Season", bundle, season, 0))
        if "Episode" in types:
            for season, episode in bundle.units:
                if season_scope is not None and season != season_scope:
                    continue
                if episode_scope is not None and episode != episode_scope:
                    continue
                entries.append(("Episode", bundle, season, episode))
    return entries


def _parse_int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _entry_sort_value(name: str, entry: Entry):
    """排序键直接从数据取（不受 fields/enableUserData 门控影响）。"""
    kind, bundle, season, episode = entry
    if name in ("SortName", "Name"):
        if kind == "Episode":
            row = bundle.episodes.get((season, episode))
            return ((row.name if row else "") or f"{season:04d}{episode:04d}").lower()
        if kind == "Season":
            return f"{season:04d}"
        return bundle.item.title.lower()
    if name == "ProductionYear":
        return bundle.item.year or 0
    if name == "PremiereDate":
        if kind == "Episode":
            row = bundle.episodes.get((season, episode))
            return (row.air_date.isoformat() if row and row.air_date else "")
        meta = bundle.metadata
        return meta.release_date.isoformat() if meta and meta.release_date else ""
    if name == "CommunityRating":
        if kind == "Episode":
            row = bundle.episodes.get((season, episode))
            return row.vote_average or 0 if row else 0
        return (bundle.metadata.vote_average if bundle.metadata else 0) or 0
    if name == "Runtime":
        return bundle.unit_runtime_ms(season, episode) or 0
    if name == "DateCreated":
        units = [(season, episode)] if kind in ("Movie", "Episode") else bundle.units
        stamps = [
            f.created_at for u in units for f in bundle.files.get(u, [])
        ]
        return max(stamps).isoformat() if stamps else ""
    if name == "DatePlayed":
        units = [(season, episode)] if kind in ("Movie", "Episode") else bundle.units
        stamps = [
            st.last_played_at
            for u in units
            if (st := bundle.state(*u)) and st.last_played_at
        ]
        return max(stamps).isoformat() if stamps else ""
    if name in ("ParentIndexNumber", "AiredEpisodeOrder", "IndexNumber"):
        return (season, episode)
    return None


_SORTABLE = {
    "SortName", "Name", "ProductionYear", "PremiereDate", "CommunityRating",
    "Runtime", "DateCreated", "DatePlayed", "ParentIndexNumber",
    "AiredEpisodeOrder", "IndexNumber",
}


def _sort_entries(entries: list[Entry], sort_by: list[str], sort_orders: list[str]) -> list[Entry]:
    if not sort_by:
        return entries
    if sort_by[0] == "Random":
        import random

        shuffled = entries[:]
        random.shuffle(shuffled)
        return shuffled
    result = entries
    # 从次要键到主键逐轮稳定排序；未知键静默忽略（枚举宽容语义）
    for pos in range(len(sort_by) - 1, -1, -1):
        name = sort_by[pos]
        if name not in _SORTABLE:
            continue
        order = (
            sort_orders[pos]
            if pos < len(sort_orders)
            else (sort_orders[-1] if sort_orders else "Ascending")
        )
        result = sorted(
            result,
            key=lambda e: _entry_sort_value(name, e),
            reverse=order.lower().startswith("desc"),
        )
    return result


def _entry_dto(ctx: DtoContext, entry: Entry, options: DtoOptions) -> dict[str, Any]:
    kind, bundle, season, episode = entry
    if kind == "Movie":
        return movie_dto(ctx, bundle, options)
    if kind == "Series":
        return series_dto(ctx, bundle, options)
    if kind == "Season":
        return season_dto(ctx, bundle, season, options)
    return episode_dto(ctx, bundle, season, episode, options)


async def _query_items(request: Request) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    include_types = set(parse_comma(q.get("includeItemTypes")))
    exclude_types = set(parse_comma(q.get("excludeItemTypes")))
    recursive = parse_bool(q.get("recursive"))
    parent_raw = q.get("parentId")
    ids_raw = parse_comma(q.get("ids"))
    search_term = q.get("searchTerm")

    async with get_database().session() as session:
        if ids_raw:
            entries = await _entries_for_ids(session, ids_raw)
        else:
            entries = await _entries_for_parent(
                session,
                parent_raw,
                include_types=include_types,
                recursive=recursive,
                has_search=bool(search_term),
            )
            if entries is None:
                # 根级：返回视图列表
                libraries = await list_libraries(session)
                stats = await load_library_stats(session)
                dtos = [
                    library_view_dto(
                        ctx, lib, stats.get(lib.id), await _cover_tag(lib.id)
                    )
                    for lib in libraries
                ]
                return JSONResponse(query_result(dtos, len(dtos)))

        person_ids_raw = parse_comma(q.get("personIds"))
        if person_ids_raw:
            member_ids = await _items_with_persons(session, person_ids_raw)
            entries = [e for e in entries if e[1].item.id in member_ids]

    if exclude_types:
        entries = [e for e in entries if e[0] not in exclude_types]

    # ---- 过滤 -------------------------------------------------------------
    if search_term:
        entries = [e for e in entries if _search_match(e[1], search_term)]
    years = {int(y) for y in parse_comma(q.get("years")) if y.isdigit()}
    if years:
        entries = [e for e in entries if e[1].item.year in years]
    genres = set(parse_pipe(q.get("genres")))
    if genres:
        entries = [
            e
            for e in entries
            if e[1].metadata and genres & set(e[1].metadata.genres or [])
        ]
    ratings = set(parse_pipe(q.get("officialRatings")))
    if ratings:
        entries = [
            e for e in entries if e[1].metadata and e[1].metadata.content_rating in ratings
        ]

    filters = set(parse_comma(q.get("filters")))
    is_played = parse_bool(q.get("isPlayed"))
    if "IsPlayed" in filters or is_played is True:
        entries = [e for e in entries if _entry_played(e)]
    if "IsUnplayed" in filters or is_played is False:
        entries = [e for e in entries if not _entry_played(e)]
    if "IsResumable" in filters:
        entries = [e for e in entries if _entry_resumable(e)]
    if "IsFavorite" in filters or parse_bool(q.get("isFavorite")) is True:
        entries = [e for e in entries if _entry_favorite(e)]

    # ---- 排序 / 分页 / DTO -------------------------------------------------
    entries = _sort_entries(
        entries, parse_comma(q.get("sortBy")), parse_comma(q.get("sortOrder"))
    )
    total = len(entries)
    start_index = _parse_int(q.get("startIndex"))
    limit = _parse_int(q.get("limit"), default=-1)
    page = entries[start_index : start_index + limit] if limit >= 0 else entries[start_index:]
    dtos = [_entry_dto(ctx, e, options) for e in page]
    return JSONResponse(query_result(dtos, total, start_index))


async def _entries_for_ids(session: AsyncSession, ids_raw: list[str]) -> list[Entry]:
    refs = [r for r in (decode_guid(i) for i in ids_raw) if r is not None]
    scoped = {r.entity_id for r in refs if r.kind != EntityKind.LIBRARY}
    bundles = await load_bundles(session, list(scoped))
    entries: list[Entry] = []
    for ref in refs:
        bundle = bundles.get(ref.entity_id)
        if bundle is None:
            continue
        if ref.kind == EntityKind.ITEM:
            entries.append(
                ("Movie" if bundle.item.kind == "movie" else "Series", bundle, 0, 0)
            )
        elif ref.kind == EntityKind.SEASON:
            entries.append(("Season", bundle, ref.season, 0))
        elif ref.kind == EntityKind.EPISODE:
            entries.append(("Episode", bundle, ref.season, ref.episode))
    return entries


async def _entries_for_parent(
    session: AsyncSession,
    parent_raw: str | None,
    *,
    include_types: set[str],
    recursive: bool | None,
    has_search: bool,
) -> list[Entry] | None:
    """按 parentId 语义展开候选。返回 None 表示"根级 → 视图列表"。"""
    if not parent_raw or is_empty_guid(parent_raw):
        if has_search or include_types:
            # 无 parent 的全局搜索/类型查询：跨全部库递归
            types = include_types or {"Movie", "Series"}
            ids = await item_ids_with_files(session)
            bundles = await load_bundles(session, ids)
            return _build_entries(bundles, types)
        return None

    ref = decode_guid(parent_raw)
    if ref is None:
        raise not_found()
    if ref.kind == EntityKind.FIXED and ref.entity_id == FIXED_ROOT:
        return None  # 根文件夹 → 视图列表

    if ref.kind == EntityKind.LIBRARY:
        library = await session.get(Library, ref.entity_id)
        if library is None:
            raise not_found()
        # 自动递归特例（ItemsController.cs:335-340）：库 + includeItemTypes + 未传 recursive
        effective_recursive = recursive
        if include_types and recursive is None:
            effective_recursive = True
        default_types = {"Movie"} if library.kind == "movie" else {"Series"}
        all_types = (
            {"Movie"} if library.kind == "movie" else {"Series", "Season", "Episode"}
        )
        if effective_recursive:
            types = include_types or all_types
        else:
            # 非递归 = 只看直接子级，includeItemTypes 在其上做过滤（可为空集）
            types = (include_types & default_types) if include_types else default_types
        ids = await item_ids_with_files(session, library_id=ref.entity_id)
        bundles = await load_bundles(session, ids, library_id=ref.entity_id)
        return _build_entries(bundles, types)

    if ref.kind == EntityKind.ITEM:
        bundles = await load_bundles(session, [ref.entity_id])
        types = include_types or {"Season"}
        return _build_entries(bundles, types)

    if ref.kind == EntityKind.SEASON:
        bundles = await load_bundles(session, [ref.entity_id])
        return _build_entries(bundles, {"Episode"}, season_scope=ref.season)

    raise not_found()


@router.get("/Items")
@router.get("/Users/{user_id}/Items")
async def get_items(request: Request, user_id: str | None = None) -> JSONResponse:
    return await _query_items(request)


# ---------------------------------------------------------------------------
# Latest / Resume / Root（须在 /Items/{item_id} 之前注册）
# ---------------------------------------------------------------------------


@router.get("/Items/Latest")
@router.get("/Users/{user_id}/Items/Latest")
async def items_latest(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(default=20),
    groupItems: bool = Query(default=True),  # noqa: N803 —— 对齐协议参数名
) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    parent_ref = decode_guid(q.get("parentId") or "") if q.get("parentId") else None
    library_id = (
        parent_ref.entity_id
        if parent_ref and parent_ref.kind == EntityKind.LIBRARY
        else None
    )
    is_played = parse_bool(q.get("isPlayed"))
    if is_played is None:
        is_played = False  # HidePlayedInLatest=True 的默认语义

    async with get_database().session() as session:
        ids = await item_ids_with_files(session, library_id=library_id)
        bundles = await load_bundles(session, ids, library_id=library_id)

    # 每个"最新单元"= 文件入库时间最大的 (bundle, season, episode)
    latest_units: list[tuple[Any, ItemBundle, int, int]] = []
    for bundle in bundles.values():
        for (season, episode), files in bundle.files.items():
            created = max(f.created_at for f in files)
            latest_units.append((created, bundle, season, episode))
    latest_units.sort(key=lambda t: t[0], reverse=True)

    dtos: list[dict[str, Any]] = []
    grouped_series: dict[int, int] = {}
    for _created, bundle, season, episode in latest_units:
        if len(dtos) >= limit:
            break
        if bundle.item.kind == "movie":
            entry: Entry = ("Movie", bundle, 0, 0)
            if is_played is not None and _entry_played(entry) is not is_played:
                continue
            dtos.append(movie_dto(ctx, bundle, options))
            continue
        # 剧集：两态简化（设计文档偏离⑥）——同剧多集新入库聚合为 Series
        entry = ("Episode", bundle, season, episode)
        if is_played is not None and _entry_played(entry) is not is_played:
            continue
        if not groupItems:
            dtos.append(episode_dto(ctx, bundle, season, episode, options))
            continue
        if bundle.item.id in grouped_series:
            grouped_series[bundle.item.id] += 1
            continue
        grouped_series[bundle.item.id] = 1
        dtos.append(episode_dto(ctx, bundle, season, episode, options))

    if groupItems:
        # 同剧 ≥2 个新单元 → 用 Series 条目替换该剧的 Episode 占位
        for i, dto in enumerate(dtos):
            if dto.get("Type") != "Episode":
                continue
            ref = decode_guid(dto["SeriesId"])
            if ref and grouped_series.get(ref.entity_id, 0) > 1:
                bundle = bundles[ref.entity_id]
                series = series_dto(ctx, bundle, options)
                series["ChildCount"] = grouped_series[ref.entity_id]
                dtos[i] = series

    return JSONResponse(dtos)


@router.get("/UserItems/Resume")
@router.get("/Users/{user_id}/Items/Resume")
async def items_resume(request: Request, user_id: str | None = None) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    media_types = set(parse_comma(q.get("mediaTypes")))

    async with get_database().session() as session:
        ids = await item_ids_with_files(session)
        bundles = await load_bundles(session, ids)

    entries = _build_entries(bundles, {"Movie", "Episode"})
    if media_types and "Video" not in media_types:
        entries = []
    # 服务端强制语义：可续播 = 位置 > 0；按最近播放降序（客户端不可覆盖）
    resumable = [e for e in entries if _entry_resumable(e)]
    resumable.sort(
        key=lambda e: e[1].state(e[2], e[3]).last_played_at or e[1].item.created_at,
        reverse=True,
    )

    total = len(resumable)
    start_index = _parse_int(q.get("startIndex"))
    limit = _parse_int(q.get("limit"), default=-1)
    page = (
        resumable[start_index : start_index + limit]
        if limit >= 0
        else resumable[start_index:]
    )
    dtos = [_entry_dto(ctx, e, options) for e in page]
    return JSONResponse(query_result(dtos, total, start_index))


@router.get("/Items/Root")
@router.get("/Users/{user_id}/Items/Root")
async def items_root(user_id: str | None = None) -> JSONResponse:
    from movieclaw_jellyfin.ids import root_guid

    ctx = await dto_context()
    return JSONResponse(
        {
            "Name": "Media Folders",
            "ServerId": ctx.server_id,
            "Id": root_guid(),
            "Type": "UserRootFolder",
            "MediaType": "Unknown",
            "IsFolder": True,
        }
    )


@router.get("/Items/Counts")
async def items_counts(request: Request) -> JSONResponse:
    """全服统计（LibraryController.cs:453，ItemCounts 的 12 个非可空计数）。

    播放器的服务器/媒体库卡片用它显示"多少部电影、多少部剧"。"""
    async with get_database().session() as session:
        movie_ids = await item_ids_with_files(session, kind="movie")
        tv_ids = await item_ids_with_files(session, kind="tv")
        episode_count = 0
        if tv_ids:
            from sqlalchemy import select as sa_select

            from movieclaw_db.models import LibraryFile

            units = (
                await session.execute(
                    sa_select(
                        LibraryFile.media_item_id,
                        LibraryFile.season_number,
                        LibraryFile.episode_number,
                    )
                    .where(
                        LibraryFile.media_item_id.in_(tv_ids),
                        LibraryFile.missing_since.is_(None),
                    )
                    .distinct()
                )
            ).all()
            episode_count = len(units)
    return JSONResponse(
        {
            "MovieCount": len(movie_ids),
            "SeriesCount": len(tv_ids),
            "EpisodeCount": episode_count,
            "ArtistCount": 0,
            "ProgramCount": 0,
            "TrailerCount": 0,
            "SongCount": 0,
            "AlbumCount": 0,
            "MusicVideoCount": 0,
            "BoxSetCount": 0,
            "BookCount": 0,
            "ItemCount": len(movie_ids) + len(tv_ids) + episode_count,
        }
    )


@router.get("/Items/Filters")
@router.get("/Items/Filters2")
async def items_filters(request: Request) -> JSONResponse:
    """筛选面板兜底：给空结构，客户端隐藏筛选项（P2）。"""
    if request.url.path.endswith("Filters2"):
        return JSONResponse({"Genres": [], "Tags": []})
    return JSONResponse({"Genres": [], "Tags": [], "OfficialRatings": [], "Years": []})


# ---------------------------------------------------------------------------
# 单条目（全字段语义，无 fields 参数）
# ---------------------------------------------------------------------------


async def _overlay_layered_meta(dto: dict[str, Any], bundle: ItemBundle) -> None:
    """单条目详情叠加分层元数据（与 Web 详情页同一份读策略，layered_item_meta）。

    列表装配只读库内档案（批量性能不容 NFO 磁盘 IO 与 TMDB 兜底）；点进
    详情的这一条走完整分层——NFO 里人工维护的简介优先生效，还没刮过的
    条目当场用 TMDB 兜底填充文本（后台自愈刮削由分层读内部触发）。
    People 不叠加：人物页要靠关系表里的影人 id，NFO/TMDB 兜底给不出稳定
    id，缺口由自愈刮削收敛。库内档案来源与 DTO 同源，无需二次装配。
    """
    from movieclaw_api.services.library.items import layered_item_meta, resolve_entry_dirs
    from movieclaw_media.models import MediaKind

    async with get_database().session() as session:
        rows = (
            await session.execute(
                select_files_with_roots(bundle.item.id)  # type: ignore[arg-type]
            )
        ).all()
        if not rows:
            return
        files = [f for f, _ in rows]
        roots: list[Path] = []
        for _, lib in rows:
            for p in lib.root_paths:
                path = Path(p)
                if path not in roots:
                    roots.append(path)
        entry_dirs = resolve_entry_dirs(roots, files)
        meta = await layered_item_meta(
            session, bundle.item, entry_dirs, files, MediaKind(bundle.item.kind)
        )
    if meta is None or meta.source not in ("nfo", "tmdb"):
        return
    if meta.plot:
        dto["Overview"] = meta.plot
    if meta.rating:
        dto["CommunityRating"] = meta.rating
    if meta.genres:
        dto["Genres"] = list(meta.genres)


def select_files_with_roots(media_item_id: int):
    """条目的在册文件 + 所属库（取根路径用），单条目详情与图片接口同款联查。"""
    from sqlalchemy import select as sa_select

    from movieclaw_db.models import LibraryFile

    return (
        sa_select(LibraryFile, Library)
        .join(Library, Library.id == LibraryFile.library_id)
        .where(
            LibraryFile.media_item_id == media_item_id,
            LibraryFile.missing_since.is_(None),
        )
    )


@router.get("/Items/{item_id}")
@router.get("/Users/{user_id}/Items/{item_id}")
async def get_item(request: Request, item_id: str, user_id: str | None = None) -> JSONResponse:
    ctx = await dto_context()
    if is_empty_guid(item_id):
        return await items_root()
    ref = decode_guid(item_id)
    if ref is None:
        raise not_found()
    options = DtoOptions(all_fields=True)

    async with get_database().session() as session:
        if ref.kind == EntityKind.PERSON:
            from movieclaw_db.models import Person

            person = await session.get(Person, ref.entity_id)
            if person is None:
                raise not_found()
            import hashlib

            dto: dict[str, Any] = {
                "Name": person.name,
                "ServerId": ctx.server_id,
                "Id": item_id.lower().replace("-", ""),
                "Type": "Person",
                "MediaType": "Unknown",
                "IsFolder": False,
                "ImageTags": (
                    {"Primary": hashlib.md5(person.profile_path.encode()).hexdigest()}
                    if person.profile_path
                    else {}
                ),
                "BackdropImageTags": [],
            }
            if person.original_name and person.original_name != person.name:
                dto["OriginalTitle"] = person.original_name
            return JSONResponse(dto)
        if ref.kind == EntityKind.LIBRARY:
            library = await session.get(Library, ref.entity_id)
            if library is None:
                raise not_found()
            stats = await load_library_stats(session)
            return JSONResponse(
                library_view_dto(
                    ctx, library, stats.get(library.id), await _cover_tag(library.id)
                )
            )
        bundles = await load_bundles(session, [ref.entity_id])

    bundle = bundles.get(ref.entity_id)
    if bundle is None:
        raise not_found()
    # 自愈刮削的第二触发条件：档案里有 cast 但影人关系表为空——影人功能
    # 上线前刮的存量条目，cast 非空证明 TMDB 有数据，补刮一次关系落库后
    # 条件即不再成立（收敛，不会对"确实没有演职员"的条目反复触发）。
    # 第一触发条件（档案缺失/从未刮过）由 _overlay_layered_meta 里的分层读
    # 内部触发（与网页详情完全同一份逻辑），这里不重复。只挂单条目详情、
    # 不挂列表查询：列表一次装配几十条，逐条自愈会放大成 TMDB 请求风暴。
    needs_heal = (
        bundle.metadata is not None
        and bundle.metadata.scraped_at is not None
        and not bundle.people
        and bool(bundle.metadata.cast)
    )
    if needs_heal:
        from movieclaw_api.services.media_scrape import scrape_media_item

        assert bundle.item.id is not None
        asyncio.get_running_loop().create_task(scrape_media_item(bundle.item.id))
    if ref.kind == EntityKind.ITEM:
        dto = (
            movie_dto(ctx, bundle, options)
            if bundle.item.kind == "movie"
            else series_dto(ctx, bundle, options)
        )
        await _overlay_layered_meta(dto, bundle)
    elif ref.kind == EntityKind.SEASON:
        dto = season_dto(ctx, bundle, ref.season, options)
    elif ref.kind == EntityKind.EPISODE:
        if (ref.season, ref.episode) not in bundle.files:
            raise not_found()
        dto = episode_dto(ctx, bundle, ref.season, ref.episode, options)
    else:
        raise not_found()
    return JSONResponse(dto)


@router.get("/Items/{item_id}/Similar")
async def items_similar(item_id: str) -> JSONResponse:
    return JSONResponse(query_result([], 0))


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------


@router.get("/Shows/NextUp")
async def shows_next_up(request: Request) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    series_filter = decode_guid(q.get("seriesId") or "") if q.get("seriesId") else None

    async with get_database().session() as session:
        ids = await item_ids_with_files(session, kind="tv")
        bundles = await load_bundles(session, ids)

    candidates: list[tuple[Any, dict[str, Any]]] = []
    for bundle in bundles.values():
        if series_filter and bundle.item.id != series_filter.entity_id:
            continue
        # 已看判定排除 0 季
        watched = [
            (u, st)
            for u in bundle.units
            if u[0] != 0 and (st := bundle.state(*u)) and (st.played or st.position_ms > 0)
        ]
        if not watched:
            continue
        last_activity = max(
            (st.last_played_at for _, st in watched if st.last_played_at),
            default=None,
        )
        # 锚点 = 最近活动（last_played_at 最新）的单元：
        # 锚点未看完 → 锚点本身（enableResumable 语义）；已看完 → 其后第一集
        fallback_stamp = bundle.item.created_at
        anchor_unit, anchor_state = max(
            watched, key=lambda pair: pair[1].last_played_at or fallback_stamp
        )
        if anchor_state.position_ms > 0 and not anchor_state.played:
            next_unit = anchor_unit
        else:
            following = [u for u in bundle.units if u[0] != 0 and u > anchor_unit]
            if not following:
                continue
            next_unit = min(following)
        candidates.append(
            (
                last_activity or bundle.item.created_at,
                episode_dto(ctx, bundle, next_unit[0], next_unit[1], options),
            )
        )

    candidates.sort(key=lambda t: t[0], reverse=True)
    start_index = _parse_int(q.get("startIndex"))
    limit = _parse_int(q.get("limit"), default=-1)
    dtos = [dto for _, dto in candidates]
    page = dtos[start_index : start_index + limit] if limit >= 0 else dtos[start_index:]
    return JSONResponse(query_result(page, len(dtos), start_index))


@router.get("/Shows/{series_id}/Seasons")
async def shows_seasons(request: Request, series_id: str) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    ref = decode_guid(series_id)
    if ref is None or ref.kind != EntityKind.ITEM:
        raise not_found()
    async with get_database().session() as session:
        bundles = await load_bundles(session, [ref.entity_id])
    bundle = bundles.get(ref.entity_id)
    if bundle is None or bundle.item.kind != "tv":
        raise not_found()

    seasons = sorted({s for s, _ in bundle.units})
    if parse_bool(q.get("isSpecialSeason")) is False:
        seasons = [s for s in seasons if s != 0]
    dtos = [season_dto(ctx, bundle, s, options) for s in seasons]
    return JSONResponse(query_result(dtos, len(dtos)))


@router.get("/Shows/{series_id}/Episodes")
async def shows_episodes(request: Request, series_id: str) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    season_scope: int | None = None
    season_id = q.get("seasonId")
    if season_id:
        # seasonId 优先于 season 号（该分支不校验 seriesId，对齐源码）
        season_ref = decode_guid(season_id)
        if season_ref is None or season_ref.kind != EntityKind.SEASON:
            raise not_found_message(f"No season exists with Id {season_id}")
        target_item_id = season_ref.entity_id
        season_scope = season_ref.season
    else:
        ref = decode_guid(series_id)
        if ref is None or ref.kind != EntityKind.ITEM:
            raise not_found_message("Series not found")
        target_item_id = ref.entity_id
        season_param = q.get("season")
        if season_param is not None and season_param.lstrip("-").isdigit():
            season_scope = int(season_param)

    async with get_database().session() as session:
        bundles = await load_bundles(session, [target_item_id])
    bundle = bundles.get(target_item_id)
    if bundle is None or bundle.item.kind != "tv":
        raise not_found_message("Series not found")
    if season_id and season_scope not in {s for s, _ in bundle.units}:
        # seasonId 指向不存在（无文件）的季 → 404（对齐 TvShowsController.cs:238）
        raise not_found_message(f"No season exists with Id {season_id}")

    units = [
        u
        for u in bundle.units
        if season_scope is None or u[0] == season_scope
    ]
    dtos = [episode_dto(ctx, bundle, s, e, options) for s, e in units]
    if q.get("sortBy") == "Random":
        import random

        random.shuffle(dtos)

    total = len(dtos)
    start_index = _parse_int(q.get("startIndex"))
    limit = _parse_int(q.get("limit"), default=-1)
    page = dtos[start_index : start_index + limit] if limit >= 0 else dtos[start_index:]
    return JSONResponse(query_result(page, total, start_index))
