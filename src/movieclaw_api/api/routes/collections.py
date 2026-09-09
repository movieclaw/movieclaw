"""合集接口（docs/design/library-collections.md 第 3 节）。

合集是**存好的筛选**：规则与 ``library.match_rules`` 同构，所以"筛完存为合集"
是一次纯粹的形状转换。这里只做增删改查与可见性收口，成员解析一律走
``services.library.collections.resolve_members()``——那是全局唯一的实现，
web 与 Jellyfin 兼容层共用它（见该模块的模块级注释）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.api.deps import require_login
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.library import (
    CollectionPayload,
    CollectionView,
    LibraryItemView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.services.library.collections import (
    count_members,
    effective_rules,
    is_rule_driven,
    resolve_members,
    visible_collections,
)
from movieclaw_api.services.library.items import _aggregate_wall_views, favorite_item_ids
from movieclaw_db.engine import get_session
from movieclaw_db.models import Collection, CollectionItem

router = APIRouter(prefix="/collections", tags=["collections"])


async def _scope(session: AsyncSession, principal: Principal) -> tuple[int, set[int] | None]:
    """观看者身份与可见库范围——三层可见性收口里的第一层用它。"""
    member_id = principal.member_id if principal.member_id is not None else 0
    return member_id, await visible_library_ids(session, principal)


async def _view(
    session: AsyncSession,
    row: Collection,
    *,
    member_id: int,
    visible: set[int] | None,
) -> CollectionView:
    return CollectionView(
        id=row.id or 0,
        name=row.name,
        library_id=row.library_id,
        rules=effective_rules(row),
        sort=row.sort,
        visibility=row.visibility,
        builtin=row.builtin,
        # 形态是推导的：能不能改看 builtin，会不会自己长看有没有规则
        editable=row.builtin is None,
        rule_driven=is_rule_driven(row),
        item_count=await count_members(
            session, row, member_id=member_id, visible_library_ids=visible
        ),
        cover_item_id=row.cover_item_id,
        position=row.position,
    )


async def _get_or_404(session: AsyncSession, collection_id: int) -> Collection:
    row = await session.get(Collection, collection_id)
    if row is None:
        raise NotFoundException("合集不存在（可能已被删除）")
    return row


def _guard_visible(row: Collection, member_id: int, visible: set[int] | None) -> None:
    """私有合集与不可见库里的合集一律按 404 拒绝，不返回空列表。

    与既有条目可见性的处理一致：id 可枚举，空列表等于确认它存在。
    """
    if row.visibility == "private" and row.member_id != member_id:
        raise NotFoundException("合集不存在（可能已被删除）")
    if visible is not None and row.library_id is not None and row.library_id not in visible:
        raise NotFoundException("合集不存在（可能已被删除）")


@router.get(
    "",
    response_model=ApiResponse[list[CollectionView]],
    summary="合集列表（按元数据可见性过滤，成员为空的不列）",
    operation_id="collection.list",
)
async def list_collections(
    library_id: Annotated[int | None, Query(description="只看某个库的合集；不给则全部")] = None,
    include_empty: Annotated[
        bool, Query(description="是否保留成员为 0 的合集（管理界面要，浏览界面不要）")
    ] = False,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[list[CollectionView]]:
    """成员为 0 的合集默认**不列**：点进去空无一物的合集是纯粹的死路。"""

    member_id, visible = await _scope(session, principal)
    rows = await visible_collections(
        session, library_id=library_id, member_id=member_id, visible_library_ids=visible
    )
    views = [await _view(session, row, member_id=member_id, visible=visible) for row in rows]
    return ok(views if include_empty else [v for v in views if v.item_count > 0])


@router.post(
    "",
    response_model=ApiResponse[CollectionView],
    summary="创建合集（筛完存为合集）",
    operation_id="collection.create",
)
async def create_collection(
    payload: CollectionPayload,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[CollectionView]:
    """规则驱动还是名单驱动，由传了 ``rules`` 还是 ``item_ids`` 决定。

    「固定当前这 N 部」就是把此刻的命中集当 ``item_ids`` 传过来——不需要拖拽、
    不需要挑选 UI，一次写入即可。
    """

    member_id, visible = await _scope(session, principal)
    if not payload.name:
        raise BadRequestException("合集需要一个名字")
    if payload.library_id is None and not payload.item_ids:
        raise BadRequestException("规则驱动的合集必须指定所属库（跨库合集只能给固定名单）")
    if visible is not None and payload.library_id is not None and payload.library_id not in visible:
        raise NotFoundException("库不存在或不可见")

    row = Collection(
        name=payload.name,
        library_id=payload.library_id,
        rules=list(payload.rules or []),
        sort=payload.sort or "title",
        visibility=payload.visibility or "household",
        member_id=member_id if (payload.visibility == "private") else None,
    )
    session.add(row)
    await session.flush()
    if payload.item_ids:
        session.add_all(
            CollectionItem(collection_id=row.id or 0, media_item_id=item_id, position=index)
            for index, item_id in enumerate(payload.item_ids)
        )
        await session.flush()
    return ok(await _view(session, row, member_id=member_id, visible=visible))


@router.get(
    "/{collection_id}",
    response_model=ApiResponse[CollectionView],
    summary="合集详情",
    operation_id="collection.get",
)
async def get_collection(
    collection_id: int,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[CollectionView]:
    member_id, visible = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    return ok(await _view(session, row, member_id=member_id, visible=visible))


@router.put(
    "/{collection_id}",
    response_model=ApiResponse[CollectionView],
    summary="改合集（改名 / 改规则 / 改可见性）",
    operation_id="collection.update",
)
async def update_collection(
    collection_id: int,
    payload: CollectionPayload,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[CollectionView]:
    """内置合集只能改名与可见性——规则是内置的，改了它就不是那个合集了。"""

    member_id, visible = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)

    if payload.name:
        row.name = payload.name
    if payload.visibility:
        row.visibility = payload.visibility
        row.member_id = member_id if payload.visibility == "private" else None
    if payload.sort:
        row.sort = payload.sort
    if payload.rules is not None:
        if row.builtin:
            raise BadRequestException("内置合集的规则不可修改（可以改名或隐藏）")
        row.rules = list(payload.rules)
    if payload.item_ids is not None:
        if row.builtin:
            raise BadRequestException("内置合集的成员由规则决定，不能手工指定")
        for old in (
            await session.execute(
                select(CollectionItem).where(CollectionItem.collection_id == collection_id)
            )
        ).scalars():
            await session.delete(old)
        session.add_all(
            CollectionItem(collection_id=collection_id, media_item_id=item_id, position=index)
            for index, item_id in enumerate(payload.item_ids)
        )
    await session.flush()
    return ok(await _view(session, row, member_id=member_id, visible=visible))


@router.delete(
    "/{collection_id}",
    response_model=ApiResponse[None],
    summary="删除合集（不动作品本身）",
    operation_id="collection.delete",
)
async def delete_collection(
    collection_id: int,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[None]:
    """删的是那层视图，作品一部都不会少——合集从来不拥有作品。"""

    member_id, visible = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    if row.builtin:
        raise BadRequestException("内置合集不能删除（可以隐藏）")
    await session.delete(row)
    return ok(None)


@router.get(
    "/{collection_id}/items",
    response_model=ApiResponse[list[LibraryItemView]],
    summary="合集成员（与单库海报墙同一份聚合）",
    operation_id="collection.items.list",
)
async def list_collection_items(
    collection_id: int,
    limit: Annotated[int | None, Query(ge=1, le=200, description="本页条目数")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[list[LibraryItemView]]:
    """卡片与主墙长得一模一样——两处走的是同一个 ``_aggregate_wall_views``。

    不共用的话，同一部片在合集页和库页会显示不同的库存概况/缺集数。
    """

    member_id, visible = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)

    ids = await resolve_members(
        session,
        row,
        member_id=member_id,
        visible_library_ids=visible,
        limit=limit,
        offset=offset,
    )
    if not ids:
        return ok([])
    views = await _aggregate_wall_views(session, row.library_id, ids, ids)
    favorites = await favorite_item_ids(session, ids, member_id=member_id)
    for view in views:
        view.is_favorite = view.media_item_id in favorites
    return ok(views)


@router.post(
    "/{collection_id}/apply-to-library",
    response_model=ApiResponse[None],
    summary="把合集的规则设为某个库的收藏范围",
    operation_id="collection.apply-to-library",
)
async def apply_to_library(
    collection_id: int,
    library_id: Annotated[int, Query(description="要写入 match_rules 的库")],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[None]:
    """筛选 → 合集 → 库收藏范围，同一份条件的第三个时态。

    用户不用理解"路由"这个词，就完成了分库配置（docs/design/library-routing.md）。
    """

    from movieclaw_db.models import Library

    member_id, visible = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    rules = effective_rules(row)
    if not rules:
        raise BadRequestException("名单驱动的合集没有规则，无法作为库的收藏范围")
    library = await session.get(Library, library_id)
    if library is None or (visible is not None and library_id not in visible):
        raise NotFoundException("库不存在或不可见")
    # 路由只认 genres / origin_countries 两个字段，其余条件在这里被丢弃——
    # 如实告诉调用方，而不是假装整条规则都生效了
    routable = [r for r in rules if r.get("field") in {"genres", "origin_countries"}]
    if not routable:
        raise BadRequestException("这个合集的条件里没有类型或地区，路由用不上")
    library.match_rules = routable
    await session.flush()
    return ok(None)
