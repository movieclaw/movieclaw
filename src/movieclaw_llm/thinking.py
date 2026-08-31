"""思维链强度档位 → 各端点方言的翻译（docs/design/agent-thinking-level.md）。

设计定案（调研 apache/maka 后照抄的三条 + 一条自创）：

1. **回落默认，不就近取整**：模型菜单不含所选档位时当作「默认」处理——
   成本不可预期（中悄悄变最高）、语义不可解释，maka 明确拒绝就近映射；
2. **fail-closed**：未声明 thinking_control 的模型没有菜单，档位永不落请求；
3. **off 只在有真关闭协议时存在**（supports_off 声明），绝不发明线协议；
4. budget 制的分段比例（低 25% / 中 50% / 高 100%）是本项目自创约定
   （maka 各协议均为 effort 枚举制，无预算制先例），常量在 models.py。

翻译结果是请求体顶层字段的 dict，由协议层并入 extra_body 通道发送；
与用户显式 extra_body 冲突时用户值优先（逃生舱语义不变）。
"""

from __future__ import annotations

import logging

from movieclaw_llm.models import BUDGET_LEVEL_RATIOS, ModelInfo, ThinkingLevel

logger = logging.getLogger(__name__)


def resolve_thinking_level(
    level: ThinkingLevel | None, model: ModelInfo | None
) -> ThinkingLevel | None:
    """丢弃语义门（maka 的 resolveThinkingLevel 同款）。

    返回档位仅当模型菜单包含它；否则返回 None = 回落模型默认。目录外模型
    （model=None）没有任何声明，同样回落。
    """
    if level is None or model is None:
        return None
    return level if level in model.thinking_levels else None


def translate_thinking_level(level: ThinkingLevel, model: ModelInfo) -> dict:
    """把已通过 resolve 的档位翻译成该模型方言的请求体片段。

    调用方必须先过 resolve_thinking_level——本函数假定档位在菜单内
    （因此 thinking_control 必非 None、budget 制必有预算上限）。
    """
    control = model.thinking_control
    assert control is not None, "translate 之前必须先 resolve"
    if control.kind == "effort":
        # 档位原词直传；off 的关闭编码是 reasoning_effort="none"
        return {"reasoning_effort": "none" if level == "off" else level}
    if control.kind == "budget":
        if level == "off":
            return {"enable_thinking": False}
        budget = int((model.max_thinking_tokens or 0) * BUDGET_LEVEL_RATIOS[level])
        return {"enable_thinking": True, "thinking_budget": budget}
    # toggle：菜单只有「关」，开 = 默认（不发参数，到不了这里）
    return {"thinking": {"type": "disabled"}}


def thinking_body_fragment(
    level: ThinkingLevel | None, model: ModelInfo | None, model_id: str
) -> dict:
    """resolve + translate 一步到位；回落时打中文 info 日志，返回空 dict。"""
    if level is None:
        return {}
    resolved = resolve_thinking_level(level, model)
    if resolved is None:
        logger.info(
            "模型 %s 的思考档位菜单不包含「%s」，本次请求回落为模型默认强度", model_id, level
        )
        return {}
    assert model is not None  # resolve 非 None 蕴含 model 非 None
    return translate_thinking_level(resolved, model)
