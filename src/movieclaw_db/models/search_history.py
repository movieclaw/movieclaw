from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin
from movieclaw_db.models.member_scoped import MemberScopedMixin, register_member_scoped


@register_member_scoped
class SearchHistory(MemberScopedMixin, TimestampMixin, table=True):
    """搜索历史表：记录用户在搜索面板提交过的搜索，支撑「点历史记录快捷再搜」。

    设计取舍
    --------
    - **独立建表而非塞进 app_setting 的 JSON**：app_setting 定位是「一个配置域一条
      记录、整体覆盖式读写」，而搜索历史是持续增长、需要逐条增删和排序的列表数据，
      按行存才能自然地做去重计数、按时间排序、逐条删除。
    - **存「组合快照」而非预设引用**：标签栏支持自定义分类（多分类 × 多站点）后，
      历史行存搜索发生时的分类/站点组合（排序去重后的 JSON 串）与展示名快照。
      预设后来改名或删除都不影响历史的展示与重搜——历史永远按快照原样再搜。
    - **按 (keyword, 组合快照) 去重**：同一关键词 + 同一组合重复搜索不产生新行，
      只累加 ``search_count`` 并刷新 ``updated_at``（即「最近一次搜索时间」）与
      ``label``（预设改名后，最新名字跟着刷新）。
    - **容量上限由 Repository 维护**（超出后删最旧的行），防止长期使用后无限膨胀。
    """

    __tablename__ = "search_history"

    id: int | None = Field(default=None, primary_key=True)
    # 搜索者：member_id 由 MemberScopedMixin 提供（0=超管哨兵、非外键）。
    # 搜索历史是隐私性最强的个人数据之一，各人只看/只删自己的——清理由
    # register_member_scoped 登记后的 delete_member 统一覆盖。
    # 搜索关键词（已去首尾空白）。加索引：record 时按 (keyword, 快照) 查重。
    keyword: str = Field(index=True, description="搜索关键词")
    # 搜索垂直："torrent"=站点资源（种子）/ "media"=影视条目（豆瓣）。
    # 参与去重键：同一关键词分别搜媒体和资源是两条独立历史，各自维护快照。
    # 媒体历史的 categories/site_ids 恒为 None（豆瓣搜索没有这两个维度）。
    vertical: str = Field(default="torrent", description="搜索垂直：torrent/media")
    # 展示名快照：内置分类的中文名 / 预设名；None 表示「全部」。
    label: str | None = Field(default=None, description="展示名快照；None=全部")
    # 分类组合快照：排序去重后的 JSON 数组串（如 '["movie","tv"]'）；None=不限分类。
    # 存归一化后的串，去重查询才能用简单的相等比较。
    categories_json: str | None = Field(
        default=None, description="分类组合快照（归一化 JSON）；None=不限分类"
    )
    # 站点组合快照：排序去重后的 JSON 数组串；None=全部可用站点。
    site_ids_json: str | None = Field(
        default=None, description="站点组合快照（归一化 JSON）；None=全部站点"
    )
    # 该 (keyword, 组合快照) 的累计搜索次数。
    search_count: int = Field(default=1, description="累计搜索次数")
    # 图览模式快照：发起搜索时的展示模式偏好（来自自定义分类的 poster_mode）。
    # 是「怎么展示」而非「搜什么」，故不进去重键——同一搜索换展示模式仍是一条历史，
    # 只把本字段刷新为最新值（同 label）。点历史/看快照时据此还原结果页展示模式。
    poster_mode: bool = Field(default=False, description="发起搜索时的图览模式偏好")
    # 最近一次搜索的**结果快照**（合并后的完整结果集，JSON 串：total/items/sites）。
    # 同一历史行重搜时覆盖——历史行按组合去重，快照语义即「该组合最近一次的结果」。
    # 点历史记录先看快照（秒开、不打扰站点），需要新数据时再点「重新搜索」。
    snapshot_json: str | None = Field(
        default=None, description="最近一次搜索的结果快照（JSON）；None=尚无快照"
    )
    snapshot_at: datetime | None = Field(
        default=None, description="快照生成时间（naive UTC）；None=尚无快照"
    )
