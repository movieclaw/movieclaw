"""网页播放器的观看状态上报（docs/design/web-player.md §6.5「续播点来自 playback_state」）。

与 Jellyfin 的 ``/Sessions/Playing`` 系列**同语义、同落库路径**（都走
``movieclaw_playback.state`` 与同一套阈值三分支），差别只在身份来源：这里的
观看者来自 Web 登录会话，Jellyfin 那边来自设备凭据。两条入口共用领域层，
是为了让「在浏览器里看了一半、换 Infuse 接着看」这件事天然成立。

Webhook 事件也一并发出（started / stopped / completed / progress），否则
配了推送的用户会发现「用 App 看有通知、用网页看没有」——这种不一致比没有
通知更难排查。
"""

from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.webhook import emit_events
from movieclaw_db.models import PlaybackState
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import ClientInfo, build_playback_event
from movieclaw_playback.state import Unit

#: 网页播放器在 webhook 里的客户端标识。与 Jellyfin 客户端上报的 name 同一位置，
#: 下游据此区分「这次播放来自哪儿」。
WEB_CLIENT = ClientInfo(name="MovieClaw Web")

#: ``playback.progress`` 事件的节流：每单元最多 30 秒一条。与 Jellyfin 侧同值——
#: 播放器每 10 秒一次心跳，不节流会把 webhook 刷屏。
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


async def record_start(
    session: AsyncSession,
    unit: Unit,
    *,
    member_id: int,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> PlaybackState:
    """开始播放：play_count +1、刷新最近播放时间，并发 ``playback.started``。

    计数点放在开始而不是结束——关页面不会发任何信号，放结束会漏计（这也是
    Jellyfin 的取舍，见 movieclaw_playback.progress 模块文档）。
    """
    row = await playback_state.record_playback_start(session, unit, member_id=member_id)
    playback_state.apply_track_selection(
        row, audio_track=audio_track, subtitle_track=subtitle_track
    )
    await session.commit()
    event = await build_playback_event(
        session, "playback.started", unit, row, client=WEB_CLIENT
    )
    emit_events([event] if event is not None else [])
    return row


async def record_progress(
    session: AsyncSession,
    unit: Unit,
    *,
    member_id: int,
    position_ms: int | None,
    stopped: bool,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> PlaybackState:
    """进度上报（心跳与停止同入口，按阈值三分支落库）。

    ``position_ms=None`` 表示客户端没报位置（视同播到结尾，标已看），与报 0
    （拖回开头）语义不同——这条区分由 ``resolve_progress`` 承担，本层原样透传。
    """
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
    await session.commit()

    # 事件语义与 Jellyfin 侧逐条对齐：停止发 stopped；本次刚翻转为已看追加
    # completed（心跳也可能触发翻转——播过 90% 直接关页面就是这条路径）；
    # 普通心跳按单元节流发 progress。
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
            session, name, unit, row, duration_ms=runtime_ms, client=WEB_CLIENT
        )
        if event is not None:
            events.append(event)
    emit_events(events)
    return row
