"""Agent 执行过程的事件协议 —— 前后端之间的「执行进度语言」。

设计原则（对齐 pi 的 agent 事件思路，做适配简化）：

1. 事件类型面向「渲染语义」而非模型协议：前端拿到事件就知道往哪个区域
   画什么，不需要理解底层 LLM 流的细节；
2. 增量事件（*_delta）负责打字机效果，结束事件（agent_done）带完整结果，
   前端断线重连/中途加入也能靠终态恢复；
3. 事件集为未来 agent loop 预留：tool_call / tool_result 是循环的基础，
   骨架版只上报 tool_call 不执行，协议不用改。

事件序列（agent loop 版，每一步 = 一次模型调用）：
    agent_start → [ (thinking_delta|text_delta)*
                    → (tool_call_start → tool_call_delta* → tool_call)*
                    → tool_result* ]×N → agent_done
    工具调用的三段式：名称确定即发 tool_call_start，参数逐片发 tool_call_delta，
    参数完整（JSON 解析完毕）后发 tool_call —— 前端从名称确定的一刻起就能展示
    进度。tool_result 在本步流结束后逐个执行时发出。
    模型不再发起工具调用即为 STOP，循环结束；任何阶段出错 → agent_error 收尾
    （不抛异常，与 LLM 流层同约定）
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from movieclaw_llm import ChatMessage, ContentPart, ModelSettings, TokenUsage, ToolCall

AgentEventType = Literal[
    "agent_start",  # 运行开始：run_id / 实际路由到的 provider 与 model
    "thinking_delta",  # 思维链增量（delta 字段）
    "text_delta",  # 正文文本增量（delta 字段）
    "tool_call_start",  # 工具名称已确定，参数开始生成（tool_call 字段：仅 id/name）
    "tool_call_delta",  # 工具参数 JSON 增量（delta 字段；tool_call_id 标识归属）
    "tool_call",  # 模型发起一次工具调用（参数已解析完整）
    "tool_result",  # 一次工具执行完成（tool_result 字段：结果/是否失败/耗时）
    "context_compacted",  # 上下文已压缩（compaction 字段：摘要与前后 token 数）
    "agent_done",  # 正常结束：完整结果与用量（result 字段）
    "agent_error",  # 出错结束：中文错误说明（error 字段）
    "agent_cancelled",  # 用户主动取消：运行已停止，但不视为执行失败
]


class AgentStartParams(BaseModel):
    """启动一次 Agent 运行的完整入参。

    工具集不在此处——工具属于服务端编排职责，注册在 AgentRunner 上。
    """

    #: 用户的任务描述（作为本轮 user 消息）。日常是纯文本；带图片时是
    #: text/image 内容块列表（图文混合，由 API 编排层组装并水合）
    input: str | list[ContentPart]
    #: 多轮历史（可选）：追加在 system 之后、本轮 input 之前
    history: list[ChatMessage] = Field(default_factory=list)
    #: 模型引用（movieclaw_llm 路由写法）；空串 = 默认供应商的默认模型
    model: str = ""
    #: 覆盖默认系统提示词（None 用 prompts.build_system_prompt()）
    system_prompt: str | None = None
    settings: ModelSettings = Field(default_factory=ModelSettings)


class AgentToolResult(BaseModel):
    """一次工具执行的回执（tool_result 事件的载荷）。

    output 是喂回模型的文本（事件里截断到 2000 字，完整版进对话上下文）。
    """

    tool_call_id: str
    name: str
    output: str
    is_error: bool = False
    elapsed_ms: int = 0


class AgentCompaction(BaseModel):
    """context_compacted 事件载荷。

    只带展示所需的摘要与前后 token 估算；重建历史本体不进事件流
    （可达几十 KB，且属于转录持久化的职责，不是渲染数据）。
    """

    summary: str
    tokens_before: int
    tokens_after: int


class AgentDone(BaseModel):
    """一次运行的终态汇总（agent_done 事件的载荷）。

    text/thinking 是**最后一步**的产出（loop 的最终答复）；
    usage 是全部步骤的累计；steps 是模型调用次数。
    """

    text: str | None = None
    thinking: str | None = None
    finish_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    steps: int = 1
    model: str = ""
    provider: str = ""
    elapsed_ms: int = 0


class AgentEvent(BaseModel):
    """Agent 事件：type 决定哪些字段有值（见各字段注释）。

    作为 SSE 载荷时，type 同时用作 SSE 的 event 名。
    """

    type: AgentEventType
    #: 本次运行的唯一 id，一次 start 产生的所有事件共享同一个值
    run_id: str
    #: thinking_delta / text_delta / tool_call_delta 的增量文本
    delta: str | None = None
    #: tool_call_start：仅含 id/name 的调用；tool_call：完整调用（参数已尽力解析）
    tool_call: ToolCall | None = None
    #: tool_call_delta 事件：增量所属的工具调用 id（对应 tool_call_start 的 id）
    tool_call_id: str | None = None
    #: tool_result 事件：工具执行回执
    tool_result: AgentToolResult | None = None
    #: context_compacted 事件：压缩回执（摘要与前后 token 数）
    compaction: AgentCompaction | None = None
    #: agent_start 事件：实际路由到的供应商实例名与模型 id
    provider: str | None = None
    model: str | None = None
    #: agent_done 事件的终态载荷
    result: AgentDone | None = None
    #: agent_error 事件的中文错误说明
    error: str | None = None
