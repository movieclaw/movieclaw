"""搜索建议接口（`Jellyfin.Api/Controllers/SearchController.cs`）。

Infuse 不用这个接口（它直接打 /Items + /Persons），但 Jellyfin 官方 Web /
Android 客户端的搜索框走的就是它。此前未实现，请求会掉到前端 catch-all 返回
一整页 HTML 404。

与 /Items 的关系：真 Jellyfin 的 SearchManager 先由搜索提供方给出候选 id，再
回 `GetItemList` 取条目，最后映射成扁平的 SearchHint（SearchManager.cs:176）。
这里同构——复用 /Items 的候选装配与匹配口径，只在最后换一套更窄的输出结构。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from movieclaw_db.engine import get_database
from movieclaw_db.models import Library
from movieclaw_jellyfin.catalog import (
    TICKS_PER_MS,
    DtoContext,
    ItemBundle,
    person_dto,
)
from movieclaw_jellyfin.errors import bad_request_text
from movieclaw_jellyfin.ids import (
    EntityKind,
    decode_guid,
    episode_guid,
    is_empty_guid,
    item_guid,
    season_guid,
)
from movieclaw_jellyfin.routes.common import dto_context, parse_bool, parse_comma
from movieclaw_jellyfin.routes.library import (
    Entry,
    ViewerScope,
    collect_search_entries,
    viewer_scope,
)
from movieclaw_jellyfin.search import parse_term
from movieclaw_jellyfin.security import require_device

router = APIRouter(dependencies=[Depends(require_device)])

# includeMedia=false 时，只有这些"按名"类型还留在结果里（SearchManager.cs:417）。
# movieclaw 没有 Genre/Studio/MusicArtist 实体，能给的按名类型只有 Person。
_MEDIA_KINDS = {"Movie", "Series", "Season", "Episode"}


def _hint(ctx: DtoContext, entry: Entry) -> dict[str, Any]:
    """Entry → SearchHint（MediaBrowser.Model/Search/SearchHint.cs）。

    只输出本层拿得到的字段：SearchHint 里 Album/Artists/ChannelName 等音乐与
    直播电视字段在 movieclaw 没有对应数据，按"可空字段值为 null 时省略"的
    全局约定不输出（设计文档 5.3）。
    """
    kind, payload, season, episode = entry
    if kind == "Person":
        dto = person_dto(ctx, payload)
        return {
            "ItemId": dto["Id"],
            "Id": dto["Id"],
            "Name": dto["Name"],
            "MatchedTerm": dto["Name"],
            "Type": "Person",
            "MediaType": "Unknown",
            **({"PrimaryImageTag": dto["ImageTags"]["Primary"]} if dto["ImageTags"] else {}),
        }

    bundle: ItemBundle = payload
    item = bundle.item
    if kind == "Episode":
        guid = episode_guid(item.id or 0, season, episode)
        row = bundle.episodes.get((season, episode))
        name = (row.name if row else None) or f"第 {episode} 集"
    elif kind == "Season":
        guid = season_guid(item.id or 0, season)
        row = bundle.seasons.get(season)
        name = (row.name if row else None) or f"第 {season} 季"
    else:
        guid = item_guid(item.id or 0)
        name = item.title

    hint: dict[str, Any] = {
        "ItemId": guid,  # 老客户端仍读这个键（SearchController.cs:156）
        "Id": guid,
        "Name": name,
        "MatchedTerm": name,
        "Type": kind,
        "MediaType": "Unknown" if kind in ("Series", "Season") else "Video",
    }
    if kind in ("Series", "Season"):
        hint["IsFolder"] = True
    if kind == "Episode":
        hint["IndexNumber"] = episode
        hint["ParentIndexNumber"] = season
        hint["Series"] = item.title
    if item.year:
        hint["ProductionYear"] = item.year
    runtime_ms = bundle.unit_runtime_ms(season, episode)
    if runtime_ms:
        hint["RunTimeTicks"] = runtime_ms * TICKS_PER_MS
    if kind == "Series" and item.status:
        hint["Status"] = item.status
    return hint


@router.get("/Search/Hints")
async def get_search_hints(
    request: Request, scope: ViewerScope = Depends(viewer_scope)
) -> JSONResponse:
    q = request.query_params
    search = parse_term(q.get("searchTerm"))
    # searchTerm 是 [Required]，且 SearchManager 入口还有
    # ArgumentException.ThrowIfNullOrWhiteSpace（SearchManager.cs:179）——
    # 缺失和纯空白都是 400。注意 /Items 那边用的是 IsNullOrEmpty，纯空白会被
    # 当成正常搜索词照走（结果自然为空），两处不一致是 Jellyfin 自身如此。
    if search is None or not search.raw.strip():
        raise bad_request_text()

    include_types = set(parse_comma(q.get("includeItemTypes")))
    exclude_types = set(parse_comma(q.get("excludeItemTypes")))
    include_media = parse_bool(q.get("includeMedia")) is not False
    include_people = parse_bool(q.get("includePeople")) is not False
    start_index = _int_or_zero(q.get("startIndex"))
    limit = _int_or_zero(q.get("limit"))

    # 类型收敛照抄 SearchManager.BuildIncludeItemTypes / BuildExcludeItemTypes：
    # includeMedia=false 时媒体类型整体退出，只留按名类型（本层只有 Person）
    media_types = (
        ((include_types & _MEDIA_KINDS) if include_types else set(_MEDIA_KINDS))
        if include_media
        else set()
    ) - exclude_types
    # isMovie/isSeries 是媒体类型的另一个入口；isNews/isKids/isSports 本层无
    # 对应数据（无直播电视、无分级标签），接受但忽略
    if parse_bool(q.get("isMovie")) is True:
        media_types &= {"Movie"}
    if parse_bool(q.get("isSeries")) is True:
        media_types &= {"Series", "Season", "Episode"}
    want_person = (
        include_people
        and "Person" not in exclude_types
        and (not include_types or "Person" in include_types)
    )

    ctx = await dto_context()
    async with get_database().session() as session:
        library_id = await _parent_library_id(session, q.get("parentId"))
        if library_id is False:  # parentId 给了但不是本层认识的库 → 空结果
            return JSONResponse({"SearchHints": [], "TotalRecordCount": 0})
        entries = await collect_search_entries(
            session,
            scope,
            search,
            media_types=media_types,
            include_persons=want_person,
            library_id=library_id,
        )

    total = len(entries)
    page = entries[start_index : start_index + limit] if limit > 0 else entries[start_index:]
    return JSONResponse(
        {"SearchHints": [_hint(ctx, e) for e in page], "TotalRecordCount": total}
    )


def _int_or_zero(raw: str | None) -> int:
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


async def _parent_library_id(session, parent_raw: str | None) -> int | None | bool:
    """parentId → 库 id；未传返回 None（不收窄）；不是已知库返回 False（空结果）。"""
    if not parent_raw or is_empty_guid(parent_raw):
        return None
    ref = decode_guid(parent_raw)
    if ref is None or ref.kind != EntityKind.LIBRARY:
        return False
    return ref.entity_id if await session.get(Library, ref.entity_id) else False
