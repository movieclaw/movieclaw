"""媒体库首页「接下来继续」的业务查询。

这一块回答的是**"现在点哪个能接着放"**，不是"我最近看了什么"。后者是后视镜：
看完的片摆在首页最贵的位置上，除了陈述事实什么也做不了；而看完一集之后，
用户真正要干的事是看下一集——旧版卡片写着「S01E03 已看完」，他得点进剧、
找到季、滚到 E04，三步。

整套规则只有一条：

    展示单元 = 从锚点起、按季集正序，第一个**没看完**且**文件在位**的单元；
    一个都没有就不出这张卡。

锚点是这部作品最近播放过的那个单元。这一条规则自然覆盖了全部情形，不需要
分电影/剧集写分支：

- 电影没看完 → 锚点就是 (0,0) 本身，继续看；
- 电影看完了 → 后面没有单元了，卡片消失；
- 剧集当前集没看完 → 锚点本身，继续看（**不跳下一集**）；
- 剧集当前集看完了 → 往后第一个没看完的，通常就是下一集；
- 整部剧看完 → 消失；之后新入库一集，它自己回来——这正是「接下来」该有的语义。

**"看完"只认 ``played``**，不认"碰过"。旧版判"看过"用的是
``played OR last_played_at 非空``，那条口径放在这里会出错：先看了 E04 一半、
又回头补完 E03，锚点变成 E03，而 E04 因为"碰过"被跳掉，卡片指向 E05——
用户看了一半的那集反而回不去了。把没播完的单元算作"能接着看"，正是这块的本职。

Jellyfin 只负责把播放事实写进 ``playback_state``；首页直接读领域表，不反向
调用协议接口。查询收紧到当前账号可见、文件仍在位的库，权限变更或文件丢失后
不继续泄露条目。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.playback import UpNextItemView
from movieclaw_api.services.library.thumbs import primary_aspect
from movieclaw_api.services.media_scrape import asset_version
from movieclaw_db.models import (
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    PlaybackState,
)
from movieclaw_media.models import MediaKind

#: 锚点最多往回扫几部作品。全都看完的用户理论上要扫遍整张表才找得到那一部
#: 没看完的；给它一个地平线，代价是"比这 200 部都更久以前看的那一部半截片"
#: 不再出现在首页——它本来也已经不是"接下来"了。
_ANCHOR_SCAN = 200

#: 季集单元的排序键。``(季, 集)`` 字典序，与库存行、角标口径同一套
Unit = tuple[int, int]


@dataclass(frozen=True)
class _Anchor:
    """一部作品最近播放的那个单元——决定"从哪往后找"和"排在第几张"。"""

    media_item_id: int
    unit: Unit
    last_played_at: datetime


def _runtime_ms(
    file_duration_seconds: int | None,
    episode_runtime_minutes: int | None,
    item_runtime_minutes: int | None,
) -> int | None:
    """时长优先取真实文件，其次分集档案，最后退回条目常规片长。"""
    if file_duration_seconds and file_duration_seconds > 0:
        return file_duration_seconds * 1000
    runtime_minutes = episode_runtime_minutes or item_runtime_minutes
    return runtime_minutes * 60_000 if runtime_minutes and runtime_minutes > 0 else None


def _progress_percent(position_ms: int, duration_ms: int | None) -> int | None:
    """生成卡片进度；保留 1%~99%——0 与 100 都不是"接着看"的状态。"""
    if position_ms <= 0 or not duration_ms:
        return None
    return max(1, min(99, round(position_ms * 100 / duration_ms)))


async def _anchors(session: AsyncSession, member_id: int) -> list[_Anchor]:
    """每部作品最近播放的单元，最近的在前。

    在库内按作品取最新一行，避免连看一整季时同一部剧铺满整行。哨兵单元
    （整剧 ``(-1,-1)`` 的收藏行）不是播放事实，排除在外。
    """
    ranked = (
        select(
            PlaybackState.media_item_id.label("media_item_id"),  # type: ignore[attr-defined]
            PlaybackState.season_number.label("season_number"),
            PlaybackState.episode_number.label("episode_number"),
            PlaybackState.last_played_at.label("last_played_at"),
            func.row_number()
            .over(
                partition_by=PlaybackState.media_item_id,
                order_by=(PlaybackState.last_played_at.desc(), PlaybackState.id.desc()),
            )
            .label("item_rank"),
        )
        .where(
            PlaybackState.member_id == member_id,
            PlaybackState.last_played_at.is_not(None),  # type: ignore[union-attr]
            PlaybackState.season_number >= 0,
            PlaybackState.episode_number >= 0,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                ranked.c.media_item_id,
                ranked.c.season_number,
                ranked.c.episode_number,
                ranked.c.last_played_at,
            )
            .where(ranked.c.item_rank == 1)
            .order_by(ranked.c.last_played_at.desc(), ranked.c.media_item_id.asc())
            .limit(_ANCHOR_SCAN)
        )
    ).all()
    return [
        _Anchor(int(item_id), (int(season), int(episode)), played_at)
        for item_id, season, episode, played_at in rows
        if played_at is not None
    ]


async def _in_place_units(
    session: AsyncSession, item_ids: list[int], visible_library_ids: set[int] | None
) -> dict[int, dict[Unit, int]]:
    """作品 → {季集单元: 落点库}。只要在位文件，且库对当前身份可见。

    同一集的多个版本（1080p / 2160p）会有多行；落点库按媒体库首页的展示顺序
    取第一个，保证卡片有稳定、可访问的详情落点。按 ``media_item_id IN (...)``
    取数打的是 ``ix_library_file_browse_unit`` 的前缀，只扫这几部作品的库存行，
    不会为整张台账建临时索引。
    """
    if not item_ids:
        return {}
    statement = (
        select(
            LibraryFile.media_item_id,
            LibraryFile.season_number,
            LibraryFile.episode_number,
            Library.id,
        )
        .join(Library, Library.id == LibraryFile.library_id)
        .where(
            LibraryFile.media_item_id.in_(item_ids),  # type: ignore[attr-defined]
            # 在位口径：失联与待回收的都不算"能接着看"。走 in_place() 而不是
            # 裸比较——它是全站共享的唯一判别，将来加状态时消费方零改动
            LibraryFile.in_place(),
            LibraryFile.season_number >= 0,
            LibraryFile.episode_number >= 0,
        )
        .order_by(Library.sort_order.asc(), Library.id.asc())
    )
    if visible_library_ids is not None:
        statement = statement.where(Library.id.in_(visible_library_ids))  # type: ignore[attr-defined]

    units: dict[int, dict[Unit, int]] = {}
    for item_id, season, episode, library_id in (await session.execute(statement)).all():
        by_unit = units.setdefault(int(item_id), {})
        by_unit.setdefault((int(season), int(episode)), int(library_id))
    return units


async def _states(
    session: AsyncSession, member_id: int, item_ids: list[int]
) -> dict[tuple[int, Unit], tuple[bool, int]]:
    """(作品, 单元) → (看完了吗, 续播点毫秒)。"""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(
                PlaybackState.media_item_id,
                PlaybackState.season_number,
                PlaybackState.episode_number,
                PlaybackState.played,
                PlaybackState.position_ms,
            ).where(
                PlaybackState.member_id == member_id,
                PlaybackState.media_item_id.in_(item_ids),  # type: ignore[attr-defined]
            )
        )
    ).all()
    return {
        (int(item_id), (int(season), int(episode))): (bool(played), int(position or 0))
        for item_id, season, episode, played, position in rows
    }


async def up_next_items(
    session: AsyncSession,
    *,
    member_id: int,
    visible_library_ids: set[int] | None,
    limit: int,
) -> list[UpNextItemView]:
    """一个账号"接下来该接着看"的卡片，最近动过的在前。"""
    if visible_library_ids == set():
        return []

    anchors = await _anchors(session, member_id)
    if not anchors:
        return []
    item_ids = [a.media_item_id for a in anchors]
    units_by_item = await _in_place_units(session, item_ids, visible_library_ids)
    states = await _states(session, member_id, item_ids)

    # —— 一条规则：锚点起第一个没看完、且文件在位的单元 ——
    picks: list[tuple[_Anchor, Unit, int, int]] = []  # (锚点, 展示单元, 落点库, 后面还剩几个)
    for anchor in anchors:
        available = units_by_item.get(anchor.media_item_id)
        if not available:
            continue
        pending = sorted(
            unit
            for unit in available
            if unit >= anchor.unit and not states.get((anchor.media_item_id, unit), (False, 0))[0]
        )
        if not pending:
            continue
        display = pending[0]
        picks.append((anchor, display, available[display], len(pending) - 1))
        if len(picks) >= limit:
            break
    if not picks:
        return []

    return await _hydrate(session, picks, states)


async def _hydrate(
    session: AsyncSession,
    picks: list[tuple[_Anchor, Unit, int, int]],
    states: dict[tuple[int, Unit], tuple[bool, int]],
) -> list[UpNextItemView]:
    """展示单元 → 卡片：条目档案、分集档案、真实时长。

    分集元数据必须按**展示单元**取而不是锚点：卡片指向下一集时，标题、剧照与
    时长都该是下一集的。第一版这里沿用了锚点，界面上表现为"E04 的卡片写着
    E03 的集名"。
    """
    item_ids = [anchor.media_item_id for anchor, _, _, _ in picks]
    rows = (
        await session.execute(
            select(
                MediaItem,
                MediaMetadata.poster_file,
                MediaMetadata.poster_width,
                MediaMetadata.poster_height,
                MediaMetadata.backdrop_file,
                MediaMetadata.runtime_minutes,
            )
            .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)
            .where(MediaItem.id.in_(item_ids))  # type: ignore[attr-defined]
        )
    ).all()
    archive = {row[0].id: row for row in rows}

    # 分集档案与真实时长各批一次：展示单元散落在不同作品的不同季集上，
    # 逐张卡片查一次就是 N 次往返
    wanted = {(anchor.media_item_id, unit) for anchor, unit, _, _ in picks}
    episodes = {
        (int(i), (int(s), int(e))): (name, runtime, still_file, still_path)
        for i, s, e, name, runtime, still_file, still_path in (
            await session.execute(
                select(
                    MediaEpisode.media_item_id,
                    MediaEpisode.season_number,
                    MediaEpisode.episode_number,
                    MediaEpisode.name,
                    MediaEpisode.runtime_minutes,
                    MediaEpisode.still_file,
                    MediaEpisode.still_path,
                ).where(MediaEpisode.media_item_id.in_(item_ids))  # type: ignore[attr-defined]
            )
        ).all()
        if (int(i), (int(s), int(e))) in wanted
    }
    durations = {
        (int(i), (int(s), int(e))): int(d or 0)
        for i, s, e, d in (
            await session.execute(
                select(
                    LibraryFile.media_item_id,
                    LibraryFile.season_number,
                    LibraryFile.episode_number,
                    func.max(LibraryFile.duration_seconds),
                )
                .where(
                    LibraryFile.media_item_id.in_(item_ids),  # type: ignore[attr-defined]
                    LibraryFile.in_place(),
                )
                .group_by(
                    LibraryFile.media_item_id,
                    LibraryFile.season_number,
                    LibraryFile.episode_number,
                )
            )
        ).all()
    }

    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    result: list[UpNextItemView] = []
    for anchor, unit, library_id, ahead in picks:
        row = archive.get(anchor.media_item_id)
        if row is None:
            continue
        item, poster_file, poster_width, poster_height, backdrop_file, item_runtime = row
        if item.id is None:
            continue
        if poster_file:
            poster_url = f"/images/assets/{poster_file}?v={asset_version(poster_file)}"
        else:
            poster_url = f"{image_base}/w500{item.poster_path}" if item.poster_path else None
        if backdrop_file:
            backdrop_url = f"/images/assets/{backdrop_file}?v={asset_version(backdrop_file)}"
        else:
            backdrop_url = f"{image_base}/w780{item.backdrop_path}" if item.backdrop_path else None

        is_tv = item.kind == MediaKind.TV.value
        episode_name, episode_runtime, still_file, still_path = episodes.get(
            (item.id, unit), (None, None, None, None)
        )
        episode_still_url = None
        if is_tv:
            if still_file:
                episode_still_url = f"/images/assets/{still_file}?v={asset_version(still_file)}"
            elif still_path:
                episode_still_url = f"{image_base}/w500{still_path}"
        duration_ms = _runtime_ms(
            durations.get((item.id, unit)),
            episode_runtime if is_tv else None,
            item_runtime,
        )
        position_ms = states.get((item.id, unit), (False, 0))[1]
        result.append(
            UpNextItemView(
                media_item_id=item.id,
                library_id=library_id,
                kind=MediaKind(item.kind),
                title=item.title,
                year=item.year,
                poster_url=poster_url,
                poster_aspect=primary_aspect(item, poster_width, poster_height),
                backdrop_url=backdrop_url,
                episode_still_url=episode_still_url,
                season_number=unit[0],
                episode_number=unit[1],
                episode_title=episode_name or None,
                # 卡片上这一集**之后**还剩几个没看完的，不是相对锚点算
                unwatched_ahead_count=ahead if is_tv else 0,
                position_ms=position_ms,
                duration_ms=duration_ms,
                progress_percent=_progress_percent(position_ms, duration_ms),
                advanced=unit != anchor.unit,
                last_played_at=anchor.last_played_at,
            )
        )
    return result


__all__ = ["_progress_percent", "_runtime_ms", "up_next_items"]
