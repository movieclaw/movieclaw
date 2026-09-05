"""待处理事项 → Agent 诊断工单的快捷入口。

只负责一件事：按待办类型把「发生了什么 / 系统已判定什么 / 已自动做过什么 /
你能做什么」组装成一段文本返回。会话本身仍由前端走 ``session.start`` 创建——
不在这里起会话，避免复制一套会话所有权、附件、递归防护的逻辑。

组装过程会做现场自检（路径 stat、查下载器实时状态），所以是 POST 而不是 GET。
管理员专属：工单内含下载器地址、路径、订阅明细等运维信息。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.diagnosis_handoff import build_handoff_prompt
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/agent-handoff", tags=["session"])


class HandoffRequest(BaseModel):
    kind: Literal["notice", "download", "job"]
    ref: str


class HandoffPromptView(BaseModel):
    title: str
    prompt: str


@router.post(
    "",
    response_model=ApiResponse[HandoffPromptView],
    summary="为一条待处理事项生成交给 Agent 的诊断工单",
    operation_id="session.handoff.prompt",
    openapi_extra={"x-cli-hidden": True},
)
async def build_prompt(
    payload: HandoffRequest, session: AsyncSession = Depends(get_session)
) -> ApiResponse[HandoffPromptView]:
    """``kind``：notice（ref=告警 id）/ download（ref=info_hash）/ job（ref=任务 id）。

    返回的 ``prompt`` 直接作为新会话的首条用户消息提交给 ``session.start``。"""
    result = await build_handoff_prompt(session, payload.kind, payload.ref)
    return ok(HandoffPromptView(title=result.title, prompt=result.prompt))
