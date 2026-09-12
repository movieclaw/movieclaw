"""NFO 解析缓存的正确性护栏（services/library/nfo._cached_parse）。

NFO 是读路径上唯一「每次都要回媒体盘」的一层：分层读约定本地 NFO 最优先
（docs/design/metadata.md 第 5 节），而它的展示内容从不落库，所以拿不到
数据库里去。详情页每打开一次读一份，分集区每打开一次把一季每集都读一遍。

缓存按 ``(mtime_ns, 文件大小)`` 失效，因此最该守住的是：

- **手改 NFO 下一次请求就生效**——这是「尊重既有刮削成果」的全部意义；
- 文件没了要立刻认，不能继续端出上一次的内容；
- 命中时发的是**副本**：详情装配会就地往演员表回填头像，缓存里的实例
  一旦被交出去改，下一个请求就会读到上一个请求的残留。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from movieclaw_api.services.library import nfo as nfo_mod
from movieclaw_api.services.library.nfo import (
    forget_parsed_nfo,
    read_entry_metadata,
    read_episode_metadata,
)

MOVIE = """<movie>
  <title>{title}</title>
  <plot>{plot}</plot>
  <genre>剧情</genre>
  <actor><name>演员甲</name><role>角色甲</role></actor>
</movie>
"""

EPISODE = """<episodedetails>
  <title>{title}</title>
  <plot>{plot}</plot>
  <season>1</season>
  <episode>1</episode>
</episodedetails>
"""


@pytest.fixture(autouse=True)
def _clean():
    forget_parsed_nfo()
    yield
    forget_parsed_nfo()


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_hit_skips_reparse(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path / "movie.nfo", MOVIE.format(title="甲", plot="原始简介"))
    assert read_entry_metadata(path).plot == "原始简介"

    calls: list[Path] = []
    real = nfo_mod._parse_entry_metadata

    def counting(p):  # noqa: ANN001
        calls.append(p)
        return real(p)

    monkeypatch.setattr(nfo_mod, "_parse_entry_metadata", counting)
    for _ in range(5):
        assert read_entry_metadata(path).plot == "原始简介"
    assert calls == [], "内容没变就不该重新读盘解析"


def test_edited_nfo_takes_effect_next_read(tmp_path: Path) -> None:
    path = _write(tmp_path / "movie.nfo", MOVIE.format(title="甲", plot="原始简介"))
    assert read_entry_metadata(path).plot == "原始简介"
    # 用户手改了 NFO（长度也变了，最常见的情形）
    _write(path, MOVIE.format(title="甲", plot="用户手改过的简介"))
    assert read_entry_metadata(path).plot == "用户手改过的简介"


def test_edited_nfo_with_same_size_takes_effect(tmp_path: Path) -> None:
    """等长改写也要认出来——靠 mtime_ns，不能只比大小。"""
    path = _write(tmp_path / "movie.nfo", MOVIE.format(title="甲", plot="简介一"))
    assert read_entry_metadata(path).plot == "简介一"
    before = path.stat()
    _write(path, MOVIE.format(title="甲", plot="简介二"))
    assert path.stat().st_size == before.st_size, "本用例的前提是两版等长"
    assert read_entry_metadata(path).plot == "简介二"


def test_deleted_nfo_reads_as_none(tmp_path: Path) -> None:
    path = _write(tmp_path / "movie.nfo", MOVIE.format(title="甲", plot="简介"))
    assert read_entry_metadata(path) is not None
    path.unlink()
    assert read_entry_metadata(path) is None
    # 再建回来（内容不同）也要立刻认
    _write(path, MOVIE.format(title="乙", plot="新简介"))
    assert read_entry_metadata(path).plot == "新简介"


def test_caller_mutation_does_not_leak(tmp_path: Path) -> None:
    """详情装配会往演员表就地写头像；那次改动绝不能渗进缓存。"""
    path = _write(tmp_path / "movie.nfo", MOVIE.format(title="甲", plot="简介"))
    first = read_entry_metadata(path)
    assert first.actors[0].thumb is None
    first.actors[0].thumb = "https://example.invalid/a.jpg"
    first.genres.append("被调用方加的")

    second = read_entry_metadata(path)
    assert second.actors[0].thumb is None, "缓存里的演员被上一个请求改脏了"
    assert second.genres == ["剧情"], "缓存里的列表被上一个请求改脏了"


def test_episode_nfo_cache_and_invalidation(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path / "S01E01.nfo", EPISODE.format(title="第一集", plot="简介"))
    assert read_episode_metadata(path).title == "第一集"

    calls: list[Path] = []
    real = nfo_mod._parse_episode_metadata
    monkeypatch.setattr(
        nfo_mod, "_parse_episode_metadata", lambda p: (calls.append(p), real(p))[1]
    )
    assert read_episode_metadata(path).title == "第一集"
    assert calls == []

    monkeypatch.undo()
    _write(path, EPISODE.format(title="改过的集名", plot="简介"))
    assert read_episode_metadata(path).title == "改过的集名"


def test_same_file_read_as_entry_and_episode_do_not_collide(tmp_path: Path) -> None:
    """多集合一的 NFO 两个口径都会读；两种解析结果不能互相顶掉。"""
    path = _write(tmp_path / "x.nfo", EPISODE.format(title="第一集", plot="简介"))
    assert read_entry_metadata(path) is None  # 根元素不是 movie/tvshow
    assert read_episode_metadata(path).title == "第一集"
    assert read_entry_metadata(path) is None


def test_cache_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(nfo_mod, "_PARSED_MAX", 4)
    for i in range(10):
        path = _write(tmp_path / f"{i}.nfo", MOVIE.format(title=f"片{i}", plot=f"简介{i}"))
        assert read_entry_metadata(path).plot == f"简介{i}"
    assert len(nfo_mod._PARSED) <= 4
    # 被淘汰的那几份照样读得回来（只是要重新解析）
    assert read_entry_metadata(tmp_path / "0.nfo").plot == "简介0"


def test_mtime_rollback_is_noticed(tmp_path: Path) -> None:
    """恢复备份会把 mtime 拨回去；键是「相等才命中」而不是「更新才失效」。"""
    path = _write(tmp_path / "movie.nfo", MOVIE.format(title="甲", plot="新版简介"))
    assert read_entry_metadata(path).plot == "新版简介"
    _write(path, MOVIE.format(title="甲", plot="旧版简介"))
    old = path.stat().st_mtime - 86_400
    os.utime(path, (old, old))
    assert read_entry_metadata(path).plot == "旧版简介"
