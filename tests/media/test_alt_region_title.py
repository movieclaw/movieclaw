"""主语言译名缺失时用同地区 alternative_titles 补位（``_alt_region_title``）。

TMDB 的译名与地区别名是两套独立数据。华语内容大量条目 ``zh-CN`` 译名槽为空，
API 静默退回原名（英文），而大陆通行译名躺在 ``alternative_titles`` 的 ``CN``
区里——这些用例用真实结构的载荷固定该行为。
"""

from __future__ import annotations

import pytest

from movieclaw_media.library import _alt_region_title

# 结构照搬 TMDB /movie/{id}?append_to_response=alternative_titles 的真实响应
SPIDER_MAN = {
    "alternative_titles": {
        "titles": [
            {"iso_3166_1": "HK", "title": "蜘蛛俠：不戰無歸"},
            {"iso_3166_1": "CN", "title": "蜘蛛侠：英雄无归"},
            {"iso_3166_1": "US", "title": "Spider-Man 3"},
        ]
    }
}


def test_picks_region_of_language_tag():
    assert _alt_region_title(SPIDER_MAN, "zh-CN") == "蜘蛛侠：英雄无归"
    assert _alt_region_title(SPIDER_MAN, "zh-HK") == "蜘蛛俠：不戰無歸"
    assert _alt_region_title(SPIDER_MAN, "en-US") == "Spider-Man 3"


def test_region_match_is_case_insensitive_on_tag():
    assert _alt_region_title(SPIDER_MAN, "zh-cn") == "蜘蛛侠：英雄无归"


def test_missing_region_returns_none():
    """该地区没有别名 → None，交回上层继续按语言优先级回落。"""
    assert _alt_region_title(SPIDER_MAN, "zh-SG") is None


def test_language_tag_without_region_returns_none():
    """``zh`` 这类没有地区段的标签无从定位地区。"""
    assert _alt_region_title(SPIDER_MAN, "zh") is None
    assert _alt_region_title(SPIDER_MAN, "") is None


def test_tv_payload_uses_results_key():
    """TMDB 的接口差异：电影用 ``titles``，剧集用 ``results``。"""
    tv = {"alternative_titles": {"results": [{"iso_3166_1": "CN", "title": "怪奇物语"}]}}
    assert _alt_region_title(tv, "zh-CN") == "怪奇物语"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"alternative_titles": None},
        {"alternative_titles": {}},
        {"alternative_titles": {"titles": []}},
        {"alternative_titles": {"titles": [{"iso_3166_1": "CN", "title": ""}]}},
        {"alternative_titles": {"titles": [{"iso_3166_1": "CN", "title": "   "}]}},
    ],
)
def test_empty_payloads_return_none(payload):
    """载荷缺失/为空/别名是空白串都要回 None，不能把空串当译名写进标题。"""
    assert _alt_region_title(payload, "zh-CN") is None
