"""观看状态存取——playback_state 表的领域服务。

键是 (member_id, media_item_id, season, episode)：``member_id`` 是观看者
（0=超管哨兵，见 PlaybackState 模型注释），每人各看各的进度与收藏。
所有入口都要求调用方显式传 ``member_id``——协议层（Web 播放器 / Jellyfin）
从各自的会话/设备凭据解析身份后传入，本层不做身份判定。
所有写入走 upsert：状态行按需创建，缺行即"从未播过"。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from movieclaw_db.models import (
    LibraryFile,
    MediaEpisode,
    MediaMetadata,
    PlaybackState,
)
from movieclaw_db.models.base import utcnow
from movieclaw_playback.progress import resolve_mark_played, resolve_progress

Unit = tuple[int, int, int]  # (media_item_id, season, episode)


async def unit_runtime_ms(session: AsyncSession, unit: Unit) -> int | None:
    """一个播放单元的片长（毫秒），按可信度降序回退：在位文件实测时长 >
    分集刮削时长 > 条目刮削时长；都没有返回 None。

    **必须服务端算，不能听客户端报**——它是 ``resolve_progress`` 的分母，
    直接决定「看到哪算已看」。网页播放器与 Jellyfin 客户端共用同一个来源，
    同一部片才不会在两个入口给出不同的已看结论。
    """
    item_id, season, episode = unit
    files = (
        await session.execute(
            select(LibraryFile).where(
                LibraryFile.media_item_id == item_id,
                LibraryFile.season_number == season,
                LibraryFile.episode_number == episode,
                LibraryFile.in_place(),
            )
        )
    ).scalars()
    for file in files:
        if file.duration_seconds:
            return file.duration_seconds * 1000
    ep = (
        await session.execute(
            select(MediaEpisode).where(
                MediaEpisode.media_item_id == item_id,
                MediaEpisode.season_number == season,
                MediaEpisode.episode_number == episode,
            )
        )
    ).scalar_one_or_none()
    if ep and ep.runtime_minutes:
        return ep.runtime_minutes * 60_000
    meta = (
        await session.execute(
            select(MediaMetadata).where(MediaMetadata.media_item_id == item_id)
        )
    ).scalar_one_or_none()
    if meta and meta.runtime_minutes:
        return meta.runtime_minutes * 60_000
    return None


async def get_states(
    session: AsyncSession, media_item_ids: Iterable[int], *, member_id: int
) -> dict[Unit, PlaybackState]:
    """批量取一组条目**该观看者**的全部状态行，按 (item, season, episode) 索引。"""
    ids = list(set(media_item_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(PlaybackState).where(
                PlaybackState.media_item_id.in_(ids),
                PlaybackState.member_id == member_id,
            )
        )
    ).scalars()
    return {(r.media_item_id, r.season_number, r.episode_number): r for r in rows}


async def _get_or_create(
    session: AsyncSession, unit: Unit, *, member_id: int
) -> PlaybackState:
    row = (
        await session.execute(
            select(PlaybackState).where(
                PlaybackState.member_id == member_id,
                PlaybackState.media_item_id == unit[0],
                PlaybackState.season_number == unit[1],
                PlaybackState.episode_number == unit[2],
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlaybackState(
            member_id=member_id,
            media_item_id=unit[0],
            season_number=unit[1],
            episode_number=unit[2],
        )
        session.add(row)
    return row


async def record_playback_start(
    session: AsyncSession, unit: Unit, *, member_id: int
) -> PlaybackState:
    """开始播放：play_count +1、刷新最近播放时间（scrobble 语义的计数点）。"""
    row = await _get_or_create(session, unit, member_id=member_id)
    row.play_count += 1
    row.last_played_at = utcnow()
    row.updated_at = utcnow()
    return row


async def record_playback_progress(
    session: AsyncSession,
    unit: Unit,
    *,
    member_id: int,
    position_ms: int | None,
    runtime_ms: int | None,
) -> tuple[PlaybackState, bool]:
    """进度上报（Progress 与 Stopped 同入口）：按阈值三分支落库。

    第二个返回值 ``newly_played`` = played 是否在本次从 False 翻转为 True，
    供协议层判定是否发出 ``playback.completed`` webhook 事件。
    """
    row = await _get_or_create(session, unit, member_id=member_id)
    was_played = row.played
    outcome = resolve_progress(position_ms, runtime_ms, currently_played=row.played)
    row.position_ms = outcome.position_ms
    row.played = outcome.played
    row.last_played_at = utcnow()
    row.updated_at = utcnow()
    return row, (outcome.played and not was_played)


async def mark_played(
    session: AsyncSession,
    units: list[Unit],
    *,
    member_id: int,
    date_played: datetime | None = None,
) -> PlaybackState | None:
    """标记已看（可级联多单元，如整剧/整季）。返回第一个单元的状态行。"""
    first: PlaybackState | None = None
    for unit in units:
        row = await _get_or_create(session, unit, member_id=member_id)
        row.played = True
        row.position_ms = 0
        row.play_count, row.last_played_at = resolve_mark_played(
            play_count=row.play_count,
            date_played=date_played,
            last_played_at=row.last_played_at,
        )
        row.updated_at = utcnow()
        first = first or row
    return first


async def mark_unplayed(
    session: AsyncSession, units: list[Unit], *, member_id: int
) -> PlaybackState | None:
    """取消已看：全部清零（对齐 BaseItem.ResetPlayedState，不是减一）。"""
    first: PlaybackState | None = None
    for unit in units:
        row = await _get_or_create(session, unit, member_id=member_id)
        row.played = False
        row.position_ms = 0
        row.play_count = 0
        row.last_played_at = None
        row.updated_at = utcnow()
        first = first or row
    return first


async def set_favorite(
    session: AsyncSession, unit: Unit, *, member_id: int, favorite: bool
) -> PlaybackState:
    row = await _get_or_create(session, unit, member_id=member_id)
    row.is_favorite = favorite
    row.updated_at = utcnow()
    return row


def apply_track_selection(
    row: PlaybackState,
    *,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> None:
    """在已取得的状态行上记忆轨选择（docs/design/jellyfin-subtitle.md §3.3）。

    与 record_playback_start/progress 同一会话内使用（它们已经
    get-or-create 了该单元的行，这里绝不能再建第二行——会撞唯一键）。
    参数值是中性轨引用（movieclaw_playback.subtitles 的
    embedded:<k> / external:<文件名> / 字幕特有 "off"）。None = 本次上报
    没带该轨，**保持原值不动**——播放器的心跳可能只报进度不报轨。
    """
    changed = False
    if audio_track is not None and row.audio_track != audio_track:
        row.audio_track = audio_track
        changed = True
    if subtitle_track is not None and row.subtitle_track != subtitle_track:
        row.subtitle_track = subtitle_track
        changed = True
    if changed:
        row.updated_at = utcnow()


async def get_remembered_tracks(
    session: AsyncSession, unit: Unit, *, member_id: int
) -> tuple[str | None, str | None]:
    """读取记忆的 (音轨, 字幕轨) 中性引用；无记录返回 (None, None)。"""
    row = (
        await session.execute(
            select(PlaybackState).where(
                PlaybackState.member_id == member_id,
                PlaybackState.media_item_id == unit[0],
                PlaybackState.season_number == unit[1],
                PlaybackState.episode_number == unit[2],
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None, None
    return row.audio_track, row.subtitle_track
