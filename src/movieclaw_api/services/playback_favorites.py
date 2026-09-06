"""媒体库首页「我的收藏」的业务查询。

收藏事实只有一处：``playback_state.is_favorite``（网页详情页的心与 Jellyfin
客户端的心写的是同一列，见 services/playback/marks.py）。首页直接读这张领域表，
并收紧到当前账号可见、文件仍在位的媒体库——权限变更或文件丢失后不再露出。

一部作品只出一格：Jellyfin 客户端可以分别收藏整剧、某一季、某一集，同一部剧
可能有多行收藏，首页按作品去重，取最近一次收藏的那一行说明收藏的层级。
卡片本体复用单库海报墙的聚合视图（海报、库存概况、缺集数同一口径）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.playback import FavoriteItemView
from movieclaw_api.services.library.items import _aggregate_wall_views
from movieclaw_db.models import Library, LibraryFile, PlaybackState
from movieclaw_media.models import MediaKind


async def favorite_items(
    session: AsyncSession,
    *,
    member_id: int,
    visible_library_ids: set[int] | None,
    limit: int,
    offset: int = 0,
) -> tuple[list[FavoriteItemView], int]:
    """返回一个账号收藏的作品（最近收藏的在前）与去重后的总数。

    ``offset`` / ``limit`` 是「全部收藏」海报墙的滚动分页窗口；首页横滚行只取
    最前面一页。同一作品跨库存在时按媒体库首页的展示顺序选择第一个可见库，
    保证卡片有稳定、可访问的详情落点；没有任何可见在位文件的收藏不计入总数。
    """
    if visible_library_ids == set():
        return [], 0

    rows = (
        (
            await session.execute(
                select(PlaybackState)
                .where(
                    PlaybackState.member_id == member_id,
                    PlaybackState.is_favorite.is_(True),  # type: ignore[union-attr]
                )
                .order_by(
                    PlaybackState.updated_at.desc(),  # type: ignore[union-attr]
                    PlaybackState.id.desc(),  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    # 每部作品只留最近收藏的那一行（dict 保持插入顺序 = 最近在前）
    latest: dict[int, PlaybackState] = {}
    for row in rows:
        latest.setdefault(row.media_item_id, row)
    if not latest:
        return [], 0

    # 落点库：作品有在位文件的可见库，按首页库排序取第一个
    library_query = (
        select(LibraryFile.media_item_id, Library.id)
        .join(Library, Library.id == LibraryFile.library_id)  # type: ignore[arg-type]
        .where(
            LibraryFile.media_item_id.in_(list(latest)),  # type: ignore[union-attr]
            LibraryFile.in_place(),
        )
        .order_by(Library.sort_order.asc(), Library.id.asc())  # type: ignore[union-attr]
        .distinct()
    )
    if visible_library_ids is not None:
        library_query = library_query.where(Library.id.in_(visible_library_ids))  # type: ignore[attr-defined]
    library_of: dict[int, int] = {}
    for item_id, library_id in (await session.execute(library_query)).all():
        if item_id is not None and library_id is not None:
            library_of.setdefault(item_id, library_id)

    ordered = [item_id for item_id in latest if item_id in library_of]
    total = len(ordered)
    ordered = ordered[offset : offset + limit]

    by_library: dict[int, list[int]] = {}
    for item_id in ordered:
        by_library.setdefault(library_of[item_id], []).append(item_id)
    views = {}
    for library_id, ids in by_library.items():
        for view in await _aggregate_wall_views(session, library_id, ids, ids):
            views[view.media_item_id] = view

    result: list[FavoriteItemView] = []
    for item_id in ordered:
        view = views.get(item_id)
        if view is None:
            continue
        state = latest[item_id]
        # 收藏层级由哨兵单元翻译：整剧 (-1,-1) / 整季 (s,-1) / 单集 (s,e)；
        # 电影的 (0,0) 是播放单元哨兵而不是季集，不外泄
        is_tv = view.kind == MediaKind.TV
        result.append(
            FavoriteItemView(
                **view.model_dump(),
                library_id=library_of[item_id],
                favorite_season_number=(
                    state.season_number if is_tv and state.season_number >= 0 else None
                ),
                favorite_episode_number=(
                    state.episode_number if is_tv and state.episode_number >= 0 else None
                ),
            )
        )
    return result, total
