"""人物接口（`Jellyfin.Api/Controllers/PersonsController.cs`）。

Infuse 的搜索页是两路并发：`/Items?searchTerm=` 取媒体、`/Persons?searchTerm=`
取演职员，两者结果合并渲染。本层此前没有这个命名空间，请求会掉到前端的
catch-all 返回一整页 HTML 404——搜索页因此表现为不可用（2026-08-22 抓包实证）。

人物是 ItemsByName：不属于任何库、没有文件、没有 UserData。可见性由
「至少在一部该观看者可见的条目里有署名」推导，对齐 PersonsController.cs:128
的 BuildAccessFilter。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from movieclaw_db.engine import get_database
from movieclaw_db.models import Library
from movieclaw_jellyfin.catalog import (
    item_ids_with_files,
    person_dto,
    query_persons,
)
from movieclaw_jellyfin.errors import not_found
from movieclaw_jellyfin.ids import EntityKind, decode_guid
from movieclaw_jellyfin.routes.common import (
    dto_context,
    parse_bool,
    parse_comma,
    query_result,
)
from movieclaw_jellyfin.routes.library import ViewerScope, viewer_scope
from movieclaw_jellyfin.security import require_device

router = APIRouter(dependencies=[Depends(require_device)])

# Jellyfin PersonKind ↔ movieclaw 的 media_item_person.department。
# movieclaw 只收 cast/director 两类署名（见 MediaItemPerson 的类注释），
# 其余 PersonKind（Writer/Producer/Composer…）在本库里恒无匹配。
_PERSON_KIND_TO_DEPARTMENT = {"actor": "cast", "director": "director"}


def _departments(raw: str | None) -> set[str]:
    """personTypes/excludePersonTypes → department 集合（未知种类静默丢弃）。"""
    return {
        dept
        for kind in parse_comma(raw)
        if (dept := _PERSON_KIND_TO_DEPARTMENT.get(kind.lower()))
    }


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


async def _library_item_ids(session, parent_raw: str | None) -> list[int] | None:
    """parentId 收窄：把人物限定在该库有署名的条目上。

    非库 GUID / 未知 GUID 一律当作"没有这样的库"，返回空列表（结果为空），
    不抛 404——PersonsController 对 parentId 不做存在性校验。
    """
    if not parent_raw:
        return None
    ref = decode_guid(parent_raw)
    if ref is None or ref.kind != EntityKind.LIBRARY:
        return []
    library = await session.get(Library, ref.entity_id)
    if library is None:
        return []
    return await item_ids_with_files(session, library_id=ref.entity_id)


@router.get("/Persons")
async def get_persons(
    request: Request, scope: ViewerScope = Depends(viewer_scope)
) -> JSONResponse:
    q = request.query_params
    ctx = await dto_context()
    start_index = _int_or_none(q.get("startIndex")) or 0
    limit = _int_or_none(q.get("limit")) or 0
    search_term = q.get("searchTerm")
    filters = set(parse_comma(q.get("filters")))
    is_favorite = parse_bool(q.get("isFavorite"))

    # 人物没有 UserData 台账（本层不落人物收藏），IsFavorite 恒无匹配。
    # 对齐真 Jellyfin 的「筛选后为空」而不是「忽略该筛选」——忽略会把整个
    # 人物列表当成收藏夹返回，比空列表离谱得多。
    if is_favorite is True or "IsFavorite" in filters:
        return JSONResponse(query_result([], 0, start_index))

    async with get_database().session() as session:
        parent_ids = await _library_item_ids(session, q.get("parentId"))
        visible_ids: list[int] | None = None
        if scope.visible is not None:
            visible_ids = await item_ids_with_files(
                session, visible_library_ids=scope.visible
            )
        if parent_ids is not None:
            visible_ids = (
                parent_ids
                if visible_ids is None
                else [i for i in parent_ids if i in set(visible_ids)]
            )

        appears_in = decode_guid(q.get("appearsInItemId") or "")
        total, page = await query_persons(
            session,
            name_contains=search_term,
            name_starts_with=q.get("nameStartsWith"),
            name_less_than=q.get("nameLessThan"),
            name_starts_with_or_greater=q.get("nameStartsWithOrGreater"),
            appears_in_item_id=(
                appears_in.entity_id
                if appears_in is not None and appears_in.kind == EntityKind.ITEM
                else None
            ),
            person_types=_departments(q.get("personTypes")),
            exclude_person_types=_departments(q.get("excludePersonTypes")),
            visible_item_ids=visible_ids,
            start_index=start_index,
            limit=limit,
        )

    return JSONResponse(query_result([person_dto(ctx, p) for p in page], total, start_index))


@router.get("/Persons/{name}")
async def get_person_by_name(
    name: str, scope: ViewerScope = Depends(viewer_scope)
) -> JSONResponse:
    """按**姓名**取单个人物（PersonsController.cs:151，路由段是名字不是 GUID）。"""
    ctx = await dto_context()
    async with get_database().session() as session:
        visible_ids: list[int] | None = None
        if scope.visible is not None:
            visible_ids = await item_ids_with_files(
                session, visible_library_ids=scope.visible
            )
        _, candidates = await query_persons(
            session, name_contains=name, visible_item_ids=visible_ids
        )
    # NameContains 是子串匹配，这里要的是精确同名；大小写不敏感对齐 GetPerson
    exact = [p for p in candidates if p.name.upper() == name.upper()]
    if not exact:
        raise not_found()
    return JSONResponse(person_dto(ctx, exact[0]))
