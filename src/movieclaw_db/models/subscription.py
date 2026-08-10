from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, ForeignKey, Index, Integer, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class SubscriptionStatus(StrEnum):
    """订阅状态。tracking/completed 是派生值（随时可从工单集合重算），
    paused 是用户显式操作——重算只在 active/completed 之间翻转，不碰 paused。"""

    ACTIVE = "active"  # 调和进行中（前端语义即 tracking）
    PAUSED = "paused"  # 用户暂停：worker 与被动匹配跳过其工单
    COMPLETED = "completed"  # 缺口为零且期望集合不再生长


class WantedStatus(StrEnum):
    """工单状态机：每一步对应不可逆的现实事件。"""

    WANTED = "wanted"  # 缺着（这个子集才是"缺口"）
    GRABBED = "grabbed"  # 已向下载器投递成功
    DOWNLOADED = "downloaded"  # 下载器确认文件全部落盘
    IMPORTED = "imported"  # 已整理入库（硬链到库目录 + library_file 落账）——终态
    UPGRADING = "upgrading"  # 已有可用版本，继续搜索洗版目标


class Subscription(TimestampMixin, table=True):
    """订阅——期望集合 E 的定义（docs/design/subscription.md 推论一）。

    E = 勾选季的全部已知集 ∪（follow_future ? 订阅时刻之后播出的一切集(含新季) : ∅）

    - 「补缺失」= 勾选已播季；「只追未来」= 不勾季 + follow_future；两者可叠加。
      引擎里没有"模式"分支——模式只是 E 的初始化参数与生长开关。
    - 同一媒体条目至多一个订阅（唯一约束），重复订阅由服务层幂等返回已有。
    - status 遵守不变量④：completed 可随时从工单集合重算，不存第二真相。
    """

    __tablename__ = "subscription"
    __table_args__ = (
        # 同一条目一个订阅——重复订阅幂等复用的依据
        UniqueConstraint("media_item_id", name="uq_subscription_media_item"),
    )

    id: int | None = Field(default=None, primary_key=True)

    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="订阅的媒体条目（身份锚）",
    )
    # 冗余条目 kind，电影/剧集分栏列表不用 join（同 wanted 冗余 media_item_id 的思路）
    kind: str = Field(index=True, description="movie / tv（冗余自 media_item）")

    # -- E 的定义 -----------------------------------------------------------
    selected_seasons: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="勾选的季号列表（勾了=要整季，含未播集）；电影为空列表；特别季 0 须显式勾选",
    )
    follow_future: bool = Field(
        default=False, description="追新开关：订阅后播出的一切集（含新集/新季）自动纳入"
    )

    # -- 入库目标 -----------------------------------------------------------
    # NULL = 用该 kind 的默认库；库被删除时外键 SET NULL，自动回落默认。
    # 投递 save_path 由库主根推导（services.library.config.derive_save_path）。
    library_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description="入库到哪个媒体库；NULL=该类型的默认库",
    )

    # -- 载体规则（只持引用，不做 override）---------------------------------
    rule_set_id: int = Field(
        sa_column=Column(Integer, ForeignKey("rule_set.id"), nullable=False, index=True),
        description="引用的规则组；规则组被引用时禁删（服务层保证）",
    )

    quality_policy: dict | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="首个版本锁定/洗版目标及运行态；NULL=关闭",
    )

    status: str = Field(
        default=SubscriptionStatus.ACTIVE,
        index=True,
        description="active / paused / completed（见 SubscriptionStatus 注释）",
    )


class WantedItem(TimestampMixin, table=True):
    """工单——期望单元及其满足状态的物化（不只是缺口）。

    三个职责，多一件不做（docs/design/subscription-plan.md 数据模型汇总）：
    ① 表达期望单元身份（订阅 + 季集）；② 携带满足状态（wanted→grabbed→downloaded，
    对应不可逆现实事件）；③ 内嵌搜索调度（队列即字段）。
    匹配历史/拒绝原因在 match_record（P4），质量规则在 rule_set，
    内容元数据在 media_*（air_date 是唯一冗余快照，F3 负责同步）。

    生命周期**只进不出**：唯一的删除是"修改订阅移除季"时删该季未完成工单；
    已 grabbed/downloaded 的永不回收（现实不可逆），这也让重新勾选季时
    diff 不会重复下载。

    季/集号 NOT NULL：SQLite 唯一索引里 NULL 互不相等，用了 NULL 不变量①
    （每个期望单元至多一个工单）就没有 DB 兜底。电影 = (0,0) 哨兵；
    剧集特别季是 (0, n≥1)，与电影不冲突。

    调度语义（创建时写死，铁律：本地缓存只用来追新，补旧永远真实搜索）：
    - 补旧（air_date 已过/电影）：next_search_at=now，立即排队真实 PT 搜索；
    - 追新（未来播出）：next_search_at = air_date + 宽限期，被动匹配为主通道，
      到点未满足即天然漏抓兜底，零翻转机制；
    - 未定档：next_search_at=NULL（不可调度），F3 定档时回填。
    """

    __tablename__ = "wanted_item"
    __table_args__ = (
        # 不变量①：每个期望单元至多一个工单
        UniqueConstraint(
            "subscription_id",
            "season_number",
            "episode_number",
            name="uq_wanted_sub_season_episode",
        ),
        # 被动匹配直达索引：种子 → 条目 → 未满足工单，一次查询不 join 订阅
        Index("ix_wanted_media_status", "media_item_id", "status"),
    )

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
    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        description="冗余自订阅，给被动匹配的直达索引",
    )

    season_number: int = Field(default=0, description="季号；电影=0（哨兵）")
    episode_number: int = Field(default=0, description="集号；电影=0（哨兵）")

    status: str = Field(
        default=WantedStatus.WANTED,
        index=True,
        description="wanted / upgrading / grabbed / downloaded / imported",
    )
    air_date: date | None = Field(
        default=None, description="播出日期快照（冗余自 episodes JSON，F3 同步）；NULL=未定档"
    )

    # -- 内嵌搜索调度（队列即字段）------------------------------------------
    priority: int = Field(default=0, description="worker 排序权重，大者优先")
    next_search_at: datetime | None = Field(
        default=None, index=True, description="下次真实 PT 搜索到期时刻；NULL=未定档不可调度"
    )
    search_attempts: int = Field(default=0, description="已搜索次数（退避曲线的输入）")
    last_search_at: datetime | None = Field(default=None, description="上次搜索时间")

    grabbed_at: datetime | None = Field(default=None, description="投递成功时间")

    # -- 入库管线（媒体库 L2）------------------------------------------------
    # 真实投递成功时记录种子 infohash——check_download_progress 据此轮询
    # 下载器进度；dry-run 投递不产生 infohash，工单停在 grabbed 不进管线。
    info_hash: str | None = Field(
        default=None, index=True, description="投递种子的 infohash；NULL=模拟投递/未投递"
    )
    downloaded_at: datetime | None = Field(default=None, description="下载器确认完成时间")
    imported_at: datetime | None = Field(default=None, description="整理入库完成时间")
