"""AI 字幕生成接口的响应形态（docs/design/subtitle-ai-translate.md §6）。"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class SourceCandidateView(BaseModel):
    kind: str  # embedded/external
    key: str
    language: str | None
    format: str | None
    provenance: str
    excluded: str | None  # 非空 = 被排除原因
    reasons: list[str]  # 打分明细（用户要能看懂"为什么选了这条"）
    selectable: bool  # 文本可直接选；PGS 可选后进入 OCR
    requires_ocr: bool = False


class GenPreviewBlockerView(BaseModel):
    """无法生成时的原因与下一步；前端据此渲染指导弹窗。"""

    code: str
    title: str
    message: str
    suggestions: list[str]


class GenPreviewPendingView(BaseModel):
    """预检还没有结论：内封轨正在后台抽取，稍后重试同一个接口即可。

    与 ``blocker`` 互斥语义：blocker 说「这份片源做不了」，pending 说
    「再等一会儿」。前端据此显示进度文案并轮询，而不是把用户挡在错误里。
    """

    message: str = Field(description="面向用户的等待文案")
    candidate_key: str = Field(description="正在抽取的候选，如 embedded:0")
    retry_after_ms: int = Field(default=2500, ge=0, description="建议的下次轮询间隔")


class PgsOcrLanguageOptionView(BaseModel):
    """当前设备可用的一种 PGS 图片语言。"""

    code: str
    label: str


class PgsConversionView(BaseModel):
    """PGS 自动转 SRT 的候选轨道与当前设备能力。"""

    candidate_key: str  # embedded:<字幕轨序号>
    language: str | None
    available: bool
    engine: str | None
    platform: str
    architecture: str
    cached: bool
    message: str
    suggestions: list[str]
    ocr_language: str | None
    ocr_language_label: str | None
    language_confirmation_required: bool
    language_reason: str
    language_options: list[PgsOcrLanguageOptionView]


class GenPreviewView(BaseModel):
    """发起前的确认素材：选源结果 + 成本估算。"""

    candidates: list[SourceCandidateView]
    chosen_key: str | None  # kind:key；None=无可用参考
    selected_source_key: str | None  # 用户指定或英语优先策略实际选中的候选
    event_count: int
    estimated_tokens: int
    already_generated: bool  # 目标语言的 AI 字幕已存在（再生成=覆盖）
    warnings: list[str]
    pgs_conversion: PgsConversionView | None = None
    blocker: GenPreviewBlockerView | None = None
    output_filename: str | None = None
    #: 非空 = 本次还没有结论，内封轨正在后台抽取，前端轮询等它落缓存。
    pending: GenPreviewPendingView | None = None


class GenStartPayload(BaseModel):
    target_language: str = Field(default="chs", description="目标语言，如 chs / eng")
    secondary_language: str | None = Field(default=None, description="双语第二行语言")
    source_candidate_key: str | None = Field(
        default=None, description="参考字幕标识，如 embedded:1"
    )
    convert_pgs: bool = Field(default=False, description="确认把图片字幕识别为文本")
    pgs_ocr_language: str | None = Field(default=None, description="图片字幕原始语言")


class CalibratePayload(BaseModel):
    filename: str  # 台账里的外挂字幕文件名


class CalibrateResultView(BaseModel):
    ok: bool
    message: str
    scale: float | None
    offset_ms: int | None
    score: float | None
