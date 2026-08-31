"""扫描落库的片源取值（``scan.scanned_media_source``）测试。

覆盖：只写 Remux 不写片源词的命名要折进 media_source（Emby/MoviePilot 的
整理模板就是这种）、片源词已存在时不覆盖、非 Remux 与无信息命名保持原样，
折入后能被洗版阶梯判成 T5，以及原盘容器按结构判 T6（issue #163）。
"""

from __future__ import annotations

import pytest

from movieclaw_api.services.library.scan import scanned_media_source
from movieclaw_enrich import enrich
from movieclaw_matcher import DISC_SOURCE
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


@pytest.mark.parametrize("container", ["bluray", "dvd", "iso"])
def test_disc_container_wins_over_name(container):
    """原盘容器（BDMV/VIDEO_TS/ISO）按结构判 T6，压过名称里的一切片源词。

    此前一张完整原盘最好也只能落成 Blu-ray(T4)，低于 Remux(T5)——而 Remux
    正是从这张盘剥出来的，于是一个 Remux 候选会被判成升级，把原盘洗进
    回收站（issue #163）。
    """
    attrs = enrich("Casino Royale 2006 UHD BluRay REMUX 2160p HEVC-CHD")
    assert scanned_media_source(attrs, container) == DISC_SOURCE
    assert source_tier(DISC_SOURCE, False) > source_tier("Remux", False)


def test_non_disc_container_keeps_name_parsed_source():
    """普通视频文件不受影响：容器是 mkv/mp4 时仍走名称解析那条路。"""
    attrs = enrich("Some Movie 2020 1080p WEB-DL DDP5.1 x264-GRP")
    assert scanned_media_source(attrs, "mkv") == "WEB-DL"
