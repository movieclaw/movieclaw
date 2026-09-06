"""活动页「观看」视角的聚合查询（docs/design/activity.md）。

正在播放 / 正在下载来自 ``movieclaw_playback.activity`` 的进程内实时快照
（播放器上报 + 取流字节计量），重启即清空；本服务只做只读投影，不新增任何
状态表。``jellyfin_device`` 凭据表只用来判断一条会话能否「注销设备」。

「播放记录」与「观看统计」另有读侧（services/playback_stats.py），复用这里的
单元上下文装配（``_load_unit_contexts``）与可见范围口径（``VisibilityScope``）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.playback import (
    ActiveFileDownloadView,
    ActivePlaybackSessionView,
    MediaActivityTarget,
    MediaActivityView,
    PlaybackFileSpec,
)
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.media_scrape import asset_version
from movieclaw_api.services.playback.session import get_session_manager

# 与首页"最近观看"共用同一套时长回退与进度换算口径，避免两处各算各的
from movieclaw_api.services.playback_recent import _progress_percent, _runtime_ms
from movieclaw_db.models import (
    JellyfinDevice,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    Member,
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


def _target(
    unit: Unit, ctx: _UnitContext, *, library_id: int | None, browsable: bool
) -> MediaActivityTarget:
    return MediaActivityTarget(
        media_item_id=unit[0],
        library_id=library_id,
        browsable=browsable,
        kind=MediaKind(ctx.item.kind),
        title=ctx.item.title,
        year=ctx.item.year,
        poster_url=ctx.poster_url,
        season_number=unit[1],
        episode_number=unit[2],
        episode_title=ctx.episode_title if ctx.item.kind == "tv" else None,
    )


@dataclass(frozen=True)
class _Placement:
    """一条活动记录相对当前超管浏览范围的落点。

    - ``library_id``：详情页落点。条目跨库时优先取正在播放的那个文件所在库，
      否则取范围内 id 最小的库，保证链接确定且可达；
    - ``browsable``：落点库是否在超管的可浏览范围内。范围外的记录在「全部」
      口径下照常出片名，但前端不渲染链接（浏览类接口对范围外超管是 404）；
    - ``hidden``：按「我的浏览范围」口径要折叠成计数、不出片名与海报。
    """

    library_id: int | None
    browsable: bool
    hidden: bool


class VisibilityScope:
    """活动页的可见范围口径（docs/design/activity.md「范围切换」）。

    ``browsable_library_ids`` 是当前超管的可浏览库集合（None = 内部流程不受限）；
    ``fold_hidden`` 为真时范围外记录折叠成计数（默认口径），为假时全量展示、
    只用 ``browsable`` 标志区分链接可达性（「全部」口径）。
    """

    def __init__(
        self,
        libraries_by_item: dict[int, set[int]],
        browsable_library_ids: set[int] | None,
        *,
        fold_hidden: bool,
    ) -> None:
        self._libraries = libraries_by_item
        self._browsable = browsable_library_ids
        self._fold = fold_hidden

    def place(self, item_id: int, preferred: int | None) -> _Placement:
        libraries = self._libraries.get(item_id, set())
        if not libraries:
            # 没有任何台账行的条目不属于任何库（文件已删/只剩记录），不受范围约束
            return _Placement(preferred, True, False)
        if self._browsable is None:
            return _Placement(self._pick(libraries, preferred), True, False)
        allowed = libraries & self._browsable
        if allowed:
            return _Placement(self._pick(allowed, preferred), True, False)
        return _Placement(self._pick(libraries, preferred), False, self._fold)

    @staticmethod
    def _pick(candidates: set[int], preferred: int | None) -> int:
        return preferred if preferred in candidates else min(candidates)


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
    # 分集档案与台账行都按 (条目, 季, 集) 精确命中，而不是按条目整表拉取：
    # 一页 30 条播放记录若落在几部几百集的长剧上，按条目拉会把几千行分集
    # 简介与带音轨/字幕 JSON 的台账行全部水合进 ORM，实测 15 条日志的接口从
    # 10 毫秒涨到 140 毫秒以上，且这段 CPU 工作跑在事件循环上，同进程的其他
    # 请求一起停顿——这正是活动页「最近播放」偶尔打开特别卡的根源。
    # 行值 IN 走 uq_media_episode_unit / ix_library_file_media_unit 两棵索引。
    unit_keys = list(units)
    episode_rows = {
        (e.media_item_id, e.season_number, e.episode_number): e
        for e in (
            await session.execute(
                select(MediaEpisode).where(
                    tuple_(
                        MediaEpisode.media_item_id,
                        MediaEpisode.season_number,
                        MediaEpisode.episode_number,
                    ).in_(unit_keys)
                )
            )
        ).scalars()
    }
    file_rows: dict[Unit, list[LibraryFile]] = {}
    for f in (
        await session.execute(
            select(LibraryFile).where(
                tuple_(
                    LibraryFile.media_item_id,
                    LibraryFile.season_number,
                    LibraryFile.episode_number,
                ).in_(unit_keys),
                LibraryFile.in_place(),
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
    """成员 ID → 展示名；0 = 超管（哨兵），-1 = 分享访客（哨兵），
    已删除成员给可读兜底。"""
    names: dict[int, str] = {}
    if 0 in member_ids:
        account = await auth_service.get_admin_account()
        names[0] = account.username
    if -1 in member_ids:
        # docs/design/media-share.md §4.5：分享出去的影片被不登录的人播放
        names[-1] = "分享访客"
    real_ids = {i for i in member_ids if i > 0}
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


async def media_activity_overview(
    session: AsyncSession,
    *,
    browsable_library_ids: set[int] | None = None,
    fold_hidden: bool = True,
) -> MediaActivityView:
    """装配活动页「观看」视角的实时快照：正在播放与正在下载。

    ``browsable_library_ids``：当前超管的可浏览库集合（None = 内部流程不受限）。
    ``fold_hidden`` 为真是「我的浏览范围」口径：落在范围外的正在播放与正在下载
    统一折叠成计数（``hidden_*_count``），不出片名与海报——活动页是管理视角，
    但「不可见就彻底不可见」对超管自己摘掉的库同样成立。为假是「全部」口径：
    跨成员、跨库全量展示，范围外记录只带 ``browsable=false``，前端据此不渲染
    详情链接（浏览类接口对范围外超管是 404）。

    这个接口被活动页每 8 秒轮询一次，只装配页面真正渲染的两段实时数据；
    「最近观看」与设备清单曾经也在这里算，前端撤掉之后仍每轮白算一遍窗口函数
    与全表设备行，已一并去掉——历史看播放记录（/playback/history）。
    """
    play_sessions, meters = activity.snapshot()
    play_meters = [m for m in meters if m.kind == activity.STREAM_KIND_PLAY]
    download_meters = [m for m in meters if m.kind == activity.STREAM_KIND_DOWNLOAD]

    units = {s.unit for s in play_sessions} | {m.unit for m in download_meters}
    preferred_file_ids = {m.file_id for m in meters if m.file_id is not None}
    contexts = await _load_unit_contexts(session, units, preferred_file_ids)

    names_needed = {s.member_id for s in play_sessions}
    names_needed.update(m.member_id for m in download_meters)

    # 只有持 Jellyfin 设备凭据的会话才能「注销」；网页播放器走登录会话，
    # 没有可撤销的设备凭据，前端据此隐藏菜单
    revocable_device_ids = set(
        (await session.execute(select(JellyfinDevice.device_id))).scalars()
    )

    names = await _member_names(session, names_needed)
    # 条目可能同时存在于多个库：只要有一个库在可浏览范围内就展示，详情落点
    # 取范围内 id 最小的库；一个都不在的按口径折叠或标记为不可浏览
    item_libraries = await libraries_by_item(session, {u[0] for u in units})
    scope = VisibilityScope(item_libraries, browsable_library_ids, fold_hidden=fold_hidden)
    hidden_session_count = 0
    hidden_download_count = 0

    session_views: list[ActivePlaybackSessionView] = []
    for play in sorted(play_sessions, key=lambda s: s.started_at, reverse=True):
        ctx = contexts.get(play.unit)
        if ctx is None:  # 条目在播放中被删除的边角：快照里直接略过
            continue
        placement = scope.place(play.unit[0], ctx.file.library_id if ctx.file else None)
        if placement.hidden:
            hidden_session_count += 1
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
                revocable=play.device_id in revocable_device_ids,
                member_name=names[play.member_id],
                client=play.client.name,
                device_name=play.client.device_name,
                client_version=play.client.version,
                media=_target(
                    play.unit,
                    ctx,
                    library_id=placement.library_id,
                    browsable=placement.browsable,
                ),
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
        placement = (
            scope.place(first.unit[0], ctx.file.library_id if ctx.file else None)
            if ctx
            else None
        )
        if placement is not None and placement.hidden:
            hidden_download_count += 1
            continue
        # 下载进度取"推进得最远的那条连接"：顺序下载（播放器离线缓存的常态，
        # 含断点续传）下这就是真实位置。并发分段下载会偏乐观，但那类客户端
        # 不是本接口的服务对象，不为它把读数复杂化。
        position = max(m.position_bytes for m in group)
        size = first.size_bytes
        download_views.append(
            ActiveFileDownloadView(
                device_id=first.device_id,
                revocable=first.device_id in revocable_device_ids,
                member_name=names[first.member_id],
                client=first.client.name,
                device_name=first.client.device_name,
                media=(
                    _target(
                        first.unit,
                        ctx,
                        library_id=placement.library_id,
                        browsable=placement.browsable,
                    )
                    if ctx and placement
                    else None
                ),
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

    return MediaActivityView(
        sessions=session_views,
        downloads=download_views,
        hidden_session_count=hidden_session_count,
        hidden_download_count=hidden_download_count,
    )


async def libraries_by_item(session: AsyncSession, item_ids: set[int]) -> dict[int, set[int]]:
    """条目 id → 它有在位台账的库 id 集合（一页播放记录涉及的几十个条目一次取回）。"""
    if not item_ids:
        return {}
    rows = await session.execute(
        select(LibraryFile.media_item_id, LibraryFile.library_id)
        .where(
            LibraryFile.media_item_id.in_(item_ids),  # type: ignore[union-attr]
            LibraryFile.library_id.is_not(None),  # type: ignore[union-attr]
        )
        .distinct()
    )
    grouped: dict[int, set[int]] = {}
    for item_id, library_id in rows.all():
        grouped.setdefault(item_id, set()).add(library_id)
    return grouped


def live_session_label(device_id: str) -> str | None:
    """当前正在播放的设备的展示名；不在播放返回 None。"""
    sessions, _ = activity.snapshot()
    for play in sessions:
        if play.device_id == device_id:
            client = play.client
            return client.device_name or client.name or device_id
    return None


async def end_playback(device_id: str) -> int:
    """管理员结束一台设备**本次**播放，不动凭据（与「注销设备」的区别）。

    三件事：实时会话立即消失并进入拒绝窗口（``activity.end_device``，否则
    播放器换条连接就续上了）、停掉仍在读盘的直出取流、停掉这台浏览器的转码
    会话。设备下次亲手点播放即可继续，观看进度照常保存。返回停掉的取流连接数。
    """
    activity.end_device(device_id)
    stopped = stop_device_streams(device_id)
    sessions = await get_session_manager().stop_for_device(device_id)
    logger.info(
        "管理员已结束设备「%s」的播放：停止 %d 条取流、%d 个转码会话",
        device_id,
        stopped,
        sessions,
    )
    return stopped


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
