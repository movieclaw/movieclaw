from __future__ import annotations

from sqlalchemy import JSON, Column
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class RuleSet(TimestampMixin, table=True):
    """规则组——"什么样的载体可接受"的可复用参数包。

    职责边界（docs/design/subscription-plan.md 数据模型汇总）：
    - 回答"候选可不可接受"（硬过滤）与"谁更好"（偏好排序），
      洗版目标见 spec 的 upgrade_source/cutoff_resolution；
    - **纯参数**：判断逻辑全在 movieclaw_matcher，本表一行不含行为；
    - 订阅只持引用、不做 per-订阅 override——想微调就复制一个规则组；
    - 被订阅引用时禁删（服务层保证）；修改只影响之后的评估，不追溯已 grabbed。

    ``spec`` 的 schema 是 ``movieclaw_matcher.RuleSetSpec``（此处存其 JSON 序列化，
    db 层不反向依赖 matcher 包）。空 spec = 全不限，即默认规则组的形态。

    ``match_rules`` 是规则组的**适用范围声明**（docs/design/rule-set-scope.md）：
    与媒体库收藏范围同构的条件列表，外加 ``kind`` 字段（电影/剧集）。新订阅
    未显式指定规则组时，在全部规则组里找适用范围命中的那个（条件多者优先），
    都不命中落 ``is_default`` 的组。空列表 = 未声明，只能被手选或作为默认兜底。
    """

    __tablename__ = "rule_set"

    id: int | None = Field(default=None, primary_key=True)

    name: str = Field(index=True, unique=True, description="规则组名（唯一，展示用）")
    is_default: bool = Field(default=False, description="新订阅默认选中；全表至多一个 True")
    spec: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="RuleSetSpec 的 JSON；空对象=全不限",
    )
    # 适用范围：[{"field": "kind"|"genres"|"origin_countries", "op": "any_of",
    # "values": [...]}]，条件间 AND、条件内交集即满足。只决定新订阅的预选，
    # 不影响已挂靠本组的订阅
    match_rules: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
        description="适用范围条件列表；空=未声明（只能手选或作默认兜底）",
    )
