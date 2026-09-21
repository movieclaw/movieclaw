"""AI 字幕生成接口（docs/design/subtitle-ai-translate.md §6，G1 手动触发）。

挂管理区：翻译消费 LLM 配额（真金白银），G1 只对管理员开放；成员开放
随 G2 的额度护栏一起评估。目标语言用文件名语言 token（默认 chs），
产物 sidecar 落视频同目录，任务结束台账即时刷新、播放器立刻可选。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_admin
from movieclaw_api.api.routes.jobs import job_view
from movieclaw_api.schemas.jobs import JobView
from movieclaw_api.schemas.response import ApiResponse
from movieclaw_api.schemas.subtitle_gen import (
    CalibratePayload,
    CalibrateResultView,
    GenPreviewBlockerView,
    GenPreviewPendingView,
    GenPreviewView,
    GenStartPayload,
    PgsConversionView,
    PgsOcrLanguageOptionView,
    SourceCandidateView,
)
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.subtitle_gen import tasks as gen_tasks
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/libraries/files", tags=["subtitle-gen"])


def _preview_view(pv: gen_tasks.Preview) -> GenPreviewView:
    return GenPreviewView(
        candidates=[
            SourceCandidateView(
                kind=c.candidate.kind,
                key=c.candidate.key,
                language=c.candidate.language,
                format=c.candidate.format,
                provenance=c.candidate.provenance,
                excluded=c.excluded,
                reasons=c.reasons,
                selectable=gen_tasks.candidate_selectable(c),
                requires_ocr=c.exclusion_code == "pgs",
            )
            for c in pv.candidates
        ],
        chosen_key=(f"{pv.chosen.candidate.kind}:{pv.chosen.candidate.key}" if pv.chosen else None),
        selected_source_key=pv.selected_source_key,
        event_count=pv.event_count,
        estimated_tokens=pv.estimated_tokens,
        already_generated=pv.already_generated,
        warnings=pv.warnings,
        pgs_conversion=(
            PgsConversionView(
                candidate_key=(
                    f"{pv.pgs_conversion.candidate.candidate.kind}:"
                    f"{pv.pgs_conversion.candidate.candidate.key}"
                ),
                language=pv.pgs_conversion.candidate.candidate.language,
                available=pv.pgs_conversion.capability.available,
                engine=pv.pgs_conversion.capability.engine,
                platform=pv.pgs_conversion.capability.platform,
                architecture=pv.pgs_conversion.capability.architecture,
                cached=pv.pgs_conversion.capability.cached,
                message=pv.pgs_conversion.capability.message,
                suggestions=list(pv.pgs_conversion.capability.suggestions),
                ocr_language=pv.pgs_conversion.language.code,
                ocr_language_label=pv.pgs_conversion.language.label,
                language_confirmation_required=(pv.pgs_conversion.language.confirmation_required),
                language_reason=pv.pgs_conversion.language.reason,
                language_options=[
                    PgsOcrLanguageOptionView(code=code, label=label)
                    for code, label in pv.pgs_conversion.language_options
                ],
            )
            if pv.pgs_conversion
            else None
        ),
        blocker=(
            GenPreviewBlockerView(
                code=pv.blocker.code,
                title=pv.blocker.title,
                message=pv.blocker.message,
                suggestions=pv.blocker.suggestions,
            )
            if pv.blocker
            else None
        ),
        output_filename=pv.output_filename,
        pending=(
            GenPreviewPendingView(
                message=pv.pending.message,
                candidate_key=pv.pending.candidate_key,
            )
            if pv.pending
            else None
        ),
    )


def _request_origin(principal: Principal, client_name: str | None) -> str:
    if principal.kind == "agent":
        return "agent"
    if client_name and client_name.lower() in {"web", "cli", "agent", "scheduler"}:
        return client_name.lower()
    return "cli" if principal.kind == "pat" else "web"


@router.get(
    "/{file_id}/subtitles/generation-preview",
    response_model=ApiResponse[GenPreviewView],
    summary="AI 字幕生成预检：选源结果与成本估算（发起确认框素材）",
    operation_id="library.subtitles.preview-generation",
)
async def gen_preview(
    file_id: int,
    target_language: str = "chs",
    secondary_language: str | None = None,
    source_candidate_key: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[GenPreviewView]:
    """内封轨首次预检不在这里等 ffmpeg（issue #432）。

    抽取要通读整个容器，16 GB 的 MKV 是 80 秒级，而 iPhone Safari 约 60 秒
    就掐断请求、对话框显示浏览器原话 ``Load failed``——用户以为文件坏了，
    服务端却照跑到底。``wait=False`` 让预检立刻返回 ``pending``，抽取转后台
    单飞进行，前端按 ``retry_after_ms`` 轮询同一个接口拿最终结论。
    """
    pv = await gen_tasks.preview(
        session,
        file_id,
        target_language,
        secondary_language=secondary_language,
        source_candidate_key=source_candidate_key,
        wait=False,
    )
    return ApiResponse(data=_preview_view(pv))


@router.post(
    "/{file_id}/subtitles/generations",
    response_model=ApiResponse[JobView],
    summary="发起 AI 字幕生成（后台执行；同文件单飞）",
    operation_id="library.subtitles.generate",
    openapi_extra={
        "x-cli-job": {"id_path": "id", "wait_op": "jobs.wait"},
    },
    status_code=202,
)
async def gen_start(
    file_id: int,
    payload: GenStartPayload,
    response: Response,
    principal: Principal = Depends(require_admin),
    client_name: str | None = Header(default=None, alias="X-MovieClaw-Client"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[JobView]:
    _preview, created = await gen_tasks.enqueue_generation_job(
        session,
        file_id,
        payload.target_language,
        secondary_language=payload.secondary_language,
        source_candidate_key=payload.source_candidate_key,
        convert_pgs=payload.convert_pgs,
        pgs_ocr_language=payload.pgs_ocr_language,
        actor_kind=principal.kind,
        actor_name=principal.name,
        actor_id=str(principal.member_id) if principal.member_id is not None else None,
        origin=_request_origin(principal, client_name),
    )
    response.headers["Location"] = f"/api/v1/jobs/{created.job.id}"
    return ApiResponse(
        data=await job_view(session, created.job),
        message="任务已入队" if created.created else "相同任务已在进行中",
    )


@router.post(
    "/{file_id}/subtitles/timing-calibration",
    response_model=ApiResponse[CalibrateResultView],
    summary="一键校准外挂字幕时间轴（音轨互相关；strm 退字幕对字幕）",
    operation_id="library.subtitles.calibrate-timing",
)
async def subtitle_calibrate(
    file_id: int,
    payload: CalibratePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[CalibrateResultView]:
    """独立于 AI 翻译的零成本工具（subtitle-ai-translate.md §5.4）：用户
    手工下载的不同步字幕就地修正，校准后覆盖写回并即时刷新台账。"""
    result = await gen_tasks.calibrate_external_subtitle(session, file_id, payload.filename)
    return ApiResponse(
        data=CalibrateResultView(
            ok=result.ok,
            message=result.message,
            scale=result.scale,
            offset_ms=result.offset_ms,
            score=result.score,
        ),
        message=result.message,
    )
