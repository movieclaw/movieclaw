"""媒体库条目详情装配与真实删除（条目详情页的后端）。

三块职责：

1. **详情装配** ``build_item_detail``：把一个条目在库内的全部信息拼成一页——
   基本信息（MediaItem）、本地刮削元数据（NFO：简介/评分/片长/演职员）、
   本地美术图（poster/fanart 优先于 TMDB 图床）、逐文件的真实介质规格
   （ffprobe：分辨率/音轨/内封字幕）与外挂字幕。旧台账没探测过音轨的，
   **不在浏览时补探**（浏览不碰媒体文件本体，云盘挂载上读文件就是流量
   与延迟）——前端提示用户重新扫描，由扫描的补探阶段统一回填。
2. **本地美术图定位** ``local_item_artwork``：条目目录下按 Kodi/Jellyfin 惯例
   命名的图片文件（规则只在 ``artwork.py`` 维护一份，Jellyfin 图片接口与
   本地条目的资产生成同源）；找到即由 /libraries/.../artwork 接口直接回吐，
   没有时前端退回 TMDB 图床。
3. **真实删除** ``delete_item_files``：条目详情页「删除」的语义是**从磁盘
   彻底删除**（与其他一切"只删台账"的接口截然相反）——整个条目目录
   （视频+NFO+海报+字幕）一起清掉，不留刮削残渣。唯一的克制：目录里
   混有**其他条目**的文件时退化为只删本条目的文件及其同名附属文件。
   ``delete_single_file`` 是它的文件级姊妹：只删一个版本/一集的文件及
   同名附属（多版本洗版、删某集重下），最后一个文件时升级为整条目删除。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple

from sqlalchemy import Integer, and_, func, not_, nullslast, or_, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.library import (
    EpisodeView,
    FacetValueView,
    LibraryFacetsView,
    LibraryGalleryGroupView,
    LibraryGalleryImageView,
    LibraryInventorySummaryView,
    LibraryItemView,
    LibraryRecentAdditionView,
    LibraryRelaxView,
    RelaxSuggestionView,
    derive_air_status,
)
from movieclaw_api.services.library.access import ContentLimit
from movieclaw_api.services.library.artwork import (
    ART_EXTS,
    DirListing,
    dir_listing,
    find_artwork,
)
from movieclaw_api.services.library.bluray import (
    enrich_spec_with_clpi,
    read_clpi_languages,
    streams_have_clpi_metadata,
)
from movieclaw_api.services.library.content_rating import ratings_at_or_below
from movieclaw_api.services.library.layout import STRM_EXT, entry_dir_of
from movieclaw_api.services.library.nfo import (
    EntryMetadata,
    NfoActor,
)
from movieclaw_api.services.library.sort_key import title_initial, title_sort_key
from movieclaw_api.services.library.thumbs import primary_aspect
from movieclaw_api.services.media_probe import (
    PROBE_SCHEMA_VERSION,
    note_probe_failure,
    note_probe_success,
    probe_media,
    probe_retry_due,
)
from movieclaw_api.services.media_scrape import asset_version, file_version
from movieclaw_api.services.scrape_config import effective_language, scrape_setting_for_item
from movieclaw_db.models import (
    FileState,
    Library,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    MediaSeason,
    PlaybackState,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.genres import country_label, genre_label
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.library_items")

# TMDB 展示信息（条目简介/演职员、分季集名/剧照）的持久缓存：详情页
# 每次访问都实时打 TMDB 是首屏慢的主因之一。展示信息变化极慢——新鲜期
# 内直接命中（毫秒级），过期后先回旧值、后台静默刷新（SWR），跨重启不丢
_DISPLAY_CACHE_FRESH_TTL = 24 * 3600.0
_DISPLAY_CACHE_STALE_TTL = 30 * 24 * 3600.0

_display_cache = None


def _get_display_cache():
    global _display_cache
    if _display_cache is None:
        from movieclaw_cache import SwrCache
        from movieclaw_db.stores import SqlCacheStore

        _display_cache = SwrCache(SqlCacheStore(), "library_display")
    return _display_cache


# ---------------------------------------------------------------------------
# 条目目录与本地美术图
# ---------------------------------------------------------------------------

# 美术图扩展名与命名规则见 artwork.py（这里只保留别名给字幕/附属文件判定用）
_ART_EXTS = ART_EXTS

# 外挂字幕扩展名（同名或"同名.语言"命名，整理器的 sidecar 同一口径）
_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".sup", ".vtt"}

# 补探过程中每探完这么多个文件提交一次：整库补探是小时级的活，不能攒成
# 一个大事务（中途中断就全白探了），也不必一个一个提交
_PROBE_COMMIT_EVERY = 32


def find_local_artwork(
    entry_dir: Path, kind: str, own_files: list[Path], *, cache: DirListing | None = None
) -> Path | None:
    """条目目录下的本地美术图；``kind``: poster / fanart / thumb。找不到返回 None。

    规则见 artwork.find_artwork：文件自己的 ``<主干>-poster`` 精确匹配优先，
    目录级 ``poster.jpg`` 只在目录归这个条目时才认（混放目录不串图）。
    """
    return find_artwork(entry_dir, kind, own_files, cache=cache)


def _external_subtitles_many(
    videos: list[Path], cache: DirListing | None = None
) -> dict[Path, list[str]]:
    """一批视频各自旁边的外挂字幕文件名（同名前缀匹配：
    "片名.chs.srt" 算 "片名.mkv" 的）。

    **按目录批处理**而不是逐文件列目录：原本每个文件列一次所在目录，同一个
    季目录要被列 N 遍（每遍还要对目录里的每个文件 stat 一次判断是不是文件），
    于是成本是 O(文件数 × 目录内文件数)——**平方**。改成按目录归组、每个目录
    只列一次，成本变成 O(目录数)，与文件数无关，结果完全一致。

    平方那一项在长剧上是会爆的。实测（``scripts/perf/bench_disk_io.py``，
    一次「打开剧集详情页 + 拉一季分集」的系统调用数）：

        30 集的剧     1294 → 22
        400 集的剧   52565 → 64   （该页耗时 590ms → 111ms，SSD 上）

    机械盘 NAS 上这些 stat 多数落在冷 inode 上，每一次都是一次真实寻道，
    差距只会比 SSD 上更大。
    """
    by_dir: dict[Path, list[Path]] = {}
    for video in videos:
        by_dir.setdefault(video.parent, []).append(video)
    found: dict[Path, list[str]] = {video: [] for video in videos}
    for directory, members in by_dir.items():
        listing = dir_listing(directory, cache)
        if not listing:
            continue
        subtitles = [
            name for name in sorted(listing) if Path(name).suffix.lower() in _SUBTITLE_EXTS
        ]
        if not subtitles:
            continue
        for video in members:
            stem = video.stem.lower()
            found[video] = sorted(
                listing[name].name
                for name in subtitles
                if (sub_stem := Path(name).stem) == stem or sub_stem.startswith(stem + ".")
            )
    return found


# ---------------------------------------------------------------------------
# 详情装配
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 海报墙聚合（单库条目列表）
# ---------------------------------------------------------------------------


class _FileFacts(NamedTuple):
    """海报墙聚合用到的十个台账字段（顺序与查询列一致）。

    代码读起来与整行取时一模一样，只是不再拖着四十列（含三列 JSON）走。"""

    library_id: int
    id: int
    season_number: int
    episode_number: int
    size_bytes: int
    resolution: str | None
    state: str
    created_at: datetime
    added_batch_id: str | None
    # 尚未探出介质规格（audio_streams IS NULL 在 SQL 里算好，不取 JSON 本体）
    unprobed: bool


_PIXEL_SIZE = re.compile(r"^(\d+)x(\d+)$")


def _pixel_size_of(files: list[_FileFacts]) -> tuple[int | None, int | None]:
    """台账行记的原图像素尺寸（图片库入账时探测得到，形如 ``4032x3024``）；
    视频的规格是 ``1080p`` 这类，不匹配即 (None, None)。"""
    for facts in files:
        match = _PIXEL_SIZE.match(facts.resolution or "")
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def _build_inventory_summary(
    units: set[tuple[int, int]],
    season_episode_counts: dict[int, int | None],
) -> LibraryInventorySummaryView | None:
    """按本库在位单元与 TMDB 季结构计算 hover 完整度，不把“连续”猜成“全”。

    正季与特别篇不混算：只要有正季，摘要就聚焦正季；只有 S00 时才展示
    “特别篇”。季度完整表示已覆盖 TMDB 已知的所有正季，集数完整则要求
    当前摘要覆盖的每一季都精确拥有 E01..EN。任何官方集数未知时都不写“全”。
    """
    regular_by_season: dict[int, set[int]] = {}
    specials: set[int] = set()
    for season_number, episode_number in units:
        if episode_number <= 0:
            continue
        if season_number > 0:
            regular_by_season.setdefault(season_number, set()).add(episode_number)
        elif season_number == 0:
            specials.add(episode_number)

    if regular_by_season:
        by_season = regular_by_season
        season_count = len(by_season)
        known_regular_seasons = {
            season_number for season_number in season_episode_counts if season_number > 0
        }
        all_seasons_owned = bool(known_regular_seasons) and (
            set(by_season) == known_regular_seasons
        )
    elif specials:
        by_season = {0: specials}
        season_count = 0
        all_seasons_owned = False
    else:
        return None

    total_episode_count: int | None = 0
    all_episodes_owned = True
    for season_number, episodes in by_season.items():
        expected = season_episode_counts.get(season_number)
        if expected is None or expected <= 0:
            total_episode_count = None
            all_episodes_owned = False
            break
        total_episode_count += expected
        if episodes != set(range(1, expected + 1)):
            all_episodes_owned = False

    return LibraryInventorySummaryView(
        season_count=season_count,
        episode_count=sum(len(episodes) for episodes in by_season.values()),
        season_number=next(iter(by_season)) if len(by_season) == 1 else None,
        total_episode_count=total_episode_count,
        all_seasons_owned=all_seasons_owned,
        all_episodes_owned=all_episodes_owned,
    )


WallSort = Literal[
    "title",
    "added_at",
    "release_date",
    # 上映**正序**。墙上的默认方向是倒序（新的在前，那是浏览的语义），
    # 但**系列要按上映顺序看**——《死亡圣器(上)》排在《混血王子》前面这种事，
    # 用户会当成 bug。它不进筛选栏的排序下拉，只作为系列合集的 sort 存在
    "release_date_asc",
    "probing",
    # 以下四档随筛选一起加（docs/design/library-filtering.md 3.1「排序」）。
    # 方向的取舍：评分/体积/最近观看都是「大的在前」，唯独片长是**升序**
    # ——「今晚只有 90 分钟」是真实诉求，「最长的在前」几乎没人要。
    "rating",
    "runtime",
    "size",
    "last_played",
]

#: 排序方向（2026-09-11 起可切换）。不给方向 = 该档的**自然方向**（见 ``_NATURAL_ASC``），
#: 与加方向之前逐字等价——老调用方、合集的 sort、首页「最近添加」都不受影响。
WallOrder = Literal["asc", "desc"]

#: 各档的自然方向是不是升序：标题 A→Z、片长短→长、上映正序档是升序；
#: 其余都是「大的 / 新的 / 近的在前」。补探序是临时接管，方向无意义
_NATURAL_ASC: dict[str, bool] = {
    "title": True,
    "added_at": False,
    "release_date": False,
    "release_date_asc": True,
    "probing": True,
    "rating": False,
    "runtime": True,
    "size": False,
    "last_played": False,
}


def _ascending(sort: WallSort, order: WallOrder | None) -> bool:
    """这一次按升序排还是降序排：没指定方向就用该档的自然方向。"""
    return _NATURAL_ASC[sort] if order is None else order == "asc"


# ── 筛选（docs/design/library-filtering.md 3.1/3.2）──────────────────────
# 维内 OR、维间 AND。取值与合集规则同构（library-routing.md 1.1 的
# [{field, op, values}]），所以「筛完存为合集」是一次纯粹的形状转换。

#: 观看状态。前三者是一个**划分**：任何条目恰好落在其中一档，三档计数之和
#: 等于总数（facet 计数因此永远对得上）。favorite 与它们正交，单选而已。
WatchFilter = Literal["unwatched", "watching", "played", "favorite"]

#: 年代档 → 年份闭区间；None 表示不设下界。缺年份的条目（release_date 与
#: media_item.year 都为空）不属于任何一档——「未知年份」不是年代，硬塞进
#: 「更早」是编数据。它们只在不筛年代时出现。
#: 片长档 → 分钟闭区间；None 表示不设该侧界。缺片长的条目不属于任何档
#: （同「未知年份」，见 _DECADE_RANGES）。
_RUNTIME_RANGES: dict[str, tuple[int | None, int | None]] = {
    "lte60": (None, 60),
    "60to90": (60, 90),
    "90to120": (90, 120),
    "gt120": (120, None),
}

_DECADE_RANGES: dict[str, tuple[int | None, int]] = {
    "2020s": (2020, 2029),
    "2010s": (2010, 2019),
    "2000s": (2000, 2009),
    "1990s": (1990, 1999),
    "earlier": (None, 1989),
}


@dataclass(frozen=True)
class LibraryFilter:
    """单库墙的收窄条件。

    全空 = 不收窄（``is_empty`` 为真时 ``_filter_subquery`` 返回 None，
    调用方原样不动，零成本）。字段按维度分，**维内 OR、维间 AND**——
    勾「动画」再勾「科幻」是两者都要看到，再勾「日本」才是收窄。
    """

    # —— 一级（常驻 chips，找片）——
    genres: tuple[int, ...] = ()  # TMDB genre id（语言无关，见 metadata.genre_ids）
    countries: tuple[str, ...] = ()  # ISO 3166-1 二字码
    decades: tuple[str, ...] = ()  # _DECADE_RANGES 的键
    watch: WatchFilter | None = None  # 按观看者算，需要 member_id
    # —— 二级·找片（作品是什么样的，来自刮削档案）——
    rating_gte: float | None = None  # 评分下限
    runtimes: tuple[str, ...] = ()  # _RUNTIME_RANGES 的键
    languages: tuple[str, ...] = ()  # 原始语言码
    # —— 二级·查库（文件是什么规格，来自库存台账）——
    resolutions: tuple[str, ...] = ()  # 2160p / 1080p / …
    hdr: bool | None = None  # True=只看 HDR；False=只看 SDR
    #: 库存状态：missing=有文件失联 / unscraped=没刮到档案
    stock: tuple[str, ...] = ()
    #: 作品系列（``media_metadata.series_key``）。**界面上没有这一维的下拉**——
    #: 一个库几百个系列，下拉根本没法用，合集才是它正确的呈现形态。它存在是
    #: 因为系列合集就是一条规则驱动的合集，规则正是「series_key = X」
    series_keys: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.genres
            or self.countries
            or self.decades
            or self.watch
            or self.rating_gte is not None
            or self.runtimes
            or self.languages
            or self.resolutions
            or self.hdr is not None
            or self.stock
            or self.series_keys
        )


def _last_played_at(member_id: int):
    """本人在该条目上的最近一次播放活动（标量子查询）。

    不 join playback_state：那张表按 (成员, 条目, 季, 集) 一行，join 会把
    一部 200 集的剧炸成 200 行，再靠 group_by 收回来——子查询直接给一个值。
    """
    return (
        select(func.max(PlaybackState.last_played_at))
        .where(
            PlaybackState.media_item_id == MediaItem.id,
            PlaybackState.member_id == member_id,
        )
        .scalar_subquery()
    )


def _year_expr():
    """条目的年份：优先刮削档案的上映/首播日期，回落 media_item.year。

    两者都为空 = 未知年份，任何年代档都不命中（见 _DECADE_RANGES 注释）。
    """
    return func.coalesce(
        func.cast(func.strftime("%Y", MediaMetadata.release_date), Integer),
        MediaItem.year,
    )


def _json_any_of(column, values: Sequence) -> Any:
    """JSON 数组列与给定取值有交集——SQLite 的 ``json_each`` 展开后 IN。

    ``genre_ids`` / ``origin_countries`` 都是小数组（个位数元素），展开的代价
    可以忽略；相比在 Python 里把整库档案读回来过滤，它让筛选留在 SQL 里、
    与分页和计数共用同一条路径。
    """
    each = func.json_each(column).table_valued("value")
    return select(1).select_from(each).where(each.c.value.in_(list(values))).exists()


def _watch_clause(watch: WatchFilter, member_id: int):
    """观看状态的判定（按 member_id 隔离，与 playback_state 的成员维度一致）。

    「已看完」对剧集是近似：真正的"看完"要对齐 TMDB 全集结构逐集比对，
    代价与收益不成比例。这里用的口径是「有看完的单元，且没有看到一半的
    单元」——对电影精确，对剧集与用户口中的"看完了"足够接近，且保证
    未看/在看/已看完三档是一个划分。
    """
    mine = (PlaybackState.media_item_id == MediaItem.id, PlaybackState.member_id == member_id)

    def _exists(*conds):
        return select(1).select_from(PlaybackState).where(*mine, *conds).exists()

    watching = _exists(PlaybackState.position_ms > 0, PlaybackState.played.is_(False))
    played = _exists(PlaybackState.played.is_(True))
    if watch == "watching":
        return watching
    if watch == "played":
        return and_(not_(watching), played)
    if watch == "unwatched":
        return and_(not_(watching), not_(played))
    # favorite：条目级收藏落在哨兵单元上（剧 (-1,-1) / 电影 (0,0)），
    # 与 services/playback/marks.item_favorite_unit 同一份约定
    is_tv = MediaItem.kind == MediaKind.TV.value
    return _exists(
        PlaybackState.is_favorite.is_(True),
        or_(
            and_(is_tv, PlaybackState.season_number == -1, PlaybackState.episode_number == -1),
            and_(not_(is_tv), PlaybackState.season_number == 0, PlaybackState.episode_number == 0),
        ),
    )


def _filter_subquery(
    filters: LibraryFilter | None,
    member_id: int | None,
    skip: str | None = None,
    library_id: int | None = None,
    content_limit: ContentLimit | None = None,
):
    """命中筛选条件的 ``media_item_id`` 子查询；不收窄时返回 None。

    **这是整个筛选功能在服务端唯一的收窄点。** 做成子查询而不是往各个排序
    分支上加 WHERE，是因为那四个分支 join 的表各不相同（按标题只 join
    media_item、按内容时间还要 outerjoin media_metadata、最近添加谁都不 join）；
    收敛成一句 ``media_item_id IN (…)`` 之后，排序、分页、索引条、图廊全都
    不必知道筛选的存在。

    ``skip`` 排除某一个维度自身的条件——算某维的 facet 计数时必须这么做，
    否则勾了「动画」之后其他类型全变 0，多选就废了（library-filtering.md 3.3）。
    """
    limited = content_limit is not None and not content_limit.unrestricted
    if (filters is None or filters.is_empty) and not limited:
        return None
    # 只有分级约束、没有筛选条件时 filters 可能是 None：给一份全空的，
    # 下面的每一条都自然不成立，省掉十几个 `filters is not None and`
    filters = filters or LibraryFilter()
    conds = []
    if filters.genres and skip != "genres":
        conds.append(_json_any_of(MediaMetadata.genre_ids, filters.genres))
    if filters.countries and skip != "countries":
        conds.append(_json_any_of(MediaMetadata.origin_countries, filters.countries))
    if filters.decades and skip != "decades":
        year = _year_expr()
        spans = [
            (year <= hi) if lo is None else and_(year >= lo, year <= hi)
            for lo, hi in (_DECADE_RANGES[d] for d in filters.decades if d in _DECADE_RANGES)
        ]
        if spans:
            conds.append(or_(*spans))
    if filters.watch and skip != "watch":
        # 观看状态是按人算的；不认人的调用（内部任务、CLI）落到超管哨兵 0
        conds.append(_watch_clause(filters.watch, member_id or 0))
    if filters.rating_gte is not None and skip != "rating_gte":
        conds.append(MediaMetadata.vote_average >= filters.rating_gte)
    if filters.runtimes and skip != "runtimes":
        spans = []
        for key in filters.runtimes:
            if key not in _RUNTIME_RANGES:
                continue
            lo, hi = _RUNTIME_RANGES[key]
            parts = [MediaMetadata.runtime_minutes.is_not(None)]  # type: ignore[union-attr]
            if lo is not None:
                parts.append(MediaMetadata.runtime_minutes > lo)
            if hi is not None:
                parts.append(MediaMetadata.runtime_minutes <= hi)
            spans.append(and_(*parts))
        if spans:
            conds.append(or_(*spans))
    if filters.languages and skip != "languages":
        conds.append(MediaMetadata.original_language.in_(filters.languages))
    if filters.resolutions and skip != "resolutions":
        conds.append(_file_exists(library_id, LibraryFile.resolution.in_(filters.resolutions)))
    if filters.hdr is not None and skip != "hdr":
        clause = _file_exists(library_id, LibraryFile.hdr.is_not(None))  # type: ignore[union-attr]
        conds.append(clause if filters.hdr else not_(clause))
    if filters.series_keys and skip != "series_keys":
        conds.append(MediaMetadata.series_key.in_(filters.series_keys))
    if filters.stock and skip != "stock":
        spans = []
        if "missing" in filters.stock:
            # 有文件失联：台账还在、盘上没了（missing_since 非空）
            spans.append(
                _file_exists(library_id, LibraryFile.missing_since.is_not(None))  # type: ignore[union-attr]
            )
        if "unscraped" in filters.stock:
            # 没刮到档案：详情页只能降级展示，也是「元数据刷新」该处理的那批
            spans.append(MediaMetadata.scraped_at.is_(None))
        if spans:
            conds.append(or_(*spans))
    if content_limit is not None and not content_limit.unrestricted:
        # **强制收窄**，与上面那些"用户自己选的"条件不同：它不受 skip 影响
        # （facet 算某一维时排除的是那一维自己的条件，不是观看者的约束），
        # 也不会被"清空筛选"清掉。判定收口在 access.content_limit_for()
        allowed = MediaMetadata.content_rating.in_(  # type: ignore[union-attr]
            ratings_at_or_below(content_limit.max_age or 0)
        )
        if content_limit.allow_unrated:
            # 没有档案行时 outerjoin 出来也是 NULL，这一条同时覆盖两种"未分级"
            allowed = or_(allowed, MediaMetadata.content_rating.is_(None))  # type: ignore[union-attr]
        conds.append(allowed)
    if not conds:
        return None
    return (
        select(MediaItem.id)
        .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)  # type: ignore[arg-type]
        .where(and_(*conds))
    )


def _file_exists(library_id: int | None, *conds):
    """本库内存在满足条件的文件——画质/HDR/失联这类**文件级**条件的判定。

    必须限定 ``library_id``：同一部片散在两个库时，「本库有没有 4K」问的是
    这个库，不是全世界。不给 library_id（内部调用）则跨库判定。
    """
    where = [LibraryFile.media_item_id == MediaItem.id, *conds]
    if library_id is not None:
        where.append(LibraryFile.library_id == library_id)
    return select(1).select_from(LibraryFile).where(*where).exists()


def _narrow(
    filters: LibraryFilter | None,
    member_id: int | None,
    skip: str | None = None,
    library_id: int | None = None,
    content_limit: ContentLimit | None = None,
) -> tuple:
    """筛选收窄的 WHERE 片段（**含观看者的内容分级约束**）。

    不收窄时是空元组，调用处 ``*_narrow(...)`` 展开后等于什么都没加——
    未筛选、且观看者不受限的路径与改造前逐字相同，不多一次 join、
    不多一个子查询。

    分级约束走同一处的理由：海报墙、筛选 facet、索引条、合集成员求值全都
    经过 ``_narrow``，收窄点只有一个，就不存在"某一处忘了加"这种洞——
    而这个功能一旦漏一处就是假的（孩子照样能从那一处看到）。
    """
    subq = _filter_subquery(filters, member_id, skip, library_id, content_limit=content_limit)
    return () if subq is None else (LibraryFile.media_item_id.in_(subq),)  # type: ignore[union-attr]


# 海报墙口径：confirmed=正式条目（默认，首页/搜索/索引同口径）；provisional=
# 影视库里认不出、按文件名挂着的临时条目（库页单独一段展示）。其他库没有
# 临时条目，两口径下 provisional 恒空
WallIdentity = Literal["confirmed", "provisional"]


def _identity_clause(identity: WallIdentity):
    """临时条目的判别只看文件行的 ``unidentified_code``：挂了原因的行就是临时的。"""
    column = LibraryFile.unidentified_code
    return column.is_(None) if identity == "confirmed" else column.is_not(None)  # type: ignore[union-attr]


def _wall_scope(
    library_id: int, identity: WallIdentity = "confirmed", only_item_id: int | None = None
):
    """海报墙的成员口径：本库、挂了条目、**在架**（没进回收站）、指定身份档。

    ``only_item_id`` 把候选集收窄成**一个条目**——"这部片在不在这个合集里"
    走的就是这条路。它收窄的是"谁是候选"，不是"要满足什么条件"，所以
    放在这里而不是 ``LibraryFilter``：后者是用户能表达的维度，这个不是。
    反查合集因此与正查成员**逐字同一条查询**，不存在两处答案不一致。

    ``on_shelf()`` 这一条是补上去的。在此之前墙的成员查询完全不看文件状态，
    于是用户把最后一个文件移进回收站之后，库卡片上的作品数已经减一
    （``refresh_stats`` 只算在位文件），墙上那部片却还摆着——同一个库两个数字。

    口径只能有一处：墙、筛选 facet、合集、Jellyfin 兼容层现在全部经过这里，
    「墙上有什么，合集里就有什么」这条结构性保证因此仍然成立。失联的片仍然
    在架（见 ``LibraryFile.on_shelf`` 的说明），回收站里的片恢复之后自己回到
    墙上——成员永远是现算的，没有补偿逻辑要写。
    """
    scope = (
        LibraryFile.library_id == library_id,
        LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
        LibraryFile.on_shelf(),
        _identity_clause(identity),
    )
    return scope if only_item_id is None else (*scope, LibraryFile.media_item_id == only_item_id)


async def _titles_sorted(
    session: AsyncSession,
    library_id: int,
    identity: WallIdentity = "confirmed",
    filters: LibraryFilter | None = None,
    member_id: int | None = None,
    content_limit: ContentLimit | None = None,
    only_item_id: int | None = None,
) -> list[tuple[int, str]]:
    """本库全部条目的 (id, 标题)，按拼音序排好。

    按标题排序不走 SQL：SQLite 对中文按码点排，出来的顺序对用户没有意义
    （详见 sort_key 模块）。这里只取一列标题排序，本页的聚合仍只算 limit
    个条目——贵的是聚合，不是排一列标题。索引条（A-Z 分档）与按标题分页
    共用这份排好的名单，口径天然一致。
    """
    rows = (
        await session.execute(
            select(LibraryFile.media_item_id, MediaItem.title)
            .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .where(
                *_wall_scope(library_id, identity, only_item_id),
                *_narrow(filters, member_id, library_id=library_id, content_limit=content_limit),
            )
            .distinct()
        )
    ).all()
    # id 收尾：拼音串相同（同名不同条目）时顺序必须稳定，否则翻页会重复或漏项
    return sorted(
        ((i, t) for i, t in rows if i is not None), key=lambda r: (title_sort_key(r[1]), r[0])
    )


async def build_library_index(
    session: AsyncSession,
    library_id: int,
    sort: WallSort = "title",
    *,
    filters: LibraryFilter | None = None,
    member_id: int | None = None,
    content_limit: ContentLimit | None = None,
    order: WallOrder | None = None,
) -> list[tuple[str, int, int]]:
    """海报墙跳转索引：[(档, 条目数, 起始 offset)]，只回非空档。

    - ``sort=title``：按标题排序下的首字母分档（A-Z / #）；
    - ``sort=rating``：评分档（9+ / 8+ / 7+ / 更低 / 未评分）；
    - ``sort=release_date``：按内容时间倒序下的月份分档（``2026-08``），缺日期的
      归到 ``未知`` 档并排在最后——图片库/其他库的时间线靠它按月分组与跳转
      （docs/design/library-photo-kind.md 2.6）。

    起始 offset 就是海报墙 ``?sort=<同一排序>&offset=`` 的取值——前端点一下
    档名即可跳到该档第一格；两种排序与分页共用同一份排序，口径天然一致。

    ``order`` 与墙的 ``order`` 必须是同一个值：档位是在排好的序列上就地分段的，
    倒过来排，档的先后与 offset 就一起倒过来（Z→A、低分档在前），不需要另算。
    """
    buckets: list[tuple[str, int, int]] = []
    if sort == "rating":
        # 与墙读同一份有序名单：档位是在已排好的序列上就地分段，
        # 因此点档名拿到的 offset 一定指向该档第一格
        ids = await _wall_page_ids(
            session,
            library_id,
            "rating",
            None,
            0,
            filters=filters,
            member_id=member_id,
            content_limit=content_limit,
            order=order,
        )
        scored = dict(
            (
                await session.execute(
                    select(MediaMetadata.media_item_id, MediaMetadata.vote_average).where(
                        MediaMetadata.media_item_id.in_(ids)  # type: ignore[attr-defined]
                    )
                )
            ).all()
        )
        for index, item_id in enumerate(ids):
            score = scored.get(item_id)
            if score is None:
                label = "未评分"
            elif score >= 9:
                label = "9+"
            elif score >= 8:
                label = "8+"
            elif score >= 7:
                label = "7+"
            else:
                label = "更低"
            if buckets and buckets[-1][0] == label:
                head, count, start = buckets[-1]
                buckets[-1] = (head, count + 1, start)
            else:
                buckets.append((label, 1, index))
        return buckets
    if sort == "release_date":
        ids = await _wall_page_ids(
            session,
            library_id,
            "release_date",
            None,
            0,
            filters=filters,
            member_id=member_id,
            content_limit=content_limit,
            order=order,
        )
        dated = dict(
            (
                await session.execute(
                    select(MediaMetadata.media_item_id, MediaMetadata.release_date).where(
                        MediaMetadata.media_item_id.in_(ids)  # type: ignore[attr-defined]
                    )
                )
            ).all()
        )
        for index, item_id in enumerate(ids):
            released = dated.get(item_id)
            label = released.strftime("%Y-%m") if released else "未知"
            if buckets and buckets[-1][0] == label:
                head, count, start = buckets[-1]
                buckets[-1] = (head, count + 1, start)
            else:
                buckets.append((label, 1, index))
        return buckets
    ordered = await _titles_sorted(
        session, library_id, "confirmed", filters, member_id, content_limit
    )
    if not _ascending("title", order):
        ordered.reverse()
    for index, (_, title) in enumerate(ordered):
        initial = title_initial(title)
        if buckets and buckets[-1][0] == initial:
            head, count, start = buckets[-1]
            buckets[-1] = (head, count + 1, start)
        else:
            buckets.append((initial, 1, index))
    return buckets


#: 观看维度的四个候选值与展示名（前三者是一个划分，见 WatchFilter）
_WATCH_LABELS: list[tuple[WatchFilter, str]] = [
    ("unwatched", "未看"),
    ("watching", "在看"),
    ("played", "已看完"),
    ("favorite", "我收藏的"),
]


def _facet_scope(
    library_id: int,
    filters: LibraryFilter | None,
    member_id: int | None,
    skip: str,
    content_limit: ContentLimit | None = None,
):
    """算某一维 facet 时的库内范围：本库、已识别、其他维度的条件都算上。

    观看者的分级约束**不受 skip 影响**：skip 排除的是"这一维自己的筛选条件"，
    而分级不是用户选的。面板上的计数因此与墙上真能看到的数量一致。
    """
    return (
        *_wall_scope(library_id),
        *_narrow(filters, member_id, skip=skip, library_id=library_id, content_limit=content_limit),
    )


def _with_selected(rows, selected: tuple) -> list[FacetValueView]:
    """分组结果 → 候选列表，并补齐当前选中但一条都没数到的取值（计数 0）。

    这些维度的取值是**从数据里长出来的**（语言码、分辨率），所以别的维度一
    收窄，用户勾着的那个值可能整个消失——下拉里看不见也就取消不掉。
    """
    counts = [(str(v), int(c)) for v, c in rows if v is not None]
    present = {value for value, _ in counts}
    counts.extend((str(v), 0) for v in selected if str(v) not in present)
    return [
        FacetValueView(value=value, label=value, count=count)
        for value, count in sorted(counts, key=lambda r: (-r[1], r[0]))
    ]


async def _json_facet(
    session: AsyncSession,
    column,
    library_id: int,
    filters: LibraryFilter | None,
    member_id: int | None,
    skip: str,
    selected: tuple = (),
    content_limit: ContentLimit | None = None,
) -> list[tuple[str, int]]:
    """JSON 数组列的取值分布：展开后按值分组数条目（类型、地区共用）。

    ``selected`` 里的取值**一定出现在结果里**（数不到就补一条 0）。不补的话，
    别的维度一收窄，用户自己勾着的那个值会从本维的候选里消失：下拉里看不见它，
    也就取消不掉；条件行找不到它的展示名，只能把裸值印出来（「科幻」变成
    「878」）。为 0 的候选照常返回本来就是这里的约定，选中的更不能少。
    """
    each = func.json_each(column).table_valued("value")
    rows = (
        await session.execute(
            select(each.c.value, func.count(func.distinct(LibraryFile.media_item_id)))
            .select_from(LibraryFile)
            .join(MediaMetadata, MediaMetadata.media_item_id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .join(each, true())
            .where(*_facet_scope(library_id, filters, member_id, skip, content_limit=content_limit))
            .group_by(each.c.value)
        )
    ).all()
    counts = [(str(value), count) for value, count in rows if value is not None]
    present = {value for value, _ in counts}
    counts.extend((str(value), 0) for value in selected if str(value) not in present)
    return counts


async def _bucket_facet(
    session: AsyncSession,
    library_id: int,
    filters: LibraryFilter | None,
    member_id: int | None,
    skip: str,
    options: list[tuple[str, str, LibraryFilter]],
    content_limit: ContentLimit | None = None,
) -> list[FacetValueView]:
    """一组固定档位的计数：逐档带着「本档条件」重数一次。

    档位不是从数据里长出来的（评分/片长/库存状态都是人定的分界），所以不能
    像类型/地区那样 group by，只能逐档数。每档一条带索引的 COUNT，档位个位数。
    """
    out: list[FacetValueView] = []
    for value, label, probe in options:
        count = (
            await session.execute(
                select(func.count(func.distinct(LibraryFile.media_item_id)))
                .select_from(LibraryFile)
                .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
                .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)  # type: ignore[arg-type]
                .where(
                    *_facet_scope(
                        library_id,
                        filters,
                        member_id,
                        skip,
                        content_limit=content_limit,
                    ),
                    *_narrow(probe, member_id, library_id=library_id),
                )
            )
        ).scalar_one()
        out.append(FacetValueView(value=value, label=label, count=int(count)))
    return out


async def _second_tier_facets(
    session: AsyncSession,
    library_id: int,
    filters: LibraryFilter | None,
    member_id: int | None,
    content_limit: ContentLimit | None = None,
) -> dict:
    """「更多筛选」面板的候选值与计数（找片二级 + 查库）。"""
    ratings = await _bucket_facet(
        session,
        library_id,
        filters,
        member_id,
        "rating_gte",
        [(str(v), f"≥ {v:g}", LibraryFilter(rating_gte=v)) for v in (9, 8, 7)],
        content_limit,
    )
    runtimes = await _bucket_facet(
        session,
        library_id,
        filters,
        member_id,
        "runtimes",
        [
            ("lte60", "≤ 60′", LibraryFilter(runtimes=("lte60",))),
            ("60to90", "60–90′", LibraryFilter(runtimes=("60to90",))),
            ("90to120", "90–120′", LibraryFilter(runtimes=("90to120",))),
            ("gt120", "> 120′", LibraryFilter(runtimes=("gt120",))),
        ],
        content_limit,
    )
    hdr = await _bucket_facet(
        session,
        library_id,
        filters,
        member_id,
        "hdr",
        [("1", "HDR", LibraryFilter(hdr=True)), ("0", "SDR", LibraryFilter(hdr=False))],
        content_limit,
    )
    stock = await _bucket_facet(
        session,
        library_id,
        filters,
        member_id,
        "stock",
        [
            ("missing", "文件失联", LibraryFilter(stock=("missing",))),
            ("unscraped", "没刮到档案", LibraryFilter(stock=("unscraped",))),
        ],
        content_limit,
    )

    # 语言与分辨率是**从数据里长出来的**取值，可以直接分组数
    lang_rows = (
        await session.execute(
            select(
                MediaMetadata.original_language,
                func.count(func.distinct(LibraryFile.media_item_id)),
            )
            .select_from(LibraryFile)
            .join(MediaMetadata, MediaMetadata.media_item_id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .where(
                *_facet_scope(
                    library_id,
                    filters,
                    member_id,
                    "languages",
                    content_limit=content_limit,
                ),
                MediaMetadata.original_language.is_not(None),  # type: ignore[union-attr]
            )
            .group_by(MediaMetadata.original_language)
        )
    ).all()
    res_rows = (
        await session.execute(
            select(LibraryFile.resolution, func.count(func.distinct(LibraryFile.media_item_id)))
            .select_from(LibraryFile)
            .where(
                *_facet_scope(
                    library_id,
                    filters,
                    member_id,
                    "resolutions",
                    content_limit=content_limit,
                ),
                LibraryFile.resolution.is_not(None),  # type: ignore[union-attr]
            )
            .group_by(LibraryFile.resolution)
        )
    ).all()
    return {
        "ratings": ratings,
        "runtimes": runtimes,
        "hdr": hdr,
        "stock": stock,
        # 语言与画质同理：选中的取值补一条 0，别让它从自己那一维里消失
        "languages": _with_selected(lang_rows, filters.languages if filters else ()),
        "resolutions": _with_selected(res_rows, filters.resolutions if filters else ()),
    }


async def build_library_facets(
    session: AsyncSession,
    library_id: int,
    kind: str,
    *,
    filters: LibraryFilter | None = None,
    member_id: int | None = None,
    tier: str = "primary",
    content_limit: ContentLimit | None = None,
) -> LibraryFacetsView:
    """筛选面板的候选值与计数（docs/design/library-filtering.md 3.3）。

    每一维的计数都在**排除该维自身条件**的前提下算（``skip``）——否则勾了
    「动画」之后其他类型全变 0，多选就废了。与 ``build_library_wall`` 共用
    同一个 ``filters``，所以「面板上显示多少部、点下去墙上就是多少部」是
    结构保证的，不靠两处各写一遍。

    为 0 的候选值照常返回（前端置灰不可点），这是「永不空货架」的第一道闸。
    """
    total = (
        await session.execute(
            select(func.count(func.distinct(LibraryFile.media_item_id))).where(
                *_facet_scope(library_id, filters, member_id, skip="", content_limit=content_limit)
            )
        )
    ).scalar_one()

    genres = await _json_facet(
        session,
        MediaMetadata.genre_ids,
        library_id,
        filters,
        member_id,
        "genres",
        selected=filters.genres if filters else (),
        content_limit=content_limit,
    )
    countries = await _json_facet(
        session,
        MediaMetadata.origin_countries,
        library_id,
        filters,
        member_id,
        "countries",
        selected=filters.countries if filters else (),
        content_limit=content_limit,
    )

    # 年代：取（条目, 年份）后在 Python 里分档——档位是闭区间常量，用 SQL 的
    # CASE 表达只会让这段更难读，而行数与库内条目数同量级
    year_rows = (
        await session.execute(
            select(LibraryFile.media_item_id, _year_expr())
            .select_from(LibraryFile)
            .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .outerjoin(MediaMetadata, MediaMetadata.media_item_id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .where(
                *_facet_scope(
                    library_id,
                    filters,
                    member_id,
                    "decades",
                    content_limit=content_limit,
                )
            )
            .distinct()
        )
    ).all()
    decade_counts: dict[str, int] = {key: 0 for key in _DECADE_RANGES}
    for _, year in year_rows:
        if year is None:
            continue  # 未知年份不属于任何档（见 _DECADE_RANGES）
        for key, (lo, hi) in _DECADE_RANGES.items():
            if (lo is None or year >= lo) and year <= hi:
                decade_counts[key] += 1
                break

    watch_counts: list[tuple[str, str, int]] = []
    for value, label in _WATCH_LABELS:
        count = (
            await session.execute(
                select(func.count(func.distinct(LibraryFile.media_item_id)))
                .select_from(LibraryFile)
                .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
                .where(
                    *_facet_scope(
                        library_id,
                        filters,
                        member_id,
                        "watch",
                        content_limit=content_limit,
                    ),
                    _watch_clause(value, member_id or 0),
                )
            )
        ).scalar_one()
        watch_counts.append((value, label, count))

    # 二级维度只在「更多筛选」面板打开时才算：它们是十几条 COUNT，
    # 常用路径（四个一级 chips）不该为一个用户还没打开的面板买单
    second: dict = {}
    if tier == "all":
        second = await _second_tier_facets(session, library_id, filters, member_id, content_limit)

    return LibraryFacetsView(
        total=int(total),
        # 类型与地区按数量倒序：用户扫的是「这个库里主要有什么」，不是字典序
        genres=[
            FacetValueView(value=v, label=genre_label(kind, int(v)) if v.isdigit() else v, count=c)
            for v, c in sorted(genres, key=lambda r: (-r[1], r[0]))
        ],
        countries=[
            FacetValueView(value=v, label=country_label(v), count=c)
            for v, c in sorted(countries, key=lambda r: (-r[1], r[0]))
        ],
        # 年代按时间倒序（_DECADE_RANGES 本就是这个顺序），空档也回
        decades=[
            FacetValueView(value=key, label="更早" if key == "earlier" else key, count=count)
            for key, count in decade_counts.items()
        ],
        watch=[FacetValueView(value=v, label=lb, count=c) for v, lb, c in watch_counts],
        **second,
    )


#: 维度名 → 展示名（放宽建议的文案用）
_DIM_LABELS = {
    "genres": "类型",
    "countries": "地区",
    "decades": "年代",
    "watch": "观看",
    "rating_gte": "评分",
    "runtimes": "片长",
    "languages": "语言",
    "resolutions": "画质",
    "hdr": "动态范围",
    "stock": "库存",
}

#: 放宽建议要逐个取值评估的「多值」维度（单值的 watch / rating_gte / hdr 另处理）
_RELAX_LIST_DIMS = (
    "genres",
    "countries",
    "decades",
    "runtimes",
    "languages",
    "resolutions",
    "stock",
)

#: 二级维度里那些取值本身不可读的档位，展示名与「更多筛选」面板保持一致
_RELAX_VALUE_LABELS = {
    "lte60": "≤ 60′",
    "60to90": "60–90′",
    "90to120": "90–120′",
    "gt120": "> 120′",
    "missing": "文件失联",
    "unscraped": "没刮到档案",
}


async def _count_matching(
    session: AsyncSession,
    library_id: int,
    filters: LibraryFilter | None,
    member_id: int | None,
    content_limit: ContentLimit | None = None,
) -> int:
    """当前条件下的命中数（与墙同口径：本库、已识别、有在位文件）。"""
    return int(
        (
            await session.execute(
                select(func.count(func.distinct(LibraryFile.media_item_id))).where(
                    *_facet_scope(
                        library_id,
                        filters,
                        member_id,
                        skip="",
                        content_limit=content_limit,
                    )
                )
            )
        ).scalar_one()
    )


async def build_library_relax(
    session: AsyncSession,
    library_id: int,
    kind: str,
    *,
    filters: LibraryFilter,
    member_id: int | None = None,
    content_limit: ContentLimit | None = None,
) -> LibraryRelaxView:
    """筛空时的放宽建议：逐条剔除已选条件后重算命中数，取最大的前三条。

    **只列命中数 > 0 的条件。** 多维交叉时经常出现"去掉它还是 0 部"的剔除项
    （比如同时选了纪录片、日韩、已看完，去掉任意一条仍然是 0），把它们摆出来
    是噪音不是建议——用户要的是一条真能救回内容的出路，不是一份无效操作清单。
    一条都救不回时返回空表，前端只留「清空全部条件」。
    """
    total = await _count_matching(session, library_id, filters, member_id, content_limit)
    rows: list[tuple[str, str, int]] = []
    # 每一个**已选**的取值都要评估——包括二级维度。只看一级四维的话，用户
    # 用「4K + 评分≥9」筛空时一条建议都给不出，界面却会说"去掉任意一条也
    # 救不回来"——那是句假话：去掉评分就救回来了
    for dim in _RELAX_LIST_DIMS:
        for value in getattr(filters, dim):
            kept = tuple(v for v in getattr(filters, dim) if v != value)
            trimmed = replace(filters, **{dim: kept})
            left = await _count_matching(session, library_id, trimmed, member_id, content_limit)
            rows.append((dim, str(value), left))
    for dim, current in (
        ("watch", filters.watch),
        ("rating_gte", filters.rating_gte),
        ("hdr", filters.hdr),
    ):
        if current is None:
            continue
        trimmed = replace(filters, **{dim: None})
        left = await _count_matching(session, library_id, trimmed, member_id, content_limit)
        rows.append((dim, str(current), left))

    watch_labels = dict(_WATCH_LABELS)

    def _label(dim: str, value: str) -> str:
        if dim == "genres":
            return genre_label(kind, int(value)) if value.lstrip("-").isdigit() else value
        if dim == "countries":
            return country_label(value)
        if dim == "decades":
            return "更早" if value == "earlier" else value
        if dim == "watch":
            return watch_labels.get(value, value)  # type: ignore[arg-type]
        if dim == "rating_gte":
            return f"≥ {float(value):g}"
        if dim == "hdr":
            return "HDR" if value == "True" else "SDR"
        return _RELAX_VALUE_LABELS.get(value, value)

    best = sorted((r for r in rows if r[2] > 0), key=lambda r: -r[2])[:3]
    return LibraryRelaxView(
        total=total,
        suggestions=[
            RelaxSuggestionView(
                dim=dim,
                dim_label=_DIM_LABELS.get(dim, dim),
                value=value,
                label=_label(dim, value),
                count=count,
            )
            for dim, value, count in best
        ],
    )


async def _wall_page_ids(
    session: AsyncSession,
    library_id: int,
    sort: WallSort,
    limit: int | None,
    offset: int,
    identity: WallIdentity = "confirmed",
    filters: LibraryFilter | None = None,
    member_id: int | None = None,
    content_limit: ContentLimit | None = None,
    order: WallOrder | None = None,
    only_item_id: int | None = None,
) -> list[int]:
    """按 sort 排好序的本页条目 id（无 limit 时是全库）。

    ``only_item_id`` 把候选集收窄成一个条目（见 ``_wall_scope``）：反查
    "这部片属于哪些合集"时，每个合集走的是与列成员**同一条**查询，
    只是候选集只有一个。

    先把「这一页是哪些条目」定下来，后面的聚合才能只算这几十个条目。
    每个排序都以 media_item_id 收尾——排序键相等时顺序必须稳定，
    否则翻页会出现某条目重复出现、另一条目永远刷不到的漏项。

    ``order`` 反转方向时，**收尾的 id 跟着一起反**：反向后的序列恰好是自然序列
    倒过来，翻页、索引、「回到上次位置」的 offset 口径都不必另算。
    度量为空（没评分、没看过）的条目两个方向都沉底——它们不是"最小值"，是"没数据"。
    """
    narrow = _narrow(filters, member_id, library_id=library_id, content_limit=content_limit)

    if sort == "title":
        ids = [
            i
            for i, _ in await _titles_sorted(
                session, library_id, identity, filters, member_id, content_limit, only_item_id
            )
        ]
        if not _ascending(sort, order):
            ids.reverse()
        return ids if limit is None else ids[offset : offset + limit]

    if sort == "added_at":
        # 「最近添加」：条目的入账时间取它名下最新的一次文件入账
        ascending = _ascending(sort, order)
        added = func.max(LibraryFile.created_at)
        query = (
            select(LibraryFile.media_item_id)
            .where(
                *_wall_scope(library_id, identity, only_item_id),
                *narrow,
            )
            .group_by(LibraryFile.media_item_id)  # type: ignore[arg-type]
            .order_by(
                added.asc() if ascending else added.desc(),
                LibraryFile.media_item_id.asc() if ascending else LibraryFile.media_item_id.desc(),  # type: ignore[union-attr]
            )
        )
        if limit is not None:
            query = query.limit(limit).offset(offset)
        return [i for i in (await session.execute(query)).scalars().all() if i is not None]

    if sort in ("release_date", "release_date_asc"):
        # 「按内容时间」：其他库的家庭录像按拍摄日期倒序最自然（release_date 由
        # 扫描从 sidecar NFO / 容器日期标签 / 文件 mtime 回落而来，见
        # local_identity）；影视库则是上映/首播日期。缺日期的退到年份、再到 id
        query = (
            select(LibraryFile.media_item_id)
            .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)  # type: ignore[arg-type]
            .where(
                *_wall_scope(library_id, identity, only_item_id),
                *narrow,
            )
            .group_by(LibraryFile.media_item_id)  # type: ignore[arg-type]
        )
        if _ascending(sort, order):
            # 正序：系列合集的 release_date_asc 档、或用户把「按上映时间」切成旧→新。
            # 三个键一起翻向，只翻主键会让同年的片仍按倒序，读起来更乱。
            # 缺日期的沉底（SQLite 升序默认把 NULL 排最前）：与倒序一致，
            # 索引条的「未知」档因此两个方向都在最后
            query = query.order_by(
                nullslast(func.max(MediaMetadata.release_date).asc()),
                func.max(MediaItem.year).asc(),
                func.max(MediaItem.title).asc(),
                LibraryFile.media_item_id.asc(),  # type: ignore[union-attr]
            )
        else:
            query = query.order_by(
                func.max(MediaMetadata.release_date).desc(),
                func.max(MediaItem.year).desc(),
                # release_date 只有日期没有时分：同一天的照片/录像按标题（文件名
                # 主干，相机序号单调）排，比按入账 id 稳定得多
                func.max(MediaItem.title).desc(),
                LibraryFile.media_item_id.desc(),  # type: ignore[union-attr]
            )
        if limit is not None:
            query = query.limit(limit).offset(offset)
        return [i for i in (await session.execute(query)).scalars().all() if i is not None]

    # —— 以下四档共用同一个形状：按某个度量聚合后倒/正序，末尾一律以
    #    media_item_id 收尾保证稳定分页；度量为空的条目靠 NULLS LAST 沉底，
    #    而不是随排序方向在头尾之间跳
    if sort in ("rating", "runtime", "size", "last_played"):
        query = (
            select(LibraryFile.media_item_id)
            .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)  # type: ignore[arg-type]
            .where(
                *_wall_scope(library_id, identity, only_item_id),
                *narrow,
            )
            .group_by(LibraryFile.media_item_id)  # type: ignore[arg-type]
        )
        if sort == "rating":
            measure = func.max(MediaMetadata.vote_average)
        elif sort == "runtime":
            measure = func.max(MediaMetadata.runtime_minutes)
        elif sort == "size":
            # 体积按本库内的在位文件求和：同一部片散在两个库时，
            # 这面墙上显示的应该是它在**这个库**占多少地方
            measure = func.sum(LibraryFile.size_bytes)
        else:
            measure = func.max(_last_played_at(member_id or 0))
        ascending = _ascending(sort, order)
        # 自然方向下收尾一律是 id 倒序（加方向之前的行为）；反向时整条序列倒过来，
        # 收尾也跟着变 id 正序——否则同分的片在两个方向里是同一个先后，不是"倒过来"
        flipped = ascending != _NATURAL_ASC[sort]
        query = query.order_by(
            nullslast(measure.asc() if ascending else measure.desc()),
            LibraryFile.media_item_id.asc() if flipped else LibraryFile.media_item_id.desc(),  # type: ignore[union-attr]
        )
        if limit is not None:
            query = query.limit(limit).offset(offset)
        return [i for i in (await session.execute(query)).scalars().all() if i is not None]

    # probing：扫描补探阶段把「还有文件没读出规格」的条目提到墙最前面，
    # 用户能看见"在处理哪几部"；两段各自保持拼音序（sorted 稳定排序）。
    # strm 占位文件不算"没读出"——它永远探不出规格，算进来会让网盘库
    # 每轮扫描都全墙置顶、永不落位
    ordered = await _titles_sorted(
        session, library_id, identity, filters, member_id, content_limit
    )
    unprobed = {
        i
        for i in (
            await session.execute(
                select(LibraryFile.media_item_id)
                .where(
                    LibraryFile.library_id == library_id,
                    LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
                    LibraryFile.audio_streams.is_(None),  # type: ignore[union-attr]
                    LibraryFile.in_place(),
                    LibraryFile.file_path.not_like(f"%{STRM_EXT}"),  # type: ignore[union-attr]
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    }
    ids = [i for i, _ in sorted(ordered, key=lambda row: row[0] not in unprobed)]
    return ids if limit is None else ids[offset : offset + limit]


async def _wall_count(
    session: AsyncSession,
    library_id: int,
    identity: WallIdentity = "confirmed",
    filters: LibraryFilter | None = None,
    member_id: int | None = None,
    content_limit: ContentLimit | None = None,
) -> int:
    """符合条件的条目**数量**——与 ``_wall_page_ids`` 同一套收窄，只是不取 id。

    排序不影响数量，所以这里没有 sort 参数：一条 ``COUNT(DISTINCT)`` 覆盖全部
    档位。存在的理由是合集列表——那里只要"这个合集有几部"，走 ``_wall_page_ids``
    则要把整批 id 取出来（按标题排序时还要在 Python 里做一次全库拼音排序），
    一个库几十个自动生成的系列合集时这就是那道性能悬崖。

    **收窄条件仍然只有一处**（``_narrow`` + 这几行 WHERE），换的只是投影，
    所以它不构成"合集自己的查询"。口径一致由回归用例压着：
    ``_wall_count == len(_wall_page_ids)``。
    """
    return int(
        (
            await session.execute(
                select(func.count(func.distinct(LibraryFile.media_item_id))).where(
                    *_wall_scope(library_id, identity),
                    *_narrow(
                        filters, member_id, library_id=library_id, content_limit=content_limit
                    ),
                )
            )
        ).scalar_one()
        or 0
    )


async def favorite_item_ids(
    session: AsyncSession, media_item_ids: list[int], *, member_id: int
) -> set[int]:
    """这批条目里当前观看者收藏了哪几部（条目级收藏，不含单季 / 单集）。

    收藏落在条目级哨兵单元上（剧 ``(-1,-1)``、电影 ``(0,0)``，见
    ``services/playback/marks.py``），这里只捞已收藏的行再按哨兵过滤——一页
    几十部，命中的通常是个位数，比给每部作品单独问一次 ``/playback/marks``
    便宜得多。海报墙与图廊共用这一份口径。
    """
    from movieclaw_api.services.playback.marks import item_favorite_unit
    from movieclaw_db.models import PlaybackState

    if not media_item_ids:
        return set()
    kinds = {
        item_id: kind
        for item_id, kind in (
            await session.execute(
                select(MediaItem.id, MediaItem.kind).where(MediaItem.id.in_(media_item_ids))  # type: ignore[attr-defined]
            )
        ).all()
    }
    return {
        item_id
        for item_id, season, episode in (
            await session.execute(
                select(
                    PlaybackState.media_item_id,
                    PlaybackState.season_number,
                    PlaybackState.episode_number,
                ).where(
                    PlaybackState.member_id == member_id,
                    PlaybackState.media_item_id.in_(media_item_ids),  # type: ignore[attr-defined]
                    PlaybackState.is_favorite.is_(True),  # type: ignore[union-attr]
                )
            )
        ).all()
        if item_id in kinds
        and (item_id, season, episode) == item_favorite_unit(item_id, kinds[item_id])
    }


async def build_library_wall(
    session: AsyncSession,
    library_id: int,
    *,
    sort: WallSort = "title",
    limit: int | None = None,
    offset: int = 0,
    identity: WallIdentity = "confirmed",
    member_id: int | None = None,
    filters: LibraryFilter | None = None,
    content_limit: ContentLimit | None = None,
    order: WallOrder | None = None,
) -> list[LibraryItemView]:
    """库内媒体条目的库存聚合（单库海报墙数据源）。

    ``identity`` 选正式条目还是临时条目（见 ``WallIdentity``）：两者不混排——
    正片的 2:3 海报和认不出的文件的 16:9 抓帧塞进同一套拼音序里只会两败俱伤。

    ``limit`` 给定时只聚合这一页的条目——首页「最近添加」只要 20 格，
    海报墙滚动加载一次要一屏，都不该为此把整库的台账行捞出来算一遍。
    不给 limit 则是全库（保留给一次性拿完整库存的调用方）。

    给了 ``member_id`` 才带这位观看者的收藏态（海报右上角那颗心）；不给就一律
    ``False``——内部调用与不认人的场景不必为此多查一次。

    调用方需自行完成库存在性检查（404）。
    """
    page_ids = await _wall_page_ids(
        session, library_id, sort, limit, offset, identity, filters, member_id, content_limit, order
    )
    if not page_ids:
        return []
    # 分页时按 id 列表收窄（一页几十个，绑定变量绰绰有余）；全库时走子查询
    # ——SQLite 的绑定变量数有上限，几千个 id 塞进 IN 列表会撞
    in_page = (
        select(LibraryFile.media_item_id).where(
            LibraryFile.library_id == library_id,
            LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            *_narrow(filters, member_id, library_id=library_id, content_limit=content_limit),
        )
        if limit is None
        else page_ids
    )
    views = await _aggregate_wall_views(session, library_id, in_page, page_ids)
    if member_id is not None:
        favorites = await favorite_item_ids(session, page_ids, member_id=member_id)
        for view in views:
            view.is_favorite = view.media_item_id in favorites
    return views


@dataclass(frozen=True, slots=True)
class PosterFacts:
    """一个条目的海报事实：最终 URL、模糊占位图、本地资产像素尺寸、上映日、评分。"""

    url: str | None = None
    blur: str | None = None
    #: 本地刮削资产的像素尺寸；没有本地资产时为 None（比例只能回落到文件探测值）
    asset_size: tuple[int | None, int | None] | None = None
    release_date: date | None = None
    #: 评分（0~10，TMDB 或 NFO）；没有档案或没评过为 None。与上映日同一条查询顺带取，不多一次往返
    rating: float | None = None


async def poster_facts_many(
    session: AsyncSession, item_ids: Sequence[int]
) -> dict[int, PosterFacts]:
    """一批条目的海报事实——**海报墙与合集封面共用的唯一实现**。

    「本地刮削资产优先（断网可用），没有资产才回落 TMDB 图床」这条规则只在
    这里写一次。合集卡片上的封面与海报墙上的那张图必须是同一张：共用一个
    函数是结构性保证，两处各写一遍再靠人记得同步则是纪律，纪律会松。

    一条查询覆盖整批（``MediaItem`` 左连 ``MediaMetadata``）。合集列表正是
    靠它把封面代价从「每个合集一次完整墙聚合」压成**整页一条查询**——
    否则一个库自动生成几十个系列合集之后，打开合集页就是几百条查询。
    """
    from movieclaw_api.core.config import get_settings

    ids = [i for i in item_ids if i is not None]
    if not ids:
        return {}
    base = get_settings().tmdb_image_base_url.rstrip("/")
    out: dict[int, PosterFacts] = {}
    for item_id, poster_path, poster_file, width, height, released, blur, rating in (
        await session.execute(
            select(
                MediaItem.id,
                MediaItem.poster_path,
                MediaMetadata.poster_file,
                MediaMetadata.poster_width,
                MediaMetadata.poster_height,
                MediaMetadata.release_date,
                MediaMetadata.poster_blur,
                MediaMetadata.vote_average,
            )
            .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)  # type: ignore[arg-type]
            .where(MediaItem.id.in_(ids))  # type: ignore[attr-defined]
        )
    ).all():
        if poster_file:
            # ?v=<mtime>：换图原地覆盖同一路径，不带版本海报墙会一直显示旧图
            url = f"/images/assets/{poster_file}?v={asset_version(poster_file)}"
            out[item_id] = PosterFacts(url, blur or None, (width, height), released, rating)
        else:
            url = f"{base}/w500{poster_path}" if poster_path else None
            out[item_id] = PosterFacts(url, None, None, released, rating)
    return out


async def _aggregate_wall_views(
    session: AsyncSession,
    library_id: int | None,
    in_page,
    ordered_ids: list[int],
    *,
    library_ids: set[int] | None = None,
) -> list[LibraryItemView]:
    """把一批条目在某个库内的台账行聚合成海报墙视图，按 ``ordered_ids`` 排列。

    海报墙分页与媒体库搜索共用这份聚合（口径必须一致：库存概况、缺集数、
    海报的本地资产优先级）。``in_page`` 是条目 id 列表或等价子查询。

    ``library_id=None`` 是**跨库合集**那一支：同一部片可能散在两个库，这时
    "几个文件、占多大"问的是它总共，而不是某一个库里。``library_ids`` 把范围
    收在观看者可见的库上——不给的话跨库聚合会把不可见库里的文件也算进来。
    """
    from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
    from movieclaw_db.repositories.media_repo import MediaItemRepository

    # 只取聚合真正用得上的六列，不整行取 ORM 对象：台账行有四十来列、其中
    # 三列是 JSON（音轨/字幕/候选），整行取意味着一部三万集的库要反序列化
    # 九万段 JSON——实测 2000 部剧的库因此要跑四秒多，而这些列一个都用不上
    file_rows = (
        await session.execute(
            select(
                LibraryFile.media_item_id,
                LibraryFile.library_id,
                LibraryFile.id,
                LibraryFile.season_number,
                LibraryFile.episode_number,
                LibraryFile.size_bytes,
                LibraryFile.resolution,
                LibraryFile.state,
                LibraryFile.created_at,
                LibraryFile.added_batch_id,
                # strm 占位文件永远探不出规格，不算「待补探」
                and_(
                    LibraryFile.audio_streams.is_(None),  # type: ignore[union-attr]
                    LibraryFile.file_path.not_like(f"%{STRM_EXT}"),  # type: ignore[union-attr]
                ),
            ).where(
                (
                    LibraryFile.library_id == library_id
                    if library_id is not None
                    else (
                        LibraryFile.library_id.in_(library_ids)  # type: ignore[union-attr]
                        if library_ids is not None
                        else true()
                    )
                ),
                LibraryFile.media_item_id.in_(in_page),  # type: ignore[union-attr]
            )
        )
    ).all()
    files_by_item: dict[int, list[_FileFacts]] = {}
    for media_item_id, *facts in file_rows:
        files_by_item.setdefault(media_item_id, []).append(_FileFacts(*facts))
    # 条目行整取——展示要用到标题/年份/状态等大部分列
    items = (
        (await session.execute(select(MediaItem).where(MediaItem.id.in_(in_page))))  # type: ignore[attr-defined]
        .scalars()
        .all()
    )
    grouped: dict[int, tuple[MediaItem, list[_FileFacts]]] = {
        item.id: (item, files_by_item[item.id])  # type: ignore[index]
        for item in items
        if item.id is not None
    }

    # 剧集的已播单元集合：海报悬浮操作（订阅追新/补齐缺集）的判断依据。
    # 集数据在条目建档时已落库（media_episode），批量查询本地即得，不打 TMDB。
    # 已播/在位口径走仓储层唯一实现（与订阅预检、工单生成同源）：
    # 缺集 = 已播（特别季除外）− 跨库在位，「补齐缺集」建订阅后恰好只为这些集生成工单
    tv_item_ids = [i for i, (item, _) in grouped.items() if item.kind == "tv"]
    aired_by_item = await MediaItemRepository(session).aired_units_many(tv_item_ids)
    owned_by_item = await LibraryFileRepository(session).owned_units_many(tv_item_ids)
    # 季集完整度判定只取季号与官方集数：本地恰好覆盖 E01..EN 才能写「全 N 集」，
    # 不能因为文件看起来连续就猜一季已经完整（在播季后面可能还有集）。保留
    # episode_count=NULL 的季行：季度覆盖仍然可判断，但集数未知时不能声称“全”。
    season_episode_counts_by_item: dict[int, dict[int, int | None]] = {}
    season_rows = (
        await session.execute(
            select(
                MediaSeason.media_item_id,
                MediaSeason.season_number,
                MediaSeason.episode_count,
            ).where(MediaSeason.media_item_id.in_(tv_item_ids))  # type: ignore[attr-defined]
        )
    ).all()
    for item_id, season_number, episode_count in season_rows:
        season_episode_counts_by_item.setdefault(item_id, {})[season_number] = episode_count

    # 海报优先本地刮削资产（断网可用），没有资产的回落 TMDB 图床——
    # 这条规则的唯一实现在 poster_facts_many，合集封面走的是同一处
    posters = await poster_facts_many(session, list(grouped.keys()))
    by_id: dict[int, LibraryItemView] = {}
    for item, files in grouped.values():
        season_episode_counts = season_episode_counts_by_item.get(item.id, {})  # type: ignore[arg-type]
        units = {(f.season_number, f.episode_number) for f in files}
        available_units = {
            (f.season_number, f.episode_number) for f in files if f.state == FileState.IN_PLACE
        }
        latest_file = max(files, key=lambda file: file.created_at)
        recent_addition: LibraryRecentAdditionView | None = None
        if item.kind == "tv" and latest_file.added_batch_id is not None:
            recent_units = sorted(
                {
                    (file.season_number, file.episode_number)
                    for file in files
                    if file.added_batch_id == latest_file.added_batch_id
                }
            )
            recent_by_season: dict[int, list[int]] = {}
            for season_number, episode_number in recent_units:
                recent_by_season.setdefault(season_number, []).append(episode_number)
            single_season = (
                next(iter(recent_by_season.items())) if len(recent_by_season) == 1 else None
            )
            if single_season is None:
                season_number = first_episode = last_episode = None
                complete_season = False
            else:
                season_number, episodes = single_season
                first_episode, last_episode = episodes[0], episodes[-1]
                consecutive = episodes == list(range(first_episode, last_episode + 1))
                if not consecutive:
                    first_episode = last_episode = None
                expected = season_episode_counts.get(season_number)
                complete_season = (
                    expected is not None
                    and expected > 0
                    and episodes == list(range(1, expected + 1))
                )
            recent_addition = LibraryRecentAdditionView(
                season_count=len(recent_by_season),
                episode_count=len(recent_units),
                season_number=season_number,
                first_episode_number=first_episode,
                last_episode_number=last_episode,
                complete_season=complete_season,
            )
        if item.kind == "tv":
            missing_episodes = len(
                aired_by_item.get(item.id, set()) - owned_by_item.get(item.id, set())  # type: ignore[arg-type]
            )
        else:
            missing_episodes = 0
        poster = posters.get(item.id) or PosterFacts()
        by_id[item.id] = LibraryItemView(  # type: ignore[index]
            media_item_id=item.id,  # type: ignore[arg-type]
            kind=MediaKind(item.kind),
            # 跨库合集里每一格要落回它自己那个库；单库墙上就是那个库。
            # 多库都有这部片时取台账行里的第一个，与文件聚合同一份来源
            library_id=library_id if library_id is not None else files[0].library_id,
            source=item.source,
            tmdb_id=item.tmdb_id,
            title=item.title,
            year=item.year,
            poster_url=poster.url,
            # 缩略图还没生成时用扫描入账记下的原图尺寸定比例：墙一开始就是最终
            # 布局，缩略图到达不会引起重排（渐进式加载的第 0 级）
            primary_aspect=primary_aspect(item, *(poster.asset_size or _pixel_size_of(files))),
            poster_blur=poster.blur,
            release_date=poster.release_date,
            rating=poster.rating,
            # 首个在位文件：一文件一条目的库就是那一个；多文件条目取最早入账的
            primary_file_id=min(
                (f.id for f in files if f.state == FileState.IN_PLACE), default=None
            ),
            file_count=len(files),
            total_size_bytes=sum(f.size_bytes for f in files),
            seasons=sorted({s for s, _ in units if item.kind == "tv"}),
            episode_count=len(units) if item.kind == "tv" else 0,
            resolutions=sorted({f.resolution for f in files if f.resolution}, reverse=True),
            missing_count=sum(1 for f in files if f.state == FileState.MISSING),
            air_status=derive_air_status(item.status) if item.kind == "tv" else None,
            missing_episode_count=missing_episodes,
            added_at=latest_file.created_at,
            recent_addition=recent_addition,
            inventory_summary=(
                _build_inventory_summary(available_units, season_episode_counts)
                if item.kind == "tv"
                else None
            ),
            # 缺失文件不算「待补探」：文件都不在了，探不是「还没轮到」而是「探不了」
            probe_pending_count=sum(
                1 for f in files if f.unprobed and f.state == FileState.IN_PLACE
            ),
        )
    # 顺序以调用方给定的 ordered_ids 为准，不在这里二次排序
    return [by_id[i] for i in ordered_ids if i in by_id]


#: 剧照 / 分集剧照 / 章节场景图的比例：横幅与抓帧都按 16:9 惯例
_LANDSCAPE_ASPECT = 16 / 9


async def build_library_gallery(
    session: AsyncSession,
    library_id: int,
    *,
    member_id: int,
    limit: int | None = None,
    offset: int = 0,
    sort: WallSort = "title",
    filters: LibraryFilter | None = None,
    content_limit: ContentLimit | None = None,
) -> list[LibraryGalleryGroupView]:
    """影视库 / 其他库的「图床浏览模式」数据源：条目的图铺平成组。

    与海报墙共用同一份条目名单、同一套排序与分页口径（``offset`` / ``limit``
    都按**条目**数），本页条目定下来之后交给 :func:`build_gallery_groups` 组图。
    默认按标题排（图廊的常驻序），``sort=added_at`` 是用户在 ⋯ 菜单里选的
    「最近添加」——两面墙同一个 ``offset`` 口径，切了排序「回到上次位置」
    仍然跳得准（前端把排序写进位置记录的形态里，见 lib/library-wall-recall.ts）。
    """
    page_ids = await _wall_page_ids(
        session, library_id, sort, limit, offset, "confirmed", filters, member_id, content_limit
    )
    return await build_gallery_groups(
        session, [(item_id, library_id) for item_id in page_ids], member_id=member_id
    )


async def build_gallery_groups(
    session: AsyncSession,
    page: Sequence[tuple[int, int]],
    *,
    member_id: int,
) -> list[LibraryGalleryGroupView]:
    """把「本页条目」铺平成图廊分组，顺序与 ``page`` 一致。

    ``page`` 的每一项是 ``(条目 id, 落点库 id)``：单库图廊里落点库恒等于本库，
    「我的收藏」的图廊是跨库的一面墙，每部作品各带自己的落点库（同收藏海报墙
    的口径，见 services/playback_favorites.py）。落点库决定两件事——组标题和
    灯箱里「前往详情」的地址，以及**取哪些文件**的章节图与分集剧照：同一部
    作品可能在多个库里各有一份文件，只认落点库那一份，图廊看的是"这一格点
    进去的那个库里有的"。

    一组就是一部作品的全部图，顺序固定为海报 → 横幅剧照 → 逐集（分集剧照 →
    该集章节图）。只取**在位**文件名下的章节图与分集剧照，缺集的剧照不混进来。
    没有任何图的条目也占一组（``images`` 为空）——一页的组数恒等于条目数，
    前端据此判断还有没有下一页。每组还带上 ``member_id`` 这位观看者有没有
    收藏这部作品，供瓦片角标与灯箱里的心一次拿齐（详情页那样逐条目问
    ``/playback/marks``，一屏几十张图就是几十个请求）。

    图片来源与海报墙、详情页同一优先级：本地刮削资产（断网可用，带
    ``?v=`` 版本戳）优先，其次 TMDB 图床——但海报取 w780、剧照取 w1280
    而不是墙上的 w500 / w300：图廊的灯箱要放大看，小图会糊。条目目录里
    的 poster.jpg / fanart.jpg 这一层这里不探（要逐条目摸文件系统，一页
    几十部太贵），与海报墙一致。
    """
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.library import chapters as chapters_mod
    from movieclaw_db.models import MediaEpisode

    if not page:
        return []
    page_ids = [item_id for item_id, _ in page]
    library_of = dict(page)
    items_by_id: dict[int, MediaItem] = {
        item.id: item
        for item in (
            await session.execute(select(MediaItem).where(MediaItem.id.in_(page_ids)))  # type: ignore[attr-defined]
        )
        .scalars()
        .all()
        if item.id is not None
    }
    favorite_ids = await favorite_item_ids(session, page_ids, member_id=member_id)
    meta_by_id: dict[int, tuple[str | None, int | None, int | None, str | None]] = {
        item_id: (poster_file, width, height, backdrop_file)
        for item_id, poster_file, width, height, backdrop_file in (
            await session.execute(
                select(
                    MediaMetadata.media_item_id,
                    MediaMetadata.poster_file,
                    MediaMetadata.poster_width,
                    MediaMetadata.poster_height,
                    MediaMetadata.backdrop_file,
                ).where(MediaMetadata.media_item_id.in_(page_ids))  # type: ignore[attr-defined]
            )
        ).all()
    }
    # 在位文件：只取章节相关的几列，不整行取（音轨/字幕 JSON 用不上）。
    # 库集合先粗筛（跨库时是这一页涉及的几个库），再按 (条目, 落点库) 精确留下
    file_rows = (
        await session.execute(
            select(
                LibraryFile.media_item_id,
                LibraryFile.library_id,
                LibraryFile.id,
                LibraryFile.season_number,
                LibraryFile.episode_number,
                LibraryFile.duration_seconds,
                LibraryFile.chapters,
                LibraryFile.chapter_images,
            )
            .where(
                LibraryFile.library_id.in_(sorted(set(library_of.values()))),  # type: ignore[attr-defined]
                LibraryFile.media_item_id.in_(page_ids),  # type: ignore[union-attr]
                LibraryFile.in_place(),
            )
            .order_by(LibraryFile.season_number, LibraryFile.episode_number, LibraryFile.id)
        )
    ).all()
    files_by_item: dict[int, list[tuple]] = {}
    for media_item_id, file_library_id, *facts in file_rows:
        if library_of.get(media_item_id) != file_library_id:
            continue
        files_by_item.setdefault(media_item_id, []).append(tuple(facts))
    tv_ids = [i for i in page_ids if (item := items_by_id.get(i)) and item.kind == "tv"]
    stills_by_unit: dict[tuple[int, int, int], tuple[str, str]] = {}
    if tv_ids:
        for item_id, season, episode, name, still_file, still_path in (
            await session.execute(
                select(
                    MediaEpisode.media_item_id,
                    MediaEpisode.season_number,
                    MediaEpisode.episode_number,
                    MediaEpisode.name,
                    MediaEpisode.still_file,
                    MediaEpisode.still_path,
                ).where(MediaEpisode.media_item_id.in_(tv_ids))  # type: ignore[attr-defined]
            )
        ).all():
            if still_file:
                url = f"/images/assets/{still_file}?v={asset_version(still_file)}"
            elif still_path:
                url = f"{get_settings().tmdb_image_base_url.rstrip('/')}/w1280{still_path}"
            else:
                continue
            stills_by_unit[(item_id, season, episode)] = (url, (name or "").strip())

    base = get_settings().tmdb_image_base_url.rstrip("/")
    groups: list[LibraryGalleryGroupView] = []
    for item_id in page_ids:
        item = items_by_id.get(item_id)
        if item is None:
            continue
        poster_file, width, height, backdrop_file = meta_by_id.get(
            item_id, (None, None, None, None)
        )
        images: list[LibraryGalleryImageView] = []
        if poster_file:
            poster_url: str | None = f"/images/assets/{poster_file}?v={asset_version(poster_file)}"
        else:
            poster_url = f"{base}/w780{item.poster_path}" if item.poster_path else None
        if poster_url:
            images.append(
                LibraryGalleryImageView(
                    kind="poster",
                    url=poster_url,
                    aspect=primary_aspect(item, width, height),
                    label="海报",
                )
            )
        if backdrop_file:
            backdrop_url: str | None = (
                f"/images/assets/{backdrop_file}?v={asset_version(backdrop_file)}"
            )
        else:
            backdrop_url = f"{base}/w1280{item.backdrop_path}" if item.backdrop_path else None
        if backdrop_url:
            images.append(
                LibraryGalleryImageView(
                    kind="backdrop", url=backdrop_url, aspect=_LANDSCAPE_ASPECT, label="剧照"
                )
            )
        is_tv = item.kind == "tv"
        seen_units: set[tuple[int, int]] = set()
        for _file_id, season, episode, duration, chapters, chapter_images in files_by_item.get(
            item_id, []
        ):
            unit = (season, episode)
            if is_tv and unit not in seen_units:
                seen_units.add(unit)
                still = stills_by_unit.get((item_id, season, episode))
                if still is not None:
                    still_url, name = still
                    images.append(
                        LibraryGalleryImageView(
                            kind="still",
                            url=still_url,
                            aspect=_LANDSCAPE_ASPECT,
                            label=f"第 {episode} 集" + (f" · {name}" if name else ""),
                            season=season,
                            episode=episode,
                        )
                    )
            if chapters is None:
                continue  # 旧行没探过章节
            image_map = chapters_mod.chapter_image_map(chapter_images)
            for chapter in chapters_mod.effective_chapters(chapters, duration):
                entry = image_map.get(chapter.start_ms)
                if entry is None:
                    continue
                rel = str(entry["image"])
                frame_raw = entry.get("frame_ms")
                frame_ms = int(frame_raw) if isinstance(frame_raw, int | float) else None
                images.append(
                    LibraryGalleryImageView(
                        kind="chapter",
                        url=f"/images/assets/{rel}?v={asset_version(rel)}",
                        aspect=_LANDSCAPE_ASPECT,
                        label=chapter.title or f"章节 {chapter.index + 1}",
                        season=season if is_tv else None,
                        episode=episode if is_tv else None,
                        t_seconds=(frame_ms if frame_ms is not None else chapter.start_ms) / 1000,
                    )
                )
        # 没图的条目也占一组（images 为空）：分页按条目数走，前端靠「拿到的组数
        # 是否满一页」判断有没有下一页，滤掉空组是前端的事
        groups.append(
            LibraryGalleryGroupView(
                media_item_id=item_id,
                library_id=library_of[item_id],
                kind=MediaKind(item.kind),
                title=item.title,
                year=item.year,
                is_favorite=item_id in favorite_ids,
                images=images,
            )
        )
    return groups


async def search_library_items(
    session: AsyncSession,
    keyword: str,
    *,
    member_id: int | None = None,
    content_limit: ContentLimit | None = None,
) -> dict[int, list[LibraryItemView]]:
    """按关键词搜索全部媒体库的已识别条目：library_id -> 命中条目视图。

    搜索弹窗「媒体库」垂直的数据源。标题/原名子串匹配（忽略英文大小写），
    只搜已识别入库的条目——待识别文件没有可靠的标题可匹配，去待识别清单
    处理更合适。组内按标题拼音排序，与海报墙同一套排序规则。

    **观看者的分级约束在这里同样生效**：搜得到就等于看得到（点进去是详情页），
    墙上藏起来而搜索里搜得出来，那道约束只是障眼法。
    """
    pattern = f"%{keyword.strip().lower()}%"
    rows = (
        await session.execute(
            select(LibraryFile.library_id, LibraryFile.media_item_id, MediaItem.title)
            .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .where(
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
                LibraryFile.unidentified_code.is_(None),  # type: ignore[union-attr]  # 临时条目不进搜索
                or_(
                    func.lower(MediaItem.title).like(pattern),
                    func.lower(MediaItem.original_title).like(pattern),
                ),
                *_narrow(None, member_id, content_limit=content_limit),
            )
            .distinct()
        )
    ).all()
    matched: dict[int, list[tuple[int, str]]] = {}
    for library_id, item_id, title in rows:
        if item_id is not None:
            matched.setdefault(library_id, []).append((item_id, title))

    result: dict[int, list[LibraryItemView]] = {}
    for library_id, pairs in matched.items():
        ordered = [i for i, _ in sorted(pairs, key=lambda p: (title_sort_key(p[1]), p[0]))]
        result[library_id] = await _aggregate_wall_views(session, library_id, ordered, ordered)
    return result


@dataclass
class ItemDetailBundle:
    """详情页所需的全部原料（路由层映射为响应 schema）。"""

    item: MediaItem
    files: list[LibraryFile]
    entry_dirs: list[str]  # 条目在磁盘上的目录（删除确认展示；可能多根多个）
    local_meta: EntryMetadata | None  # NFO 解出的展示元数据；无 NFO 为 None
    has_local_poster: bool
    has_local_fanart: bool
    # 条目目录美术图的版本戳（mtime）：图片被替换后 URL 随之变化，
    # 否则浏览器拿缓存里的旧图显示，用户看到"换了没生效"
    local_poster_version: int
    local_fanart_version: int
    external_subtitles: dict[int, list[str]]  # file_id -> 外挂字幕文件名


def resolve_entry_dirs(roots: list[Path], files: list[LibraryFile]) -> list[Path]:
    """条目目录集合：多根/多版本可能给出多个，去重保序（第一个用于 NFO 与美术图）。"""
    entry_dirs: list[Path] = []
    for row in files:
        entry = entry_dir_of(roots, Path(row.file_path))
        # 原盘条目 file_path 本身就是目录（BDMV/VIDEO_TS），直接在根下时以它为条目目录
        if entry is None and row.container in ("bluray", "dvd"):
            entry = Path(row.file_path)
        if entry is not None and entry not in entry_dirs:
            entry_dirs.append(entry)
    return entry_dirs


async def layered_item_meta(session: AsyncSession, item: MediaItem) -> EntryMetadata | None:
    """条目展示元数据的**唯一读口径**（docs/design/metadata.md 第 5 节），
    Web 详情页与 Jellyfin 兼容层共用——分层策略只在这里维护一份：
    库内刮削档案（media_metadata，断网可用）→ TMDB 实时兜底（条目还没刮过，
    顺带触发后台刮削自愈）。

    **本地 NFO 不在这里读**：它在刮削时就被吸收进库内档案了（见
    ``services/library/nfo_absorb``），NFO 里有值的字段压过 TMDB，出处记在
    ``media_metadata.nfo_name`` 上。这样详情页无论打开多少次都只读库，
    不再回媒体盘——NAS 上那块盘本来就该少碰。用户改了 NFO 想立刻生效，
    走「刷新元数据」重新吸收。
    """
    local_meta = await _db_meta(session, item)
    if local_meta is None:
        local_meta = await _tmdb_fallback_meta(session, item)
        if item.id is not None:
            from movieclaw_api.services.media_scrape import scrape_media_item

            asyncio.get_running_loop().create_task(scrape_media_item(item.id))
    return local_meta


def local_item_artwork(
    roots: list[Path], files: list[LibraryFile], kind: str, *, cache: DirListing | None = None
) -> Path | None:
    """条目目录里的本地美术图（逐个条目目录找，第一张命中即用）。

    Web 的 artwork 接口与 Jellyfin 图片接口共用；找不到时两端各自退回
    刮削资产 / TMDB 图床。文件直接躺在库根下（没有条目目录）时按它所在
    目录找，此时只可能命中文件自己的 sidecar。同步磁盘 IO——调用方自行
    决定是否进线程池。
    """
    paths = [Path(row.file_path) for row in files]
    entry_dirs = resolve_entry_dirs(roots, files) or list(dict.fromkeys(p.parent for p in paths))
    for entry in entry_dirs:
        if not entry.is_dir():
            continue
        art = find_local_artwork(entry, kind, paths, cache=cache)
        if art is not None:
            return art
    return None


def _detail_local_files(
    roots: list[Path], files: list[LibraryFile]
) -> tuple[Path | None, Path | None, dict[Path, list[str]]]:
    """详情页要的全部本地磁盘信息，**一次线程跳转、一批目录只列一次**。

    海报、背景图、逐文件外挂字幕原本是三段独立的磁盘遍历，把同一个条目
    目录（剧集则是每个季目录）翻来覆去列好几遍。合成一次之后，一次详情页
    的目录列举数 = 条目涉及的目录数，与文件数无关。
    """
    cache: DirListing = {}
    poster = local_item_artwork(roots, files, "poster", cache=cache)
    fanart = local_item_artwork(roots, files, "fanart", cache=cache)
    videos = [
        Path(row.file_path)
        for row in files
        if row.state == FileState.IN_PLACE and row.container not in ("bluray", "dvd")
    ]
    return poster, fanart, _external_subtitles_many(videos, cache)


async def build_item_detail(
    session: AsyncSession, library: Library, item: MediaItem, files: list[LibraryFile]
) -> ItemDetailBundle:
    """装配条目详情：NFO 元数据 + 本地美术图 + 外挂字幕（介质规格直接读台账）。

    磁盘 IO（NFO 解析、目录列举）全部放线程池；库根不可达时各环节自然
    落空，页面仍能靠台账字段渲染。不触发 ffprobe——探测只在入库/扫描时做。
    """
    roots = [Path(p) for p in library.root_paths]
    entry_dirs = resolve_entry_dirs(roots, files)
    local_meta = await layered_item_meta(session, item)

    poster_art, fanart_art, subtitles_by_path = await asyncio.to_thread(
        _detail_local_files, roots, files
    )

    external: dict[int, list[str]] = {}
    for row in files:
        if row.state != FileState.IN_PLACE or row.container in ("bluray", "dvd"):
            continue
        assert row.id is not None
        external[row.id] = subtitles_by_path.get(Path(row.file_path), [])

    return ItemDetailBundle(
        item=item,
        files=files,
        entry_dirs=[str(d) for d in entry_dirs],
        local_meta=local_meta,
        has_local_poster=poster_art is not None,
        has_local_fanart=fanart_art is not None,
        local_poster_version=file_version(poster_art),
        local_fanart_version=file_version(fanart_art),
        external_subtitles=external,
    )


# ---------------------------------------------------------------------------
# 剧集分集（播放器式分集区的数据源）
# ---------------------------------------------------------------------------


@dataclass
class EpisodeInfo:
    """一集的展示信息：季集结构（media_season）+ 本地分集 NFO/缩略图 +
    TMDB 分季兜底 三源合并的结果。"""

    episode_number: int
    name: str | None = None
    overview: str | None = None
    air_date: str | None = None
    # 剧照：本地 "<视频名>-thumb.jpg" 走 /libraries/files/{id}/thumb 相对路径，
    # 否则 TMDB still 图床绝对地址；都没有为 None（前端给占位）
    still_url: str | None = None
    owned: bool = False  # 该集有在位文件
    file_ids: list[int] = field(default_factory=list)  # 该集的台账行（含 missing）
    # 当前观看者的进度（build_season_episodes 传 member_id 才填）：分集卡据此
    # 画进度条与已看对勾，口径与首页「最近观看」一致
    position_ms: int = 0
    played: bool = False
    progress_percent: int | None = None  # 1~99；完成态由 played 单独表达


def episode_view(info: EpisodeInfo) -> EpisodeView:
    """EpisodeInfo → 接口视图。两个分集端点（库详情页 / 播放页）共用，
    字段加减只改这一处。"""
    return EpisodeView(
        episode_number=info.episode_number,
        name=info.name,
        overview=info.overview,
        air_date=info.air_date,
        still_url=info.still_url,
        owned=info.owned,
        file_ids=info.file_ids,
        position_ms=info.position_ms,
        played=info.played,
        progress_percent=info.progress_percent,
    )


def find_episode_thumb(video: Path, cache: DirListing | None = None) -> Path | None:
    """分集本地缩略图：Kodi 惯例 "<视频文件名>-thumb.jpg"。

    走目录列举而不是逐个扩展名 stat：整季分集区一次要问十几集，逐集 4 次
    stat 就是几十次，而同一个季目录列一次就够（``cache`` 让整季共享）。
    """
    listing = dir_listing(video.parent, cache)
    if not listing:
        return None
    stem = video.stem.lower()
    for ext in _ART_EXTS:
        found = listing.get(f"{stem}-thumb{ext}")
        if found is not None:
            return found
    return None


def _season_local_thumbs(videos: list[Path]) -> dict[Path, bool]:
    """一季各集有没有本地缩略图（``<视频名>-thumb.jpg``）。

    **分集 NFO 不在这里读**：它在刮削时就被吸收进 ``media_episode`` 了
    （见 ``services/library/nfo_absorb``）。缩略图仍要现场判断——那是"图还在
    不在磁盘上"的事实，不是元数据，而且一整季只需列一次目录（结果还有进程级
    缓存兜着），成本是一次 stat 量级。
    """
    cache: DirListing = {}
    return {video: find_episode_thumb(video, cache) is not None for video in videos}


async def build_season_episodes(
    session: AsyncSession,
    item: MediaItem,
    files: list[LibraryFile],
    season_number: int,
    *,
    member_id: int | None = None,
) -> list[EpisodeInfo]:
    """装配一季的分集清单：并集"元数据里的集 ∪ 库里实际拥有的集"。

    元数据有而库里没有的集照样列出（owned=False，前端置灰）——用户一眼
    看到缺口；库里有而元数据没有的集也不丢（TMDB 元数据可能滞后）。
    读路径分层（docs/design/metadata.md 第 5 节）：分集 NFO/本地缩略图
    最优先（尊重既有刮削成果）→ media_episode 表（刮削落库的主体）→
    条目还没刮过时拉一次 TMDB 分季实时兜底。

    ``member_id`` 非 None 时附带该观看者的每集进度（position/played/percent）：
    分集卡与首页「最近观看」同一套视觉语言，数据同样只认 ``playback_state``
    ——网页播放器与 Jellyfin 客户端写的是同一张表，这里读出来天然一致。
    """
    from sqlmodel import select

    from movieclaw_db.models import MediaEpisode

    season_rows = [
        row for row in files if row.season_number == season_number and row.episode_number > 0
    ]
    by_episode: dict[int, list[LibraryFile]] = {}
    for row in season_rows:
        by_episode.setdefault(row.episode_number, []).append(row)

    meta_by_number: dict[int, MediaEpisode] = {
        e.episode_number: e
        for e in (
            await session.execute(
                select(MediaEpisode).where(
                    MediaEpisode.media_item_id == item.id,
                    MediaEpisode.season_number == season_number,
                )
            )
        )
        .scalars()
        .all()
    }

    # 本季每集「首个在位文件」的本地读盘（分集 NFO + 本地缩略图）一次做完，
    # 逐集在循环里各跳一次线程池的代价远高于读盘本身
    season_videos: dict[int, tuple[LibraryFile, Path]] = {}
    for number, rows in by_episode.items():
        for row in rows:
            if row.state == FileState.IN_PLACE:
                season_videos[number] = (row, Path(row.file_path))
                break
    local_thumbs = (
        await asyncio.to_thread(
            _season_local_thumbs, [video for _row, video in season_videos.values()]
        )
        if season_videos
        else {}
    )

    image_base = None
    infos: list[EpisodeInfo] = []
    for number in sorted(set(meta_by_number) | set(by_episode)):
        meta = meta_by_number.get(number)
        rows = by_episode.get(number, [])
        info = EpisodeInfo(
            episode_number=number,
            name=(meta.name if meta else "").strip() or None,
            overview=meta.overview if meta else None,
            air_date=meta.air_date.isoformat() if meta and meta.air_date else None,
            owned=any(row.state == FileState.IN_PLACE for row in rows),
            file_ids=[row.id for row in rows if row.id is not None],
        )
        # 剧照：本地资产 → TMDB 图床（经前端缓存代理）
        if meta is not None:
            if meta.still_file:
                # ?v=<mtime>：剧照原地覆盖同一路径，不带版本浏览器会用旧图
                version = asset_version(meta.still_file)
                info.still_url = f"/images/assets/{meta.still_file}?v={version}"
            elif meta.still_path:
                if image_base is None:
                    from movieclaw_api.core.config import get_settings

                    image_base = get_settings().tmdb_image_base_url.rstrip("/")
                info.still_url = f"{image_base}/w300{meta.still_path}"
        # 本地优先：分集 NFO 的标题/简介、同名 -thumb 缩略图（取首个在位文件）
        owned_file = season_videos.get(number)
        if owned_file is not None:
            row, video = owned_file
            # 集名/简介/首播日已经在 media_episode 里（分集 NFO 刮削时吸收），
            # 上面取 meta 时就带出来了；这里只补"本地有缩略图就用本地的"
            if local_thumbs.get(video):
                info.still_url = f"/libraries/files/{row.id}/thumb"
        infos.append(info)

    # 条目还没刮削过（该季在库里毫无集数据）才实时兜底，顺带触发后台刮削自愈
    if not meta_by_number and infos:
        await _fill_from_tmdb_season(session, item, season_number, infos)
        if item.id is not None:
            from movieclaw_api.services.media_scrape import scrape_media_item

            asyncio.get_running_loop().create_task(scrape_media_item(item.id))

    if member_id is not None and item.id is not None:
        from movieclaw_playback import state as playback_state

        states = await playback_state.get_states(session, [item.id], member_id=member_id)
        for info in infos:
            row = states.get((item.id, season_number, info.episode_number))
            if row is None:
                continue
            info.played = row.played
            info.position_ms = row.position_ms
            if row.position_ms > 0:
                # 百分比的分母与首页「最近观看」同口径：在位文件实测时长优先，
                # 其次分集刮削时长；clamp 到 1~99——完成态由 played 单独表达
                meta = meta_by_number.get(info.episode_number)
                duration_ms = max(
                    (
                        r.duration_seconds * 1000
                        for r in by_episode.get(info.episode_number, [])
                        if r.state == FileState.IN_PLACE and r.duration_seconds
                    ),
                    default=(meta.runtime_minutes or 0) * 60_000 if meta else 0,
                )
                if duration_ms > 0:
                    info.progress_percent = max(
                        1, min(99, round(row.position_ms * 100 / duration_ms))
                    )
    return infos


async def _fill_from_tmdb_season(
    session: AsyncSession, item: MediaItem, season_number: int, infos: list[EpisodeInfo]
) -> None:
    """TMDB 分季详情兜底：只填空缺字段，绝不覆盖本地刮削成果。失败静默
    （分集区退化为无剧照/无简介，不阻断）。"""
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.media_discover import get_tmdb_client

    settings = get_settings()
    # 语言按条目的刮削归属库（设计文档 §14）——缓存键必须带上它，否则动漫库
    # 与剧集库的条目会互相串味（同一 tmdb_id 缓存一份，先访问的语言说了算）
    language = effective_language(await scrape_setting_for_item(session, item))
    try:
        # 与条目展示信息同一套持久缓存：分季数据首访之后秒开
        data = await _get_display_cache().get_or_fetch(
            f"season:{item.tmdb_id}:{season_number}:{language}",
            fresh_ttl=_DISPLAY_CACHE_FRESH_TTL,
            stale_ttl=_DISPLAY_CACHE_STALE_TTL,
            factory=lambda: get_tmdb_client().get(
                f"tv/{item.tmdb_id}/season/{season_number}",
                {"language": language},
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- 兜底信息拉不到不阻断
        logger.warning(
            "条目 #%s 第 %s 季的 TMDB 分集信息拉取失败（分集区降级）：%s",
            item.id,
            season_number,
            exc,
        )
        return
    image_base = settings.tmdb_image_base_url.rstrip("/")
    remote = {e.get("episode_number"): e for e in data.get("episodes", [])}
    for info in infos:
        episode = remote.get(info.episode_number)
        if not episode:
            continue
        info.name = info.name or (episode.get("name") or "").strip() or None
        info.overview = info.overview or (episode.get("overview") or "").strip() or None
        info.air_date = info.air_date or episode.get("air_date") or None
        still = episode.get("still_path")
        if info.still_url is None and still:
            info.still_url = f"{image_base}/w300{still}"


async def _db_meta(session: AsyncSession, item: MediaItem) -> EntryMetadata | None:
    """库内刮削档案（media_metadata）→ 详情页展示元数据。

    标记 ``source="db"``，前端据此注明"信息来自本地档案"。演员头像用
    TMDB 图床地址（经前端缓存代理），头像不入本地资产（几十 KB × 全库
    演员数不值得，缓存代理已经把二次访问变成本地读）。
    """
    if item.id is None:
        return None
    from movieclaw_api.core.config import get_settings
    from movieclaw_db.repositories import MediaItemRepository

    row = await MediaItemRepository(session).get_metadata(item.id)
    if row is None or row.scraped_at is None:
        return None
    image_base = get_settings().tmdb_image_base_url.rstrip("/")

    def _thumb(actor: dict) -> str | None:
        # NFO 自带的绝对地址优先（吸收时原样存下，见 nfo_absorb._merge_cast），
        # 否则用 TMDB 图床（经前端缓存代理）
        if actor.get("nfo_thumb"):
            return str(actor["nfo_thumb"])
        if actor.get("profile_path"):
            return f"{image_base}/w300{actor['profile_path']}"
        return None

    meta = EntryMetadata(
        plot=row.overview,
        rating=row.vote_average,
        runtime_minutes=row.runtime_minutes,
        genres=list(row.genres),
        directors=list(row.directors),
        actors=[
            NfoActor(
                name=actor["name"],
                role=actor.get("character") or None,
                thumb=_thumb(actor),
                tmdb_person_id=actor.get("tmdb_person_id"),
            )
            for actor in row.cast
            if actor.get("name")
        ],
        # 档案里带着 NFO 出处就照实说「信息来自 xxx.nfo」——那些字段确实是
        # 从它吸收来的，只是吸收发生在刮削时而不是这次请求里
        nfo_name=row.nfo_name or "",
        source="nfo" if row.nfo_name else "db",
    )
    return meta if meta.has_content() else None


async def _tmdb_fallback_meta(session: AsyncSession, item: MediaItem) -> EntryMetadata | None:
    """本地无刮削 NFO 时的展示兜底：TMDB 实时拉简介/评分/片长/演职员。

    一次 append_to_response=credits 请求拿全；网络失败返回 None（页面退回
    "本地未刮削"提示，不阻断详情）。结果标记 ``source="tmdb"``，前端据此
    注明信息来自 TMDB 而非本地刮削。
    """
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.media_discover import get_tmdb_client
    from movieclaw_media.models import MediaKind as _Kind

    settings = get_settings()
    kind = _Kind(item.kind)
    # 同 _fill_from_tmdb_season：语言按归属库，缓存键带上语言避免跨库串味
    language = effective_language(await scrape_setting_for_item(session, item))
    try:
        # 经持久缓存（SWR）：首次访问后同条目秒开，过期先回旧值后台刷新
        data = await _get_display_cache().get_or_fetch(
            f"meta:{kind.value}:{item.tmdb_id}:{language}",
            fresh_ttl=_DISPLAY_CACHE_FRESH_TTL,
            stale_ttl=_DISPLAY_CACHE_STALE_TTL,
            factory=lambda: get_tmdb_client().get(
                f"{kind.value}/{item.tmdb_id}",
                {"language": language, "append_to_response": "credits"},
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- 兜底信息拉不到不阻断详情页
        logger.warning("条目 #%s 的 TMDB 展示信息拉取失败（详情页降级）：%s", item.id, exc)
        return None

    credits = data.get("credits") or {}
    if kind is _Kind.MOVIE:
        directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
        runtime = data.get("runtime")
    else:
        directors = [c["name"] for c in data.get("created_by", []) if c.get("name")]
        run_times = data.get("episode_run_time") or []
        runtime = run_times[0] if run_times else None
    image_base = settings.tmdb_image_base_url.rstrip("/")
    actors = [
        NfoActor(
            name=c["name"],
            role=(c.get("character") or "").strip() or None,
            thumb=f"{image_base}/w300{c['profile_path']}" if c.get("profile_path") else None,
            tmdb_person_id=c.get("id"),
        )
        for c in credits.get("cast", [])[:40]
        if c.get("name")
    ]
    vote = data.get("vote_average")
    meta = EntryMetadata(
        plot=(data.get("overview") or "").strip() or None,
        rating=round(float(vote), 1) if vote else None,
        runtime_minutes=int(runtime) if runtime else None,
        genres=[g["name"] for g in data.get("genres", []) if g.get("name")],
        directors=directors[:5],
        actors=actors,
        source="tmdb",
    )
    return meta if meta.has_content() else None


async def backfill_streams(
    session: AsyncSession,
    files: list[LibraryFile],
    *,
    limit: int | None = None,
    on_processed: Callable[[], bool | Awaitable[bool]] | None = None,
    on_checkpoint: Callable[[], None | Awaitable[None]] | None = None,
) -> int:
    """补齐未探测的介质详情，并为存量 BDMV 回填 CLPI 语言；返回 ffprobe 数。

    这是「ffprobe 后装」和「旧 BDMV 尚未读取 CLPI」的统一补救路径——扫描对
    已识别且在位的行整体秒过，不会在主循环回头重探。**只由扫描的补探阶段
    调用**（有进度与停止按钮的后台任务，慢没关系，半途而废才是问题），用
    ``on_processed`` 汇报进度——无论最终是否需要 ffprobe，每检查完一个
    候选都回调一次，返回 False 即收尾（用户点了停止）。详情页曾经也会
    限量触发补探，已移除：浏览不碰媒体文件本体（云盘挂载上 ffprobe 读文件
    就是流量与延迟），未探测的行由前端提示用户重新扫描补齐。

    探测失败的行保持原值、下次再试：失败常常是暂时的（挂载还没就绪）。
    失败会记入 media_probe 的失败记忆（进程内、翻倍退避）：退避未到点的行
    本轮直接跳过，坏文件不再让每轮手动扫描都全额付一次 30 秒超时。
    BDMV 已有流但缺 CLPI 版本戳时先读对应 CLPI；只有 CLPI 有效才重探 m2ts，
    随后用 PID 合并，避免缺失元数据导致无意义的大文件读取。
    """
    from movieclaw_api.services.library.scan import disc_main_stream

    probed = 0
    since_checkpoint = 0

    async def keep_going() -> bool:
        nonlocal since_checkpoint
        since_checkpoint += 1
        # 先提交领域数据再让进度回调写 Job：SQLite 同一时刻只允许一个写
        # 事务，顺序反过来会让两个会话互等。即使本批全是无需探测的候选，
        # 也结束读事务后再保存观察进度。
        if since_checkpoint >= _PROBE_COMMIT_EVERY:
            await session.commit()
            since_checkpoint = 0
            if on_checkpoint is not None:
                checkpoint_result = on_checkpoint()
                if inspect.isawaitable(checkpoint_result):
                    await checkpoint_result
        if on_processed is None:
            return True
        result = on_processed()
        return bool(await result) if inspect.isawaitable(result) else result

    for row in files:
        if limit is not None and probed >= limit:
            break
        needs_clpi = row.container == "bluray" and not streams_have_clpi_metadata(
            row.audio_streams, row.subtitle_streams
        )
        # 新增帧率/色彩空间后，历史行两列同时为空时允许整库扫描补探一次。
        # 正常视频至少能取得其中一项，避免个别元数据缺失的文件每轮重复 ffprobe。
        needs_visual_details = row.frame_rate is None and row.color_space is None
        if (
            row.audio_streams is not None and not needs_clpi and not needs_visual_details
        ) or row.state != FileState.IN_PLACE:
            if not await keep_going():
                break
            continue
        if row.file_path.lower().endswith(STRM_EXT):
            if not await keep_going():
                break
            continue  # strm 占位文件没有媒体流，探了必失败，别每轮白试
        if not probe_retry_due(row.file_path):
            # 失败退避中（media_probe 的失败记忆）：上次探测失败且还没到
            # 重试点，跳过省一次几乎必然的失败——坏文件一次就是 30 秒超时。
            # 退避到点后自然回到这条链路。
            if not await keep_going():
                break
            continue
        path = Path(row.file_path)
        target = disc_main_stream(path) if row.container in ("bluray", "dvd") else path
        if target is None or not await asyncio.to_thread(target.exists):
            if not await keep_going():
                break
            continue
        languages = None
        if row.container == "bluray":
            languages = await asyncio.to_thread(read_clpi_languages, target)
            # 已有 ffprobe 流的存量行只缺 CLPI 回填；CLPI 不存在/损坏时不值得
            # 再读一遍大 m2ts。audio_streams=NULL 的行仍按原逻辑补普通规格。
            if row.audio_streams is not None and languages is None:
                if not await keep_going():
                    break
                continue
        probed += 1
        spec = await asyncio.to_thread(probe_media, target)
        if spec is None:
            note_probe_failure(row.file_path)
        else:
            note_probe_success(row.file_path)
        if spec is not None:
            if languages is not None:
                spec = enrich_spec_with_clpi(spec, languages)
            row.audio_streams = list(spec.audio_streams)
            row.subtitle_streams = list(spec.subtitle_streams)
            if row.chapters is None:
                row.chapters = list(spec.chapters)
            # 顺手回填缺失的视频规格（同一次探测的免费产出，不覆盖已有值）
            row.resolution = row.resolution or spec.resolution
            row.video_codec = row.video_codec or spec.video_codec
            # 新探测能把历史上笼统的 HDR10 细化成 Dolby Vision/HDR10+。
            row.hdr = spec.hdr or row.hdr
            row.bit_depth = row.bit_depth or spec.bit_depth
            row.duration_seconds = row.duration_seconds or spec.duration_seconds
            row.bit_rate = row.bit_rate or spec.bit_rate
            row.frame_rate = row.frame_rate or spec.frame_rate
            row.color_space = row.color_space or spec.color_space
            # 补探的终止条件：写上当前字段集版本，这一行下次就不再进补探队列。
            # 少了它，缺色彩标签的文件会每次手动扫描都被白探一遍。
            row.probe_version = PROBE_SCHEMA_VERSION
            if row.file_mtime_ns is None:
                # 播放 ETag 用的 mtime 顺手回填（文件刚探测过，stat 是热的）
                with contextlib.suppress(OSError):
                    row.file_mtime_ns = Path(row.file_path).stat().st_mtime_ns
            row.updated_at = utcnow()
        if not await keep_going():
            break
    # 分批提交而不是攒到最后：整库补探可能要几个小时，中途断电/重启
    # 时已经探完的那部分不该白探。这里无论有无写入都 commit，确保随后
    # 的 Job 进度回调不与本会话的读事务交叠。
    await session.commit()
    if on_checkpoint is not None and since_checkpoint:
        checkpoint_result = on_checkpoint()
        if inspect.isawaitable(checkpoint_result):
            await checkpoint_result
    return probed


# ---------------------------------------------------------------------------
# 真实删除（唯一会动磁盘的删除路径，务必克制）
# ---------------------------------------------------------------------------


@dataclass
class DeleteResult:
    """一次条目删除的结论（全部路径都会回给前端展示）。"""

    removed_paths: list[str] = field(default_factory=list)  # 实际从磁盘删除的目录/文件
    rows_deleted: int = 0  # 删掉的台账行数
    freed_bytes: int = 0  # 释放的空间（按台账 size 估算）
    errors: list[str] = field(default_factory=list)


async def delete_item_files(
    session: AsyncSession,
    library: Library,
    media_item_id: int,
    files: list[LibraryFile],
    all_library_files: list[LibraryFile],
) -> DeleteResult:
    """把条目从库中**彻底删除**：磁盘上的条目目录（视频+NFO+海报+字幕）
    整个清掉，台账行随之删除。

    安全边界：
    - 只删库根路径**之内**的内容，且绝不删根目录本身；
    - 条目目录里混有其他条目的台账文件时，不删目录，只删本条目的文件
      及其同名附属文件（NFO/字幕/图片）；
    - 磁盘删除失败的文件保留台账行（并报错给用户），不制造"账没了文件还在"
      的幽灵——下次扫描会把它当新文件重新入账反而更乱。
    """
    result = DeleteResult()
    roots = [Path(p) for p in library.root_paths]

    # 其他条目占用的路径：判定条目目录是否可整删
    foreign_paths = [
        Path(row.file_path) for row in all_library_files if row.media_item_id != media_item_id
    ]

    dirs_to_remove: list[Path] = []
    files_to_remove: dict[int, Path] = {}  # row.id -> 主文件路径（附属文件删除时一并找）
    covered_rows: dict[Path, list[LibraryFile]] = {}  # 整删目录覆盖的行

    for row in files:
        path = Path(row.file_path)
        if row.state == FileState.MISSING:
            continue  # 无磁盘实体，最后统一清账
        if row.state == FileState.TRASHED:
            # 待回收行按单文件清理：moved 形态的路径在 .movieclaw-trash 里，
            # 绝不能让回收站目录被当作条目目录整删（会卷走其他条目的回收文件）
            assert row.id is not None
            files_to_remove[row.id] = path
            continue
        entry = entry_dir_of(roots, path)
        if entry is None and row.container in ("bluray", "dvd"):
            entry = path  # 直接躺在根下的原盘目录：目录本身就是条目
        if entry is not None and _safe_inside_roots(entry, roots):
            if any(entry in fp.parents or entry == fp for fp in foreign_paths):
                # 目录里混着其他条目：退化为逐文件删除
                assert row.id is not None
                files_to_remove[row.id] = path
            else:
                if entry not in dirs_to_remove:
                    dirs_to_remove.append(entry)
                covered_rows.setdefault(entry, []).append(row)
        elif _safe_inside_roots(path, roots):
            assert row.id is not None
            files_to_remove[row.id] = path
        else:
            result.errors.append(f"「{path}」不在库根路径之内，已跳过（不删库外文件）")

    deleted_row_ids: set[int] = set()

    for directory in dirs_to_remove:
        ok = await asyncio.to_thread(_remove_tree, directory, result)
        if ok:
            for row in covered_rows.get(directory, []):
                assert row.id is not None
                deleted_row_ids.add(row.id)
                result.freed_bytes += row.size_bytes

    by_id = {row.id: row for row in files}
    for row_id, path in files_to_remove.items():
        row = by_id[row_id]
        ok = await asyncio.to_thread(_remove_file_with_sidecars, path, result)
        if ok:
            deleted_row_ids.add(row_id)
            result.freed_bytes += row.size_bytes

    # 缺失行没有磁盘实体，账直接清（待回收行不在此列——它有实体，
    # 删除失败必须保留行，否则"账没了文件还在"，扫描会重新收编）
    for row in files:
        if row.state == FileState.MISSING:
            assert row.id is not None
            deleted_row_ids.add(row.id)

    for row in files:
        if row.id in deleted_row_ids:
            await session.delete(row)
    result.rows_deleted = len(deleted_row_ids)
    await session.commit()
    if result.rows_deleted and library.id is not None:
        await LibraryRepository(session).refresh_stats([library.id])

    if result.removed_paths:
        logger.info(
            "已从磁盘删除条目 #%s 的 %d 个路径（库「%s」，释放约 %.1f GB）：%s",
            media_item_id,
            len(result.removed_paths),
            library.name,
            result.freed_bytes / 1024**3,
            "；".join(result.removed_paths),
        )
    return result


async def delete_single_file(
    session: AsyncSession,
    library: Library,
    row: LibraryFile,
    item_rows: list[LibraryFile],
    all_library_files: list[LibraryFile],
) -> DeleteResult:
    """从磁盘删除条目的**单个文件**（多版本洗版 / 删某一集重下的出口）。

    与整条目删除（``delete_item_files``）的分工：
    - 只删这一个主文件及其同名附属文件（NFO/字幕/图片），条目目录与其他
      文件（含剧集的 tvshow.nfo/海报）纹丝不动；
    - **该行是条目在本库的最后一行时退化为整条目删除**——只删文件会留下
      一个装着 NFO/海报的空刮削目录，下次扫描它还在，违背"不留刮削残渣"
      的原则（调用方须在确认界面明确告知这一升级）；
    - missing 行没有磁盘实体，直接清台账；
    - 磁盘删除失败保留台账行（与整条目删除同规则，不制造幽灵账）。
    """
    assert row.media_item_id is not None
    if len(item_rows) == 1:
        return await delete_item_files(
            session, library, row.media_item_id, item_rows, all_library_files
        )

    result = DeleteResult()
    if row.state == FileState.MISSING:
        await session.delete(row)
        result.rows_deleted = 1
        await session.commit()
        if library.id is not None:
            await LibraryRepository(session).refresh_stats([library.id])
        return result

    path = Path(row.file_path)
    roots = [Path(p) for p in library.root_paths]
    if not _safe_inside_roots(path, roots):
        result.errors.append(f"「{path}」不在库根路径之内，已跳过（不删库外文件）")
        return result

    # 原盘目录形态整树删除前查台账：监听导入按站点原始目录结构落盘，
    # 新版本文件可能就在旧原盘目录里面——rmtree 会把它一起炸掉
    if path.is_dir():
        prefix = str(path).rstrip("/") + "/"
        if any(
            other.id != row.id and other.file_path.startswith(prefix) for other in all_library_files
        ):
            result.errors.append(
                f"「{path}」目录内还有其他在案文件（可能是新入库的版本），已跳过整目录删除"
            )
            return result

    ok = await asyncio.to_thread(_remove_file_with_sidecars, path, result)
    if ok:
        result.rows_deleted = 1
        result.freed_bytes = row.size_bytes
        await session.delete(row)
        await session.commit()
        if library.id is not None:
            await LibraryRepository(session).refresh_stats([library.id])
        logger.info(
            "已从磁盘删除条目 #%s 的单个文件（库「%s」，释放约 %.1f GB）：%s",
            row.media_item_id,
            library.name,
            result.freed_bytes / 1024**3,
            "；".join(result.removed_paths),
        )
    return result


def _safe_inside_roots(path: Path, roots: list[Path]) -> bool:
    """路径必须严格位于某个库根之内（不等于根本身）——删除的硬边界。"""
    return any(root in path.parents for root in roots)


def _remove_tree(directory: Path, result: DeleteResult) -> bool:
    """整删条目目录（同步，放线程池）。目录已不存在视为成功（幂等）。"""
    if not directory.exists():
        result.removed_paths.append(str(directory))
        return True
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        result.errors.append(f"删除目录失败：{directory}（{exc}）")
        return False
    result.removed_paths.append(str(directory))
    return True


def _remove_file_with_sidecars(path: Path, result: DeleteResult) -> bool:
    """删单个视频文件及其同名附属文件（NFO/字幕/图片）。

    主文件删除失败返回 False（台账保留）；附属文件失败只记错误不影响结论。
    原盘目录（path 是目录）整目录删除。
    """
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            os.remove(path)
    except OSError as exc:
        result.errors.append(f"删除文件失败：{path}（{exc}）")
        return False
    result.removed_paths.append(str(path))

    if path.suffix:
        stem = path.stem.lower()
        try:
            entries = list(path.parent.iterdir())
        except OSError:
            return True
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.stem.lower()
            is_sidecar = entry.suffix.lower() in _SUBTITLE_EXTS | {".nfo"} | set(_ART_EXTS)
            if is_sidecar and (
                name == stem or name.startswith(stem + ".") or name.startswith(stem + "-")
            ):
                try:
                    os.remove(entry)
                    result.removed_paths.append(str(entry))
                except OSError as exc:
                    result.errors.append(f"删除附属文件失败：{entry}（{exc}）")
    return True
