from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin
from movieclaw_db.models.site_credential import ConfigStatus


class LlmProvider(TimestampMixin, table=True):
    """LLM 供应商实例配置表：**多实例**，一行即一个接入的账号/端点。

    与下载器一样可以配多个：官方 OpenAI、一家中转、一台自建 vLLM 可以同时
    接入，接入后该实例目录里的全部模型都可在对话框里选用。路由由
    movieclaw_llm.LlmRouter 负责，本表只负责持久化实例配置。

    - ``name`` 是实例名，全局唯一、不能含斜杠：对话框选模型时若同一模型 id
      出现在多个实例，前端用「实例名/模型id」精确路由，实例名就是路由键；
    - ``default_model`` 是连接测试发 ping 用的模型（目录里第一个，服务层
      自动填）。「哪个模型是默认」不在本表——智能体 / 字幕处理各自的默认
      模型是 AI 设定（settings/llm.py 的 LlmDefaultsSetting），接入与设定
      是两件事；
    - ``provider_type`` 关联 movieclaw_llm 的供应商预设（openai / bailian /
      openai_compat …），base_url 留空时用预设默认端点。

    验证状态机与站点/下载器一致（复用 ConfigStatus）：保存后置 PENDING，
    后台用 default_model 发一次最小对话验证，成功 ACTIVE / 失败 FAILED。

    安全：``api_key`` 经 SecretBox 加密后落库（``enc::`` 前缀密文），
    加解密统一在 Repository 层完成。
    """

    __tablename__ = "llm_provider"

    id: int | None = Field(default=None, primary_key=True)
    # 实例名：用户给这个接入起的名字（如「官方 OpenAI」「家里的 vLLM」），路由键
    name: str = Field(unique=True, index=True, description="实例名（全局唯一，不含斜杠）")
    provider_type: str = Field(description="供应商预设 id：openai / bailian / openai_compat")
    base_url: str | None = Field(default=None, description="API 端点（留空用预设默认）")
    api_key: str = Field(description="API Key（SecretBox 加密密文）")
    # 自定义 User-Agent：留空用 openai SDK 自带 UA。自建网关/反代常按 UA
    # 放行或限流，官方渠道用不到，故仅在可自填端点的供应商上开放配置。
    user_agent: str | None = Field(default=None, description="自定义 User-Agent（留空用 SDK 默认）")
    # 连接测试用的模型：目录里第一个，服务层自动填；也是 AI 设定未配置时的兜底
    default_model: str = Field(description="连接测试用的模型 id，如 qwen-plus")

    # 验证状态机（语义见 ConfigStatus）
    status: ConfigStatus = Field(default=ConfigStatus.PENDING, description="连接验证状态")
    last_error: str | None = Field(default=None, description="最近一次测试失败原因（中文）")
    last_checked_at: datetime | None = Field(default=None, description="最近一次测试时间")
    # 最近一次验证成功时端点上报的可用模型列表（JSON 数组），供设置页选择
    available_models: list[str] | None = Field(
        default=None, sa_column=Column(JSON), description="端点上报的可用模型列表"
    )
    # 用户补录的自定义模型及其参数（JSON 数组，元素结构见 movieclaw_llm.ModelInfo）。
    # 自定义端点（openai_compat）没有内置目录，模型的上下文/输出上限/思考预算等
    # 参数全靠这里——只存裸模型 id 不够，agent 做预算决策需要完整元数据。
    extra_models: list[dict] | None = Field(
        default=None, sa_column=Column(JSON), description="用户补录的模型目录（含参数）"
    )
