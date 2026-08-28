"""播放记录与网页播放器的 Web 业务接口。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path as PathLib
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.api.deps import require_admin, require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import (
    BadRequestException,
    NotFoundException,
    ServiceUnavailableException,
)
from movieclaw_api.schemas.library import SeasonEpisodesView
from movieclaw_api.schemas.playback import (
    HwBackendStatusView,
    HwProbeView,
    MediaActivityView,
    PlaybackArtifactUploadView,
    PlaybackClientLogPayload,
    PlaybackDecideRequest,
    PlaybackDecisionView,
    PlaybackDiagnosticsView,
    PlaybackFontsView,
    PlaybackItemView,
    PlaybackMetricPayload,
    PlaybackPolicyPayload,
    PlaybackPolicyView,
    PlaybackProgressRequest,
    PlaybackSessionRequest,
    PlaybackSessionView,
    PlaybackSourceView,
    PlaybackStateView,
    PlaybackStatsView,
    RecentWatchView,
    TrickplayView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import media_scrape
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.services.library.items import build_season_episodes, episode_view
from movieclaw_api.services.media_probe import probe_keyframe_before
from movieclaw_api.services.playback import metrics, trickplay
from movieclaw_api.services.playback import plan as playback_plan
from movieclaw_api.services.playback import warmup as playback_warmup
from movieclaw_api.services.playback import watch as playback_watch
from movieclaw_api.services.playback.embedded_subs import (
    extract_embedded_fonts,
    extract_embedded_subtitle_async,
    font_cache_dir,
    safe_font_name,
)
from movieclaw_api.services.playback.ffmpeg_args import (
    HW_BACKENDS,
    INIT_NAME,
    SEGMENT_PATTERN,
    SEGMENT_SECONDS,
    effective_hw_backend,
)
from movieclaw_api.services.playback.hwprobe import (
    available_backends,
    available_local_backends,
    probe_backends_async,
)
from movieclaw_api.services.playback.limits import (
    MAX_REMUX_CONCURRENCY,
    auto_quota_bytes,
    auto_transcode_concurrency,
)
from movieclaw_api.services.playback.remote_worker import (
    effective_remote_transcode_config,
    get_remote_worker_registry,
    remote_worker_available,
)
from movieclaw_api.services.playback.session import (
    DiskQuotaError,
    SessionLimitError,
    SessionStartError,
    TranscodeSession,
    get_session_manager,
)
from movieclaw_api.services.playback.signing import issue_stream_token, verify_stream_token
from movieclaw_api.services.playback_activity import media_activity_overview, revoke_device
from movieclaw_api.services.playback_recent import recent_watch_items
from movieclaw_api.settings import PlaybackPolicySetting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_db.engine import get_session
from movieclaw_db.models import LibraryFile, MediaItem, PlaybackMetric
from movieclaw_db.repositories.media_repo import MediaItemRepository
from movieclaw_playback import state as playback_state
from movieclaw_playback.decide import PlaybackPlan
from movieclaw_playback.decide import PlaybackTier as Tier
from movieclaw_playback.hls_vod import (
    build_master_playlist,
    build_media_playlist,
    build_subtitle_playlist,
    compute_segment_plan,
    compute_uniform_plan,
)
from movieclaw_playback.keyframes import read_keyframe_index
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

logger = logging.getLogger("movieclaw_api.playback")

router = APIRouter(prefix="/playback", tags=["playback"])


class _SubtitleClientDisconnected(Exception):
    """客户端已放弃字幕请求，且底层抽取任务已经完成取消。"""

_DIAGNOSTIC_SECRET_RE = re.compile(
    r"((?:[?&]|\b)(?:token|access_token|signature|sig)=)[^&\s]+", re.IGNORECASE
)


def _diagnostic_error(error: str | None) -> str | None:
    """截断并脱敏错误文本，避免把签名 URL 带进播放器诊断。"""
    if not error:
        return None
    sanitized = _DIAGNOSTIC_SECRET_RE.sub(r"\1<redacted>", error)
    return sanitized[-1000:]


def _diagnostic_processing_mode(session: TranscodeSession) -> str:
    """把内部档位归一成面向用户的执行模式。"""
    if session.remote:
        return "remote-hardware"
    if session.plan.video.action == "transcode":
        return "local-hardware" if session.hw_backend else "local-software"
    if session.plan.audio.action == "transcode":
        return "audio-transcode"
    return "remux"


def _diagnostic_encoder(session: TranscodeSession) -> str | None:
    if session.plan.video.action != "transcode":
        return None
    if session.hw_backend:
        backend = HW_BACKENDS.get(session.hw_backend)
        if backend is not None:
            return backend.encoder
    return "libx264"


def _diagnostic_playback_cursor(session: TranscodeSession) -> int:
    """取最近一次播放器活动对应的分片号，作为缺口展示的时间游标。

    播放器会并行请求分片，完成顺序也可能与请求顺序不同，所以优先使用最近
    一次请求（它代表播放器当前的供片意图），不能让晚到的旧分片响应把游标
    往回覆盖。没有请求记录时才回退到最近供给分片，再没有播放器事件则使用
    当前转码头。
    """
    if session.last_requested_segment is not None:
        return session.last_requested_segment
    if session.last_served_segment is not None:
        return session.last_served_segment
    return session.head_segment


def _build_playback_diagnostics(
    session: TranscodeSession,
) -> PlaybackDiagnosticsView:
    """组装单次快照；只读取内存台账和本地缓存，不返回任何访问凭据。"""
    manager = get_session_manager()
    registry = get_remote_worker_registry()
    worker = None
    if session.remote_worker_id:
        worker = next(
            (
                item
                for item in registry.snapshot()
                if item.get("worker_id") == session.remote_worker_id
            ),
            None,
        )
    job = (
        registry.job_state(session.remote_job_id or "")
        if session.remote_job_id
        else None
    )
    highest_produced = (
        manager._highest_produced(session) if session.segment_plan is not None else None
    )
    uploads = [
        PlaybackArtifactUploadView(
            name=event.name,
            status=event.status,
            received_bytes=event.received_bytes,
            content_length=event.content_length,
            transfer_encoding=event.transfer_encoding,
            occurred_at_ms=event.occurred_at_ms,
        )
        for event in list(reversed(session.remote_uploads))[:12]
    ]
    try:
        cache_bytes = session.size_bytes()
    except OSError:
        # 诊断轮询可能正好与结束会话并发；目录已开始清理时仍返回快照，
        # 不能因为统计临时目录大小把播放器旁路请求变成 500。
        cache_bytes = 0
    job_type = job.get("type") if job else None
    job_out_time_ms = job.get("out_time_ms") if job else None
    if not isinstance(job_out_time_ms, int):
        job_out_time_ms = None
    job_speed = job.get("speed") if job else None
    if not isinstance(job_speed, str):
        job_speed = None
    job_phase = job.get("phase") if job else None
    if not isinstance(job_phase, str):
        job_phase = None
    job_exit_code = job.get("exit_code") if job else None
    if not isinstance(job_exit_code, int) or isinstance(job_exit_code, bool):
        job_exit_code = None
    job_error = _diagnostic_error(
        job.get("error") if job and isinstance(job.get("error"), str) else None
    )
    job_stderr_tail = _diagnostic_error(
        job.get("stderr_tail")
        if job and isinstance(job.get("stderr_tail"), str)
        else None
    )
    session_error = _diagnostic_error(session.error)
    if session_error is None:
        session_error = job_error
    failed_segments = sorted(session.remote_failed_segments)
    playback_cursor = _diagnostic_playback_cursor(session)
    active_failed_segments = [
        segment for segment in failed_segments if segment >= playback_cursor
    ][:32]
    historical_failed_segments = [
        segment for segment in failed_segments if segment < playback_cursor
    ][:32]

    return PlaybackDiagnosticsView(
        session_state=session.state,
        session_error=session_error,
        processing_mode=_diagnostic_processing_mode(session),
        execution_location="remote_worker" if session.remote else "nas",
        backend=session.hw_backend,
        encoder=_diagnostic_encoder(session),
        worker_id=session.remote_worker_id,
        worker_version=worker.get("worker_version") if worker else None,
        worker_platform=worker.get("platform") if worker else None,
        worker_arch=worker.get("arch") if worker else None,
        ffmpeg_version=worker.get("ffmpeg_version") if worker else None,
        worker_online=(
            bool(worker.get("online"))
            if worker is not None
            else (None if session.remote_restarting else False)
        ),
        worker_last_seen_seconds=(
            float(worker["last_seen_seconds"])
            if worker is not None and worker.get("last_seen_seconds") is not None
            else None
        ),
        job_id=session.remote_job_id,
        attempt_id=session.remote_job_id,
        job_state=job_type if isinstance(job_type, str) else None,
        job_out_time_ms=job_out_time_ms,
        job_speed=job_speed,
        job_phase=job_phase,
        job_exit_code=job_exit_code,
        job_error=job_error,
        job_stderr_tail=job_stderr_tail,
        head_segment=session.head_segment if session.segment_plan is not None else None,
        highest_produced_segment=highest_produced,
        requested_segment=session.last_requested_segment,
        served_segment=session.last_served_segment,
        segment_wait_ms=session.last_segment_wait_ms,
        segment_status=session.last_segment_status,
        pending_segments=sorted(session.pending_segments)[:32],
        failed_segments=active_failed_segments,
        historical_failed_segments=historical_failed_segments,
        recent_uploads=uploads,
        cache_bytes=cache_bytes,
        total_segments=session.segment_plan.count if session.segment_plan is not None else None,
    )


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
    # 决策接口与开会话接口必须共享同一组参数转发和可见性规则；否则客户端
    # 在切换音轨/字幕/清晰度时会看到与实际起播不同的计划。
    decision = await _decide(payload, principal, session)

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
# 雪碧图文件名由服务端生成，但仍经过 URL——白名单一视同仁。
_TRICKPLAY_SHEET_NAME = re.compile(r"^sprite_\d{3}\.jpg$")


def _select_execution_backend(
    decision: PlaybackPlan,
    *,
    available: tuple[str, ...],
    local_backends: tuple[str, ...],
    remote_video_available: bool,
) -> tuple[str | None, bool]:
    """为当前播放计划选择真正能执行的后端。

    ``available`` 是给决策层用的合并能力快照，``local_backends`` 则只包含
    NAS 本机实际探测通过的后端。两者不能按列表首项直接使用：列表顺序是
    全局优先级，不代表该计划（尤其是 PGS 烧录）的滤镜链兼容性。先选能在
    NAS 执行当前计划的后端；只有本地都不兼容时，才把在线 VideoToolbox
    Worker 作为候选。返回值的第二项明确标出是否需要远程会话。
    """
    if (
        decision.tier is not Tier.HARDWARE_TRANSCODE
        or decision.video.action != "transcode"
    ):
        return None, False

    for backend in local_backends:
        if effective_hw_backend(decision, backend) is not None:
            return backend, False

    if (
        remote_video_available
        and "videotoolbox" in available
        and effective_hw_backend(decision, "videotoolbox") is not None
    ):
        return "videotoolbox", True
    return None, False

#: 播放列表里 `#EXT-X-MAP` 那行的初始化段地址，形如 `URI="init.mp4"`。
_PLAYLIST_MAP_URI = re.compile(r'(#EXT-X-MAP:.*?URI=")([^"]+)(")')


def playlist_with_tokens(playlist: str, token: str) -> str:
    """给播放列表里的每个分片地址补上取流 token。

    ffmpeg 写出来的地址是裸相对路径（`init.mp4` / `seg00000.m4s`），而浏览器
    按**播放列表自身的 URL** 解析相对地址时**会把 query 丢掉**——播放列表带着
    `?token=` 请求成功，它引用的分片却一个凭据都没有，全部倒在鉴权上。整条取流
    链路在浏览器内部，JS 插不进手补 header（§4.7），所以只能由服务端在发出前把
    token 写进每个地址。

    只改地址行：`#` 开头的是标签，除了 `EXT-X-MAP` 里的 URI 之外都不带地址。
    """
    if not token:
        return playlist
    query = f"?token={token}"
    lines = []
    for line in playlist.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            lines.append(line)
        elif stripped.startswith("#"):
            lines.append(_PLAYLIST_MAP_URI.sub(rf"\g<1>\g<2>{query}\g<3>", line))
        else:
            lines.append(line.replace(stripped, stripped + query, 1))
    return "".join(lines)


#: 攒到这个行数就顺手裁一次。指标是趋势数据不是台账。
_METRIC_PURGE_TRIGGER = 3000


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
            preferred_audio=payload.audio_track,
            preferred_subtitle=payload.subtitle_track,
            max_height=payload.max_height,
            visible_library_ids=visible,
        )
    if payload.media_item_id is not None:
        files = await playback_plan.library_files_for_unit(
            session, payload.media_item_id, payload.season_number,
            payload.episode_number, visible_library_ids=visible,
        )
        return await playback_plan.decide_for_files(
            files, capability, can_self_enable=principal.is_admin, failed_tiers=failed,
            preferred_audio=payload.audio_track,
            preferred_subtitle=payload.subtitle_track,
            max_height=payload.max_height,
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

    观看状态在这里一并解析（§6.10）：``start_ms`` 缺省时用续播点（看完的
    从头播）、``audio_track`` 缺省时用上次听的那条轨，整份状态随响应带回。
    起播链路因此不用先问一次 ``/resume``——省一个串行往返，分享出去的链接
    也天然「各看各的进度」。
    """
    started_at = time.perf_counter()
    member_id = principal.member_id if principal.member_id is not None else 0
    watch_row = None
    watch_view: PlaybackStateView | None = None
    if payload.media_item_id is not None:
        unit = (payload.media_item_id, payload.season_number, payload.episode_number)
        states = await playback_state.get_states(
            session, [payload.media_item_id], member_id=member_id
        )
        watch_row = states.get(unit)
        watch_view = PlaybackStateView(
            position_ms=watch_row.position_ms if watch_row else 0,
            played=watch_row.played if watch_row else False,
            play_count=watch_row.play_count if watch_row else 0,
            duration_ms=await playback_state.unit_runtime_ms(session, unit),
            audio_track=watch_row.audio_track if watch_row else None,
            subtitle_track=watch_row.subtitle_track if watch_row else None,
        )
        if payload.audio_track is None and watch_row and watch_row.audio_track:
            # 上次听的哪条轨接着用。轨在这次选中的文件里不存在时 decide 会
            # 自动回退默认轨（换版本文件轨序会变，这是既有覆盖）。
            payload = payload.model_copy(update={"audio_track": watch_row.audio_track})
        if payload.subtitle_track is None and watch_row and watch_row.subtitle_track:
            # 字幕记忆同款：上次选的 PGS 轨会自动继续烧录，文本轨/"off" 在
            # decide 里是 no-op（只有 PGS 改变视频策略）。
            payload = payload.model_copy(
                update={"subtitle_track": watch_row.subtitle_track}
            )
    resolved_start_ms = payload.start_ms
    if resolved_start_ms is None:
        # 看完的重播从头开始——续播到最后三十秒等于点开就是片尾
        resolved_start_ms = (
            0 if (watch_row is None or watch_row.played) else watch_row.position_ms
        )

    decision = await _decide(payload, principal, session)
    decide_ms = int((time.perf_counter() - started_at) * 1000)
    if decision is None:
        raise NotFoundException("没有找到可播放的文件")
    view = playback_plan.to_view(decision)
    if view.outcome != "plan":
        return ok(PlaybackSessionView(decision=view, watch=watch_view))

    file = await session.get(LibraryFile, view.file_id)
    if file is None:
        raise NotFoundException("文件已不在台账中")

    manager = get_session_manager()
    if view.tier != int(Tier.DIRECT_PLAY):
        # 第一次决策可能看到同文件旧会话仍占着远程 Worker 的槽位，暂时落到
        # 软件转码。先释放旧会话，再重新决策一次，才能把刚空出来的远程能力
        # 纳入最终结果；没有旧会话时不重复做这次决策。
        replaced = await manager.stop_for_file(file.id, member_id)
        if replaced:
            decision = await _decide(payload, principal, session)
            if decision is None:
                raise NotFoundException("没有找到可播放的文件")
            view = playback_plan.to_view(decision)
            if view.outcome != "plan":
                return ok(PlaybackSessionView(decision=view, watch=watch_view))
            file = await session.get(LibraryFile, view.file_id)
            if file is None:
                raise NotFoundException("文件已不在台账中")

    # 诊断面板的「源 → 处理」层次要有左半边：台账真值原样带回（§6.5）
    source_view = PlaybackSourceView(
        container=file.container,
        resolution=file.resolution,
        video_codec=file.video_codec,
        hdr=file.hdr,
        bit_rate=file.bit_rate,
        frame_rate=file.frame_rate,
        size_bytes=file.size_bytes,
    )

    # 详情页可能正在为同一条目预热字幕；正式播放已经接管 IO，取消那条
    # 后台任务，避免留下与播放无关的 ffmpeg（尤其是 PGS 的 .part.sup）。
    playback_warmup.cancel(file.media_item_id)

    # 进度条缩略图：后台起，不挡首帧；延迟 90 秒 + 读入限速，起播关键窗口
    # 不与首片转码抢 IO（「首次起播卡缓冲、重进就好」的头号元凶，§6.10）。
    trickplay.schedule(file, delay_s=90)

    subtitle_urls = [
        f"/api/v1/playback/files/{file.id}/subtitles"
        f"?track={quote(s.track_ref, safe='')}"
        f"&token={await issue_stream_token(member_id=member_id, file_id=file.id)}"
        for s in view.subtitles
    ]

    if view.tier == int(Tier.DIRECT_PLAY):
        token = await issue_stream_token(member_id=member_id, file_id=file.id)
        # 分段计时（§6.10）：用户报「起播慢」时，这一行直接指认卡在哪一段。
        # 决策段偏慢多半是关键帧采样在现场读盘——详情页预热没盖住的路径。
        logger.info(
            "播放会话就绪：档 0 直出 · 决策 %d 毫秒（file_id=%s）", decide_ms, file.id
        )
        return ok(
            PlaybackSessionView(
                decision=view,
                stream_url=f"/api/v1/playback/files/{file.id}/stream?token={token}",
                # 直出没有会话时间轴，续播位置由前端 seek 到 watch.position_ms
                start_ms=resolved_start_ms,
                subtitle_urls=subtitle_urls,
                watch=watch_view,
                source=source_view,
            )
        )

    async def _keyframe_index():
        """全片关键帧索引，只有直通档的 VOD 规划要它。冷缓存时 mp4 要过
        ffprobe（上秒级）——这是把它并入 gather 的主要理由：分享链接直达
        播放页时详情页预热没跑过，串行 await 会把这一秒全记在起播上。"""
        if not file.duration_seconds or not view.video or view.video.action != "copy":
            return None
        return await asyncio.to_thread(read_keyframe_index, file.file_path)

    # 三件准备工作互相独立，并行做：策略读取（设置存储自带短会话，与请求
    # 会话无关）、硬件后端探测、关键帧索引。旧会话已在上面的最终决策前串行
    # 释放，不能重新放回这里，否则远程 Worker 的槽位又会产生竞态。
    prep_started_at = time.perf_counter()
    policy, backends, keyframe_index = await asyncio.gather(
        get_setting_store().get(PlaybackPolicySetting),
        asyncio.to_thread(available_backends),
        _keyframe_index(),
    )
    # ``backends`` 可能包含外置 Worker 的 videotoolbox；本地命令只能从真实的
    # NAS 探测快照中选编码器。只有在执行端确认仍在线时，才把 videotoolbox
    # 作为远程命令发给 Worker。
    local_backends = (
        await asyncio.to_thread(available_local_backends) if backends else ()
    )
    remote_video_available = remote_worker_available("videotoolbox")
    prep_ms = int((time.perf_counter() - prep_started_at) * 1000)
    # 只有真的转视频才谈得上硬件加速：直通档（-c:v copy）不经编码器，报个
    # 后端名只会让诊断面板骗人。烧录时 VAAPI/QSV 会退软件编码（overlay 是
    # 软件滤镜，这两家编码器吃不了软件帧），同样要报实际值。后端选择必须
    # 结合当前计划的滤镜兼容性，不能把合并能力列表的第一项当成可执行后端。
    execution_backend, use_remote = _select_execution_backend(
        decision,
        available=backends,
        local_backends=local_backends,
        remote_video_available=remote_video_available,
    )
    if view.tier == int(Tier.HARDWARE_TRANSCODE) and execution_backend is None:
        # 决策阶段看到的硬件能力可能在准备阶段断线，或本地后端与当前滤镜链
        # 不兼容。不能把硬件档的计划悄悄交给 libx264；把硬件档标记为失败后
        # 重新走统一降档逻辑：软件开关关闭时返回 consent，开启时才允许软转。
        retry_failed_tiers = sorted(
            {*payload.failed_tiers, int(Tier.HARDWARE_TRANSCODE)}
        )
        fallback_payload = payload.model_copy(update={"failed_tiers": retry_failed_tiers})
        decision = await _decide(fallback_payload, principal, session)
        if decision is None:
            raise NotFoundException("没有找到可播放的文件")
        view = playback_plan.to_view(decision)
        if view.outcome != "plan":
            return ok(PlaybackSessionView(decision=view, watch=watch_view))
        file = await session.get(LibraryFile, view.file_id)
        if file is None:
            raise NotFoundException("文件已不在台账中")
        source_view = PlaybackSourceView(
            container=file.container,
            resolution=file.resolution,
            video_codec=file.video_codec,
            hdr=file.hdr,
            bit_rate=file.bit_rate,
            frame_rate=file.frame_rate,
            size_bytes=file.size_bytes,
        )
        playback_warmup.cancel(file.media_item_id)
        trickplay.schedule(file, delay_s=90)
        subtitle_urls = [
            f"/api/v1/playback/files/{file.id}/subtitles"
            f"?track={quote(s.track_ref, safe='')}"
            f"&token={await issue_stream_token(member_id=member_id, file_id=file.id)}"
            for s in view.subtitles
        ]
        execution_backend = None
        use_remote = False
    hw_used = (
        effective_hw_backend(decision, execution_backend)
        if execution_backend and view.video and view.video.action == "transcode"
        else None
    )
    # 仅把「硬件转码」任务交给远程硬件 Worker；直通/音频单转继续走 NAS，
    # 软件转码也保留本地回路。远程 Worker 只有在配置固定 HTTP(S) 根地址时才
    # 会被 remote_worker_enabled 暴露给决策层，不能从请求 Host 头推导地址。
    remote_base_url = effective_remote_transcode_config().base_url
    # VOD 预生成规划（§12）：直通档按全片关键帧索引算分片边界；转码档
    # force_key_frames 在绝对栅格上强插关键帧，用等长规划。规划失败（时长
    # 未知 / 关键帧索引读不出）退回旧的会话相对模式，一切照旧。
    segment_plan = None
    if file.duration_seconds:
        duration_s = float(file.duration_seconds)
        if view.video and view.video.action == "transcode":
            segment_plan = compute_uniform_plan(duration_s, target_s=SEGMENT_SECONDS)
        elif keyframe_index is not None:
            segment_plan = compute_segment_plan(
                keyframe_index.times_s, duration_s, target_s=SEGMENT_SECONDS
            )
    start_ms = resolved_start_ms
    if segment_plan is None and start_ms > 0 and view.video and view.video.action == "copy":
        # 旧模式的关键帧校正（VOD 下不需要：start() 自己对齐到分片边界）
        keyframe_s = await asyncio.to_thread(
            probe_keyframe_before, file.file_path, start_ms / 1000
        )
        if keyframe_s is not None:
            start_ms = int(keyframe_s * 1000)
    spawn_started_at = time.perf_counter()
    try:
        transcode = await manager.start(
            decision,
            source_path=file.file_path,
            member_id=member_id,
            start_ms=start_ms,
            segment_plan=segment_plan,
            hw_backend=execution_backend,
            # 资源上限不再是配置项，按机器规格自动推导（limits.py）
            max_transcode=auto_transcode_concurrency(hardware=bool(execution_backend)),
            max_remux=MAX_REMUX_CONCURRENCY,
            quota_bytes=auto_quota_bytes(manager.cache_root),
            use_remote=use_remote,
            remote_base_url=remote_base_url if use_remote else None,
        )
    except (SessionLimitError, DiskQuotaError) as exc:
        raise ServiceUnavailableException(str(exc)) from exc
    except SessionStartError as exc:
        raise ServiceUnavailableException(f"播放启动失败：{exc}") from exc
    spawn_ms = int((time.perf_counter() - spawn_started_at) * 1000)

    token = await issue_stream_token(
        member_id=member_id, file_id=file.id, session_id=transcode.id
    )
    total_ms = int((time.perf_counter() - started_at) * 1000)
    # 分段计时（§6.10）：决策段偏慢 = 关键帧采样在现场读盘（详情页预热没盖住
    # 的路径）；准备段偏慢 = 杀旧会话的 SIGTERM 等待（换字幕烧录/换音轨重开
    # 时最常见，配合 stop_for_file 的日志看）或关键帧索引现场读盘；ffmpeg 段
    # 偏慢 = 进程起不来或首列表难产（转码/IO 竞争，看 trickplay 与存储负载）。
    # 用户报「起播慢」时这一行直接指认方向。
    logger.info(
        "播放会话就绪：档 %s · 决策 %d 毫秒 · 准备 %d 毫秒 · ffmpeg %d 毫秒 · 共 %d 毫秒"
        "（file_id=%s hw=%s session=%s）",
        view.tier, decide_ms, prep_ms, spawn_ms, total_ms,
        file.id, hw_used or "无", transcode.id,
    )
    return ok(
        PlaybackSessionView(
            decision=view,
            session_id=transcode.id,
            stream_url=(
                f"/api/v1/playback/sessions/{transcode.id}/index.m3u8?token={token}"
            ),
            # master 列表带 WEBVTT 字幕组：iOS 原生 HLS 用它，字幕成为系统级
            # 字幕轨——画中画小窗、原生全屏里都由系统渲染（§12）
            master_url=(
                f"/api/v1/playback/sessions/{transcode.id}/master.m3u8?token={token}"
                if segment_plan is not None
                else None
            ),
            # VOD：时间轴是文件绝对时间，start_ms 只是建议起播位置（解析后
            # 的原值，不必对齐边界——播放器 seek 到毫秒都行）
            start_ms=resolved_start_ms if segment_plan is not None else start_ms,
            timeline="file" if segment_plan is not None else "session",
            subtitle_urls=subtitle_urls,
            hw_backend=hw_used,
            watch=watch_view,
            source=source_view,
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
) -> Response:
    """HLS 播放列表。

    VOD 模式（§12）：按分片规划一次性生成完整列表（VOD + ENDLIST），播放器
    把它当真正的点播——总时长已知、seek 任意位置、绝不贴直播边缘。
    旧模式：转发 ffmpeg 边写的 EVENT 列表。
    """
    grant = await verify_stream_token(token, session_id=session_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    session = get_session_manager().get(session_id, member_id=grant.member_id)
    if session is None:
        raise NotFoundException("会话不存在或已结束")
    session.touch()  # 拉 playlist 也算活着
    if session.segment_plan is not None:
        playlist = build_media_playlist(
            session.segment_plan,
            init_name=INIT_NAME,
            segment_name=SEGMENT_PATTERN,
            query=f"?token={token}",
        )
        return Response(
            content=playlist,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )
    if not session.playlist_path.exists():
        raise NotFoundException("会话不存在或已结束")
    playlist = await asyncio.to_thread(session.playlist_path.read_text, encoding="utf-8")
    return Response(
        content=playlist_with_tokens(playlist, token),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


#: HLS 字幕组 NAME 的语言显示名。装进系统播放器的字幕菜单里给人看的，
#: 覆盖常见语言即可，冷门语言直接显示原始码也能认。
_SUBTITLE_LANG_NAMES = {
    "chi": "中文", "zho": "中文", "zh": "中文",
    "eng": "英文", "en": "英文",
    "jpn": "日文", "ja": "日文",
    "kor": "韩文", "ko": "韩文",
}

#: 能进 HLS 字幕组的轨：文本轨（vtt 含 srt 转换；ass 服务端降级转 VTT）。
#: PGS 是图形字幕转不了。**与前端 planSubtitleTracks 的 options 过滤规则
#: 必须一致**——前端按 options 的下标定位系统字幕轨。
_MASTER_SUBTITLE_KINDS = {"vtt", "ass"}

# 只有转码目标是本服务明确固定过的编码时，才把 RFC 6381 标识写入 master。
# copy 音轨只保存 codec family，没有保存 AAC profile 等完整信息，不能据此
# 猜测 mp4a.40.2；Safari 收到错误 CODECS 比没有声明更容易走错解码路径。
_HLS_AUDIO_CODEC_IDS = {
    "aac": "mp4a.40.2",
    "ac3": "ac-3",
    "eac3": "ec-3",
}


def _master_subtitle_tracks(session) -> list:
    return [s for s in session.plan.subtitles if s.kind in _MASTER_SUBTITLE_KINDS]


def _master_playlist_codecs(session: TranscodeSession) -> str | None:
    """返回本会话可以确定的 HLS CODECS，未知时返回 None。

    服务端转码视频统一是 H.264 High@4.1，RFC 6381 标识为 avc1.640029。
    音频只有在本次明确转码时才知道 profile：AAC 参数由 ffmpeg 装配器锁为
    AAC-LC，E-AC-3/AC-3 的目标编码也没有 profile 歧义。源音轨 copy 路径
    没有保存足够的 profile 信息，因此整条声明保守省略。
    """
    plan = session.plan
    if plan.video.action != "transcode" or (plan.video.codec or "").lower() != "h264":
        return None
    codecs = ["avc1.640029"]
    if plan.audio.track_ref is None:
        return ",".join(codecs)
    if plan.audio.action != "transcode":
        return None
    audio_codec = _HLS_AUDIO_CODEC_IDS.get((plan.audio.codec or "").lower())
    if audio_codec is None:
        return None
    codecs.append(audio_codec)
    return ",".join(codecs)


@router.get(
    "/sessions/{session_id}/master.m3u8",
    summary="master 播放列表（含字幕组）",
    operation_id="playback.session.master",
    openapi_extra={"x-cli-hidden": True},
)
async def get_session_master_playlist(
    session_id: Annotated[str, Path()],
    token: Annotated[str, Query()],
) -> Response:
    """master 列表：一路视频 + WEBVTT 字幕组（仅 VOD 会话）。

    iOS 原生 HLS（AVPlayer）吃它，字幕由系统在任何表面（内联/全屏/画中画）
    渲染——这是网页 DOM 字幕层做不到的（PiP 图层只含视频帧）。
    """
    grant = await verify_stream_token(token, session_id=session_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    session = get_session_manager().get(session_id, member_id=grant.member_id)
    if session is None or session.segment_plan is None:
        raise NotFoundException("会话不存在或已结束")
    session.touch()
    subtitles: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for i, track in enumerate(_master_subtitle_tracks(session)):
        name = _SUBTITLE_LANG_NAMES.get((track.language or "").lower(), track.language or "字幕")
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name} {seen[name]}"
        subtitles.append((name, f"sub{i}.m3u8"))
    return Response(
        content=build_master_playlist(
            media_uri="index.m3u8",
            subtitles=subtitles,
            codecs=_master_playlist_codecs(session),
            query=f"?token={token}",
        ),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/sessions/{session_id}/sub{index}.m3u8",
    summary="字幕媒体列表",
    operation_id="playback.session.subtitle-playlist",
    openapi_extra={"x-cli-hidden": True},
)
async def get_session_subtitle_playlist(
    session_id: Annotated[str, Path()],
    index: Annotated[int, Path(ge=0, le=99)],
    token: Annotated[str, Query()],
) -> Response:
    """字幕组里一条轨的媒体列表：整片一个 VTT 分片。"""
    grant = await verify_stream_token(token, session_id=session_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    session = get_session_manager().get(session_id, member_id=grant.member_id)
    if session is None or session.segment_plan is None:
        raise NotFoundException("会话不存在或已结束")
    tracks = _master_subtitle_tracks(session)
    if index >= len(tracks):
        raise NotFoundException("字幕轨不存在")
    session.touch()
    file_token = await issue_stream_token(
        member_id=session.member_id, file_id=session.file_id
    )
    vtt_uri = (
        f"/api/v1/playback/files/{session.file_id}/subtitles"
        f"?track={quote(tracks[index].track_ref, safe='')}"
        f"&format=vtt&token={file_token}"
    )
    return Response(
        content=build_subtitle_playlist(
            vtt_uri=vtt_uri, duration_s=session.segment_plan.duration_s
        ),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/sessions/{session_id}/diagnostics",
    response_model=ApiResponse[PlaybackDiagnosticsView],
    summary="播放会话诊断",
    operation_id="playback.session.diagnostics",
    openapi_extra={"x-cli-hidden": True},
)
async def get_session_diagnostics(
    session_id: Annotated[str, Path()],
    token: Annotated[str, Query()],
) -> ApiResponse[PlaybackDiagnosticsView]:
    """返回当前播放会话的脱敏执行与供片状态。"""
    grant = await verify_stream_token(token, session_id=session_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    manager = get_session_manager()
    session = manager.get(session_id, member_id=grant.member_id)
    if session is None:
        raise NotFoundException("会话不存在或已结束")
    session.touch()
    return ok(_build_playback_diagnostics(session))


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
    """取一个 fMP4 分片或初始化段。

    VOD 模式下这里就是 seek 的入口：播放器按预生成列表请求任意分片，
    ``ensure_segment`` 负责「等 ffmpeg 转过来」或「杀掉重启直奔目标」。
    """
    if not _SEGMENT_NAME.match(name):
        raise NotFoundException("分片不存在")
    grant = await verify_stream_token(token, session_id=session_id)
    if grant is None:
        raise NotFoundException("播放地址无效或已过期")
    manager = get_session_manager()
    session = manager.get(session_id, member_id=grant.member_id)
    if session is None:
        raise NotFoundException("会话不存在或已结束")
    session.touch()
    target = session.directory / name
    if session.segment_plan is not None and name.startswith("seg"):
        ready = await manager.ensure_segment(session, int(name[3:8]))
        if ready is None:
            raise NotFoundException("分片尚未就绪")
        target = ready
    elif name == INIT_NAME:
        # init.mp4 必须等到**写完**，不只是「文件存在」（2026-08-25 真机事故，
        # iPhone 烧录必现「解码失败」）：ffmpeg 起转就创建 init.mp4，但 avio
        # 缓冲让它长期 0 字节——实测软转会话创建后 ~5 秒才落盘，比首个分片
        # 还晚。只等存在就会把 0 字节的 init 以 immutable 缓存喂给 AVPlayer，
        # 整个会话被毒缓存钉死。判完整：非空且两次采样大小不变（moov 一次
        # 写入，落盘即稳定）。
        deadline = time.monotonic() + 15.0
        last_size = -1
        while time.monotonic() < deadline:
            if session.state in ("stopped", "failed"):
                break
            if target.exists():
                size = target.stat().st_size
                if size > 0 and size == last_size:
                    break
                last_size = size
            await asyncio.sleep(0.05)
        if not target.exists() or target.stat().st_size == 0:
            raise NotFoundException("分片尚未就绪")
    elif not target.exists():
        raise NotFoundException("分片尚未就绪")
    # 分片与 init 段在一个会话的生命期内不可变，URL 又含会话 id 与签名 token
    # （换会话必换 URL）——放给浏览器缓存，用户往回拖（back buffer 只留 30
    # 秒，回看必然重新走 HTTP）就变成本地命中，不再打服务端。
    return FileResponse(
        target,
        media_type="video/mp4",
        headers={"Cache-Control": "private, max-age=3600, immutable"},
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


async def _extract_subtitle_until_disconnect(
    request: Request, file: LibraryFile, index: int
):
    """等待字幕抽取，并在浏览器放弃请求时取消底层 ffmpeg。

    内封 PGS/ASS 首次抽取需要通读整个容器，不能把它放进不可取消的线程池。
    ``Request.is_disconnected`` 只负责发现客户端已经放弃，实际进程回收由
    ``extract_embedded_subtitle_async`` 的进程组清理逻辑完成。
    """
    task = asyncio.create_task(extract_embedded_subtitle_async(file, index))
    try:
        while not task.done():
            await asyncio.wait((task,), timeout=0.25)
            if task.done():
                break
            if await request.is_disconnected():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise _SubtitleClientDisconnected
        return await task
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise


@router.get(
    "/files/{file_id}/subtitles",
    summary="旁挂字幕",
    operation_id="playback.file.subtitle",
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_subtitle(
    request: Request,
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
            try:
                ref = await _extract_subtitle_until_disconnect(request, file, index)
            except _SubtitleClientDisconnected:
                # 浏览器切换清晰度、关闭字幕或销毁播放器时可能主动取消请求。
                # 此时连接本身通常已经关闭，204 只用于让 ASGI 边界安静结束，
                # 不能把真实的 asyncio.CancelledError 一并吞掉。
                return Response(status_code=204)
    if ref is None:
        raise NotFoundException("字幕轨不存在或暂不支持在网页端渲染")
    if ref.format == "sup":
        # PGS 位图轨：二进制、可达几十 MB，不进文本管线（那条路要按编码解码
        # 成 UTF-8）。FileResponse 流式发出，前端 libbitsub 边收边解。
        return FileResponse(
            ref.path,
            media_type="application/octet-stream",
            headers={"Cache-Control": "private, max-age=3600"},
        )
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
# 网页播放器：播放页条目信息（§6.10 路由只带 media_item_id）
# ---------------------------------------------------------------------------


async def _visible_item(
    session: AsyncSession, principal: Principal, media_item_id: int
) -> tuple[MediaItem, int]:
    """条目 + 它对当前成员可见的库 id。

    播放路由只带 ``media_item_id``（比库自增 id 稳定，§6.10），库归属在这里
    按可见性解析：取该条目**有台账行落在可见库里**的最小库 id。不可见与不存
    在同样 404——与决策接口同一判据。
    """
    item = await session.get(MediaItem, media_item_id)
    if item is None:
        raise NotFoundException("媒体条目不存在（可能已被删除）")
    visible = await visible_library_ids(session, principal)
    library_ids = sorted(
        {
            lid
            for lid in (
                await session.execute(
                    select(LibraryFile.library_id).where(
                        LibraryFile.media_item_id == media_item_id,
                        LibraryFile.library_id.is_not(None),  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
            if lid is not None and (visible is None or lid in visible)
        }
    )
    if not library_ids:
        raise NotFoundException("没有找到可播放的文件")
    return item, library_ids[0]


@router.get(
    "/items/{media_item_id}",
    response_model=ApiResponse[PlaybackItemView],
    summary="播放页条目信息（标题/海报/库归属）",
    operation_id="playback.item.info",
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_item(
    media_item_id: Annotated[int, Path()],
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackItemView]:
    """播放器要的条目信息，刻意只给几样：标题给顶栏、海报给起播占位、
    library_id 给「退出播放跳回条目页」。不复用条目详情大接口——那份要装配
    NFO/演职员/逐文件规格，播放页用不上还拖慢并行加载。"""
    item, library_id = await _visible_item(session, principal, media_item_id)
    # 海报两层：本地刮削资产（带 mtime 版本戳绕缓存）> TMDB 图床。条目目录
    # 美术图那层不查——它需要装配整个详情 bundle，这里只是起播前的占位画面
    meta_row = await MediaItemRepository(session).get_metadata(media_item_id)
    if meta_row is not None and meta_row.poster_file:
        version = media_scrape.asset_version(meta_row.poster_file)
        poster_url = f"/images/assets/{meta_row.poster_file}?v={version}"
    elif item.poster_path:
        base = get_settings().tmdb_image_base_url.rstrip("/")
        poster_url = f"{base}/w500{item.poster_path}"
    else:
        poster_url = None
    return ok(
        PlaybackItemView(
            media_item_id=item.id,
            library_id=library_id,
            kind=item.kind,
            title=item.title,
            year=item.year,
            poster_url=poster_url,
        )
    )


@router.get(
    "/items/{media_item_id}/episodes",
    response_model=ApiResponse[SeasonEpisodesView],
    summary="播放页一季的分集清单（切集/上一集下一集数据源）",
    operation_id="playback.item.episodes",
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_item_episodes(
    media_item_id: Annotated[int, Path()],
    season_number: Annotated[int, Query(ge=0, description="季号（0=特别篇）")],
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SeasonEpisodesView]:
    """与库详情页的分集接口同一装配器（并集"元数据的集 ∪ 库里实有的集"），
    只是按 media_item_id 定位、库归属服务端解析——播放页不再依赖库 id。"""
    item, library_id = await _visible_item(session, principal, media_item_id)
    rows = list(
        (
            await session.execute(
                select(LibraryFile)
                .where(
                    LibraryFile.library_id == library_id,
                    LibraryFile.media_item_id == media_item_id,
                )
                .order_by(
                    LibraryFile.season_number, LibraryFile.episode_number, LibraryFile.id
                )
            )
        )
        .scalars()
        .all()
    )
    episodes = await build_season_episodes(
        session,
        item,
        rows,
        season_number,
        member_id=principal.member_id if principal.member_id is not None else 0,
    )
    return ok(
        SeasonEpisodesView(
            season_number=season_number,
            episodes=[episode_view(e) for e in episodes],
        )
    )


# ---------------------------------------------------------------------------
# 网页播放器：策略配置（软件转码同意链路 §3.6）。独立设置页已撤（2026-08-25），
# 数字上限改为自动推导（limits.py），端点保留给同意弹窗与 API/CLI 排障用。
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
    """策略当前取值（API/CLI 排障用）。硬件加速一项是实测结果而非配置项。"""
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
    先读全量再回写——那样会把另一个标签页刚保存的值悄悄覆盖回去。
    """
    store = get_setting_store()
    stored = await store.get(PlaybackPolicySetting)
    changes = payload.model_dump(exclude_none=True)
    if changes:
        # 重新构造而不是 model_copy(update=...)：后者跳过校验，字段约束就
        # 形同虚设（写进去的非法值要到消费时才炸）。
        await store.set(PlaybackPolicySetting(**{**stored.model_dump(), **changes}))
    return ok(await _policy_view())


@router.get(
    "/files/{file_id}/fonts",
    response_model=ApiResponse[PlaybackFontsView],
    summary="内嵌字体清单",
    operation_id="playback.file.fonts",
    openapi_extra={"x-cli-hidden": True},
)
async def list_playback_fonts(
    file_id: Annotated[int, Path()],
    token: Annotated[str, Query()],
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackFontsView]:
    """ASS 字幕依赖的内嵌字体。

    番剧的 ASS 把字体作为附件放在 MKV 里，不喂给 JASSUB 就会回退成默认字体，
    排版、字号、描边全走样——这是「ASS 能播」和「ASS 播得对」的差距。

    **懒加载**：抽取要通读整个容器，放在开会话里会拖慢首帧。前端只在真的要
    渲染 ASS 轨时才来拿，抽完进缓存，之后同一部片直接命中。
    """
    grant = await verify_stream_token(token, file_id=file_id)
    if grant is None:
        raise NotFoundException("字体地址无效或已过期")
    file = await session.get(LibraryFile, file_id)
    if file is None:
        raise NotFoundException("文件不存在")
    names = await asyncio.to_thread(extract_embedded_fonts, file)
    return ok(
        PlaybackFontsView(
            fonts=[
                f"/api/v1/playback/files/{file_id}/fonts/{quote(name, safe='')}"
                f"?token={token}"
                for name in names
            ]
        )
    )


@router.get(
    "/files/{file_id}/fonts/{name}",
    summary="内嵌字体文件",
    operation_id="playback.file.font",
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_font(
    file_id: Annotated[int, Path()],
    name: Annotated[str, Path()],
    token: Annotated[str, Query()],
) -> FileResponse:
    """取一个已抽出的字体文件。

    文件名来自附件、也就是媒体文件本体——**不可信输入**。落盘时已经过白名单
    消毒（``safe_font_name``），这里再消毒一次并只在该文件的字体目录里找：
    路径穿越的防线不能只有一道。
    """
    grant = await verify_stream_token(token, file_id=file_id)
    if grant is None:
        raise NotFoundException("字体地址无效或已过期")
    safe = safe_font_name(name)
    if safe is None or safe != name:
        raise NotFoundException("字体不存在")
    target = font_cache_dir(file_id) / safe
    if not target.is_file():
        raise NotFoundException("字体不存在")
    return FileResponse(target, media_type="font/sfnt")


@router.get(
    "/hardware",
    response_model=ApiResponse[HwProbeView],
    summary="硬件加速自检",
    operation_id="playback.hardware.probe",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-hidden": True},
)
async def probe_playback_hardware(
    refresh: Annotated[bool, Query(description="重新检测而不是读缓存")] = False,
) -> ApiResponse[HwProbeView]:
    """逐个后端真跑一秒钟的编码，把失败原因翻成可操作的中文。

    自建软件里硬件加速最大的成本不是写代码，是用户配不对：设备没挂、容器
    用户不在 render 组、驱动版本不够。这些出问题的现象**全都是「转码失败」
    黑盒**——用户既不知道原因也不知道该改什么。这个接口就是为了把黑盒打开。

    `refresh=true` 供设置页的「重新检测」按钮：用户按提示挂上设备后要能立刻
    看到结果，不必重启容器。
    """
    statuses = await probe_backends_async(force=refresh)
    return ok(
        HwProbeView(
            backends=[HwBackendStatusView(**vars(s)) for s in statuses],
            hardware_available=any(s.available for s in statuses),
        )
    )


@router.get(
    "/files/{file_id}/trickplay",
    response_model=ApiResponse[TrickplayView],
    summary="进度条缩略图索引",
    operation_id="playback.file.trickplay",
    openapi_extra={"x-cli-hidden": True},
)
async def get_trickplay_index(
    file_id: Annotated[int, Path()],
    token: Annotated[str, Query()],
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TrickplayView]:
    """拖进度条时的画面预览索引。

    还没生成好就返回 `ready=false`——前端表现为「暂无预览」，不影响播放。
    生成要通读整个容器，是在开会话时后台起的。
    """
    grant = await verify_stream_token(token, file_id=file_id)
    if grant is None:
        raise NotFoundException("预览地址无效或已过期")
    index = trickplay.load_index(file_id)
    if index is None:
        file = await session.get(LibraryFile, file_id)
        if file is not None:
            trickplay.schedule(file)  # 没赶上开会话那次（或那次失败了），补一发
        return ok(TrickplayView(ready=False))
    return ok(
        TrickplayView(
            ready=True,
            interval_ms=index.interval_ms,
            tile_width=index.tile_width,
            tile_height=index.tile_height,
            columns=index.columns,
            rows=index.rows,
            count=index.count,
            sheets=[
                f"/api/v1/playback/files/{file_id}/trickplay/{quote(name, safe='')}"
                f"?token={token}"
                for name in index.sheets
            ],
        )
    )


@router.get(
    "/files/{file_id}/trickplay/{name}",
    summary="进度条缩略图雪碧图",
    operation_id="playback.file.trickplay.sheet",
    openapi_extra={"x-cli-hidden": True},
)
async def get_trickplay_sheet(
    file_id: Annotated[int, Path()],
    name: Annotated[str, Path()],
    token: Annotated[str, Query()],
) -> FileResponse:
    """取一张雪碧图。文件名走白名单——它虽然由服务端生成，但经过了 URL。"""
    grant = await verify_stream_token(token, file_id=file_id)
    if grant is None:
        raise NotFoundException("预览地址无效或已过期")
    if not _TRICKPLAY_SHEET_NAME.match(name):
        raise NotFoundException("预览图不存在")
    target = trickplay.trickplay_dir(file_id) / name
    if not target.is_file():
        raise NotFoundException("预览图不存在")
    return FileResponse(target, media_type="image/jpeg")


@router.post(
    "/client-log",
    response_model=ApiResponse[dict],
    summary="播放器客户端日志",
    operation_id="playback.client-log",
    openapi_extra={"x-cli-hidden": True},
)
async def report_playback_client_log(
    payload: PlaybackClientLogPayload,
    principal: Principal = Depends(require_login),
) -> ApiResponse[dict]:
    """浏览器侧播放现场落服务端日志（只记日志不落库）。

    iPhone 上没有可看的控制台，播放器在哪条路径上、MediaError 报了什么，
    只有让客户端主动报上来才能在服务端日志里与转码时间线对照排障。
    """
    import json as _json

    logger.warning(
        "播放器客户端日志：%s %s",
        payload.event,
        _json.dumps(payload.detail, ensure_ascii=False, default=str)[:2000],
    )
    return ok({"logged": True})


@router.post(
    "/metrics",
    response_model=ApiResponse[dict],
    summary="上报播放质量",
    operation_id="playback.metric.report",
    openapi_extra={"x-cli-hidden": True},
)
async def report_playback_metric(
    payload: PlaybackMetricPayload,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """一次播放结束时上报一行质量快照。

    **只落本地**（硬边界 3）：写进自己的数据库、设置页可看，绝不外发。
    """
    member_id = principal.member_id if principal.member_id is not None else 0
    # 用户端的真实体验落进服务端日志：ttff 是 requestVideoFrameCallback 量出
    # 的「点播放 → 真出画」，与「播放会话就绪」的服务端分段计时对照，差值就
    # 是网络 + 播放器初始化 + 首片下载解码——排查「日志都快但用户说慢」靠它
    logger.info(
        "播放质量上报：档 %s%s · 引擎 %s · 首帧 %s · 卡顿 %d 次/%d 毫秒 · "
        "拖动 %d 次 · 观看 %d 秒（file_id=%s）",
        payload.tier,
        f"（从档 {payload.degraded_from} 降档）" if payload.degraded_from is not None else "",
        payload.engine or "未知",
        f"{payload.ttff_ms} 毫秒" if payload.ttff_ms is not None else "未出画",
        payload.rebuffer_count, payload.rebuffer_ms,
        payload.seek_count, payload.watched_ms // 1000,
        payload.library_file_id,
    )
    await metrics.record(
        session,
        PlaybackMetric(member_id=member_id, **payload.model_dump()),
    )
    # 指标是趋势数据不是台账，攒到几十万行只会拖慢 data 卷上的 SQLite
    if await metrics.count(session) > _METRIC_PURGE_TRIGGER:
        await metrics.purge_older_than(session)
    return ok({"recorded": True})


@router.get(
    "/stats",
    response_model=ApiResponse[PlaybackStatsView],
    summary="播放质量汇总",
    operation_id="playback.stats",
    dependencies=[Depends(require_admin)],
    openapi_extra={"x-cli-hidden": True},
)
async def get_playback_stats(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackStatsView]:
    """最近若干次播放的质量汇总。

    `direct_ratio` 是北极星指标：档 0 + 档 1 的占比，一个数同时代表画质、
    速度和服务器负担，也是「这个软件对我的库适配得好不好」的直观答案。
    """
    stats = await metrics.summarize(session)
    return ok(PlaybackStatsView(**vars(stats)))
