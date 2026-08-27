"""工具调用参数校验 —— agent loop 的韧性地基。

模型输出的工具参数不可信（幻觉字段、类型错误、残缺 JSON）。校验失败
不抛异常中断 loop，而是返回一段中文错误描述，调用方应把它作为
tool 结果回喂给模型，让模型自行修正后重试（pi-ai 的 validateToolCall
同款思路）。
"""

from __future__ import annotations

import logging

import jsonschema

from movieclaw_llm.models import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

#: gpt-oss 系模型 Harmony 输出格式的特殊标记。这些标记本应由推理后端
#: （vLLM / Ollama / llama.cpp 等）在解析阶段消化掉；一旦泄漏到工具名或
#: 正文里，说明后端没有正确配置 gpt-oss 的输出解析（如未启用对应的
#: 工具调用解析器、chat 模板不对），属于部署问题，应用层无法修复。
_HARMONY_MARKERS = (
    "<|channel|>",
    "<|message|>",
    "<|call|>",
    "<|constrain|>",
    "<|start|>",
    "<|end|>",
    "<|return|>",
)

#: 引导用户排查部署的提示。既打进日志，也回喂给模型/展示在会话里，
#: 让非开发者部署时也能定位到是推理后端的问题而不是 movieclaw 的问题。
HARMONY_RESIDUE_HINT = (
    "检测到 gpt-oss Harmony 格式残留标记（如 <|channel|>）。"
    "这通常说明推理后端未正确解析 gpt-oss 的输出，请检查部署配置："
    "vLLM 需启用 gpt-oss 的工具调用解析器，Ollama / llama.cpp 需升级到"
    "支持该模型的版本并使用正确的 chat 模板。此问题无法在 movieclaw 侧修复，"
    "修复前建议换用其他模型。"
)


def has_harmony_residue(text: str | None) -> bool:
    """判断文本中是否泄漏了 Harmony 特殊标记（推理后端解析失败的特征）。"""
    return bool(text) and any(marker in text for marker in _HARMONY_MARKERS)


def validate_tool_call(
    tools: list[ToolDefinition], tool_call: ToolCall
) -> tuple[dict | None, str | None]:
    """校验模型发起的工具调用。

    返回 ``(参数, None)`` 表示通过；``(None, 错误描述)`` 表示失败，
    错误描述应作为 tool 消息回喂模型。
    """
    tool = next((t for t in tools if t.name == tool_call.name), None)
    if tool is None:
        names = ", ".join(t.name for t in tools) or "（无）"
        if has_harmony_residue(tool_call.name):
            logger.warning(
                "模型返回的工具名 %r 含 Harmony 格式残留。%s",
                tool_call.name,
                HARMONY_RESIDUE_HINT,
            )
            return None, (
                f"工具「{tool_call.name}」不存在。{HARMONY_RESIDUE_HINT}（可用工具：{names}）"
            )
        return None, f"工具「{tool_call.name}」不存在，可用工具：{names}"
    if tool_call.parse_error:
        return None, f"工具参数解析失败：{tool_call.parse_error}，请重新以合法 JSON 输出参数"
    try:
        jsonschema.validate(tool_call.arguments, tool.parameters)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "(根)"
        return None, f"工具参数不符合定义（字段 {path}）：{exc.message}，请修正后重试"
    return tool_call.arguments, None
