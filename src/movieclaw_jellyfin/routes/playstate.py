"""播放进度回报与已看/收藏标记（设计文档 §7）。

落库语义（v1.1 按源码修正版）：
- play_count+1 与 last_played_at 在 /Sessions/Playing（开始）时更新；
- Progress 与 Stopped 跑同一套阈值三分支（movieclaw_playback.progress）；
- Failed=true 的 Stopped 完全跳过落库；
- UserPlayedItems：datePlayed 才 +1，否则 max(count,1)；DELETE 全清零；
- 作用于 Series/Season GUID 时级联全部有文件的子单元。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from movieclaw_api.services.webhook import emit_events
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, MediaEpisode, MediaItem
from movieclaw_jellyfin.catalog import (
    TICKS_PER_MS,
    _folder_user_data,
    _leaf_user_data,
    audio_track_for_index,
    load_bundles,
    subtitle_track_for_index,
)
from movieclaw_jellyfin.errors import bad_request_text, not_found
from movieclaw_jellyfin.ids import EntityKind, EntityRef, decode_guid
from movieclaw_jellyfin.security import RequestIdentity, require_device
from movieclaw_playback import activity
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import (
    ClientInfo,
    build_favorite_event,
    build_marked_events,
    build_playback_event,
)
from movieclaw_playback.streaming import stop_device_streams
from movieclaw_playback.subtitles import SUBTITLE_OFF

router = APIRouter(dependencies=[Depends(require_device)])


def _client_info(identity: RequestIdentity) -> ClientInfo:
    """协议身份 → 协议无关的客户端信息（webhook 事件的 client 字段）。"""
    device = identity.device
    return ClientInfo(
        name=device.client or "",
        device_name=device.device_name or "",
        device_id=device.device_id or "",
        version=device.version or "",
    )


async def _read_body(request: Request) -> dict[str, Any]:
    """宽容读取 JSON body：键名小写化、未知字段忽略、坏 JSON 当空。"""
    try:
        body = await request.json()
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    return {str(k).lower(): v for k, v in body.items()}


def _paused_flag(body: dict[str, Any]) -> bool | None:
    """上报里的 IsPaused；None = 本次没带该字段（实时会话保持原值）。"""
    raw = body.get("ispaused")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.lower() == "true"
    return None


def _position_ms(body: dict[str, Any], query_ticks: str | None = None) -> int | None:
    """None = 客户端没报位置（领域层按"播到结尾"处理）；0 = 拖回开头。"""
    raw = body.get("positionticks")
    if raw is None and query_ticks is not None:
        raw = query_ticks
    if raw is None:
        return None
    try:
        ticks = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, ticks // TICKS_PER_MS)


async def _resolve_units(ref: EntityRef) -> list[playback_state.Unit]:
    """GUID → 受影响的 (item, season, episode) 单元列表（文件夹级联）。"""
    async with get_database().session() as session:
        q = select(LibraryFile.season_number, LibraryFile.episode_number).where(
            LibraryFile.media_item_id == ref.entity_id,
            LibraryFile.in_place(),
        )
        rows = list((await session.execute(q)).all())
        if not rows and ref.kind in (EntityKind.ITEM, EntityKind.SEASON):
            # 文件全部丢失的剧：真 Jellyfin 只要条目存在就允许手动标记已看
            # （走元数据级联），不应因文件不在位而 404。退回元数据集清单
            eq = select(
                MediaEpisode.season_number, MediaEpisode.episode_number
            ).where(MediaEpisode.media_item_id == ref.entity_id)
            rows = list((await session.execute(eq)).all())
    units = sorted({(ref.entity_id, s, e) for s, e in rows})
    if ref.kind == EntityKind.EPISODE:
        return [(ref.entity_id, ref.season, ref.episode)]
    if ref.kind == EntityKind.SEASON:
        return [u for u in units if u[1] == ref.season]
    if ref.kind == EntityKind.ITEM:
        # 电影 = (0,0) 单元；剧 = 全部集
        return units or [(ref.entity_id, 0, 0)]
    return []


async def _unit_runtime_ms(unit: playback_state.Unit) -> int | None:
    """片长解析下沉到领域层（``playback_state.unit_runtime_ms``），网页播放器
    共用同一份——它是已看阈值的分母，两个入口算法不一致会让同一部片在
    Jellyfin 客户端和网页端给出不同的「已看」结论。"""
    async with get_database().session() as session:
        return await playback_state.unit_runtime_ms(session, unit)


def _leaf_unit(ref: EntityRef) -> playback_state.Unit:
    if ref.kind == EntityKind.EPISODE:
        return (ref.entity_id, ref.season, ref.episode)
    return (ref.entity_id, 0, 0)


async def _favorite_unit(ref: EntityRef) -> playback_state.Unit:
    """收藏的落点单元：叶子用真实单元；Season/Series 用哨兵（-1）——
    与 catalog._folder_user_data 的读取侧约定一致，绝不污染 S00E00。"""
    if ref.kind == EntityKind.EPISODE:
        return (ref.entity_id, ref.season, ref.episode)
    if ref.kind == EntityKind.SEASON:
        return (ref.entity_id, ref.season, -1)
    async with get_database().session() as session:
        item = await session.get(MediaItem, ref.entity_id)
    if item is not None and item.kind == "tv":
        return (ref.entity_id, -1, -1)
    return (ref.entity_id, 0, 0)


def _decode_item_ref(raw: Any) -> EntityRef | None:
    if not raw:
        return None
    ref = decode_guid(str(raw))
    if ref is None or ref.kind not in (
        EntityKind.ITEM,
        EntityKind.SEASON,
        EntityKind.EPISODE,
    ):
        return None
    return ref


# ---------------------------------------------------------------------------
# Sessions/Playing*
# ---------------------------------------------------------------------------


async def _tracks_from_body(
    ref: EntityRef, body: dict[str, Any], member_id: int
) -> tuple[str | None, str | None]:
    """上报里的轨序号 → 中性轨引用（jellyfin-subtitle.md §4.5）。

    序号是相对某个 MediaSource 的合成编号，换算要落到具体文件行：按
    body 的 mediaSourceId 定位版本，缺省第一个。字幕 -1 → "off"（用户
    明确关闭也要记住）；换算失败（悬空索引/版本不见了）返回 None 丢弃。
    None = 本次没报该轨，领域层保持原值。
    """
    audio_raw = body.get("audiostreamindex")
    subtitle_raw = body.get("subtitlestreamindex")
    if audio_raw is None and subtitle_raw is None:
        return None, None
    if ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        return None, None
    # 复用播放路由的装载点：库可见性同一套约束
    from movieclaw_jellyfin.routes.playback import _files_for_ref, _select_source

    files = await _files_for_ref(ref, member_id)
    raw_ms = body.get("mediasourceid")
    selected = _select_source(files, str(raw_ms) if raw_ms else None, "")
    f = selected[0] if selected else (files[0] if files else None)
    if f is None:
        return None, None

    def _to_int(raw: Any) -> int | None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    audio_track = None
    audio_index = _to_int(audio_raw)
    if audio_index is not None:
        audio_track = audio_track_for_index(f, audio_index)

    subtitle_track = None
    subtitle_index = _to_int(subtitle_raw)
    if subtitle_index is not None:
        subtitle_track = (
            SUBTITLE_OFF if subtitle_index == -1 else subtitle_track_for_index(f, subtitle_index)
        )
    return audio_track, subtitle_track


async def _record_start(
    ref: EntityRef,
    client: ClientInfo,
    *,
    member_id: int,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> None:
    """开始播放落库 + commit 后装配/投递 ``playback.started`` 事件。"""
    unit = _leaf_unit(ref)
    async with get_database().session() as session:
        row = await playback_state.record_playback_start(session, unit, member_id=member_id)
        playback_state.apply_track_selection(
            row, audio_track=audio_track, subtitle_track=subtitle_track
        )
        await session.commit()
        event = await build_playback_event(
            session, "playback.started", unit, row, client=client
        )
    if event is not None:
        emit_events([event])


@router.post("/Sessions/Playing", status_code=204)
async def playing_start(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        member_id = identity.device.member_id
        activity.report_start(
            identity.device.device_id,
            member_id=member_id,
            client=_client_info(identity),
            unit=_leaf_unit(ref),
        )
        audio_track, subtitle_track = await _tracks_from_body(ref, body, member_id)
        await _record_start(
            ref,
            _client_info(identity),
            member_id=member_id,
            audio_track=audio_track,
            subtitle_track=subtitle_track,
        )
    return Response(status_code=204)


#: playback.progress 事件的节流：每单元最多 30 秒一条（避免播放期间刷屏，
#: 设计文档 §1.1）。键是 (item, season, episode)，量级 = 库内在播单元数
_PROGRESS_EMIT_INTERVAL = 30.0
_progress_last_emit: dict[playback_state.Unit, float] = {}


def _progress_throttled(unit: playback_state.Unit) -> bool:
    """True = 本次进度不发事件；未被节流时顺带记下本次时间。"""
    now = time.monotonic()
    last = _progress_last_emit.get(unit)
    if last is not None and now - last < _PROGRESS_EMIT_INTERVAL:
        return True
    _progress_last_emit[unit] = now
    return False


async def _apply_progress(
    ref: EntityRef,
    position_ms: int | None,
    *,
    member_id: int,
    stopped: bool = False,
    client: ClientInfo | None = None,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> None:
    """进度落库；commit 后按语义装配 webhook 事件：
    Stopped 上报发 ``playback.stopped``；played 本次翻转为 True 追加
    ``playback.completed``（Progress 与 Stopped 都可能触发翻转）；
    普通进度上报按单元节流发 ``playback.progress``。"""
    unit = _leaf_unit(ref)
    runtime_ms = await _unit_runtime_ms(unit)
    async with get_database().session() as session:
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
        emit_progress = (
            not stopped and not newly_played and not _progress_throttled(unit)
        )
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


@router.post("/Sessions/Playing/Progress", status_code=204)
async def playing_progress(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        position = _position_ms(body)
        member_id = identity.device.member_id
        # 实时会话不设位置门槛：暂停心跳（不带位置）也要刷新暂停态与保鲜时钟
        activity.report_progress(
            identity.device.device_id,
            member_id=member_id,
            client=_client_info(identity),
            unit=_leaf_unit(ref),
            position_ms=position,
            paused=_paused_flag(body),
        )
        # Progress 不带位置的心跳（如暂停事件）不落库；报 0 = 拖回开头要落
        if position is not None:
            audio_track, subtitle_track = await _tracks_from_body(ref, body, member_id)
            await _apply_progress(
                ref,
                position,
                member_id=member_id,
                client=_client_info(identity),
                audio_track=audio_track,
                subtitle_track=subtitle_track,
            )
    return Response(status_code=204)


@router.post("/Sessions/Playing/Stopped", status_code=204)
async def playing_stopped(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    # VidHub 的 Stopped 不代表它已立即关闭此前发出的 Range 请求。先取消同设备
    # 的活跃流，避免机械盘继续为已退出的播放器预读；失败停止同样必须收口。
    stop_device_streams(identity.device.device_id)
    activity.report_stop(identity.device.device_id)
    failed = body.get("failed")
    if failed is True or (isinstance(failed, str) and failed.lower() == "true"):
        # 播放失败的上报不落库（SessionManager.cs:1164-1167）；字符串 "true"
        # 一并接住——真 Jellyfin 对它 400，我们静默落库会把失败记成正常观看
        return Response(status_code=204)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        member_id = identity.device.member_id
        audio_track, subtitle_track = await _tracks_from_body(ref, body, member_id)
        await _apply_progress(
            ref,
            _position_ms(body),
            member_id=member_id,
            stopped=True,
            client=_client_info(identity),
            audio_track=audio_track,
            subtitle_track=subtitle_track,
        )
    return Response(status_code=204)


@router.post("/Sessions/Playing/Ping", status_code=204)
async def playing_ping(request: Request) -> Response:
    if "playSessionId" not in request.query_params:
        raise bad_request_text()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# legacy /PlayingItems（P2 兜底，参数走 query；停止是 DELETE）
# ---------------------------------------------------------------------------


@router.post("/PlayingItems/{item_id}", status_code=204)
@router.post("/Users/{user_id}/PlayingItems/{item_id}", status_code=204)
async def playing_start_legacy(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    ref = _decode_item_ref(item_id)
    if ref is not None:
        activity.report_start(
            identity.device.device_id,
            member_id=identity.device.member_id,
            client=_client_info(identity),
            unit=_leaf_unit(ref),
        )
        await _record_start(
            ref, _client_info(identity), member_id=identity.device.member_id
        )
    return Response(status_code=204)


@router.post("/PlayingItems/{item_id}/Progress", status_code=204)
@router.post("/Users/{user_id}/PlayingItems/{item_id}/Progress", status_code=204)
async def playing_progress_legacy(
    request: Request,
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    ref = _decode_item_ref(item_id)
    if ref is not None:
        position = _position_ms({}, request.query_params.get("positionTicks"))
        activity.report_progress(
            identity.device.device_id,
            member_id=identity.device.member_id,
            client=_client_info(identity),
            unit=_leaf_unit(ref),
            position_ms=position,
            paused=None,
        )
        if position is not None:
            await _apply_progress(
                ref,
                position,
                member_id=identity.device.member_id,
                client=_client_info(identity),
            )
    return Response(status_code=204)


@router.delete("/PlayingItems/{item_id}", status_code=204)
@router.delete("/Users/{user_id}/PlayingItems/{item_id}", status_code=204)
async def playing_stopped_legacy(
    request: Request,
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    ref = _decode_item_ref(item_id)
    stop_device_streams(identity.device.device_id)
    activity.report_stop(identity.device.device_id)
    if ref is not None:
        await _apply_progress(
            ref,
            _position_ms({}, request.query_params.get("positionTicks")),
            member_id=identity.device.member_id,
            stopped=True,
            client=_client_info(identity),
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 已看 / 收藏（200 + UserItemDataDto）
# ---------------------------------------------------------------------------


async def _user_data_response(
    ref: EntityRef, guid_raw: str, *, member_id: int
) -> JSONResponse:
    """标记类接口的 200 响应体：与浏览接口同一套 UserData 公式，
    文件夹（Series/Season）给聚合形态（含 UnplayedItemCount），客户端
    据此原地刷新角标，不必重拉列表。"""
    guid = guid_raw.lower().replace("-", "")
    async with get_database().session() as session:
        bundles = await load_bundles(
            session, [ref.entity_id], member_id=member_id, include_fileless=True
        )
    bundle = bundles.get(ref.entity_id)
    if bundle is None:
        return JSONResponse(
            {
                "PlaybackPositionTicks": 0,
                "PlayCount": 0,
                "IsFavorite": False,
                "Played": False,
                "Key": guid,
                "ItemId": guid,
            }
        )
    if ref.kind == EntityKind.EPISODE:
        return JSONResponse(_leaf_user_data(bundle, ref.season, ref.episode, guid))
    if ref.kind == EntityKind.SEASON:
        return JSONResponse(_folder_user_data(bundle, guid, season=ref.season))
    if bundle.item.kind == "movie":
        return JSONResponse(_leaf_user_data(bundle, 0, 0, guid))
    return JSONResponse(_folder_user_data(bundle, guid))


def _parse_date_played(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


@router.post("/UserPlayedItems/{item_id}")
@router.post("/Users/{user_id}/PlayedItems/{item_id}")
async def mark_played(
    request: Request,
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    units = await _resolve_units(ref)
    if not units:
        raise not_found()
    date_played = _parse_date_played(request.query_params.get("datePlayed"))
    async with get_database().session() as session:
        await playback_state.mark_played(
            session, units, member_id=identity.device.member_id, date_played=date_played
        )
        await session.commit()
        events = await build_marked_events(
            session,
            "playback.marked_played",
            units,
            member_id=identity.device.member_id,
            client=_client_info(identity),
        )
    emit_events(events)
    return await _user_data_response(ref, item_id, member_id=identity.device.member_id)


@router.delete("/UserPlayedItems/{item_id}")
@router.delete("/Users/{user_id}/PlayedItems/{item_id}")
async def mark_unplayed(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    units = await _resolve_units(ref)
    if not units:
        raise not_found()
    async with get_database().session() as session:
        await playback_state.mark_unplayed(
            session, units, member_id=identity.device.member_id
        )
        await session.commit()
        events = await build_marked_events(
            session,
            "playback.marked_unplayed",
            units,
            member_id=identity.device.member_id,
            client=_client_info(identity),
        )
    emit_events(events)
    return await _user_data_response(ref, item_id, member_id=identity.device.member_id)


async def _set_favorite(
    ref: EntityRef, *, member_id: int, favorite: bool, client: ClientInfo
) -> None:
    """收藏落库 + commit 后装配/投递 ``item.(un)favorited`` 事件。"""
    unit = await _favorite_unit(ref)
    async with get_database().session() as session:
        await playback_state.set_favorite(
            session, unit, member_id=member_id, favorite=favorite
        )
        await session.commit()
        event = await build_favorite_event(
            session, unit, favorite=favorite, client=client
        )
    if event is not None:
        emit_events([event])


@router.post("/UserFavoriteItems/{item_id}")
@router.post("/Users/{user_id}/FavoriteItems/{item_id}")
async def mark_favorite(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    await _set_favorite(
        ref,
        member_id=identity.device.member_id,
        favorite=True,
        client=_client_info(identity),
    )
    return await _user_data_response(ref, item_id, member_id=identity.device.member_id)


@router.delete("/UserFavoriteItems/{item_id}")
@router.delete("/Users/{user_id}/FavoriteItems/{item_id}")
async def unmark_favorite(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    await _set_favorite(
        ref,
        member_id=identity.device.member_id,
        favorite=False,
        client=_client_info(identity),
    )
    return await _user_data_response(ref, item_id, member_id=identity.device.member_id)
