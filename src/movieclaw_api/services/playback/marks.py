"""已看 / 收藏标记的**唯一**落点：观看状态 + webhook 事件。

网页端（``/playback/marks``）与 Jellyfin 协议（``/UserPlayedItems`` /
``/UserFavoriteItems``）两条入口都汇到这里，差别只在身份来源与目标的表达
方式：Jellyfin 用结构化 GUID，网页端用 ``(media_item_id, season, episode)``。
两边各自翻译成 :class:`MarkTarget` 之后，其余三件事全部由本模块一次做完：

1. 目标 → 受影响单元的解析（整剧/整季标记已看要级联到全部集；收藏则落在
   哨兵单元上，绝不污染 S00E00）；
2. ``playback_state`` 落库（``movieclaw_playback.state`` 的同一套语义）；
3. webhook 事件（``playback.marked_played`` / ``item.favorited`` …），否则配了
   推送的用户会发现「在 Infuse 里点心有通知、在网页上点没有」。

与 ``watch.py``（播放上报）同一思路：协议层只做翻译，落库与事件不允许分叉，
「在网页上点了心、Infuse 里立刻能看到」因此天然成立——读写的是同一张表。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.webhook import emit_events
from movieclaw_db.models import LibraryFile, MediaEpisode, MediaItem
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import (
    ClientInfo,
    build_favorite_event,
    build_marked_events,
)
from movieclaw_playback.state import Unit


@dataclass(frozen=True)
class MarkTarget:
    """标记的目标：条目（电影 / 整剧）、一季或一集。

    ``season`` / ``episode`` 为 None 表示「整个上一级」：``(None, None)`` 是
    整个条目，``(s, None)`` 是整季，``(s, e)`` 是单集。电影既可以用 ``(None,
    None)`` 也可以用哨兵 ``(0, 0)`` 表达，两者解析到同一个单元。
    """

    media_item_id: int
    season: int | None = None
    episode: int | None = None

    @property
    def is_item(self) -> bool:
        return self.season is None

    @property
    def is_season(self) -> bool:
        return self.season is not None and self.episode is None

    @property
    def is_episode(self) -> bool:
        return self.season is not None and self.episode is not None


@dataclass(frozen=True)
class MarkState:
    """目标在当前成员名下的标记状态（读接口与写接口的统一返回）。"""

    played: bool
    is_favorite: bool
    #: 文件夹（整剧/整季）尚未看完的单元数；单集与电影为 None
    unplayed_count: int | None = None


# ---------------------------------------------------------------------------
# 目标 → 单元
# ---------------------------------------------------------------------------


async def resolve_played_units(session: AsyncSession, target: MarkTarget) -> list[Unit]:
    """目标 → 受「已看」标记影响的单元列表（文件夹级联）。

    以在位文件为准；文件全部丢失的剧（条目仍在）退回元数据集清单——真
    Jellyfin 只要条目存在就允许手动标记已看，不应因文件不在位而 404。
    电影没有任何文件行时退回哨兵单元 ``(0, 0)``。空列表 = 目标不存在。
    """
    if target.is_episode:
        assert target.season is not None and target.episode is not None
        return [(target.media_item_id, target.season, target.episode)]
    rows = list(
        (
            await session.execute(
                select(LibraryFile.season_number, LibraryFile.episode_number).where(
                    LibraryFile.media_item_id == target.media_item_id,
                    LibraryFile.in_place(),
                )
            )
        ).all()
    )
    if not rows:
        rows = list(
            (
                await session.execute(
                    select(MediaEpisode.season_number, MediaEpisode.episode_number).where(
                        MediaEpisode.media_item_id == target.media_item_id
                    )
                )
            ).all()
        )
    units = sorted({(target.media_item_id, s, e) for s, e in rows})
    if target.is_season:
        return [u for u in units if u[1] == target.season]
    # 电影 = (0,0) 单元；剧 = 全部集
    return units or [(target.media_item_id, 0, 0)]


def item_favorite_unit(media_item_id: int, kind: str | None) -> Unit:
    """条目级收藏的落点单元（已知 kind 时的同步版）。

    批量场景（图廊一页几十部作品要知道各自收藏没有）用它，免得为每部作品
    回查一次 ``MediaItem``；哨兵的取值只在这里定义一次，读写两侧共用。
    """
    return (media_item_id, -1, -1) if kind == "tv" else (media_item_id, 0, 0)


async def favorite_unit(session: AsyncSession, target: MarkTarget) -> Unit:
    """收藏的落点单元：叶子用真实单元；整季/整剧用哨兵 ``(s,-1)`` / ``(-1,-1)``
    ——与 Jellyfin 兼容层 ``catalog._folder_user_data`` 的读取侧约定一致。"""
    if target.is_episode:
        assert target.season is not None and target.episode is not None
        return (target.media_item_id, target.season, target.episode)
    if target.is_season:
        assert target.season is not None
        return (target.media_item_id, target.season, -1)
    item = await session.get(MediaItem, target.media_item_id)
    return item_favorite_unit(target.media_item_id, item.kind if item is not None else None)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


async def get_state(session: AsyncSession, target: MarkTarget, *, member_id: int) -> MarkState:
    """目标的已看 / 收藏状态。文件夹的「已看」= 全部单元都已看（无单元视为
    已看，对齐 Jellyfin 的 Folder 语义），并附未看单元数。"""
    units = await resolve_played_units(session, target)
    fav_unit = await favorite_unit(session, target)
    states = await playback_state.get_states(session, [target.media_item_id], member_id=member_id)
    is_favorite = bool((st := states.get(fav_unit)) and st.is_favorite)
    # 收藏落点不是哨兵 ⇔ 目标是叶子（单集 / 电影）：已看直接读该单元
    if fav_unit[1] >= 0 and fav_unit[2] >= 0:
        st = states.get(fav_unit)
        return MarkState(played=bool(st and st.played), is_favorite=is_favorite)
    unplayed = sum(1 for u in units if not ((st := states.get(u)) and st.played))
    return MarkState(played=unplayed == 0, is_favorite=is_favorite, unplayed_count=unplayed)


# ---------------------------------------------------------------------------
# 写（落库 + commit + 事件）
# ---------------------------------------------------------------------------


async def set_played(
    session: AsyncSession,
    target: MarkTarget,
    *,
    member_id: int,
    client: ClientInfo,
    played: bool,
    date_played: datetime | None = None,
) -> bool:
    """标记已看 / 取消已看，级联到目标下全部单元；返回是否命中了单元。

    已看：``date_played`` 才 +1 播放次数，否则 ``max(count, 1)``；取消：全部
    清零（对齐 Jellyfin 的 ``ResetPlayedState``，不是减一）。commit 后逐单元
    发事件，级联的多条共享 batch_id 供下游聚合。
    """
    units = await resolve_played_units(session, target)
    if not units:
        return False
    if played:
        await playback_state.mark_played(
            session, units, member_id=member_id, date_played=date_played
        )
    else:
        await playback_state.mark_unplayed(session, units, member_id=member_id)
    await session.commit()
    events = await build_marked_events(
        session,
        "playback.marked_played" if played else "playback.marked_unplayed",
        units,
        member_id=member_id,
        client=client,
    )
    emit_events(events)
    return True


async def set_favorite(
    session: AsyncSession,
    target: MarkTarget,
    *,
    member_id: int,
    client: ClientInfo,
    favorite: bool,
) -> None:
    """收藏 / 取消收藏；commit 后发 ``item.(un)favorited`` 事件。"""
    unit = await favorite_unit(session, target)
    await playback_state.set_favorite(session, unit, member_id=member_id, favorite=favorite)
    await session.commit()
    event = await build_favorite_event(session, unit, favorite=favorite, client=client)
    if event is not None:
        emit_events([event])
