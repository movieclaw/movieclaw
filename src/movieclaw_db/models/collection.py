from __future__ import annotations

from sqlalchemy import JSON, Column, ForeignKey, Integer, String, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin
from movieclaw_db.models.member_scoped import MemberScopedMixin, register_member_scoped


@register_member_scoped
class Collection(MemberScopedMixin, TimestampMixin, table=True):
    """合集——「一组 media_item」的持久化定义（docs/design/library-collections.md）。

    定位
    ----
    合集是**存好的筛选**：它的 ``rules`` 与 ``library.match_rules`` 同构
    （``[{field, op, values}]``，见 library-routing.md 1.1），所以"筛完存为合集"
    是一次纯粹的形状转换，"把合集设为某个库的收藏范围"同样零翻译。

    **没有 mode 列**——``smart | manual | system`` 是个假三分法：前两者说的是
    "成员怎么来"，system 说的是"能不能改"，两件正交的事挤进一列，后面必然长出
    ``if mode == "system" and rules`` 这种检查。形态改成推导：

    ====================  ==============================================
    问题                  答案来自
    ====================  ==============================================
    成员怎么来            ``rules`` 非空 → 规则驱动；``collection_item``
                          有行 → 名单驱动
    能不能改              ``builtin IS NULL`` → 用户创建，可改；否则内置
    ====================  ==============================================

    副作用是"规则 + 手工增删"这种组合天然可表达（Plex 的智能播放列表就是这个
    形态），v1 **不做**这个功能——只是模型没有把路堵死。

    智能合集**不物化**：成员靠 ``services/library/collections.resolve_members()``
    实时求值，它是 ``_wall_page_ids()`` 的一层薄适配。于是 web 与 Jellyfin
    兼容层"两端结果不一致"不再靠人记得调同一个函数，而是**没有第二条路可走**
    ——合集根本没有自己的查询。
    """

    __tablename__ = "collection"
    __table_args__ = (
        # 内置合集全局唯一：同一个 builtin 标识不该有两行（"我的收藏"只有一个）
        UniqueConstraint("builtin", name="uq_collection_builtin"),
    )

    id: int | None = Field(default=None, primary_key=True)

    name: str = Field(description="展示名")
    # 库删除时合集**级联删除**：库没了，"这个库里的一批片"这个定义也就没意义，
    # 留一个指向空气的合集只会变成幽灵数据。与 media_item.scrape_library_id 的
    # SET NULL 处理不同——那一列是"归属推断"可以重新推断，合集是用户定义的东西
    library_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        description="所属库；NULL=跨库合集（F4 才开入口）",
    )
    rules: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="收录规则，与 library.match_rules 同构；空=名单驱动",
    )
    sort: str = Field(default="title", description="合集内默认排序（WallSort 取值）")

    # -- 可见性 --------------------------------------------------------------
    visibility: str = Field(
        default="household", index=True, description="household=全家可见 / private=只有我"
    )
    # member_id 来自 MemberScopedMixin：private 时是归属成员，household 时为 0。
    # 登记为成员级数据（@register_member_scoped）是必须的——删成员时要把他的私有
    # 合集一并清掉，否则 SQLite 复用行 id，下一个新成员会继承前一个人的私有合集。
    # "是不是私有"看 visibility，不看 member_id 是否为空

    # -- 来源与顺序 ----------------------------------------------------------
    # 内置合集标识：favorites / tmdb_series:{id} / …；NULL=用户创建。
    # 这一列是"这个抽象要吃掉既有特例"的落点——「我的收藏」本来就是一个合集，
    # 登记之后它自动出现在合集列表与 Jellyfin BoxSet 里，不必各写一套
    builtin: str | None = Field(
        default=None,
        sa_column=Column("builtin", String, nullable=True),
        description="内置合集标识；NULL=用户创建（可改）",
    )
    # 墓碑：自动生成的合集（内置 / 系列）那颗「删除」按钮落在这里。真删了下次
    # ensure 又会长回来，用户会觉得"删不掉"；留行当墓碑语义上也更诚实——你删掉
    # 的是"我不想看见它"，不是"这个系列不存在"。用户自建的合集照旧真删，
    # 同一颗按钮两种归宿，由 builtin is None 推导（与"形态是推导的"同源）。
    # 必须可逆：接口侧 include_hidden 与 include_empty 同形，否则就是单向黑洞
    hidden: bool = Field(default=False, index=True, description="已隐藏（自动合集的「删除」）")
    position: int = Field(default=0, description="顶栏与网格的顺序（越小越靠前）")
    cover_item_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="SET NULL"),
            nullable=True,
        ),
        description="封面取哪部作品的海报；NULL=取首个成员",
    )

    # -- 系列档案快照（只有系列合集有）----------------------------------------
    # TMDB `GET /collection/{id}` 回的 parts[]：整个系列共几部、每部的 tmdb id /
    # 标题 / 上映日 / 海报。用来算「缺哪几部」并一键去补——这一条才是把系列合集
    # 从「整理」变成「补齐」的地方（只做归类的话，用户装个 Emby 也有）。
    #
    # **懒加载**：用户第一次打开这个系列的详情页时才去拉，之后走这份快照。
    # 初稿写的是刮削时每个系列拉一次——那是白白给扫描加负担，而用户从没点开的
    # 系列一个请求都不该花。
    #
    # 它是**外部档案的快照，不是我们的事实源**：脏了重拉即可，没有一致性负担。
    series_parts: list | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="系列全片名单快照（缺片补齐用）；NULL=还没拉过",
    )
    series_image: str | None = Field(
        default=None, description="系列官方海报路径（与 parts 同一次响应里白拿的）"
    )


class CollectionItem(TimestampMixin, table=True):
    """手动合集的名单行（规则驱动的合集没有行）。

    「固定当前命中」也走这里：把规则此刻的命中集快照成一份名单——不需要拖拽、
    不需要挑选 UI，只需要写一次本表。这是手动合集在 F3 就能低成本兑现的
    一半价值。
    """

    __tablename__ = "collection_item"
    __table_args__ = (
        UniqueConstraint("collection_id", "media_item_id", name="uq_collection_item"),
    )

    id: int | None = Field(default=None, primary_key=True)

    collection_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("collection.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="所属合集",
    )
    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="成员条目",
    )
    position: int = Field(default=0, description="名单内顺序（拖拽排序的落点）")
