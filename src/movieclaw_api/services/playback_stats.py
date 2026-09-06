"""播放日志的读侧：活动页「播放记录」与「观看统计」（docs/design/activity.md）。

数据全部来自 ``playback_log``（写侧在 services/playback/watch.py）。两个口径：

- 播放记录：每场一行，按开始时间倒序；片名与海报走活动页同一套可见范围
  折叠（范围外只报个数）；
- 观看统计：一段时间内的播放次数、观看时长、看完次数、活跃成员，以及按成员 /
  按客户端 / 按天 / 按作品的分解。聚合在 Python 里做——家庭服务器几十天的日志
  也就几千行，比跨方言的 SQL 分组省心，且按天分组要用浏览器时区。

没收到停止的行（播放器异常退出）按「最后一次心跳超过会话保鲜期」视为已结束，
结束时间取最后一次心跳；仍在保鲜期内的视为进行中。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.playback import (
    MediaActivityTarget,
    PlaybackHistoryView,
    PlaybackLogEntryView,
    PlaybackStatsClientRow,
    PlaybackStatsDayRow,
    PlaybackStatsMemberRow,
    PlaybackStatsTitleRow,
    PlaybackWatchStatsView,
)
from movieclaw_api.services.playback_activity import (
    VisibilityScope,
    _load_unit_contexts,
    _member_names,
    _target,
    libraries_by_item,
)
from movieclaw_api.services.playback_recent import _progress_percent
from movieclaw_db.models import PlaybackLog
from movieclaw_db.models.base import utcnow
from movieclaw_media.models import MediaKind
from movieclaw_playback import activity
from movieclaw_playback.state import Unit

#: 顶部作品榜的长度
_TOP_TITLES = 10


def effective_end(row: PlaybackLog, now: datetime) -> datetime | None:
    """一行的结束时间：收到停止用停止时间；没收到但心跳已过保鲜期，用最后心跳；
    仍在保鲜期内视为进行中（None）。"""
    if row.ended_at is not None:
        return row.ended_at
    if (now - row.last_seen_at).total_seconds() > activity.SESSION_TTL_SECONDS:
        return row.last_seen_at
    return None


def _fallback_target(row: PlaybackLog) -> MediaActivityTarget:
    """条目已删：用日志里的片名快照，没有海报与落点。"""
    try:
        kind = MediaKind(row.kind)
    except ValueError:
        kind = MediaKind.VIDEO
    return MediaActivityTarget(
        media_item_id=row.media_item_id,
        library_id=None,
        browsable=False,
        kind=kind,
        title=row.title,
        year=None,
        poster_url=None,
        season_number=row.season_number,
        episode_number=row.episode_number,
        episode_title=None,
    )


async def _targets_for(
    session: AsyncSession,
    rows: list[PlaybackLog],
    *,
    browsable_library_ids: set[int] | None,
    fold_hidden: bool,
) -> tuple[dict[Unit, MediaActivityTarget | None], dict[Unit, int | None]]:
    """一批日志行的媒体目标与片长；范围内折叠的单元目标映射为 None。"""
    units = {(r.media_item_id, r.season_number, r.episode_number) for r in rows}
    contexts = await _load_unit_contexts(session, units, set())
    scope = VisibilityScope(
        await libraries_by_item(session, {u[0] for u in units}),
        browsable_library_ids,
        fold_hidden=fold_hidden,
    )
    by_row = {(r.media_item_id, r.season_number, r.episode_number): r for r in rows}
    targets: dict[Unit, MediaActivityTarget | None] = {}
    durations: dict[Unit, int | None] = {}
    for unit in units:
        ctx = contexts.get(unit)
        durations[unit] = ctx.duration_ms if ctx else None
        if ctx is None:
            targets[unit] = _fallback_target(by_row[unit])
            continue
        placement = scope.place(unit[0], ctx.file.library_id if ctx.file else None)
        if placement.hidden:
            targets[unit] = None
            continue
        targets[unit] = _target(
            unit, ctx, library_id=placement.library_id, browsable=placement.browsable
        )
    return targets, durations


async def playback_history(
    session: AsyncSession,
    *,
    limit: int,
    before: int | None = None,
    days: int | None,
    member_id: int | None,
    browsable_library_ids: set[int] | None,
    fold_hidden: bool,
) -> PlaybackHistoryView:
    """最近的播放记录（每场一行），按开始时间倒序、游标翻页。

    ``before`` 是上一页最后一行的 id：下一页取 (started_at, id) 严格小于它的行。
    用游标而不是 offset，是因为记录会一直往前追加——滚动续载期间新开的一场
    会把 offset 整体后推，同一行就会在两页里各出现一次。多取一行判断
    ``has_more``；折叠掉的行照常推进游标，换口径重拉即可。
    """
    statement = select(PlaybackLog).order_by(PlaybackLog.started_at.desc(), PlaybackLog.id.desc())  # type: ignore[union-attr]
    if days is not None:
        statement = statement.where(PlaybackLog.started_at >= utcnow() - timedelta(days=days))
    if member_id is not None:
        statement = statement.where(PlaybackLog.member_id == member_id)
    if before is not None:
        anchor = await session.get(PlaybackLog, before)
        if anchor is not None:
            statement = statement.where(
                or_(
                    PlaybackLog.started_at < anchor.started_at,
                    and_(
                        PlaybackLog.started_at == anchor.started_at,
                        PlaybackLog.id < anchor.id,  # type: ignore[operator]
                    ),
                )
            )
    rows = list((await session.execute(statement.limit(limit + 1))).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].id if has_more and rows else None
    names = await _member_names(session, {r.member_id for r in rows})
    targets, durations = await _targets_for(
        session, rows, browsable_library_ids=browsable_library_ids, fold_hidden=fold_hidden
    )
    now = utcnow()
    entries: list[PlaybackLogEntryView] = []
    hidden = 0
    for row in rows:
        unit = (row.media_item_id, row.season_number, row.episode_number)
        target = targets[unit]
        if target is None:
            hidden += 1
            continue
        duration_ms = durations[unit]
        entries.append(
            PlaybackLogEntryView(
                id=row.id or 0,
                member_name=names[row.member_id],
                media=target,
                client=row.client,
                device_name=row.device_name,
                started_at=row.started_at,
                ended_at=effective_end(row, now),
                watched_ms=row.watched_ms,
                start_position_ms=row.start_position_ms,
                end_position_ms=row.end_position_ms,
                duration_ms=duration_ms,
                progress_percent=_progress_percent(row.end_position_ms, duration_ms),
                completed=row.completed,
            )
        )
    return PlaybackHistoryView(
        entries=entries, hidden_count=hidden, has_more=has_more, next_cursor=next_cursor
    )


async def playback_stats(
    session: AsyncSession,
    *,
    days: int,
    tz_offset_minutes: int,
    browsable_library_ids: set[int] | None,
    fold_hidden: bool,
) -> PlaybackWatchStatsView:
    """一段时间内的观看统计。``tz_offset_minutes`` 是浏览器时区相对 UTC 的
    分钟数（东八区 = 480），按天分组用它，否则晚上的观看会被算到第二天。"""
    now = utcnow()
    since = now - timedelta(days=days)
    rows = list(
        (
            await session.execute(select(PlaybackLog).where(PlaybackLog.started_at >= since))
        ).scalars()
    )
    names = await _member_names(session, {r.member_id for r in rows})
    targets, _ = await _targets_for(
        session, rows, browsable_library_ids=browsable_library_ids, fold_hidden=fold_hidden
    )

    total_watched = 0
    completed = 0
    by_member: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])  # plays, watched, completed
    by_client: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_title: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    title_unit: dict[int, Unit] = {}
    offset = timedelta(minutes=tz_offset_minutes)
    for row in rows:
        total_watched += row.watched_ms
        completed += int(row.completed)
        member = by_member[row.member_id]
        member[0] += 1
        member[1] += row.watched_ms
        member[2] += int(row.completed)
        client = by_client[row.client or "未知客户端"]
        client[0] += 1
        client[1] += row.watched_ms
        day = by_day[(row.started_at + offset).strftime("%Y-%m-%d")]
        day[0] += 1
        day[1] += row.watched_ms
        title = by_title[row.media_item_id]
        title[0] += 1
        title[1] += row.watched_ms
        # 作品榜按条目聚合，剧集取该条目最近一场的那一集当展示锚
        title_unit.setdefault(
            row.media_item_id, (row.media_item_id, row.season_number, row.episode_number)
        )

    # 按天补齐没有播放的日子，前端画柱子不必再自己填空
    days_out: list[PlaybackStatsDayRow] = []
    first_day = (since + offset).date()
    last_day = (now + offset).date()
    cursor = first_day
    while cursor <= last_day:
        key = cursor.strftime("%Y-%m-%d")
        plays, watched = by_day.get(key, [0, 0])
        days_out.append(PlaybackStatsDayRow(date=key, plays=plays, watched_ms=watched))
        cursor += timedelta(days=1)

    top_titles: list[PlaybackStatsTitleRow] = []
    hidden_titles = 0
    for item_id, (plays, watched) in sorted(
        by_title.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True
    ):
        target = targets.get(title_unit[item_id])
        if target is None:
            hidden_titles += 1
            continue
        if len(top_titles) < _TOP_TITLES:
            top_titles.append(PlaybackStatsTitleRow(media=target, plays=plays, watched_ms=watched))

    return PlaybackWatchStatsView(
        days=days,
        plays=len(rows),
        watched_ms=total_watched,
        completed=completed,
        active_members=len(by_member),
        by_member=sorted(
            (
                PlaybackStatsMemberRow(
                    member_id=member_id,
                    member_name=names[member_id],
                    plays=plays,
                    watched_ms=watched,
                    completed=done,
                )
                for member_id, (plays, watched, done) in by_member.items()
            ),
            key=lambda r: (r.watched_ms, r.plays),
            reverse=True,
        ),
        by_client=sorted(
            (
                PlaybackStatsClientRow(client=client, plays=plays, watched_ms=watched)
                for client, (plays, watched) in by_client.items()
            ),
            key=lambda r: (r.watched_ms, r.plays),
            reverse=True,
        ),
        by_day=days_out,
        top_titles=top_titles,
        hidden_title_count=hidden_titles,
    )
