"""媒体库首页与「全部收藏」页「我的收藏」的业务查询。

收藏事实只有一处：``playback_state.is_favorite``（网页详情页的心与 Jellyfin
客户端的心写的是同一列，见 services/playback/marks.py）。首页直接读这张领域表，
并收紧到当前账号可见、文件仍在位的媒体库——权限变更或文件丢失后不再露出。

一部作品只出一格：Jellyfin 客户端可以分别收藏整剧、某一季、某一集，同一部剧
可能有多行收藏，首页按作品去重，取最近一次收藏的那一行说明收藏的层级。
卡片本体复用单库海报墙的聚合视图（海报、库存概况、缺集数同一口径）。

「全部收藏」页与单库页一样有两种浏览形态，两者共用 :func:`_favorite_page`
定下的同一份名单：海报墙走 :func:`favorite_items`，图床浏览（瀑布流）走
:func:`favorite_gallery`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.library import LibraryGalleryGroupView
from movieclaw_api.schemas.playback import FavoriteItemView
from movieclaw_api.services.library.items import _aggregate_wall_views, build_gallery_groups
from movieclaw_db.models import Library, LibraryFile, PlaybackState
from movieclaw_media.models import MediaKind


async def _favorite_page(
    session: AsyncSession,
    *,
    member_id: int,
    visible_library_ids: set[int] | None,
    limit: int,
    offset: int,
    unwatched_first: bool = False,
) -> tuple[list[tuple[int, int]], int, dict[int, PlaybackState]]:
    """收藏墙与收藏图廊共用的一页名单：``[(条目 id, 落点库 id)]`` + 去重总数 +
    每部作品最近那一行收藏状态（层级文案要用）。

    默认**最近收藏的在前**——按 ``favorited_at`` 而不是 ``updated_at``：后者
    任何写入都会动（进度上报、标记已看、记忆轨选择），会把"两年前收藏、昨晚
    看过一遍"的片顶到最前面，那不是用户理解的"最近收藏"。

    ``unwatched_first`` 只有**首页那一行**会传：收藏在这个产品里更接近"想看
    清单"而不是"珍藏架"（珍藏架已经有手动合集这个更好的归宿），所以首页把
    还没看完的整体提前；一部都不少，看过的心头好只是靠后。``/library/favorites``
    全量页不传——那里是"我收藏过什么"的完整账本，该老老实实按收藏时间排。

    同一作品跨库存在时按媒体库首页的展示顺序选择第一个可见库，保证这一格有
    稳定、可访问的详情落点；没有任何可见在位文件的收藏不计入总数。
    海报墙与图廊必须走同一份名单——两种视图翻的是同一批作品、同一个顺序，
    切换视图时看到的不能是两份内容（两者都不传 ``unwatched_first``，所以仍然一致）。
    """
    if visible_library_ids == set():
        return [], 0, {}

    rows = (
        (
            await session.execute(
                select(PlaybackState)
                .where(
                    PlaybackState.member_id == member_id,
                    PlaybackState.is_favorite.is_(True),  # type: ignore[union-attr]
                )
                .order_by(
                    # 存量行的 favorited_at 由迁移回填成 updated_at；真的为空时
                    # 排在最后，而不是被 SQLite 当成最小值顶到最前
                    PlaybackState.favorited_at.desc().nullslast(),  # type: ignore[union-attr]
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
        return [], 0, {}

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
    if unwatched_first:
        # 稳定排序：没看完的整体提前，组内仍是收藏时间倒序。
        # 判据用的是**收藏那一行自己的 played**——电影准确；剧集收藏整剧时
        # 哨兵行的 played 基本恒为假，于是剧一律靠前。这个近似偏向"早点露出"，
        # 而剧"没追完"本来就是常态；要精确就得把 up-next 那套单元遍历搬过来，
        # 为一行的排序不值当。
        ordered.sort(key=lambda item_id: latest[item_id].played)
    total = len(ordered)
    page = [(item_id, library_of[item_id]) for item_id in ordered[offset : offset + limit]]
    return page, total, latest


async def favorite_items(
    session: AsyncSession,
    *,
    member_id: int,
    visible_library_ids: set[int] | None,
    limit: int,
    offset: int = 0,
    unwatched_first: bool = False,
) -> tuple[list[FavoriteItemView], int]:
    """返回一个账号收藏的作品与去重后的总数。

    ``offset`` / ``limit`` 是「全部收藏」海报墙的滚动分页窗口；首页横滚行只取
    最前面一页。卡片本体复用单库海报墙的聚合视图，因此按落点库分组聚合、
    再按名单顺序拼回来。
    """
    page, total, latest = await _favorite_page(
        session,
        member_id=member_id,
        visible_library_ids=visible_library_ids,
        limit=limit,
        offset=offset,
        unwatched_first=unwatched_first,
    )
    if not page:
        return [], total

    by_library: dict[int, list[int]] = {}
    for item_id, library_id in page:
        by_library.setdefault(library_id, []).append(item_id)
    views = {}
    for library_id, ids in by_library.items():
        for view in await _aggregate_wall_views(session, library_id, ids, ids):
            views[view.media_item_id] = view

    result: list[FavoriteItemView] = []
    # 落点库不用再传一次：``by_library`` 就是按它分的组，``_aggregate_wall_views``
    # 拿到的 library_id 与它同一个值，视图里已经带着了（跨库合集落地后
    # LibraryItemView 才有这一列，此前这里是唯一给得出落点的地方）
    for item_id, _landing_library_id in page:
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
                favorite_season_number=(
                    state.season_number if is_tv and state.season_number >= 0 else None
                ),
                favorite_episode_number=(
                    state.episode_number if is_tv and state.episode_number >= 0 else None
                ),
            )
        )
    return result, total


async def favorite_gallery(
    session: AsyncSession,
    *,
    member_id: int,
    visible_library_ids: set[int] | None,
    limit: int,
    offset: int = 0,
) -> list[LibraryGalleryGroupView]:
    """「我的收藏」的图床浏览模式数据源：与收藏海报墙同一份名单、同一个顺序。

    单库图廊是 ``/libraries/{id}/gallery``，一面墙只有一个库；收藏是跨库的一面
    墙，所以每组各带自己的落点库（``library_id``），段标题与灯箱的「前往详情」
    据此拼地址。分页口径与单库图廊一致：``offset`` / ``limit`` 都按**作品**数，
    没有图的作品也占一组，前端靠"拿到的组数是否满一页"判断还有没有下一页。
    """
    page, _total, _latest = await _favorite_page(
        session,
        member_id=member_id,
        visible_library_ids=visible_library_ids,
        limit=limit,
        offset=offset,
    )
    return await build_gallery_groups(session, page, member_id=member_id)
