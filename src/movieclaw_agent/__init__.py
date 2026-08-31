"""movieclaw_agent —— Agent 执行层。

分层定位：movieclaw_llm 负责「怎么调模型」，本库负责「一次任务怎么跑」——
把模型的流式输出翻译成统一的 Agent 事件流，供 API 层转成 SSE、前端逐事件
渲染。当前是骨架版本：单次模型调用；后续的 agent loop（工具执行→结果回喂
→再调用）在 AgentRunner 内扩展，事件协议保持不变。
"""

from movieclaw_agent.compaction import CompactionResult, compact
from movieclaw_agent.events import (
    AgentCompaction,
    AgentDone,
    AgentEvent,
    AgentEventType,
    AgentStartParams,
    AgentToolResult,
)
from movieclaw_agent.prompts import SUMMARY_PREFIX, SYSTEM_PROMPT, build_system_prompt
from movieclaw_agent.runner import AgentRunner
from movieclaw_agent.skills import (
    Skill,
    build_skills_fragment,
    discover_skills,
    expand_skill_invocations,
    strip_skill_blocks,
)
from movieclaw_agent.toolkit import AgentTool

__all__ = [
    "SUMMARY_PREFIX",
    "SYSTEM_PROMPT",
    "AgentCompaction",
    "AgentDone",
    "AgentEvent",
    "AgentEventType",
    "AgentRunner",
    "AgentStartParams",
    "AgentTool",
    "AgentToolResult",
    "CompactionResult",
    "Skill",
    "build_skills_fragment",
    "build_system_prompt",
    "compact",
    "discover_skills",
    "expand_skill_invocations",
    "strip_skill_blocks",
]
