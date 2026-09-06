"""播放上报的**唯一**落点：观看状态 + webhook 事件 + 活动页实时注册表。

网页播放器（``/playback/progress``）与 Jellyfin 协议（``/Sessions/Playing*``）
两条入口都汇到这里，差别只在身份来源：这里的观看者来自 Web 登录会话，
Jellyfin 那边来自设备凭据；两边各自把协议形态翻译成 ``ClientInfo`` 与播放
单元后，其余三件事全部由本模块一次做完：

1. ``playback_state`` 落库（续播点、已看、播放次数、轨记忆），走
   ``movieclaw_playback.state`` 的同一套阈值三分支——「在浏览器里看了一半、
   换 Infuse 接着看」因此天然成立；
2. webhook 事件（started / stopped / completed / progress），否则配了推送的
   用户会发现「用 App 看有通知、用网页看没有」；
3. ``movieclaw_playback.activity`` 的实时注册表——活动页「正在播放」的数据源。
   曾经只有 Jellyfin 路由接了它，网页端播放在活动页上完全不可见（2026-09
   的用户反馈），收口到这里之后两条入口不可能再分叉。

实时注册表以 ``client.device_id`` 为锚。Jellyfin 设备天然带 DeviceId；网页端
由前端生成一个稳定的浏览器标识随上报带来（``web_client_info``），语义与
Jellyfin 客户端的 DeviceId 对齐。
"""

from __future__ import annotations

import re
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.webhook import emit_events
from movieclaw_db.models import MediaItem, PlaybackLog, PlaybackState
from movieclaw_db.models.base import utcnow
from movieclaw_playback import activity
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import ClientInfo, build_playback_event
from movieclaw_playback.state import Unit
from movieclaw_playback.streaming import stop_device_streams

#: 网页播放器在 webhook 与活动页里的客户端名。与 Jellyfin 客户端上报的 name
#: 同一位置，下游据此区分「这次播放来自哪儿」。
WEB_CLIENT_NAME = "MovieClaw Web"

#: 网页端设备标识的命名空间前缀：与 Jellyfin 设备 id 同在一张注册表里，
#: 加前缀避免两类标识意外撞车。
_WEB_DEVICE_PREFIX = "web-"

#: ``playback.progress`` 事件的节流：每单元最多 30 秒一条。播放器每 10 秒一次
#: 心跳，不节流会把 webhook 刷屏。键是 (item, season, episode)，量级 = 在播单元数。
_PROGRESS_EMIT_INTERVAL = 30.0
_progress_last_emit: dict[Unit, float] = {}


def _progress_throttled(unit: Unit) -> bool:
    """True = 本次进度不发事件；未被节流时顺带记下本次时间。"""
    now = time.monotonic()
    last = _progress_last_emit.get(unit)
    if last is not None and now - last < _PROGRESS_EMIT_INTERVAL:
        return True
    _progress_last_emit[unit] = now
    return False


# ---------------------------------------------------------------------------
# 网页端的客户端身份
# ---------------------------------------------------------------------------

_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)
_DEVICE_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def describe_user_agent(user_agent: str | None) -> str:
    """User-Agent → 给人看的设备名（「Safari · iPhone」）。

    只认最常见的几个浏览器与平台，认不出就退回「浏览器」。这是展示字段，
    不参与任何判定，不值得引一个 UA 解析库。
    """
    ua = user_agent or ""
    browser = next((name for needle, name in _BROWSERS if needle in ua), "浏览器")
    platform = next((name for needle, name in _PLATFORMS if needle in ua), "")
    return f"{browser} · {platform}" if platform else browser


def web_device_id(raw: str | None, *, member_id: int) -> str:
    """网页端设备标识：``web-<成员 id>-<浏览器 id>``；没带浏览器 id 就按成员兜底。

    浏览器 id 是客户端自报的，命名空间里带上成员 id，成员就不可能用别人的
    浏览器 id 顶掉别人在活动页上的会话卡片。兜底键让老版本前端（或 sendBeacon
    丢字段）的播放仍出现在活动页——代价是同一成员的两台浏览器会合并成一个
    会话，好过完全不可见。
    """
    cleaned = _DEVICE_ID_SAFE.sub("", raw or "")[:64]
    return f"{_WEB_DEVICE_PREFIX}{member_id}-{cleaned or 'browser'}"


def web_client_info(*, device_id: str, user_agent: str | None) -> ClientInfo:
    """网页播放器的客户端信息；``device_id`` 须已经过 :func:`web_device_id`。"""
    return ClientInfo(
        name=WEB_CLIENT_NAME,
        device_name=describe_user_agent(user_agent),
        device_id=device_id,
    )


# ---------------------------------------------------------------------------
# 播放日志（playback_log）：每场播放一行
# ---------------------------------------------------------------------------

#: 单次进度增量的上限：超过它视为 seek 跳过的区间，不计入观看时长。Jellyfin
#: 客户端心跳最疏也在 30 秒级，网页端 10 秒，留足余量。
_WATCH_DELTA_CAP_MS = 120_000

#: 同一设备同一单元的重复「开始」（seek、暂停后恢复、换源重协商都会再发
#: Playing）在这个窗口内视为同一场，不另开一行；与实时注册表的保鲜期同值。
_LOG_REUSE_SECONDS = activity.SESSION_TTL_SECONDS


async def _open_log(
    session: AsyncSession, unit: Unit, *, member_id: int, device_id: str
) -> PlaybackLog | None:
    """该设备在该单元上尚未收口的最近一行。"""
    return (
        await session.execute(
            select(PlaybackLog)
            .where(
                PlaybackLog.member_id == member_id,
                PlaybackLog.device_id == device_id,
                PlaybackLog.media_item_id == unit[0],
                PlaybackLog.season_number == unit[1],
                PlaybackLog.episode_number == unit[2],
                PlaybackLog.ended_at.is_(None),  # type: ignore[union-attr]
            )
            .order_by(PlaybackLog.id.desc())  # type: ignore[union-attr]
            .limit(1)
        )
    ).scalar_one_or_none()


async def _close_stale_logs(session: AsyncSession, *, member_id: int, device_id: str, now) -> None:
    """同一设备换片时把此前没收到停止的行收口：结束时间取最后一次心跳。"""
    rows = (
        await session.execute(
            select(PlaybackLog).where(
                PlaybackLog.member_id == member_id,
                PlaybackLog.device_id == device_id,
                PlaybackLog.ended_at.is_(None),  # type: ignore[union-attr]
            )
        )
    ).scalars()
    for row in rows:
        row.ended_at = row.last_seen_at
        row.updated_at = now


async def _start_log(
    session: AsyncSession,
    unit: Unit,
    *,
    member_id: int,
    client: ClientInfo,
    position_ms: int,
    now,
) -> PlaybackLog:
    """开一行新日志；片名与形态快照进来，条目删了统计仍成立。"""
    item = await session.get(MediaItem, unit[0])
    row = PlaybackLog(
        member_id=member_id,
        media_item_id=unit[0],
        kind=item.kind if item else "movie",
        title=item.title if item else "",
        season_number=unit[1],
        episode_number=unit[2],
        device_id=client.device_id,
        client=client.name,
        device_name=client.device_name,
        started_at=now,
        last_seen_at=now,
        start_position_ms=position_ms,
        end_position_ms=position_ms,
    )
    session.add(row)
    return row


async def _log_start(
    session: AsyncSession, unit: Unit, *, member_id: int, client: ClientInfo, position_ms: int
) -> None:
    now = utcnow()
    current = await _open_log(session, unit, member_id=member_id, device_id=client.device_id)
    if current is not None and (now - current.last_seen_at).total_seconds() < _LOG_REUSE_SECONDS:
        # 同一场里的重复开始：只续保鲜，不另开一行
        current.last_seen_at = now
        current.updated_at = now
        return
    await _close_stale_logs(session, member_id=member_id, device_id=client.device_id, now=now)
    await _start_log(
        session, unit, member_id=member_id, client=client, position_ms=position_ms, now=now
    )


async def _log_progress(
    session: AsyncSession,
    unit: Unit,
    *,
    member_id: int,
    client: ClientInfo,
    position_ms: int | None,
    stopped: bool,
    completed: bool,
) -> None:
    now = utcnow()
    row = await _open_log(session, unit, member_id=member_id, device_id=client.device_id)
    if row is None:
        # 丢了开始包（Jellyfin 只发 Progress、或服务刚重启）：就地开行
        row = await _start_log(
            session,
            unit,
            member_id=member_id,
            client=client,
            position_ms=position_ms or 0,
            now=now,
        )
    if position_ms is not None:
        delta = position_ms - row.end_position_ms
        if 0 < delta <= _WATCH_DELTA_CAP_MS:
            row.watched_ms += delta
        row.end_position_ms = position_ms
    row.last_seen_at = now
    row.updated_at = now
    row.completed = row.completed or completed
    if stopped:
        row.ended_at = now


# ---------------------------------------------------------------------------
# 上报入口（两条协议共用）
# ---------------------------------------------------------------------------


def end_session(device_id: str) -> None:
    """停止播放：先停掉该设备仍在读盘的取流，再结束实时会话。

    停止上报不代表播放器已经关闭此前发出的 Range 连接（VidHub 尤其），不先
    取消会让机械盘继续为已退出的播放器预读；播放失败的停止同样要走这里，
    否则活动页会按保鲜期继续显示一台已不存在的设备在播放。
    """
    stop_device_streams(device_id)
    activity.report_stop(device_id)


def report_heartbeat(
    unit: Unit,
    *,
    member_id: int,
    client: ClientInfo,
    position_ms: int | None,
    paused: bool | None,
) -> None:
    """只刷新实时会话、不落库的心跳（暂停事件等不带位置的上报）。

    ``position_ms`` / ``paused`` 为 None 表示本次没带该字段，实时会话保持原值。
    """
    activity.report_progress(
        client.device_id,
        member_id=member_id,
        client=client,
        unit=unit,
        position_ms=position_ms,
        paused=paused,
    )


async def record_start(
    session: AsyncSession,
    unit: Unit,
    *,
    member_id: int,
    client: ClientInfo,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> PlaybackState:
    """开始播放：建实时会话、play_count +1、刷新最近播放时间，并发 ``playback.started``。

    计数点放在开始而不是结束——关页面不会发任何信号，放结束会漏计（这也是
    Jellyfin 的取舍，见 movieclaw_playback.progress 模块文档）。
    """
    activity.report_start(client.device_id, member_id=member_id, client=client, unit=unit)
    row = await playback_state.record_playback_start(session, unit, member_id=member_id)
    playback_state.apply_track_selection(
        row, audio_track=audio_track, subtitle_track=subtitle_track
    )
    # 起点记续播位置：看完的从头播（position 已被清零），没看完的接着播
    await _log_start(session, unit, member_id=member_id, client=client, position_ms=row.position_ms)
    await session.commit()
    event = await build_playback_event(session, "playback.started", unit, row, client=client)
    emit_events([event] if event is not None else [])
    return row


async def record_progress(
    session: AsyncSession,
    unit: Unit,
    *,
    member_id: int,
    client: ClientInfo,
    position_ms: int | None,
    stopped: bool,
    paused: bool | None = None,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> PlaybackState:
    """进度上报（心跳与停止同入口，按阈值三分支落库）。

    ``position_ms=None`` 表示客户端没报位置（视同播到结尾，标已看），与报 0
    （拖回开头）语义不同——这条区分由 ``resolve_progress`` 承担，本层原样透传。
    Jellyfin 侧不带位置的心跳不该走到这里，用 :func:`report_heartbeat`。
    """
    if stopped:
        end_session(client.device_id)
    else:
        activity.report_progress(
            client.device_id,
            member_id=member_id,
            client=client,
            unit=unit,
            position_ms=position_ms,
            paused=paused,
        )
    runtime_ms = await playback_state.unit_runtime_ms(session, unit)
    row, newly_played = await playback_state.record_playback_progress(
        session,
        unit,
        member_id=member_id,
        position_ms=position_ms,
        runtime_ms=runtime_ms,
    )
    playback_state.apply_track_selection(
        row, audio_track=audio_track, subtitle_track=subtitle_track
    )
    await _log_progress(
        session,
        unit,
        member_id=member_id,
        client=client,
        position_ms=position_ms,
        stopped=stopped,
        completed=newly_played,
    )
    await session.commit()

    # 停止发 stopped；本次刚翻转为已看追加 completed（心跳也可能触发翻转——
    # 播过 90% 直接关页面就是这条路径）；普通心跳按单元节流发 progress。
    emit_progress = not stopped and not newly_played and not _progress_throttled(unit)
    events = []
    for name, hit in (
        ("playback.stopped", stopped),
        ("playback.completed", newly_played),
        ("playback.progress", emit_progress),
    ):
        if not hit:
            continue
        event = await build_playback_event(
            session, name, unit, row, duration_ms=runtime_ms, client=client
        )
        if event is not None:
            events.append(event)
    emit_events(events)
    return row
