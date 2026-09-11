from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, Text, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class MediaDisprovenSource(TimestampMixin, table=True):
    """条目级的"证伪来源"台账：这个发布被实测证明满足不了这个单元。

    **为什么不继续只用 ``SubscriptionDownloadAttempt.content_missing``**：那张
    表的 ``subscription_id`` 是 ``ON DELETE CASCADE``，订阅一删，它名下所有投递
    记录连同负面记忆一起消失。真实教训——用户抓到一条 113 MB 的假「正片」，
    手动删掉库里的文件、删掉订阅重建，系统把同一条种子原样又抓了一遍：证据
    明明产生过，只是挂在了一个比它短命的东西上。

    记忆的正确归属是**条目**而不是订阅：「这个发布里没有这部电影」是关于内容
    的事实，与用户订没订、订了几次无关。挂到 ``media_item`` 上，删订阅重建、
    换规则组、洗版重订都带得走；条目本身被删才随之消失（那时它确实没意义了）。

    ``content_missing`` 保留不动：它仍是那次投递自己的台账（诊断工单会展示
    "实测缺失"），选种阶段则**两边都读**——存量安装里的旧记忆不必数据迁移就
    继续生效，新证据同时落到这张表上。

    单元用 (season, episode) 表达，电影是语义零值 (0, 0)，与 ``WantedItem``
    的口径一致。
    """

    __tablename__ = "media_disproven_source"
    __table_args__ = (
        UniqueConstraint(
            "media_item_id",
            "site_id",
            "torrent_id",
            "season_number",
            "episode_number",
            name="uq_media_disproven_source_unit",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    site_id: str = Field(description="来源站点")
    torrent_id: str = Field(description="站点内种子 ID")
    season_number: int = Field(description="季号；电影为 0")
    episode_number: int = Field(description="集号；电影为 0")
    # 证伪成因。content_missing=下完后文件清单里没有这个单元；
    # runtime_mismatch=下完后实测片长远短于影片信息（预告片/sample/假种）
    reason: str = Field(description="content_missing / runtime_mismatch")
    note: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="可直接展示的中文说明；NULL=旧数据",
    )
