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
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.playback import (
    MediaActivityTarget,
    PlaybackHistoryView,
    PlaybackLogEntryView,
    PlaybackStatsClientRow,
    PlaybackStatsDayRow,
    PlaybackStatsMemberRow,
    PlaybackStatsTierRow,
    PlaybackStatsTitleRow,
    PlaybackStatsTotals,
    PlaybackWatchStatsView,
)
from movieclaw_api.services.playback_activity import (
    VisibilityScope,
    _load_unit_contexts,
    _member_names,
    _target,
    libraries_by_item,
)
from movieclaw_api.services.playback_up_next import _progress_percent
from movieclaw_db.models import PlaybackLog, PlaybackMetric
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


#: 网页播放的档位名（movieclaw_playback.decide.PlaybackTier 的展示口径）
_TIER_LABELS = {0: "直连", 1: "重封装", 2: "音频转码", 3: "硬件转码", 4: "软件转码"}


def _day_series(
    rows: list[PlaybackLog], *, since: datetime, until: datetime, offset: timedelta
) -> list[PlaybackStatsDayRow]:
    """按浏览器本地日期分桶，补齐没有播放的日子；行数 = 周期天数 + 1。"""
    buckets: dict[str, dict] = defaultdict(
        lambda: {"plays": 0, "watched": 0, "done": 0, "members": set()}
    )
    for row in rows:
        day = buckets[(row.started_at + offset).strftime("%Y-%m-%d")]
        day["plays"] += 1
        day["watched"] += row.watched_ms
        day["done"] += int(row.completed)
        day["members"].add(row.member_id)
    out: list[PlaybackStatsDayRow] = []
    cursor = (since + offset).date()
    last = (until + offset).date()
    while cursor <= last:
        key = cursor.strftime("%Y-%m-%d")
        day = buckets.get(key)
        out.append(
            PlaybackStatsDayRow(
                date=key,
                plays=day["plays"] if day else 0,
                watched_ms=day["watched"] if day else 0,
                completed=day["done"] if day else 0,
                members=len(day["members"]) if day else 0,
            )
        )
        cursor += timedelta(days=1)
    return out


@dataclass
class _TitleAgg:
    """一部作品在一个周期内的聚合：场次、时长、看过的人。"""

    unit: Unit
    plays: int = 0
    watched_ms: int = 0
    members: set[int] = field(default_factory=set)

    def row(self, target: MediaActivityTarget) -> PlaybackStatsTitleRow:
        return PlaybackStatsTitleRow(
            media=target, plays=self.plays, watched_ms=self.watched_ms, members=len(self.members)
        )


def _aggregate_titles(rows: list[PlaybackLog]) -> dict[int, _TitleAgg]:
    """按条目聚合；剧集取该条目最近一场的那一集当展示锚（rows 按时间无序，取先遇到的）。"""
    titles: dict[int, _TitleAgg] = {}
    for row in rows:
        agg = titles.get(row.media_item_id)
        if agg is None:
            agg = titles[row.media_item_id] = _TitleAgg(
                unit=(row.media_item_id, row.season_number, row.episode_number)
            )
        agg.plays += 1
        agg.watched_ms += row.watched_ms
        agg.members.add(row.member_id)
    return titles


#: 最受欢迎榜的长度（领奖台：金银铜）
_FAVORITES = 3


def _favorites(
    titles: dict[int, _TitleAgg], targets: dict[Unit, MediaActivityTarget]
) -> list[PlaybackStatsTitleRow]:
    """最受欢迎前三：看过的成员最多，并列按时长、再按场次。

    与作品榜「看得最多」（按时长）是两个问题：一个人刷完一整季会稳居时长榜首，
    但三个成员各看一遍的电影才是「家里谁都在看的」。家庭服务器成员就三五个，
    并列很常见，第二排序键不能省。范围外的作品跳过，由后面可见的顶上。
    """
    rows: list[PlaybackStatsTitleRow] = []
    for agg in sorted(
        titles.values(), key=lambda t: (len(t.members), t.watched_ms, t.plays), reverse=True
    ):
        target = targets.get(agg.unit)
        if target is not None:
            rows.append(agg.row(target))
            if len(rows) == _FAVORITES:
                break
    return rows


def _totals(rows: list[PlaybackLog]) -> PlaybackStatsTotals:
    return PlaybackStatsTotals(
        plays=len(rows),
        watched_ms=sum(r.watched_ms for r in rows),
        completed=sum(int(r.completed) for r in rows),
        active_members=len({r.member_id for r in rows}),
    )


async def playback_stats(
    session: AsyncSession,
    *,
    days: int,
    tz_offset_minutes: int,
    member_id: int | None = None,
    browsable_library_ids: set[int] | None,
    fold_hidden: bool,
) -> PlaybackWatchStatsView:
    """一段时间内的观看统计，当前周期与上一周期成对。

    ``tz_offset_minutes`` 是浏览器时区相对 UTC 的分钟数（东八区 = 480），按天与
    按小时分桶都用它，否则晚上的观看会被算到第二天。一次把两个周期的日志取回来，
    在 Python 里切分聚合——家庭服务器几十天的日志也就几千行。
    """
    now = utcnow()
    since = now - timedelta(days=days)
    previous_since = since - timedelta(days=days)
    offset = timedelta(minutes=tz_offset_minutes)

    statement = select(PlaybackLog).where(PlaybackLog.started_at >= previous_since)
    if member_id is not None:
        statement = statement.where(PlaybackLog.member_id == member_id)
    all_rows = list((await session.execute(statement)).scalars())
    rows = [r for r in all_rows if r.started_at >= since]
    previous_rows = [r for r in all_rows if r.started_at < since]

    names = await _member_names(session, {r.member_id for r in rows})
    targets, _ = await _targets_for(
        session, rows, browsable_library_ids=browsable_library_ids, fold_hidden=fold_hidden
    )

    by_member: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])  # plays, watched, completed
    by_client: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_hour = [[0] * 24 for _ in range(7)]
    for row in rows:
        member = by_member[row.member_id]
        member[0] += 1
        member[1] += row.watched_ms
        member[2] += int(row.completed)
        client = by_client[row.client or "未知客户端"]
        client[0] += 1
        client[1] += row.watched_ms
        # 时段热力图按开始时刻分桶：一场归到它开始的那个小时，够回答「什么时候有人在看」
        local = row.started_at + offset
        by_hour[local.weekday()][local.hour] += row.watched_ms

    titles = _aggregate_titles(rows)
    top_titles: list[PlaybackStatsTitleRow] = []
    hidden_titles = 0
    for agg in sorted(titles.values(), key=lambda t: (t.watched_ms, t.plays), reverse=True):
        target = targets.get(agg.unit)
        if target is None:
            hidden_titles += 1
            continue
        if len(top_titles) < _TOP_TITLES:
            top_titles.append(agg.row(target))

    # 上一周期的最受欢迎只用来对照（「蝉联」还是「上期是谁」），同样按可见范围折叠
    previous_targets, _ = await _targets_for(
        session, previous_rows, browsable_library_ids=browsable_library_ids, fold_hidden=fold_hidden
    )

    # 网页播放的档位分解来自播放质量指标（一次播放一行）；Jellyfin 客户端恒为直连
    metric_statement = select(PlaybackMetric.tier, func.count()).where(
        PlaybackMetric.created_at >= since
    )
    if member_id is not None:
        metric_statement = metric_statement.where(PlaybackMetric.member_id == member_id)
    tier_counts = dict(
        (await session.execute(metric_statement.group_by(PlaybackMetric.tier))).all()
    )
    by_tier = [
        PlaybackStatsTierRow(tier=tier, label=label, plays=int(tier_counts.get(tier, 0)))
        for tier, label in _TIER_LABELS.items()
        if tier_counts.get(tier)
    ]

    return PlaybackWatchStatsView(
        days=days,
        current=_totals(rows),
        previous=_totals(previous_rows),
        previous_available=len(previous_rows) > 0,
        by_day=_day_series(rows, since=since, until=now, offset=offset),
        previous_by_day=_day_series(
            previous_rows, since=previous_since, until=since, offset=offset
        ),
        by_hour=by_hour,
        by_member=sorted(
            (
                PlaybackStatsMemberRow(
                    member_id=mid,
                    member_name=names[mid],
                    plays=plays,
                    watched_ms=watched,
                    completed=done,
                )
                for mid, (plays, watched, done) in by_member.items()
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
        by_tier=by_tier,
        top_titles=top_titles,
        hidden_title_count=hidden_titles,
        favorites=_favorites(titles, targets),
        previous_favorites=_favorites(_aggregate_titles(previous_rows), previous_targets),
    )
