"""影视数据层的对外数据模型。

字段形态刻意对齐前端发现页的渲染需求（apps/web/lib/media-types.ts）：
后端先给出「布局」（行清单），前端据此撑起骨架，再按行拉取数据渐进填充；
每行内部仍是拿到即渲染，不在浏览器端做二次编排。命名沿用项目 API 惯例的
snake_case，前端在 lib/api/discover.ts 做一次 camelCase 映射。
"""

from __future__ import annotations

from enum import Enum, StrEnum

from pydantic import BaseModel, Field


class MediaKind(str, Enum):  # noqa: UP042 —— 改 StrEnum 会改变 str()/f-string 输出，牵连面大，维持现状
    """内容形态：电影 / 剧集 / 其他视频（docs/design/library-other-kind.md 3.1）。

    ``movie`` 与 ``tv`` 的取值与 TMDB 的路径段一致，在 ``source=tmdb`` 的
    识别/刮削路径里可直接拼接 URL；``video`` 是没有结构假设的单本视频
    （家庭录像、自录内容），只在本地来源下出现，永远不会进 TMDB 请求。
    形态描述结构，不描述题材也不描述来源。
    """

    MOVIE = "movie"
    TV = "tv"
    VIDEO = "video"


class MediaSource(StrEnum):
    """媒体数据来源；ID 只在同一来源内部唯一。"""

    TMDB = "tmdb"
    DOUBAN = "douban"


class MediaLibraryStatus(BaseModel):
    """发现卡片对应的轻量库存摘要。

    这是发现页与本地库存之间唯一共享的列表级契约：只表达是否存在在位
    文件及其聚合数量，不携带文件路径、介质规格或探测 JSON，避免海报墙
    查询扩大为逐条目明细读取。
    """

    media_item_id: int = Field(description="本地媒体条目 id，用于详情深链")
    library_count: int = Field(description="包含在位文件的媒体库数量")
    file_count: int = Field(description="在位文件数量")


class MediaLibraryLink(BaseModel):
    """发现详情跳转到一个本地媒体库条目所需的最小身份信息。"""

    library_id: int = Field(description="媒体库 id")
    library_name: str = Field(description="媒体库展示名称")
    media_item_id: int = Field(description="本地媒体条目 id")


class MediaCard(BaseModel):
    """一张海报卡片所需的全部字段（发现页列表项与 Hero 精选共用）。"""

    id: str = Field(description="TMDB 条目 ID（字符串形态，前端当作不透明键使用）")
    source: MediaSource = Field(default=MediaSource.TMDB, description="条目数据来源")
    type: MediaKind
    title: str = Field(description="中文标题（TMDB 无中文译名时为原名）")
    original_title: str = Field(description="原名（原语言）")
    year: int = Field(description="上映/首播年份")
    rating: float = Field(description="TMDB 评分（0~10，一位小数；0 表示暂无评分）")
    genres: list[str] = Field(default_factory=list, description="类型标签（中文，最多 3 个）")
    extent: str = Field(
        default="",
        description="规模：电影=片长、剧集=季数。TMDB 列表接口不含此字段，仅详情接口回填",
    )
    badges: list[str] = Field(
        default_factory=list,
        description="资源质量徽章（4K/HDR 等）。预留给后续站点资源匹配，当前恒为空",
    )
    overview: str = Field(default="", description="剧情简介（可能为空：小众条目无中文简介）")
    poster_url: str
    backdrop_url: str | None = Field(default=None, description="宽幅剧照，Hero 大横幅用")
    library_status: MediaLibraryStatus | None = Field(
        default=None,
        description="本地在位库存摘要；未精确命中或只有缺失台账时为 null",
    )


class MediaPersonDetail(BaseModel):
    """TMDB 影人档案与完整影视履历。

    ``credits`` 合并参演和幕后作品，并按 movie/tv + TMDB ID 去重；即使条目
    没有海报或上映日期也会保留，避免“全部作品”因展示素材不完整而漏项。
    """

    tmdb_person_id: int
    name: str
    avatar_url: str | None = Field(default=None, description="TMDB 头像；无照片为 null")
    credits: list[MediaCard] = Field(default_factory=list, description="TMDB 全部影视作品")


class MediaRow(BaseModel):
    """发现页里一行横滚海报（如「热门电影」「高分经典」）。"""

    id: str
    title: str
    ranked: bool = Field(default=False, description="是否为大数字排名行")
    items: list[MediaCard]


class MediaPage(BaseModel):
    """一页可继续向后加载的 TMDB 发现结果。"""

    id: str
    title: str
    ranked: bool = Field(default=False, description="是否为大数字排名行")
    items: list[MediaCard]
    page: int = Field(ge=1)
    total_pages: int = Field(ge=1)
    total_results: int = Field(ge=0)


class DiscoverRowStub(BaseModel):
    """布局里的一行占位（只有标识与标题，不含条目数据）。

    前端拿到布局后先按行清单撑起整页骨架，再逐行请求数据填充——
    这是发现页渐进加载的关键：不必等最慢的榜单，先到先渲染。
    """

    id: str
    title: str
    ranked: bool = Field(default=False, description="是否为大数字排名行")


class DiscoverLayout(BaseModel):
    """发现页布局（发现电影 / 发现剧集各一份）：纯配置，毫秒级返回。"""

    has_hero: bool = Field(description="是否有 Hero 大横幅（豆瓣视角没有）")
    rows: list[DiscoverRowStub]


class MediaSearchItem(BaseModel):
    """轻量搜索候选条目（豆瓣/TMDB 共用）；不伪造来源未提供的字段。"""

    id: str
    source: MediaSource
    title: str
    year: int | None = Field(
        default=None, description="上映/首播年份；豆瓣轻量搜索不提供，恒为 None"
    )
    type: MediaKind | None = Field(
        default=None, description="movie/tv；豆瓣轻量搜索不提供，恒为 None"
    )
    rating: float = Field(default=0, description="来源站评分；0 表示暂无评分")
    poster_url: str


class MediaCastMember(BaseModel):
    """演职员表的一位人物：姓名 + 可选角色 + 头像。

    发现页详情要按「演职员横滚条」呈现（与媒体库条目详情同一套版式），
    导演与演员都需要结构化头像和人物 ID；演员额外携带角色名。头像在数据源
    里常常缺失（小众条目、配音演员），前端按占位渲染，
    不必为此过滤掉这个人——名字与角色本身就是有效信息。
    """

    name: str = Field(description="人物姓名")
    role: str | None = Field(default=None, description="饰演角色；数据源未提供为空")
    avatar_url: str | None = Field(default=None, description="头像地址；数据源未提供为空")
    tmdb_person_id: int | None = Field(
        default=None,
        description="TMDB 影人 ID；有值时前端把这一格链到人物页。豆瓣来源没有此 id",
    )


class MediaFacts(BaseModel):
    """详情页「词条信息」卡的字段（豆瓣式条目档案）。"""

    directors: list[str] = Field(default_factory=list, description="导演（剧集为主创）")
    director_credits: list[MediaCastMember] = Field(
        default_factory=list,
        description="结构化导演/主创；头像或人物 ID 缺失时对应字段为空",
    )
    cast: list[MediaCastMember] = Field(
        default_factory=list,
        description="演职员（按数据源给出的主次顺序，最多 _CAST_LIMIT 位）",
    )
    country: str = Field(default="", description="制片地区")
    language: str = Field(default="", description="语言")
    released: str = Field(default="", description="上映/首播日期（ISO 格式）")
    network: str | None = Field(default=None, description="播出平台（仅剧集）")
    aliases: list[str] = Field(default_factory=list, description="别名/其他译名")
    source_url: str | None = Field(default=None, description="来源站条目地址")


class MediaImage(BaseModel):
    """一张剧照/海报：横滚条用预览图，灯箱看原图。"""

    preview_url: str = Field(description="缩略预览（剧照 w780 / 海报 w342）")
    full_url: str = Field(description="原图（original，灯箱全屏用）")
    width: int
    height: int


class MediaVideo(BaseModel):
    """详情页的一段预告片/花絮。

    TMDB 只给出 YouTube 的视频 key，**不提供可直接播放的视频流**，因此播放这一步
    依赖浏览器自身能连上 YouTube——服务端配的代理帮不上忙。封面图则不同：它是
    普通图片，前端统一走 /images/proxy 由服务端回源缓存，所以只要服务端能出网，
    即使浏览器连不上 YouTube，预告片卡片也照样完整展示并给出外链入口。
    """

    key: str = Field(description="YouTube 视频 ID（前端当作不透明键使用）")
    name: str = Field(description="视频标题，TMDB 原样给出（多为英文）")
    kind: str = Field(description="中文类型标签：预告片 / 先导预告 / 片段 / 花絮 / 幕后")
    thumbnail_url: str = Field(
        description="YouTube 封面图；4:3 带上下黑边，前端按 cover 裁切即得 16:9 画面"
    )
    embed_url: str = Field(description="内嵌播放地址（youtube-nocookie，不落跟踪 cookie）")
    watch_url: str = Field(description="YouTube 站内地址，供无法内嵌时外链打开")


class MediaCollection(BaseModel):
    """电影所属系列及其全部作品；剧集和不属于系列的电影没有此字段。"""

    id: str = Field(description="TMDB collection ID")
    name: str
    items: list[MediaCard] = Field(default_factory=list)


class MediaDetail(BaseModel):
    """条目详情：卡片字段（详情接口回填了 extent 等）+ 词条信息 + 图片 + 相似推荐。"""

    card: MediaCard
    facts: MediaFacts
    videos: list[MediaVideo] = Field(
        default_factory=list, description="预告片与花絮（正式预告在前）；豆瓣来源恒为空"
    )
    backdrops: list[MediaImage] = Field(default_factory=list, description="剧照（16:9 宽幅）")
    posters: list[MediaImage] = Field(
        default_factory=list, description="海报（2:3 竖版，配置语言优先）"
    )
    collection: MediaCollection | None = Field(
        default=None,
        description="电影所属系列的完整作品清单；不属于系列或剧集为 null",
    )
    related: list[MediaCard] = Field(default_factory=list, description="TMDB 推荐的相似作品")
    library_links: list[MediaLibraryLink] = Field(
        default_factory=list,
        description="主条目可跳转的本地媒体库入口；相似推荐不带此字段",
    )
