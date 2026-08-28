"""媒体库浏览接口（设计文档 §5）：Views / Items / Shows / Resume / NextUp / Latest。

路由注册顺序即匹配顺序：/Items 下的字面路径（Latest/Root/Filters…）必须先于
/Items/{item_id} 注册。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from movieclaw_api.services.library.access import member_visible_ids
from movieclaw_db.engine import get_database
from movieclaw_db.models import Library
from movieclaw_jellyfin.catalog import (
    DtoContext,
    DtoOptions,
    ItemBundle,
    LatestUnitCandidate,
    ResumeUnitCandidate,
    episode_dto,
    hydrate_leaves,
    item_ids_with_files,
    latest_unit_candidates,
    library_view_dto,
    list_libraries,
    load_bundles,
    movie_dto,
    movie_library_page,
    next_up_item_ids,
    person_dto,
    query_persons,
    resume_unit_candidates,
    search_candidate_item_ids,
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
from movieclaw_jellyfin.search import SearchTerm, parse_term
from movieclaw_jellyfin.security import RequestIdentity, require_device

router = APIRouter(dependencies=[Depends(require_device)])


async def _item_visible(session: AsyncSession, item_id: int, scope: ViewerScope) -> bool:
    """条目对该观看者是否可见：在其可见库里有任一在位文件。

    直接按条目 GUID 访问（/Items/{id}、Shows/*、ids= 查询）不经过库枚举，
    必须单独判定——GUID 是可枚举的结构化编码，只挡浏览不挡直达等于没挡。
    """
    if scope.visible is None:
        return True
    from sqlalchemy import select as sa_select

    from movieclaw_db.models import LibraryFile

    row = (
        await session.execute(
            sa_select(LibraryFile.id)
            .where(
                LibraryFile.media_item_id == item_id,
                LibraryFile.in_place(),
                LibraryFile.library_id.in_(scope.visible),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


@dataclass(frozen=True)
class ViewerScope:
    """一次请求的观看者范围：身份 + 可见库（None=不受限）。

    多用户投影的两个自由度都在这里：``member_id`` 决定 UserData（进度/
    已看/收藏）装谁的行，``visible`` 决定库/条目枚举的范围。
    """

    member_id: int
    visible: set[int] | None

    def library_hidden(self, library_id: int) -> bool:
        return self.visible is not None and library_id not in self.visible


async def viewer_scope(
    identity: RequestIdentity = Depends(require_device),
) -> ViewerScope:
    """从设备凭据解析观看者范围（require_device 有依赖缓存，不重复查库）。"""
    member_id = identity.device.member_id
    if member_id == 0:
        return ViewerScope(0, None)
    async with get_database().session() as session:
        visible = await member_visible_ids(session, member_id)
    return ViewerScope(member_id, visible)

# 这些排序键要读**每一个候选条目**的文件行（入库时间 / 时长），骨架不够用
_FULL_LEAF_SORTS = {"DateCreated", "Runtime"}
# 这些排序/筛选口径在候选里含 Episode 时要读**每一集**的分集元数据
_EPISODE_LEAF_SORTS = {"SortName", "Name", "PremiereDate", "CommunityRating", "Runtime"}


@dataclass
class _LazyLeaves:
    """列表查询的"两段式装载"联络簿。

    列表接口的筛选、排序、分页只需要"哪些单元有文件"和播放状态；真正要
    文件行和分集元数据的只有最后那一页的叶子条目。装载侧据此决定能不能只
    建骨架（``leaf_scope=set()``），能就把 ``used`` 回填给调用方，调用方选完
    页再回来补这一页的料。判定放在装载侧是因为"候选里到底会不会出现
    Episode"要看库类型和 recursive 特例，只有那里才知道。
    """

    allowed: bool
    """排序键不依赖每个条目的文件行时为 True。"""

    episode_sensitive: bool
    """排序/搜索口径会读分集元数据（候选含 Episode 时不能只建骨架）。"""

    used: bool = False
    library_id: int | None = None

    def scope_for(self, types: set[str]):
        """返回该批候选可用的 ``leaf_scope``；None = 老路径全量装载。"""
        if not self.allowed:
            return None
        if "Episode" in types and self.episode_sensitive:
            return None
        return set()


# (type, payload, season, episode)：查询管线里的一条候选。
# payload 通常是 ItemBundle；type == "Person" 时是 Person 行——人物是
# ItemsByName，没有文件/季集/播放状态，凑不出 bundle，也不参与媒体侧筛选与排序。
Entry = tuple[str, Any, int, int]


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
async def user_views(
    user_id: str | None = None,
    scope: ViewerScope = Depends(viewer_scope),
) -> JSONResponse:
    ctx = await dto_context()
    async with get_database().session() as session:
        libraries = await list_libraries(session, visible_ids=scope.visible)
    dtos = [
        library_view_dto(ctx, lib, await _cover_tag(lib.id))
        for lib in libraries
    ]
    return JSONResponse(query_result(dtos, len(dtos)))


@router.get("/UserViews/GroupingOptions")
@router.get("/Users/{user_id}/GroupingOptions")
async def user_views_grouping_options(
    user_id: str | None = None,
    scope: ViewerScope = Depends(viewer_scope),
) -> JSONResponse:
    """可分组视图清单（issue #124，Infuse 添加媒体库时请求）。

    对齐 UserViewsController.GetGroupingOptions：movies/tvshows 库天然可
    分组（IsEligibleForGrouping），映射可见库、按名称排序；legacy 路由
    /Users/{userId}/GroupingOptions 一并注册。
    """
    async with get_database().session() as session:
        libraries = await list_libraries(session, visible_ids=scope.visible)
    return JSONResponse(
        [
            {"Name": lib.name, "Id": library_guid(lib.id)}
            for lib in sorted(libraries, key=lambda lib: lib.name)
        ]
    )


def _refresh_status(library_id: int) -> tuple[str, float | None]:
    """把本库的扫描/元数据刷新任务线映射到 Jellyfin 的三态语义（LibraryManager.cs）。

    真实现：有进度值 → Active，在队列里 → Queued，其余 → Idle；RefreshProgress
    是 0~100 的百分数，非 Active 时为 null（省略输出）。我们两条任务线
    （scan 扫描 + media_scrape 整库元数据刷新）任一在跑即 Active——分母未知的
    遍历阶段报 0.0（客户端画不确定态转圈）；元数据刷新"已启动但状态尚未就绪"
    的间隙映射为 Queued。
    """
    from movieclaw_api.services import media_scrape
    from movieclaw_api.services.library.scan import scan_progress

    for state in (
        scan_progress(library_id),
        media_scrape.library_refresh_state(library_id),
    ):
        if state is not None:
            if state.total > 0:
                return "Active", round(state.processed / state.total * 100, 1)
            return "Active", 0.0
    if media_scrape.is_library_refreshing(library_id):
        return "Queued", None
    return "Idle", None


@router.get("/Library/VirtualFolders")
async def library_virtual_folders(
    scope: ViewerScope = Depends(viewer_scope),
) -> JSONResponse:
    """媒体库 → VirtualFolderInfo 映射（issue #124，Infuse 添加媒体库时请求）。

    真 Jellyfin 此接口仅管理员可用（RequiresElevation）；这里放开给已认证
    设备（Infuse 普通链路也会请求），但成员设备只见白名单库、服务器文件
    系统路径只对主账号设备下发。LibraryOptions 按真实现的实体默认值给一份
    静态子集——客户端只读，我们不开放库管理写端点。
    """
    async with get_database().session() as session:
        libraries = await list_libraries(session, visible_ids=scope.visible)
    infos = []
    for lib in libraries:
        roots = [str(p) for p in (lib.root_paths or [])] if scope.member_id == 0 else []
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


def _entry_search_match(entry: Entry, term: SearchTerm) -> bool:
    """逐条目判定 searchTerm 是否命中——口径见 ``movieclaw_jellyfin.search``。

    按 Jellyfin 的语义，Season / Episode 是各自独立的 BaseItem，只比对**自己的
    Name**：剧名命中不会把它下面几十集一并带出来，反过来集名命中也能单独出现。
    """
    kind, bundle, season, episode = entry
    if kind == "Episode":
        row = bundle.episodes.get((season, episode))
        return term.matches(row.name if row else None)
    if kind == "Season":
        row = bundle.seasons.get(season)
        return term.matches(row.name if row else None)
    return term.matches(bundle.item.title, bundle.item.original_title)


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
    if kind == "Person":
        return person_dto(ctx, bundle)
    if kind == "Movie":
        return movie_dto(ctx, bundle, options)
    if kind == "Series":
        return series_dto(ctx, bundle, options)
    if kind == "Season":
        return season_dto(ctx, bundle, season, options)
    return episode_dto(ctx, bundle, season, episode, options)


async def _query_items(request: Request, scope: ViewerScope) -> JSONResponse:
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
    search_term = parse_term(q.get("searchTerm"))
    person_ids_raw = parse_comma(q.get("personIds"))
    start_index = _parse_int(q.get("startIndex"))
    limit = _parse_int(q.get("limit"), default=-1)
    sort_by = parse_comma(q.get("sortBy"))
    sort_order = parse_comma(q.get("sortOrder"))

    # 电影库的默认列表只需条目 id 和文件存在性；在没有筛选、排序和
    # personIds 时，数据库即可完成 count/offset/limit，最终页才水合 bundle。
    simple_movie_page = False
    simple_total = 0
    lazy: _LazyLeaves | None = None

    async with get_database().session() as session:
        page_entries: list[Entry] | None = None
        parent_ref = decode_guid(parent_raw or "") if parent_raw else None
        can_page_movies = (
            not ids_raw
            and parent_ref is not None
            and parent_ref.kind == EntityKind.LIBRARY
            and include_types in (set(), {"Movie"})
            and not exclude_types
            and not search_term
            and not parse_comma(q.get("years"))
            and not parse_pipe(q.get("genres"))
            and not parse_pipe(q.get("officialRatings"))
            and not parse_comma(q.get("filters"))
            and parse_bool(q.get("isPlayed")) is None
            and parse_bool(q.get("isFavorite")) is not True
            and not person_ids_raw
            and not sort_by
            and start_index >= 0
            and limit >= 0
        )
        if (
            parent_ref is not None
            and parent_ref.kind == EntityKind.LIBRARY
            # 库级可见性：白名单外与不存在同样 404（不泄露存在性）
            and scope.library_hidden(parent_ref.entity_id)
        ):
            raise not_found()
        if can_page_movies and parent_ref is not None:
            library = await session.get(Library, parent_ref.entity_id)
            can_page_movies = library is not None and library.kind == "movie"
        if can_page_movies and parent_ref is not None:
            simple_total, page_ids = await movie_library_page(
                session,
                parent_ref.entity_id,
                start_index=start_index,
                limit=limit,
            )
            bundles = await load_bundles(
                session,
                page_ids,
                member_id=scope.member_id,
                library_id=parent_ref.entity_id,
                visible_library_ids=scope.visible,
                dto_options=options,
                leaf_scope={(item_id, 0, 0) for item_id in page_ids},
            )
            page_entries = [
                ("Movie", bundles[item_id], 0, 0)
                for item_id in page_ids
                if item_id in bundles and (0, 0) in bundles[item_id].files
            ]
            simple_movie_page = True
        elif ids_raw:
            entries = await _entries_for_ids(session, ids_raw, scope, options=options)
        else:
            lazy = _LazyLeaves(
                allowed=not (set(sort_by) & _FULL_LEAF_SORTS),
                episode_sensitive=bool(search_term)
                or bool(set(sort_by) & _EPISODE_LEAF_SORTS),
            )
            entries = await _entries_for_parent(
                session,
                parent_raw,
                scope,
                include_types=include_types,
                recursive=recursive,
                search=search_term,
                options=options,
                lazy=lazy,
            )
            if entries is None:
                # 根级：返回视图列表
                libraries = await list_libraries(session, visible_ids=scope.visible)
                dtos = [
                    library_view_dto(ctx, lib, await _cover_tag(lib.id))
                    for lib in libraries
                ]
                return JSONResponse(query_result(dtos, len(dtos)))

        if not simple_movie_page and person_ids_raw:
            member_ids = await _items_with_persons(session, person_ids_raw)
            entries = [e for e in entries if e[1].item.id in member_ids]
        # 人物条目（ItemsByName）：Person 行不属于任何库、没有 TopParentId，
        # 真 Jellyfin 用 GetExemptedItemByNameTypes 把它豁免出库过滤
        # （BaseItemRepository.QueryBuilding.cs:504-513）——只有 includeItemTypes
        # 显式点名 Person 才产出，且不参与年份/类型/播放状态这些媒体侧筛选。
        # 这里在会话内查出来，拼接放到全部媒体筛选与排序之后。
        person_entries: list[Entry] = []
        if "Person" in include_types and not ids_raw and not simple_movie_page:
            person_entries = await _person_entries(session, scope, search_term)
    if simple_movie_page:
        entries = page_entries or []
        total = simple_total
        page = entries
    else:
        if exclude_types:
            entries = [e for e in entries if e[0] not in exclude_types]

        # ---- 过滤 ---------------------------------------------------------
        if search_term:
            entries = [e for e in entries if _entry_search_match(e, search_term)]
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
                e
                for e in entries
                if e[1].metadata and e[1].metadata.content_rating in ratings
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

        # ---- 排序 / 分页 -----------------------------------------------------
        entries = _sort_entries(entries, sort_by, sort_order) + person_entries
        total = len(entries)
        page = entries[start_index : start_index + limit] if limit >= 0 else entries[start_index:]

    if lazy is not None and lazy.used:
        # 两段式装载的第二段：这一页要渲染的叶子单元现在才确定，
        # 回头只为它们补文件行与分集元数据（骨架里没有重复行，不会叠加）
        leaves = {
            (entry[1].item.id, entry[2], entry[3])
            for entry in page
            if entry[0] in ("Movie", "Episode")
        }
        if leaves:
            async with get_database().session() as session:
                await hydrate_leaves(
                    session,
                    {entry[1].item.id: entry[1] for entry in page
                     if entry[0] in ("Movie", "Episode")},
                    leaves,
                    library_id=lazy.library_id,
                    visible_library_ids=scope.visible,
                    dto_options=options,
                )

    dtos = [_entry_dto(ctx, e, options) for e in page]
    return JSONResponse(query_result(dtos, total, start_index))


async def collect_search_entries(
    session: AsyncSession,
    scope: ViewerScope,
    search: SearchTerm,
    *,
    media_types: set[str],
    include_persons: bool,
    library_id: int | None = None,
) -> list[Entry]:
    """按搜索词装配候选条目——`/Search/Hints` 与 `/Items?searchTerm=` 共用同一口径。

    独立成公开函数是为了让两个接口的"什么算命中"只有一份实现：/Search/Hints
    在真 Jellyfin 里也是先拿候选再换一套输出结构（SearchManager.cs:176），
    不是另一套匹配规则。
    """
    entries: list[Entry] = []
    if media_types:
        ids = await item_ids_with_files(
            session,
            library_id=library_id,
            visible_library_ids=scope.visible,
        )
        ids = await _narrow_by_search(session, ids, search)
        bundles = await load_bundles(
            session,
            ids,
            member_id=scope.member_id,
            library_id=library_id,
            visible_library_ids=scope.visible,
            dto_options=DtoOptions(),
        )
        entries = [
            e
            for e in _build_entries(bundles, media_types)
            if _entry_search_match(e, search)
        ]
    if include_persons:
        entries += await _person_entries(session, scope, search, library_id=library_id)
    return entries


async def _person_entries(
    session: AsyncSession,
    scope: ViewerScope,
    search: SearchTerm | None,
    *,
    library_id: int | None = None,
) -> list[Entry]:
    """/Items 里的 Person 候选。可见性 = 至少在一部可见条目里有署名。

    这里的姓名比对走 ``SearchTerm``（CleanName 口径），与 `/Persons` 的
    NameContains 不同——两条路径在真 Jellyfin 里分别落在 BaseItemRepository 与
    PeopleRepository，口径本就不一致，照抄。
    """
    visible_ids: list[int] | None = None
    if library_id is not None:
        visible_ids = await item_ids_with_files(
            session, library_id=library_id, visible_library_ids=scope.visible
        )
    elif scope.visible is not None:
        visible_ids = await item_ids_with_files(
            session, visible_library_ids=scope.visible
        )
    _, persons = await query_persons(session, visible_item_ids=visible_ids)
    if search is not None:
        persons = [p for p in persons if search.matches(p.name, p.original_name)]
    return [("Person", p, 0, 0) for p in persons]


async def _entries_for_ids(
    session: AsyncSession, ids_raw: list[str], scope: ViewerScope, *, options: DtoOptions
) -> list[Entry]:
    refs = [r for r in (decode_guid(i) for i in ids_raw) if r is not None]
    scoped = {r.entity_id for r in refs if r.kind != EntityKind.LIBRARY}
    # 直达 id 也过可见性筛（与浏览口径一致，不给可枚举 GUID 留后门）
    scoped = {i for i in scoped if await _item_visible(session, i, scope)}
    bundles = await load_bundles(
        session,
        list(scoped),
        member_id=scope.member_id,
        visible_library_ids=scope.visible,
        dto_options=options,
    )
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


async def _narrow_by_search(
    session: AsyncSession, ids: list[int], search: SearchTerm | None
) -> list[int]:
    """带 searchTerm 时，把待水合的条目收窄到搜索粗筛命中的那些。

    粗筛只读名字列，水合才是重头——顺序反过来就是「命中 1 条也要装完整库」，
    也就是 Infuse 逐字搜索被拖到 5~8 秒的原因。
    """
    if search is None:
        return ids
    matched = await search_candidate_item_ids(session, search, ids)
    return [i for i in ids if i in matched]


async def _entries_for_parent(
    session: AsyncSession,
    parent_raw: str | None,
    scope: ViewerScope,
    *,
    include_types: set[str],
    recursive: bool | None,
    search: SearchTerm | None,
    options: DtoOptions,
    lazy: _LazyLeaves | None = None,
) -> list[Entry] | None:
    """按 parentId 语义展开候选。返回 None 表示"根级 → 视图列表"。"""
    if not parent_raw or is_empty_guid(parent_raw):
        if search is not None or include_types:
            # 无 parent 的全局搜索/类型查询：跨**可见**库递归
            types = include_types or {"Movie", "Series"}
            ids = await item_ids_with_files(session, visible_library_ids=scope.visible)
            ids = await _narrow_by_search(session, ids, search)
            bundles = await load_bundles(
                session,
                ids,
                member_id=scope.member_id,
                visible_library_ids=scope.visible,
                dto_options=options,
            )
            return _build_entries(bundles, types)
        return None

    ref = decode_guid(parent_raw)
    if ref is None:
        raise not_found()
    if ref.kind == EntityKind.FIXED and ref.entity_id == FIXED_ROOT:
        return None  # 根文件夹 → 视图列表

    if ref.kind == EntityKind.LIBRARY:
        if scope.library_hidden(ref.entity_id):
            raise not_found()
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
        ids = await _narrow_by_search(session, ids, search)
        leaf_scope = lazy.scope_for(types) if lazy else None
        bundles = await load_bundles(
            session,
            ids,
            member_id=scope.member_id,
            library_id=ref.entity_id,
            visible_library_ids=scope.visible,
            dto_options=options,
            leaf_scope=leaf_scope,
            # 只出 Series 行的库浏览（剧集库的默认视图）不读任何季元数据
            include_seasons=bool(types & {"Season", "Episode"}),
        )
        if lazy and leaf_scope is not None:
            lazy.used = True
            lazy.library_id = ref.entity_id
        return _build_entries(bundles, types)

    if ref.kind == EntityKind.ITEM:
        if not await _item_visible(session, ref.entity_id, scope):
            raise not_found()
        types = include_types or {"Season"}
        leaf_scope = lazy.scope_for(types) if lazy else None
        bundles = await load_bundles(
            session,
            [ref.entity_id],
            member_id=scope.member_id,
            visible_library_ids=scope.visible,
            dto_options=options,
            leaf_scope=leaf_scope,
        )
        if lazy and leaf_scope is not None:
            lazy.used = True
        return _build_entries(bundles, types)

    if ref.kind == EntityKind.SEASON:
        if not await _item_visible(session, ref.entity_id, scope):
            raise not_found()
        leaf_scope = lazy.scope_for({"Episode"}) if lazy else None
        bundles = await load_bundles(
            session,
            [ref.entity_id],
            member_id=scope.member_id,
            visible_library_ids=scope.visible,
            dto_options=options,
            leaf_scope=leaf_scope,
        )
        if lazy and leaf_scope is not None:
            lazy.used = True
        return _build_entries(bundles, {"Episode"}, season_scope=ref.season)

    raise not_found()


@router.get("/Items")
@router.get("/Users/{user_id}/Items")
async def get_items(
    request: Request,
    user_id: str | None = None,
    scope: ViewerScope = Depends(viewer_scope),
) -> JSONResponse:
    return await _query_items(request, scope)


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
    scope: ViewerScope = Depends(viewer_scope),
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

    if library_id is not None and scope.library_hidden(library_id):
        raise not_found()
    async with get_database().session() as session:
        # 先读每个最新单元的 5 个标量列；只有通过 limit/groupItems 的最终
        # 条目才进入 load_bundles。这样 1200 部电影不会先水合整库 JSON。
        #
        # 候选行数也按需下推到 SQL。取多少够用取决于"最近入库里同一部剧占了
        # 几集"：电影库一行对一条，取 limit×4 就够；剧集库要把同剧多集折叠
        # 成一条，生产实测最费的库要扫到 527 行才凑满 20 条，第二档 limit×32
        # 覆盖得住。两档都不够才不加限制——无论走到哪一档，结果与全表取回
        # 完全一致（排序是全序，取前缀等价于取全部再切片）。
        selected_units: list[LatestUnitCandidate] = []
        grouped_series: dict[int, int] = {}
        for row_limit in (max(limit * 4, 100), max(limit * 32, 800), None):
            latest_units = await latest_unit_candidates(
                session,
                member_id=scope.member_id,
                library_id=library_id,
                visible_library_ids=scope.visible,
                is_played=is_played,
                row_limit=row_limit,
            )
            selected_units = []
            grouped_series = {}
            for candidate in latest_units:
                if len(selected_units) >= limit:
                    break
                if candidate.kind == "movie":
                    selected_units.append(candidate)
                    continue
                # 剧集：两态简化（设计文档偏离⑥），同剧多集新入库聚合为 Series。
                if not groupItems:
                    selected_units.append(candidate)
                    continue
                if candidate.media_item_id in grouped_series:
                    grouped_series[candidate.media_item_id] += 1
                    continue
                grouped_series[candidate.media_item_id] = 1
                selected_units.append(candidate)
            # 选够了，或候选本身就没被截断（说明库里就这么多）→ 结果已是最终态
            if len(selected_units) >= limit or row_limit is None or len(
                latest_units
            ) < row_limit:
                break

        selected_ids = list(dict.fromkeys(c.media_item_id for c in selected_units))
        bundles = await load_bundles(
            session,
            selected_ids,
            member_id=scope.member_id,
            library_id=library_id,
            visible_library_ids=scope.visible,
            dto_options=options,
            # 只渲染选中的这些单元：同剧聚合成 Series 的那几条也只吃单元键集合
            leaf_scope={
                (c.media_item_id, c.season_number, c.episode_number)
                for c in selected_units
            },
        )
    dtos: list[dict[str, Any]] = []
    dto_candidates: list[LatestUnitCandidate] = []
    for candidate in selected_units:
        bundle = bundles.get(candidate.media_item_id)
        if bundle is None:
            continue
        season = candidate.season_number
        episode = candidate.episode_number
        if candidate.kind == "movie":
            dtos.append(movie_dto(ctx, bundle, options))
        else:
            dtos.append(episode_dto(ctx, bundle, season, episode, options))
        dto_candidates.append(candidate)

    if groupItems:
        # 同剧 ≥2 个新单元 → 用 Series 条目替换该剧的 Episode 占位
        for i, (candidate, dto) in enumerate(zip(dto_candidates, dtos, strict=True)):
            if dto.get("Type") != "Episode":
                continue
            count = grouped_series.get(candidate.media_item_id, 0)
            if count > 1:
                bundle = bundles[candidate.media_item_id]
                series = series_dto(ctx, bundle, options)
                series["ChildCount"] = count
                dtos[i] = series

    return JSONResponse(dtos)


@router.get("/UserItems/Resume")
@router.get("/Users/{user_id}/Items/Resume")
async def items_resume(
    request: Request,
    user_id: str | None = None,
    scope: ViewerScope = Depends(viewer_scope),
) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    media_types = set(parse_comma(q.get("mediaTypes")))
    start_index = _parse_int(q.get("startIndex"))
    limit = _parse_int(q.get("limit"), default=-1)
    candidates: list[ResumeUnitCandidate] = []
    if not media_types or "Video" in media_types:
        async with get_database().session() as session:
            # 播放状态表通常远小于媒体库；只为有续播点的单元查询 bundle。
            candidates = await resume_unit_candidates(
                session, member_id=scope.member_id, visible_library_ids=scope.visible
            )
            page_candidates = (
                candidates[start_index : start_index + limit]
                if limit >= 0
                else candidates[start_index:]
            )
            selected_ids = list(dict.fromkeys(c.media_item_id for c in page_candidates))
            bundles = await load_bundles(
                session,
                selected_ids,
                member_id=scope.member_id,
                visible_library_ids=scope.visible,
                dto_options=options,
                # 本页的续播单元就是全部会渲染的叶子（电影用 (0,0) 哨兵）
                leaf_scope={
                    (
                        c.media_item_id,
                        c.season_number,
                        c.episode_number,
                    )
                    for c in page_candidates
                },
            )
    else:
        page_candidates = []
        bundles = {}

    # 服务端强制语义：可续播 = 位置 > 0；候选查询已按最近活动降序排列。
    page: list[Entry] = []
    for candidate in page_candidates:
        bundle = bundles.get(candidate.media_item_id)
        if bundle is None:
            continue
        if bundle.item.kind == "movie":
            if (0, 0) in bundle.files:
                page.append(("Movie", bundle, 0, 0))
        elif (candidate.season_number, candidate.episode_number) in bundle.files:
            page.append(
                (
                    "Episode",
                    bundle,
                    candidate.season_number,
                    candidate.episode_number,
                )
            )

    total = len(candidates)
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
async def items_counts(
    request: Request, scope: ViewerScope = Depends(viewer_scope)
) -> JSONResponse:
    """全服统计（LibraryController.cs:453，ItemCounts 的 12 个非可空计数）。

    播放器的服务器/媒体库卡片用它显示"多少部电影、多少部剧"。
    每类型只有一个在用库时直接读扫描/入库写路径维护的快照。
    同类型多库可能收藏同一作品（例如一部剧的分集跨库），此时才用
    library_file 的覆盖索引做跨库去重，避免盲目相加快照导致重复计数。"""
    async with get_database().session() as session:
        libraries = await list_libraries(session, visible_ids=scope.visible)
        movie_libraries = [
            library
            for library in libraries
            if library.kind == "movie" and library.stats_item_count > 0
        ]
        tv_libraries = [
            library
            for library in libraries
            if library.kind == "tv" and library.stats_item_count > 0
        ]

        movie_count = sum(library.stats_item_count for library in movie_libraries)
        series_count = sum(library.stats_item_count for library in tv_libraries)
        episode_count = sum(library.stats_episode_count for library in tv_libraries)

        if len(movie_libraries) > 1 or len(tv_libraries) > 1:
            from sqlalchemy import func
            from sqlalchemy import select as sa_select

            from movieclaw_db.models import LibraryFile

            async def distinct_item_count(library_ids: list[int]) -> int:
                distinct_items = (
                    sa_select(LibraryFile.media_item_id)
                    .where(
                        LibraryFile.library_id.in_(library_ids),
                        LibraryFile.media_item_id.is_not(None),
                        LibraryFile.in_place(),
                    )
                    .distinct()
                    .subquery()
                )
                return int(
                    (
                        await session.execute(
                            sa_select(func.count()).select_from(distinct_items)
                        )
                    ).scalar_one()
                )

            if len(movie_libraries) > 1:
                movie_count = await distinct_item_count(
                    [library.id for library in movie_libraries if library.id is not None]
                )
            if len(tv_libraries) > 1:
                tv_library_ids = [
                    library.id for library in tv_libraries if library.id is not None
                ]
                series_count = await distinct_item_count(tv_library_ids)
                distinct_units = (
                    sa_select(
                        LibraryFile.media_item_id,
                        LibraryFile.season_number,
                        LibraryFile.episode_number,
                    )
                    .where(
                        LibraryFile.library_id.in_(tv_library_ids),
                        LibraryFile.media_item_id.is_not(None),
                        LibraryFile.in_place(),
                    )
                    .distinct()
                    .subquery()
                )
                episode_count = int(
                    (
                        await session.execute(
                            sa_select(func.count()).select_from(distinct_units)
                        )
                    ).scalar_one()
                )
    return JSONResponse(
        {
            "MovieCount": movie_count,
            "SeriesCount": series_count,
            "EpisodeCount": episode_count,
            "ArtistCount": 0,
            "ProgramCount": 0,
            "TrailerCount": 0,
            "SongCount": 0,
            "AlbumCount": 0,
            "MusicVideoCount": 0,
            "BoxSetCount": 0,
            "BookCount": 0,
            "ItemCount": movie_count + series_count + episode_count,
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
            LibraryFile.in_place(),
        )
    )


@router.get("/Items/{item_id}")
@router.get("/Users/{user_id}/Items/{item_id}")
async def get_item(
    request: Request,
    item_id: str,
    user_id: str | None = None,
    scope: ViewerScope = Depends(viewer_scope),
) -> JSONResponse:
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
            return JSONResponse(person_dto(ctx, person))
        if ref.kind == EntityKind.LIBRARY:
            if scope.library_hidden(ref.entity_id):
                raise not_found()
            library = await session.get(Library, ref.entity_id)
            if library is None:
                raise not_found()
            return JSONResponse(
                library_view_dto(ctx, library, await _cover_tag(library.id))
            )
        # 单条目是全字段语义，People 恒输出；可见性先行（GUID 可枚举）
        if not await _item_visible(session, ref.entity_id, scope):
            raise not_found()
        # GUID 自带类型，装载前就知道这次只会渲染哪一个叶子：
        # 集 → 就那一集；剧/季 → 一个叶子都不渲染（Series/Season DTO 只吃
        # 单元键集合 + 季元数据 + 播放状态）；电影 → (0,0) 哨兵单元。
        # 剧条目 GUID 与电影同型，统一带上 (id,0,0)：对剧来说这个单元不存在，
        # 不会多装任何行。
        if ref.kind == EntityKind.EPISODE:
            detail_scope = {(ref.entity_id, ref.season, ref.episode)}
        elif ref.kind == EntityKind.SEASON:
            detail_scope = set()
        else:
            detail_scope = {(ref.entity_id, 0, 0)}
        bundles = await load_bundles(
            session,
            [ref.entity_id],
            member_id=scope.member_id,
            visible_library_ids=scope.visible,
            dto_options=options,
            leaf_scope=detail_scope,
        )

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
async def shows_next_up(
    request: Request, scope: ViewerScope = Depends(viewer_scope)
) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    options = dto_options(
        q.get("fields"),
        enable_user_data=q.get("enableUserData"),
        enable_images=q.get("enableImages"),
    )
    series_filter = decode_guid(q.get("seriesId") or "") if q.get("seriesId") else None

    async with get_database().session() as session:
        ids = await next_up_item_ids(
            session,
            member_id=scope.member_id,
            series_id=series_filter.entity_id if series_filter else None,
        )
        bundles = await load_bundles(
            session,
            ids,
            member_id=scope.member_id,
            visible_library_ids=scope.visible,
            dto_options=options,
        )

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
async def shows_seasons(
    request: Request, series_id: str, scope: ViewerScope = Depends(viewer_scope)
) -> JSONResponse:
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
        if not await _item_visible(session, ref.entity_id, scope):
            raise not_found()
        bundles = await load_bundles(
            session,
            [ref.entity_id],
            member_id=scope.member_id,
            visible_library_ids=scope.visible,
            dto_options=options,
            # Season DTO 只吃季元数据 + "哪些单元有文件"的键集合 + 播放状态，
            # 一个叶子条目都不渲染——整剧的文件行与分集元数据全部不必装载
            leaf_scope=set(),
        )
    bundle = bundles.get(ref.entity_id)
    if bundle is None or bundle.item.kind != "tv":
        raise not_found()

    seasons = sorted({s for s, _ in bundle.units})
    if parse_bool(q.get("isSpecialSeason")) is False:
        seasons = [s for s in seasons if s != 0]
    dtos = [season_dto(ctx, bundle, s, options) for s in seasons]
    return JSONResponse(query_result(dtos, len(dtos)))


@router.get("/Shows/{series_id}/Episodes")
async def shows_episodes(
    request: Request, series_id: str, scope: ViewerScope = Depends(viewer_scope)
) -> JSONResponse:
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

    start_index = _parse_int(q.get("startIndex"))
    limit = _parse_int(q.get("limit"), default=-1)
    # 不限季又不分页 = 整剧全要，那就没有"少装一点"的空间，直接走整行装载；
    # 只要限了季或分了页，先建骨架、选完页再补料才划算
    whole_series = season_scope is None and limit < 0 and start_index == 0

    async with get_database().session() as session:
        if not await _item_visible(session, target_item_id, scope):
            raise not_found_message("Series not found")
        bundles = await load_bundles(
            session,
            [target_item_id],
            member_id=scope.member_id,
            visible_library_ids=scope.visible,
            dto_options=options,
            leaf_scope=None if whole_series else set(),
        )
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
        # 洗牌对象从 DTO 换成单元：random.shuffle 只按下标置换、与元素类型无关，
        # 同一 RNG 状态下得到的排列完全相同，但不必先把整季都构建成 DTO
        if q.get("sortBy") == "Random":
            import random

            random.shuffle(units)

        total = len(units)
        page_units = (
            units[start_index : start_index + limit]
            if limit >= 0
            else units[start_index:]
        )
        if not whole_series:
            await hydrate_leaves(
                session,
                {target_item_id: bundle},
                {(target_item_id, s, e) for s, e in page_units},
                visible_library_ids=scope.visible,
                dto_options=options,
            )
    page = [episode_dto(ctx, bundle, s, e, options) for s, e in page_units]
    return JSONResponse(query_result(page, total, start_index))
