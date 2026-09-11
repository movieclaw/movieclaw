"""ingest_entry.media_item_id 补外键迁移（e6b2d4f8a137）的回归测试。"""

from __future__ import annotations

import sqlite3

from alembic import command

from movieclaw_api.core.config import get_settings
from movieclaw_db.migrations import _build_config

_BEFORE = "d9f3b6a2e814"


def _insert(connection: sqlite3.Connection, table: str, **values) -> None:
    """插入一行，NOT NULL 且无默认值的列按类型补占位值——只关心被测的那几列。"""
    for _cid, name, col_type, notnull, default, _pk in connection.execute(
        f"PRAGMA table_info('{table}')"
    ):
        if name in values or not notnull or default is not None or name == "id":
            continue
        kind = (col_type or "").upper()
        values[name] = 0 if "INT" in kind or "BOOL" in kind else "[]" if "JSON" in kind else "x"
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(values.values()))


def test_fk_migration_clears_stale_references_and_nulls_on_delete(tmp_path, monkeypatch) -> None:
    """升级前的失效引用被置空（作品已删 / id 被复用给更晚建档的作品），有效引用
    保留；升级后删除作品由数据库自动置空。"""
    database = tmp_path / "ingest-fk.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    config = _build_config()
    command.upgrade(config, _BEFORE)

    with sqlite3.connect(database) as connection:
        early, reused_at = "2026-09-01 00:00:00", "2026-09-10 00:00:00"
        anchor = {"source": "tmdb"}
        _insert(
            connection,
            "media_item",
            id=1,
            kind="movie",
            title="有效",
            tmdb_id=1,
            external_id="1",
            created_at=early,
            updated_at=early,
            **anchor,
        )
        # id 2 被复用：作品建档时间晚于台账最后一次写入
        _insert(
            connection,
            "media_item",
            id=2,
            kind="video",
            title="复用",
            tmdb_id=2,
            external_id="2",
            created_at=reused_at,
            updated_at=reused_at,
            **anchor,
        )
        written = "2026-09-09 00:00:00"
        for path, media_item_id in (
            ("/w/valid", 1),
            ("/w/reused", 2),
            ("/w/gone", 99),
            ("/w/none", None),
        ):
            _insert(
                connection,
                "ingest_entry",
                entry_path=path,
                fingerprint="1:1:1",
                status="imported",
                media_item_id=media_item_id,
                attempted_at=written,
                created_at=written,
                updated_at=written,
            )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        refs = dict(connection.execute("SELECT entry_path, media_item_id FROM ingest_entry"))
        assert refs == {"/w/valid": 1, "/w/reused": None, "/w/gone": None, "/w/none": None}
        fks = {
            row[3]: (row[2], row[6])
            for row in connection.execute("PRAGMA foreign_key_list('ingest_entry')")
        }
        assert fks["media_item_id"] == ("media_item", "SET NULL")

        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM media_item WHERE id = 1")
        connection.commit()
        assert connection.execute(
            "SELECT media_item_id FROM ingest_entry WHERE entry_path = '/w/valid'"
        ).fetchone() == (None,)
    get_settings.cache_clear()
