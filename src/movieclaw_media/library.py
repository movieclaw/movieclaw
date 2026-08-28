"""媒体身份层的纯数据获取与收敛逻辑（无数据库依赖）。

本模块是订阅功能"媒体条目"的 TMDB 侧实现（docs/design/subscription.md 第 1 节）：

- ``fetch_media_profile``：一次拉齐条目的身份信息（外部 ID、标题、别名集合、
  季集结构），产出与持久层解耦的 ``MediaProfile``；
- ``resolve_douban_to_tmdb``：豆瓣入口的收敛通路，命中 / 歧义 / 未找到三分支。
  剧集走"多路查询 + 季级证据对齐"（同时解出 TMDB 季号），证据不足时回落
  原有的"标题+年份"规则；电影只走后者。

职责边界：本包不依赖 movieclaw_db，落库编排由 API 层的
``movieclaw_api.services.media_library`` 完成。
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import NamedTuple

from pydantic import BaseModel, Field

from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient, TmdbError

# 别名收集范围：中文圈 + 英语圈的地区别名，加 zh/en 两种语言的译名。
# 种子命名以英文为主、副标题以中文为主，这两组覆盖了匹配内核的需要。
_ALIAS_REGIONS = frozenset({"CN", "HK", "TW", "SG", "US", "GB"})
_ALIAS_LANGUAGES = frozenset({"zh", "en"})

# 歧义时返回给前端确认弹层的候选数量上限
_MAX_CANDIDATES = 8

# 季号上界：越界即视为误解析（豆瓣标题里不会出现超过这个数的季号）
_MAX_SEASON_NUMBER = 100


class EpisodeInfo(BaseModel):
    """单集信息（订阅骨架 + 展示元数据，media_episode 表的传输形态）。

    air_date 保持 ISO 字符串形态（TMDB 原样），落库时再解析。
    """

    episode_number: int
    name: str = ""
    air_date: str | None = Field(default=None, description="ISO 日期字符串；None=未定档")
    overview: str | None = None
    runtime_minutes: int | None = None
    vote_average: float | None = None
    still_path: str | None = Field(default=None, description="TMDB 剧照相对路径")


class SeasonProfile(BaseModel):
    """一季的骨架与展示信息：季级订阅、wanted 生成与详情页所需。"""

    season_number: int
    name: str = ""
    air_date: date | None = None
    episode_count: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    episodes: list[EpisodeInfo] = Field(default_factory=list)


class CastMember(BaseModel):
    """演员表一行（media_metadata.cast JSON 元素的传输形态）。"""

    name: str
    character: str | None = None
    order: int = 0
    profile_path: str | None = Field(default=None, description="TMDB 头像相对路径")
    # 姓名不是身份（同名不同人 / 一人多译名），人物页的链接与 person 表的匹配
    # 一律走这个 id。老数据的 JSON 里没有这个字段，读取方按缺失处理
    tmdb_person_id: int | None = Field(default=None, description="TMDB 影人 ID")


class PersonCredit(BaseModel):
    """一条参演/执导关系（喂 person 与 media_item_person 两张表）。

    与 ``CastMember`` 并存而不是复用：那个是「该影片档案里的演员表」（进
    media_metadata 的 JSON、写 NFO），这个是「人与影片的关系」（进关系表），
    两者字段需求不同——前者不需要 department，后者不需要为缺 id 的人留位置
    （没有 person id 就没法建关系，直接跳过）。
    """

    tmdb_person_id: int
    name: str
    original_name: str | None = None
    profile_path: str | None = None
    department: str = Field(description="cast=演员 / director=导演（剧集为主创）")
    character: str | None = None
    credit_order: int = 0


class MediaProfile(BaseModel):
    """条目档案的传输模型：TMDB 原始数据 → 持久层字段的中间形态。

    身份字段供 media_item（匹配最小闭包），展示字段供 media_metadata
    （自足媒体库的作品档案，docs/design/metadata.md）；同一次请求拉齐。
    """

    kind: MediaKind
    tmdb_id: int
    imdb_id: str | None = None
    title: str
    original_title: str
    year: int | None = None
    aliases: list[str] = Field(default_factory=list)
    status: str | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    seasons: list[SeasonProfile] = Field(default_factory=list, description="仅剧集非空")

    # -- 展示层（media_metadata）--------------------------------------------
    overview: str | None = None
    tagline: str | None = None
    genres: list[str] = Field(default_factory=list)
    # genre 的 TMDB ID（与 genres 同源同序）：genres 是刮削语言的本地化名，
    # 媒体库路由的匹配必须用语言无关的 ID（docs/design/library-routing.md）
    genre_ids: list[int] = Field(default_factory=list)
    runtime_minutes: int | None = None
    release_date: date | None = None
    content_rating: str | None = None
    original_language: str | None = None
    origin_countries: list[str] = Field(default_factory=list)
    studios: list[str] = Field(default_factory=list)
    vote_average: float | None = None
    vote_count: int | None = None
    directors: list[str] = Field(default_factory=list)
    cast: list[CastMember] = Field(default_factory=list)
    # 人物页的数据来源。与上面两个字段同源但独立呈现：那两个是展示用的
    # 姓名/演员表（进 media_metadata），这个是带 TMDB person id 的关系集合
    people: list[PersonCredit] = Field(default_factory=list)


def extract_genre_ids(data: dict) -> list[int]:
    """TMDB 详情 → genre ID 列表。

    刮削（fetch_media_profile）与路由的轻量详情兜底（library_routing）
    共用本函数——两处口径必须一致，各写各的迟早漂移。
    """
    return [g["id"] for g in data.get("genres", []) if g.get("id") is not None]


def extract_origin_countries(data: dict) -> list[str]:
    """TMDB 详情 → 制片国家/地区码：剧集用 origin_country，电影回落
    production_countries（口径与 extract_genre_ids 同理，两处消费共用）。"""
    return [c for c in data.get("origin_country", []) if c] or [
        c.get("iso_3166_1") for c in data.get("production_countries", []) if c.get("iso_3166_1")
    ]


async def fetch_media_profile(
    client: TmdbClient,
    kind: MediaKind,
    tmdb_id: int,
    *,
    languages: Sequence[str] = ("zh-CN", "en-US"),
    image_prefs: ImagePrefs | None = None,
    cert_countries: Sequence[str] = ("CN", "US"),
) -> MediaProfile:
    """拉取条目的完整档案（一次详情请求 + 剧集逐季并发拉集列表）。

    ``languages`` 是元数据语言优先级（docs/design/scrape-customization.md
    §2.1）：首位是主语言（请求语言），标题/简介/tagline 在主语言缺失时按
    顺序回落——回落数据来自 append_to_response 里**同一次请求已拉回的
    translations**，零额外请求；分集文本不在 translations 里，主语言覆盖率
    过低的季按次位语言整季重拉一次（每季至多一次，见 ``_season_text_poor``）。

    ``image_prefs`` 是选图偏好（§2.2）；``cert_countries`` 是分级地区优先级。
    默认参数即历史行为（zh-CN + en 兜底、TMDB 默认海报、无文字背景优先）。
    """
    languages = list(languages) or ["zh-CN"]
    primary = languages[0]
    prefs = image_prefs or ImagePrefs()
    rating_append = "release_dates" if kind is MediaKind.MOVIE else "content_ratings"
    # images 默认只回当前语言的图，几乎必空；显式带上"无语言"(null，即无
    # 文字烧录的干净图)与偏好里出现的具体语种，选图策略才有候选可挑
    image_languages = image_language_param(prefs, primary)
    data = await client.get(
        f"{kind.value}/{tmdb_id}",
        {
            "language": primary,
            "append_to_response": (
                f"alternative_titles,translations,external_ids,credits,images,{rating_append}"
            ),
            "include_image_language": image_languages,
        },
    )
    # 偏好里含「原始语言」时，请求前无从得知它是哪种语言；详情回来后若
    # 该语言不在已拉取的图片语言集合里，补拉一次 images 合并候选。
    # 只有显式配置 orig 的用户才会走到这个分支，且中英日韩之外才需要补。
    await _ensure_original_language_images(client, kind, tmdb_id, data, prefs, image_languages)

    title = data.get("title") or data.get("name") or ""
    original_title = data.get("original_title") or data.get("original_name") or title
    release_date = data.get("release_date") or data.get("first_air_date") or ""

    translations = _translation_index(data)
    # 标题回落：主语言没有翻译时（TMDB 会静默退回原名），按优先级取
    # 下一语言的译名。主语言有翻译时 data 里的 title 就是它，不用动
    if not _translation_text(translations, primary, "title", "name"):
        for lang in languages[1:]:
            fallback_title = _translation_text(translations, lang, "title", "name")
            if fallback_title:
                title = fallback_title
                break

    seasons: list[SeasonProfile] = []
    if kind is MediaKind.TV:
        numbers = [
            s["season_number"]
            for s in data.get("seasons", [])
            if s.get("season_number") is not None
        ]
        seasons = list(
            await asyncio.gather(*(_fetch_season(client, tmdb_id, n, primary) for n in numbers))
        )
        if len(languages) > 1:
            await _fallback_poor_seasons(client, tmdb_id, seasons, languages[1])

    overview = (data.get("overview") or "").strip() or None
    tagline = (data.get("tagline") or "").strip() or None
    for lang in languages[1:]:
        if overview is not None and tagline is not None:
            break
        overview = overview or _translation_text(translations, lang, "overview")
        tagline = tagline or _translation_text(translations, lang, "tagline")

    original_language = data.get("original_language") or None
    poster_langs = resolve_image_languages(
        prefs.poster_langs, primary_language=primary, original_language=original_language
    )
    backdrop_langs = resolve_image_languages(
        prefs.backdrop_langs, primary_language=primary, original_language=original_language
    )
    if prefs.poster_mode == "language":
        # 用户显式要按语言优先级挑：策略结果优先，候选全空才回 TMDB 默认
        poster_path = pick_poster(data, poster_langs, min_width=prefs.poster_min_width) or data.get(
            "poster_path"
        )
    else:
        # 海报与发现页同源：优先 TMDB 默认 poster_path（发现页列表接口给的
        # 就是这张），订阅建档后展示不跳变；默认缺失时才用选图策略兜底
        poster_path = data.get("poster_path") or pick_poster(
            data, poster_langs, min_width=prefs.poster_min_width
        )

    return MediaProfile(
        kind=kind,
        tmdb_id=tmdb_id,
        imdb_id=(data.get("external_ids") or {}).get("imdb_id") or None,
        title=title,
        original_title=original_title,
        year=_parse_year(release_date),
        aliases=_build_aliases(data, title, original_title),
        status=data.get("status") or None,
        poster_path=poster_path,
        backdrop_path=(
            pick_backdrop(data, backdrop_langs, min_width=prefs.backdrop_min_width)
            or data.get("backdrop_path")
        ),
        seasons=seasons,
        overview=overview,
        tagline=tagline,
        genres=[g["name"] for g in data.get("genres", []) if g.get("name")],
        genre_ids=extract_genre_ids(data),
        runtime_minutes=_parse_runtime(kind, data),
        release_date=_parse_date(release_date),
        content_rating=_parse_certification(kind, data, cert_countries),
        original_language=original_language,
        origin_countries=extract_origin_countries(data),
        studios=_parse_studios(kind, data),
        vote_average=round(float(data["vote_average"]), 1) if data.get("vote_average") else None,
        vote_count=data.get("vote_count") or None,
        directors=_parse_directors(kind, data),
        cast=_parse_cast(data),
        people=_parse_people(kind, data),
    )


# ---------------------------------------------------------------------------
# 语言回落（docs/design/scrape-customization.md §2.1）
# ---------------------------------------------------------------------------


def _translation_index(data: dict) -> dict[str, dict]:
    """translations 载荷 → 「语言标签 → 译文字典」索引。

    同时登记精确标签（zh-CN）与裸语言（zh，取第一个地区版本）两个键，
    查询时先精确后裸——优先级列表里 zh-TW 与 zh-CN 因此能各取所需。
    """
    index: dict[str, dict] = {}
    for trans in (data.get("translations") or {}).get("translations") or []:
        lang = trans.get("iso_639_1")
        if not lang:
            continue
        payload = trans.get("data") or {}
        region = trans.get("iso_3166_1")
        if region:
            index.setdefault(f"{lang}-{region}", payload)
        index.setdefault(lang, payload)
    return index


def _translation_text(index: dict[str, dict], tag: str, *fields: str) -> str | None:
    """某语言译文里第一个非空字段；该语言无翻译或字段全空返回 None。"""
    payload = index.get(tag) or index.get(tag.split("-")[0])
    if not payload:
        return None
    for field_name in fields:
        value = (payload.get(field_name) or "").strip()
        if value:
            return value
    return None


# 分集占位名：TMDB 无翻译时返回的"第 N 集"/"Episode N"式占位。占比过高
# 说明该季在主语言下基本没有译文，值得按次位语言整季重拉一次
_EP_PLACEHOLDER = re.compile(r"^(第\s*\d+\s*[集话話]|episode\s*\d+)$", re.IGNORECASE)


def _is_placeholder_name(name: str) -> bool:
    cleaned = name.strip()
    return not cleaned or bool(_EP_PLACEHOLDER.match(cleaned))


def _season_text_poor(season: SeasonProfile) -> bool:
    """该季主语言文本覆盖率是否过低（≥60% 分集是占位名/空名）。"""
    episodes = season.episodes
    if not episodes:
        return False
    placeholders = sum(_is_placeholder_name(e.name) for e in episodes)
    return placeholders >= max(1, math.ceil(len(episodes) * 0.6))


async def _fallback_poor_seasons(
    client: TmdbClient, tmdb_id: int, seasons: list[SeasonProfile], fallback_language: str
) -> None:
    """对文本覆盖率过低的季，按次位语言整季重拉一次并就地合并译文。

    分集标题/简介不在 translations 载荷里，逐集拉翻译是每集一次请求，
    不可接受；整季重拉每季至多一次、只对被判定的季触发，请求量有闸。
    拉取失败静默保留主语言结果（占位名依然可用）。
    """
    flagged = [s for s in seasons if _season_text_poor(s)]
    if not flagged:
        return
    results = await asyncio.gather(
        *(_fetch_season(client, tmdb_id, s.season_number, fallback_language) for s in flagged),
        return_exceptions=True,
    )
    for season, fallback in zip(flagged, results, strict=True):
        if not isinstance(fallback, SeasonProfile):
            continue
        by_number = {e.episode_number: e for e in fallback.episodes}
        for episode in season.episodes:
            other = by_number.get(episode.episode_number)
            if other is None:
                continue
            if _is_placeholder_name(episode.name) and not _is_placeholder_name(other.name):
                episode.name = other.name
            if not episode.overview and other.overview:
                episode.overview = other.overview
        if not season.overview and fallback.overview:
            season.overview = fallback.overview


# ---------------------------------------------------------------------------
# 选图策略（docs/design/metadata.md 6.3 / scrape-customization.md §2.2）
# ---------------------------------------------------------------------------
#
# TMDB 详情里的 backdrop_path 是"按投票排序的第一张"，直接用有两个通病：
# ① 票王常是**烧了片名文字的横图**（海报化背景），铺全屏做沉浸背景很脏；
# ② 少量投票就能把一张低分辨率图顶上去。因此从 images 全量候选里按
# **语言优先级分档**重选：逐档取第一个有候选的语言（None=无文字），档内按
# 分辨率门槛 + 加权票数排序。海报/背景的语言优先级、门槛都来自 ImagePrefs
# （用户可配置），默认值即历史行为。规则是纯函数，便于单测与调参。

# 背景图的分辨率门槛：低于 1080p 宽度的图铺满视口会糊
_MIN_BACKDROP_WIDTH = 1920
# 海报的分辨率门槛（w500 展示位，500 宽以下的源图放大会糊）
_MIN_POSTER_WIDTH = 500


@dataclass(frozen=True)
class ImagePrefs:
    """选图偏好（「设置 → 刮削与整理 → 图片」）。

    语言项是 token：具体语言码（zh/en/ja…）或三个特殊项——``meta`` 跟随
    元数据主语言、``orig`` 条目的原声语言（original_language，随条目解析）、
    ``null`` 无文字。默认值 = 历史写死行为。
    """

    poster_mode: str = "default"  # default=TMDB 默认；language=按语言优先级挑选
    poster_langs: tuple[str, ...] = ("meta", "en", "null")
    backdrop_langs: tuple[str, ...] = ("null", "meta", "en")
    poster_min_width: int = _MIN_POSTER_WIDTH
    backdrop_min_width: int = _MIN_BACKDROP_WIDTH


def resolve_image_languages(
    tokens: Sequence[str], *, primary_language: str, original_language: str | None
) -> list[str | None]:
    """偏好 token → TMDB 图片语言值列表（None=无文字），保序去重。

    ``orig`` 在条目缺 original_language 时跳过（不猜）。
    """
    resolved: list[str | None] = []
    seen: set[str | None] = set()
    for token in tokens:
        value: str | None
        if token == "meta":
            value = primary_language.split("-")[0]
        elif token == "orig":
            orig = (original_language or "").strip()
            if not orig:
                continue
            value = orig
        elif token == "null":
            value = None
        else:
            value = token
        if value not in seen:
            seen.add(value)
            resolved.append(value)
    return resolved


def image_language_param(prefs: ImagePrefs, primary_language: str) -> str:
    """详情请求 include_image_language 的取值：由偏好推导，而非写死。

    始终含 null（无文字）、主语言与 en（历史口径，也是候选最多的语言）；
    偏好里出现的具体语种一并带上。meta/orig 特殊项此时无法解析（orig 要
    详情回来才知道），由 ``_ensure_original_language_images`` 补拉兜底。
    """
    codes = ["null", primary_language.split("-")[0], "en"]
    for token in (*prefs.poster_langs, *prefs.backdrop_langs):
        if token not in {"meta", "orig", "null"} and token not in codes:
            codes.append(token)
    return ",".join(dict.fromkeys(codes))


async def _ensure_original_language_images(
    client: TmdbClient,
    kind: MediaKind,
    tmdb_id: int,
    data: dict,
    prefs: ImagePrefs,
    requested: str,
) -> None:
    """偏好含「原始语言」而详情请求未覆盖该语言时，补拉一次候选图并合并。

    失败静默——选图会按后续档位回落，不阻断档案主体。
    """
    if "orig" not in (*prefs.poster_langs, *prefs.backdrop_langs):
        return
    orig = ((data.get("original_language") or "").strip()) or None
    if not orig or orig in requested.split(","):
        return
    try:
        extra = await client.get(f"{kind.value}/{tmdb_id}/images", {"include_image_language": orig})
    except Exception:  # noqa: BLE001 -- 补拉失败按无该语言候选处理
        return
    images = data.setdefault("images", {})
    for key in ("posters", "backdrops"):
        merged = images.get(key) or []
        known = {i.get("file_path") for i in merged}
        merged.extend(i for i in extra.get(key) or [] if i.get("file_path") not in known)
        images[key] = merged


def _weighted_score(image: dict) -> float:
    """加权评分：均分 × log(票数+1)。

    防"1 票 10 分"的冷门图冒顶——纯按 vote_average 排序时，只有个位数
    投票的图会盖过几百票的公认好图（TMDB 自己的默认排序即有此病）。
    """
    average = float(image.get("vote_average") or 0.0)
    count = int(image.get("vote_count") or 0)
    return average * math.log(count + 1)


def _sorted_candidates(images: list[dict], min_width: int) -> list[dict]:
    """达到分辨率门槛的候选按加权分降序；门槛过滤掉全部时不设门槛重来
    （宁可给一张小图，也不要没有图）。"""
    pool = [i for i in images if int(i.get("width") or 0) >= min_width] or list(images)
    return sorted(pool, key=_weighted_score, reverse=True)


def _pick_by_tiers(images: list[dict], langs: Sequence[str | None], min_width: int) -> str | None:
    """逐语言档取第一个有候选的档位的最优图；全部档位落空退回全量最优。"""
    for lang in langs:
        pool = [i for i in images if i.get("iso_639_1") == lang]
        if pool:
            ranked = _sorted_candidates(pool, min_width)
            return ranked[0].get("file_path")
    ranked = _sorted_candidates(list(images), min_width)
    return ranked[0].get("file_path") if ranked else None


def pick_backdrop(
    data: dict,
    langs: Sequence[str | None] = (None,),
    *,
    min_width: int = _MIN_BACKDROP_WIDTH,
) -> str | None:
    """按语言档挑一张沉浸背景。默认档位 ``(None,)`` 即历史行为：无文字
    优先，全是带字图时退回加权分最高的一张。没有候选返回 None
    （调用方回落 TMDB 默认 backdrop_path）。"""
    images = (data.get("images") or {}).get("backdrops") or []
    if not images:
        return None
    return _pick_by_tiers(images, langs, min_width)


def pick_poster(
    data: dict,
    langs: Sequence[str | None],
    *,
    min_width: int = _MIN_POSTER_WIDTH,
) -> str | None:
    """按语言档挑一张海报（档内按分辨率门槛 + 加权票数）。

    注意：poster_mode=default 时建档/刷新的海报以 TMDB 默认 poster_path
    为准（与发现页一致，见 fetch_media_profile），本函数只做默认缺失时的
    兜底与换图弹层的候选排序（用户仍能一键选到目标语言版）。
    """
    images = (data.get("images") or {}).get("posters") or []
    if not images:
        return None
    return _pick_by_tiers(images, langs, min_width)


def _tier_ordered(images: list[dict], langs: Sequence[str | None], min_width: int) -> list[dict]:
    """候选全量按语言档拼接排序：各档内部按加权分，档间按优先级。"""
    ordered: list[dict] = []
    used: set[int] = set()
    for lang in langs:
        pool = [i for i in images if i.get("iso_639_1") == lang and id(i) not in used]
        if pool:
            for image in _sorted_candidates(pool, min_width):
                ordered.append(image)
                used.add(id(image))
    rest = [i for i in images if id(i) not in used]
    if rest:
        ordered.extend(_sorted_candidates(rest, min_width))
    return ordered


def list_image_candidates(
    data: dict,
    poster_langs: Sequence[str | None],
    backdrop_langs: Sequence[str | None] = (None,),
    *,
    poster_min_width: int = _MIN_POSTER_WIDTH,
    backdrop_min_width: int = _MIN_BACKDROP_WIDTH,
) -> tuple[list[dict], list[dict]]:
    """条目的全部候选图 (海报, 背景)，按各自偏好的档位排序返回。

    供"更换图片"弹层展示——排序与自动选图（``_pick_by_tiers``）同一套
    规则，列表第一张就是自动策略会选的那张，用户一眼看出"默认给的是哪张"。
    """
    images = data.get("images") or {}
    posters = images.get("posters") or []
    backdrops = images.get("backdrops") or []
    poster_list = _tier_ordered(posters, poster_langs, poster_min_width) if posters else []
    backdrop_list = (
        _tier_ordered(backdrops, backdrop_langs, backdrop_min_width) if backdrops else []
    )
    return poster_list, backdrop_list


def _parse_runtime(kind: MediaKind, data: dict) -> int | None:
    if kind is MediaKind.MOVIE:
        runtime = data.get("runtime")
    else:
        run_times = data.get("episode_run_time") or []
        runtime = run_times[0] if run_times else None
    return int(runtime) if runtime else None


def _parse_certification(
    kind: MediaKind, data: dict, countries: Sequence[str] = ("CN", "US")
) -> str | None:
    """分级：按 ``countries`` 优先级取第一个有数据的地区，全落空取第一个
    非空（电影与剧集接口结构不同）。"""
    entries: list[tuple[str, str]] = []
    if kind is MediaKind.MOVIE:
        for country in (data.get("release_dates") or {}).get("results", []):
            for release in country.get("release_dates", []):
                cert = (release.get("certification") or "").strip()
                if cert:
                    entries.append((country.get("iso_3166_1") or "", cert))
                    break
    else:
        for country in (data.get("content_ratings") or {}).get("results", []):
            cert = (country.get("rating") or "").strip()
            if cert:
                entries.append((country.get("iso_3166_1") or "", cert))
    for region in countries:
        for iso, cert in entries:
            if iso == region:
                return cert
    return entries[0][1] if entries else None


def _parse_studios(kind: MediaKind, data: dict) -> list[str]:
    source = data.get("production_companies") if kind is MediaKind.MOVIE else data.get("networks")
    return [s["name"] for s in source or [] if s.get("name")][:5]


def _parse_directors(kind: MediaKind, data: dict) -> list[str]:
    if kind is MediaKind.MOVIE:
        crew = (data.get("credits") or {}).get("crew", [])
        names = [c["name"] for c in crew if c.get("job") == "Director" and c.get("name")]
    else:
        names = [c["name"] for c in data.get("created_by", []) if c.get("name")]
    return names[:5]


# 演员数量上限：详情页与 NFO 展示前 40 位足够（与 library_nfo._MAX_ACTORS 一致）
_MAX_CAST = 40


def _parse_cast(data: dict) -> list[CastMember]:
    return [
        CastMember(
            name=c["name"],
            character=(c.get("character") or "").strip() or None,
            order=c.get("order") or 0,
            profile_path=c.get("profile_path"),
            tmdb_person_id=c.get("id"),
        )
        for c in (data.get("credits") or {}).get("cast", [])[:_MAX_CAST]
        if c.get("name")
    ]


def _parse_people(kind: MediaKind, data: dict) -> list[PersonCredit]:
    """credits → 参演/执导关系集合（只收 cast 与导演/主创两类，见 MediaItemPerson）。

    没有 TMDB person id 的条目直接跳过：关系表以 id 为身份，拿姓名硬凑只会
    把同名不同人合并到一起。一人分饰两角时把角色名合并成「A / B」，
    与关系表 (media_item_id, person_id, department) 的唯一键对齐。
    """
    credits = data.get("credits") or {}
    people: dict[tuple[int, str], PersonCredit] = {}

    def add(raw: dict, department: str, *, character: str | None, order: int) -> None:
        person_id = raw.get("id")
        name = (raw.get("name") or "").strip()
        if not isinstance(person_id, int) or not name:
            return
        key = (person_id, department)
        existing = people.get(key)
        if existing is None:
            people[key] = PersonCredit(
                tmdb_person_id=person_id,
                name=name,
                original_name=(raw.get("original_name") or "").strip() or None,
                profile_path=raw.get("profile_path"),
                department=department,
                character=character,
                credit_order=order,
            )
            return
        # 同一个人同一身份出现第二次：合并角色名，顺序取更靠前的那个
        if character and character not in (existing.character or ""):
            existing.character = (
                f"{existing.character} / {character}" if existing.character else character
            )
        existing.credit_order = min(existing.credit_order, order)

    for c in credits.get("cast", [])[:_MAX_CAST]:
        add(
            c,
            "cast",
            character=(c.get("character") or "").strip() or None,
            order=c.get("order") or 0,
        )

    if kind is MediaKind.MOVIE:
        crew = [c for c in credits.get("crew", []) if c.get("job") == "Director"]
    else:
        crew = list(data.get("created_by") or [])
    for i, c in enumerate(crew[:5]):
        add(c, "director", character=None, order=i)

    return list(people.values())


async def _fetch_season(
    client: TmdbClient, tmdb_id: int, season_number: int, language: str
) -> SeasonProfile:
    data = await client.get(f"tv/{tmdb_id}/season/{season_number}", {"language": language})
    episodes = [
        EpisodeInfo(
            episode_number=e["episode_number"],
            name=e.get("name") or "",
            air_date=e.get("air_date") or None,
            overview=(e.get("overview") or "").strip() or None,
            runtime_minutes=int(e["runtime"]) if e.get("runtime") else None,
            vote_average=round(float(e["vote_average"]), 1) if e.get("vote_average") else None,
            still_path=e.get("still_path"),
        )
        for e in data.get("episodes", [])
        if e.get("episode_number") is not None
    ]
    return SeasonProfile(
        season_number=season_number,
        name=data.get("name") or "",
        air_date=_parse_date(data.get("air_date") or ""),
        # 详情季对象可能带 episode_count；集列表已拉到时以实际长度为准
        episode_count=len(episodes) or data.get("episode_count"),
        overview=(data.get("overview") or "").strip() or None,
        poster_path=data.get("poster_path"),
        episodes=episodes,
    )


def _build_aliases(data: dict, title: str, original_title: str) -> list[str]:
    """构建别名集合：主标题 + 原名 + 地区别名 + zh/en 译名，保序精确去重。

    存原样文本——归一化（大小写/全半角/繁简）是匹配内核的职责，
    规则进化时数据无需重写。
    """
    collected: list[str] = [title, original_title]

    alt = data.get("alternative_titles") or {}
    # TMDB 的接口差异：电影用 "titles" 键，剧集用 "results" 键
    for entry in alt.get("titles") or alt.get("results") or []:
        if entry.get("iso_3166_1") in _ALIAS_REGIONS:
            collected.append(entry.get("title") or "")

    for trans in (data.get("translations") or {}).get("translations") or []:
        if trans.get("iso_639_1") in _ALIAS_LANGUAGES:
            payload = trans.get("data") or {}
            collected.append(payload.get("title") or payload.get("name") or "")

    seen: set[str] = set()
    aliases: list[str] = []
    for text in collected:
        cleaned = text.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            aliases.append(cleaned)
    return aliases


def _parse_year(iso_date: str) -> int | None:
    if len(iso_date) >= 4 and iso_date[:4].isdigit():
        return int(iso_date[:4])
    return None


def _parse_date(iso_date: str) -> date | None:
    try:
        return date.fromisoformat(iso_date)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 豆瓣入口收敛：标题+年份搜索兜底（设计稿 1.5 的②通路）
# ---------------------------------------------------------------------------


class ResolveStatus(StrEnum):
    """收敛结果三分支。"""

    MATCHED = "matched"  # 唯一命中，可直接建档
    AMBIGUOUS = "ambiguous"  # 多候选，需用户在弹层确认一次
    NOT_FOUND = "not_found"  # TMDB 未收录，不建无锚条目


class ResolveCandidate(BaseModel):
    """返回给确认弹层的候选条目。"""

    tmdb_id: int
    title: str
    original_title: str
    year: int | None = None
    poster_path: str | None = None


class DoubanResolution(BaseModel):
    """豆瓣→TMDB 收敛结果。matched 时 tmdb_id 非空；ambiguous 时 candidates 非空。"""

    status: ResolveStatus
    tmdb_id: int | None = None
    candidates: list[ResolveCandidate] = Field(default_factory=list)
    suggested_season: int | None = Field(
        default=None,
        description=(
            "可用于订阅表单预勾选的 TMDB 季号；None=无建议，按调用方原默认规则处理。"
            "仅在两个条件同时成立时给出：① 走的是季级证据通路（兜底通路给不出可信"
            "季号）；② 该豆瓣条目确实是「季专属」的——标题带显式「第N季」后缀，"
            "或证据定案到了第一季之后的某一季。整剧条目（普通剧名、证据落在第一季）"
            "不给建议，否则会把「勾选全部已播季」窄化成只勾一季"
        ),
    )


# ---------------------------------------------------------------------------
# 证据通路：用豆瓣条目自带的首播日期把「哪一季」也一起定下来
#
# 为什么需要它：豆瓣把剧集按季拆成独立条目（「中餐厅 第十季」是一条，
# 「中餐厅 第九季」是另一条），而 TMDB 是一剧一条、季挂在下面。于是
#   ① 整段标题拿去搜必然 0 结果——TMDB 的剧名里没有「第十季」；
#   ② 豆瓣给的年份是**这一季**的年份，与 TMDB 的**整剧首播年**对不上，
#      老通路的年份过滤和「标题+年份精确相等」两条判定同时落空。
#
# 收敛策略：不去判断「该不该剥掉季号」——这个问题没有可靠信号（「歌手2026」
# 的 2026 是品牌名不是季号，「问心2」的 2 可能就是片名的一部分）。改成把各种
# 形态都拿去查生成候选，再用豆瓣条目自带的硬证据裁决：**豆瓣的首播日期与
# TMDB 某一季的 air_date 对齐**是极强的身份信号，它同时回答了「是哪部剧」
# 和「是哪一季」两个问题——后者尤其重要，实测豆瓣季号有 13% 与 TMDB 不一致
# （豆瓣《奔跑吧 第十季》= TMDB S14，《飞出个未来 第十四季》= TMDB S11），
# 只认剧不认季会让用户默认勾选到错误年份的内容。
# ---------------------------------------------------------------------------

# 豆瓣季号只有「第N季」一种形态，N 为阿拉伯数字或中文数字。这里不复用
# movieclaw_enrich 的同类解析：那是种子发布名的解析层，与本包平级，为一个
# 纯函数跨包依赖不划算，且两边的输入分布完全不同（乱序发布名 vs 规整标题）。
_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}  # fmt: skip
_SEASON_SUFFIX_RE = re.compile(r"\s*第\s*([0-9]{1,3}|[一二两三四五六七八九十百]{1,4})\s*季\s*$")
# 别名里的季号形态更杂：「中餐厅10」「喜单3」「Rock & Roast 3」「... Season 4」「... Ⅲ」
_ALIAS_TAIL_RE = re.compile(
    r"(?:\s*第\s*(?:[0-9]{1,3}|[一二两三四五六七八九十百]{1,4})\s*季"
    r"|\s*(?:[Ss]eason|SEASON)\s*[0-9IVXⅠ-Ⅹ]{1,4}"
    r"|\s*[0-9]{1,2}"
    r"|\s*[Ⅰ-Ⅹ]{1,3})\s*$"
)
_NUM_TAIL_RE = re.compile(r"\s*(?:[0-9]{1,4}|[Ⅰ-Ⅹ]{1,3})\s*$")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 证据权重（经 73 条真实豆瓣条目标定；门槛在 3~6 之间结论完全一致，不敏感）
_EV_AIR_DATE = 5  # 首播日精确对齐——最承重，去掉它整个方法崩塌
_EV_AIR_YEAR = 2  # 只对上年份（先导片/超前点映/深夜档跨日等场景的降级档）
_EV_SEASON_NO = 2  # 豆瓣季号与 TMDB 季号一致
_EV_EXACT_NAME = 1  # 候选名与豆瓣基名精确相等——国产综艺普遍有「纯享版」分身，
#                     正片与纯享版同季同首播日，全靠这一分选对正片
_EV_STUB_PENALTY = -4  # TMDB 上的残条目（整季只有 ≤1 集）几乎必是脏数据
_EV_THRESHOLD = 4  # 定案门槛：达标且严格高于第二名才算命中
_MAX_QUERIES = 8  # 多路查询上限
_MAX_POOL = 12  # 候选池上限（每个候选要拉一次季表，直接决定请求数）


def _parse_cn_int(text: str) -> int | None:
    """'12' / '十二' / '二十' / '五' → int；无法解析返回 None。"""
    if text.isdigit():
        return int(text)
    if "十" in text:
        tens, _, units = text.partition("十")
        if (tens and tens not in _CN_DIGITS) or (units and units not in _CN_DIGITS):
            return None
        return (_CN_DIGITS.get(tens, 1) if tens else 1) * 10 + (
            _CN_DIGITS.get(units, 0) if units else 0
        )
    return _CN_DIGITS.get(text)


def _split_douban_season(title: str) -> tuple[str, int | None]:
    """「中餐厅 第十季」→ ('中餐厅', 10)；无季号后缀原样返回 (title, None)。"""
    match = _SEASON_SUFFIX_RE.search(title)
    if not match:
        return title, None
    number = _parse_cn_int(match.group(1))
    if number is None or not 0 < number <= _MAX_SEASON_NUMBER:
        return title, None
    base = title[: match.start()].strip()
    # 基名为空说明整个标题就是后缀（如条目名直接叫「第一季」），剥了反而搜不到
    return (base, number) if base else (title, None)


def _douban_queries(title: str, base: str, aliases: Sequence[str]) -> list[str]:
    """生成多路查询词：基名、原标题、各别名（剥掉季号尾巴）、去掉裸尾数字的形态。

    豆瓣别名是这一层的关键补充——「乘风2026」的别名里写着「乘风破浪的姐姐
    第七季」，没有它根本搜不到正确的剧；实测别名一路能在「裸尾数字/年份品牌」
    类条目上把命中数从 6 抬到 11。
    """
    # 原标题无条件保留：它是兜底通路的同源输入，也是单字片名（《蝉》）唯一
    # 可用的查询词——长度守卫只该拦「剥出来的碎片」，不能把原标题本身滤掉
    seen: list[str] = [title.strip()]
    for raw in (base, *(_ALIAS_TAIL_RE.sub("", a).strip(" ·:：-") for a in aliases)):
        text = raw.strip()
        if len(text) >= 2 and text not in seen:
            seen.append(text)
    # 裸尾数字（「一饭封神2」「心脏信号5」）无法可靠判断是季号还是片名的一部分，
    # 因此不做取舍，两种形态都查，交给证据裁决
    for source in (title, base):
        stripped = _NUM_TAIL_RE.sub("", source).strip(" ·:：-")
        if len(stripped) >= 2 and stripped not in seen:
            seen.append(stripped)
    return seen[:_MAX_QUERIES]


def _douban_dates(released: str, year: int | None) -> tuple[set[str], set[str]]:
    """从豆瓣的上映串（形如「2026-06-19(中国大陆)」）提取可比对的日期与年份集合。"""
    dates = set(_ISO_DATE_RE.findall(released or ""))
    years = {d[:4] for d in dates}
    if year is not None:
        years.add(str(year))
    return dates, years


class _SeasonBrief(NamedTuple):
    """打分只需要季号、首播日与集数三个字段。"""

    season_number: int
    air_date: str
    episode_count: int


async def _season_briefs(client: TmdbClient, tmdb_id: int, language: str) -> list[_SeasonBrief]:
    """拉候选剧的季表；单次请求即可拿到全部季的首播日与集数。"""
    try:
        data = await client.get(f"tv/{tmdb_id}", {"language": language})
    except TmdbError:
        # 单个候选拉不到不该让整次收敛失败——它只是拿不到证据分而已
        return []
    return [
        _SeasonBrief(
            season_number=season["season_number"],
            air_date=season.get("air_date") or "",
            episode_count=season.get("episode_count") or 0,
        )
        for season in data.get("seasons") or []
        # 特别篇（season 0）不参与身份判定
        if season.get("season_number")
    ]


def _score_season(
    brief: _SeasonBrief, *, dates: set[str], years: set[str], season_hint: int | None
) -> int:
    """给候选剧的某一季打证据分。"""
    score = 0
    if brief.air_date and brief.air_date in dates:
        score += _EV_AIR_DATE
    elif brief.air_date[:4] and brief.air_date[:4] in years:
        score += _EV_AIR_YEAR
    if season_hint is not None and brief.season_number == season_hint:
        score += _EV_SEASON_NO
    if brief.episode_count <= 1:
        score += _EV_STUB_PENALTY
    return score


async def resolve_douban_to_tmdb(
    client: TmdbClient,
    kind: MediaKind,
    title: str,
    *,
    year: int | None = None,
    language: str = "zh-CN",
    aliases: Sequence[str] = (),
    released: str = "",
) -> DoubanResolution:
    """把豆瓣条目收敛到 TMDB 锚（证据优先，标题+年份兜底）。

    两条通路，证据通路先跑、兜底通路保持原行为不变：

    1. **证据通路**（需要调用方传入 ``aliases``/``released``）：多路查询生成候选，
       再用豆瓣首播日期与 TMDB 的季 air_date 对齐来定案。达到门槛且严格高于
       第二名才算命中，同时给出 TMDB 季号。它解决的是老通路根本搜不到的
       豆瓣季条目，也能识破 TMDB 上的残条目与「纯享版」分身。
    2. **兜底通路**（证据不足或调用方没传证据时）：沿用「标题+年份」的老规则，
       行为与改造前完全一致——保守优先，绝不静默错配。
    """
    # 证据通路只对剧集生效：豆瓣「按季拆条目」是剧集独有的问题，电影没有这个
    # 形态，也没有可对齐的季结构，保持改造前的行为不动
    evidenced = kind is MediaKind.TV and bool(aliases or released)
    base, season_hint = _split_douban_season(title) if evidenced else (title, None)
    queries = _douban_queries(title, base, aliases) if evidenced else [title]

    # 多路查询并发发出，整体延迟就是一次往返（客户端自带 20 QPS 漏桶护栏）
    responses = await asyncio.gather(
        *(client.get(f"search/{kind.value}", {"query": q, "language": language}) for q in queries),
        return_exceptions=True,
    )
    legacy: list[ResolveCandidate] = []
    pool: dict[int, ResolveCandidate] = {}
    for query, response in zip(queries, responses, strict=True):
        if isinstance(response, BaseException):
            continue
        found = [c for raw in response.get("results", []) if (c := _to_candidate(raw))]
        if query == title:
            legacy = found  # 兜底通路只认「整段标题」这一路，与改造前完全同源
        for candidate in found[:5]:
            pool.setdefault(candidate.tmdb_id, candidate)

    if not pool:
        return DoubanResolution(status=ResolveStatus.NOT_FOUND)

    dates, years = _douban_dates(released, year)
    if evidenced and (dates or years):
        matched = await _resolve_by_evidence(
            client,
            kind,
            list(pool.values())[:_MAX_POOL],
            base=base,
            season_hint=season_hint,
            dates=dates,
            years=years,
            language=language,
        )
        if matched is not None:
            return matched

    return _resolve_by_title(legacy or list(pool.values()), title=title, year=year)


async def _resolve_by_evidence(
    client: TmdbClient,
    kind: MediaKind,
    candidates: list[ResolveCandidate],
    *,
    base: str,
    season_hint: int | None,
    dates: set[str],
    years: set[str],
    language: str,
) -> DoubanResolution | None:
    """季级证据打分；分不出胜负时返回 None 交给兜底通路。"""
    wanted = _loose(base)
    scores: list[tuple[int, ResolveCandidate, int | None]] = []

    briefs = await asyncio.gather(
        *(_season_briefs(client, c.tmdb_id, language) for c in candidates)
    )
    for candidate, seasons in zip(candidates, briefs, strict=True):
        best_score, best_season = 0, None
        for brief in seasons:
            score = _score_season(brief, dates=dates, years=years, season_hint=season_hint)
            if score > best_score:
                best_score, best_season = score, brief.season_number
        if wanted in (_loose(candidate.title), _loose(candidate.original_title)):
            best_score += _EV_EXACT_NAME
        scores.append((best_score, candidate, best_season))

    scores.sort(key=lambda row: -row[0])
    top = scores[0]
    runner = scores[1][0] if len(scores) > 1 else -1
    if top[0] < _EV_THRESHOLD or top[0] <= runner:
        return None
    # 只有「季专属」的豆瓣条目才给预勾选建议：带显式季号后缀（「中餐厅 第十季」），
    # 或证据定案到了第一季之后（「问心2」→S2、「歌手2026」→S11）。整剧条目的
    # 首播日期天然对上第一季，若照此预勾选，会把原本的「全部已播季」窄化成一季
    season = top[2]
    suggested = season if season is not None and (season_hint is not None or season > 1) else None
    return DoubanResolution(
        status=ResolveStatus.MATCHED, tmdb_id=top[1].tmdb_id, suggested_season=suggested
    )


def _resolve_by_title(
    candidates: list[ResolveCandidate], *, title: str, year: int | None
) -> DoubanResolution:
    """兜底通路：改造前的「标题+年份」规则，逐条保持原判定不变。

    1. 年份过滤（容差 ±1，豆瓣与 TMDB 偶有跨年差异）后唯一 → 命中；
    2. 过滤后多个，但"标题精确相等且年份精确相等"者唯一 → 命中；
    3. 其余 → 歧义，返回候选让用户确认。
    """
    if not candidates:
        return DoubanResolution(status=ResolveStatus.NOT_FOUND)

    pool = candidates
    if year is not None:
        filtered = [c for c in candidates if c.year is not None and abs(c.year - year) <= 1]
        # 年份全部对不上时退回全量候选——豆瓣年份可能就是错的，交给用户判断
        pool = filtered or candidates

    if len(pool) == 1:
        return DoubanResolution(status=ResolveStatus.MATCHED, tmdb_id=pool[0].tmdb_id)

    if year is not None:
        wanted = _loose(title)
        exact = [
            c
            for c in pool
            if c.year == year and wanted in (_loose(c.title), _loose(c.original_title))
        ]
        if len(exact) == 1:
            return DoubanResolution(status=ResolveStatus.MATCHED, tmdb_id=exact[0].tmdb_id)

    return DoubanResolution(status=ResolveStatus.AMBIGUOUS, candidates=pool[:_MAX_CANDIDATES])


def _to_candidate(raw: dict) -> ResolveCandidate | None:
    tmdb_id = raw.get("id")
    title = raw.get("title") or raw.get("name") or ""
    if not tmdb_id or not title:
        return None
    return ResolveCandidate(
        tmdb_id=tmdb_id,
        title=title,
        original_title=raw.get("original_title") or raw.get("original_name") or title,
        year=_parse_year(raw.get("release_date") or raw.get("first_air_date") or ""),
        poster_path=raw.get("poster_path"),
    )


def _loose(text: str) -> str:
    """仅用于"精确相等"判定的宽松形态：忽略大小写与空白差异。"""
    return "".join(text.split()).casefold()
