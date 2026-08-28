from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin, utcnow


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


class DownloadAttemptStatus(StrEnum):
    """订阅下载尝试状态。

    ``wanted_item`` 表达用户仍缺哪些内容；本表状态表达为了满足这些内容先后
    投递过哪些种子。换源时旧种与试用种会短暂并存，因此不能再把单个
    ``wanted_item.info_hash`` 当作完整下载历史。
    """

    ACTIVE = "active"  # 当前主源，正常观察下载进度
    REPLACEMENT_PENDING = "replacement_pending"  # 主源无进度，等待/正在寻找替代源
    TRIAL = "trial"  # 替代源已投递，等待真实字节增长后晋升
    CLEANUP_PENDING = "cleanup_pending"  # 已切换工单，等待幂等清理旧下载任务
    SUPERSEDED = "superseded"  # 已由健康替代源接管，旧任务已安全清理
    RETAINED = "retained"  # 已被替代，但因用户所有权/H&R 风险保留旧任务
    COMPLETED = "completed"  # 下载器已确认完成，继续观察落点并等待入库
    IMPORTED = "imported"  # 关联工单已入库，退出下载观察与任务中心关联
    FAILED = "failed"  # 试用源同样无进度，进入失败候选冷却
    CANCELLED = "cancelled"  # 已退出当前订阅范围；保留历史但停止搜索、观察与救援


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
        # 海报墙只按持久化时间倒序扫描；id 是同一时间戳下的稳定次序。
        Index("ix_subscription_last_activity", "last_activity_at", "id"),
    )

    id: int | None = Field(default=None, primary_key=True)

    # 订阅活动是用户与后台管线的统一事实流水。数据库触发器在每条活动插入时
    # 原子推进此时间，列表查询无需 join/聚合活动表，也不依赖调用方手工维护。
    # 保留服务端默认值，应用回退到不了解此列的旧版本后仍可继续创建订阅。
    last_activity_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        description="最近一条订阅活动的发生时间；订阅海报墙的持久化排序键",
    )

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

    status: str = Field(
        default=SubscriptionStatus.ACTIVE,
        index=True,
        description="active / paused / completed（见 SubscriptionStatus 注释）",
    )

    # -- 归属（docs/design/member-management.md §3.5）-----------------------
    # NULL = 超管发起。成员被删除时外键 SET NULL——订阅自动转为超管发起，
    # 绝不连带删除订阅与下载任务（资源是全家的）。
    created_by_member_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("member.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description="发起成员；NULL=超管发起",
    )


class SubscriptionFollower(TimestampMixin, table=True):
    """订阅关注——成员 × 订阅 的多对多（docs/design/member-management.md §3.5）。

    订阅保持全局唯一（同一作品一份下载全家共享），第二个成员再订同一部时
    幂等转为关注：订阅出现在他的「我的订阅」里，但期望集合 E 不变。
    取消关注只删本行；发起人取消且仍有关注者时，发起人转移给最早的关注者
    （订阅继续活着）——成员的取消永远不影响别人正在追的内容。
    """

    __tablename__ = "subscription_follower"
    __table_args__ = (
        UniqueConstraint("subscription_id", "member_id", name="uq_subscription_follower"),
    )

    id: int | None = Field(default=None, primary_key=True)
    subscription_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("subscription.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    member_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("member.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )


class WantedItem(TimestampMixin, table=True):
    """工单——单元身份、当前订阅范围及其满足状态的物化（不只是缺口）。

    三个职责，多一件不做（docs/design/subscription-plan.md 数据模型汇总）：
    ① 表达期望单元身份（订阅 + 季集）；② 携带满足状态（wanted→grabbed→downloaded，
    对应不可逆现实事件）；③ 内嵌搜索调度（队列即字段）。
    匹配历史/拒绝原因在 match_record（P4），质量规则在 rule_set，
    内容元数据在 media_*（air_date 是唯一冗余快照，F3 负责同步）。

    ``status`` 只记录不可逆现实事件，``in_scope`` 独立记录该单元是否仍属于
    当前订阅期望集合。取消季只把 ``in_scope`` 置为 False，不抹掉投递历史；
    重新勾选时恢复范围并复用原工单，因此不会重复下载。

    季/集号 NOT NULL：SQLite 唯一索引里 NULL 互不相等，用了 NULL 不变量①
    （每个期望单元至多一个工单）就没有 DB 兜底。电影 = (0,0) 哨兵；
    剧集特别季是 (0, n≥1)，与电影不冲突。

    调度语义（创建时写死，铁律：本地缓存只用来追新，补旧永远真实搜索）：
    - 补旧（air_date 已过）：next_search_at=now，立即排队真实 PT 搜索；
    - 追新（未来播出）：next_search_at = air_date + 宽限期，被动匹配为主通道，
      到点未满足即天然漏抓兜底，零翻转机制；
    - 未定档：next_search_at=NULL（不可调度），F3 定档时回填。
    电影哨兵单元走上映感知调度（movie_schedule）：已上映超宽限=补旧、
    未上映/刚上映=上映+宽限、明确未上映且未定档=NULL 等 F3 回填；上映日
    **不写进 air_date**——covered_units 会拿 air_date 当发布时间物理上限剔除
    候选，而电影资源早于 TMDB 档期流出真实存在（分地区/提前数字发行）。
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
        default=WantedStatus.WANTED, index=True, description="wanted / grabbed / downloaded"
    )
    in_scope: bool = Field(
        default=True,
        description="是否属于当前订阅期望集合；False 仅保留历史，不参与搜索、救援与进度",
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

    # -- 追新资源发布时间预测 ------------------------------------------------
    # 预测是本工单的派生调度快照，不另建 observation / hint 表：原始发布时间与
    # 首次发现时间已经由 site_torrent 保存；站点同步是否消费某个预测探测点，
    # 由 SiteSyncCursor.last_sync_at 可重算。NULL=样本不足/不适用。
    release_forecast: dict | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="追新资源发布时间预测快照；NULL=尚无可靠预测",
    )

    grabbed_at: datetime | None = Field(default=None, description="投递成功时间")

    # -- 洗版（docs/design/quality-upgrade.md §4/§6）--------------------------
    # 满足该单元的当前版本质量快照（QualitySnapshot schema，movieclaw_matcher）。
    # 洗版比较的唯一基线：只在"确认升级"时刷新（基线单调），实测维度以
    # ffprobe 为准、出处维度采信名称解析。NULL=未满足或无法识别（不参与洗版）。
    # none_as_null：Python None ↔ SQL NULL（同 library_file 的三态 JSON 约定），
    # 否则 None 会被存成 JSON 'null' 字符串，回填任务的 IS NULL 查询失效
    quality: dict | None = Field(
        default=None,
        sa_column=Column(JSON(none_as_null=True), nullable=True),
        description="当前版本质量快照（洗版基线）；NULL=未回填；{}=已回填但无法识别",
    )
    # 洗版证伪熔断计数：连续证伪达到阈值后该单元洗版转入长冷却并提示人工介入，
    # 防连环造假资源烧流量烧 ratio；确认升级成功时清零。
    upgrade_verify_failures: int = Field(
        default=0, description="洗版候选连续证伪次数；确认升级时清零"
    )

    # -- 单集履历注解（订阅详情页里程碑链）------------------------------------
    # 工单自身已携带六站时间戳（air_date → 搜索 → grabbed → downloaded →
    # imported → 洗版），唯一缺的叙事细节是「最近为什么被拒 / 投的是哪个种子」
    # ——它们散在活动流水里，按季集捞既慢又受条数上限。选择冗余快照而不是
    # 活动↔工单关联表：关联表要迁移回填、逐轮写关联，换来的仍是一条读不快的
    # 流水；两列在写入点顺手落，详情页一次查询即得（同类冗余先例：
    # subscription.last_activity_at 由数据库触发器维护）。
    last_reject_reason: str | None = Field(
        default=None,
        description="最近一次候选被拒原因（含站点与种子名）；NULL=从未被拒",
    )
    grab_title: str | None = Field(
        default=None,
        description="最近一次投递的种子（站点 · 标题）；NULL=未投递",
    )

    # -- 入库管线（媒体库 L2）------------------------------------------------
    # 真实投递成功时记录种子 infohash——check_download_progress 据此轮询
    # 下载器进度；dry-run 投递不产生 infohash，工单停在 grabbed 不进管线。
    info_hash: str | None = Field(
        default=None, index=True, description="投递种子的 infohash；NULL=模拟投递/未投递"
    )
    downloaded_at: datetime | None = Field(default=None, description="下载器确认完成时间")
    imported_at: datetime | None = Field(default=None, description="整理入库完成时间")


class SubscriptionDownloadAttempt(TimestampMixin, table=True):
    """一次订阅种子投递及其救援观察。

    一次整季包可覆盖多条 wanted，因此尝试按 ``subscription + infohash`` 建模，
    ``units`` 累计同一 infohash 后续认领的覆盖单元。进度只保存完成字节、
    网络下载字节和最后增长时刻，不复制下载器实时状态；这些字段只是跨重启
    可靠判断“连续多久没有前进”的心跳。
    """

    __tablename__ = "subscription_download_attempt"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "info_hash",
            name="uq_subscription_download_attempt_hash",
        ),
        Index("ix_subscription_download_attempt_status_due", "status", "next_search_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    subscription_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("subscription.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    downloader_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("downloader_client.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description="实际承载该任务的下载器；NULL=旧数据或下载器配置已删除",
    )
    replaces_attempt_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("subscription_download_attempt.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description="试用替代源所替换的旧尝试",
    )

    info_hash: str = Field(index=True, description="BT infohash（统一小写）")
    site_id: str | None = Field(default=None, description="候选来源站点")
    torrent_id: str | None = Field(default=None, description="站点内种子 ID")
    torrent_title: str = Field(default="", description="投递时的种子标题快照")
    download_name: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True),
        description="下载器返回的真实任务/内容名；用于监听目录可靠关联",
    )
    save_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="MovieClaw 视角的投递路径；用于同名任务消歧",
    )
    units: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="该 infohash 历次认领累计覆盖的 [[season, episode], ...]",
    )
    quality: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="分辨率/片源/HDR 等质量属性快照，换源时作为不降级底线",
    )
    # 内容核验的负面记忆：种子下完后文件清单里**明确没有**的单元。
    # 没有它，"退回重找 → 又选中同一个种子 → 秒完成 → 再退回"会无限循环
    # （真实教训：S04 全集包不含被 TMDB 编号为 S04E14 的特别篇，工单每
    # 30 分钟空转一轮）。``sources`` 记下投递过这份内容的站点条目，匹配层
    # 据此在**选种阶段**就跳过——同一发布在多站镜像，逐个证伪一次即收敛。
    content_missing: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default=text("'{}'")),
        description='实测不含的单元与来源：{"units": [[季, 集]], "sources": [[站点, 种子ID]]}',
    )
    hit_and_run: bool | None = Field(default=None, description="投递时观测到的 H&R 状态")
    owned_by_movieclaw: bool = Field(
        default=False,
        description="提交前下载器中不存在该任务；只有这类任务才允许自动清理",
    )
    # 投递目的：download=常规缺口下载（缺省）/ upgrade=洗版投递。
    # 洗版在途去重、活动文案分流与旧版本清理定位都以此为据
    # （docs/design/quality-upgrade.md §6.2）。
    purpose: str = Field(default="download", description="投递目的：download / upgrade")
    # 手动选种标记：用户显式挑的种子。洗版验证据此改变裁决——未能证明
    # 更优时新旧版本共存保留（绝不丢弃用户挑的文件），也不计熔断/排除
    # （docs/design/quality-upgrade.md §13.8）。
    manual: bool = Field(default=False, description="是否手动选种投递（用户显式选择）")

    status: str = Field(default=DownloadAttemptStatus.ACTIVE, index=True)
    last_downloader_state: str | None = Field(default=None)
    last_observed_at: datetime | None = Field(default=None)
    baseline_completed_bytes: int | None = Field(
        default=None,
        description="完成块观测基线；试用源晋升只认累计网络下载字节",
    )
    last_completed_bytes: int | None = Field(default=None)
    baseline_downloaded_bytes: int | None = Field(
        default=None,
        description="试用源累计网络下载字节基线；本地校验不会增加该计数",
    )
    last_downloaded_bytes: int | None = Field(
        default=None, description="最近一次观测到的累计网络下载字节"
    )
    last_progress_at: datetime = Field(description="完成字节最后增长时刻，也是无进度计时起点")
    stalled_notified_at: datetime | None = Field(
        default=None, description="15 分钟无进度提醒已产生的时刻；进度恢复后清空"
    )
    missing_observations: int = Field(
        default=0, description="下载器可达但连续找不到任务的次数"
    )

    next_search_at: datetime | None = Field(
        default=None, index=True, description="死种下一次真实跨站搜索时间"
    )
    search_attempts: int = Field(default=0, description="成功执行但没找到替代源的次数")
    last_search_at: datetime | None = Field(default=None)
    cleanup_note: str | None = Field(default=None, description="旧任务清理或保留原因")
