"""附属文件判定（``library.sidecar``）的测试。

覆盖：三种收录形态（``主文件名.`` / ``-thumb.`` / trickplay bif）、独立版本
的排除（同名异容器视频 / ``.iso`` 原盘镜像 / ``.strm`` 占位 / 多版本标签
文件）、未收录形态保持保守、目录不可读时的降级。
"""

from __future__ import annotations

import pytest

from movieclaw_api.services.library.sidecar import (
    SIDECAR_SKIP_EXTS,
    find_sidecars,
    sidecar_tail,
)

MAIN = "华尔街之狼 (2013).mkv"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # 通例：主文件名 + "."
        ("华尔街之狼 (2013).zh.srt", ".zh.srt"),
        ("华尔街之狼 (2013).nfo", ".nfo"),
        # Kodi/Emby 分集剧照
        ("华尔街之狼 (2013)-thumb.jpg", "-thumb.jpg"),
        # Emby/Jellyfin trickplay 预览索引：中缀是"宽度-间隔"，随配置变化
        ("华尔街之狼 (2013)-320-10.bif", "-320-10.bif"),
        ("华尔街之狼 (2013)-320.bif", "-320.bif"),
        ("华尔街之狼 (2013)-320-10.BIF", "-320-10.BIF"),
    ],
)
def test_recognised_sidecar_forms(tmp_path, name, expected):
    """三种收录形态都要认出来，并返回可直接拼接的尾巴。"""
    assert sidecar_tail(tmp_path / MAIN, tmp_path / name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "华尔街之狼 (2013).mp4",  # 同名异容器 = 另一个版本
        "华尔街之狼 (2013).iso",  # 原盘镜像 = 另一个版本
        "华尔街之狼 (2013).strm",  # 网盘占位 = 另一个版本
        "华尔街之狼 (2013).mkv",  # 主文件本身
        "华尔街之狼 (2013) - 2160p.mkv",  # 多版本标签文件，绝不能被当附属拖走
        "华尔街之狼 (2013)-fanart.jpg",  # 未收录形态：保守不搬，宁可留下也不误搬
        "别的片子.srt",
    ],
)
def test_not_sidecar(tmp_path, name):
    assert sidecar_tail(tmp_path / MAIN, tmp_path / name) is None


def test_strm_is_excluded_by_skip_exts():
    """``.strm`` 必须在排除集里——它是独立的网盘占位版本，不是附属。"""
    assert ".strm" in SIDECAR_SKIP_EXTS
    assert ".iso" in SIDECAR_SKIP_EXTS


def test_find_sidecars_scans_directory(tmp_path):
    """目录扫描：只挑出附属文件，按名排序，返回 (路径, 尾巴)。"""
    for name in (
        MAIN,
        "华尔街之狼 (2013).zh.srt",
        "华尔街之狼 (2013)-320-10.bif",
        "华尔街之狼 (2013).mp4",
        "无关文件.txt",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    found = find_sidecars(tmp_path / MAIN)
    assert [entry.name for entry, _ in found] == [
        "华尔街之狼 (2013)-320-10.bif",
        "华尔街之狼 (2013).zh.srt",
    ]
    assert [tail for _, tail in found] == ["-320-10.bif", ".zh.srt"]


def test_disc_dir_has_no_sidecars(tmp_path):
    """原盘目录（无扩展名）没有附属概念。"""
    disc = tmp_path / "华尔街之狼 (2013)"
    (disc / "BDMV").mkdir(parents=True)
    assert find_sidecars(disc) == []


def test_unreadable_dir_degrades_to_empty(tmp_path):
    """目录读不到时按"没有附属"处理，不让主流程整轮中断。"""
    assert find_sidecars(tmp_path / "不存在的目录" / MAIN) == []
