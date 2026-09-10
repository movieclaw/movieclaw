"""成员级推荐行（docs/design/library-filtering.md F5）。

**没有模型，也不该有。** 一个家庭 NAS 里一个人的观看记录常常只有几十条，
协同过滤和向量召回在这个数据量上算不出比"你常看动画，这几部动画你还没看"
更好的结果，却要背上一整套离线训练与在线服务。这里做的是**说得清楚的推荐**：
每一行都能用一句中文讲明白它为什么在这儿，而那句话就写在行标题上。

三种行，全部从**这个人自己的**观看记录长出来：

1. 「因为你看了《X》」——同一个作品系列里他还没看的（系列合集已经把这层
   关系落成了 ``series_key``，白拿）；
2. 「你常看的类型」——他看得最多的那个类型下评分高、还没看的；
3. 「继续看下去」——追到一半的剧（有续播点、还没看完）。

三条都收敛在一次库内查询上：推荐行是首页的一块，不该为它引入后台作业、
也不该让首页多等几百毫秒。看得少（记录不足）时**宁可不出这一行**，而不是
拿全库热门凑数——"给你推荐"却推的是所有人都一样的东西，比没有更让人失望。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, nullslast, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.library import LibraryItemView
from movieclaw_api.services.library.access import ContentLimit
from movieclaw_api.services.library.items import (
    LibraryFilter,
    _aggregate_wall_views,
    _wall_page_ids,
)
from movieclaw_db.models import LibraryFile, MediaItem, MediaMetadata, PlaybackState

#: 一行摆几部。与首页其他横排同一口径（再多用户也推不完）
ROW_SIZE = 12

#: 少于这么多条观看记录就不出推荐行。样本太少时推出来的东西是噪音，
#: 而"给你推荐"推错比不推更伤
MIN_HISTORY = 3


@dataclass
class RecommendRow:
    """一行推荐：标题就是它的理由。"""

    key: str
    title: str
    #: 副标题：把理由说完整（"你看过 4 部动画"）；没有就不显示
    reason: str | None = None
    items: list[LibraryItemView] = field(default_factory=list)


async def _watched_item_ids(session: AsyncSession, member_id: int, library_id: int) -> list[int]:
    """这个人在这个库里看过（或看到一半）的条目，最近的在前。"""
    rows = (
        await session.execute(
            select(PlaybackState.media_item_id, func.max(PlaybackState.last_played_at))
            .join(LibraryFile, LibraryFile.media_item_id == PlaybackState.media_item_id)
            .where(
                PlaybackState.member_id == member_id,
                # 手工标「已看完」的也算：用户明说了他看过，不能因为没经过
                # 我们的播放器就当没发生（很多人在别处看完再回来标记）
                or_(
                    PlaybackState.last_played_at.is_not(None),  # type: ignore[union-attr]
                    PlaybackState.played.is_(True),
                ),
                LibraryFile.library_id == library_id,
                LibraryFile.on_shelf(),
            )
            .group_by(PlaybackState.media_item_id)
            .order_by(nullslast(func.max(PlaybackState.last_played_at).desc()))
        )
    ).all()
    return [int(i) for i, _ in rows]


async def _played_ids(session: AsyncSession, member_id: int) -> set[int]:
    """已经看完的条目——推荐里不该再出现它们。"""
    return {
        int(i)
        for i in (
            await session.execute(
                select(PlaybackState.media_item_id).where(
                    PlaybackState.member_id == member_id, PlaybackState.played.is_(True)
                )
            )
        )
        .scalars()
        .all()
        if i is not None
    }


async def _views(
    session: AsyncSession, library_id: int, ids: list[int], member_id: int
) -> list[LibraryItemView]:
    """一批 id → 卡片。与海报墙同一份聚合，所以卡片长得一模一样。"""
    if not ids:
        return []
    return await _aggregate_wall_views(session, library_id, ids, ids)


async def build_recommendations(
    session: AsyncSession,
    library_id: int,
    kind: str,
    *,
    member_id: int,
    content_limit: ContentLimit | None = None,
) -> list[RecommendRow]:
    """这个人在这个库里的推荐行；没有可说的理由时返回空表。"""
    history = await _watched_item_ids(session, member_id, library_id)
    if len(history) < MIN_HISTORY:
        return []
    played = await _played_ids(session, member_id)
    rows: list[RecommendRow] = []

    # —— 1. 追到一半的剧：最直接的"接着看"，排在最前 ——
    resuming = [
        int(i)
        for i in (
            await session.execute(
                select(PlaybackState.media_item_id)
                .join(LibraryFile, LibraryFile.media_item_id == PlaybackState.media_item_id)
                .where(
                    PlaybackState.member_id == member_id,
                    PlaybackState.position_ms > 0,
                    PlaybackState.played.is_(False),
                    LibraryFile.library_id == library_id,
                    LibraryFile.on_shelf(),
                )
                .order_by(PlaybackState.last_played_at.desc())  # type: ignore[union-attr]
                .distinct()
                .limit(ROW_SIZE)
            )
        )
        .scalars()
        .all()
        if i is not None
    ]
    if resuming:
        rows.append(
            RecommendRow(
                key="resume",
                title="接着看",
                items=await _views(session, library_id, resuming, member_id),
            )
        )

    # —— 2. 同系列里还没看的：关系是现成的（series_key），理由也最硬 ——
    keys = [
        str(k)
        for k in (
            await session.execute(
                select(MediaMetadata.series_key)
                .where(
                    MediaMetadata.media_item_id.in_(history[:20]),  # type: ignore[attr-defined]
                    MediaMetadata.series_key.is_not(None),  # type: ignore[union-attr]
                    MediaMetadata.series_key != "",
                )
                .distinct()
            )
        )
        .scalars()
        .all()
        if k
    ]
    if keys:
        candidates = await _wall_page_ids(
            session,
            library_id,
            "release_date_asc",
            None,
            0,
            "confirmed",
            LibraryFilter(series_keys=tuple(keys)),
            member_id,
            content_limit,
        )
        unseen = [i for i in candidates if i not in played][:ROW_SIZE]
        if unseen:
            names = (
                await session.execute(
                    select(MediaItem.title).where(MediaItem.id == history[0])  # type: ignore[arg-type]
                )
            ).scalar_one_or_none()
            rows.append(
                RecommendRow(
                    key="series",
                    title="同系列里你还没看的",
                    reason=f"因为你看了《{names}》" if names else None,
                    items=await _views(session, library_id, unseen, member_id),
                )
            )

    # —— 3. 常看的类型：从他自己的记录里数出来，不是全库热门 ——
    genre_counts: dict[int, int] = {}
    for raw in (
        await session.execute(
            select(MediaMetadata.genre_ids).where(
                MediaMetadata.media_item_id.in_(history)  # type: ignore[attr-defined]
            )
        )
    ).scalars():
        for genre_id in raw or []:
            genre_counts[int(genre_id)] = genre_counts.get(int(genre_id), 0) + 1
    if genre_counts:
        top_genre, seen_count = max(genre_counts.items(), key=lambda pair: pair[1])
        candidates = await _wall_page_ids(
            session,
            library_id,
            "rating",
            None,
            0,
            "confirmed",
            LibraryFilter(genres=(top_genre,)),
            member_id,
            content_limit,
        )
        unseen = [i for i in candidates if i not in played and i not in history][:ROW_SIZE]
        if unseen:
            from movieclaw_media.genres import genre_label

            # 类型名按库的形态查（电影与剧集的 TMDB genre 表不是同一张）
            label = genre_label(kind, top_genre)
            rows.append(
                RecommendRow(
                    key=f"genre:{top_genre}",
                    title=f"你常看{label}",
                    reason=f"这个库里你看过 {seen_count} 部{label}",
                    items=await _views(session, library_id, unseen, member_id),
                )
            )
    return rows
