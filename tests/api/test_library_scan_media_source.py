"""扫描落库的片源取值（``scan.scanned_media_source``）测试。

覆盖：只写 Remux 不写片源词的命名要折进 media_source（Emby/MoviePilot 的
整理模板就是这种）、片源词已存在时不覆盖、非 Remux 与无信息命名保持原样，
以及折入后能被洗版阶梯判成 T5。
"""

from __future__ import annotations

import pytest

from movieclaw_api.services.library.scan import scanned_media_source
from movieclaw_enrich import enrich
from movieclaw_matcher.decision import source_tier


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # 只写 Remux、不写片源词：此前落库为"片源未知"，洗版基线不可比
        ("华尔街之狼 (2013) - 2160p Remux CHD", "Remux"),
        ("怪奇物语 S01E03 - 2160p Remux HEVC", "Remux"),
        # 片源词已存在：保留更具体的值，不被覆盖
        ("Casino Royale 2006 UHD BluRay REMUX 2160p HEVC DTS-HD MA5.1-CHD", "UHD Blu-ray"),
        ("Some Movie 2020 1080p WEB-DL DDP5.1 x264-GRP", "WEB-DL"),
        # 非 Remux 且无片源词：保持未知，绝不猜
        ("星河入梦 (2026) - 2160p H.265 CMCTV", None),
        ("片名 (2021)", None),
    ],
)
def test_scanned_media_source(name, expected):
    assert scanned_media_source(enrich(name)) == expected


def test_folded_remux_reaches_top_tier():
    """折入的 "Remux" 必须能被片源阶梯判成最高档——否则折了也白折。

    ``library_file`` 没有 remux 布尔列，快照从库文件行重建时 remux 恒为
    False，只能靠 media_source 取值表达档位。
    """
    value = scanned_media_source(enrich("华尔街之狼 (2013) - 2160p Remux CHD"))
    assert source_tier(value, False) == source_tier(None, True)


def test_no_regression_for_named_sources():
    """已有片源词的命名档位不变（回归保护）。"""
    for name in (
        "Some Movie 2020 1080p WEB-DL DDP5.1 x264-GRP",
        "Some Movie 2020 1080p BluRay x264-GRP",
    ):
        attrs = enrich(name)
        assert scanned_media_source(attrs) == attrs.media_source
