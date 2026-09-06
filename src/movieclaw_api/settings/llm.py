"""AI 设定配置域（「设置 → AI 设定」）：各用途的默认模型。

接入与设定是两件事：供应商实例（llm_provider 表）只回答「怎么连上」，
这里回答「什么场景用哪个模型」。值是对话框同款的模型引用（裸模型 id，或
同 id 在多个实例时的「实例名/模型id」，见 services.llm_config 模块说明）；
None = 未设置，运行时按「第一个接入实例的连接测试模型」兜底，设置页会把
兜底结果展示出来。

新增用途（如内容识别）时在这里加字段，并在 services.llm_config 的
effective_defaults 里补上兜底与解析。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

LLM_DEFAULTS_NAMESPACE = "llm.defaults"


@register_setting(namespace=LLM_DEFAULTS_NAMESPACE, title="AI 设定")
class LlmDefaultsSetting(SettingSchema):
    """各用途的默认模型引用；None = 未设置（运行时兜底）。"""

    agent_model: str | None = Field(
        default=None,
        description="智能体默认模型：对话框未选模型、IM 通道对话、CLI 不带 --model 时使用",
    )
    subtitle_model: str | None = Field(
        default=None, description="字幕处理默认模型：字幕翻译 / 生成任务使用"
    )
