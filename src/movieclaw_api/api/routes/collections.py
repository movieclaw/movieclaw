"""合集接口（docs/design/library-collections.md 第 3 节）。

合集是**存好的筛选**：规则与 ``library.match_rules`` 同构，所以"筛完存为合集"
是一次纯粹的形状转换。这里只做增删改查与可见性收口，成员解析一律走
``services.library.collections.resolve_members()``——那是全局唯一的实现，
web 与 Jellyfin 兼容层共用它（见该模块的模块级注释）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.api.deps import require_admin, require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.library import (
    CollectionCover,
    CollectionItemsPayload,
    CollectionPayload,
    CollectionSeriesView,
    CollectionView,
    LibraryItemView,
    SeriesPartView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import (
    ContentLimit,
    content_limit_for,
    visible_library_ids,
)
from movieclaw_api.services.library.collections import (
    effective_rules,
    is_rule_driven,
    resolve_members,
    visible_collections,
)
from movieclaw_api.services.library.items import (
    _aggregate_wall_views,
    favorite_item_ids,
    poster_facts_many,
)
from movieclaw_api.services.library.series import (
    is_series_collection,
    load_series_parts,
)
from movieclaw_db.engine import get_session
from movieclaw_db.models import (
    Collection,
    CollectionItem,
    LibraryFile,
    MediaItem,
    Subscription,
)
from movieclaw_media.models import MediaKind

router = APIRouter(prefix="/collections", tags=["collections"])


async def _scope(
    session: AsyncSession, principal: Principal
) -> tuple[int, set[int] | None, ContentLimit]:
    """观看者身份、可见库范围与内容分级约束。

    两个收窄都在这里一次取齐、原样往下传：合集是"存好的筛选"，如果它能绕过
    儿童档案，那道约束就等于没有——手工挑进合集的片更是最需要挡住的一种。
    """
    member_id = principal.member_id if principal.member_id is not None else 0
    return (
        member_id,
        await visible_library_ids(session, principal),
        await content_limit_for(session, principal),
    )


#: 卡片上铺几张封面。三张够看出"这里面装的是哪一类片"，再多就成了缩略图墙。
_COVER_COUNT = 3


def _iso_date(raw: str | None) -> date | None:
    """TMDB 的日期串 → date；畸形值当没有（上游档案脏了不该让整页 500）。"""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _kind_of(row: Collection) -> str:
    """合集从哪来：用户自建 / 内置 / 自动生成的系列。

    推导而不是存列（与"没有 mode 列"同源）。前端要它来分组展示——让前端自己
    去 ``startswith("series:")`` 解字符串，等于把推导规则抄第二遍。
    """
    if is_series_collection(row):
        return "series"
    return "builtin" if row.builtin else "user"


def _cover_head(row: Collection, ids: list[int]) -> list[int]:
    """卡片上要铺的那几张封面对应的条目——指定了封面就把它挪到最前。"""
    if not ids:
        return []
    head = list(ids)
    if row.cover_item_id in head:
        head.remove(row.cover_item_id)
        head.insert(0, row.cover_item_id)
    return head[:_COVER_COUNT]


async def _views(
    session: AsyncSession,
    rows: Sequence[Collection],
    *,
    member_id: int,
    visible: set[int] | None,
    content_limit: ContentLimit,
) -> list[CollectionView]:
    """一批合集的视图。

    两条代价上的分寸，都是为「一个库自动生成几十个系列合集」准备的：

    1. 每个合集的成员**只解析一次**——数量与封面都从这一份名单里取。分开取
       的话，一次列表请求里同一个合集要把成员算两遍，而成员解析就是一次
       完整的海报墙查询；
    2. **封面整页只取一次**（``poster_facts_many``）。初版是每个合集调一次
       完整的墙聚合（十条查询），40 个合集就是四百多条——在 NAS 的 SQLite
       上是肉眼可见的卡。现在封面的代价与合集数无关。

    仍然一个合集一次 ``resolve_members()``：批量化的只是取图，成员判定还是
    那条唯一的墙查询——"合集没有自己的查询"这条不能为性能让步。计数以后要
    换成缓存的话，换的也只是这里这一处（设计文档 5.5.2「计数可替换的形状」）。
    """

    resolved: list[tuple[Collection, list[int]]] = []
    for row in rows:
        ids = await resolve_members(
            session,
            row,
            member_id=member_id,
            visible_library_ids=visible,
            content_limit=content_limit,
        )
        resolved.append((row, ids))
    # 合集卡片上的图与海报墙上的图永远是同一张：共用 poster_facts_many 这一处实现
    facts = await poster_facts_many(
        session, sorted({i for row, ids in resolved for i in _cover_head(row, ids)})
    )
    views: list[CollectionView] = []
    for row, ids in resolved:
        covers = [
            CollectionCover(url=fact.url, blur=fact.blur)
            for fact in (facts.get(i) for i in _cover_head(row, ids))
            if fact is not None and fact.url
        ]
        views.append(
            CollectionView(
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
                item_count=len(ids),
                cover_item_id=row.cover_item_id,
                covers=covers,
                # 合集从哪来：用户自建 / 内置 / 自动生成的系列。分组展示要它，
                # 让前端去 startswith("series:") 解字符串等于把推导规则抄第二遍
                kind=_kind_of(row),
                hidden=row.hidden,
                position=row.position,
            )
        )
    return views


async def _view(
    session: AsyncSession,
    row: Collection,
    *,
    member_id: int,
    visible: set[int] | None,
    content_limit: ContentLimit,
) -> CollectionView:
    """单个合集的视图（增删改这三条路径用，列表走 ``_views``）。"""
    views = await _views(
        session, [row], member_id=member_id, visible=visible, content_limit=content_limit
    )
    return views[0]


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
    include_hidden: Annotated[
        bool, Query(description="是否带上已隐藏的合集（「显示已隐藏的合集」用它翻墓碑）")
    ] = False,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[list[CollectionView]]:
    """成员为 0 的合集默认**不列**：点进去空无一物的合集是纯粹的死路。

    已隐藏的同样默认不列，但 ``include_hidden=true`` 一定要能把它们翻出来——
    藏得回来才叫隐藏，藏不回来那是删除（设计文档 4.6.4）。
    """

    member_id, visible, content_limit = await _scope(session, principal)
    rows = await visible_collections(
        session,
        library_id=library_id,
        member_id=member_id,
        visible_library_ids=visible,
        include_hidden=include_hidden,
    )
    views = await _views(
        session, rows, member_id=member_id, visible=visible, content_limit=content_limit
    )
    if include_empty:
        return ok(views)
    # 只入库了一部的系列同样列出：进去能看到缺的那几部、就地补订阅——那正是系列
    # 合集的用处。与 Jellyfin 的 BoxSet 列表同一口径：只有空合集不列
    return ok([v for v in views if v.item_count > 0])


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

    member_id, visible, content_limit = await _scope(session, principal)
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
        # household 归 0（哨兵）：私有与否看 visibility，member_id 只回答"归谁"
        member_id=member_id if (payload.visibility == "private") else 0,
    )
    session.add(row)
    await session.flush()

    item_ids = list(payload.item_ids or [])
    if payload.snapshot and row.rules:
        # 「固定这 N 部」：在服务端把此刻的命中集固化成名单，然后清空规则。
        # 客户端只表达意图，不用把上千个 id 传过来再传回去
        item_ids = await resolve_members(
            session,
            row,
            member_id=member_id,
            visible_library_ids=visible,
            content_limit=content_limit,
        )
        row.rules = []
    if item_ids:
        session.add_all(
            CollectionItem(collection_id=row.id or 0, media_item_id=item_id, position=index)
            for index, item_id in enumerate(item_ids)
        )
        await session.flush()
    view = await _view(
        session, row, member_id=member_id, visible=visible, content_limit=content_limit
    )
    await session.commit()  # 事务边界由路由显式控制（见 engine.get_session 的说明）
    return ok(view)


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
    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    view = await _view(
        session, row, member_id=member_id, visible=visible, content_limit=content_limit
    )
    return ok(view)


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

    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)

    if payload.name:
        row.name = payload.name
    if payload.visibility:
        row.visibility = payload.visibility
        row.member_id = member_id if payload.visibility == "private" else 0
    if payload.sort:
        row.sort = payload.sort
    if payload.hidden is not None:
        # 取消隐藏走的也是这一条（前端的"显示已隐藏的合集"里点「恢复」）
        row.hidden = payload.hidden
    if payload.rules is not None:
        if row.builtin:
            raise BadRequestException("自动生成的合集规则不可修改（可以改名，或者隐藏它）")
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
    view = await _view(
        session, row, member_id=member_id, visible=visible, content_limit=content_limit
    )
    await session.commit()
    return ok(view)


def _guard_manual(row: Collection) -> None:
    """手工改成员的前置：只有**名单驱动**的合集能改。

    规则驱动的合集成员是 ``series_key = X`` 这类条件求值出来的
    （``resolve_members`` 对它压根不看 ``collection_item``），往里手工塞一部片
    只会**静默消失**——那比报错糟得多。所以这里拒绝，并把出路说清楚。
    """
    if row.builtin:
        raise BadRequestException("自动生成的合集成员由规则决定，不能手工增删")
    if row.rules:
        raise BadRequestException(
            "这是个会自动收录的合集，成员由条件决定。想手工挑片请新建一个合集，"
            "或者在创建时勾「固定现在这些」把它定格成名单"
        )


async def _member_rows(session: AsyncSession, collection_id: int) -> list[CollectionItem]:
    return list(
        (
            await session.execute(
                select(CollectionItem)
                .where(CollectionItem.collection_id == collection_id)
                .order_by(CollectionItem.position, CollectionItem.id)
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/{collection_id}/items",
    response_model=ApiResponse[CollectionView],
    summary="把作品加进手动合集（已在里面的忽略，不报错）",
    operation_id="collection.items.add",
)
async def add_collection_items(
    collection_id: int,
    payload: CollectionItemsPayload,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[CollectionView]:
    """「加入合集」的落点。

    **幂等**：已经在里面的 id 直接跳过，不报错也不重复插。这个动作用户会从
    海报悬浮、详情页、批量选择三处发起，还可能连点两下——把"已经加过了"做成
    错误，只会逼每个调用方先查一遍。
    """

    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    _guard_manual(row)

    existing = await _member_rows(session, collection_id)
    known = {r.media_item_id for r in existing}
    tail = max((r.position for r in existing), default=-1)
    added = 0
    for item_id in payload.media_item_ids:
        if item_id in known:
            continue
        tail += 1
        session.add(
            CollectionItem(collection_id=collection_id, media_item_id=item_id, position=tail)
        )
        known.add(item_id)
        added += 1
    await session.flush()
    view = await _view(
        session, row, member_id=member_id, visible=visible, content_limit=content_limit
    )
    await session.commit()
    message = f"已加入「{row.name}」" if added else f"这些作品已经在「{row.name}」里了"
    return ok(view, message=message)


@router.delete(
    "/{collection_id}/items/{media_item_id}",
    response_model=ApiResponse[CollectionView],
    summary="把一部作品移出手动合集（不动作品本身）",
    operation_id="collection.items.remove",
    # 删的是名单里的一行，作品一部不少，所以是 confirm 而不是 destructive
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def remove_collection_item(
    collection_id: int,
    media_item_id: int,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[CollectionView]:
    """移出去的只是名单里的一行，作品一部都不会少。

    不在名单里也返回成功：与加入同一条幂等口径（用户点两下「移出」不该看到
    一个红色错误）。
    """

    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    _guard_manual(row)

    for old in await _member_rows(session, collection_id):
        if old.media_item_id == media_item_id:
            await session.delete(old)
    await session.flush()
    view = await _view(
        session, row, member_id=member_id, visible=visible, content_limit=content_limit
    )
    await session.commit()
    return ok(view, message=f"已移出「{row.name}」")


@router.put(
    "/{collection_id}/order",
    response_model=ApiResponse[CollectionView],
    summary="手动合集的排序（拖拽结果整体覆盖）",
    operation_id="collection.items.reorder",
)
async def reorder_collection_items(
    collection_id: int,
    payload: CollectionItemsPayload,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[CollectionView]:
    """拖拽出来的顺序整体覆盖 ``position``。

    **没给到的成员留在末尾**（按原有顺序），而不是被删掉：前端可能只把可见的
    那一页传上来，把没传的当成"要删"会在分页的合集里吃掉成员。名单里没有的
    id 忽略——加成员走 ``/items``，这一条只管顺序。

    顺序在海报墙、Jellyfin、分享页三处一致：三处都走 ``resolve_members()``，
    名单驱动那一支就是按 ``position`` 取的。
    """

    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    _guard_manual(row)

    rows = await _member_rows(session, collection_id)
    by_item = {r.media_item_id: r for r in rows}
    position = 0
    for item_id in payload.media_item_ids:
        target = by_item.pop(item_id, None)
        if target is None:
            continue
        target.position = position
        session.add(target)
        position += 1
    for leftover in by_item.values():  # 没传上来的按原序接在后面
        leftover.position = position
        session.add(leftover)
        position += 1
    await session.flush()
    view = await _view(
        session, row, member_id=member_id, visible=visible, content_limit=content_limit
    )
    await session.commit()
    return ok(view, message="顺序已保存")


@router.delete(
    "/{collection_id}",
    response_model=ApiResponse[None],
    summary="删除合集（不动作品本身）",
    operation_id="collection.delete",
    # confirm 而不是 destructive：删的是那层视图，作品一部都不会少
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_collection(
    collection_id: int,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[None]:
    """删的是那层视图，作品一部都不会少——合集从来不拥有作品。

    **同一颗按钮，两种归宿**，由 ``builtin is None`` 推导（与"形态是推导的、
    不存 mode 列"同一条思路）：

    - 用户自建的合集 → 真删行，``collection_item`` 随之级联清掉；
    - 自动生成的合集（内置的「我的收藏」、系列合集）→ 落 ``hidden`` 墓碑。
      真删了下次 ensure 又会把它建回来，用户会觉得"删不掉"；而且行上还挂着
      推导不出来的东西（稳定 id、改过的名字、封面、顺序）。

    藏起来的合集在「显示已隐藏的合集」里能找回来——不可逆的隐藏是单向黑洞。
    """

    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    if row.builtin:
        row.hidden = True
        await session.flush()
        await session.commit()
        return ok(None, message=f"已隐藏「{row.name}」（在「显示已隐藏的合集」里可以放回来）")
    await session.delete(row)
    await session.commit()
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

    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)

    ids = await resolve_members(
        session,
        row,
        member_id=member_id,
        visible_library_ids=visible,
        content_limit=content_limit,
        limit=limit,
        offset=offset,
    )
    if not ids:
        return ok([])
    views = await _aggregate_wall_views(
        session, row.library_id, ids, ids, library_ids=visible
    )
    favorites = await favorite_item_ids(session, ids, member_id=member_id)
    for view in views:
        view.is_favorite = view.media_item_id in favorites
    return ok(views)


@router.get(
    "/{collection_id}/series",
    response_model=ApiResponse[CollectionSeriesView],
    summary="系列合集的「已有 N / 共 M」与缺片名单",
    operation_id="collection.series.get",
)
async def get_collection_series(
    collection_id: int,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[CollectionSeriesView]:
    """把系列合集从「整理」变成「补齐」的那一块。

    只做归类的话，用户装个 Emby 也有；能说出"缺哪两部、点一下去补"的只有
    这个产品。缺的那几部走**现成的订阅入口**（``title_ref="tmdb:movie:{id}"``），
    不需要新的下游链路——只是把两个已有的东西接起来。

    懒加载：第一次打开这个系列时才去拉一次上游档案，之后读快照。
    """

    member_id, visible, content_limit = await _scope(session, principal)
    row = await _get_or_404(session, collection_id)
    _guard_visible(row, member_id, visible)
    if not is_series_collection(row):
        return ok(CollectionSeriesView(available=False))

    parts = await load_series_parts(session, row)
    await session.commit()  # 快照落盘（懒加载只发生一次）
    if not parts:
        return ok(CollectionSeriesView(series_name=row.name, available=False))

    tmdb_ids = [part["tmdb_id"] for part in parts]
    # 「库里有没有」按本合集所在库的在架文件判定，与海报墙同一口径——
    # 详情页说"已有 6 部"、墙上摆着 5 部，那种矛盾比不显示更糟
    owned: dict[int, int] = {}
    query = select(MediaItem.tmdb_id, MediaItem.id).where(
        MediaItem.tmdb_id.in_(tmdb_ids),  # type: ignore[attr-defined]
        MediaItem.kind == MediaKind.MOVIE.value,
    )
    if row.library_id is not None:
        query = query.where(
            select(LibraryFile.id)
            .where(
                LibraryFile.media_item_id == MediaItem.id,
                LibraryFile.library_id == row.library_id,
                LibraryFile.on_shelf(),
            )
            .exists()
        )
    for tmdb_id, item_id in (await session.execute(query)).all():
        owned[int(tmdb_id)] = int(item_id)

    # 已经在追的显示「追踪中」而不是「订阅」——按现成的订阅行判定，不另建状态
    tracked = {
        int(t)
        for t in (
            await session.execute(
                select(MediaItem.tmdb_id)
                .join(Subscription, Subscription.media_item_id == MediaItem.id)
                .where(MediaItem.tmdb_id.in_(tmdb_ids))  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
        if t is not None
    }

    base = get_settings().tmdb_image_base_url.rstrip("/")
    views = [
        SeriesPartView(
            tmdb_id=part["tmdb_id"],
            title=part["title"],
            release_date=_iso_date(part.get("release_date")),
            # 缺片画进海报墙、与库存海报同一规格，w200 放大到卡片尺寸会糊
            poster_url=(f"{base}/w500{part['poster_path']}" if part.get("poster_path") else None),
            media_item_id=owned.get(part["tmdb_id"]),
            subscribed=part["tmdb_id"] in tracked,
        )
        for part in parts
    ]
    return ok(
        CollectionSeriesView(
            series_name=row.name,
            owned_count=sum(1 for v in views if v.media_item_id is not None),
            total=len(views),
            image_url=(f"{base}/w500{row.series_image}" if row.series_image else None),
            parts=views,
        )
    )


@router.post(
    "/{collection_id}/apply-to-library",
    response_model=ApiResponse[None],
    summary="把合集的规则设为某个库的收藏范围",
    operation_id="collection.apply-to-library",
    # 这一条改的是**库配置**（match_rules 决定订阅入哪个库），不是合集本身，
    # 所以它与合集其余接口不同，只对管理员开放
    dependencies=[Depends(require_admin)],
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

    member_id, visible, content_limit = await _scope(session, principal)
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
    await session.commit()
    return ok(None)
