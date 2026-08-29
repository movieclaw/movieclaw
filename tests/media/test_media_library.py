"""媒体身份层纯函数的单元测试：档案拉取、别名构建、豆瓣收敛三分支（MockTransport，不出网）。"""

from __future__ import annotations

import httpx

from movieclaw_media.library import (
    ResolveStatus,
    fetch_media_profile,
    list_image_candidates,
    pick_backdrop,
    pick_poster,
    resolve_douban_to_tmdb,
)
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"


def _client(routes: dict[str, dict], captured: list[httpx.Request] | None = None) -> TmdbClient:
    """按 URL path 路由返回固定 JSON 的假 TMDB。未注册的 path 一律 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        payload = routes.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=payload)

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


_MOVIE_DETAIL = {
    "id": 693134,
    "title": "沙丘2",
    "original_title": "Dune: Part Two",
    "release_date": "2024-02-27",
    "status": "Released",
    "poster_path": "/poster.jpg",
    "backdrop_path": "/backdrop.jpg",
    # 展示层（media_metadata）字段：有中文简介时不触发英文兜底的第二次请求
    "overview": "保罗·厄崔迪与弗雷曼人汇合，踏上复仇之路。",
    "tagline": "Long live the fighters.",
    "runtime": 167,
    "vote_average": 8.16,
    "vote_count": 5000,
    "original_language": "en",
    "genres": [{"id": 878, "name": "科幻"}, {"id": 12, "name": "冒险"}],
    "production_companies": [{"name": "Legendary Pictures"}],
    "production_countries": [{"iso_3166_1": "US"}],
    "release_dates": {
        "results": [
            {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
        ]
    },
    "credits": {
        "crew": [{"name": "Denis Villeneuve", "job": "Director"}],
        "cast": [
            {
                "name": "Timothée Chalamet",
                "character": "Paul",
                "order": 0,
                "profile_path": "/tc.jpg",
            },
            {"name": "Zendaya", "character": "Chani", "order": 1, "profile_path": None},
        ],
    },
    "external_ids": {"imdb_id": "tt15239678"},
    "alternative_titles": {
        "titles": [
            {"iso_3166_1": "CN", "title": "沙丘：第二部"},
            {"iso_3166_1": "US", "title": "Dune Part 2"},
            {"iso_3166_1": "FR", "title": "Dune Deuxième Partie"},  # 不在收集范围
            {"iso_3166_1": "HK", "title": "沙丘瀚战：第二章"},
        ]
    },
    "translations": {
        "translations": [
            {"iso_639_1": "zh", "data": {"title": "沙丘2"}},  # 与主标题重复，应去重
            {"iso_639_1": "en", "data": {"title": "Dune: Part Two"}},  # 与原名重复
            {"iso_639_1": "ja", "data": {"title": "デューン 砂の惑星PART2"}},  # 不收集
        ]
    },
}


async def test_fetch_movie_profile_fields_and_aliases() -> None:
    """电影档案：字段齐全；别名=主标题+原名+指定地区/语言，跨来源精确去重。"""
    client = _client({"/3/movie/693134": _MOVIE_DETAIL})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)

    assert profile.imdb_id == "tt15239678"
    assert profile.title == "沙丘2"
    assert profile.original_title == "Dune: Part Two"
    assert profile.year == 2024
    assert profile.status == "Released"
    assert profile.poster_path == "/poster.jpg"
    assert profile.seasons == []
    assert profile.aliases == [
        "沙丘2",
        "Dune: Part Two",
        "沙丘：第二部",
        "Dune Part 2",
        "沙丘瀚战：第二章",
    ]
    # 展示层字段（media_metadata 的数据源）随同一次请求拉齐
    assert profile.overview == "保罗·厄崔迪与弗雷曼人汇合，踏上复仇之路。"
    assert profile.tagline == "Long live the fighters."
    assert profile.genres == ["科幻", "冒险"]
    assert profile.runtime_minutes == 167
    assert profile.content_rating == "PG-13"
    assert profile.vote_average == 8.2
    assert profile.studios == ["Legendary Pictures"]
    assert profile.origin_countries == ["US"]
    assert profile.directors == ["Denis Villeneuve"]
    assert [c.name for c in profile.cast] == ["Timothée Chalamet", "Zendaya"]
    assert profile.cast[0].character == "Paul"


async def test_fetch_movie_uses_append_to_response() -> None:
    """整个电影建档只发一次请求：别名/译名/外部 ID/演职员/候选图/分级全走
    append_to_response 合并（有中文简介时不触发英文兜底）。"""
    captured: list[httpx.Request] = []
    client = _client({"/3/movie/693134": _MOVIE_DETAIL}, captured)
    await fetch_media_profile(client, MediaKind.MOVIE, 693134)

    assert len(captured) == 1
    params = dict(captured[0].url.params)
    assert params["append_to_response"] == (
        "alternative_titles,translations,external_ids,credits,images,release_dates"
    )
    assert params["language"] == "zh-CN"
    # 选图策略要的是"无文字"(null) 与中英文候选，不带这个参数 images 几乎必空
    assert params["include_image_language"] == "null,zh,en"


async def test_fetch_movie_overview_falls_back_via_translations() -> None:
    """主语言简介缺失时按语言优先级从 translations 本地回落——**零额外请求**
    （docs/design/scrape-customization.md §2.1，替代旧的补拉 en-US 兜底）。"""
    detail = {
        **_MOVIE_DETAIL,
        "overview": "",
        "tagline": "",
        "translations": {
            "translations": [
                {"iso_639_1": "zh", "iso_3166_1": "CN", "data": {"title": "沙丘2"}},
                {
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "data": {
                        "title": "Dune: Part Two",
                        "overview": "Paul seeks revenge.",
                        "tagline": "EN tagline",
                    },
                },
            ]
        },
    }
    captured: list[httpx.Request] = []
    client = _client({"/3/movie/693134": detail}, captured)
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert len(captured) == 1
    assert profile.overview == "Paul seeks revenge."
    assert profile.tagline == "EN tagline"


async def test_fetch_movie_title_prefers_same_region_alias_when_primary_untranslated() -> None:
    """主语言无译名时先取**同地区别名**（CN），而不是直接落到下一语言。

    TMDB 的译名与地区别名是两套独立数据，华语内容常见 zh-CN 译名缺席、
    CN 区别名却齐全；此时给简体用户回英文是明显更差的选择。
    """
    detail = {
        **_MOVIE_DETAIL,
        "title": "Dune: Part Two",  # TMDB 无 zh 翻译时静默退回原名
        "translations": {
            "translations": [
                {
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "data": {"title": "Dune: Part Two", "overview": "x"},
                },
            ]
        },
    }
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(
        client, MediaKind.MOVIE, 693134, languages=["zh-CN", "en-US"]
    )
    # _MOVIE_DETAIL 的 alternative_titles 里有 CN 区别名
    assert profile.title == "沙丘：第二部"


async def test_fetch_movie_title_falls_back_when_primary_untranslated() -> None:
    """主语言既无译名、同地区也无别名时，才按优先级取下一语言的译名。"""
    detail = {
        **_MOVIE_DETAIL,
        "title": "Dune: Part Two",  # TMDB 无 zh 翻译时静默退回原名
        # CN 区别名一并去掉，只留无关地区——回落必须继续走到 en-US
        "alternative_titles": {"titles": [{"iso_3166_1": "FR", "title": "Dune Deuxième Partie"}]},
        "translations": {
            "translations": [
                {
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "data": {"title": "Dune: Part Two", "overview": "x"},
                },
            ]
        },
    }
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(
        client, MediaKind.MOVIE, 693134, languages=["zh-CN", "en-US"]
    )
    assert profile.title == "Dune: Part Two"


async def test_fetch_movie_title_keeps_translated_title_without_translations_payload() -> None:
    """顶层 title 已是译名（≠ 原名）时不许回落——回落会把好数据换成差数据。"""
    detail = {**_MOVIE_DETAIL, "translations": {"translations": []}}
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(
        client, MediaKind.MOVIE, 693134, languages=["zh-CN", "en-US"]
    )
    assert profile.title == "沙丘2"


_TV_DETAIL = {
    "id": 94997,
    "name": "龙之家族",
    "original_name": "House of the Dragon",
    "first_air_date": "2022-08-21",
    "status": "Returning Series",
    "poster_path": "/tv.jpg",
    "backdrop_path": None,
    "external_ids": {"imdb_id": "tt11198330"},
    # 剧集的 alternative_titles 用 "results" 键（TMDB 接口差异）
    "alternative_titles": {"results": [{"iso_3166_1": "TW", "title": "龍族前傳"}]},
    "translations": {"translations": []},
    "seasons": [
        {"season_number": 0},
        {"season_number": 1},
        {"season_number": 2},
    ],
}

_SEASONS = {
    "/3/tv/94997/season/0": {"name": "特别篇", "air_date": None, "episodes": []},
    "/3/tv/94997/season/1": {
        "name": "第 1 季",
        "air_date": "2022-08-21",
        "episodes": [
            {"episode_number": 1, "name": "龙之继承人", "air_date": "2022-08-21"},
            {"episode_number": 2, "name": "反叛的王子", "air_date": "2022-08-28"},
        ],
    },
    "/3/tv/94997/season/2": {
        "name": "第 2 季",
        "air_date": "2024-06-16",
        "episodes": [
            {"episode_number": 1, "name": "黑色之子", "air_date": "2024-06-16"},
            {"episode_number": 2, "name": None, "air_date": None},  # 未定档集
        ],
    },
}


async def test_fetch_tv_profile_with_seasons_and_episodes() -> None:
    """剧集档案：季按季号齐全（含特别季 0），集列表带播出日期，tv 别名键兼容。"""
    client = _client({"/3/tv/94997": _TV_DETAIL, **_SEASONS})
    profile = await fetch_media_profile(client, MediaKind.TV, 94997)

    assert profile.title == "龙之家族"
    assert profile.year == 2022
    assert "龍族前傳" in profile.aliases
    assert [s.season_number for s in profile.seasons] == [0, 1, 2]

    season1 = profile.seasons[1]
    assert season1.episode_count == 2
    assert season1.episodes[0].air_date == "2022-08-21"
    # 未定档集：air_date 为 None 而非假日期
    assert profile.seasons[2].episodes[1].air_date is None
    assert profile.seasons[2].episodes[1].name == ""


# ---------------------------------------------------------------------------
# 选图策略（docs/design/metadata.md 6.3）
# ---------------------------------------------------------------------------


def _img(path: str, *, lang: str | None, width: int, avg: float, count: int) -> dict:
    return {
        "file_path": path,
        "iso_639_1": lang,
        "width": width,
        "height": int(width * 9 / 16),
        "vote_average": avg,
        "vote_count": count,
    }


def test_pick_backdrop_prefers_textless() -> None:
    """背景首选无文字图：带片名文字的横图铺全屏很脏，哪怕它票数更高。"""
    data = {
        "images": {
            "backdrops": [
                _img("/with-text.jpg", lang="en", width=3840, avg=9.0, count=500),
                _img("/clean.jpg", lang=None, width=1920, avg=6.0, count=40),
            ]
        }
    }
    assert pick_backdrop(data) == "/clean.jpg"


def test_pick_backdrop_weighted_score_beats_low_vote_outlier() -> None:
    """加权票数：1 票 10 分的冷门图不该盖过几百票的公认好图。"""
    data = {
        "images": {
            "backdrops": [
                _img("/outlier.jpg", lang=None, width=1920, avg=10.0, count=1),
                _img("/popular.jpg", lang=None, width=1920, avg=7.0, count=400),
            ]
        }
    }
    assert pick_backdrop(data) == "/popular.jpg"


def test_pick_backdrop_falls_back_when_all_below_width() -> None:
    """候选全都低于分辨率门槛时不放弃——宁可给小图也不要没有图。"""
    data = {"images": {"backdrops": [_img("/small.jpg", lang=None, width=1280, avg=8.0, count=9)]}}
    assert pick_backdrop(data) == "/small.jpg"


def test_pick_backdrop_none_without_candidates() -> None:
    """没有候选返回 None（调用方回落 TMDB 默认 backdrop_path）。"""
    assert pick_backdrop({"images": {"backdrops": []}}) is None
    assert pick_backdrop({}) is None


def test_pick_poster_prefers_localized() -> None:
    """海报相反：**要**文字，中文版比英文原版更符合中文用户预期。"""
    data = {
        "images": {
            "posters": [
                _img("/en.jpg", lang="en", width=2000, avg=9.5, count=900),
                _img("/zh.jpg", lang="zh", width=1000, avg=5.0, count=10),
            ]
        }
    }
    assert pick_poster(data, ["zh"]) == "/zh.jpg"
    # 当前语言没有海报时退回全部候选里的最优
    assert pick_poster(data, ["ja"]) == "/en.jpg"


def test_list_candidates_ordering_matches_auto_pick() -> None:
    """弹层首张 == 自动策略会选的那张（用户一眼看出默认给的是哪张），
    且带文字/非本地化的候选仍在列表里可选。"""
    data = {
        "images": {
            "posters": [
                _img("/p-en.jpg", lang="en", width=2000, avg=9.0, count=800),
                _img("/p-zh.jpg", lang="zh", width=1000, avg=6.0, count=30),
            ],
            "backdrops": [
                _img("/b-text.jpg", lang="en", width=3840, avg=9.5, count=700),
                _img("/b-clean.jpg", lang=None, width=1920, avg=6.5, count=50),
            ],
        }
    }
    posters, backdrops = list_image_candidates(data, ["zh"], [None])
    assert posters[0]["file_path"] == pick_poster(data, ["zh"]) == "/p-zh.jpg"
    assert backdrops[0]["file_path"] == pick_backdrop(data) == "/b-clean.jpg"
    assert {i["file_path"] for i in posters} == {"/p-zh.jpg", "/p-en.jpg"}
    assert {i["file_path"] for i in backdrops} == {"/b-clean.jpg", "/b-text.jpg"}


async def test_fetch_profile_poster_default_backdrop_picked() -> None:
    """海报以 TMDB 默认为准（与发现页看到的一致，订阅前后不跳变）；
    背景仍从候选里按无文字策略重选。"""
    detail = {
        **_MOVIE_DETAIL,
        "poster_path": "/default-poster.jpg",
        "backdrop_path": "/default-backdrop.jpg",
        "images": {
            "posters": [_img("/picked-poster.jpg", lang="zh", width=1000, avg=7.0, count=50)],
            "backdrops": [_img("/picked-backdrop.jpg", lang=None, width=1920, avg=7.0, count=50)],
        },
    }
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert profile.poster_path == "/default-poster.jpg"
    assert profile.backdrop_path == "/picked-backdrop.jpg"


async def test_fetch_profile_poster_falls_back_to_pick() -> None:
    """TMDB 默认海报缺失时才走选图策略从候选里兜底。"""
    detail = {
        **_MOVIE_DETAIL,
        "poster_path": None,
        "images": {
            "posters": [_img("/picked-poster.jpg", lang="zh", width=1000, avg=7.0, count=50)],
        },
    }
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert profile.poster_path == "/picked-poster.jpg"


async def test_fetch_profile_falls_back_to_default_images() -> None:
    """条目没有 images 候选时回落 TMDB 默认字段，不会把图丢成 None。"""
    client = _client({"/3/movie/693134": _MOVIE_DETAIL})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert profile.poster_path == "/poster.jpg"
    assert profile.backdrop_path == "/backdrop.jpg"


# ---------------------------------------------------------------------------
# 豆瓣收敛三分支
# ---------------------------------------------------------------------------


def _search_result(*items: dict) -> dict:
    return {"results": list(items)}


def _movie(tmdb_id: int, title: str, original: str, year: int) -> dict:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": original,
        "release_date": f"{year}-01-01",
        "poster_path": "/p.jpg",
    }


async def test_resolve_matched_when_unique_after_year_filter() -> None:
    """年份过滤后唯一 → 直接命中，无需用户确认。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "沙丘2", "Dune: Part Two", 2024),
                _movie(2, "沙丘", "Dune", 1984),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "沙丘2", year=2024)
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 1


async def test_resolve_matched_by_exact_title_and_year_among_many() -> None:
    """过滤后仍多个，但标题+年份精确相等者唯一 → 命中。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "小丑", "Joker", 2019),
                _movie(2, "小丑回魂", "It", 2019),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "小丑", year=2019)
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 1


async def test_resolve_ambiguous_returns_candidates() -> None:
    """无法唯一判定 → 歧义，候选交给弹层确认，绝不静默错配。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "机器人总动员", "WALL·E", 2008),
                _movie(2, "机器人总动员2", "WALL·E 2", 2008),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "机器人", year=2008)
    assert result.status is ResolveStatus.AMBIGUOUS
    assert [c.tmdb_id for c in result.candidates] == [1, 2]


async def test_resolve_not_found() -> None:
    """TMDB 未收录 → not_found（上层据此拒绝创建无锚条目）。"""
    client = _client({"/3/search/movie": _search_result()})
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "极冷门条目", year=2001)
    assert result.status is ResolveStatus.NOT_FOUND
    assert result.candidates == []


async def test_resolve_year_mismatch_falls_back_to_all_candidates() -> None:
    """年份全对不上时退回全量候选做歧义确认——豆瓣年份可能有误。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "某片", "Film A", 2010),
                _movie(2, "某片", "Film B", 2015),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "某片", year=1990)
    assert result.status is ResolveStatus.AMBIGUOUS
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# 语言优先级与选图偏好（docs/design/scrape-customization.md）
# ---------------------------------------------------------------------------


async def test_fetch_tv_season_fallback_when_primary_coverage_poor() -> None:
    """主语言分集大面积是占位名时，该季按次位语言整季重拉一次并合并译文；
    覆盖率正常的季不触发额外请求。"""
    detail = {
        "id": 94997,
        "name": "某剧",
        "original_name": "Some Show",
        "first_air_date": "2022-08-21",
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    }
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/3/tv/94997":
            return httpx.Response(200, json=detail)
        if request.url.path == "/3/tv/94997/season/1":
            if dict(request.url.params).get("language") == "en-US":
                return httpx.Response(
                    200,
                    json={
                        "name": "Season 1",
                        "episodes": [
                            {"episode_number": 1, "name": "Pilot", "overview": "EN ov"},
                            {"episode_number": 2, "name": "Second", "overview": ""},
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "name": "第 1 季",
                    "episodes": [
                        {"episode_number": 1, "name": "第 1 集", "overview": ""},
                        {"episode_number": 2, "name": "Episode 2", "overview": ""},
                    ],
                },
            )
        return httpx.Response(404, json={})

    client = TmdbClient(_KEY, transport=httpx.MockTransport(handler))
    profile = await fetch_media_profile(
        client, MediaKind.TV, 94997, languages=["zh-CN", "en-US"]
    )
    episodes = profile.seasons[0].episodes
    assert [e.name for e in episodes] == ["Pilot", "Second"]
    assert episodes[0].overview == "EN ov"
    # 详情 1 次 + 季主语言 1 次 + 季回落 1 次
    assert len(captured) == 3


async def test_fetch_profile_poster_language_mode() -> None:
    """poster_mode=language 时按语言优先级挑海报（不再以 TMDB 默认为准）。"""
    from movieclaw_media.library import ImagePrefs

    detail = {
        **_MOVIE_DETAIL,
        "poster_path": "/default-poster.jpg",
        "images": {
            "posters": [
                _img("/zh-poster.jpg", lang="zh", width=1000, avg=6.0, count=40),
                _img("/en-poster.jpg", lang="en", width=2000, avg=9.0, count=900),
            ],
        },
    }
    client = _client({"/3/movie/693134": detail})
    prefs = ImagePrefs(poster_mode="language", poster_langs=("meta", "en", "null"))
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134, image_prefs=prefs)
    assert profile.poster_path == "/zh-poster.jpg"


async def test_fetch_profile_backdrop_language_tiers() -> None:
    """背景语言优先级：把语言排在「无文字」前面时选带字图（收藏 logo 横图）。"""
    from movieclaw_media.library import ImagePrefs

    detail = {
        **_MOVIE_DETAIL,
        "images": {
            "backdrops": [
                _img("/clean.jpg", lang=None, width=3840, avg=9.0, count=500),
                _img("/logo-en.jpg", lang="en", width=1920, avg=6.0, count=40),
            ],
        },
    }
    client = _client({"/3/movie/693134": detail})
    prefs = ImagePrefs(backdrop_langs=("en", "null"))
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134, image_prefs=prefs)
    assert profile.backdrop_path == "/logo-en.jpg"


async def test_fetch_profile_orig_token_triggers_extra_images_fetch() -> None:
    """偏好含「原始语言」且详情请求未覆盖该语言时，补拉一次 images 合并候选。"""
    from movieclaw_media.library import ImagePrefs

    detail = {
        **_MOVIE_DETAIL,
        "original_language": "ko",
        "poster_path": None,
        "images": {"posters": [_img("/en-poster.jpg", lang="en", width=1000, avg=8.0, count=100)]},
    }
    extra_images = {
        "posters": [_img("/ko-poster.jpg", lang="ko", width=1000, avg=7.0, count=50)],
        "backdrops": [],
    }
    captured: list[httpx.Request] = []
    client = _client(
        {"/3/movie/693134": detail, "/3/movie/693134/images": extra_images}, captured
    )
    prefs = ImagePrefs(poster_mode="language", poster_langs=("orig", "en"))
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134, image_prefs=prefs)
    assert profile.poster_path == "/ko-poster.jpg"
    assert len(captured) == 2


async def test_fetch_profile_certification_priority() -> None:
    """分级按配置的地区优先级取值。"""
    detail = {
        **_MOVIE_DETAIL,
        "release_dates": {
            "results": [
                {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
                {"iso_3166_1": "JP", "release_dates": [{"certification": "G"}]},
            ]
        },
    }
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(
        client, MediaKind.MOVIE, 693134, cert_countries=["JP", "US"]
    )
    assert profile.content_rating == "G"


def test_resolve_image_languages_tokens() -> None:
    """token 解析：meta/orig/null/具体码，保序去重，orig 缺失时跳过。"""
    from movieclaw_media.library import resolve_image_languages

    assert resolve_image_languages(
        ("meta", "orig", "en", "null"), primary_language="zh-CN", original_language="ja"
    ) == ["zh", "ja", "en", None]
    assert resolve_image_languages(
        ("orig", "meta", "zh"), primary_language="zh-CN", original_language=None
    ) == ["zh"]


# ---------------------------------------------------------------------------
# 豆瓣季条目的证据收敛
#
# 用例取自真实数据（73 条豆瓣条目的对照实验）里各自暴露出的行为，
# 每条对应一个曾经真实错掉或漏掉的场景。
# ---------------------------------------------------------------------------


def _tv_client(searches: dict[str, dict], shows: dict[int, dict]) -> TmdbClient:
    """按 search 的 query 参数与 tv/{id} 路由的假 TMDB（多路查询必须区分 query）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/search/tv":
            query = request.url.params.get("query", "")
            return httpx.Response(200, json=_search_result(*searches.get(query, [])))
        if path.startswith("/3/tv/"):
            show = shows.get(int(path.rsplit("/", 1)[1]))
            if show is not None:
                return httpx.Response(200, json=show)
        return httpx.Response(404, json={})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


def _tv(tmdb_id: int, name: str, first_air: str = "2020-01-01") -> dict:
    return {"id": tmdb_id, "name": name, "original_name": name, "first_air_date": first_air}


def _show(*seasons: tuple[int, str, int]) -> dict:
    """(季号, 首播日, 集数) → tv/{id} 详情里的 seasons 结构。"""
    return {
        "seasons": [
            {"season_number": n, "air_date": air, "episode_count": count}
            for n, air, count in seasons
        ]
    }


async def test_resolve_douban_season_entry_by_air_date() -> None:
    """豆瓣「中餐厅 第十季」：整段标题搜不到，剥出基名后靠首播日对齐命中并解出季号。"""
    client = _tv_client(
        searches={"中餐厅 第十季": [], "中餐厅": [_tv(91914, "中餐厅", "2017-07-22")]},
        shows={91914: _show((9, "2025-06-20", 12), (10, "2026-06-19", 12))},
    )
    result = await resolve_douban_to_tmdb(
        client,
        MediaKind.TV,
        "中餐厅 第十季",
        year=2026,
        aliases=["中餐厅10"],
        released="2026-06-19(中国大陆)",
    )
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 91914
    assert result.suggested_season == 10


async def test_resolve_douban_season_number_differs_from_tmdb() -> None:
    """豆瓣季号不等于 TMDB 季号（实测 13% 如此）：以首播日对齐的那一季为准。

    只认豆瓣季号会让用户默认勾到差好几年的内容——真实案例是《奔跑吧 第十季》
    对应 TMDB 的 S14，勾 S10 拿到的是四年前那一季。
    """
    client = _tv_client(
        searches={"奔跑吧 第十季": [], "奔跑吧": [_tv(98031, "奔跑吧", "2014-10-10")]},
        shows={98031: _show((10, "2022-05-13", 12), (14, "2026-04-24", 12))},
    )
    result = await resolve_douban_to_tmdb(
        client,
        MediaKind.TV,
        "奔跑吧 第十季",
        year=2026,
        aliases=[],
        released="2026-04-24(中国大陆)",
    )
    assert result.status is ResolveStatus.MATCHED
    assert result.suggested_season == 14


async def test_resolve_douban_uses_aliases_to_find_show() -> None:
    """裸尾数字条目靠豆瓣别名找到正确的剧：「乘风2026」的别名写着「乘风破浪的姐姐 第七季」。"""
    client = _tv_client(
        searches={
            "乘风2026": [_tv(317948, "乘风2026", "2026-04-02")],
            "乘风破浪的姐姐": [_tv(104716, "乘风破浪的姐姐", "2020-06-12")],
            "乘风": [],
        },
        shows={
            317948: _show((1, "2026-04-02", 29)),
            104716: _show((6, "2025-03-21", 24), (7, "2026-04-03", 28)),
        },
    )
    result = await resolve_douban_to_tmdb(
        client,
        MediaKind.TV,
        "乘风2026",
        year=2026,
        aliases=["乘风破浪的姐姐 第七季"],
        released="2026-04-03(中国大陆)",
    )
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 104716
    assert result.suggested_season == 7


async def test_resolve_douban_rejects_stub_entry() -> None:
    """TMDB 上只有 1 集的残条目要被压过：改造前《诛仙 第四季》正是错配到这种脏数据。"""
    client = _tv_client(
        searches={
            "诛仙 第四季": [_tv(332444, "诛仙第四季")],
            "诛仙": [_tv(206484, "诛仙", "2022-08-02"), _tv(332444, "诛仙第四季")],
        },
        shows={
            206484: _show((4, "2026-08-21", 26)),
            # 残条目：季号与豆瓣一致、首播日缺失，只有 1 集
            332444: _show((4, "", 1)),
        },
    )
    result = await resolve_douban_to_tmdb(
        client,
        MediaKind.TV,
        "诛仙 第四季",
        year=2026,
        aliases=["诛仙4"],
        released="2026-08-21(中国大陆)",
    )
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 206484


async def test_resolve_douban_prefers_main_cut_over_clean_version() -> None:
    """正片与「纯享版」同季同首播日时，靠标题精确同名选中正片。"""
    client = _tv_client(
        searches={
            "喜剧之王单口季 第三季": [],
            "喜剧之王单口季": [
                _tv(292210, "喜剧之王单口季·纯享版"),
                _tv(261391, "喜剧之王单口季"),
            ],
        },
        shows={
            261391: _show((3, "2026-07-03", 41)),
            292210: _show((3, "2026-07-03", 20)),
        },
    )
    result = await resolve_douban_to_tmdb(
        client,
        MediaKind.TV,
        "喜剧之王单口季 第三季",
        year=2026,
        aliases=["喜单3"],
        released="2026-07-03(中国大陆)",
    )
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 261391


async def test_resolve_douban_whole_series_gives_no_season_suggestion() -> None:
    """整剧条目（普通剧名、证据落在第一季）不给预勾选建议，保持「全部已播季」的默认。"""
    client = _tv_client(
        searches={"早春晴朗": [_tv(299952, "早春晴朗", "2026-01-05")]},
        shows={299952: _show((1, "2026-01-05", 24))},
    )
    result = await resolve_douban_to_tmdb(
        client,
        MediaKind.TV,
        "早春晴朗",
        year=2026,
        aliases=[],
        released="2026-01-05(中国大陆)",
    )
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 299952
    assert result.suggested_season is None


async def test_resolve_douban_falls_back_to_title_rules_without_evidence() -> None:
    """证据分不出胜负时回落「标题+年份」老通路，判定与改造前一致。"""
    client = _tv_client(
        searches={"某剧": [_tv(1, "某剧", "2020-01-01"), _tv(2, "某剧外传", "2020-01-01")]},
        # 两个候选都没有能对上豆瓣首播日的季 → 证据通路弃权
        shows={1: _show((1, "2019-05-05", 10)), 2: _show((1, "2019-06-06", 10))},
    )
    result = await resolve_douban_to_tmdb(
        client, MediaKind.TV, "某剧", year=2020, aliases=[], released="2020-03-03(中国大陆)"
    )
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 1  # 标题精确相等 + 年份精确相等者唯一
    assert result.suggested_season is None
