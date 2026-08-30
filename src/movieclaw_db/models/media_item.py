from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class MediaItem(TimestampMixin, table=True):
    """统一媒体条目——订阅、资源匹配与媒体库共同的身份锚点。

    定位
    ----
    订阅（期望 E）、种子匹配、媒体库文件台账（库存 H，L2 起）都锚定本表，
    谁也不拥有它——这是"订阅↔库存同锚"的前提（docs/design/library.md 第 0 节）。
    任何入口（TMDB 发现页、豆瓣、未来其他源）的订阅都收敛为本表的一行，
    以 ``(kind, tmdb_id)`` 为唯一锚。本表**不是 TMDB 镜像**，只存"订阅逻辑
    与匹配内核会消费"的最小闭包字段：外部 ID、标题与别名集合、年份、status、
    海报路径。简介/演职员/评分等展示信息走 ``MediaDiscoverService`` 实时接口，
    不落库（详见 docs/design/subscription.md 1.1/1.3）。

    为什么锚定 TMDB：匹配内核依赖英文名/别名集合（种子以英文场景命名）、
    季集结构、每集播出日期，三者只有 TMDB 免费且完整提供。豆瓣条目在订阅
    创建时收敛到本表（douban_id 留存为来源与精确匹配信号），不允许创建
    无 tmdb_id 的"无锚条目"。

    三态铁律与全表约定同 ``SiteTorrent``：可缺失字段 NULL=未知，
    语义空值用空串/空列表。
    """

    __tablename__ = "media_item"
    __table_args__ = (
        # 同一类型下 TMDB ID 唯一——ensure_media_item 幂等复用的依据
        UniqueConstraint("kind", "tmdb_id", name="uq_media_item_kind_tmdb"),
    )

    id: int | None = Field(default=None, primary_key=True)

    # -- 身份锚（唯一键，永不为空）-----------------------------------------
    # 存 MediaKind 的字符串值（"movie"/"tv"），db 层不反向依赖 media 层枚举
    # （同 site_torrent.category 的处理方式）
    kind: str = Field(index=True, description="媒体类型：movie / tv")
    tmdb_id: int = Field(description="TMDB 条目 ID（锚）")

    # -- 外部 ID：与 site_torrent 详情层精确匹配的桥 ------------------------
    # imdb_id / douban_id 与种子富化带回的同名字段精确相等时，是比标题匹配
    # 可靠得多的命中信号（匹配内核的第一优先级）
    imdb_id: str | None = Field(default=None, index=True, description="IMDb ID；无/未知为 NULL")
    douban_id: str | None = Field(
        default=None, index=True, description="豆瓣 ID；非豆瓣入口且未知为 NULL"
    )

    # -- 标题与匹配素材 ------------------------------------------------------
    title: str = Field(description="主展示标题（zh-CN 优先）")
    original_title: str = Field(description="原始语言标题")
    year: int | None = Field(
        default=None, description="上映/首播年份；NULL=未知（匹配的硬约束之一）"
    )
    # 别名集合存**原样文本**（TMDB alternative_titles/translations + 豆瓣标题），
    # 仅精确去重；归一化（大小写/全半角/繁简）是匹配内核的职责——规则会进化，
    # 数据不动、规则动，避免内核升级时全量重写数据
    aliases: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="匹配用别名集合（原样文本，精确去重）",
    )

    # -- 生命周期 ------------------------------------------------------------
    # 存 TMDB status 原值（Released / Returning Series / Ended / Canceled…），
    # 元数据刷新任务据此分档决定刷新间隔
    status: str | None = Field(default=None, description="TMDB status 原值；NULL=未知")

    # -- 展示（仅海报路径，前端经 image-proxy 拼接）-------------------------
    poster_path: str | None = Field(default=None, description="TMDB 海报相对路径")
    backdrop_path: str | None = Field(default=None, description="TMDB 宽幅剧照相对路径")

    # -- 刮削归属库（docs/design/scrape-customization.md §14）----------------
    # "这条条目按哪套刮削配置"的答案。元数据与图片的产物挂**全局条目**
    # （一部片一份档案、图片按条目 id 存一份），所以语言/选图/图片档位这类
    # 设置没法像命名模板那样"按库各来一套"——同一条目的文件散在两个库时，
    # 两库不同口味会轮流覆盖同一行、同一个文件。把归属库钉在条目上，这件事
    # 就有了唯一确定的答案：刮削与后台刷新读同一列（**刷新任务是纯按条目
    # 全表扫的，没有库上下文**，这才是非钉不可的根本原因），口味不会被洗回全局。
    #
    # NULL = 未定：读取端 ``scrape_config.resolve_scrape_library`` 惰性推断并
    # 回填固化（在位文件所属库 → 订阅目标库 → 该类型默认库 → 仍无则跟全局）。
    # 存量条目升级后全是 NULL，靠这条自愈，不需要数据迁移脚本。
    # 库删除时置 NULL（FK ondelete SET NULL），下次读取重新推断。
    scrape_library_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description="刮削归属库；NULL=未定（读取时推断并回填）",
    )

    # -- 元数据刷新台账（仿 SiteSyncCursor 的 tick 模式）--------------------
    metadata_refreshed_at: datetime | None = Field(
        default=None, description="上次成功刷新元数据；NULL=建档后未刷过"
    )
    # NULL=立即到期：建档后首个刷新 tick 即处理，由刷新任务按 status 分档重排
    next_refresh_at: datetime | None = Field(
        default=None, description="下次刷新到期时刻；NULL=立即到期"
    )


class MediaSeason(TimestampMixin, table=True):
    """剧集条目的季——按季订阅的骨架 + 季级展示信息（仅 kind=tv 的条目有行）。

    集数据在 ``media_episode`` 表（docs/design/metadata.md 第 1 节决策）：
    单集简介可达数百字，几百集的剧塞进一行 JSON 会膨胀到 MB 级，按集
    upsert / 按季查询都需要真正的行。``wanted_item`` / ``library_file`` 对集的
    引用仍是 (season_number, episode_number) 数字对，不设外键（约定不变）。
    """

    __tablename__ = "media_season"
    __table_args__ = (
        # 同一条目内季号唯一——元数据刷新按 (条目, 季号) upsert
        UniqueConstraint("media_item_id", "season_number", name="uq_media_season_item_season"),
    )

    id: int | None = Field(default=None, primary_key=True)

    # 条目删除时季随之级联删除（engine 已开启 SQLite 外键约束）
    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="所属媒体条目",
    )
    # 0=特别季（Specials），允许存在但默认不参与订阅
    season_number: int = Field(description="季号；0=特别季")

    name: str = Field(default="", description="季名；语义空值为空串")
    air_date: date | None = Field(default=None, description="该季首播日期；NULL=未定档/未知")
    episode_count: int | None = Field(default=None, description="TMDB 宣称的集数；NULL=未知")

    # -- 季级展示（docs/design/metadata.md 2.3）-----------------------------
    overview: str | None = Field(default=None, description="季简介；NULL=TMDB 也没有")
    poster_path: str | None = Field(default=None, description="季海报 TMDB 相对路径")
    poster_file: str | None = Field(default=None, description="本地季海报资产相对路径")
