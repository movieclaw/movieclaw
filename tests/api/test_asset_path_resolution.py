"""资产路径解析与其缓存（services/media_scrape.resolve_asset_path）。

``/images/assets/<相对路径>`` 与 Jellyfin 的条目图片接口共用这一道判定，它
既是**防目录穿越的闸门**，也是海报墙上每张图都要走一遍的热路径。缓存加在
这里，所以安全性质必须钉死：

- 穿越路径任何时候都不能放过，且**不进缓存**（否则构造一堆越界路径就能撑内存）；
- 缓存键带资产根，换了 METADATA_DIR 不会串到上一份的结果上。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import media_scrape


@pytest.fixture(autouse=True)
def _isolated_assets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()
    media_scrape._resolved_assets_root.cache_clear()
    media_scrape._ASSET_PATHS.clear()
    root = media_scrape.assets_root()
    root.mkdir(parents=True, exist_ok=True)
    yield root
    media_scrape._ASSET_PATHS.clear()
    media_scrape._resolved_assets_root.cache_clear()
    get_settings.cache_clear()


def test_normal_path_resolves_inside_root(_isolated_assets: Path) -> None:
    (_isolated_assets / "12").mkdir()
    (_isolated_assets / "12" / "poster.jpg").write_bytes(b"x")
    target = media_scrape.resolve_asset_path("12/poster.jpg")
    assert target == (_isolated_assets / "12" / "poster.jpg").resolve()


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "../../etc/passwd",
        "12/../../secret.txt",
        "./../../secret.txt",
    ],
)
def test_traversal_is_refused(_isolated_assets: Path, path: str) -> None:
    (_isolated_assets.parent.parent / "secret.txt").write_bytes(b"x")
    assert media_scrape.resolve_asset_path(path) is None
    # 失败结果不进缓存：否则可以用大量越界路径把内存撑起来
    assert media_scrape._ASSET_PATHS == {}


def test_symlink_escape_is_refused(_isolated_assets: Path) -> None:
    outside = _isolated_assets.parent.parent / "outside"
    outside.mkdir()
    (outside / "secret.jpg").write_bytes(b"x")
    os.symlink(outside, _isolated_assets / "link")
    assert media_scrape.resolve_asset_path("link/secret.jpg") is None


def test_repeat_calls_do_not_resolve_again(_isolated_assets: Path, monkeypatch) -> None:
    (_isolated_assets / "7").mkdir()
    (_isolated_assets / "7" / "poster.jpg").write_bytes(b"x")
    first = media_scrape.resolve_asset_path("7/poster.jpg")

    calls: list[str] = []
    real = Path.resolve

    def counting_resolve(self, *args, **kwargs):  # noqa: ANN001
        calls.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    for _ in range(5):
        assert media_scrape.resolve_asset_path("7/poster.jpg") == first
    assert calls == [], "命中缓存就不该再 resolve（那是每级目录一次系统调用）"


def test_cache_is_keyed_by_assets_root(tmp_path: Path, monkeypatch) -> None:
    """换了 METADATA_DIR 必须得到新根下的路径，不能串到上一份缓存。"""
    first_root = media_scrape.assets_root()
    (first_root / "3").mkdir(parents=True)
    (first_root / "3" / "poster.jpg").write_bytes(b"x")
    assert media_scrape.resolve_asset_path("3/poster.jpg").is_relative_to(first_root)

    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "other"))
    get_settings.cache_clear()
    second_root = media_scrape.assets_root()
    (second_root / "3").mkdir(parents=True)
    (second_root / "3" / "poster.jpg").write_bytes(b"y")
    target = media_scrape.resolve_asset_path("3/poster.jpg")
    assert target.is_relative_to(second_root.resolve())
    assert not target.is_relative_to(first_root)
