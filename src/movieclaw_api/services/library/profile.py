"""媒体库能力档案：形态 × 来源 → 一份不可变的能力声明（docs/design/library-other-kind.md 3.1）。

一个库由两个坐标定位：**形态**（``MediaKind``：movie / tv / video，描述结构）
与**身份来源**（``MediaSource``：tmdb / local，描述谁负责识别与刮削）。
两者的组合决定这个库怎么扫、怎么摆、怎么播、对外报什么类型。

为什么不按形态一个维度分叉：``video`` 单独看显得宽泛，是因为它本来就只是
半个坐标——"成人（番号）"在结构上是 ``movie`` 形态、只是来源不是 TMDB；
"动漫"是 ``tv`` 形态 + TMDB 来源、库名叫动漫。把来源做成维度之后，
将来接新来源（番号、豆瓣独有条目、Bangumi）就是本表加一行 + 一个识别策略
+ 一个刮削器，四条消费链（扫描、展示、播放、Jellyfin）不用再碰。

**所有消费方读能力位，不按枚举值分叉**：新增 ``kind == "movie"`` 这类字面
比较在评审里应被拒绝——每多一处就是将来每加一个类型要多改的一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from movieclaw_api.exceptions import BadRequestException
from movieclaw_db.models.media_item import MediaSource
from movieclaw_media.models import MediaKind

if TYPE_CHECKING:
    from movieclaw_db.models.library import Library


class IgnoreProfile(StrEnum):
    """扫描遍历的忽略口径。

    - ``SCRAPED``：影视库的全套规则——花絮/预告子目录、``-trailer`` 后缀、
      「花絮/预告片」关键词、``sample`` 子串。花絮不是独立作品，拿它们的
      文件名去 TMDB 搜必然搜出别的影片；
    - ``PLAIN``：本地内容库的口径——只挡隐藏目录、系统目录与主干精确等于
      ``sample`` 的文件。``婚礼花絮.mp4``、``clips/`` 正是家庭视频的常态，
      子串规则用在这里必伤（与 Jellyfin homevideos 库的匹配语义一致：
      精确/后缀，没有子串）。
    """

    SCRAPED = "scraped"
    PLAIN = "plain"


@dataclass(frozen=True)
class LibraryProfile:
    """一种库类型（形态 × 来源）的全部能力声明。"""

    kind: MediaKind
    source: str
    label: str  # 创建库时用户看到的名字
    scraped: bool  # 身份来自远程数据源：走识别链、刮削、写 NFO、定时刷新
    naming: bool  # 可按命名模板整理目录与文件名
    ignore_rules: IgnoreProfile
    write_nfo: bool
    subscribable: bool  # 可作为订阅/自动路由的目标库
    jellyfin_type: str  # 条目对外报的 Jellyfin 类型：Movie / Series / Video
    jellyfin_collection: str  # 库视图的 CollectionType：movies / tvshows / homevideos
    default_aspect: float  # 无主图时卡片的兜底长宽比（有主图按真实尺寸）

    @property
    def episodic(self) -> bool:
        """是否季集结构——这是形态的固有属性，不随来源变。"""
        return self.kind is MediaKind.TV

    @property
    def key(self) -> tuple[str, str]:
        return self.kind.value, self.source


_POSTER = 2 / 3
_THUMB = 16 / 9

PROFILES: dict[tuple[str, str], LibraryProfile] = {
    (MediaKind.MOVIE.value, MediaSource.TMDB): LibraryProfile(
        kind=MediaKind.MOVIE,
        source=MediaSource.TMDB,
        label="电影",
        scraped=True,
        naming=True,
        ignore_rules=IgnoreProfile.SCRAPED,
        write_nfo=True,
        subscribable=True,
        jellyfin_type="Movie",
        jellyfin_collection="movies",
        default_aspect=_POSTER,
    ),
    (MediaKind.TV.value, MediaSource.TMDB): LibraryProfile(
        kind=MediaKind.TV,
        source=MediaSource.TMDB,
        label="剧集",
        scraped=True,
        naming=True,
        ignore_rules=IgnoreProfile.SCRAPED,
        write_nfo=True,
        subscribable=True,
        jellyfin_type="Series",
        jellyfin_collection="tvshows",
        default_aspect=_POSTER,
    ),
    (MediaKind.VIDEO.value, MediaSource.LOCAL): LibraryProfile(
        kind=MediaKind.VIDEO,
        source=MediaSource.LOCAL,
        label="其他",
        scraped=False,
        naming=False,
        ignore_rules=IgnoreProfile.PLAIN,
        write_nfo=False,
        subscribable=False,
        jellyfin_type="Video",
        jellyfin_collection="homevideos",
        default_aspect=_THUMB,
    ),
}

# 形态的展示名（日志、清单文案共用）
KIND_LABELS: dict[MediaKind, str] = {
    MediaKind.MOVIE: "电影",
    MediaKind.TV: "剧集",
    MediaKind.VIDEO: "其他",
}


def kind_label(kind: MediaKind | str) -> str:
    value = kind if isinstance(kind, MediaKind) else MediaKind(kind)
    return KIND_LABELS[value]


def default_source_for(kind: MediaKind | str) -> str:
    """某形态建库时的默认身份来源：影视走 TMDB，其他走本地。"""
    value = kind.value if isinstance(kind, MediaKind) else kind
    for (k, source), _profile in PROFILES.items():
        if k == value:
            return source
    raise BadRequestException(f"不支持的媒体库类型：{value}")


def profile_for(kind: MediaKind | str, source: str | None = None) -> LibraryProfile:
    """按 (形态, 来源) 取能力档案；来源缺省取该形态的默认来源。

    组合不存在时抛中文 400——这是创建库时唯一会碰到它的地方；存量库的
    组合都是本表里的行。
    """
    value = kind.value if isinstance(kind, MediaKind) else kind
    resolved_source = source or default_source_for(value)
    profile = PROFILES.get((value, resolved_source))
    if profile is None:
        raise BadRequestException(
            f"不支持的媒体库类型组合：形态「{value}」× 来源「{resolved_source}」"
        )
    return profile


def profile_of(library: Library) -> LibraryProfile:
    """库行 → 能力档案（存量库的 source 已由迁移回填为 tmdb）。"""
    return profile_for(library.kind, library.source)


def capabilities_of(profile: LibraryProfile) -> dict:
    """导出给接口的能力位（前端按它分叉，不认字符串）。"""
    return {
        "scraped": profile.scraped,
        "episodic": profile.episodic,
        "naming": profile.naming,
        "subscribable": profile.subscribable,
        "write_nfo": profile.write_nfo,
        "default_aspect": round(profile.default_aspect, 4),
        "jellyfin_collection": profile.jellyfin_collection,
    }


def library_kind_options() -> list[dict]:
    """创建库弹窗的类型选项：形态 × 来源各一项。"""
    return [
        {
            "kind": profile.kind.value,
            "source": profile.source,
            "label": profile.label,
            "capabilities": capabilities_of(profile),
        }
        for profile in PROFILES.values()
    ]
