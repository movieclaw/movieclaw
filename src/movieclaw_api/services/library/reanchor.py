"""改锚原语：一组文件从旧条目单元换到新条目单元时的标准动作
（docs/design/library-other-kind.md 4.9）。

四个调用方共用同一套动作，谁都不许各自拼：

- 扫描自动转正：影视库里临时本地身份的文件，TMDB 后来认出来了；
- 人工认领 / 重新识别：用户把文件挂到另一条目；
- 将来的库类型转换。

动作只有三步：改 ``library_file`` 的锚与季集号 → 把观看状态从旧单元
**复制**到新单元（文件行是两代条目之间唯一稳定的桥，进度不能因为换了
身份就丢）→ 旧条目交给孤儿清理（无文件无订阅即删）。清理必须在调用方
提交事务之后做（它自己开会话），所以本模块只返回旧条目 id 集合。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import LibraryFile, PlaybackState, utcnow

logger = logging.getLogger("movieclaw_api.library_reanchor")

Unit = tuple[int, int, int]


async def migrate_watch_state(session: AsyncSession, old_unit: Unit, new_unit: Unit) -> int:
    """把旧单元的观看状态（每个成员一行）复制到新单元；返回处理的行数。

    目标单元已有状态时按"更靠后的进度赢"合并：``last_played_at`` 较晚的
    一份决定进度与轨道记忆，已看/收藏取并，播放次数取较大者。旧行不删
    ——旧条目若被孤儿清理删除，外键级联会带走它；若旧条目仍有别的文件
    （同一部作品的另一版本仍挂在旧锚上），旧进度理应保留。
    """
    if old_unit == new_unit:
        return 0
    old_item, old_season, old_episode = old_unit
    new_item, new_season, new_episode = new_unit
    rows = (
        (
            await session.execute(
                select(PlaybackState).where(
                    PlaybackState.media_item_id == old_item,
                    PlaybackState.season_number == old_season,
                    PlaybackState.episode_number == old_episode,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    for old in rows:
        target = (
            await session.execute(
                select(PlaybackState).where(
                    PlaybackState.member_id == old.member_id,
                    PlaybackState.media_item_id == new_item,
                    PlaybackState.season_number == new_season,
                    PlaybackState.episode_number == new_episode,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            session.add(
                PlaybackState(
                    member_id=old.member_id,
                    media_item_id=new_item,
                    season_number=new_season,
                    episode_number=new_episode,
                    position_ms=old.position_ms,
                    audio_track=old.audio_track,
                    subtitle_track=old.subtitle_track,
                    played=old.played,
                    play_count=old.play_count,
                    is_favorite=old.is_favorite,
                    last_played_at=old.last_played_at,
                )
            )
            continue
        old_wins = (old.last_played_at or datetime.min) > (target.last_played_at or datetime.min)
        if old_wins:
            target.position_ms = old.position_ms
            target.audio_track = old.audio_track or target.audio_track
            target.subtitle_track = old.subtitle_track or target.subtitle_track
            target.last_played_at = old.last_played_at
        target.played = target.played or old.played
        target.is_favorite = target.is_favorite or old.is_favorite
        target.play_count = max(target.play_count, old.play_count)
        target.updated_at = utcnow()
        session.add(target)
    return len(rows)


async def reanchor_rows(
    session: AsyncSession,
    rows: Iterable[LibraryFile],
    new_item_id: int,
    *,
    unit_of,
    identity_source: str | None,
    resolved_version: int | None,
) -> set[int]:
    """把一组文件行改挂到 ``new_item_id``，观看状态随迁；返回被换下的旧条目 id。

    ``unit_of(row) -> (season, episode)`` 给出每行在新条目下的季集号（电影/
    单本 (0,0)；剧集按解析结果）。调用方负责 commit，并在 commit 之后对
    返回的旧条目 id 调 ``cleanup_orphan_items``。
    """
    old_ids: set[int] = set()
    now = utcnow()
    for row in rows:
        season, episode = unit_of(row)
        if row.media_item_id is not None and row.media_item_id != new_item_id:
            old_ids.add(row.media_item_id)
            await migrate_watch_state(
                session,
                (row.media_item_id, row.season_number, row.episode_number),
                (new_item_id, season, episode),
            )
        row.media_item_id = new_item_id
        row.season_number = season
        row.episode_number = episode
        row.unidentified_reason = None
        row.unidentified_code = None
        row.unidentified_candidates = None
        row.identity_source = identity_source
        row.resolved_version = resolved_version
        row.review_suggestion = None
        row.updated_at = now
        session.add(row)
    return old_ids
