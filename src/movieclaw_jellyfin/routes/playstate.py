"""播放进度回报与已看/收藏标记（设计文档 §7）。

落库语义（v1.1 按源码修正版）：
- play_count+1 与 last_played_at 在 /Sessions/Playing（开始）时更新；
- Progress 与 Stopped 跑同一套阈值三分支（movieclaw_playback.progress）；
- Failed=true 的 Stopped 完全跳过落库；
- UserPlayedItems：datePlayed 才 +1，否则 max(count,1)；DELETE 全清零；
- 作用于 Series/Season GUID 时级联全部有文件的子单元。

本路由只做协议翻译——解 GUID、换算轨序号、读 PositionTicks——落库、webhook
与活动页实时会话统一交给 ``movieclaw_api.services.playback``：播放上报
（Playing / Progress / Stopped 及 legacy PlayingItems）走 ``watch``，已看/收藏
标记走 ``marks``，与网页端（``/api/v1/playback/marks``）同一份逻辑，两边写的
是同一张 ``playback_state`` 表，「在 Infuse 里点心、网页上立刻看到」天然成立。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from movieclaw_api.services.playback import marks as playback_marks
from movieclaw_api.services.playback import watch as playback_watch
from movieclaw_db.engine import get_database
from movieclaw_jellyfin.catalog import (
    TICKS_PER_MS,
    _folder_user_data,
    _leaf_user_data,
    audio_track_for_index,
    is_leaf_kind,
    load_bundles,
    subtitle_track_for_index,
)
from movieclaw_jellyfin.errors import bad_request_text, not_found
from movieclaw_jellyfin.ids import EntityKind, EntityRef, decode_guid
from movieclaw_jellyfin.security import RequestIdentity, require_device
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import ClientInfo
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


def _leaf_unit(ref: EntityRef) -> playback_state.Unit:
    if ref.kind == EntityKind.EPISODE:
        return (ref.entity_id, ref.season, ref.episode)
    return (ref.entity_id, 0, 0)


def _mark_target(ref: EntityRef) -> playback_marks.MarkTarget:
    """结构化 GUID → 协议无关的标记目标（Series/Movie → 整条目，Season →
    整季，Episode → 单集）；级联与哨兵落点由标记服务统一解析。"""
    if ref.kind == EntityKind.EPISODE:
        return playback_marks.MarkTarget(ref.entity_id, ref.season, ref.episode)
    if ref.kind == EntityKind.SEASON:
        return playback_marks.MarkTarget(ref.entity_id, ref.season)
    return playback_marks.MarkTarget(ref.entity_id)


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
    identity: RequestIdentity,
    *,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> None:
    """开始播放：实时会话、落库与 ``playback.started`` 事件统一交给 watch。"""
    async with get_database().session() as session:
        await playback_watch.record_start(
            session,
            _leaf_unit(ref),
            member_id=identity.device.member_id,
            client=_client_info(identity),
            audio_track=audio_track,
            subtitle_track=subtitle_track,
        )


async def _record_progress(
    ref: EntityRef,
    identity: RequestIdentity,
    position_ms: int | None,
    *,
    stopped: bool = False,
    paused: bool | None = None,
    audio_track: str | None = None,
    subtitle_track: str | None = None,
) -> None:
    """进度 / 停止：实时会话、落库与 stopped / completed / progress 事件统一交给 watch。"""
    async with get_database().session() as session:
        await playback_watch.record_progress(
            session,
            _leaf_unit(ref),
            member_id=identity.device.member_id,
            client=_client_info(identity),
            position_ms=position_ms,
            stopped=stopped,
            paused=paused,
            audio_track=audio_track,
            subtitle_track=subtitle_track,
        )


@router.post("/Sessions/Playing", status_code=204)
async def playing_start(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        audio_track, subtitle_track = await _tracks_from_body(
            ref, body, identity.device.member_id
        )
        await _record_start(
            ref, identity, audio_track=audio_track, subtitle_track=subtitle_track
        )
    return Response(status_code=204)


@router.post("/Sessions/Playing/Progress", status_code=204)
async def playing_progress(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        position = _position_ms(body)
        paused = _paused_flag(body)
        if position is None:
            # 不带位置的心跳（如暂停事件）不落库，但实时会话要刷新暂停态与
            # 保鲜时钟；报 0 = 拖回开头，要落库
            playback_watch.report_heartbeat(
                _leaf_unit(ref),
                member_id=identity.device.member_id,
                client=_client_info(identity),
                position_ms=None,
                paused=paused,
            )
        else:
            audio_track, subtitle_track = await _tracks_from_body(
                ref, body, identity.device.member_id
            )
            await _record_progress(
                ref,
                identity,
                position,
                paused=paused,
                audio_track=audio_track,
                subtitle_track=subtitle_track,
            )
    return Response(status_code=204)


@router.post("/Sessions/Playing/Stopped", status_code=204)
async def playing_stopped(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    failed = body.get("failed")
    if failed is True or (isinstance(failed, str) and failed.lower() == "true"):
        # 播放失败的上报不落库（SessionManager.cs:1164-1167）；字符串 "true"
        # 一并接住——真 Jellyfin 对它 400，我们静默落库会把失败记成正常观看。
        # 但取流与实时会话仍要收口：VidHub 的 Stopped 不代表它已关闭此前的
        # Range 请求，不停就会继续为已退出的播放器预读
        playback_watch.end_session(identity.device.device_id)
        return Response(status_code=204)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is None:
        playback_watch.end_session(identity.device.device_id)
        return Response(status_code=204)
    audio_track, subtitle_track = await _tracks_from_body(
        ref, body, identity.device.member_id
    )
    await _record_progress(
        ref,
        identity,
        _position_ms(body),
        stopped=True,
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
        await _record_start(ref, identity)
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
        if position is None:
            playback_watch.report_heartbeat(
                _leaf_unit(ref),
                member_id=identity.device.member_id,
                client=_client_info(identity),
                position_ms=None,
                paused=None,
            )
        else:
            await _record_progress(ref, identity, position)
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
    if ref is None:
        playback_watch.end_session(identity.device.device_id)
        return Response(status_code=204)
    await _record_progress(
        ref,
        identity,
        _position_ms({}, request.query_params.get("positionTicks")),
        stopped=True,
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
    if is_leaf_kind(bundle.item.kind):
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
    date_played = _parse_date_played(request.query_params.get("datePlayed"))
    async with get_database().session() as session:
        hit = await playback_marks.set_played(
            session,
            _mark_target(ref),
            member_id=identity.device.member_id,
            client=_client_info(identity),
            played=True,
            date_played=date_played,
        )
    if not hit:
        raise not_found()
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
    async with get_database().session() as session:
        hit = await playback_marks.set_played(
            session,
            _mark_target(ref),
            member_id=identity.device.member_id,
            client=_client_info(identity),
            played=False,
        )
    if not hit:
        raise not_found()
    return await _user_data_response(ref, item_id, member_id=identity.device.member_id)


async def _set_favorite(
    ref: EntityRef, *, member_id: int, favorite: bool, client: ClientInfo
) -> None:
    """收藏：落库、commit、``item.(un)favorited`` 事件全部由标记服务完成。"""
    async with get_database().session() as session:
        await playback_marks.set_favorite(
            session, _mark_target(ref), member_id=member_id, client=client, favorite=favorite
        )


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
