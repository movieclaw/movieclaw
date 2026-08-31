"""原盘片源回填迁移的回归测试（issue #163）。

存量台账里的原盘行不会自愈——秒过行的增量刷新不重写 ``media_source``，
只有文件本体变化才会重新落值。所以这条一次性迁移是修复对存量库生效的
唯一路径，值得为它锁一次行为。
"""

from __future__ import annotations

import sqlite3

from alembic import command

from movieclaw_api.core.config import get_settings
from movieclaw_db.migrations import _build_config

# 回填迁移的前一版：先升到这里造存量数据，再升到 head 看回填结果
_BEFORE_BACKFILL = "a1f6c72b9d04"


def _insert_file(connection, *, path: str, container: str, source, manual: int) -> int:
    cursor = connection.execute(
        "INSERT INTO library_file "
        "(library_id, season_number, episode_number, file_path, size_bytes, "
        " container, media_source, media_source_manual, source, state, created_at, updated_at) "
        "VALUES (1, 0, 0, ?, 1, ?, ?, ?, 'scanned', 'in_place', "
        " '2026-08-31 00:00:00', '2026-08-31 00:00:00')",
        (path, container, source, manual),
    )
    return cursor.lastrowid


def test_backfill_marks_disc_rows_and_spares_manual(tmp_path, monkeypatch) -> None:
    """BDMV / VIDEO_TS / ISO 三种容器都回填成 Disc；人工标注行与普通文件不动。"""
    database = tmp_path / "disc-backfill.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    config = _build_config()
    command.upgrade(config, _BEFORE_BACKFILL)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO library "
            "(id, name, kind, root_paths, is_default, created_at, updated_at) "
            "VALUES (1, '电影库', 'movie', '[\"/media/movies\"]', 0, "
            "'2026-08-31 00:00:00', '2026-08-31 00:00:00')"
        )
        ids = {
            # 目录名解析出 Blu-ray 的 BDMV 原盘：正是被 Remux 洗掉的那一类
            "bdmv": _insert_file(
                connection, path="/media/movies/A BluRay", container="bluray",
                source="Blu-ray", manual=0,
            ),
            # ffprobe 读不了镜像，此前连片源都没有
            "iso": _insert_file(
                connection, path="/media/movies/B.iso", container="iso",
                source=None, manual=0,
            ),
            "dvd": _insert_file(
                connection, path="/media/movies/C", container="dvd", source=None, manual=0
            ),
            # 人工标注过的行：保护位的语义就是"自动写入不得覆盖"
            "manual": _insert_file(
                connection, path="/media/movies/D", container="bluray",
                source="user-lowest", manual=1,
            ),
            # 普通视频文件：与本次回填无关
            "plain": _insert_file(
                connection, path="/media/movies/E.mkv", container="mkv",
                source="WEB-DL", manual=0,
            ),
        }
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        rows = dict(
            connection.execute(
                "SELECT id, media_source FROM library_file"
            ).fetchall()
        )
    assert rows[ids["bdmv"]] == "Disc"
    assert rows[ids["iso"]] == "Disc"
    assert rows[ids["dvd"]] == "Disc"
    assert rows[ids["manual"]] == "user-lowest"
    assert rows[ids["plain"]] == "WEB-DL"
    get_settings.cache_clear()
