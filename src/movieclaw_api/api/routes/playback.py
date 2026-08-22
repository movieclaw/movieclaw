"""播放记录与网页播放器的 Web 业务接口。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path as PathLib
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_admin, require_login
from movieclaw_api.exceptions import (
    BadRequestException,
    NotFoundException,
    ServiceUnavailableException,
)
from movieclaw_api.schemas.playback import (
    MediaActivityView,
    PlaybackDecideRequest,
    PlaybackDecisionView,
    PlaybackPolicyPayload,
    PlaybackPolicyView,
    PlaybackProgressRequest,
    PlaybackSessionRequest,
    PlaybackSessionView,
    PlaybackStateView,
    RecentWatchView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.services.playback import plan as playback_plan
from movieclaw_api.services.playback import watch as playback_watch
from movieclaw_api.services.playback.embedded_subs import extract_embedded_subtitle
from movieclaw_api.services.playback.hwprobe import available_backends
from movieclaw_api.services.playback.session import (
    DiskQuotaError,
    SessionLimitError,
    SessionStartError,
    get_session_manager,
)
from movieclaw_api.services.playback.signing import issue_stream_token, verify_stream_token
from movieclaw_api.services.playback_activity import media_activity_overview, revoke_device
from movieclaw_api.services.playback_recent import recent_watch_items
from movieclaw_api.settings import PlaybackPolicySetting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_db.engine import get_session
from movieclaw_db.models import LibraryFile
from movieclaw_playback import state as playback_state
from movieclaw_playback.decide import PlaybackTier as Tier
from movieclaw_playback.streaming import (
    DisconnectAwareFileResponse,
    container_mime_type,
    is_strm,
    resolve_strm_url,
)
from movieclaw_playback.subtitles import (
    SubtitleServeError,
    parse_embedded_track,
    resolve_external_subtitle,
    serve_subtitle,
)

router = APIRouter(prefix="/playback", tags=["playback"])


@router.get(
    "/recent",
    response_model=ApiResponse[RecentWatchView],
    summary="最近观看",
    operation_id="playback.recent",
    openapi_extra={"x-cli-hidden": True},
)
async def list_recent_watch(
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RecentWatchView]:
    """列出当前账号在可见媒体库中的最近观看作品。"""
    visible_ids = await visible_library_ids(session, principal)
    member_id = principal.member_id if principal.member_id is not None else 0
    items = await recent_watch_items(
        session,
        member_id=member_id,
        visible_library_ids=visible_ids,
        limit=limit,
    )
    return ok(RecentWatchView(items=items))


@router.get(
    "/activity",
    response_model=ApiResponse[MediaActivityView],
    summary="媒体库活动快照",
    operation_id="playback.activity",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-hidden": True},
)
async def get_media_activity(
    recent_limit: Annotated[int, Query(ge=1, le=100)] = 30,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[MediaActivityView]:
    """活动页「观看」视角：正在播放/下载、设备清单与全成员最近观看。

    管理员运维视角（跨成员可见），与首页按成员隔离的最近观看接口分离。
    """
    return ok(await media_activity_overview(session, recent_limit=recent_limit))


@router.delete(
    "/devices/{device_id}",
    response_model=ApiResponse[None],
    summary="注销播放器设备",
    operation_id="playback.device.revoke",
    dependencies=[Depends(require_admin)],
    # confirm 而非 destructive：注销会中断该设备正在进行的播放/下载并要求重新
    # 登录，但不销毁任何数据——观看进度、收藏按成员保存，与设备无关。
    openapi_extra={"x-cli-hidden": True, "x-cli-dangerous": "confirm"},
)
async def revoke_playback_device(
    device_id: Annotated[str, Path(min_length=1, max_length=256)],
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    """注销一台播放器设备：凭据即刻失效，正在进行的播放与取流一并停止。

    该设备下次访问需重新登录；已看进度、收藏等观看状态按成员保存，不受影响。
    """
    label = await revoke_device(session, device_id)
    if label is None:
        raise NotFoundException("设备不存在或已注销")
    return ok(None, message=f"已注销「{label}」，该设备需重新登录")


@router.post(
    "/decide",
    response_model=ApiResponse[PlaybackDecisionView],
    summary="播放决策",
    operation_id="playback.decide",
    openapi_extra={"x-cli-hidden": True},
)
async def decide_playback_route(
    payload: PlaybackDecideRequest,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackDecisionView]:
    """算出「这部片在你的浏览器上该怎么放」（docs/design/web-player.md §3）。

    请求带上客户端的解码能力快照，服务端结合入库时 ffprobe 落下的规格真值，
    在五档降级阶梯里取能成立的最小档——尽可能不转码。

    返回三态：``plan`` 可以播；``consent`` 需要用户同意开启软件转码；
    ``rejected`` 放不了，附中文原因与下一步建议。
    """
    visible = await visible_library_ids(session, principal)
    capability = playback_plan.capability_from_request(payload.capability)
    failed = frozenset(Tier(t) for t in payload.failed_tiers if t in Tier._value2member_map_)

    if payload.file_id is not None:
        decision = await playback_plan.decide_for_file(
            session,
            payload.file_id,
            capability,
            can_self_enable=principal.is_admin,
            failed_tiers=failed,
            visible_library_ids=visible,
        )
    elif payload.media_item_id is not None:
        files = await playback_plan.library_files_for_unit(
            session,
            payload.media_item_id,
            payload.season_number,
            payload.episode_number,
            visible_library_ids=visible,
        )
        decision = await playback_plan.decide_for_files(
            files,
            capability,
            can_self_enable=principal.is_admin,
            failed_tiers=failed,
        )
    else:
        raise BadRequestException("需要提供 file_id 或 media_item_id")

    if decision is None:
        raise NotFoundException("没有找到可播放的文件")
    return ok(playback_plan.to_view(decision))


# ---------------------------------------------------------------------------
# 网页播放器：会话与取流（docs/design/web-player.md §4）
#
# 取流端点一律**不挂登录依赖**，改用查询参数里的短时效签名 token：
# <video src>、hls.js 拉分片、原生 HLS 都带不了自定义 header，整条取流链路
# 在浏览器内部，JS 插不进手（§4.7）。
# ---------------------------------------------------------------------------

# 只放行会话目录里由 ffmpeg 产出的两类文件名。**这是路径穿越的唯一防线**：
# 会话 id 来自签名 token 可信，但文件名来自 URL，必须白名单而不是过滤。
_SEGMENT_NAME = re.compile(r"^(init\.mp4|seg\d{5}\.m4s)$")


async def _decide(
    payload: PlaybackDecideRequest,
    principal: Principal,
    session: AsyncSession,
):
    """decide 与开会话共用的取数与判定。"""
    visible = await visible_library_ids(session, principal)
    capability = playback_plan.capability_from_request(payload.capability)
    failed = frozenset(Tier(t) for t in payload.failed_tiers if t in Tier._value2member_map_)
    if payload.file_id is not None:
        return await playback_plan.decide_for_file(
            session, payload.file_id, capability,
            can_self_enable=principal.is_admin, failed_tiers=failed,
            visible_library_ids=visible,
        )
    if payload.media_item_id is not None:
        files = await playback_plan.library_files_for_unit(
            session, payload.media_item_id, payload.season_number,
            payload.episode_number, visible_library_ids=visible,
        )
        return await playback_plan.decide_for_files(
            files, capability, can_self_enable=principal.is_admin, failed_tiers=failed
        )
    raise BadRequestException("需要提供 file_id 或 media_item_id")


@router.post(
    "/sessions",
    response_model=ApiResponse[PlaybackSessionView],
    summary="开始播放",
    operation_id="playback.session.start",
    openapi_extra={"x-cli-hidden": True},
)
async def start_playback_session(
    payload: PlaybackSessionRequest,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackSessionView]:
    """判定档位并（需要时）起转码会话，返回可直接播放的地址。

    档 0 不起会话，直接给原文件的签名直出地址；档 1–4 起 ffmpeg 会话，
    playlist 一出现就返回——分片按需生成，客户端边拉边转。
    """
    decision = await _decide(payload, principal, session)
    if decision is None:
        raise NotFoundException("没有找到可播放的文件")
    view = playback_plan.to_view(decision)
    if view.outcome != "plan":
        return ok(PlaybackSessionView(decision=view))

    member_id = principal.member_id if principal.member_id is not None else 0
    file = await session.get(LibraryFile, view.file_id)
    if file is None:
        raise NotFoundException("文件已不在台账中")

    subtitle_urls = [
        f"/api/v1/playback/files/{file.id}/subtitles"
        f"?track={quote(s.track_ref, safe='')}"
        f"&token={await issue_stream_token(member_id=member_id, file_id=file.id)}"
        for s in view.subtitles
    ]

    if view.tier == int(Tier.DIRECT_PLAY):
        token = await issue_stream_token(member_id=member_id, file_id=file.id)
        return ok(
            PlaybackSessionView(
                decision=view,
                stream_url=f"/api/v1/playback/files/{file.id}/stream?token={token}",
                subtitle_urls=subtitle_urls,
            )
        )

    manager = get_session_manager()
    # seek 出已转区间要起新会话，同文件的旧会话必须先杀——不然用户连拖五下
    # 进度条就有五个 ffmpeg 在跑（§4.4）。
    await manager.stop_for_file(file.id, member_id)
    policy = await get_setting_store().get(PlaybackPolicySetting)
    backends = await asyncio.to_thread(available_backends)
    # 只有真的转视频才谈得上硬件加速：直通档（-c:v copy）不经编码器，报个
    # 后端名只会让诊断面板骗人。
    hw_used = backends[0] if backends and view.video and view.video.action == "transcode" else None
    try:
        transcode = await manager.start(
            decision,
            source_path=file.file_path,
            member_id=member_id,
            start_ms=payload.start_ms,
            hw_backend=backends[0] if backends else None,
            max_transcode=policy.max_transcode_concurrency,
            max_remux=policy.max_remux_concurrency,
            quota_bytes=policy.transcode_cache_quota_gb * 1024**3,
        )
    except (SessionLimitError, DiskQuotaError) as exc:
        raise ServiceUnavailableException(str(exc)) from exc
    except SessionStartError as exc:
        raise ServiceUnavailableException(f"播放启动失败：{exc}") from exc

    token = await issue_stream_token(
        member_id=member_id, file_id=file.id, session_id=transcode.id
    )
    return ok(
        PlaybackSessionView(
            decision=view,
            session_id=transcode.id,
            stream_url=(
                f"/api/v1/playback/sessions/{transcode.id}/index.m3u8?token={token}"
            ),
            start_ms=payload.start_ms,
            subtitle_urls=subtitle_urls,
            hw_backend=hw_used,
        )
    )


@router.post(
    "/sessions/{session_id}/ping",
    response_model=ApiResponse[dict],
    summary="播放心跳",
    operation_id="playback.session.ping",
    openapi_extra={"x-cli-hidden": True},
)
async def ping_playback_session(
    session_id: Annotated[str, Path()],
    principal: Principal = Depends(require_login),
) -> ApiResponse[dict]:
    """续命。用户关页面不会发任何信号，超时回收是唯一可靠兜底。"""
    member_id = principal.member_id if principal.member_id is not None else 0
    if not get_session_manager().ping(session_id, member_id=member_id):
        raise NotFoundException("会话不存在或已结束")
    return ok({"alive": True})


@router.delete(
    "/sessions/{session_id}",
    response_model=ApiResponse[dict],
    summary="结束播放",
    operation_id="playback.session.stop",
    # confirm 而非 destructive：只掐断本次播放并清掉临时分片，
    # 观看进度、媒体文件都不受影响。
    openapi_extra={"x-cli-hidden": True, "x-cli-dangerous": "confirm"},
)
async def stop_playback_session(
    session_id: Annotated[str, Path()],
    principal: Principal = Depends(require_login),
) -> ApiResponse[dict]:
    manager = get_session_manager()
    member_id = principal.member_id if principal.member_id is not None else 0
    if manager.get(session_id, member_id=member_id) is None:
        raise NotFoundException("会话不存在或已结束")
    await manager.stop(session_id)
    return ok({"stopped": True})


@router.get(
    "/sessions/{session_id}/index.m3u8",
    summary="播放列表",
    operation_id="playback.session.playlist",
    openapi_extra={"x-cli-hidden": True},
)
async def get_session_playlist(
    session_id: Annotated[str, Path()],
    token: Annotated[str, Query()],
) -> FileResponse:
    """HLS 播放列表。EVENT 类型，只增不改——边转边给。"""
    grant = await verify_stream_token(token, session_id=session_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    session = get_session_manager().get(session_id, member_id=grant.member_id)
    if session is None or not session.playlist_path.exists():
        raise NotFoundException("会话不存在或已结束")
    session.touch()  # 拉 playlist 也算活着
    return FileResponse(
        session.playlist_path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/sessions/{session_id}/{name}",
    summary="播放分片",
    operation_id="playback.session.segment",
    openapi_extra={"x-cli-hidden": True},
)
async def get_session_segment(
    session_id: Annotated[str, Path()],
    name: Annotated[str, Path()],
    token: Annotated[str, Query()],
) -> FileResponse:
    """取一个 fMP4 分片或初始化段。"""
    if not _SEGMENT_NAME.match(name):
        raise NotFoundException("分片不存在")
    grant = await verify_stream_token(token, session_id=session_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    session = get_session_manager().get(session_id, member_id=grant.member_id)
    if session is None:
        raise NotFoundException("会话不存在或已结束")
    target = session.directory / name
    if not target.exists():
        raise NotFoundException("分片尚未就绪")
    session.touch()
    return FileResponse(
        target,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/files/{file_id}/stream",
    summary="原文件直出",
    operation_id="playback.file.stream",
    openapi_extra={"x-cli-hidden": True},
)
async def stream_library_file(
    file_id: Annotated[int, Path()],
    token: Annotated[str, Query()],
    session: AsyncSession = Depends(get_session),
):
    """档 0 Direct Play：原文件按 Range 直出，零转码零开销。

    strm 网盘条目在这里跳转到云端直链——服务器零流量（硬边界 2）。
    """
    grant = await verify_stream_token(token, file_id=file_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    file = await session.get(LibraryFile, file_id)
    if file is None:
        raise NotFoundException("文件不存在")
    if is_strm(file.file_path):
        remote = resolve_strm_url(file.file_path)
        if remote is None:
            raise NotFoundException("网盘直链无效")
        return RedirectResponse(remote, status_code=302)
    path = PathLib(file.file_path)
    if not path.exists():
        raise NotFoundException("文件已不在磁盘上")
    return DisconnectAwareFileResponse(
        path, media_type=container_mime_type(file.container)
    )


@router.get(
    "/files/{file_id}/subtitles",
    summary="旁挂字幕",
    operation_id="playback.file.subtitle",
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_subtitle(
    file_id: Annotated[int, Path()],
    track: Annotated[
        str, Query(description="中性轨引用：external:<文件名> / embedded:<序号>")
    ],
    token: Annotated[str, Query()],
    format: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """字幕**永远旁挂**，绝不烧录（硬边界 1）——烧录会把任何档位拖进全转码。

    外挂轨直接读文件；内封轨按需 ffmpeg 抽出来（首次要通读整个容器，之后走
    缓存）。PT 片源的字幕绝大多数是内封的，只服务外挂等于对大部分片子没字幕。
    """
    grant = await verify_stream_token(token, file_id=file_id)
    if grant is None:
        raise NotFoundException("字幕地址无效或已过期")
    file = await session.get(LibraryFile, file_id)
    if file is None:
        raise NotFoundException("文件不存在")
    ref = resolve_external_subtitle(file, track)
    if ref is None:
        index = parse_embedded_track(track)
        if index is not None:
            # 抽取是阻塞的子进程调用，必须放线程池——单进程 async 服务里
            # 一个阻塞调用会卡死全站的搜索、订阅、扫描。
            ref = await asyncio.to_thread(extract_embedded_subtitle, file, index)
    if ref is None:
        raise NotFoundException("字幕轨不存在或暂不支持在网页端渲染")
    try:
        body, media_type = serve_subtitle(ref, format)
    except SubtitleServeError as exc:
        raise NotFoundException(str(exc)) from exc
    return Response(content=body, media_type=media_type)


# ---------------------------------------------------------------------------
# 网页播放器：观看状态（续播点、已看、轨记忆）
#
# 与 Jellyfin 的 /Sessions/Playing 系列同落库路径，「浏览器看一半换 App
# 接着看」因此天然成立。身份来自 Web 登录会话，可见性按成员的库范围校验。
# ---------------------------------------------------------------------------


async def _visible_unit(
    session: AsyncSession,
    principal: Principal,
    media_item_id: int,
    season_number: int,
    episode_number: int,
) -> playback_state.Unit:
    """校验该播放单元对当前成员可见，返回领域层的单元三元组。

    可见性以「有在位文件落在可见库里」为准——与决策接口同一判据，避免
    出现「能报进度却点不开」的错位。
    """
    visible = await visible_library_ids(session, principal)
    files = await playback_plan.library_files_for_unit(
        session,
        media_item_id,
        season_number,
        episode_number,
        visible_library_ids=visible,
    )
    if not files:
        raise NotFoundException("没有找到可播放的文件")
    return (media_item_id, season_number, episode_number)


@router.post(
    "/progress",
    response_model=ApiResponse[PlaybackStateView],
    summary="上报观看进度",
    operation_id="playback.progress",
    openapi_extra={"x-cli-hidden": True},
)
async def report_playback_progress(
    payload: PlaybackProgressRequest,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackStateView]:
    """开始 / 心跳 / 停止三种事件同一入口，落 ``playback_state`` 并发 webhook。

    已看判定的分母（片长）一律服务端算，不听客户端报——否则同一部片在
    网页端和 Jellyfin 客户端会给出不同的「已看」结论。
    """
    unit = await _visible_unit(
        session,
        principal,
        payload.media_item_id,
        payload.season_number,
        payload.episode_number,
    )
    member_id = principal.member_id if principal.member_id is not None else 0
    if payload.event == "start":
        row = await playback_watch.record_start(
            session,
            unit,
            member_id=member_id,
            audio_track=payload.audio_track,
            subtitle_track=payload.subtitle_track,
        )
    else:
        row = await playback_watch.record_progress(
            session,
            unit,
            member_id=member_id,
            position_ms=payload.position_ms,
            stopped=payload.event == "stop",
            audio_track=payload.audio_track,
            subtitle_track=payload.subtitle_track,
        )
    return ok(
        PlaybackStateView(
            position_ms=row.position_ms,
            played=row.played,
            play_count=row.play_count,
            duration_ms=await playback_state.unit_runtime_ms(session, unit),
            audio_track=row.audio_track,
            subtitle_track=row.subtitle_track,
        )
    )


@router.get(
    "/resume",
    response_model=ApiResponse[PlaybackStateView],
    summary="续播点与记忆轨",
    operation_id="playback.resume",
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_resume(
    media_item_id: Annotated[int, Query()],
    season_number: Annotated[int, Query()] = 0,
    episode_number: Annotated[int, Query()] = 0,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackStateView]:
    """起播前问一次「上次看到哪、用的哪条音轨/字幕」。

    从未播过不是错误——返回全零状态，播放器从头开始放。
    """
    unit = await _visible_unit(
        session, principal, media_item_id, season_number, episode_number
    )
    member_id = principal.member_id if principal.member_id is not None else 0
    states = await playback_state.get_states(session, [media_item_id], member_id=member_id)
    row = states.get(unit)
    return ok(
        PlaybackStateView(
            position_ms=row.position_ms if row else 0,
            played=row.played if row else False,
            play_count=row.play_count if row else 0,
            duration_ms=await playback_state.unit_runtime_ms(session, unit),
            audio_track=row.audio_track if row else None,
            subtitle_track=row.subtitle_track if row else None,
        )
    )


# ---------------------------------------------------------------------------
# 网页播放器：策略配置（「设置 → 播放」页 + 软件转码同意链路 §3.6）
# ---------------------------------------------------------------------------


async def _policy_view() -> PlaybackPolicyView:
    stored = await get_setting_store().get(PlaybackPolicySetting)
    backends = await asyncio.to_thread(available_backends)
    return PlaybackPolicyView(
        **stored.model_dump(),
        hardware_available=bool(backends),
        hw_backends=list(backends),
    )


@router.get(
    "/policy",
    response_model=ApiResponse[PlaybackPolicyView],
    summary="读取播放策略",
    operation_id="playback.policy.show",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_policy() -> ApiResponse[PlaybackPolicyView]:
    """「设置 → 播放」页的数据源。硬件加速一项是实测结果而非配置项。"""
    return ok(await _policy_view())


@router.put(
    "/policy",
    response_model=ApiResponse[PlaybackPolicyView],
    summary="保存播放策略",
    operation_id="playback.policy.set",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-hidden": True},
)
async def save_playback_policy(
    payload: PlaybackPolicyPayload,
) -> ApiResponse[PlaybackPolicyView]:
    """按字段增量保存：``None`` 的项保持原值。

    同意弹窗（§3.6）只翻 ``software_transcode_enabled`` 一个开关，不该被迫
    先读全量再回写——那样会把另一个标签页刚改的并发数悄悄覆盖回去。
    """
    store = get_setting_store()
    stored = await store.get(PlaybackPolicySetting)
    changes = payload.model_dump(exclude_none=True)
    if changes:
        # 重新构造而不是 model_copy(update=...)：后者跳过校验，并发数、码率上限
        # 上的 ge/le 约束就形同虚设（写进去的非法值要到起转码时才炸）。
        await store.set(PlaybackPolicySetting(**{**stored.model_dump(), **changes}))
    return ok(await _policy_view())
