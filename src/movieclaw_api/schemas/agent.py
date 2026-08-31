from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from movieclaw_api.schemas.base import BaseModel
from movieclaw_db.models import AgentSession
from movieclaw_db.repositories.agent_session_repo import is_running
from movieclaw_llm import ChatMessage, ContentPart, TokenUsage, ToolCall
from movieclaw_llm.models import THINKING_LEVELS

#: 思维链档位的对外取值：词汇表 + 「default」（显式清回模型默认）。
#: None/未传 = 沿用会话最近一条 user 消息的档位。
_THINKING_LEVEL_CHOICES = {*THINKING_LEVELS, "default"}


def _validate_thinking_level(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value not in _THINKING_LEVEL_CHOICES:
        raise ValueError(
            f"思维链档位「{value}」不在支持的取值里："
            f"{', '.join(sorted(_THINKING_LEVEL_CHOICES))}"
        )
    return value


def _iso_utc(value: datetime | None) -> str | None:
    """naive UTC → 带 +00:00 的 ISO 串（项目时间约定，见 base.utcnow）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class SessionStartPayload(BaseModel):
    """提交一条用户消息；有 session_id 时继续已有会话，否则开始新会话。

    图片以 attachment_id 引用（先经 ``session.attachments.upload`` 上传）；
    协议永不接受调用方直接提交内容块——ContentPart 由服务端组装，杜绝注入
    任意 url 或内联 base64（docs/design/agent-image-input.md §8.1）。
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        default="", max_length=4000, description="用户消息正文；带图片时允许为空"
    )
    attachments: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="随消息发送的图片附件编号列表（上传接口返回的 attachment_id）",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已有会话编号；留空时创建新会话",
    )
    model: str = Field(default="", description="模型 ID；留空时使用默认供应商的默认模型")
    thinking_level: str | None = Field(
        default=None,
        description=(
            "思维链强度档位（off/minimal/low/medium/high/xhigh/max），"
            "传 default 显式清回模型默认；不传时沿用会话最近一条消息的档位"
        ),
    )

    @field_validator("content", "session_id", "model", mode="before")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("thinking_level")
    @classmethod
    def _thinking_level_vocab(cls, value: str | None) -> str | None:
        return _validate_thinking_level(value)

    @model_validator(mode="after")
    def _require_content_or_attachments(self) -> SessionStartPayload:
        if not self.content and not self.attachments:
            raise ValueError("消息正文与图片附件不能同时为空")
        return self


class AttachmentUploadView(BaseModel):
    """图片附件上传成功的回执；attachment_id 随后交给 session.start 引用。"""

    attachment_id: str = Field(description="附件稳定编号，绑定会话前 24 小时内有效")
    name: str = Field(description="原始文件名（服务端截断到 120 字符）")
    width: int = Field(description="图片像素宽度")
    height: int = Field(description="图片像素高度")
    bytes: int = Field(description="原始字节数")


class SkillView(BaseModel):
    """一个可显式调用的 Agent 技能（composer 加号菜单的数据源）。"""

    name: str = Field(description="技能名，也是 /skill:名字 占位符里的名字")
    description: str = Field(description="技能用途描述（菜单项的提示文案）")
    scope: str = Field(description="来源层级：builtin=随产品内置，user=用户技能目录")


class SessionMessageAcceptedView(BaseModel):
    """用户消息已持久化并开始处理的回执。"""

    session_id: str = Field(description="会话稳定编号，后续 session 操作都使用它")
    message_id: str = Field(
        description="本次新建的 user message 稳定编号，可作为 retry 的定位锚点"
    )


class SessionRetryPayload(BaseModel):
    """从指定用户消息处重试，可用新内容替换原问题。"""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(
        min_length=1,
        max_length=64,
        description="要重新提问的 user message 编号",
    )
    content: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
        description="替换后的用户消息正文；留空时原文重试",
    )
    attachments: list[str] | None = Field(
        default=None,
        max_length=4,
        description=(
            "重试消息携带的图片附件编号：不传（null）沿用原消息的附件，"
            "空数组显式去掉图片，非空数组替换为新附件"
        ),
    )
    model: str = Field(default="", description="模型 ID；留空时使用默认供应商的默认模型")
    thinking_level: str | None = Field(
        default=None,
        description="思维链强度档位；传 default 清回模型默认，不传沿用原消息的档位",
    )

    @field_validator("message_id", "content", "model", mode="before")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("thinking_level")
    @classmethod
    def _thinking_level_vocab(cls, value: str | None) -> str | None:
        return _validate_thinking_level(value)


class SessionRenamePayload(BaseModel):
    """重命名会话的请求体。

    标题只存索引表（元数据不入转录文件，见 agent_sessions 模块的
    append-only 约定）；索引整体重建时非空标题会被保留。
    """

    title: str = Field(min_length=1, max_length=80, description="新的会话标题")

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class SessionSummary(BaseModel):
    """会话列表项（索引表投影 + 派生的运行状态）。"""

    id: str = Field(description="会话稳定编号（session_id）")
    title: str | None = Field(description="会话标题；未命名时为空")
    last_prompt: str | None = Field(description="最近一条用户消息的短预览")
    entry_count: int = Field(description="完整轨迹的 entry 数量，包含 message 与 compaction")
    running: bool = Field(description="当前是否有仍在处理的用户消息")
    created_at: datetime = Field(description="会话创建时间（ISO 8601 UTC）")
    updated_at: datetime = Field(description="会话最近活动时间（ISO 8601 UTC）")

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _iso_utc(value)

    @classmethod
    def from_model(cls, row: AgentSession) -> SessionSummary:
        running = is_running(row)
        return cls(
            id=row.id,
            title=row.title,
            last_prompt=row.last_prompt,
            entry_count=row.entry_count,
            running=running,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SessionMessageView(BaseModel):
    """会话中的一条协议消息。

    user 是用户输入；assistant 是模型输出；tool 是工具执行回执；system 通常
    只在运行时组装而不写入 transcript，但保留在完整消息定义中。
    """

    role: Literal["system", "user", "assistant", "tool"] = Field(
        description="消息角色：用户输入、模型输出、工具回执或系统消息"
    )
    content: str | list[ContentPart] = Field(
        default="", description="消息正文；可能是纯文本或 text/thinking/image 内容块"
    )
    tool_calls: list[ToolCall] | None = Field(
        default=None, description="assistant 发起的工具调用；其他角色通常为空"
    )
    tool_call_id: str | None = Field(
        default=None, description="tool 消息所回应的工具调用编号"
    )
    name: str | None = Field(default=None, description="tool 消息对应的工具名称")

    @classmethod
    def from_model(cls, message: ChatMessage) -> SessionMessageView:
        dumped = message.model_dump()
        # 防御性剔除图片字节：落库脱水（store 写入口）之外的第二道闸。
        # 老数据或异常路径里若混入内联 base64，也不能把几 MB 推给前端——
        # 引用型图片块的展示只依赖 attachment_id（下载接口取图）。
        content = dumped.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image" and part.get("attachment_id"):
                    part["data"] = None
        return cls.model_validate(dumped)


class SessionMessageEntryView(BaseModel):
    """完整轨迹中的一条消息记录；message_id 是可寻址的持久化身份。"""

    type: Literal["message"] = Field(default="message", description="轨迹类型判别值")
    message_id: str = Field(description="这条持久化消息的稳定编号")
    parent_id: str | None = Field(default=None, description="上一条轨迹 entry 的编号")
    timestamp: str = Field(description="消息写入时间（ISO 8601 UTC）")
    message: SessionMessageView = Field(description="LLM 协议格式的完整消息")
    model: str | None = Field(default=None, description="assistant 消息实际使用的模型")
    usage: TokenUsage | None = Field(default=None, description="assistant 消息的 token 用量")
    finish_reason: str | None = Field(
        default=None, description="assistant 消息的模型结束原因"
    )
    thinking_level: str | None = Field(
        default=None, description="user 消息生效的思维链档位；null = 模型默认"
    )


class SessionCompactionEntryView(BaseModel):
    """完整轨迹中的上下文压缩记录，不属于 message。"""

    type: Literal["compaction"] = Field(default="compaction", description="轨迹类型判别值")
    compaction_id: str = Field(description="这条上下文压缩记录的稳定编号")
    parent_id: str | None = Field(default=None, description="上一条轨迹 entry 的编号")
    timestamp: str = Field(description="压缩记录写入时间（ISO 8601 UTC）")
    summary: str = Field(description="模型生成的历史交接摘要")
    replacement_history: list[SessionMessageView] = Field(
        description="后续模型请求实际采用的压缩后历史，不包含 system 消息"
    )
    tokens_before: int | None = Field(default=None, description="压缩前的估算 token 数")
    tokens_after: int | None = Field(default=None, description="压缩后的估算 token 数")


class SessionHandoffEntryView(BaseModel):
    """新会话的来源快照；replacement_history 是后续请求实际继承的上下文。"""

    type: Literal["handoff"] = Field(default="handoff", description="轨迹类型判别值")
    handoff_id: str = Field(description="交接记录的稳定编号")
    parent_id: str | None = Field(default=None, description="上一条轨迹 entry 的编号")
    timestamp: str = Field(description="创建快照的时间（ISO 8601 UTC）")
    source_session_id: str = Field(description="源会话稳定编号")
    source_leaf_id: str | None = Field(default=None, description="源会话快照时的链尾编号")
    source_title: str | None = Field(default=None, description="快照时的源会话标题")
    replacement_history: list[SessionMessageView] = Field(
        description=(
            "新会话后续模型请求继承的完整上下文（不含 system 消息）。仅在 "
            "session.fork 的创建响应中携带；session.get-transcript 固定返回空列表"
            "——快照已持久化在服务端，每次读取会话都重复下发整份历史太浪费"
        )
    )


SessionTranscriptEntryView = Annotated[
    SessionMessageEntryView | SessionCompactionEntryView | SessionHandoffEntryView,
    Field(discriminator="type"),
]


class SessionTranscriptView(BaseModel):
    """会话详情：列表项字段 + 完整轨迹回放。

    message / compaction / handoff 是三种明确的 entry；各自使用自己的稳定
    编号，parent_id 只描述同一会话内跨 entry 的线性轨迹链。
    """

    session: SessionSummary = Field(description="会话摘要与当前运行状态")
    entries: list[SessionTranscriptEntryView] = Field(
        description="按写入顺序排列的完整 message/compaction 轨迹"
    )


class SessionContextCompactionView(BaseModel):
    """手动压缩的回执：摘要与前后 token 估算（bytes/4 启发式，非精确值）。"""

    summary: str = Field(description="模型生成的历史交接摘要")
    tokens_before: int = Field(description="压缩前的估算 token 数")
    tokens_after: int = Field(description="压缩后的估算 token 数")
    compaction_id: str = Field(description="写入完整轨迹的 compaction 记录编号")
