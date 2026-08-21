"""活动页「观看」视角的聚合查询（docs/design/activity.md）。

三段数据、三个事实源，本服务只做只读投影，不新增任何状态表：

- 正在播放 / 正在下载：``movieclaw_playback.activity`` 的进程内实时快照
  （播放器上报 + 取流字节计量），重启即清空；
- 设备清单：``jellyfin_device`` 凭据表，叠加"当前是否有活跃会话"的在线标记；
- 最近观看：``playback_state`` 领域表的全成员视角（管理员运维口径，
  与首页按成员隔离的 /playback/recent 不同）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.playback import (
    ActiveFileDownloadView,
    ActivePlaybackSessionView,
    MediaActivityTarget,
    MediaActivityView,
    MediaRecentPlayView,
    PlaybackDeviceView,
    PlaybackFileSpec,
)
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.media_scrape import asset_version

# 与首页"最近观看"共用同一套时长回退与进度换算口径，避免两处各算各的
from movieclaw_api.services.playback_recent import _progress_percent, _runtime_ms
from movieclaw_db.models import (
    JellyfinDevice,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    Member,
    PlaybackState,
)
from movieclaw_media.models import MediaKind
from movieclaw_playback import activity
from movieclaw_playback.state import Unit
from movieclaw_playback.streaming import is_strm, stop_device_streams

logger = logging.getLogger("movieclaw_api.playback_activity")


@dataclass
class _UnitContext:
    """一个播放单元在装配视图时需要的全部媒体上下文。"""

    item: MediaItem
    poster_url: str | None
    episode_title: str | None
    duration_ms: int | None
    file: LibraryFile | None


def _poster_url(item: MediaItem, poster_file: str | None, image_base: str) -> str | None:
    if poster_file:
        return f"/images/assets/{poster_file}?v={asset_version(poster_file)}"
    if item.poster_path:
        return f"{image_base}/w500{item.poster_path}"
    return None


def _target(unit: Unit, ctx: _UnitContext) -> MediaActivityTarget:
    return MediaActivityTarget(
        media_item_id=unit[0],
        library_id=ctx.file.library_id if ctx.file else None,
        kind=MediaKind(ctx.item.kind),
        title=ctx.item.title,
        year=ctx.item.year,
        poster_url=ctx.poster_url,
        season_number=unit[1],
        episode_number=unit[2],
        episode_title=ctx.episode_title if ctx.item.kind == "tv" else None,
    )


async def _load_unit_contexts(
    session: AsyncSession, units: set[Unit], preferred_file_ids: set[int]
) -> dict[Unit, _UnitContext]:
    """批量装配一组播放单元的媒体上下文；条目已删的单元缺席。

    ``preferred_file_ids`` 是活跃字节流正在读的台账行——多版本条目优先展示
    真正被播放的那个文件的规格，而不是猜第一个。
    """
    item_ids = {u[0] for u in units}
    if not item_ids:
        return {}
    items = {
        i.id: i
        for i in (
            await session.execute(select(MediaItem).where(MediaItem.id.in_(item_ids)))
        ).scalars()
    }
    metadata_rows = {
        m.media_item_id: m
        for m in (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id.in_(item_ids))
            )
        ).scalars()
    }
    episode_rows = {
        (e.media_item_id, e.season_number, e.episode_number): e
        for e in (
            await session.execute(
                select(MediaEpisode).where(MediaEpisode.media_item_id.in_(item_ids))
            )
        ).scalars()
    }
    file_rows: dict[Unit, list[LibraryFile]] = {}
    for f in (
        await session.execute(
            select(LibraryFile).where(
                LibraryFile.media_item_id.in_(item_ids), LibraryFile.in_place()
            )
        )
    ).scalars():
        file_rows.setdefault(
            (f.media_item_id, f.season_number, f.episode_number), []
        ).append(f)

    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    contexts: dict[Unit, _UnitContext] = {}
    for unit in units:
        item = items.get(unit[0])
        if item is None:
            continue
        meta = metadata_rows.get(unit[0])
        episode = episode_rows.get(unit)
        files = file_rows.get(unit, [])
        chosen = next((f for f in files if f.id in preferred_file_ids), None)
        if chosen is None and files:
            chosen = files[0]
        duration_ms = _runtime_ms(
            max((f.duration_seconds or 0) for f in files) if files else None,
            episode.runtime_minutes if episode and item.kind == "tv" else None,
            meta.runtime_minutes if meta else None,
        )
        contexts[unit] = _UnitContext(
            item=item,
            poster_url=_poster_url(item, meta.poster_file if meta else None, image_base),
            episode_title=episode.name if episode else None,
            duration_ms=duration_ms,
            file=chosen,
        )
    return contexts


async def _member_names(session: AsyncSession, member_ids: set[int]) -> dict[int, str]:
    """成员 ID → 展示名；0 = 超管（哨兵），已删除成员给可读兜底。"""
    names: dict[int, str] = {}
    if 0 in member_ids:
        account = await auth_service.get_admin_account()
        names[0] = account.username
    real_ids = {i for i in member_ids if i != 0}
    if real_ids:
        for member in (
            await session.execute(select(Member).where(Member.id.in_(real_ids)))
        ).scalars():
            names[member.id] = member.username
    for member_id in member_ids:
        names.setdefault(member_id, f"成员 #{member_id}")
    return names


def _file_spec(f: LibraryFile | None) -> PlaybackFileSpec | None:
    if f is None:
        return None
    return PlaybackFileSpec(
        resolution=f.resolution,
        video_codec=f.video_codec,
        hdr=f.hdr,
        container=f.container,
        bit_rate=f.bit_rate,
        size_bytes=f.size_bytes or None,
    )


async def _recent_plays(
    session: AsyncSession, names_needed: set[int], *, limit: int
) -> list[tuple]:
    """全成员最近观看的原始行：每个 (成员, 作品) 只保留最后活动的那一集。"""
    ranked_states = (
        select(
            PlaybackState.id.label("state_id"),
            func.row_number()
            .over(
                partition_by=(PlaybackState.member_id, PlaybackState.media_item_id),
                order_by=(PlaybackState.last_played_at.desc(), PlaybackState.id.desc()),
            )
            .label("rank"),
        )
        .where(
            PlaybackState.last_played_at.is_not(None),  # type: ignore[union-attr]
            PlaybackState.season_number >= 0,
            PlaybackState.episode_number >= 0,
        )
        .subquery()
    )
    statement = (
        select(
            PlaybackState,
            MediaItem,
            MediaMetadata.poster_file,
            MediaMetadata.runtime_minutes,
            MediaEpisode.name,
            MediaEpisode.runtime_minutes,
            func.max(LibraryFile.duration_seconds).label("file_duration_seconds"),
            # 详情页落点：跨库时取 id 最小的库，保证链接确定且可达
            func.min(LibraryFile.library_id).label("library_id"),
        )
        .join(ranked_states, ranked_states.c.state_id == PlaybackState.id)
        .join(MediaItem, MediaItem.id == PlaybackState.media_item_id)
        .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)
        .outerjoin(
            MediaEpisode,
            and_(
                MediaEpisode.media_item_id == PlaybackState.media_item_id,
                MediaEpisode.season_number == PlaybackState.season_number,
                MediaEpisode.episode_number == PlaybackState.episode_number,
            ),
        )
        .outerjoin(
            LibraryFile,
            and_(
                LibraryFile.media_item_id == PlaybackState.media_item_id,
                LibraryFile.season_number == PlaybackState.season_number,
                LibraryFile.episode_number == PlaybackState.episode_number,
                LibraryFile.in_place(),
            ),
        )
        .where(ranked_states.c.rank == 1)
        .group_by(PlaybackState.id, MediaItem.id, MediaMetadata.id, MediaEpisode.id)
        .order_by(PlaybackState.last_played_at.desc(), PlaybackState.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    for state, *_ in rows:
        names_needed.add(state.member_id)
    return rows


async def media_activity_overview(
    session: AsyncSession, *, recent_limit: int = 30
) -> MediaActivityView:
    """装配活动页「观看」视角的完整快照。"""
    play_sessions, meters = activity.snapshot()
    play_meters = [m for m in meters if m.kind == activity.STREAM_KIND_PLAY]
    download_meters = [m for m in meters if m.kind == activity.STREAM_KIND_DOWNLOAD]

    units = {s.unit for s in play_sessions} | {m.unit for m in download_meters}
    preferred_file_ids = {m.file_id for m in meters if m.file_id is not None}
    contexts = await _load_unit_contexts(session, units, preferred_file_ids)

    names_needed = {s.member_id for s in play_sessions}
    names_needed.update(m.member_id for m in download_meters)

    device_rows = list(
        (
            await session.execute(
                select(JellyfinDevice).order_by(JellyfinDevice.last_seen_at.desc())
            )
        ).scalars()
    )
    names_needed.update(d.member_id for d in device_rows)

    recent_rows = await _recent_plays(session, names_needed, limit=recent_limit)
    names = await _member_names(session, names_needed)

    session_views: list[ActivePlaybackSessionView] = []
    for play in sorted(play_sessions, key=lambda s: s.started_at, reverse=True):
        ctx = contexts.get(play.unit)
        if ctx is None:  # 条目在播放中被删除的边角：快照里直接略过
            continue
        device_meters = [
            m for m in play_meters if m.device_id == play.device_id and m.unit == play.unit
        ]
        # 播放方式认台账里的文件形态，不靠"是否已经看到字节"倒推——上报先于
        # 取流到达是常态，用字节推断会把刚开播的本地文件误标成网盘直链。
        # 没有在位台账行（条目刚被删/改）才退回字节证据。
        if ctx.file is not None:
            streaming = not is_strm(ctx.file.file_path)
        else:
            streaming = play.local_streamed or bool(device_meters)
        session_views.append(
            ActivePlaybackSessionView(
                device_id=play.device_id,
                member_name=names[play.member_id],
                client=play.client.name,
                device_name=play.client.device_name,
                client_version=play.client.version,
                media=_target(play.unit, ctx),
                position_ms=play.position_ms,
                duration_ms=ctx.duration_ms,
                progress_percent=_progress_percent(
                    play.position_ms or 0, ctx.duration_ms
                ),
                paused=play.paused,
                play_method="local" if streaming else "remote",
                rate_bytes_per_second=(
                    sum(m.rate_bytes_per_second() for m in device_meters)
                    if device_meters
                    else None
                ),
                # 已传输 = 本会话内已结束连接的累计 + 当前在服务连接的实时增量。
                # 播放器每次 seek / 续拉缓冲都换一条 Range 连接，只看在服务的
                # 连接会让读数反复归零，和观看进度对不上。全程无字节（网盘直链）
                # 才是 None——展示层据此隐藏该项，而不是显示一个 0。
                bytes_sent=(
                    play.bytes_transferred
                    + sum(m.bytes_sent for m in device_meters)
                    or None
                ),
                connections=len(device_meters),
                file=_file_spec(ctx.file),
                started_at=play.started_at,
                last_report_at=play.last_report_at,
            )
        )

    # 同一设备下载同一文件会并发/续传出多条 Range 连接，聚合为一条展示
    download_groups: dict[tuple[str, int | str], list] = {}
    for meter in download_meters:
        key = (meter.device_id, meter.file_id if meter.file_id is not None else meter.file_name)
        download_groups.setdefault(key, []).append(meter)
    download_views: list[ActiveFileDownloadView] = []
    for group in sorted(
        download_groups.values(), key=lambda g: min(m.started_at for m in g), reverse=True
    ):
        first = group[0]
        ctx = contexts.get(first.unit)
        # 下载进度取"推进得最远的那条连接"：顺序下载（播放器离线缓存的常态，
        # 含断点续传）下这就是真实位置。并发分段下载会偏乐观，但那类客户端
        # 不是本接口的服务对象，不为它把读数复杂化。
        position = max(m.position_bytes for m in group)
        size = first.size_bytes
        download_views.append(
            ActiveFileDownloadView(
                device_id=first.device_id,
                member_name=names[first.member_id],
                client=first.client.name,
                device_name=first.client.device_name,
                media=_target(first.unit, ctx) if ctx else None,
                file_name=first.file_name,
                size_bytes=size,
                bytes_sent=sum(m.bytes_sent for m in group),
                rate_bytes_per_second=sum(m.rate_bytes_per_second() for m in group),
                connections=len(group),
                position_bytes=position,
                progress_percent=(
                    max(1, min(100, round(position * 100 / size))) if size > 0 else None
                ),
                started_at=min(m.started_at for m in group),
            )
        )

    active_device_ids = {s.device_id for s in play_sessions} | {
        m.device_id for m in meters
    }
    device_views = [
        PlaybackDeviceView(
            device_id=d.device_id,
            device_name=d.device_name,
            client=d.client,
            client_version=d.version,
            member_name=names[d.member_id],
            last_seen_at=d.last_seen_at,
            online=d.device_id in active_device_ids,
        )
        for d in device_rows
    ]

    recent_views: list[MediaRecentPlayView] = []
    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    for (
        state,
        item,
        poster_file,
        item_runtime_minutes,
        episode_name,
        episode_runtime_minutes,
        file_duration_seconds,
        library_id,
    ) in recent_rows:
        duration_ms = _runtime_ms(
            file_duration_seconds,
            episode_runtime_minutes if item.kind == "tv" else None,
            item_runtime_minutes,
        )
        recent_views.append(
            MediaRecentPlayView(
                member_name=names[state.member_id],
                media=MediaActivityTarget(
                    media_item_id=item.id,
                    library_id=library_id,
                    kind=MediaKind(item.kind),
                    title=item.title,
                    year=item.year,
                    poster_url=_poster_url(item, poster_file, image_base),
                    season_number=state.season_number,
                    episode_number=state.episode_number,
                    episode_title=episode_name if item.kind == "tv" else None,
                ),
                position_ms=state.position_ms,
                duration_ms=duration_ms,
                progress_percent=_progress_percent(state.position_ms, duration_ms),
                played=state.played,
                play_count=state.play_count,
                last_played_at=state.last_played_at,
            )
        )

    return MediaActivityView(
        sessions=session_views,
        downloads=download_views,
        devices=device_views,
        recent=recent_views,
    )


async def revoke_device(session: AsyncSession, device_id: str) -> str | None:
    """注销一台播放器设备：删凭据行并当场把它踢下线。

    三件事缺一不可，否则"注销"名不副实：
    1. 删 ``jellyfin_device`` 行——AccessToken 是设备级长期凭据，删行即失效，
       该设备下次请求就要重新登录；
    2. 结束实时会话——否则活动页还会按 5 分钟保鲜期继续显示一台已不存在的
       设备在播放；
    3. 停掉仍在读盘的取流——播放器不会因为 token 失效就主动断开已建立的
       Range 连接，不停就会继续为一台已注销的设备转磁盘。

    返回被注销设备的展示名；设备不存在返回 None（调用方给 404）。
    """
    device = (
        await session.execute(
            select(JellyfinDevice).where(JellyfinDevice.device_id == device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        return None
    label = device.device_name or device.client or device.device_id
    await session.delete(device)
    await session.commit()

    activity.report_stop(device_id)
    stopped = stop_device_streams(device_id)
    logger.info(
        "播放器设备「%s」已注销，凭据即刻失效，同时停止了 %d 条仍在读取的流",
        label,
        stopped,
    )
    return label
