from __future__ import annotations

from enum import StrEnum

from sqlalchemy import JSON, Column, ForeignKey, Integer
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class ActivityType(StrEnum):
    """订阅活动类型。

    透明化原则：订阅背后发生的**每一个动作**都要落一条活动，用户在详情页的
    时间线上能完整回放"系统到底做了什么、为什么还没下到"。

    生命周期类（Phase 2 起记录）：
    - CREATED / ADJUSTED / PAUSED / RESUMED：用户操作
    - COMPLETED / REOPENED：派生状态翻转（收齐 / 有新缺口重新追踪）

    管线类（Phase 4 起记录，类型先占位保证前端枚举稳定）：
    - SEARCHED：执行了一次真实站点搜索（站点、关键词、结果数进 payload）
    - MATCH_ACCEPTED / MATCH_REJECTED：候选判定（拒绝原因是可解释性的核心）
    - GRABBED / DISPATCH_FAILED：投递结果
    - WANTED_ADDED：元数据刷新发现新集，追加了工单

    入库管线类（媒体库 L2 起记录）：
    - DOWNLOADED：下载器确认文件全部落盘
    - IMPORTED：整理入库完成（硬链到库目录 + 台账落账，payload 带路径）
    - IMPORT_FAILED：整理失败（跨盘硬链/路径不可达/解析不出集号等，中文原因）
    """

    CREATED = "created"
    ADJUSTED = "adjusted"
    PAUSED = "paused"
    RESUMED = "resumed"
    COMPLETED = "completed"
    REOPENED = "reopened"
    SEARCHED = "searched"
    MATCH_ACCEPTED = "match_accepted"
    MATCH_REJECTED = "match_rejected"
    GRABBED = "grabbed"
    DISPATCH_FAILED = "dispatch_failed"
    WANTED_ADDED = "wanted_added"
    DOWNLOADED = "downloaded"
    IMPORTED = "imported"
    IMPORT_FAILED = "import_failed"
    DOWNLOAD_STALLED = "download_stalled"
    REPLACEMENT_SEARCHED = "replacement_searched"
    REPLACEMENT_TRIAL = "replacement_trial"
    REPLACEMENT_PROMOTED = "replacement_promoted"
    REPLACEMENT_CLEANUP = "replacement_cleanup"
    # 洗版（docs/design/quality-upgrade.md §8.4）
    UPGRADE_GRABBED = "upgrade_grabbed"  # 发现更高版本并提交下载
    UPGRADED = "upgraded"  # 洗版完成：新版本入库、旧版本清理
    UPGRADE_VERIFY_FAILED = "upgrade_verify_failed"  # 洗版候选实测证伪，已排除
    # 入库规格核验（services/subscription/spec_audit.py）
    SPEC_MISMATCH = "spec_mismatch"  # 入库文件实测规格与种子声称不符


class SubscriptionActivity(TimestampMixin, table=True):
    """订阅活动流水——详情页时间线的数据源，订阅可解释性的落点。

    设计取舍：
    - ``message`` 在**写入时**渲染成完整中文句子（本项目原则：非开发者部署时
      也要看得懂），时间线直接展示，不做前端模板拼接；
    - ``payload`` 存结构化细节（季集号、站点、种子 id、评分、原因码……），
      供未来的筛选/统计分析，展示层不依赖它；
    - 不建独立的 match_record 表：候选判定作为活动记录（payload 带结构化字段）
      已覆盖台账需求，一张时间线表打天下（原计划的 match_record 并入此表，
      见 docs/design/subscription-plan.md 数据模型汇总的修订）。
    - ``wanted_item_id`` 用 SET NULL：工单可能因修改订阅被删，活动作为历史
      必须保留（payload 里的季集号仍可定位）。
    - 每次 INSERT 由数据库触发器原子推进 ``subscription.last_activity_at``；
      因此所有写入路径（包括与换源状态同事务的直接 INSERT）都会及时刷新
      海报墙排序键，读取列表时无需扫描本表。
    """

    __tablename__ = "subscription_activity"

    id: int | None = Field(default=None, primary_key=True)

    subscription_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("subscription.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="所属订阅",
    )
    wanted_item_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("wanted_item.id", ondelete="SET NULL"),
            nullable=True,
        ),
        description="关联工单（订阅级活动为 NULL；工单被删后置 NULL 保留历史）",
    )

    type: str = Field(index=True, description="活动类型（ActivityType）")
    message: str = Field(description="完整中文句子，时间线直接展示")
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="结构化细节（季集/站点/原因码等），供筛选与分析",
    )
