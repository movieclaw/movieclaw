from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class BoostTaskState(StrEnum):
    """刷流任务的生命周期状态。

    - ``ACTIVE``：在下载器中（下载中或做种中），占用刷流预算。
    - ``EVICTED``：被引擎主动汰换删除（上传效率过低，连数据一起删）。
    - ``MISSING``：下载器里找不到了——用户手动删除是明确意图，引擎只让出
      预算、不追究也不重新抢回，避免与用户拉扯。

    EVICTED / MISSING 都是终态；台账保留下来既是"累计上传贡献"的统计来源，
    也是去重依据（同一 (site_id, torrent_id) 不会被再次抢下）。
    """

    ACTIVE = "active"
    EVICTED = "evicted"
    MISSING = "missing"


class RatioBoostTask(TimestampMixin, table=True):
    """刷流任务台账：自动刷分享率引擎抢下的每一个免费种子。

    设计要点（完整机制见 docs/design/site-protection-ratio-boost.md）：

    - **所有权铁律**：只有"提交前下载器中不存在、由引擎亲手加入"的种子才会
      入本表；提交时发现已存在（用户自己在下）的绝不入账，从而永远不会被
      自动删除——与订阅管线 ``owned_by_movieclaw`` 同一哲学。
    - **效率追踪**：每 tick 从下载器读一次全量列表，按 infohash 差分累计
      上传量得到上传速度 EMA。EMA 而非瞬时值：PT 上传是突发的，瞬时 0
      不代表没人要。
    - ``created_at``（Mixin）即入池时间，汰换的 72 小时最低保留期以它起算。
    """

    __tablename__ = "ratio_boost_task"

    id: int | None = Field(default=None, primary_key=True)
    # 站点与站内种子 ID：去重与统计的维度（同一种子汰换后不再抢回）
    site_id: str = Field(index=True, description="站点标识")
    torrent_id: str = Field(description="站点内种子 ID")
    # 种子 v1 infohash（40 位小写十六进制），与下载器对账的唯一键
    info_hash: str = Field(unique=True, index=True, description="种子 infohash（小写）")
    # 实际承载任务的下载器；删除下载器配置时置空，台账保留
    downloader_id: int | None = Field(
        default=None,
        foreign_key="downloader_client.id",
        ondelete="SET NULL",
        description="承载该任务的下载器",
    )
    title: str = Field(default="", description="种子标题（展示用）")
    # 入池时记录的种子体积，预算占用按它累加（下载器视角的体积可能因未选
    # 文件而略小，刷流不选文件，二者一致）
    size_bytes: int = Field(description="种子体积（字节）")
    # 准入时从索引快照的免费截止；窗口已过仍未下完的任务由止损逻辑放弃
    # （继续下载就是付费下载，反而伤分享率）。NULL=无截止/长期免费
    free_deadline: datetime | None = Field(
        default=None, description="准入时的免费截止快照；NULL=无截止"
    )

    state: BoostTaskState = Field(
        default=BoostTaskState.ACTIVE, index=True, description="任务状态"
    )
    # 下载是否已完成（完成才可能进入汰换候选）
    completed: bool = Field(default=False, description="下载是否完成")
    # 首次观测到下载完成的时间：汰换判定窗口以它起算（完成前是在下载，
    # 谈不上"传得慢"）。NULL=尚未完成或历史数据（历史数据回退用 created_at）
    completed_at: datetime | None = Field(default=None, description="下载完成时间")
    # 准入时该种是否明确标注 H&R 考核：True 的任务保留期取
    # max(站点保留期, 站点 hr_seed_hours)——真实考核时长优先于保底配置
    hit_and_run: bool = Field(default=False, description="是否明确标注 H&R 考核")
    # -- 入场快照（准入瞬间从索引抄录，此后永不覆盖）------------------------
    # 与 swarm_seeders/leechers（每 tick 刷新的"最近观测"）不同，这组字段
    # 冻结的是**决策依据**：事后才能回归"什么样的入场特征预测高产出"、
    # 评估准入策略本身的好坏。NULL=历史任务（快照机制上线前入池）
    entry_seeders: int | None = Field(default=None, description="入场时索引观测的做种数")
    entry_leechers: int | None = Field(default=None, description="入场时索引观测的下载数")
    entry_score: float | None = Field(default=None, description="准入评分（决策时的值）")
    # 种子在站点的发布时间：created_at - torrent_published_at = 入场延迟
    # （种龄），"上桌早晚"是刷流收益的一等因子，要能被度量
    torrent_published_at: datetime | None = Field(
        default=None, description="种子发布时间（入场快照）"
    )
    # 最近观测的累计上传量；差分驱动 EMA 更新
    uploaded_bytes: int = Field(default=0, description="累计上传量（字节）")
    # 最近观测的累计下载量（含重下块，可能略大于体积）：真实带宽成本台账，
    # 任务被汰换/止损后收支仍有账可查（qb 里删了任务就再也拿不到这个数）
    downloaded_bytes: int = Field(default=0, description="累计下载量（字节）")
    # 上传速度 EMA（字节/秒），汰换排序的依据
    upload_rate_ema: float = Field(default=0.0, description="上传速度 EMA（字节/秒）")
    last_checked_at: datetime | None = Field(
        default=None, description="最近一次与下载器对账时间"
    )
    # 蜂群快照（每 tick 由下载器回报刷新）：汰换的第二信源——EMA 只说
    # "过去传得快不快"，蜂群说"未来还有没有人要"。leechers=0 的死种
    # 不必等测量成熟即可汰换；NULL=下载器未提供（旧适配器/无 tracker 数据）
    swarm_seeders: int | None = Field(default=None, description="蜂群做种数；NULL=未知")
    swarm_leechers: int | None = Field(default=None, description="蜂群下载数；NULL=未知")
    # 用户请求清理后最早可删除的时刻（docs/design/site-protection-ratio-boost.md §2.9）：
    # 请求时还在保留期内的记保留期到期时刻（提前删可能被记 H&R），下载器暂时不可达
    # 没删成的记请求时刻；引擎每 tick 检查、到点连数据删除。请求时就把到期时刻算好
    # 落库，站点配置之后被删（保留天数随之丢失）也不会提前删。NULL=未请求清理
    cleanup_after: datetime | None = Field(
        default=None, description="用户请求清理后最早可删除的时刻；NULL=未请求清理"
    )
    evicted_at: datetime | None = Field(default=None, description="汰换/失踪的时间")
    evict_reason: str | None = Field(default=None, description="汰换原因（中文，展示用）")
