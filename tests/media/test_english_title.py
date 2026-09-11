"""国际英文名的提取（``_english_title``）——主动搜索的第一召回词。

起因是一例真实漏配：订阅韩语片《恶人传》，主动搜索拿 ``original_title``
（「악인전」）去打站点，召回 4 条且全被规则拒；换中文名搜有 70 条正片，
英文名 *The Gangster, the Cop, the Devil* 才是那些发布的片名段。
``original_title`` 是「原始语言标题」，不是英文名——这些用例钉死两者的区别。
"""

from __future__ import annotations

import pytest

from movieclaw_media.library import _english_title, _translation_index

# 结构照搬 TMDB /movie/{id}?append_to_response=translations,alternative_titles
GANGSTER = {
    "original_title": "악인전",
    "translations": {
        "translations": [
            {"iso_639_1": "zh", "data": {"title": "恶人传"}},
            {"iso_639_1": "en", "data": {"title": "The Gangster, the Cop, the Devil"}},
        ]
    },
    "alternative_titles": {
        "titles": [
            {"iso_3166_1": "CN", "title": "恶人传"},
            {"iso_3166_1": "US", "title": "The Gangster, the Cop, the Devil"},
        ]
    },
}


def _extract(payload: dict) -> str | None:
    return _english_title(payload, _translation_index(payload))


def test_non_english_film_yields_the_release_naming_title():
    """韩语片：原名召不回资源，英文名才是发布组用的片名段。"""
    assert _extract(GANGSTER) == "The Gangster, the Cop, the Devil"
    assert _extract(GANGSTER) != GANGSTER["original_title"]


def test_english_film_yields_its_own_title_for_dedup():
    """英语片的 en 译名就等于原名——调用方按原样文本去重时自然消掉，
    不会为同一个词多打一次站点。"""
    payload = {
        "original_title": "Dune: Part Two",
        "translations": {
            "translations": [
                {"iso_639_1": "en", "data": {"title": "Dune: Part Two"}},
                {"iso_639_1": "zh", "data": {"title": "沙丘2"}},
            ]
        },
    }
    assert _extract(payload) == payload["original_title"]


def test_falls_back_to_us_then_gb_region_alias():
    """en 译名槽为空是常态，地区别名补位（US 优先于 GB）。"""
    us_only = {
        "translations": {"translations": [{"iso_639_1": "en", "data": {"title": ""}}]},
        "alternative_titles": {
            "titles": [
                {"iso_3166_1": "GB", "title": "The Boy and the Heron"},
                {"iso_3166_1": "US", "title": "How Do You Live?"},
            ]
        },
    }
    assert _extract(us_only) == "How Do You Live?"

    gb_only = {
        "alternative_titles": {"titles": [{"iso_3166_1": "GB", "title": "The Boy and the Heron"}]}
    }
    assert _extract(gb_only) == "The Boy and the Heron"


def test_translation_wins_over_region_alias():
    """译名优先于地区别名——别名是用户投稿，质量不如译名（与
    ``_alt_region_title`` 的取向一致）。"""
    payload = {
        "translations": {"translations": [{"iso_639_1": "en", "data": {"title": "Shoplifters"}}]},
        "alternative_titles": {"titles": [{"iso_3166_1": "US", "title": "Shoplifting Family"}]},
    }
    assert _extract(payload) == "Shoplifters"


def test_tv_payload_uses_name_and_results_keys():
    """TMDB 的接口差异：剧集译名在 ``name``、地区别名在 ``results``。"""
    tv = {
        "translations": {"translations": [{"iso_639_1": "en", "data": {"name": "Squid Game"}}]},
    }
    assert _extract(tv) == "Squid Game"

    tv_alias = {"alternative_titles": {"results": [{"iso_3166_1": "US", "title": "Squid Game"}]}}
    assert _extract(tv_alias) == "Squid Game"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"translations": None, "alternative_titles": None},
        {"translations": {"translations": []}, "alternative_titles": {"titles": []}},
        {"translations": {"translations": [{"iso_639_1": "zh", "data": {"title": "恶人传"}}]}},
        {"alternative_titles": {"titles": [{"iso_3166_1": "CN", "title": "恶人传"}]}},
    ],
)
def test_missing_evidence_returns_none(payload):
    """证据不足一律 None——召回词集合据此少一个词，退回今天的行为。"""
    assert _extract(payload) is None
